"""PostgreSQL persistence for immutable Ella diagnostic evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import asyncpg

from database.ella_postgres import get_ella_postgres_pool
from utils.ella.account_diagnostics import (
    DIAGNOSTIC_RETENTION_DAYS,
    MAX_EVENTS_PER_ACCOUNT_HOUR,
    DiagnosticAccountAuthority,
    DiagnosticEventV1,
    StoredDiagnosticEvent,
    event_from_record,
)


class DiagnosticAccountNotFound(LookupError):
    pass


class DiagnosticRateLimitExceeded(RuntimeError):
    pass


class DiagnosticSupportGrantLimitExceeded(RuntimeError):
    pass


class DiagnosticSupportGrantInvalid(LookupError):
    pass


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

    async def append_events(
        self,
        authority: DiagnosticAccountAuthority,
        events: list[DiagnosticEventV1],
    ) -> tuple[int, int]:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"ella-account-diagnostics:{authority.account_user_id}",
            )
            event_ids = [event.event_id for event in events]
            existing_rows = await conn.fetch(
                """
                SELECT event_id
                FROM ella_diagnostic_events
                WHERE account_user_id = $1::uuid
                  AND event_id = ANY($2::text[])
                """,
                authority.account_user_id,
                event_ids,
            )
            existing_event_ids = {str(row["event_id"]) for row in existing_rows}
            new_event_count = sum(1 for event_id in event_ids if event_id not in existing_event_ids)
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
            if recent_count + new_event_count > MAX_EVENTS_PER_ACCOUNT_HOUR:
                raise DiagnosticRateLimitExceeded
            accepted = 0
            for event in events:
                inserted = await conn.fetchval(
                    """
                    INSERT INTO ella_diagnostic_events (
                        account_user_id, profile_user_id, event_id,
                        diagnostic_session_id, capture_attempt_id,
                        account_binding_fingerprint, authority_generation,
                        layer, event_name, outcome, stable_failure_code,
                        client_sequence, client_monotonic_ms, client_utc_time,
                        payload, expires_at
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3, $4, $5, $6, $7,
                        $8, $9, $10, $11, $12, $13, $14,
                        $15::jsonb,
                        CURRENT_TIMESTAMP + make_interval(days => $16)
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    authority.account_user_id,
                    authority.profile_user_id,
                    event.event_id,
                    event.diagnostic_session_id,
                    event.capture_attempt_id,
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
                accepted += int(inserted is not None)
        return accepted, len(events) - accepted

    async def list_session_events(
        self,
        authority: DiagnosticAccountAuthority,
        diagnostic_session_id: str,
    ) -> list[StoredDiagnosticEvent]:
        pool = await self._pool_factory()
        rows = await pool.fetch(
            """
            SELECT payload, server_received_at
            FROM ella_diagnostic_events
            WHERE account_user_id = $1::uuid
              AND profile_user_id = $2::uuid
              AND diagnostic_session_id = $3
              AND expires_at > CURRENT_TIMESTAMP
            ORDER BY server_received_at, client_sequence
            """,
            authority.account_user_id,
            authority.profile_user_id,
            diagnostic_session_id,
        )
        return [event_from_record(row) for row in rows]

    async def create_support_grant(
        self,
        authority: DiagnosticAccountAuthority,
        *,
        diagnostic_session_id: str,
        code_hash: str,
        evidence_not_before: datetime,
        evidence_not_after: datetime,
        expires_at: datetime,
    ) -> str:
        pool = await self._pool_factory()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"ella-account-diagnostics:{authority.account_user_id}",
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
    ) -> tuple[str, list[StoredDiagnosticEvent]]:
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
            rows = await conn.fetch(
                """
                SELECT payload, server_received_at
                FROM ella_diagnostic_events
                WHERE account_user_id = $1
                  AND profile_user_id = $2
                  AND diagnostic_session_id = $3
                  AND server_received_at >= $4
                  AND server_received_at <= $5
                  AND expires_at > CURRENT_TIMESTAMP
                ORDER BY server_received_at, client_sequence
                """,
                grant["account_user_id"],
                grant["profile_user_id"],
                grant["diagnostic_session_id"],
                grant["evidence_not_before"],
                grant["evidence_not_after"],
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
        return str(grant["diagnostic_session_id"]), [event_from_record(row) for row in rows]
