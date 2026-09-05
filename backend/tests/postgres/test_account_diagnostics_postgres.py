import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from database import authority_advisory_lock, managed_cloud_consent
from database.account_diagnostics import (
    DiagnosticAccountAuthorityChanged,
    DiagnosticEventConflict,
    DiagnosticProjectionLimitExceeded,
    DiagnosticSupportGrantInvalid,
    PostgresAccountDiagnosticsRepository,
    MAX_EVENTS_PER_PROJECTION,
    account_binding_fingerprint,
)
from utils.ella.account_diagnostics import DiagnosticEventV1

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "017_create_account_diagnostics.sql"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for account-diagnostics PostgreSQL tests",
)

BASE_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE
);
CREATE TABLE ella_runtime_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    account_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    profile_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ella_managed_cloud_consent_authority (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    consent_receipt_ref TEXT,
    profile_binding_id TEXT
);
"""


async def _pool_for_schema(schema: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )


async def _create_schema() -> tuple[asyncpg.Connection, asyncpg.Pool, str]:
    schema = f"account_diagnostics_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await _pool_for_schema(schema)
    async with pool.acquire() as conn:
        await conn.execute(BASE_SCHEMA)
        migration = MIGRATION.read_text(encoding="utf-8")
        await conn.execute(migration)
        await conn.execute(migration)
    return admin, pool, schema


async def _drop_schema(admin: asyncpg.Connection, pool: asyncpg.Pool, schema: str) -> None:
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


def _event() -> DiagnosticEventV1:
    return DiagnosticEventV1.model_validate(
        {
            "event_id": "event-1",
            "diagnostic_session_id": "session-1",
            "capture_attempt_id": "attempt-1",
            "account_binding_fingerprint": "a" * 64,
            "authority_generation": 3,
            "source_revision": "build-849",
            "layer": "account_binding",
            "event_name": "account_bound",
            "outcome": "succeeded",
            "retry_class": "never",
            "client_sequence": 0,
            "client_monotonic_ms": 0,
            "client_utc_time": "2026-09-05T19:00:00Z",
        }
    )


def test_event_append_is_idempotent_immutable_cascading_and_support_read_is_single_use():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "diagnostic-user"
            user_id = await pool.fetchval("INSERT INTO users (omi_uid) VALUES ($1) RETURNING id", uid)
            await pool.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, account_user_id, profile_user_id,
                    role, status, active, revision
                )
                VALUES
                    ($1, $1, $1, 'user', 'active', TRUE, 7),
                    ($1, $1, $1, 'scanner', 'active', TRUE, 99)
                """,
                user_id,
            )
            await pool.execute(
                """
                INSERT INTO ella_managed_cloud_consent_authority (
                    user_id, decision, consent_receipt_ref, profile_binding_id
                )
                VALUES ($1, 'granted', $2, 'aipb_test')
                """,
                user_id,
                managed_cloud_consent.consent_receipt_ref(uid, "aicr_test"),
            )

            async def get_pool():
                return pool

            repository = PostgresAccountDiagnosticsRepository(get_pool)
            authority = await repository.resolve_account_authority(uid)
            assert authority.binding_revision == 7
            assert authority.account_user_id == authority.profile_user_id == str(user_id)
            fingerprint = account_binding_fingerprint(
                uid=uid,
                profile_binding_id="aipb_test",
                binding_revision=authority.binding_revision,
                consent_receipt_id="aicr_test",
            )
            event = _event().model_copy(update={"account_binding_fingerprint": fingerprint})

            append_authority = {
                "uid": uid,
                "profile_binding_id": "aipb_test",
                "consent_receipt_id": "aicr_test",
                "expected_fingerprint": fingerprint,
            }
            assert await repository.append_events(authority, [event], **append_authority) == (1, 0)
            assert await repository.append_events(authority, [event], **append_authority) == (0, 1)
            assert len(await repository.list_session_events(authority, "session-1", **append_authority)) == 1

            same_id_different_payload = event.model_copy(update={"source_revision": "build-850"})
            with pytest.raises(DiagnosticEventConflict):
                await repository.append_events(authority, [same_id_different_payload], **append_authority)
            same_coordinate_different_id = event.model_copy(update={"event_id": "event-coordinate-collision"})
            with pytest.raises(DiagnosticEventConflict):
                await repository.append_events(authority, [same_coordinate_different_id], **append_authority)
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM ella_diagnostic_events WHERE diagnostic_session_id = 'session-1'"
                )
                == 1
            )

            concurrent_events = [
                event.model_copy(
                    update={
                        "event_id": f"event-race-{suffix}",
                        "diagnostic_session_id": "session-race",
                        "capture_attempt_id": "attempt-race",
                        "client_sequence": 0,
                    }
                )
                for suffix in ("a", "b")
            ]
            concurrent_results = await asyncio.gather(
                *(
                    repository.append_events(authority, [candidate], **append_authority)
                    for candidate in concurrent_events
                ),
                return_exceptions=True,
            )
            assert concurrent_results.count((1, 0)) == 1
            assert sum(isinstance(result, DiagnosticEventConflict) for result in concurrent_results) == 1
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM ella_diagnostic_events WHERE diagnostic_session_id = 'session-race'"
                )
                == 1
            )

            replacement_profile_id = await pool.fetchval(
                "INSERT INTO users (omi_uid) VALUES ('replacement-profile') RETURNING id"
            )
            await pool.execute(
                """
                UPDATE ella_runtime_bindings
                SET profile_user_id = $2, revision = 8, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND role = 'user'
                """,
                user_id,
                replacement_profile_id,
            )
            with pytest.raises(DiagnosticAccountAuthorityChanged):
                await repository.list_session_events(authority, "session-1", **append_authority)
            await pool.execute(
                """
                UPDATE ella_runtime_bindings
                SET profile_user_id = $1, revision = 7, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND role = 'user'
                """,
                user_id,
            )

            await pool.execute(
                """
                INSERT INTO ella_diagnostic_events (
                    account_user_id, profile_user_id, event_id,
                    diagnostic_session_id, capture_attempt_id,
                    account_binding_fingerprint, authority_generation,
                    layer, event_name, outcome, stable_failure_code,
                    client_sequence, client_monotonic_ms, client_utc_time,
                    payload, expires_at
                )
                SELECT $1, $1, 'event-oversized-' || series::text,
                       'session-oversized', 'attempt-oversized',
                       $2, 3, 'ble_transport',
                       CASE WHEN series = 0 THEN 'capture_attempt_started' ELSE 'peripheral_connected' END,
                       CASE WHEN series = 0 THEN 'started' ELSE 'succeeded' END,
                       NULL, series, series,
                       CURRENT_TIMESTAMP + series * INTERVAL '1 millisecond',
                       $3::jsonb, CURRENT_TIMESTAMP + INTERVAL '30 days'
                FROM generate_series(0, $4) AS series
                """,
                user_id,
                fingerprint,
                event.model_dump_json(exclude_none=True),
                MAX_EVENTS_PER_PROJECTION,
            )
            with pytest.raises(DiagnosticProjectionLimitExceeded):
                await repository.list_session_events(authority, "session-oversized", **append_authority)

            oversized_now = datetime.now(timezone.utc)
            oversized_grant_id = await repository.create_support_grant(
                authority,
                diagnostic_session_id="session-oversized",
                code_hash="d" * 64,
                evidence_not_before=oversized_now - timedelta(hours=1),
                evidence_not_after=oversized_now + timedelta(minutes=1),
                expires_at=oversized_now + timedelta(minutes=5),
                **append_authority,
            )
            with pytest.raises(DiagnosticProjectionLimitExceeded):
                await repository.consume_support_grant(
                    code_hash="d" * 64,
                    operator_id="operator@example.invalid",
                    case_id="case-oversized",
                    reason="customer_requested_help",
                )
            assert await pool.fetchval(
                "SELECT redeemed_at IS NULL FROM ella_diagnostic_support_grants WHERE id = $1::uuid",
                oversized_grant_id,
            )

            expired_event = event.model_copy(
                update={
                    "event_id": "event-expired",
                    "client_sequence": 1,
                }
            )
            await pool.execute(
                """
                INSERT INTO ella_diagnostic_events (
                    account_user_id, profile_user_id, event_id,
                    diagnostic_session_id, capture_attempt_id,
                    account_binding_fingerprint, authority_generation,
                    layer, event_name, outcome, stable_failure_code,
                    client_sequence, client_monotonic_ms, client_utc_time,
                    payload, server_received_at, expires_at
                ) VALUES (
                    $1, $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, NULL, $10, $11, $12,
                    $13::jsonb, CURRENT_TIMESTAMP - INTERVAL '31 days',
                    CURRENT_TIMESTAMP - INTERVAL '1 day'
                )
                """,
                user_id,
                expired_event.event_id,
                expired_event.diagnostic_session_id,
                expired_event.capture_attempt_id,
                expired_event.account_binding_fingerprint,
                expired_event.authority_generation,
                expired_event.layer.value,
                expired_event.event_name,
                expired_event.outcome.value,
                expired_event.client_sequence,
                expired_event.client_monotonic_ms,
                expired_event.client_utc_time,
                expired_event.model_dump_json(exclude_none=True),
            )
            assert await repository.delete_expired_events(batch_size=1) == 1
            assert await repository.delete_expired_events(batch_size=1) == 0

            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await pool.execute(
                    "UPDATE ella_diagnostic_events SET event_name = 'changed' WHERE account_user_id = $1",
                    user_id,
                )

            now = datetime.now(timezone.utc)
            await repository.create_support_grant(
                authority,
                diagnostic_session_id="session-1",
                code_hash="b" * 64,
                evidence_not_before=now - timedelta(hours=1),
                evidence_not_after=now + timedelta(seconds=1),
                expires_at=now + timedelta(minutes=5),
                **append_authority,
            )
            session_id, evidence = await repository.consume_support_grant(
                code_hash="b" * 64,
                operator_id="operator@example.invalid",
                case_id="case-1258",
                reason="customer_requested_help",
            )
            assert session_id == "session-1"
            assert len(evidence) == 1
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_support_audit") == 1
            with pytest.raises(DiagnosticSupportGrantInvalid):
                await repository.consume_support_grant(
                    code_hash="b" * 64,
                    operator_id="operator@example.invalid",
                    case_id="case-1258",
                    reason="customer_requested_help",
                )

            await pool.execute("DELETE FROM users WHERE id = $1", user_id)
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_events") == 0
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_support_grants") == 0
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_support_audit") == 0
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())


def test_append_and_support_grant_revalidate_consent_after_canonical_lock_wait():
    async def scenario() -> None:
        admin, pool, schema = await _create_schema()
        try:
            uid = "diagnostic-authority-race"
            user_id = await pool.fetchval("INSERT INTO users (omi_uid) VALUES ($1) RETURNING id", uid)
            await pool.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, account_user_id, profile_user_id,
                    role, status, active, revision
                ) VALUES ($1, $1, $1, 'user', 'active', TRUE, 7)
                """,
                user_id,
            )
            await pool.execute(
                """
                INSERT INTO ella_managed_cloud_consent_authority (
                    user_id, decision, consent_receipt_ref, profile_binding_id
                ) VALUES ($1, 'granted', $2, 'aipb_test')
                """,
                user_id,
                managed_cloud_consent.consent_receipt_ref(uid, "aicr_test"),
            )

            async def get_pool():
                return pool

            repository = PostgresAccountDiagnosticsRepository(get_pool)
            authority = await repository.resolve_account_authority(uid)
            fingerprint = account_binding_fingerprint(
                uid=uid,
                profile_binding_id="aipb_test",
                binding_revision=authority.binding_revision,
                consent_receipt_id="aicr_test",
            )
            event = _event().model_copy(update={"account_binding_fingerprint": fingerprint})
            async with pool.acquire() as writer, writer.transaction():
                owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
                await authority_advisory_lock.acquire_authority_lock(writer, owner=owner)
                append_task = asyncio.create_task(
                    repository.append_events(
                        authority,
                        [event],
                        uid=uid,
                        profile_binding_id="aipb_test",
                        consent_receipt_id="aicr_test",
                        expected_fingerprint=fingerprint,
                    )
                )
                await asyncio.sleep(0.05)
                assert not append_task.done()
                await writer.execute(
                    """
                    UPDATE ella_managed_cloud_consent_authority
                    SET decision = 'revoked', consent_receipt_ref = NULL
                    WHERE user_id = $1
                    """,
                    user_id,
                )

            with pytest.raises(DiagnosticAccountAuthorityChanged):
                await append_task
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_events") == 0

            await pool.execute(
                """
                UPDATE ella_managed_cloud_consent_authority
                SET decision = 'granted', consent_receipt_ref = $2
                WHERE user_id = $1
                """,
                user_id,
                managed_cloud_consent.consent_receipt_ref(uid, "aicr_test"),
            )
            now = datetime.now(timezone.utc)
            async with pool.acquire() as writer, writer.transaction():
                owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
                await authority_advisory_lock.acquire_authority_lock(writer, owner=owner)
                grant_task = asyncio.create_task(
                    repository.create_support_grant(
                        authority,
                        diagnostic_session_id="session-1",
                        code_hash="c" * 64,
                        evidence_not_before=now - timedelta(hours=1),
                        evidence_not_after=now,
                        expires_at=now + timedelta(minutes=5),
                        uid=uid,
                        profile_binding_id="aipb_test",
                        consent_receipt_id="aicr_test",
                        expected_fingerprint=fingerprint,
                    )
                )
                await asyncio.sleep(0.05)
                assert not grant_task.done()
                await writer.execute(
                    """
                    UPDATE ella_managed_cloud_consent_authority
                    SET decision = 'revoked', consent_receipt_ref = NULL
                    WHERE user_id = $1
                    """,
                    user_id,
                )

            with pytest.raises(DiagnosticAccountAuthorityChanged):
                await grant_task
            assert await pool.fetchval("SELECT COUNT(*) FROM ella_diagnostic_support_grants") == 0
        finally:
            await _drop_schema(admin, pool, schema)

    asyncio.run(scenario())
