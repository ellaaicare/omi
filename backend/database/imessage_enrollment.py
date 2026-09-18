"""PostgreSQL authority for owner-scoped iMessage enrollment."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

import asyncpg

from database import authority_advisory_lock
from database.ella_provisioning import get_pool


class ImessageAuthorityError(RuntimeError):
    """A content-free enrollment authority predicate failed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImessageConsentInput:
    request_id: uuid.UUID
    decision: str
    policy_version: str
    processor_set_hash: str
    scope_version: str
    scope_hash: str
    app_version: str
    build_number: str


@dataclass(frozen=True)
class ImessageConsentContract:
    policy_version: str
    processor_set_hash: str
    scope_version: str
    scope_hash: str


@dataclass(frozen=True)
class ImessageRuntimeSnapshot:
    uid: str
    binding_id: uuid.UUID
    target_id: Optional[uuid.UUID]
    authority_kind: str
    authority_digest: str
    binding_revision: int
    entitlement_revision: int
    account_user_id: uuid.UUID
    profile_user_id: uuid.UUID


def _row_dict(row: Any) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


class ImessageEnrollmentRepository:
    """Lock-ordered writes for dedicated iMessage consent and bindings."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def create(cls) -> "ImessageEnrollmentRepository":
        return cls(await get_pool())

    async def assert_schema_ready(self) -> None:
        row = await self.pool.fetchrow("""
            SELECT
                to_regclass('ella_imessage_consent_authority') IS NOT NULL AS consent_authority,
                to_regclass('ella_imessage_registration_attempts') IS NOT NULL AS attempts,
                to_regclass('ella_imessage_channel_bindings') IS NOT NULL AS bindings,
                to_regclass('ella_imessage_proof_receipts') IS NOT NULL AS proof_receipts,
                to_regclass('ella_imessage_account_deletion_fences') IS NOT NULL AS deletion_fences,
                (
                    SELECT COUNT(*) = 2
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name IN (
                          'ella_imessage_registration_attempts',
                          'ella_imessage_channel_bindings'
                      )
                      AND column_name = 'runtime_authority_kind'
                ) AS retained_authority_kind
            """)
        if not row or not all(row.values()):
            raise ImessageAuthorityError("imessage_enrollment_schema_not_ready")

    async def submit_consent(self, *, uid: str, submission: ImessageConsentInput) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                if not await self._active_user(connection, user_id=user_id):
                    raise ImessageAuthorityError("imessage_owner_not_active")

                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_consent_receipts
                    WHERE user_id = $1 AND request_id = $2
                    FOR SHARE
                    """,
                    user_id,
                    submission.request_id,
                )
                if existing:
                    expected = (
                        submission.decision,
                        submission.policy_version,
                        submission.processor_set_hash,
                        submission.scope_version,
                        submission.scope_hash,
                        submission.app_version,
                        submission.build_number,
                    )
                    observed = tuple(
                        str(existing[field])
                        for field in (
                            "decision",
                            "policy_version",
                            "processor_set_hash",
                            "scope_version",
                            "scope_hash",
                            "app_version",
                            "build_number",
                        )
                    )
                    if observed != expected:
                        raise ImessageAuthorityError("imessage_consent_idempotency_conflict")
                    return dict(existing)

                previous = await connection.fetchrow(
                    """
                    SELECT revision
                    FROM ella_imessage_consent_authority
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                revision = int(previous["revision"]) + 1 if previous else 1
                authority_epoch = uuid.uuid4()
                receipt = await connection.fetchrow(
                    """
                    INSERT INTO ella_imessage_consent_receipts (
                        user_id, request_id, decision, policy_version,
                        processor_set_hash, scope_version, scope_hash,
                        authority_epoch, authority_revision, app_version,
                        build_number
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING *
                    """,
                    user_id,
                    submission.request_id,
                    submission.decision,
                    submission.policy_version,
                    submission.processor_set_hash,
                    submission.scope_version,
                    submission.scope_hash,
                    authority_epoch,
                    revision,
                    submission.app_version,
                    submission.build_number,
                )
                await connection.execute(
                    """
                    INSERT INTO ella_imessage_consent_authority (
                        user_id, current_receipt_id, decision, policy_version,
                        processor_set_hash, scope_version, scope_hash,
                        authority_epoch, revision, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) DO UPDATE SET
                        current_receipt_id = EXCLUDED.current_receipt_id,
                        decision = EXCLUDED.decision,
                        policy_version = EXCLUDED.policy_version,
                        processor_set_hash = EXCLUDED.processor_set_hash,
                        scope_version = EXCLUDED.scope_version,
                        scope_hash = EXCLUDED.scope_hash,
                        authority_epoch = EXCLUDED.authority_epoch,
                        revision = EXCLUDED.revision,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    user_id,
                    receipt["id"],
                    submission.decision,
                    submission.policy_version,
                    submission.processor_set_hash,
                    submission.scope_version,
                    submission.scope_hash,
                    authority_epoch,
                    revision,
                )
                if submission.decision != "granted":
                    await connection.execute(
                        """
                        UPDATE ella_imessage_channel_bindings
                        SET status = 'revoked',
                            generation = generation + 1,
                            revision = revision + 1,
                            revoked_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = $1
                          AND status IN ('verification_pending', 'active')
                        """,
                        user_id,
                    )
                return dict(receipt)

    async def get_owner_state(self, *, uid: str) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT
                u.id AS user_id,
                u.status AS user_status,
                c.current_receipt_id,
                c.decision AS consent_decision,
                c.policy_version,
                c.processor_set_hash,
                c.scope_version,
                c.scope_hash,
                c.authority_epoch,
                c.revision AS consent_revision,
                b.id AS binding_id,
                b.status AS binding_status,
                b.generation,
                b.revision AS binding_revision,
                b.assigned_destination_e164,
                b.runtime_binding_id,
                b.runtime_target_id,
                b.runtime_authority_kind,
                b.runtime_authority_digest,
                b.challenge_expires_at,
                b.verified_at,
                b.revoked_at,
                b.last_transport_healthy_at,
                b.updated_at AS binding_updated_at
            FROM users u
            LEFT JOIN ella_imessage_consent_authority c ON c.user_id = u.id
            LEFT JOIN LATERAL (
                SELECT candidate.*
                FROM ella_imessage_channel_bindings candidate
                WHERE candidate.user_id = u.id
                ORDER BY candidate.generation DESC, candidate.created_at DESC
                LIMIT 1
            ) b ON TRUE
            WHERE u.omi_uid = $1
            """,
            uid,
        )
        if not row:
            raise ImessageAuthorityError("imessage_owner_not_found")
        return dict(row)

    async def get_binding_for_attempt(self, *, uid: str, attempt_id: uuid.UUID) -> Optional[dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT b.*
            FROM ella_imessage_channel_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE u.omi_uid = $1 AND b.registration_attempt_id = $2
            """,
            uid,
            attempt_id,
        )
        return _row_dict(row)

    async def prepare_registration(
        self,
        *,
        uid: str,
        idempotency_key: uuid.UUID,
        handset_ref_hmac: str,
        consent_receipt_id: uuid.UUID,
        consent_contract: ImessageConsentContract,
        runtime: ImessageRuntimeSnapshot,
    ) -> tuple[dict[str, Any], bool]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                if not await self._active_user(connection, user_id=user_id):
                    raise ImessageAuthorityError("imessage_owner_not_active")
                if runtime.account_user_id != user_id or runtime.profile_user_id != user_id:
                    raise ImessageAuthorityError("imessage_runtime_owner_mismatch")

                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_registration_attempts
                    WHERE user_id = $1 AND idempotency_key = $2
                    FOR UPDATE
                    """,
                    user_id,
                    idempotency_key,
                )
                if existing:
                    expected = (
                        handset_ref_hmac,
                        consent_receipt_id,
                        runtime.binding_id,
                        runtime.target_id,
                        runtime.authority_kind,
                        runtime.authority_digest,
                    )
                    observed = (
                        str(existing["handset_ref_hmac"]),
                        existing["consent_receipt_id"],
                        existing["runtime_binding_id"],
                        existing["runtime_target_id"],
                        str(existing["runtime_authority_kind"]),
                        str(existing["runtime_authority_digest"]),
                    )
                    if observed != expected:
                        raise ImessageAuthorityError("imessage_enrollment_idempotency_conflict")
                    await self._current_grant(
                        connection,
                        user_id=user_id,
                        receipt_id=existing["consent_receipt_id"],
                        authority_epoch=existing["consent_authority_epoch"],
                        contract=consent_contract,
                    )
                    await self._require_runtime(connection, user_id=user_id, runtime=runtime)
                    return dict(existing), False

                consent = await self._current_grant(
                    connection,
                    user_id=user_id,
                    receipt_id=consent_receipt_id,
                    contract=consent_contract,
                )
                await self._require_runtime(connection, user_id=user_id, runtime=runtime)
                current_binding = await connection.fetchrow(
                    """
                    SELECT status
                    FROM ella_imessage_channel_bindings
                    WHERE user_id = $1 AND status IN ('verification_pending', 'active')
                    FOR UPDATE
                    """,
                    user_id,
                )
                if current_binding:
                    raise ImessageAuthorityError("imessage_binding_already_exists")
                unresolved_attempt = await connection.fetchval(
                    """
                    SELECT state
                    FROM ella_imessage_registration_attempts
                    WHERE user_id = $1
                      AND state IN ('prepared', 'provider_accepted', 'uncertain')
                    ORDER BY created_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if unresolved_attempt:
                    raise ImessageAuthorityError("imessage_registration_manual_reconciliation_required")
                recent_attempts = int(
                    await connection.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM ella_imessage_registration_attempts
                        WHERE user_id = $1
                          AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
                        """,
                        user_id,
                    )
                )
                if recent_attempts >= 5:
                    raise ImessageAuthorityError("imessage_enrollment_rate_limited")

                attempt = await connection.fetchrow(
                    """
                    INSERT INTO ella_imessage_registration_attempts (
                        user_id, idempotency_key, handset_ref_hmac,
                        consent_receipt_id, consent_authority_epoch,
                        runtime_binding_id, runtime_target_id, runtime_authority_kind,
                        runtime_authority_digest, provider_request_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING *
                    """,
                    user_id,
                    idempotency_key,
                    handset_ref_hmac,
                    consent_receipt_id,
                    consent["authority_epoch"],
                    runtime.binding_id,
                    runtime.target_id,
                    runtime.authority_kind,
                    runtime.authority_digest,
                    uuid.uuid4(),
                )
                return dict(attempt), True

    async def finalize_registration(
        self,
        *,
        uid: str,
        attempt_id: uuid.UUID,
        runtime: ImessageRuntimeSnapshot,
        provider_registration_ref_hmac: str,
        assigned_destination_e164: str,
        assigned_destination_ref_hmac: str,
        challenge_salt: str,
        challenge_hash: str,
        challenge_expires_at: datetime,
        consent_contract: ImessageConsentContract,
    ) -> tuple[dict[str, Any], bool]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                if not await self._active_user(connection, user_id=user_id):
                    raise ImessageAuthorityError("imessage_owner_not_active")
                attempt = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_registration_attempts
                    WHERE id = $1 AND user_id = $2
                    FOR UPDATE
                    """,
                    attempt_id,
                    user_id,
                )
                if not attempt:
                    raise ImessageAuthorityError("imessage_registration_attempt_not_found")
                if str(attempt["state"]) == "finalized":
                    binding = await connection.fetchrow(
                        "SELECT * FROM ella_imessage_channel_bindings WHERE registration_attempt_id = $1",
                        attempt_id,
                    )
                    if not binding:
                        raise ImessageAuthorityError("imessage_registration_finalization_incomplete")
                    return dict(binding), False
                if str(attempt["state"]) != "provider_accepted":
                    raise ImessageAuthorityError("imessage_registration_state_invalid")
                if runtime.account_user_id != user_id or runtime.profile_user_id != user_id:
                    raise ImessageAuthorityError("imessage_runtime_owner_mismatch")
                if (
                    attempt["runtime_binding_id"] != runtime.binding_id
                    or attempt["runtime_target_id"] != runtime.target_id
                    or str(attempt["runtime_authority_kind"]) != runtime.authority_kind
                    or not hmac.compare_digest(str(attempt["runtime_authority_digest"]), runtime.authority_digest)
                    or not hmac.compare_digest(
                        str(attempt["provider_registration_ref_hmac"]),
                        provider_registration_ref_hmac,
                    )
                    or str(attempt["assigned_destination_e164"]) != assigned_destination_e164
                    or not hmac.compare_digest(
                        str(attempt["assigned_destination_ref_hmac"]),
                        assigned_destination_ref_hmac,
                    )
                ):
                    raise ImessageAuthorityError("imessage_runtime_authority_changed")
                await self._current_grant(
                    connection,
                    user_id=user_id,
                    receipt_id=attempt["consent_receipt_id"],
                    authority_epoch=attempt["consent_authority_epoch"],
                    contract=consent_contract,
                )
                await self._require_runtime(connection, user_id=user_id, runtime=runtime)
                generation = int(
                    await connection.fetchval(
                        "SELECT COALESCE(MAX(generation), 0) + 1 FROM ella_imessage_channel_bindings WHERE user_id = $1",
                        user_id,
                    )
                )
                binding = await connection.fetchrow(
                    """
                    INSERT INTO ella_imessage_channel_bindings (
                        user_id, registration_attempt_id, generation,
                        handset_ref_hmac, assigned_destination_e164,
                        assigned_destination_ref_hmac,
                        provider_registration_ref_hmac, runtime_binding_id,
                        runtime_target_id, runtime_authority_kind, runtime_authority_digest,
                        consent_receipt_id, consent_authority_epoch,
                        challenge_salt, challenge_hash, challenge_expires_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15, $16
                    )
                    RETURNING *
                    """,
                    user_id,
                    attempt_id,
                    generation,
                    attempt["handset_ref_hmac"],
                    assigned_destination_e164,
                    assigned_destination_ref_hmac,
                    provider_registration_ref_hmac,
                    runtime.binding_id,
                    runtime.target_id,
                    runtime.authority_kind,
                    runtime.authority_digest,
                    attempt["consent_receipt_id"],
                    attempt["consent_authority_epoch"],
                    challenge_salt,
                    challenge_hash,
                    challenge_expires_at,
                )
                await connection.execute(
                    """
                    UPDATE ella_imessage_registration_attempts
                    SET state = 'finalized',
                        provider_registration_ref_hmac = $2,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    attempt_id,
                    provider_registration_ref_hmac,
                )
                return dict(binding), True

    async def mark_provider_accepted(
        self,
        *,
        uid: str,
        attempt_id: uuid.UUID,
        provider_registration_ref_hmac: str,
        assigned_destination_e164: str,
        assigned_destination_ref_hmac: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                attempt = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_registration_attempts
                    WHERE id = $1 AND user_id = $2
                    FOR UPDATE
                    """,
                    attempt_id,
                    user_id,
                )
                if not attempt:
                    raise ImessageAuthorityError("imessage_registration_attempt_not_found")
                if str(attempt["state"]) == "provider_accepted":
                    expected = (
                        provider_registration_ref_hmac,
                        assigned_destination_e164,
                        assigned_destination_ref_hmac,
                    )
                    observed = (
                        str(attempt["provider_registration_ref_hmac"]),
                        str(attempt["assigned_destination_e164"]),
                        str(attempt["assigned_destination_ref_hmac"]),
                    )
                    if observed != expected:
                        raise ImessageAuthorityError("imessage_provider_acceptance_conflict")
                    return dict(attempt)
                if str(attempt["state"]) != "prepared":
                    raise ImessageAuthorityError("imessage_registration_state_invalid")
                try:
                    accepted = await connection.fetchrow(
                        """
                        UPDATE ella_imessage_registration_attempts
                        SET state = 'provider_accepted',
                            provider_registration_ref_hmac = $2,
                            assigned_destination_e164 = $3,
                            assigned_destination_ref_hmac = $4,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1 AND state = 'prepared'
                        RETURNING *
                        """,
                        attempt_id,
                        provider_registration_ref_hmac,
                        assigned_destination_e164,
                        assigned_destination_ref_hmac,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ImessageAuthorityError("imessage_provider_identity_conflict") from exc
                if not accepted:
                    raise ImessageAuthorityError("imessage_registration_state_invalid")
                return dict(accepted)

    async def mark_registration_uncertain(self, *, uid: str, attempt_id: uuid.UUID) -> None:
        await self._set_attempt_terminal(uid=uid, attempt_id=attempt_id, state="uncertain")

    async def mark_registration_failed(self, *, uid: str, attempt_id: uuid.UUID, error_code: str) -> None:
        await self._set_attempt_terminal(
            uid=uid,
            attempt_id=attempt_id,
            state="failed",
            error_code=error_code,
        )

    async def retire_expired_pending_binding(
        self,
        *,
        uid: str,
        binding_id: uuid.UUID,
        now: datetime,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                binding = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_channel_bindings
                    WHERE id = $1 AND user_id = $2
                    FOR UPDATE
                    """,
                    binding_id,
                    user_id,
                )
                if not binding:
                    raise ImessageAuthorityError("imessage_proof_binding_not_found")
                if str(binding["status"]) != "verification_pending" or binding["challenge_expires_at"] > now:
                    return dict(binding)
                retired = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_channel_bindings
                    SET status = 'quarantined',
                        revision = revision + 1,
                        updated_at = $3
                    WHERE id = $1
                      AND user_id = $2
                      AND status = 'verification_pending'
                      AND challenge_expires_at <= $3
                    RETURNING *
                    """,
                    binding_id,
                    user_id,
                    now,
                )
                if not retired:
                    current = await connection.fetchrow(
                        "SELECT * FROM ella_imessage_channel_bindings WHERE id = $1 AND user_id = $2",
                        binding_id,
                        user_id,
                    )
                    if not current:
                        raise ImessageAuthorityError("imessage_proof_binding_not_found")
                    return dict(current)
                attempt_update = await connection.execute(
                    """
                    UPDATE ella_imessage_registration_attempts
                    SET state = 'quarantined',
                        error_code = 'imessage_proof_expired',
                        updated_at = $2
                    WHERE id = $1 AND state = 'finalized'
                    """,
                    retired["registration_attempt_id"],
                    now,
                )
                if attempt_update != "UPDATE 1":
                    raise ImessageAuthorityError("imessage_registration_state_invalid")
                return dict(retired)

    async def verify_inbound_proof(
        self,
        *,
        assigned_destination_ref_hmac: str,
        handset_ref_hmac: str,
        line_identity_hmac: str,
        contact_identity_hmac: str,
        provider_message_ref_hmac: str,
        candidate_challenge_hash: str,
        consent_contract: ImessageConsentContract,
        runtime: ImessageRuntimeSnapshot,
        now: datetime,
    ) -> dict[str, Any]:
        if await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM ella_imessage_proof_receipts WHERE provider_message_ref_hmac = $1)",
            provider_message_ref_hmac,
        ):
            raise ImessageAuthorityError("imessage_proof_replayed")
        candidate = await self.pool.fetchrow(
            """
            SELECT b.id, u.omi_uid
            FROM ella_imessage_channel_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE b.assigned_destination_ref_hmac = $1
              AND b.handset_ref_hmac = $2
              AND b.status = 'verification_pending'
              AND u.status = 'ACTIVE'
            """,
            assigned_destination_ref_hmac,
            handset_ref_hmac,
        )
        if not candidate:
            raise ImessageAuthorityError("imessage_proof_binding_not_found")
        uid = str(candidate["omi_uid"] or "")
        failure_code: Optional[str] = None
        activated: Optional[Mapping[str, Any]] = None
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                binding = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_channel_bindings
                    WHERE id = $1
                      AND user_id = $2
                      AND assigned_destination_ref_hmac = $3
                      AND handset_ref_hmac = $4
                    FOR UPDATE
                    """,
                    candidate["id"],
                    user_id,
                    assigned_destination_ref_hmac,
                    handset_ref_hmac,
                )
                if not binding:
                    raise ImessageAuthorityError("imessage_proof_binding_not_found")
                if await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM ella_imessage_proof_receipts WHERE provider_message_ref_hmac = $1)",
                    provider_message_ref_hmac,
                ):
                    raise ImessageAuthorityError("imessage_proof_replayed")
                if str(binding["status"]) != "verification_pending":
                    raise ImessageAuthorityError("imessage_proof_binding_not_pending")
                if not await self._active_user(connection, user_id=user_id):
                    raise ImessageAuthorityError("imessage_owner_not_active")
                if runtime.account_user_id != user_id or runtime.profile_user_id != user_id:
                    raise ImessageAuthorityError("imessage_runtime_owner_mismatch")
                if (
                    binding["runtime_binding_id"] != runtime.binding_id
                    or binding["runtime_target_id"] != runtime.target_id
                    or str(binding["runtime_authority_kind"]) != runtime.authority_kind
                    or not hmac.compare_digest(str(binding["runtime_authority_digest"]), runtime.authority_digest)
                ):
                    raise ImessageAuthorityError("imessage_runtime_authority_changed")
                await self._current_grant(
                    connection,
                    user_id=user_id,
                    receipt_id=binding["consent_receipt_id"],
                    authority_epoch=binding["consent_authority_epoch"],
                    contract=consent_contract,
                )
                await self._require_runtime(connection, user_id=user_id, runtime=runtime)
                if binding["challenge_expires_at"] <= now:
                    await self._reject_proof(
                        connection,
                        binding=binding,
                        provider_message_ref_hmac=provider_message_ref_hmac,
                        outcome="expired",
                        quarantine=True,
                    )
                    failure_code = "imessage_proof_expired"
                if failure_code is None and not hmac.compare_digest(
                    str(binding["challenge_hash"]), candidate_challenge_hash
                ):
                    quarantine = int(binding["challenge_attempts"]) + 1 >= 5
                    await self._reject_proof(
                        connection,
                        binding=binding,
                        provider_message_ref_hmac=provider_message_ref_hmac,
                        outcome="rejected",
                        quarantine=quarantine,
                    )
                    failure_code = "imessage_proof_invalid"
                if failure_code is None:
                    try:
                        activated = await connection.fetchrow(
                            """
                            UPDATE ella_imessage_channel_bindings
                            SET status = 'active',
                                line_identity_hmac = $2,
                                contact_identity_hmac = $3,
                                verified_at = $4,
                                last_transport_healthy_at = $4,
                                revision = revision + 1,
                                updated_at = $4
                            WHERE id = $1 AND status = 'verification_pending'
                            RETURNING *
                            """,
                            binding["id"],
                            line_identity_hmac,
                            contact_identity_hmac,
                            now,
                        )
                    except asyncpg.UniqueViolationError as exc:
                        raise ImessageAuthorityError("imessage_identity_already_bound") from exc
                    await connection.execute(
                        """
                        INSERT INTO ella_imessage_proof_receipts (
                            binding_id, provider_message_ref_hmac, outcome
                        ) VALUES ($1, $2, 'accepted')
                        """,
                        binding["id"],
                        provider_message_ref_hmac,
                    )
        if failure_code is not None:
            raise ImessageAuthorityError(failure_code)
        if not activated:
            raise ImessageAuthorityError("imessage_proof_activation_failed")
        return dict(activated)

    async def resolve_proof_authority(
        self,
        *,
        assigned_destination_ref_hmac: str,
        handset_ref_hmac: str,
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT
                u.omi_uid,
                b.runtime_binding_id,
                b.runtime_target_id,
                b.runtime_authority_kind,
                b.runtime_authority_digest,
                b.challenge_salt
            FROM ella_imessage_channel_bindings b
            JOIN users u ON u.id = b.user_id
            WHERE b.assigned_destination_ref_hmac = $1
              AND b.handset_ref_hmac = $2
              AND b.status = 'verification_pending'
              AND u.status = 'ACTIVE'
            """,
            assigned_destination_ref_hmac,
            handset_ref_hmac,
        )
        if not row:
            raise ImessageAuthorityError("imessage_proof_binding_not_found")
        return dict(row)

    async def revoke_binding(
        self,
        *,
        uid: str,
        expected_generation: int,
        idempotency_key: uuid.UUID,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                binding = await connection.fetchrow(
                    """
                    SELECT b.*, a.provider_request_id
                    FROM ella_imessage_channel_bindings b
                    JOIN ella_imessage_registration_attempts a
                      ON a.user_id = b.user_id AND a.id = b.registration_attempt_id
                    WHERE b.user_id = $1
                    ORDER BY b.generation DESC, b.created_at DESC
                    LIMIT 1
                    FOR UPDATE OF b
                    """,
                    user_id,
                )
                if not binding:
                    return None
                if binding["last_revoke_idempotency_key"] == idempotency_key and str(binding["status"]) == "revoked":
                    return dict(binding)
                if int(binding["generation"]) != expected_generation:
                    raise ImessageAuthorityError("imessage_binding_generation_conflict")
                revoked = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_channel_bindings
                    SET status = 'revoked',
                        generation = generation + 1,
                        revision = revision + 1,
                        revoked_at = CURRENT_TIMESTAMP,
                        last_revoke_idempotency_key = $2,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    RETURNING *
                    """,
                    binding["id"],
                    idempotency_key,
                )
                result = dict(revoked)
                result["provider_request_id"] = binding["provider_request_id"]
                return result

    async def provider_request_ids_for_owner(self, *, uid: str) -> list[uuid.UUID]:
        rows = await self.pool.fetch(
            """
            SELECT a.provider_request_id
            FROM ella_imessage_registration_attempts a
            JOIN users u ON u.id = a.user_id
            WHERE u.omi_uid = $1
            ORDER BY a.created_at, a.id
            """,
            uid,
        )
        return [row["provider_request_id"] for row in rows]

    async def begin_account_deletion_cleanup(
        self,
        *,
        uid: str,
        request_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM ella_imessage_account_deletion_fences
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if existing:
                    return dict(existing)
                provider_request_ids = [
                    row["provider_request_id"]
                    for row in await connection.fetch(
                        """
                        SELECT provider_request_id
                        FROM ella_imessage_registration_attempts
                        WHERE user_id = $1
                        ORDER BY created_at, id
                        FOR UPDATE
                        """,
                        user_id,
                    )
                ]
                fence = await connection.fetchrow(
                    """
                    INSERT INTO ella_imessage_account_deletion_fences (
                        user_id, request_id, provider_request_ids
                    ) VALUES ($1, $2, $3::uuid[])
                    RETURNING *
                    """,
                    user_id,
                    request_id,
                    provider_request_ids,
                )
                await connection.execute(
                    """
                    UPDATE ella_imessage_channel_bindings
                    SET status = 'revoked',
                        generation = generation + 1,
                        revision = revision + 1,
                        revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
                        transport_connection_ref_hmac = NULL,
                        transport_connected_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND status IN ('verification_pending', 'active')
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE ella_imessage_registration_attempts
                    SET state = 'quarantined',
                        error_code = 'imessage_account_deletion_pending',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND state IN ('provider_accepted', 'finalized')
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    UPDATE ella_imessage_message_receipts
                    SET status = CASE WHEN status = 'sending' THEN 'uncertain' ELSE 'quarantined' END,
                        reconciliation_status = 'manual_required',
                        error_code = 'imessage_account_deletion_pending',
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND status IN ('claimed', 'running', 'awaiting_delivery', 'sending')
                    """,
                    user_id,
                )
                return dict(fence)

    async def complete_account_deletion_cleanup(
        self,
        *,
        uid: str,
        request_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                row = await connection.fetchrow(
                    """
                    UPDATE ella_imessage_account_deletion_fences
                    SET state = 'cleaned',
                        cleaned_at = COALESCE(cleaned_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = $1
                      AND request_id = $2
                      AND state IN ('pending', 'cleaned')
                    RETURNING *
                    """,
                    user_id,
                    request_id,
                )
                if not row:
                    raise ImessageAuthorityError("imessage_account_cleanup_conflict")
                return dict(row)

    async def _set_attempt_terminal(
        self,
        *,
        uid: str,
        attempt_id: uuid.UUID,
        state: str,
        error_code: Optional[str] = None,
    ) -> None:
        async with self.pool.acquire() as connection:
            owner = await authority_advisory_lock.resolve_self_owner_unlocked(connection, uid=uid)
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                user_id = await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                result = await connection.execute(
                    """
                    UPDATE ella_imessage_registration_attempts
                    SET state = $3, error_code = $4, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1 AND user_id = $2 AND state IN ('prepared', 'provider_accepted')
                    """,
                    attempt_id,
                    user_id,
                    state,
                    error_code,
                )
                if result != "UPDATE 1":
                    raise ImessageAuthorityError("imessage_registration_state_invalid")

    @staticmethod
    async def _active_user(connection: asyncpg.Connection, *, user_id: uuid.UUID) -> bool:
        return bool(
            await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users u
                    WHERE u.id = $1
                      AND u.status = 'ACTIVE'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM ella_imessage_account_deletion_fences deletion
                          WHERE deletion.user_id = u.id
                      )
                )
                """,
                user_id,
            )
        )

    @staticmethod
    async def _current_grant(
        connection: asyncpg.Connection,
        *,
        user_id: uuid.UUID,
        receipt_id: uuid.UUID,
        authority_epoch: Optional[uuid.UUID] = None,
        contract: Optional[ImessageConsentContract] = None,
    ) -> Mapping[str, Any]:
        row = await connection.fetchrow(
            """
            SELECT *
            FROM ella_imessage_consent_authority
            WHERE user_id = $1
            FOR SHARE
            """,
            user_id,
        )
        if not row or str(row["decision"]) != "granted":
            raise ImessageAuthorityError("imessage_consent_required")
        if row["current_receipt_id"] != receipt_id:
            raise ImessageAuthorityError("imessage_consent_receipt_stale")
        if authority_epoch is not None and row["authority_epoch"] != authority_epoch:
            raise ImessageAuthorityError("imessage_consent_authority_changed")
        if contract is not None and (
            str(row["policy_version"]) != contract.policy_version
            or str(row["processor_set_hash"]) != contract.processor_set_hash
            or str(row["scope_version"]) != contract.scope_version
            or str(row["scope_hash"]) != contract.scope_hash
        ):
            raise ImessageAuthorityError("imessage_consent_policy_stale")
        return row

    @staticmethod
    async def _require_runtime(
        connection: asyncpg.Connection,
        *,
        user_id: uuid.UUID,
        runtime: ImessageRuntimeSnapshot,
        compare_revisions: bool = True,
    ) -> None:
        if runtime.authority_kind == "retained_owner":
            if runtime.target_id is not None or runtime.entitlement_revision != 0:
                raise ImessageAuthorityError("imessage_runtime_authority_changed")
            row = await connection.fetchrow(
                """
                SELECT
                    b.id AS binding_id,
                    b.revision AS binding_revision,
                    b.active,
                    b.status AS binding_status,
                    b.health_state,
                    b.provider AS binding_provider,
                    b.role AS binding_role,
                    b.account_user_id,
                    b.profile_user_id,
                    u.omi_uid,
                    u.status AS user_status,
                    EXISTS (
                        SELECT 1
                        FROM ella_runtime_targets target
                        WHERE target.runtime_binding_id = b.id
                    ) AS has_runtime_target
                FROM ella_runtime_bindings b
                JOIN users u ON u.id = b.user_id
                WHERE b.id = $1 AND u.id = $2 AND u.omi_uid = $3
                FOR SHARE OF b, u
                """,
                runtime.binding_id,
                user_id,
                runtime.uid,
            )
            if not row or (
                row["account_user_id"] != user_id
                or row["profile_user_id"] != user_id
                or str(row["user_status"]) != "ACTIVE"
                or str(row["binding_provider"]) != "hermes"
                or str(row["binding_role"]) != "user"
                or str(row["binding_status"]) != "active"
                or row["active"] is not True
                or str(row["health_state"]) != "healthy"
                or row["has_runtime_target"] is True
            ):
                raise ImessageAuthorityError("imessage_runtime_unavailable")
            if compare_revisions and int(row["binding_revision"]) != runtime.binding_revision:
                raise ImessageAuthorityError("imessage_runtime_authority_changed")
            return

        if runtime.authority_kind != "target" or runtime.target_id is None:
            raise ImessageAuthorityError("imessage_runtime_authority_changed")
        row = await connection.fetchrow(
            """
            SELECT
                b.id AS binding_id,
                b.revision AS binding_revision,
                b.active,
                b.status AS binding_status,
                b.health_state,
                b.provider AS binding_provider,
                b.role AS binding_role,
                b.account_user_id,
                b.profile_user_id,
                u.omi_uid,
                u.status AS user_status,
                t.id AS target_id,
                t.provider AS target_provider,
                t.role AS target_role,
                t.account_user_id AS target_account_user_id,
                t.profile_user_id AS target_profile_user_id,
                t.mode,
                t.status AS target_status,
                t.entitlement_revision
            FROM ella_runtime_bindings b
            JOIN users u ON u.id = b.user_id
            JOIN ella_runtime_targets t ON t.runtime_binding_id = b.id
            WHERE b.id = $1 AND t.id = $2 AND u.id = $3 AND u.omi_uid = $4
            FOR SHARE OF b, u, t
            """,
            runtime.binding_id,
            runtime.target_id,
            user_id,
            runtime.uid,
        )
        if not row:
            raise ImessageAuthorityError("imessage_runtime_unavailable")
        if (
            row["account_user_id"] != user_id
            or row["profile_user_id"] != user_id
            or str(row["user_status"]) != "ACTIVE"
            or row["target_account_user_id"] != user_id
            or row["target_profile_user_id"] != user_id
            or str(row["binding_provider"]) != "hermes"
            or str(row["binding_role"]) != "user"
            or str(row["binding_status"]) != "active"
            or row["active"] is not True
            or str(row["health_state"]) != "healthy"
            or str(row["target_provider"]) != "hermes"
            or str(row["target_role"]) != "user"
            or str(row["mode"]) != "hermes-chat"
            or str(row["target_status"]) != "ready"
        ):
            raise ImessageAuthorityError("imessage_runtime_unavailable")
        if compare_revisions and (
            int(row["binding_revision"]) != runtime.binding_revision
            or int(row["entitlement_revision"] or 0) != runtime.entitlement_revision
        ):
            raise ImessageAuthorityError("imessage_runtime_authority_changed")

    @staticmethod
    async def _reject_proof(
        connection: asyncpg.Connection,
        *,
        binding: Mapping[str, Any],
        provider_message_ref_hmac: str,
        outcome: str,
        quarantine: bool,
    ) -> None:
        await connection.execute(
            """
            UPDATE ella_imessage_channel_bindings
            SET challenge_attempts = challenge_attempts + 1,
                status = CASE WHEN $2 THEN 'quarantined' ELSE status END,
                generation = generation + CASE WHEN $2 THEN 1 ELSE 0 END,
                revision = revision + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            """,
            binding["id"],
            quarantine,
        )
        await connection.execute(
            """
            INSERT INTO ella_imessage_proof_receipts (
                binding_id, provider_message_ref_hmac, outcome
            ) VALUES ($1, $2, $3)
            """,
            binding["id"],
            provider_message_ref_hmac,
            outcome,
        )
