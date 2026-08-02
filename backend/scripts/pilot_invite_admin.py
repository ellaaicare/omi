#!/usr/bin/env python3
"""Root-only invitation operator for the invite-gated self-hosted launch."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import authority_advisory_lock, invitations, voice_canary
from database.ella_provisioning import invalidate_self_hosted_authority_on_connection
from database.invitations import (
    SELF_HOSTED_OPERATOR_COHORT,
    SELF_HOSTED_OPERATOR_ENTITLEMENT_POLICY,
    SELF_HOSTED_OPERATOR_POLICY_REVISION,
)
from ella.services.ai_consent import (
    CURRENT_POLICY_VERSION,
    CURRENT_PROCESSOR_SET_HASH,
    CURRENT_SCOPE_HASH,
    CURRENT_SCOPE_VERSION,
)
from scripts.synthetic_invite_admin import (
    ProtectedCodeFileError,
    discard_uncommitted_protected_code_file,
    prepare_protected_code_file,
)

ROOT_UID = 0
OPERATOR_PURPOSE = "self_hosted_invitation_v1"
MIN_EXPIRY = timedelta(minutes=5)
MAX_EXPIRY = timedelta(days=400)
MAX_NON_REVIEW_PILOTS = 5
PILOT_CAPACITY_LOCK = "self-hosted-pilot-capacity-v1"
SAFE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_EMAIL_INPUT_BYTES = 320
EMAIL_INPUT_ALLOWED_MODES = {0o400, 0o600}


class PilotInvitationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _OwnerSetChanged(RuntimeError):
    pass


def _require_root() -> None:
    if os.geteuid() != ROOT_UID:
        raise SystemExit("operator_refused:root_required")


def _print_receipt(action: str, receipt: dict[str, Any], *, code_output_file: str = "") -> None:
    payload = {"action": action, **receipt, "content_free": True}
    if code_output_file:
        payload["code_output_file"] = code_output_file
    print(json.dumps(payload, sort_keys=True, default=str))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PilotInvitationError("expiry_timezone_required")
    return value.astimezone(timezone.utc)


def _safe_context(value: str, *, code: str) -> str:
    normalized = value.strip()
    if not SAFE_CONTEXT_RE.fullmatch(normalized):
        raise PilotInvitationError(code)
    return normalized


def _email_hash(config: invitations.InvitationConfig, email: str) -> Optional[str]:
    return invitations.email_hash_for(config, email) if email else None


def _read_protected_email(
    *,
    approved_root: str,
    email_input_file: str,
    expected_owner_uid: int,
) -> str:
    """Read one email through a no-follow root-owned file boundary."""
    if (
        not approved_root
        or not email_input_file
        or not os.path.isabs(approved_root)
        or not os.path.isabs(email_input_file)
        or os.path.normpath(approved_root) != approved_root
        or os.path.normpath(email_input_file) != email_input_file
        or Path(email_input_file).parent != Path(approved_root)
    ):
        raise PilotInvitationError("email_input_path_invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    try:
        root_descriptor = os.open(approved_root, directory_flags)
    except OSError as exc:
        raise PilotInvitationError("email_input_unavailable") from exc
    try:
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != expected_owner_uid
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise PilotInvitationError("email_input_insecure")
        try:
            descriptor = os.open(Path(email_input_file).name, file_flags, dir_fd=root_descriptor)
        except OSError as exc:
            raise PilotInvitationError("email_input_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != expected_owner_uid
                or stat.S_IMODE(metadata.st_mode) not in EMAIL_INPUT_ALLOWED_MODES
                or metadata.st_nlink != 1
                or not 3 <= metadata.st_size <= MAX_EMAIL_INPUT_BYTES
            ):
                raise PilotInvitationError("email_input_insecure")
            raw = os.read(descriptor, MAX_EMAIL_INPUT_BYTES + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_descriptor)
    if len(raw) > MAX_EMAIL_INPUT_BYTES:
        raise PilotInvitationError("email_input_invalid")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PilotInvitationError("email_input_invalid") from exc
    if decoded.endswith("\n"):
        decoded = decoded[:-1]
    if not decoded or decoded != decoded.strip() or "\n" in decoded or "\r" in decoded or "\x00" in decoded:
        raise PilotInvitationError("email_input_invalid")
    normalized = decoded.lower()
    if len(normalized) > 254 or normalized.count("@") != 1 or any(character.isspace() for character in normalized):
        raise PilotInvitationError("email_input_invalid")
    return normalized


def _absolute_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise PilotInvitationError("expiry_invalid") from exc
    return _utc(parsed)


async def _lock_pilot_capacity(conn) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        PILOT_CAPACITY_LOCK,
    )


async def _active_non_review_slots(conn, *, now: datetime) -> int:
    return int(
        await conn.fetchval(
            """
            SELECT COALESCE(SUM(reservation.reserved_slots), 0)
            FROM ella_invitation_capacity_reservations reservation
            JOIN ella_invitations invitation
              ON invitation.capacity_reservation_id = reservation.id
            WHERE reservation.pool_key = 'self_hosted_pilot'
              AND reservation.state IN ('reserved', 'consumed')
              AND invitation.kind = 'ordinary'
              AND invitation.state IN ('sent', 'redeemed')
              AND (
                    invitation.state = 'redeemed'
                    OR invitation.expires_at > $1
                  )
            """,
            now,
        )
        or 0
    )


def _recovery_binding(
    *,
    code: str,
    code_file_ref_hmac: str,
    kind: str,
    allowed_email_hash: Optional[str],
    expires_at: Optional[datetime],
    environment: str,
    config: invitations.InvitationConfig,
) -> str:
    normalized_code = invitations.normalize_invite_code(code)
    if not normalized_code or not re.fullmatch(r"[0-9a-f]{64}", code_file_ref_hmac):
        raise PilotInvitationError("issue_contract_invalid")
    payload = json.dumps(
        {
            "allowed_email_hash": allowed_email_hash,
            "code_file_ref_hmac": code_file_ref_hmac,
            "code_hmac": invitations.code_hmac(config, normalized_code),
            "environment": environment,
            "expires_at": _utc(expires_at).isoformat() if expires_at else None,
            "kind": kind,
            "policy_version": CURRENT_POLICY_VERSION,
            "processor_set_hash": CURRENT_PROCESSOR_SET_HASH,
            "purpose": OPERATOR_PURPOSE,
            "scope_hash": CURRENT_SCOPE_HASH,
            "scope_version": CURRENT_SCOPE_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        config.hmac_pepper,
        b"self-hosted-invitation-recovery-v1\x1f" + payload,
        hashlib.sha256,
    ).hexdigest()


def _rotation_recovery_binding(
    *,
    code: str,
    code_file_ref_hmac: str,
    receipt_id: str,
    expected_version: int,
    environment: str,
    config: invitations.InvitationConfig,
) -> str:
    normalized_code = invitations.normalize_invite_code(code)
    if not normalized_code or not re.fullmatch(r"[0-9a-f]{64}", code_file_ref_hmac) or expected_version < 1:
        raise PilotInvitationError("rotate_contract_invalid")
    payload = json.dumps(
        {
            "code_file_ref_hmac": code_file_ref_hmac,
            "code_hmac": invitations.code_hmac(config, normalized_code),
            "environment": environment,
            "expected_version": expected_version,
            "purpose": OPERATOR_PURPOSE,
            "rotated_from": str(_receipt_uuid(receipt_id)),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        config.hmac_pepper,
        b"self-hosted-invitation-rotate-recovery-v1\x1f" + payload,
        hashlib.sha256,
    ).hexdigest()


def _stored_shape_matches(
    row: dict[str, Any],
    *,
    kind: str,
    allowed_email_hash: Optional[str],
    expires_at: Optional[datetime],
    code_file_ref_hmac: str,
) -> bool:
    metadata = row.get("operator_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return False
    expected_app_review = kind == "app_review"
    return bool(
        row["kind"] == kind
        and row["state"] == "sent"
        and row["delivery_state"] == "sent"
        and row["usage_mode"] == ("capped_multi_redeem" if expected_app_review else "single_use")
        and int(row["max_redemptions"]) == (20 if expected_app_review else 1)
        and int(row["reserved_setup_slots"]) == (2 if expected_app_review else 1)
        and row["cohort"] == ("app_review" if expected_app_review else SELF_HOSTED_OPERATOR_COHORT)
        and bool(row["exclude_from_product_analytics"]) is expected_app_review
        and row["required_consent_policy_version"] == CURRENT_POLICY_VERSION
        and row["required_consent_processor_set_hash"] == CURRENT_PROCESSOR_SET_HASH
        and row["required_consent_scope_version"] == CURRENT_SCOPE_VERSION
        and row["required_consent_scope_hash"] == CURRENT_SCOPE_HASH
        and row["allowed_email_hash"] == allowed_email_hash
        and (row["expires_at"] is None if expires_at is None else _utc(row["expires_at"]) == _utc(expires_at))
        and isinstance(metadata, dict)
        and metadata.get("purpose") == OPERATOR_PURPOSE
        and metadata.get("code_file_ref_hmac") == code_file_ref_hmac
        and metadata.get("content_free") is True
    )


async def _issue_invitation(
    *,
    code: str,
    code_file_existed: bool,
    code_file_ref_hmac: str,
    kind: str,
    allowed_email_hash: Optional[str],
    expires_at: Optional[datetime],
    environment: str,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    if kind not in {"ordinary", "app_review"}:
        raise PilotInvitationError("invalid_kind")
    if not config.redemption_enabled:
        raise PilotInvitationError("redemption_disabled")
    if kind == "ordinary" and not config.ordinary_enabled:
        raise PilotInvitationError("ordinary_disabled")
    if kind == "app_review" and not config.app_review_enabled:
        raise PilotInvitationError("app_review_disabled")
    now = datetime.now(timezone.utc)
    expiry = _utc(expires_at) if expires_at else None
    if kind == "ordinary" and expiry is None:
        raise PilotInvitationError("invalid_expiry")
    if kind == "app_review" and expiry is not None:
        raise PilotInvitationError("reviewer_expiry_forbidden")

    normalized_code = invitations.normalize_invite_code(code)
    if not normalized_code:
        raise PilotInvitationError("issue_contract_invalid")
    invitation_code_hmac = invitations.code_hmac(config, normalized_code)
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if kind == "ordinary":
                await _lock_pilot_capacity(conn)
            existing = await conn.fetchrow(
                """
                SELECT i.*, audit.metadata AS operator_metadata
                FROM ella_invitations i
                LEFT JOIN LATERAL (
                    SELECT metadata
                    FROM ella_invitation_audit_receipts
                    WHERE invitation_id = i.id
                      AND event_type = 'pilot_operator_issued'
                    ORDER BY created_at, id
                    LIMIT 1
                ) audit ON TRUE
                WHERE i.code_hmac = $1
                FOR UPDATE OF i
                """,
                invitation_code_hmac,
            )
            if existing:
                row = dict(existing)
                if not code_file_existed or not _stored_shape_matches(
                    row,
                    kind=kind,
                    allowed_email_hash=allowed_email_hash,
                    expires_at=expiry,
                    code_file_ref_hmac=code_file_ref_hmac,
                ):
                    raise PilotInvitationError("invitation_recovery_mismatch")
                await invitations._record_audit(
                    conn,
                    invitation_id=row["id"],
                    uid_ref_hmac=invitations._hmac_ref(config, "uid-v1", OPERATOR_PURPOSE),
                    source_ref_hmac=invitations._hmac_ref(config, "source-v1", environment),
                    event_type="pilot_operator_idempotent_retry",
                    support_code=invitations._support_code(),
                    correlation_id=invitations._correlation_id(),
                    metadata={"purpose": OPERATOR_PURPOSE, "content_free": True},
                )
                return {
                    "receipt_id": str(row["id"]),
                    "kind": kind,
                    "state": "sent",
                    "email_scoped": allowed_email_hash is not None,
                    "idempotent": True,
                }

            is_review = kind == "app_review"
            if not is_review and (expiry < now + MIN_EXPIRY or expiry > now + MAX_EXPIRY):
                raise PilotInvitationError("invalid_expiry")
            if not is_review and await _active_non_review_slots(conn, now=now) >= MAX_NON_REVIEW_PILOTS:
                raise PilotInvitationError("pilot_capacity_exhausted")
            reserved_slots = 2 if is_review else 1
            max_redemptions = 20 if is_review else 1
            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ($1, 'reserved', $2, $3)
                RETURNING id
                """,
                "app_review" if is_review else "self_hosted_pilot",
                reserved_slots,
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
                    first_sent_at, expires_at, allowed_email_hash
                ) VALUES (
                    $1, $2, $3, $4, 'sent', 'sent', $5, $6, $7,
                    $8, $9::jsonb, $10, $11, $12, $13, $14, $15, $16, $17, $18
                )
                RETURNING id
                """,
                reservation_id,
                kind,
                invitation_code_hmac,
                normalized_code[:2],
                "capped_multi_redeem" if is_review else "single_use",
                max_redemptions,
                reserved_slots,
                SELF_HOSTED_OPERATOR_POLICY_REVISION,
                json.dumps(SELF_HOSTED_OPERATOR_ENTITLEMENT_POLICY, sort_keys=True),
                CURRENT_POLICY_VERSION,
                CURRENT_PROCESSOR_SET_HASH,
                CURRENT_SCOPE_VERSION,
                CURRENT_SCOPE_HASH,
                "app_review" if is_review else SELF_HOSTED_OPERATOR_COHORT,
                is_review,
                now,
                expiry,
                allowed_email_hash,
            )
            await invitations._record_audit(
                conn,
                invitation_id=invitation_id,
                uid_ref_hmac=invitations._hmac_ref(config, "uid-v1", OPERATOR_PURPOSE),
                source_ref_hmac=invitations._hmac_ref(config, "source-v1", environment),
                event_type="pilot_operator_issued",
                support_code=invitations._support_code(),
                correlation_id=invitations._correlation_id(),
                metadata={
                    "purpose": OPERATOR_PURPOSE,
                    "code_file_ref_hmac": code_file_ref_hmac,
                    "email_scoped": allowed_email_hash is not None,
                    "content_free": True,
                },
            )
            return {
                "receipt_id": str(invitation_id),
                "kind": kind,
                "state": "sent",
                "email_scoped": allowed_email_hash is not None,
                "idempotent": False,
            }


def _receipt_uuid(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PilotInvitationError("receipt_invalid") from exc
    if str(parsed) != value:
        raise PilotInvitationError("receipt_invalid")
    return parsed


async def _show_invitation(*, receipt_id: str) -> dict[str, Any]:
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, state, delivery_state, redemption_count,
                   max_redemptions, allowed_email_hash, expires_at, version
            FROM ella_invitations
            WHERE id = $1
            """,
            _receipt_uuid(receipt_id),
        )
    if not row:
        raise PilotInvitationError("receipt_not_found")
    return {
        "receipt_id": str(row["id"]),
        "kind": str(row["kind"]),
        "state": str(row["state"]),
        "delivery_state": str(row["delivery_state"]),
        "redemption_count": int(row["redemption_count"]),
        "max_redemptions": int(row["max_redemptions"]),
        "email_scoped": row["allowed_email_hash"] is not None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "version": int(row["version"]),
    }


async def _revoke_invitation(
    *,
    receipt_id: str,
    expected_version: int,
    environment: str,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    invitation_uuid = _receipt_uuid(receipt_id)
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        for _attempt in range(22):
            owner_rows = await conn.fetch(
                """
                SELECT DISTINCT app_user.id, app_user.omi_uid
                FROM ella_invitation_redemptions redemption
                JOIN users app_user ON app_user.id = redemption.user_id
                WHERE redemption.invitation_id = $1
                  AND redemption.user_mapping_state = 'mapped'
                ORDER BY app_user.id
                """,
                invitation_uuid,
            )
            expected_owners = tuple((row["id"], str(row["omi_uid"] or "")) for row in owner_rows)
            try:
                async with conn.transaction():
                    owner_locks = {}
                    for user_id, uid in expected_owners:
                        if not uid:
                            raise PilotInvitationError("revoke_owner_invalid")
                        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
                        owner_locks[user_id] = await authority_advisory_lock.acquire_authority_lock(
                            conn,
                            owner=owner,
                        )
                    preview_kind = await conn.fetchval(
                        "SELECT kind FROM ella_invitations WHERE id = $1",
                        invitation_uuid,
                    )
                    if preview_kind == "ordinary":
                        await _lock_pilot_capacity(conn)
                    row = await conn.fetchrow(
                        "SELECT * FROM ella_invitations WHERE id = $1 FOR UPDATE",
                        invitation_uuid,
                    )
                    if not row:
                        raise PilotInvitationError("receipt_not_found")
                    current_owner_rows = await conn.fetch(
                        """
                        SELECT DISTINCT app_user.id, app_user.omi_uid
                        FROM ella_invitation_redemptions redemption
                        JOIN users app_user ON app_user.id = redemption.user_id
                        WHERE redemption.invitation_id = $1
                          AND redemption.user_mapping_state = 'mapped'
                        ORDER BY app_user.id
                        """,
                        invitation_uuid,
                    )
                    current_owners = tuple(
                        (owner_row["id"], str(owner_row["omi_uid"] or "")) for owner_row in current_owner_rows
                    )
                    if current_owners != expected_owners:
                        raise _OwnerSetChanged()
                    if row["state"] == "revoked":
                        return {"receipt_id": str(row["id"]), "state": "revoked", "idempotent": True}
                    if int(row["version"]) != expected_version:
                        raise PilotInvitationError("receipt_stale")
                    if row["state"] not in {"issued", "sent", "redeemed"}:
                        raise PilotInvitationError("revoke_refused")
                    updated = await conn.fetchrow(
                        """
                        UPDATE ella_invitations
                        SET state = 'revoked', delivery_state = 'suppressed',
                            revoked_at = $2, version = version + 1, updated_at = $2
                        WHERE id = $1 AND version = $3
                        RETURNING id, state, version
                        """,
                        row["id"],
                        now,
                        expected_version,
                    )
                    if not updated:
                        raise PilotInvitationError("receipt_stale")
                    await conn.execute(
                        """
                        UPDATE ella_invitation_capacity_reservations
                        SET state = 'released', released_at = COALESCE(released_at, $2),
                            version = version + 1, updated_at = $2
                        WHERE id = $1 AND state IN ('reserved', 'consumed')
                        """,
                        row["capacity_reservation_id"],
                        now,
                    )
                    for user_id, uid in current_owners:
                        await invalidate_self_hosted_authority_on_connection(
                            conn,
                            uid=uid,
                            user_id=user_id,
                            invitation_id=invitation_uuid,
                            reason="self_hosted_invitation_revoked",
                            owner_lock=owner_locks[user_id],
                        )
                    await invitations._record_audit(
                        conn,
                        invitation_id=row["id"],
                        uid_ref_hmac=invitations._hmac_ref(config, "uid-v1", OPERATOR_PURPOSE),
                        source_ref_hmac=invitations._hmac_ref(config, "source-v1", environment),
                        event_type="pilot_operator_revoked",
                        support_code=invitations._support_code(),
                        correlation_id=invitations._correlation_id(),
                        metadata={
                            "purpose": OPERATOR_PURPOSE,
                            "content_free": True,
                            "invalidated_users": len(current_owners),
                        },
                    )
                    return {
                        "receipt_id": str(updated["id"]),
                        "state": str(updated["state"]),
                        "version": int(updated["version"]),
                        "idempotent": False,
                        "invalidated_users": len(current_owners),
                    }
            except _OwnerSetChanged:
                continue
    raise PilotInvitationError("revoke_concurrent_change")


async def _rotate_invitation(
    *,
    receipt_id: str,
    expected_version: int,
    code: str,
    code_file_existed: bool,
    code_file_ref_hmac: str,
    environment: str,
    config: invitations.InvitationConfig,
) -> dict[str, Any]:
    old_id = _receipt_uuid(receipt_id)
    normalized_code = invitations.normalize_invite_code(code)
    if not normalized_code or not re.fullmatch(r"[0-9a-f]{64}", code_file_ref_hmac):
        raise PilotInvitationError("rotate_contract_invalid")
    code_hmac = invitations.code_hmac(config, normalized_code)
    now = datetime.now(timezone.utc)
    pool = await voice_canary.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT * FROM ella_invitations WHERE id = $1 FOR UPDATE",
                old_id,
            )
            if not old:
                raise PilotInvitationError("receipt_not_found")
            existing = await conn.fetchrow(
                """
                SELECT invitation.*, audit.metadata AS operator_metadata
                FROM ella_invitations invitation
                LEFT JOIN LATERAL (
                    SELECT metadata
                    FROM ella_invitation_audit_receipts
                    WHERE invitation_id = invitation.id
                      AND event_type = 'pilot_operator_rotated'
                    ORDER BY created_at, id
                    LIMIT 1
                ) audit ON TRUE
                WHERE invitation.code_hmac = $1
                FOR UPDATE OF invitation
                """,
                code_hmac,
            )
            if existing:
                metadata = existing["operator_metadata"]
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError as exc:
                        raise PilotInvitationError("rotation_recovery_mismatch") from exc
                if not (
                    code_file_existed
                    and old["state"] == "revoked"
                    and existing["state"] == "sent"
                    and existing["delivery_state"] == "sent"
                    and existing["kind"] == old["kind"]
                    and existing["allowed_email_hash"] == old["allowed_email_hash"]
                    and existing["entitlement_policy_revision"] == old["entitlement_policy_revision"]
                    and existing["required_consent_policy_version"] == old["required_consent_policy_version"]
                    and existing["required_consent_processor_set_hash"] == old["required_consent_processor_set_hash"]
                    and existing["required_consent_scope_version"] == old["required_consent_scope_version"]
                    and existing["required_consent_scope_hash"] == old["required_consent_scope_hash"]
                    and isinstance(metadata, dict)
                    and metadata.get("purpose") == OPERATOR_PURPOSE
                    and metadata.get("rotated_from") == str(old_id)
                    and metadata.get("code_file_ref_hmac") == code_file_ref_hmac
                    and metadata.get("content_free") is True
                ):
                    raise PilotInvitationError("rotation_recovery_mismatch")
                return {
                    "receipt_id": str(existing["id"]),
                    "rotated_from": str(old_id),
                    "state": "sent",
                    "idempotent": True,
                }
            if code_file_existed:
                raise PilotInvitationError("rotation_recovery_mismatch")
            if int(old["version"]) != expected_version:
                raise PilotInvitationError("receipt_stale")
            if old["state"] not in {"issued", "sent"} or int(old["redemption_count"]) != 0:
                raise PilotInvitationError("rotate_refused")
            if old["expires_at"] and old["expires_at"] <= now:
                raise PilotInvitationError("rotate_refused")

            old_reservation = await conn.fetchrow(
                """
                SELECT *
                FROM ella_invitation_capacity_reservations
                WHERE id = $1
                FOR UPDATE
                """,
                old["capacity_reservation_id"],
            )
            if not old_reservation or old_reservation["state"] != "reserved":
                raise PilotInvitationError("rotate_refused")
            new_reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots, expires_at
                ) VALUES ($1, 'reserved', $2, $3)
                RETURNING id
                """,
                old_reservation["pool_key"],
                old_reservation["reserved_slots"],
                old_reservation["expires_at"],
            )
            new_invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    reserved_setup_slots, entitlement_policy_revision,
                    entitlement_policy, required_consent_policy_version,
                    required_consent_processor_set_hash,
                    required_consent_scope_version, required_consent_scope_hash,
                    cohort, exclude_from_product_analytics,
                    first_sent_at, expires_at, allowed_email_hash
                ) VALUES (
                    $1, $2, $3, $4, 'sent', 'sent', $5, $6, $7,
                    $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18
                )
                RETURNING id
                """,
                new_reservation_id,
                old["kind"],
                code_hmac,
                normalized_code[:2],
                old["usage_mode"],
                old["max_redemptions"],
                old["reserved_setup_slots"],
                old["entitlement_policy_revision"],
                old["entitlement_policy"],
                old["required_consent_policy_version"],
                old["required_consent_processor_set_hash"],
                old["required_consent_scope_version"],
                old["required_consent_scope_hash"],
                old["cohort"],
                old["exclude_from_product_analytics"],
                now,
                old["expires_at"],
                old["allowed_email_hash"],
            )
            await conn.execute(
                """
                UPDATE ella_invitations
                SET state = 'revoked', delivery_state = 'suppressed',
                    revoked_at = $2, version = version + 1, updated_at = $2
                WHERE id = $1
                """,
                old_id,
                now,
            )
            await conn.execute(
                """
                UPDATE ella_invitation_capacity_reservations
                SET state = 'released', released_at = $2,
                    version = version + 1, updated_at = $2
                WHERE id = $1
                """,
                old["capacity_reservation_id"],
                now,
            )
            await invitations._record_audit(
                conn,
                invitation_id=new_invitation_id,
                uid_ref_hmac=invitations._hmac_ref(config, "uid-v1", OPERATOR_PURPOSE),
                source_ref_hmac=invitations._hmac_ref(config, "source-v1", environment),
                event_type="pilot_operator_rotated",
                support_code=invitations._support_code(),
                correlation_id=invitations._correlation_id(),
                metadata={
                    "purpose": OPERATOR_PURPOSE,
                    "rotated_from": str(old_id),
                    "code_file_ref_hmac": code_file_ref_hmac,
                    "content_free": True,
                },
            )
            return {
                "receipt_id": str(new_invitation_id),
                "rotated_from": str(old_id),
                "state": "sent",
                "idempotent": False,
            }


def _configuration() -> invitations.InvitationConfig:
    try:
        return invitations.InvitationConfig.from_env()
    except invitations.InviteConfigurationError as exc:
        raise SystemExit("operator_refused:configuration_invalid") from exc


async def _issue(args: argparse.Namespace) -> None:
    config = _configuration()
    environment = _safe_context(args.expected_environment, code="environment_invalid")
    allowed_email_hash = None
    if args.email_input_file:
        email = _read_protected_email(
            approved_root=args.approved_code_output_root,
            email_input_file=args.email_input_file,
            expected_owner_uid=ROOT_UID,
        )
        try:
            allowed_email_hash = _email_hash(config, email)
        except ValueError as exc:
            raise PilotInvitationError("email_input_invalid") from exc
        finally:
            email = ""
    expiry = None
    if args.kind == "ordinary":
        if not args.expires_at:
            raise PilotInvitationError("ordinary_expiry_required")
        expiry = _absolute_expiry(args.expires_at)
    elif args.expires_at is not None:
        raise PilotInvitationError("reviewer_expiry_forbidden")
    code_file_ref_hmac = invitations.invitation_code_file_ref(config, args.code_output_file)

    def recovery_binding_for_code(code: str) -> str:
        return _recovery_binding(
            code=code,
            code_file_ref_hmac=code_file_ref_hmac,
            kind=args.kind,
            allowed_email_hash=allowed_email_hash,
            expires_at=expiry,
            environment=environment,
            config=config,
        )

    prepared = prepare_protected_code_file(
        approved_root=args.approved_code_output_root,
        code_output_file=args.code_output_file,
        recovery_binding_for_code=recovery_binding_for_code,
        expected_owner_uid=ROOT_UID,
    )
    try:
        receipt = await _issue_invitation(
            code=prepared.code,
            code_file_existed=prepared.existed,
            code_file_ref_hmac=code_file_ref_hmac,
            kind=args.kind,
            allowed_email_hash=allowed_email_hash,
            expires_at=expiry,
            environment=environment,
            config=config,
        )
    except Exception:
        discard_uncommitted_protected_code_file(
            approved_root=args.approved_code_output_root,
            code_output_file=args.code_output_file,
            prepared=prepared,
            expected_owner_uid=ROOT_UID,
        )
        raise
    finally:
        prepared = None
    _print_receipt("issue", receipt, code_output_file=args.code_output_file)


async def _rotate(args: argparse.Namespace) -> None:
    config = _configuration()
    environment = _safe_context(args.expected_environment, code="environment_invalid")
    code_file_ref_hmac = invitations.invitation_code_file_ref(config, args.code_output_file)

    def recovery_binding_for_code(code: str) -> str:
        return _rotation_recovery_binding(
            code=code,
            code_file_ref_hmac=code_file_ref_hmac,
            receipt_id=args.receipt_id,
            expected_version=args.expected_version,
            environment=environment,
            config=config,
        )

    prepared = prepare_protected_code_file(
        approved_root=args.approved_code_output_root,
        code_output_file=args.code_output_file,
        recovery_binding_for_code=recovery_binding_for_code,
        expected_owner_uid=ROOT_UID,
    )
    try:
        receipt = await _rotate_invitation(
            receipt_id=args.receipt_id,
            expected_version=args.expected_version,
            code=prepared.code,
            code_file_existed=prepared.existed,
            code_file_ref_hmac=code_file_ref_hmac,
            environment=environment,
            config=config,
        )
    finally:
        prepared = None
    _print_receipt("rotate", receipt, code_output_file=args.code_output_file)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    issue = subparsers.add_parser("issue")
    issue.add_argument("--kind", required=True, choices=["ordinary", "app_review"])
    issue.add_argument("--email-input-file")
    issue.add_argument("--expires-at")
    issue.add_argument("--expected-environment", required=True)
    issue.add_argument("--approved-code-output-root", required=True)
    issue.add_argument("--code-output-file", required=True)
    issue.set_defaults(handler=_issue)

    show = subparsers.add_parser("show")
    show.add_argument("--receipt-id", required=True)
    show.set_defaults(handler=lambda args: _show_and_print(args))

    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--receipt-id", required=True)
    revoke.add_argument("--expected-version", required=True, type=int)
    revoke.add_argument("--expected-environment", required=True)
    revoke.set_defaults(handler=lambda args: _revoke_and_print(args))

    rotate = subparsers.add_parser("rotate")
    rotate.add_argument("--receipt-id", required=True)
    rotate.add_argument("--expected-version", required=True, type=int)
    rotate.add_argument("--expected-environment", required=True)
    rotate.add_argument("--approved-code-output-root", required=True)
    rotate.add_argument("--code-output-file", required=True)
    rotate.set_defaults(handler=_rotate)
    return parser


def _reject_sensitive_argv(arguments: list[str]) -> None:
    """Refuse likely plaintext identity or invitation codes before argparse can echo them."""
    if any(
        argument == "--email"
        or argument.startswith("--email=")
        or "@" in argument
        or bool(invitations.normalize_invite_code(argument))
        for argument in arguments
    ):
        raise PilotInvitationError("secret_argv_forbidden")


async def _show_and_print(args: argparse.Namespace) -> None:
    _print_receipt("show", await _show_invitation(receipt_id=args.receipt_id))


async def _revoke_and_print(args: argparse.Namespace) -> None:
    config = _configuration()
    environment = _safe_context(args.expected_environment, code="environment_invalid")
    _print_receipt(
        "revoke",
        await _revoke_invitation(
            receipt_id=args.receipt_id,
            expected_version=args.expected_version,
            environment=environment,
            config=config,
        ),
    )


async def _main() -> None:
    _require_root()
    _reject_sensitive_argv(sys.argv[1:])
    args = _parser().parse_args()
    await args.handler(args)


def _run_cli() -> int:
    try:
        asyncio.run(_main())
    except (PilotInvitationError, ProtectedCodeFileError) as exc:
        print(json.dumps({"status": "error", "code": exc.code}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"status": "error", "code": "outcome_ambiguous"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
