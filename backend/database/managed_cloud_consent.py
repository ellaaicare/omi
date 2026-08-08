"""Serialized PostgreSQL authority for managed-cloud consent."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from database import authority_advisory_lock, voice_canary
from database.ella_provisioning import invalidate_self_hosted_authority_on_connection
from database.runtime_targets import SELF_HOSTED_RUNTIME_TARGET_MODES

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


def invitation_consent_receipt_ref(uid: str, receipt_id: str) -> str:
    material = f"ella-invitation-consent-receipt-v1\x1f{uid}\x1f{receipt_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
    owner_lock: authority_advisory_lock.AuthorityLockProof,
) -> None:
    await authority_advisory_lock.require_self_owner_lock(
        conn,
        owner_lock,
        user_id=user_id,
    )
    await invalidate_self_hosted_authority_on_connection(
        conn,
        uid=uid,
        user_id=user_id,
        reason=reason,
        owner_lock=owner_lock,
    )
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
    owner: authority_advisory_lock.AuthorityOwner,
    owner_lock: authority_advisory_lock.AuthorityLockProof,
) -> uuid.UUID:
    """Lock the exact grant epoch after the caller acquires the v1 owner lock."""
    grant.validate()
    user_id = await authority_advisory_lock.verify_self_owner_after_lock(
        conn,
        uid=grant.account_uid,
        owner=owner,
        proof=owner_lock,
    )
    await authority_advisory_lock.require_self_owner_lock(
        conn,
        owner_lock,
        user_id=user_id,
    )
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
    allow_fresh_uid_bootstrap: bool = False,
    bootstrap_email: str = "",
) -> dict[str, Any]:
    """Publish a Firestore grant into the PostgreSQL ordering authority.

    When ``allow_fresh_uid_bootstrap`` is set, a UID with no ``users`` row gets a
    deterministic ``users`` row created inside the advisory-lock transaction so a
    fresh self-hosted account can complete consent (the reversible relax flag).
    Without it, the strict ``authority_lock_owner_missing`` behavior is preserved.
    """
    grant.validate()
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as conn:
            resolution = await authority_advisory_lock.resolve_self_owner_unlocked_or_bootstrap(
                conn,
                uid=grant.account_uid,
                allow_bootstrap=allow_fresh_uid_bootstrap,
                bootstrap_email=bootstrap_email,
            )
            async with conn.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    conn,
                    owner=resolution.owner,
                )
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid=grant.account_uid,
                )
                user_id = await authority_advisory_lock.verify_self_owner_after_lock_or_bootstrap(
                    conn,
                    uid=grant.account_uid,
                    resolution=resolution,
                    proof=owner_lock,
                    allow_bootstrap=allow_fresh_uid_bootstrap,
                    bootstrap_email=bootstrap_email,
                )
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
                elif not _grant_matches(row, grant):
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
                        owner_lock=owner_lock,
                    )
                if row is None or not _grant_matches(row, grant):
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_grant_failed")
                pending_count = int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM voice_entitlements
                        WHERE uid = $1
                          AND invitation_id IS NOT NULL
                          AND invitation_consent_pending = TRUE
                        """,
                        grant.account_uid,
                    )
                    or 0
                )
                if pending_count > 1:
                    raise ManagedCloudAuthorityUnavailable("invitation_consent_binding_ambiguous")
                if pending_count == 1:
                    entitlement = await conn.fetchrow(
                        """
                        UPDATE voice_entitlements
                        SET consent_authority_epoch = $2,
                            invitation_consent_pending = FALSE,
                            revision = revision + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE uid = $1
                          AND invitation_id IS NOT NULL
                          AND invitation_consent_pending = TRUE
                          AND consent_policy_version = $3
                          AND consent_processor_set_hash = $4
                          AND consent_scope_version = $5
                          AND consent_scope_hash = $6
                        RETURNING invitation_id, revision
                        """,
                        grant.account_uid,
                        row["authority_epoch"],
                        grant.policy_version,
                        grant.processor_set_hash,
                        grant.scope_version,
                        grant.scope_hash,
                    )
                    if not entitlement:
                        raise ManagedCloudAuthorityUnavailable("invitation_consent_lineage_stale")
                    redemption = await conn.fetchrow(
                        """
                        UPDATE ella_invitation_redemptions
                        SET consent_pending = FALSE,
                            consent_receipt_ref_hmac = $3
                        WHERE invitation_id = $1
                          AND user_id = $2
                          AND consent_pending = TRUE
                        RETURNING id
                        """,
                        entitlement["invitation_id"],
                        user_id,
                        invitation_consent_receipt_ref(
                            grant.account_uid,
                            grant.consent_receipt_id,
                        ),
                    )
                    if not redemption:
                        raise ManagedCloudAuthorityUnavailable("invitation_consent_redemption_missing")
                    target_result = await conn.execute(
                        """
                        UPDATE ella_runtime_targets target
                        SET entitlement_revision = $3,
                            updated_at = CURRENT_TIMESTAMP
                        FROM ella_invitation_redemptions redemption
                        WHERE redemption.invitation_id = $1
                          AND redemption.user_id = $2
                          AND target.invitation_target_id = redemption.invitation_target_id
                          AND target.provider = 'hermes'
                          AND target.status = 'reserved'
                        """,
                        entitlement["invitation_id"],
                        user_id,
                        entitlement["revision"],
                    )
                    if target_result != f"UPDATE {len(SELF_HOSTED_RUNTIME_TARGET_MODES)}":
                        raise ManagedCloudAuthorityUnavailable("invitation_runtime_target_missing")
                return dict(row)
    except ManagedCloudAuthorityUnavailable:
        raise
    except Exception as exc:
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc


async def unlink_self_owner_account_on_deletion(*, uid: str) -> None:
    """Unlink a confirmed account from its data and free the UID for re-admission.

    Runs under the same per-UID authority advisory lock that gates every other
    writer. Clears the FK dependents (consent authority, entitlement/runtime
    links) then deletes the ``users`` row so the same Firebase UID's next login
    bootstraps a *fresh* account (Plato's "fresh account on relogin" semantic),
    rather than resuming the old one. Idempotent and a no-op when no ``users``
    row exists — so the deletion receipt is only issued when the server state
    was actually unlinked.
    """
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as conn:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                conn,
                uid=uid,
            )
            async with conn.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    conn,
                    owner=owner,
                )
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid=uid,
                )
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    conn,
                    uid=uid,
                    owner=owner,
                    proof=owner_lock,
                )
                # Clear the FK dependents under the lock before freeing omi_uid.
                await _quarantine_on_connection(
                    conn,
                    uid=uid,
                    user_id=user_id,
                    reason="account_deletion_confirmed",
                    owner_lock=owner_lock,
                )
                # ella_invitation_redemptions.user_id is ON DELETE RESTRICT; detach it.
                await conn.execute(
                    """
                    UPDATE ella_invitation_redemptions
                    SET user_id = NULL
                    WHERE user_id = $1
                    """,
                    user_id,
                )
                await conn.execute(
                    """
                    DELETE FROM users
                    WHERE id = $1
                    """,
                    user_id,
                )
    except authority_advisory_lock.AuthorityLockError as exc:
        if exc.code == "authority_lock_owner_missing":
            # Nothing server-side to unlink; the receipt stays accurate.
            return
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc
    except ManagedCloudAuthorityUnavailable:
        raise
    except Exception as exc:
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc


async def synchronize_denial(
    *,
    uid: str,
    decision: Literal["declined", "revoked"],
    verified_email: str = "",
) -> dict[str, Any]:
    """Order denial before Firestore and quarantine all durable Cloud authority."""
    try:
        pool = await voice_canary.get_pool()
        async with pool.acquire() as conn:
            email = verified_email.strip().lower()
            if email:
                resolution = await authority_advisory_lock.resolve_identity_owner_unlocked(
                    conn,
                    uid=uid,
                    email=email,
                )
            else:
                try:
                    owner = await authority_advisory_lock.resolve_self_owner_unlocked(
                        conn,
                        uid=uid,
                    )
                    resolution = authority_advisory_lock.IdentityOwnerResolution(
                        owner=owner,
                        allow_create=False,
                    )
                except authority_advisory_lock.AuthorityLockError as exc:
                    if exc.code != "authority_lock_owner_missing":
                        raise
                    resolution = authority_advisory_lock.IdentityOwnerResolution(
                        owner=authority_advisory_lock.provisional_identity_owner(uid),
                        allow_create=True,
                    )
            async with conn.transaction():
                owner_lock = await authority_advisory_lock.acquire_authority_lock(
                    conn,
                    owner=resolution.owner,
                )
                await voice_canary.lock_runtime_authority_on_connection(
                    conn,
                    uid=uid,
                )
                identity_rows = await authority_advisory_lock.verify_identity_owner_after_lock(
                    conn,
                    uid=uid,
                    email=email,
                    resolution=resolution,
                    proof=owner_lock,
                )
                by_uid = next((row for row in identity_rows if row["omi_uid"] == uid), None)
                by_email = next(
                    (row for row in identity_rows if email and str(row["email"] or "").strip().lower() == email),
                    None,
                )
                identity_row = by_uid or by_email
                if identity_row is None:
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
                    return {
                        "decision": decision,
                        "authority_absent": True,
                    }

                if by_uid is None and by_email is not None:
                    identity_row = await conn.fetchrow(
                        """
                        UPDATE users
                        SET omi_uid = $1,
                            identities = COALESCE(identities, '{}'::jsonb)
                                || jsonb_build_object('omi_uid', $1::text, 'email', $2::text),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $3
                          AND (omi_uid IS NULL OR omi_uid = '' OR omi_uid = $1)
                        RETURNING id, omi_uid, email, name, timezone, status
                        """,
                        uid,
                        email,
                        resolution.owner.account_id,
                    )
                    if identity_row is None:
                        raise authority_advisory_lock.AuthorityLockError("authority_lock_owner_drift")

                existing_uid = str(identity_row["omi_uid"] or "").strip()
                if existing_uid and existing_uid != uid:
                    raise authority_advisory_lock.AuthorityLockError("authority_lock_identity_conflict")
                if by_uid is not None and email:
                    stored_email = str(by_uid["email"] or "").strip().lower()
                    if stored_email != email:
                        raise authority_advisory_lock.AuthorityLockError("authority_lock_uid_email_mismatch")
                user_id = uuid.UUID(str(identity_row["id"]))
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
                    owner_lock=owner_lock,
                )
                if row is None or row["decision"] != decision:
                    raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_denial_failed")
                return dict(row)
    except ManagedCloudAuthorityUnavailable:
        raise
    except Exception as exc:
        raise ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable") from exc
