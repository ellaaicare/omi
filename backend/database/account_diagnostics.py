"""PostgreSQL persistence for immutable Ella diagnostic evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import asyncpg

from database import authority_advisory_lock, managed_cloud_consent
from database.ella_postgres import get_ella_postgres_pool

DIAGNOSTIC_RETENTION_DAYS = 30
MAX_EVENTS_PER_ACCOUNT_HOUR = 600
MAX_EVENTS_PER_PROJECTION = 1_000


@dataclass(frozen=True)
class DiagnosticAccountAuthority:
    account_user_id: str
    profile_user_id: str
    binding_revision: int


class DiagnosticAccountNotFound(LookupError):
    pass


class DiagnosticAccountAuthorityChanged(RuntimeError):
    pass


class DiagnosticRateLimitExceeded(RuntimeError):
    pass


class DiagnosticEventConflict(RuntimeError):
    pass


class DiagnosticProjectionLimitExceeded(RuntimeError):
    pass


class DiagnosticSupportGrantLimitExceeded(RuntimeError):
    pass


class DiagnosticSupportGrantInvalid(LookupError):
    pass


def account_binding_fingerprint(
    *,
    uid: str,
    profile_binding_id: str,
    binding_revision: int,
    consent_receipt_id: str,
) -> str:
    """Mirror Dart ``WalOwner.authorityFingerprint`` byte-for-byte."""
    preimage = json.dumps(
        ["wal-owner-authority-v1", uid, profile_binding_id, binding_revision, consent_receipt_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


class PostgresAccountDiagnosticsRepository:
    def __init__(
        self,
        pool_factory: Callable[[], Awaitable[asyncpg.Pool]] = get_ella_postgres_pool,
    ) -> None:
        self._pool_factory = pool_factory

    async def resolve_account_authority(self, uid: str) -> DiagnosticAccountAuthority:
        pool = await self._pool_factory()
        row = await pool.fetchrow(
            """
            SELECT app_user.id AS account_user_id,
                   COALESCE(binding.profile_user_id, app_user.id) AS profile_user_id,
                   COALESCE(binding.revision, 1) AS binding_revision
            FROM users app_user
            LEFT JOIN LATERAL (
                SELECT candidate.profile_user_id, candidate.revision
                FROM ella_runtime_bindings candidate
                WHERE candidate.user_id = app_user.id
                  AND candidate.role = 'user'
                  AND candidate.active = TRUE
                  AND candidate.status = 'active'
                  AND (
                      candidate.account_user_id IS NULL
                      OR candidate.account_user_id = app_user.id
                  )
                ORDER BY candidate.revision DESC, candidate.updated_at DESC
                LIMIT 1
            ) binding ON TRUE
            WHERE app_user.omi_uid = $1
            """,
            uid,
        )
        if row is None:
            raise DiagnosticAccountNotFound(uid)
        return DiagnosticAccountAuthority(
            account_user_id=str(row["account_user_id"]),
            profile_user_id=str(row["profile_user_id"]),
            binding_revision=int(row["binding_revision"]),
        )

    async def _lock_and_verify_current_authority(
        self,
        conn: asyncpg.Connection,
        authority: DiagnosticAccountAuthority,
        *,
        uid: str,
        profile_binding_id: str,
        consent_receipt_id: str,
        expected_fingerprint: str,
        event_fingerprints: tuple[str, ...] = (),
    ) -> None:
        """Acquire the canonical lock first, then fail closed on any drift."""
        account_user_id = uuid.UUID(authority.account_user_id)
        owner = authority_advisory_lock.AuthorityOwner.from_values(
            account_user_id,
            account_user_id,
        )
        owner_lock = await authority_advisory_lock.acquire_authority_lock(
            conn,
            owner=owner,
        )
        current_user_id = await authority_advisory_lock.verify_self_owner_after_lock(
            conn,
            uid=uid,
            owner=owner,
            proof=owner_lock,
        )
        binding = await conn.fetchrow(
            """
            SELECT candidate.profile_user_id, candidate.revision
            FROM ella_runtime_bindings candidate
            WHERE candidate.user_id = $1
              AND candidate.role = 'user'
              AND candidate.active = TRUE
              AND candidate.status = 'active'
              AND (
                  candidate.account_user_id IS NULL
                  OR candidate.account_user_id = $1
              )
            ORDER BY candidate.revision DESC, candidate.updated_at DESC
            LIMIT 1
            """,
            current_user_id,
        )
        current_profile_user_id = current_user_id if binding is None else binding["profile_user_id"] or current_user_id
        current_binding_revision = 1 if binding is None else int(binding["revision"])
        consent = await conn.fetchrow(
            """
            SELECT decision, consent_receipt_ref, profile_binding_id
            FROM ella_managed_cloud_consent_authority
            WHERE user_id = $1
            FOR UPDATE
            """,
            current_user_id,
        )
        expected_receipt_ref = managed_cloud_consent.consent_receipt_ref(uid, consent_receipt_id)
        current_fingerprint = account_binding_fingerprint(
            uid=uid,
            profile_binding_id=profile_binding_id,
            binding_revision=current_binding_revision,
            consent_receipt_id=consent_receipt_id,
        )
        authority_is_current = bool(
            str(current_user_id) == authority.account_user_id
            and str(current_profile_user_id) == authority.profile_user_id
            and current_binding_revision == authority.binding_revision
            and consent is not None
            and consent["decision"] == "granted"
            and hmac.compare_digest(str(consent["profile_binding_id"] or ""), profile_binding_id)
            and hmac.compare_digest(str(consent["consent_receipt_ref"] or ""), expected_receipt_ref)
            and hmac.compare_digest(expected_fingerprint, current_fingerprint)
            and all(hmac.compare_digest(fingerprint, current_fingerprint) for fingerprint in event_fingerprints)
        )
        if not authority_is_current:
            raise DiagnosticAccountAuthorityChanged

    @staticmethod
    async def _event_is_exact_retry(
        conn: asyncpg.Connection,
        authority: DiagnosticAccountAuthority,
        event: Any,
    ) -> bool:
        """Return true only when both immutable identities and payload match."""
        rows = await conn.fetch(
            """
            SELECT event_id, diagnostic_session_id, capture_attempt_id,
                   client_sequence, payload = $6::jsonb AS payload_matches
            FROM ella_diagnostic_events
            WHERE account_user_id = $1::uuid
              AND (
                  event_id = $2
                  OR (
                      diagnostic_session_id = $3
                      AND capture_attempt_id = $4
                      AND client_sequence = $5
                  )
              )
            """,
            authority.account_user_id,
            event.event_id,
            event.diagnostic_session_id,
            event.capture_attempt_id,
            event.client_sequence,
            event.model_dump_json(exclude_none=True),
        )
        if not rows:
            return False
        exact_retry = bool(
            len(rows) == 1
            and str(rows[0]["event_id"]) == event.event_id
            and str(rows[0]["diagnostic_session_id"]) == event.diagnostic_session_id
            and str(rows[0]["capture_attempt_id"]) == event.capture_attempt_id
            and int(rows[0]["client_sequence"]) == event.client_sequence
            and rows[0]["payload_matches"] is True
        )
        if not exact_retry:
            raise DiagnosticEventConflict
        return True

    @staticmethod
    async def _validate_attempt_identity(
        conn: asyncpg.Connection,
        authority: DiagnosticAccountAuthority,
        event: Any,
    ) -> None:
        """Keep each session's attempt ID and ordinal in a one-to-one mapping."""
        rows = await conn.fetch(
            """
            SELECT DISTINCT capture_attempt_id, capture_attempt_ordinal
            FROM ella_diagnostic_events
            WHERE account_user_id = $1::uuid
              AND diagnostic_session_id = $2
              AND (
                  capture_attempt_id = $3
                  OR capture_attempt_ordinal = $4
              )
            """,
            authority.account_user_id,
            event.diagnostic_session_id,
            event.capture_attempt_id,
            event.capture_attempt_ordinal,
        )
        if any(
            str(row["capture_attempt_id"]) != event.capture_attempt_id
            or int(row["capture_attempt_ordinal"]) != event.capture_attempt_ordinal
            for row in rows
        ):
            raise DiagnosticEventConflict

    async def append_events(
        self,
        authority: DiagnosticAccountAuthority,
        events: list[Any],
        *,
        uid: str,
        profile_binding_id: str,
        consent_receipt_id: str,
        expected_fingerprint: str,
    ) -> tuple[int, int]:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            await self._lock_and_verify_current_authority(
                conn,
                authority,
                uid=uid,
                profile_binding_id=profile_binding_id,
                consent_receipt_id=consent_receipt_id,
                expected_fingerprint=expected_fingerprint,
                event_fingerprints=tuple(event.account_binding_fingerprint for event in events),
            )
            pending_events: list[Any] = []
            pending_by_event_id: dict[str, Any] = {}
            pending_by_coordinate: dict[tuple[str, str, int], Any] = {}
            pending_ordinals_by_attempt: dict[tuple[str, str], int] = {}
            pending_attempts_by_ordinal: dict[tuple[str, int], str] = {}
            duplicates = 0
            for event in events:
                await self._validate_attempt_identity(conn, authority, event)
                attempt_key = (event.diagnostic_session_id, event.capture_attempt_id)
                ordinal_key = (event.diagnostic_session_id, event.capture_attempt_ordinal)
                if (
                    pending_ordinals_by_attempt.get(attempt_key, event.capture_attempt_ordinal)
                    != event.capture_attempt_ordinal
                    or pending_attempts_by_ordinal.get(ordinal_key, event.capture_attempt_id)
                    != event.capture_attempt_id
                ):
                    raise DiagnosticEventConflict
                if await self._event_is_exact_retry(conn, authority, event):
                    duplicates += 1
                    continue
                coordinate = (
                    event.diagnostic_session_id,
                    event.capture_attempt_id,
                    event.client_sequence,
                )
                pending_collision = pending_by_event_id.get(event.event_id) or pending_by_coordinate.get(coordinate)
                if pending_collision is not None:
                    if (
                        pending_collision.event_id != event.event_id
                        or pending_collision.diagnostic_session_id != event.diagnostic_session_id
                        or pending_collision.capture_attempt_id != event.capture_attempt_id
                        or pending_collision.client_sequence != event.client_sequence
                        or pending_collision.model_dump(mode="json", exclude_none=True)
                        != event.model_dump(mode="json", exclude_none=True)
                    ):
                        raise DiagnosticEventConflict
                    duplicates += 1
                    continue
                pending_events.append(event)
                pending_by_event_id[event.event_id] = event
                pending_by_coordinate[coordinate] = event
                pending_ordinals_by_attempt[attempt_key] = event.capture_attempt_ordinal
                pending_attempts_by_ordinal[ordinal_key] = event.capture_attempt_id
            recent_count = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_diagnostic_events
                    WHERE account_user_id = $1::uuid
                      AND server_received_at >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
                    """,
                    authority.account_user_id,
                )
                or 0
            )
            if recent_count + len(pending_events) > MAX_EVENTS_PER_ACCOUNT_HOUR:
                raise DiagnosticRateLimitExceeded
            accepted = 0
            for event in pending_events:
                inserted = await conn.fetchval(
                    """
                    INSERT INTO ella_diagnostic_events (
                        account_user_id, profile_user_id, event_id,
                        diagnostic_session_id, capture_attempt_id, capture_attempt_ordinal,
                        account_binding_fingerprint, authority_generation,
                        layer, event_name, outcome, stable_failure_code,
                        client_sequence, client_monotonic_ms, client_utc_time,
                        payload, expires_at
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15,
                        $16::jsonb,
                        CURRENT_TIMESTAMP + make_interval(days => $17)
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    authority.account_user_id,
                    authority.profile_user_id,
                    event.event_id,
                    event.diagnostic_session_id,
                    event.capture_attempt_id,
                    event.capture_attempt_ordinal,
                    event.account_binding_fingerprint,
                    event.authority_generation,
                    event.layer.value,
                    event.event_name,
                    event.outcome.value,
                    event.stable_failure_code.value if event.stable_failure_code else None,
                    event.client_sequence,
                    event.client_monotonic_ms,
                    event.client_utc_time,
                    event.model_dump_json(exclude_none=True),
                    DIAGNOSTIC_RETENTION_DAYS,
                )
                if inserted is None:
                    if await self._event_is_exact_retry(conn, authority, event):
                        duplicates += 1
                        continue
                    raise DiagnosticEventConflict
                accepted += 1
        return accepted, duplicates

    async def list_session_events(
        self,
        authority: DiagnosticAccountAuthority,
        diagnostic_session_id: str,
        *,
        uid: str,
        profile_binding_id: str,
        consent_receipt_id: str,
        expected_fingerprint: str,
    ) -> list[Any]:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            await self._lock_and_verify_current_authority(
                conn,
                authority,
                uid=uid,
                profile_binding_id=profile_binding_id,
                consent_receipt_id=consent_receipt_id,
                expected_fingerprint=expected_fingerprint,
            )
            rows = await self._list_bounded_latest_attempt_events(
                conn,
                account_user_id=authority.account_user_id,
                profile_user_id=authority.profile_user_id,
                diagnostic_session_id=diagnostic_session_id,
            )
        return list(rows)

    @staticmethod
    async def _list_bounded_latest_attempt_events(
        conn: asyncpg.Connection,
        *,
        account_user_id: str,
        profile_user_id: str,
        diagnostic_session_id: str,
        evidence_not_before: datetime | None = None,
        evidence_not_after: datetime | None = None,
    ) -> list[Any]:
        """Fetch only one attempt and reject rather than materialize an oversized projection."""
        rows = await conn.fetch(
            """
            WITH selected_attempt AS MATERIALIZED (
                SELECT candidate.capture_attempt_id
                FROM ella_diagnostic_events candidate
                WHERE candidate.account_user_id = $1::uuid
                  AND candidate.profile_user_id = $2::uuid
                  AND candidate.diagnostic_session_id = $3
                  AND candidate.expires_at > CURRENT_TIMESTAMP
                  AND ($4::timestamptz IS NULL OR candidate.server_received_at >= $4::timestamptz)
                  AND ($5::timestamptz IS NULL OR candidate.server_received_at <= $5::timestamptz)
                ORDER BY
                    candidate.capture_attempt_ordinal DESC,
                    (candidate.event_name = 'capture_attempt_started') DESC,
                    candidate.client_utc_time DESC,
                    candidate.client_monotonic_ms DESC,
                    candidate.client_sequence DESC,
                    candidate.event_id DESC
                LIMIT 1
            )
            SELECT event.payload, event.server_received_at
            FROM ella_diagnostic_events event
            JOIN selected_attempt selected
              ON selected.capture_attempt_id = event.capture_attempt_id
            WHERE event.account_user_id = $1::uuid
              AND event.profile_user_id = $2::uuid
              AND event.diagnostic_session_id = $3
              AND event.expires_at > CURRENT_TIMESTAMP
              AND ($4::timestamptz IS NULL OR event.server_received_at >= $4::timestamptz)
              AND ($5::timestamptz IS NULL OR event.server_received_at <= $5::timestamptz)
            ORDER BY event.client_sequence, event.client_monotonic_ms, event.server_received_at
            LIMIT $6
            """,
            account_user_id,
            profile_user_id,
            diagnostic_session_id,
            evidence_not_before,
            evidence_not_after,
            MAX_EVENTS_PER_PROJECTION + 1,
        )
        if len(rows) > MAX_EVENTS_PER_PROJECTION:
            raise DiagnosticProjectionLimitExceeded
        return list(rows)

    async def delete_expired_events(self, *, batch_size: int = 1_000) -> int:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("diagnostic retention batch size must be between 1 and 10000")
        pool = await self._pool_factory()
        rows = await pool.fetch(
            """
            WITH expired AS (
                SELECT id
                FROM ella_diagnostic_events
                WHERE expires_at <= CURRENT_TIMESTAMP
                ORDER BY expires_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM ella_diagnostic_events event
            USING expired
            WHERE event.id = expired.id
            RETURNING event.id
            """,
            batch_size,
        )
        return len(rows)

    async def create_support_grant(
        self,
        authority: DiagnosticAccountAuthority,
        *,
        diagnostic_session_id: str,
        code_hash: str,
        evidence_not_before: datetime,
        evidence_not_after: datetime,
        expires_at: datetime,
        uid: str,
        profile_binding_id: str,
        consent_receipt_id: str,
        expected_fingerprint: str,
    ) -> str:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            await self._lock_and_verify_current_authority(
                conn,
                authority,
                uid=uid,
                profile_binding_id=profile_binding_id,
                consent_receipt_id=consent_receipt_id,
                expected_fingerprint=expected_fingerprint,
            )
            active_count = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_diagnostic_support_grants
                    WHERE account_user_id = $1::uuid
                      AND redeemed_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    authority.account_user_id,
                )
                or 0
            )
            if active_count >= 3:
                raise DiagnosticSupportGrantLimitExceeded
            grant_id = await conn.fetchval(
                """
                INSERT INTO ella_diagnostic_support_grants (
                    account_user_id, profile_user_id, diagnostic_session_id,
                    code_hash, evidence_not_before, evidence_not_after, expires_at
                )
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                authority.account_user_id,
                authority.profile_user_id,
                diagnostic_session_id,
                code_hash,
                evidence_not_before,
                evidence_not_after,
                expires_at,
            )
        return str(grant_id)

    async def revoke_support_grant(
        self,
        authority: DiagnosticAccountAuthority,
        grant_id: str,
    ) -> bool:
        pool = await self._pool_factory()
        result = await pool.execute(
            """
            UPDATE ella_diagnostic_support_grants
            SET revoked_at = CURRENT_TIMESTAMP
            WHERE id = $1::uuid
              AND account_user_id = $2::uuid
              AND profile_user_id = $3::uuid
              AND redeemed_at IS NULL
              AND revoked_at IS NULL
            """,
            grant_id,
            authority.account_user_id,
            authority.profile_user_id,
        )
        return result == "UPDATE 1"

    async def consume_support_grant(
        self,
        *,
        code_hash: str,
        operator_id: str,
        case_id: str,
        reason: str,
    ) -> tuple[str, list[Any]]:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            grant = await conn.fetchrow(
                """
                UPDATE ella_diagnostic_support_grants
                SET redeemed_at = CURRENT_TIMESTAMP
                WHERE code_hash = $1
                  AND redeemed_at IS NULL
                  AND revoked_at IS NULL
                  AND expires_at > CURRENT_TIMESTAMP
                RETURNING id, account_user_id, profile_user_id,
                          diagnostic_session_id, evidence_not_before, evidence_not_after
                """,
                code_hash,
            )
            if grant is None:
                raise DiagnosticSupportGrantInvalid
            rows = await self._list_bounded_latest_attempt_events(
                conn,
                account_user_id=str(grant["account_user_id"]),
                profile_user_id=str(grant["profile_user_id"]),
                diagnostic_session_id=str(grant["diagnostic_session_id"]),
                evidence_not_before=grant["evidence_not_before"],
                evidence_not_after=grant["evidence_not_after"],
            )
            await conn.execute(
                """
                INSERT INTO ella_diagnostic_support_audit (
                    grant_id, account_user_id, operator_id, case_id,
                    reason, action, observed_event_count
                )
                VALUES ($1, $2, $3, $4, $5, 'support_projection_read', $6)
                """,
                grant["id"],
                grant["account_user_id"],
                operator_id,
                case_id,
                reason,
                len(rows),
            )
        return str(grant["diagnostic_session_id"]), list(rows)
