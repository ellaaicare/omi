"""Serialized PostgreSQL authority for managed-cloud consent."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from database import voice_canary

AuthorityDecision = Literal["granted", "declined", "revoked"]


class ManagedCloudAuthorityDenied(RuntimeError):
    pass


class ManagedCloudAuthorityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedCloudGrant:
    account_uid: str
    profile_uid: str
    consent_receipt_id: str
    profile_binding_id: str
    policy_version: str
    processor_set_hash: str
    scope_version: str
    scope_hash: str

    @classmethod
    def from_mapping(
        cls,
        uid: str,
        value: Any,
    ) -> "ManagedCloudGrant":
        mapping = value if isinstance(value, dict) else {}
        grant = cls(
            account_uid=uid,
            profile_uid=uid,
            consent_receipt_id=str(mapping.get("receipt_id") or ""),
            profile_binding_id=str(mapping.get("profile_binding_id") or ""),
            policy_version=str(mapping.get("policy_version") or ""),
            processor_set_hash=str(mapping.get("processor_set_hash") or ""),
            scope_version=str(mapping.get("scope_version") or ""),
            scope_hash=str(mapping.get("scope_hash") or ""),
        )
        grant.validate()
        return grant

    def validate(self) -> None:
        if (
            not self.account_uid
            or self.account_uid != self.profile_uid
            or not all(
                (
                    self.consent_receipt_id,
                    self.profile_binding_id,
                    self.policy_version,
                    self.processor_set_hash,
                    self.scope_version,
                    self.scope_hash,
                )
            )
        ):
            raise ManagedCloudAuthorityDenied("managed_cloud_authority_incomplete")


def consent_receipt_ref(uid: str, receipt_id: str) -> str:
    material = f"ella-managed-cloud-consent-receipt-v1\x1f{uid}\x1f{receipt_id}"
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _grant_matches(row: asyncpg.Record, grant: ManagedCloudGrant) -> bool:
    return bool(
        row["decision"] == "granted"
        and hmac.compare_digest(
            str(row["consent_receipt_ref"] or ""),
            consent_receipt_ref(grant.account_uid, grant.consent_receipt_id),
        )
        and hmac.compare_digest(
            str(row["profile_binding_id"] or ""),
            grant.profile_binding_id,
        )
        and hmac.compare_digest(
            str(row["policy_version"] or ""),
            grant.policy_version,
        )
        and hmac.compare_digest(
            str(row["processor_set_hash"] or ""),
            grant.processor_set_hash,
        )
        and hmac.compare_digest(
            str(row["scope_version"] or ""),
            grant.scope_version,
        )
        and hmac.compare_digest(
            str(row["scope_hash"] or ""),
            grant.scope_hash,
        )
    )


async def _quarantine_on_connection(
    conn: asyncpg.Connection,
    *,
    uid: str,
    user_id: uuid.UUID,
    reason: str,
) -> None:
    await conn.execute(
        """
        UPDATE voice_entitlements
        SET status = 'revoked',
            revision = revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE uid = $1
          AND status <> 'revoked'
        """,
        uid,
    )
    await conn.execute(
        "DELETE FROM voice_active_sessions WHERE uid = $1",
        uid,
    )
    await conn.execute(
        """
        UPDATE ella_runtime_targets
        SET status = 'revoked',
            revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        WHERE account_user_id = $1
          AND profile_user_id = $1
          AND provider = 'hermes_cloud'
          AND status = 'ready'
        """,
        user_id,
    )
    await conn.execute(
        """
        UPDATE ella_runtime_bindings
        SET status = 'quarantined',
            active = false,
            health_state = 'unhealthy',
            health_receipt = $2::jsonb,
            quarantine_reason = $3,
            quarantined_at = COALESCE(quarantined_at, CURRENT_TIMESTAMP),
            claim_lease_expires_at = NULL,
            revision = revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = $1
          AND provider = 'hermes_cloud'
          AND status IN ('claiming', 'shadow', 'internal_canary', 'active')
        """,
        user_id,
        json.dumps(
            {
                "content_free": True,
                "reason": reason,
            }
        ),
        reason,
    )


async def lock_or_bootstrap_grant_on_connection(
    conn: asyncpg.Connection,
    *,
    grant: ManagedCloudGrant,
) -> uuid.UUID:
    """Lock the exact grant epoch for the remainder of the caller transaction."""
    grant.validate()
    user_id = await conn.fetchval(
        "SELECT id FROM users WHERE omi_uid = $1 FOR UPDATE",
        grant.account_uid,
    )
    if not user_id:
        raise ManagedCloudAuthorityDenied("managed_cloud_authority_user_missing")
    row = await conn.fetchrow(
        """
        SELECT *
        FROM ella_managed_cloud_consent_authority
        WHERE user_id = $1
        FOR UPDATE
        """,
        user_id,
    )
    if row is None:
        row = await conn.fetchrow(
            """
            INSERT INTO ella_managed_cloud_consent_authority (
                user_id, decision, consent_receipt_ref, profile_binding_id,
                policy_version, processor_set_hash, scope_version, scope_hash
            ) VALUES ($1, 'granted', $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            user_id,
            consent_receipt_ref(grant.account_uid, grant.consent_receipt_id),
            grant.profile_binding_id,
            grant.policy_version,
            grant.processor_set_hash,
            grant.scope_version,
            grant.scope_hash,
        )
    if row is None or not _grant_matches(row, grant):
        raise ManagedCloudAuthorityDenied("managed_cloud_authority_stale")
    return uuid.UUID(str(row["authority_epoch"]))


async def synchronize_grant(
    *,
    grant: ManagedCloudGrant,
) -> dict[str, Any]:
    """Publish a Firestore grant into the PostgreSQL ordering authority."""
    grant.validate()
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid=grant.account_uid,
                )
                user_id = await conn.fetchval(
                    "SELECT id FROM users WHERE omi_uid = $1 FOR UPDATE",
                    grant.account_uid,
                )
                if not user_id:
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_user_missing")
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if row is not None and _grant_matches(row, grant):
                    return dict(row)
                if row is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ella_managed_cloud_consent_authority (
                            user_id, decision, consent_receipt_ref,
                            profile_binding_id, policy_version,
                            processor_set_hash, scope_version, scope_hash
                        ) VALUES ($1, 'granted', $2, $3, $4, $5, $6, $7)
                        RETURNING *
                        """,
                        user_id,
                        consent_receipt_ref(
                            grant.account_uid,
                            grant.consent_receipt_id,
                        ),
                        grant.profile_binding_id,
                        grant.policy_version,
                        grant.processor_set_hash,
                        grant.scope_version,
                        grant.scope_hash,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE ella_managed_cloud_consent_authority
                        SET decision = 'granted',
                            consent_receipt_ref = $2,
                            profile_binding_id = $3,
                            policy_version = $4,
                            processor_set_hash = $5,
                            scope_version = $6,
                            scope_hash = $7,
                            authority_epoch = gen_random_uuid(),
                            revision = revision + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = $1
                        RETURNING *
                        """,
                        user_id,
                        consent_receipt_ref(
                            grant.account_uid,
                            grant.consent_receipt_id,
                        ),
                        grant.profile_binding_id,
                        grant.policy_version,
                        grant.processor_set_hash,
                        grant.scope_version,
                        grant.scope_hash,
                    )
                    await _quarantine_on_connection(
                        conn,
                        uid=grant.account_uid,
                        user_id=user_id,
                        reason="managed_cloud_consent_grant_changed",
                    )
                if row is None or not _grant_matches(row, grant):
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_grant_failed")
                return dict(row)
    except ManagedCloudAuthorityUnavailable:
        raise
    except Exception as exc:
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc


async def synchronize_denial(
    *,
    uid: str,
    decision: Literal["declined", "revoked"],
) -> dict[str, Any]:
    """Order denial before Firestore and quarantine all durable Cloud authority."""
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid=uid,
                )
                user_id = await conn.fetchval(
                    "SELECT id FROM users WHERE omi_uid = $1 FOR UPDATE",
                    uid,
                )
                if not user_id:
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_user_missing")
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if row is None:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO ella_managed_cloud_consent_authority (
                            user_id, decision
                        ) VALUES ($1, $2)
                        RETURNING *
                        """,
                        user_id,
                        decision,
                    )
                elif row["decision"] != decision:
                    row = await conn.fetchrow(
                        """
                        UPDATE ella_managed_cloud_consent_authority
                        SET decision = $2,
                            consent_receipt_ref = NULL,
                            profile_binding_id = NULL,
                            policy_version = NULL,
                            processor_set_hash = NULL,
                            scope_version = NULL,
                            scope_hash = NULL,
                            authority_epoch = gen_random_uuid(),
                            revision = revision + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = $1
                        RETURNING *
                        """,
                        user_id,
                        decision,
                    )
                await _quarantine_on_connection(
                    conn,
                    uid=uid,
                    user_id=user_id,
                    reason=f"managed_cloud_consent_{decision}",
                )
                if row is None or row["decision"] != decision:
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_denial_failed")
                return dict(row)
    except ManagedCloudAuthorityUnavailable:
        raise
    except Exception as exc:
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc
