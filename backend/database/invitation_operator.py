"""Root-CLI database contract for one disposable synthetic invitation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from database import authority_advisory_lock, invitations, voice_canary

OPERATOR_PURPOSE = "hermes_cloud_synthetic_prototype"
OPERATOR_POLICY_REVISION = invitations.SYNTHETIC_OPERATOR_POLICY_REVISION
OPERATOR_POOL_KEY = "synthetic_operator"
SAFE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FORBIDDEN_IDENTITIES = {
    "plato-eval",
    "plato_eval",
    "real-crypto-plato",
    "real_crypto_plato",
    "realcryptoplato",
}
MIN_EXPIRY = timedelta(minutes=5)
MAX_EXPIRY = timedelta(hours=24)
OPERATOR_POLICY = invitations.SYNTHETIC_OPERATOR_ENTITLEMENT_POLICY
RECOVERY_BINDING_VERSION = "synthetic-invitation-recovery-v1"


class SyntheticInvitationOperatorError(RuntimeError):
    """A content-free operator refusal safe to surface on stderr."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SyntheticInvitationIdentity:
    uid: str
    account_uid: str
    profile_uid: str

    def validate(self) -> None:
        values = (self.uid, self.account_uid, self.profile_uid)
        if (
            any(not isinstance(value, str) or not value or len(value) > 256 for value in values)
            or len(set(values)) != 1
            or not self.uid.startswith(("synthetic-", "staging-synthetic-"))
            or any(value.strip().lower() in FORBIDDEN_IDENTITIES for value in values)
        ):
            raise SyntheticInvitationOperatorError("operator_identity_refused")


@dataclass(frozen=True)
class SyntheticInvitationContext:
    environment: str
    expected_database: str
    operator: str

    def validate(self) -> None:
        if not all(
            SAFE_CONTEXT_RE.fullmatch(value) for value in (self.environment, self.expected_database, self.operator)
        ):
            raise SyntheticInvitationOperatorError("operator_context_invalid")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SyntheticInvitationOperatorError("operator_expiry_timezone_required")
    return value.astimezone(timezone.utc)


def _operator_source(context: SyntheticInvitationContext) -> str:
    return f"synthetic-invite-operator:{context.environment}:{context.operator}"


def synthetic_invitation_recovery_binding_hmac(
    *,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    admission: invitations.InvitationPilotAdmission,
    code: str,
    code_file_ref_hmac: str,
    expires_at: datetime,
    config: invitations.InvitationConfig,
) -> str:
    """Bind a protected-file recovery receipt to the complete issuance intent."""
    identity.validate()
    context.validate()
    normalized_code = invitations.normalize_invite_code(code)
    expiry = _utc(expires_at)
    if (
        not normalized_code
        or not isinstance(admission, invitations.InvitationPilotAdmission)
        or admission.account_uid != identity.account_uid
        or admission.profile_uid != identity.profile_uid
        or not re.fullmatch(r"[0-9a-f]{64}", code_file_ref_hmac)
    ):
        raise SyntheticInvitationOperatorError("operator_issue_contract_invalid")
    payload = json.dumps(
        {
            "account_uid": identity.account_uid,
            "code_file_ref_hmac": code_file_ref_hmac,
            "code_hmac": invitations.code_hmac(config, normalized_code),
            "consent_policy_version": admission.policy_version,
            "consent_processor_set_hash": admission.processor_set_hash,
            "consent_receipt_id": admission.consent_receipt_id,
            "consent_scope_hash": admission.scope_hash,
            "consent_scope_version": admission.scope_version,
            "environment": context.environment,
            "expected_database": context.expected_database,
            "expires_at": expiry.isoformat(),
            "operator": context.operator,
            "policy_revision": OPERATOR_POLICY_REVISION,
            "profile_binding_id": admission.profile_binding_id,
            "profile_uid": identity.profile_uid,
            "purpose": OPERATOR_PURPOSE,
            "uid": identity.uid,
            "version": RECOVERY_BINDING_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        config.hmac_pepper,
        b"synthetic-invitation-recovery-v1\x1f" + payload,
        hashlib.sha256,
    ).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _safe_receipt(row: dict[str, Any], *, profile_class: str, idempotent: bool = False) -> dict[str, Any]:
    expires_at = row.get("expires_at")
    return {
        "receipt_id": str(row["id"]),
        "purpose": OPERATOR_PURPOSE,
        "required_profile_class": "synthetic",
        "current_profile_class": profile_class,
        "state": str(row["state"]),
        "delivery_state": str(row["delivery_state"]),
        "redemption_count": int(row["redemption_count"]),
        "max_redemptions": int(row["max_redemptions"]),
        "expires_at": _utc(expires_at).isoformat() if expires_at else None,
        "version": int(row["version"]),
        "idempotent": idempotent,
        "content_free": True,
    }


async def _database_guard(
    conn: asyncpg.Connection,
    context: SyntheticInvitationContext,
) -> None:
    actual_database = str(await conn.fetchval("SELECT current_database()") or "")
    if not hmac.compare_digest(actual_database, context.expected_database):
        raise SyntheticInvitationOperatorError("operator_database_mismatch")


async def _lock_owner(
    conn: asyncpg.Connection,
    identity: SyntheticInvitationIdentity,
    owner: authority_advisory_lock.AuthorityOwner,
) -> authority_advisory_lock.AuthorityLockProof:
    proof = await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
    await voice_canary.lock_runtime_authority_on_connection(conn, uid=identity.uid)
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"synthetic-invitation-operator-v1:{identity.uid}",
    )
    return proof


async def _locked_user(
    conn: asyncpg.Connection,
    identity: SyntheticInvitationIdentity,
    owner: authority_advisory_lock.AuthorityOwner,
    proof: authority_advisory_lock.AuthorityLockProof,
) -> dict[str, Any]:
    await authority_advisory_lock.require_authority_lock(conn, proof, owner=owner)
    row = await conn.fetchrow(
        """
        SELECT id, omi_uid, status, profile_class
        FROM users
        WHERE omi_uid = $1
        FOR UPDATE
        """,
        identity.uid,
    )
    if not row or row["id"] != owner.account_id or owner.account_id != owner.profile_id:
        raise SyntheticInvitationOperatorError("operator_identity_drift")
    user = dict(row)
    if str(user["status"]) != "ACTIVE":
        raise SyntheticInvitationOperatorError("operator_user_not_active")
    return user


async def _artifact_snapshot(
    conn: asyncpg.Connection,
    *,
    user_id: uuid.UUID,
    uid: str,
) -> dict[str, bool]:
    row = await conn.fetchrow(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM voice_entitlements WHERE uid = $2
            ) AS voice_entitlement,
            EXISTS (
                SELECT 1 FROM ella_provisioning_jobs WHERE user_id = $1
            ) AS provisioning_job,
            EXISTS (
                SELECT 1
                FROM ella_runtime_bindings
                WHERE user_id = $1
                   OR account_user_id = $1
                   OR profile_user_id = $1
            ) AS runtime_binding,
            EXISTS (
                SELECT 1
                FROM ella_runtime_targets
                WHERE account_user_id = $1 OR profile_user_id = $1
            ) AS runtime_target,
            EXISTS (
                SELECT 1
                FROM ella_managed_cloud_consent_authority
                WHERE user_id = $1
            ) AS consent_authority
        """,
        user_id,
        uid,
    )
    if not row:
        raise SyntheticInvitationOperatorError("operator_artifact_check_unavailable")
    return {key: bool(value) for key, value in dict(row).items()}


def _assert_pristine_artifacts(artifacts: dict[str, bool]) -> None:
    if any(artifacts.values()):
        raise SyntheticInvitationOperatorError("operator_existing_profile_artifacts")


async def _operator_rows_for_target(
    conn: asyncpg.Connection,
    *,
    account_ref_hmac: str,
    profile_ref_hmac: str,
    for_update: bool,
) -> list[dict[str, Any]]:
    lock_clause = "FOR UPDATE OF i, t, r" if for_update else ""
    rows = await conn.fetch(
        f"""
        SELECT
            i.*,
            t.id AS target_id,
            t.account_ref_hmac,
            t.profile_ref_hmac,
            t.required_profile_class,
            t.consumed_at AS target_consumed_at,
            r.id AS reservation_id,
            r.pool_key,
            r.state AS reservation_state,
            r.reserved_slots,
            r.consumed_slots,
            (
                SELECT COUNT(*)
                FROM ella_invitation_audit_receipts a
                WHERE a.invitation_id = i.id
                  AND a.event_type = 'operator_issued'
            ) AS operator_issued_audit_count,
            (
                SELECT jsonb_build_object(
                    'metadata', a.metadata,
                    'source_ref_hmac', a.source_ref_hmac
                )
                FROM ella_invitation_audit_receipts a
                WHERE a.invitation_id = i.id
                  AND a.event_type = 'operator_issued'
                ORDER BY a.created_at, a.id
                LIMIT 1
            ) AS operator_issued_receipt
        FROM ella_invitation_targets t
        JOIN ella_invitations i ON i.id = t.invitation_id
        JOIN ella_invitation_capacity_reservations r
          ON r.id = i.capacity_reservation_id
        WHERE t.account_ref_hmac = $1
          AND t.profile_ref_hmac = $2
          AND i.kind = 'ordinary'
          AND i.cohort = $3
        ORDER BY i.created_at, i.id
        {lock_clause}
        """,
        account_ref_hmac,
        profile_ref_hmac,
        invitations.SYNTHETIC_OPERATOR_COHORT,
    )
    return [dict(row) for row in rows]


def _assert_stored_operator_row_shape(
    row: dict[str, Any],
    *,
    identity: SyntheticInvitationIdentity,
    config: invitations.InvitationConfig,
) -> None:
    account_ref_hmac, profile_ref_hmac = invitations.invitation_target_refs(
        config,
        account_uid=identity.account_uid,
        profile_uid=identity.profile_uid,
    )
    try:
        policy = invitations.normalize_entitlement_policy(row["entitlement_policy"])
    except invitations.InviteConfigurationError as exc:
        raise SyntheticInvitationOperatorError("operator_invitation_policy_drift") from exc
    checks = (
        invitations.is_synthetic_operator_invitation(row),
        hmac.compare_digest(str(row["account_ref_hmac"]), account_ref_hmac),
        hmac.compare_digest(str(row["profile_ref_hmac"]), profile_ref_hmac),
        str(row["required_profile_class"]) == "synthetic",
        str(row["pool_key"]) == OPERATOR_POOL_KEY,
        int(row["reserved_slots"]) == 1,
        int(row["consumed_slots"]) in {0, 1},
        str(row["entitlement_policy_revision"]) == OPERATOR_POLICY_REVISION,
        policy == OPERATOR_POLICY,
        int(row.get("operator_issued_audit_count") or 0) == 1,
    )
    if not all(checks):
        raise SyntheticInvitationOperatorError("operator_invitation_drift")


def _assert_issued_context(
    row: dict[str, Any],
    *,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
    expected_code_file_ref_hmac: Optional[str] = None,
) -> None:
    receipt = _json_object(row.get("operator_issued_receipt"))
    metadata = _json_object(receipt.get("metadata"))
    _, expected_source_ref_hmac = invitations.invitation_audit_refs(
        config,
        uid=identity.uid,
        source=_operator_source(context),
    )
    stored_source_ref_hmac = str(receipt.get("source_ref_hmac") or "")
    context_matches = (
        bool(stored_source_ref_hmac)
        and hmac.compare_digest(stored_source_ref_hmac, expected_source_ref_hmac)
        and metadata.get("purpose") == OPERATOR_PURPOSE
        and metadata.get("environment") == context.environment
        and metadata.get("profile_class") == "synthetic"
        and metadata.get("content_free") is True
    )
    if not context_matches:
        raise SyntheticInvitationOperatorError("operator_receipt_context_mismatch")
    if expected_code_file_ref_hmac is not None:
        stored_file_ref = str(metadata.get("code_file_ref_hmac") or "")
        if not stored_file_ref or not hmac.compare_digest(
            stored_file_ref,
            expected_code_file_ref_hmac,
        ):
            raise SyntheticInvitationOperatorError("operator_stale_code_receipt")


def _assert_operator_row_shape(
    row: dict[str, Any],
    *,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    admission: invitations.InvitationPilotAdmission,
    config: invitations.InvitationConfig,
    expected_code_hmac: Optional[str] = None,
    expected_expires_at: Optional[datetime] = None,
    expected_code_file_ref_hmac: Optional[str] = None,
) -> None:
    _assert_stored_operator_row_shape(
        row,
        identity=identity,
        config=config,
    )
    _assert_issued_context(
        row,
        identity=identity,
        context=context,
        config=config,
        expected_code_file_ref_hmac=expected_code_file_ref_hmac,
    )
    consent_matches = (
        row["required_consent_policy_version"] == admission.policy_version
        and row["required_consent_processor_set_hash"] == admission.processor_set_hash
        and row["required_consent_scope_version"] == admission.scope_version
        and row["required_consent_scope_hash"] == admission.scope_hash
    )
    if not consent_matches:
        raise SyntheticInvitationOperatorError("operator_invitation_consent_drift")
    if expected_code_hmac is not None and not hmac.compare_digest(
        str(row["code_hmac"]),
        expected_code_hmac,
    ):
        raise SyntheticInvitationOperatorError("operator_stale_code_receipt")
    if expected_expires_at is not None and _utc(row["expires_at"]) != _utc(expected_expires_at):
        raise SyntheticInvitationOperatorError("operator_invitation_expiry_drift")


async def _record_operator_audit(
    conn: asyncpg.Connection,
    *,
    invitation_id: uuid.UUID,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
    event_type: str,
    metadata: dict[str, Any],
) -> None:
    uid_ref_hmac, source_ref_hmac = invitations.invitation_audit_refs(
        config,
        uid=identity.uid,
        source=_operator_source(context),
    )
    await invitations._record_audit(
        conn,
        invitation_id=invitation_id,
        uid_ref_hmac=uid_ref_hmac,
        source_ref_hmac=source_ref_hmac,
        event_type=event_type,
        support_code=invitations._support_code(),
        correlation_id=invitations._correlation_id(),
        metadata=metadata,
    )


async def issue_synthetic_invitation(
    *,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    admission: invitations.InvitationPilotAdmission,
    code: str,
    code_file_existed: bool,
    code_file_ref_hmac: str,
    recovery_binding_hmac: str,
    expires_at: datetime,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    """Create or safely recover one protected-file-backed invitation."""
    identity.validate()
    context.validate()
    now = datetime.now(timezone.utc)
    normalized_code = invitations.normalize_invite_code(code)
    expiry = _utc(expires_at)
    if (
        not normalized_code
        or not isinstance(admission, invitations.InvitationPilotAdmission)
        or admission.account_uid != identity.account_uid
        or admission.profile_uid != identity.profile_uid
        or expiry <= now
        or expiry > now + MAX_EXPIRY
        or not re.fullmatch(r"[0-9a-f]{64}", code_file_ref_hmac)
        or not re.fullmatch(r"[0-9a-f]{64}", recovery_binding_hmac)
    ):
        raise SyntheticInvitationOperatorError("operator_issue_contract_invalid")
    if not config.redemption_enabled or config.ordinary_enabled or config.app_review_enabled:
        raise SyntheticInvitationOperatorError("operator_invite_flags_invalid")

    invitation_code_hmac = invitations.code_hmac(config, normalized_code)
    expected_recovery_binding_hmac = synthetic_invitation_recovery_binding_hmac(
        identity=identity,
        context=context,
        admission=admission,
        code=code,
        code_file_ref_hmac=code_file_ref_hmac,
        expires_at=expiry,
        config=config,
    )
    if not hmac.compare_digest(recovery_binding_hmac, expected_recovery_binding_hmac):
        raise SyntheticInvitationOperatorError("operator_stale_code_receipt")
    account_ref_hmac, profile_ref_hmac = invitations.invitation_target_refs(
        config,
        account_uid=identity.account_uid,
        profile_uid=identity.profile_uid,
    )
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        await _database_guard(conn, context)
        owner = await authority_advisory_lock.resolve_self_owner_unlocked(
            conn,
            uid=identity.uid,
        )
        async with conn.transaction():
            proof = await _lock_owner(conn, identity, owner)
            user = await _locked_user(conn, identity, owner, proof)
            if str(user["profile_class"]) != "synthetic":
                raise SyntheticInvitationOperatorError("operator_real_profile_refused")
            existing = await _operator_rows_for_target(
                conn,
                account_ref_hmac=account_ref_hmac,
                profile_ref_hmac=profile_ref_hmac,
                for_update=True,
            )
            if len(existing) > 1:
                raise SyntheticInvitationOperatorError("operator_invitation_history_conflict")
            if existing:
                row = existing[0]
                _assert_operator_row_shape(
                    row,
                    identity=identity,
                    context=context,
                    admission=admission,
                    config=config,
                    expected_code_hmac=invitation_code_hmac,
                    expected_expires_at=expiry,
                    expected_code_file_ref_hmac=code_file_ref_hmac,
                )
                if (
                    not code_file_existed
                    or str(row["state"]) != "sent"
                    or str(row["delivery_state"]) != "sent"
                    or row["target_consumed_at"] is not None
                    or int(row["redemption_count"]) != 0
                    or str(row["reservation_state"]) != "reserved"
                    or int(row["consumed_slots"]) != 0
                    or _utc(row["expires_at"]) <= now
                ):
                    raise SyntheticInvitationOperatorError("operator_invitation_not_retryable")
                await _record_operator_audit(
                    conn,
                    invitation_id=row["id"],
                    identity=identity,
                    context=context,
                    config=config,
                    event_type="operator_idempotent_retry",
                    metadata={
                        "purpose": OPERATOR_PURPOSE,
                        "environment": context.environment,
                        "code_file_ref_hmac": code_file_ref_hmac,
                        "content_free": True,
                    },
                )
                return _safe_receipt(
                    row,
                    profile_class=str(user["profile_class"]),
                    idempotent=True,
                )
            if expiry < now + MIN_EXPIRY:
                raise SyntheticInvitationOperatorError("operator_expiry_too_soon")

            _assert_pristine_artifacts(
                await _artifact_snapshot(
                    conn,
                    user_id=user["id"],
                    uid=identity.uid,
                )
            )
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM ella_invitations WHERE code_hmac = $1)",
                invitation_code_hmac,
            ):
                raise SyntheticInvitationOperatorError("operator_code_collision")

            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ($1, 'reserved', 1, $2)
                RETURNING id
                """,
                OPERATOR_POOL_KEY,
                expiry,
            )
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, required_consent_policy_version,
                    required_consent_processor_set_hash,
                    required_consent_scope_version, required_consent_scope_hash,
                    cohort, exclude_from_product_analytics,
                    first_sent_at, expires_at
                ) VALUES (
                    $1, 'ordinary', $2, NULL,
                    'sent', 'sent', 'single_use', 1,
                    1, $3, $4::jsonb, $5, $6, $7, $8,
                    $9, TRUE, $10, $11
                )
                RETURNING id
                """,
                reservation_id,
                invitation_code_hmac,
                OPERATOR_POLICY_REVISION,
                json.dumps(OPERATOR_POLICY, sort_keys=True),
                admission.policy_version,
                admission.processor_set_hash,
                admission.scope_version,
                admission.scope_hash,
                invitations.SYNTHETIC_OPERATOR_COHORT,
                now,
                expiry,
            )
            await conn.execute(
                """
                INSERT INTO ella_invitation_targets (
                    invitation_id, account_ref_hmac, profile_ref_hmac,
                    required_profile_class
                ) VALUES ($1, $2, $3, 'synthetic')
                """,
                invitation_id,
                account_ref_hmac,
                profile_ref_hmac,
            )
            await _record_operator_audit(
                conn,
                invitation_id=invitation_id,
                identity=identity,
                context=context,
                config=config,
                event_type="operator_issued",
                metadata={
                    "purpose": OPERATOR_PURPOSE,
                    "environment": context.environment,
                    "profile_class": "synthetic",
                    "expires_at": expiry.isoformat(),
                    "code_file_ref_hmac": code_file_ref_hmac,
                    "reconciled_from_protected_file": code_file_existed,
                    "content_free": True,
                },
            )
            row = {
                "id": invitation_id,
                "state": "sent",
                "delivery_state": "sent",
                "redemption_count": 0,
                "max_redemptions": 1,
                "expires_at": expiry,
                "version": 1,
            }
            return _safe_receipt(
                row,
                profile_class=str(user["profile_class"]),
            )


def _receipt_uuid(receipt_id: str) -> uuid.UUID:
    try:
        value = uuid.UUID(receipt_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SyntheticInvitationOperatorError("operator_receipt_invalid") from exc
    if str(value) != receipt_id:
        raise SyntheticInvitationOperatorError("operator_receipt_invalid")
    return value


async def _load_exact_receipt(
    conn: asyncpg.Connection,
    *,
    receipt_id: uuid.UUID,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    account_ref_hmac, profile_ref_hmac = invitations.invitation_target_refs(
        config,
        account_uid=identity.account_uid,
        profile_uid=identity.profile_uid,
    )
    row = await conn.fetchrow(
        """
        SELECT
            i.*,
            t.id AS target_id,
            t.account_ref_hmac,
            t.profile_ref_hmac,
            t.required_profile_class,
            t.consumed_at AS target_consumed_at,
            r.id AS reservation_id,
            r.pool_key,
            r.state AS reservation_state,
            r.reserved_slots,
            r.consumed_slots,
            (
                SELECT COUNT(*)
                FROM ella_invitation_audit_receipts a
                WHERE a.invitation_id = i.id
                  AND a.event_type = 'operator_issued'
            ) AS operator_issued_audit_count,
            (
                SELECT jsonb_build_object(
                    'metadata', a.metadata,
                    'source_ref_hmac', a.source_ref_hmac
                )
                FROM ella_invitation_audit_receipts a
                WHERE a.invitation_id = i.id
                  AND a.event_type = 'operator_issued'
                ORDER BY a.created_at, a.id
                LIMIT 1
            ) AS operator_issued_receipt
        FROM ella_invitations i
        JOIN ella_invitation_targets t ON t.invitation_id = i.id
        JOIN ella_invitation_capacity_reservations r
          ON r.id = i.capacity_reservation_id
        WHERE i.id = $1
          AND t.account_ref_hmac = $2
          AND t.profile_ref_hmac = $3
        FOR UPDATE OF i, t, r
        """,
        receipt_id,
        account_ref_hmac,
        profile_ref_hmac,
    )
    user = await conn.fetchrow(
        """
        SELECT id, omi_uid, status, profile_class
        FROM users
        WHERE omi_uid = $1
        FOR UPDATE
        """,
        identity.uid,
    )
    if not row or not user:
        raise SyntheticInvitationOperatorError("operator_receipt_binding_mismatch")
    stored = dict(row)
    _assert_stored_operator_row_shape(
        stored,
        identity=identity,
        config=config,
    )
    _assert_issued_context(
        stored,
        identity=identity,
        context=context,
        config=config,
    )
    return stored, dict(user)


async def _with_exact_receipt(
    *,
    receipt_id: str,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
    operation: str,
    expected_version: Optional[int] = None,
) -> dict[str, Any]:
    identity.validate()
    context.validate()
    parsed_receipt_id = _receipt_uuid(receipt_id)
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        await _database_guard(conn, context)
        owner = await authority_advisory_lock.resolve_self_owner_unlocked(
            conn,
            uid=identity.uid,
        )
        async with conn.transaction():
            proof = await _lock_owner(conn, identity, owner)
            locked_user = await _locked_user(conn, identity, owner, proof)
            row, user = await _load_exact_receipt(
                conn,
                receipt_id=parsed_receipt_id,
                identity=identity,
                context=context,
                config=config,
            )
            if locked_user["id"] != user["id"]:
                raise SyntheticInvitationOperatorError("operator_identity_drift")
            if expected_version is not None and (expected_version < 1 or int(row["version"]) != expected_version):
                raise SyntheticInvitationOperatorError("operator_stale_receipt")
            if operation == "show":
                return _safe_receipt(
                    row,
                    profile_class=str(user["profile_class"]),
                )
            if operation == "revoke":
                return await _revoke_locked(
                    conn,
                    row=row,
                    user=user,
                    identity=identity,
                    context=context,
                    config=config,
                )
            if operation == "cleanup":
                return await _cleanup_locked(
                    conn,
                    row=row,
                    user=user,
                    identity=identity,
                    context=context,
                    config=config,
                )
            raise SyntheticInvitationOperatorError("operator_action_invalid")


async def _has_redemption_or_entitlement(
    conn: asyncpg.Connection,
    *,
    invitation_id: uuid.UUID,
    uid: str,
) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT
                EXISTS (
                    SELECT 1
                    FROM ella_invitation_redemptions
                    WHERE invitation_id = $1
                )
                OR EXISTS (
                    SELECT 1
                    FROM voice_entitlements
                    WHERE invitation_id = $1 OR uid = $2
                )
            """,
            invitation_id,
            uid,
        )
    )


async def _revoke_locked(
    conn: asyncpg.Connection,
    *,
    row: dict[str, Any],
    user: dict[str, Any],
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    if str(user["profile_class"]) != "synthetic":
        raise SyntheticInvitationOperatorError("operator_profile_class_drift")
    if str(row["state"]) == "revoked":
        return _safe_receipt(
            row,
            profile_class=str(user["profile_class"]),
            idempotent=True,
        )
    if (
        str(row["state"]) not in {"issued", "sent"}
        or row["target_consumed_at"] is not None
        or int(row["redemption_count"]) != 0
        or await _has_redemption_or_entitlement(
            conn,
            invitation_id=row["id"],
            uid=identity.uid,
        )
    ):
        raise SyntheticInvitationOperatorError("operator_revoke_refused")
    now = datetime.now(timezone.utc)
    updated = dict(
        await conn.fetchrow(
            """
            UPDATE ella_invitations
            SET state = 'revoked',
                delivery_state = 'suppressed',
                revoked_at = $2,
                version = version + 1,
                updated_at = $2
            WHERE id = $1
            RETURNING *
            """,
            row["id"],
            now,
        )
    )
    await conn.execute(
        """
        UPDATE ella_invitation_capacity_reservations
        SET state = 'released',
            released_at = $2,
            version = version + 1,
            updated_at = $2
        WHERE id = $1 AND state = 'reserved'
        """,
        row["reservation_id"],
        now,
    )
    await _record_operator_audit(
        conn,
        invitation_id=row["id"],
        identity=identity,
        context=context,
        config=config,
        event_type="operator_revoked",
        metadata={
            "purpose": OPERATOR_PURPOSE,
            "environment": context.environment,
            "content_free": True,
        },
    )
    return _safe_receipt(
        updated,
        profile_class=str(user["profile_class"]),
    )


async def _cleanup_locked(
    conn: asyncpg.Connection,
    *,
    row: dict[str, Any],
    user: dict[str, Any],
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expired = bool(row.get("expires_at") and _utc(row["expires_at"]) <= now)
    if (
        str(user["profile_class"]) != "synthetic"
        or (str(row["state"]) != "revoked" and not expired)
        or row["target_consumed_at"] is not None
        or int(row["redemption_count"]) != 0
        or await _has_redemption_or_entitlement(
            conn,
            invitation_id=row["id"],
            uid=identity.uid,
        )
    ):
        raise SyntheticInvitationOperatorError("operator_cleanup_refused")
    artifacts = await _artifact_snapshot(
        conn,
        user_id=user["id"],
        uid=identity.uid,
    )
    artifacts["voice_entitlement"] = False
    _assert_pristine_artifacts(artifacts)
    updated = dict(
        await conn.fetchrow(
            """
            UPDATE ella_invitations
            SET state = CASE WHEN state = 'revoked' THEN state ELSE 'expired' END,
                delivery_state = 'suppressed',
                version = version + 1,
                updated_at = $2
            WHERE id = $1
            RETURNING *
            """,
            row["id"],
            now,
        )
    )
    capacity_state = "expired" if updated["state"] == "expired" else "released"
    await conn.execute(
        """
        UPDATE ella_invitation_capacity_reservations
        SET state = $2,
            released_at = CASE WHEN $2 = 'released' THEN $3 ELSE released_at END,
            version = version + 1,
            updated_at = $3
        WHERE id = $1 AND state IN ('reserved', 'released')
        """,
        row["reservation_id"],
        capacity_state,
        now,
    )
    changed = await conn.fetchval(
        """
        UPDATE users
        SET profile_class = 'real'
        WHERE id = $1 AND omi_uid = $2 AND profile_class = 'synthetic'
        RETURNING id
        """,
        user["id"],
        identity.uid,
    )
    if changed != user["id"]:
        raise SyntheticInvitationOperatorError("operator_profile_class_drift")
    await _record_operator_audit(
        conn,
        invitation_id=row["id"],
        identity=identity,
        context=context,
        config=config,
        event_type="operator_cleanup",
        metadata={
            "purpose": OPERATOR_PURPOSE,
            "environment": context.environment,
            "profile_class": "real",
            "content_free": True,
        },
    )
    return _safe_receipt(
        updated,
        profile_class="real",
    )


async def show_synthetic_invitation(
    *,
    receipt_id: str,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    return await _with_exact_receipt(
        receipt_id=receipt_id,
        identity=identity,
        context=context,
        config=config,
        operation="show",
    )


async def revoke_synthetic_invitation(
    *,
    receipt_id: str,
    expected_version: int,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    return await _with_exact_receipt(
        receipt_id=receipt_id,
        expected_version=expected_version,
        identity=identity,
        context=context,
        config=config,
        operation="revoke",
    )


async def cleanup_synthetic_invitation(
    *,
    receipt_id: str,
    expected_version: int,
    identity: SyntheticInvitationIdentity,
    context: SyntheticInvitationContext,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    return await _with_exact_receipt(
        receipt_id=receipt_id,
        expected_version=expected_version,
        identity=identity,
        context=context,
        config=config,
        operation="cleanup",
    )
