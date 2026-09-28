import asyncio
import os
import uuid

import asyncpg
import pytest

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "guardian-delivery-claim-test")
os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")

from ella.routers import guardian

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for Guardian delivery claim PostgreSQL tests",
)


SCHEMA = """
CREATE TABLE guardian_delivery_log (
    id BIGSERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    caregiver_id TEXT,
    recipient_phone TEXT,
    recipient_email TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    provider_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX guardian_delivery_log_trace_channel_target_uidx
    ON guardian_delivery_log (trace_id, channel, target);

CREATE TABLE guardian_pipeline_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    latency_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def _run_with_database(scenario):
    schema = f"guardian_claim_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=2,
        max_size=6,
        server_settings={"search_path": schema},
    )
    try:
        await pool.execute(SCHEMA)
        await scenario(pool, schema)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _step(channel="imessage", target="user"):
    return {
        "channel": channel,
        "target": target,
        "recipient_phone": "+15550000001",
        "recipient_email": "synthetic@example.test",
    }


def test_two_connections_produce_exactly_one_delivery_claim():
    async def scenario(pool, _schema):
        gate = asyncio.Event()
        ready = 0
        ready_lock = asyncio.Lock()

        async def contender(connection):
            nonlocal ready
            async with ready_lock:
                ready += 1
                if ready == 2:
                    gate.set()
            await gate.wait()
            return await guardian._claim_delivery_step(
                connection,
                trace_id="trace-concurrent",
                uid="uid-1",
                step=_step(),
                claimed_status="pending",
            )

        async with pool.acquire() as first, pool.acquire() as second:
            results = await asyncio.gather(contender(first), contender(second))

        assert sorted(claimed for claimed, _reason in results) == [False, True]
        loser_reason = next(reason for claimed, reason in results if not claimed)
        assert loser_reason == "already_pending"
        assert (
            await pool.fetchval("SELECT count(*) FROM guardian_delivery_log WHERE trace_id = 'trace-concurrent'") == 1
        )

    asyncio.run(_run_with_database(scenario))


def test_claim_retries_only_explicit_failures_and_survives_rollback_and_new_pool():
    async def scenario(pool, schema):
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            claimed, reason = await guardian._claim_delivery_step(
                connection,
                trace_id="trace-rollback",
                uid="uid-1",
                step=_step(),
                claimed_status="pending",
            )
            assert (claimed, reason) == (True, None)
            await transaction.rollback()

        assert await pool.fetchval("SELECT count(*) FROM guardian_delivery_log") == 0

        claimed, reason = await guardian._claim_delivery_step(
            pool,
            trace_id="trace-retry",
            uid="uid-1",
            step=_step(),
            claimed_status="pending",
        )
        assert (claimed, reason) == (True, None)

        await pool.execute("UPDATE guardian_delivery_log SET status = 'dispatch_failed' WHERE trace_id = 'trace-retry'")
        claimed, reason = await guardian._claim_delivery_step(
            pool,
            trace_id="trace-retry",
            uid="uid-1",
            step=_step(),
            claimed_status="pending",
        )
        assert (claimed, reason) == (True, None)

        await pool.execute(
            "UPDATE guardian_delivery_log SET status = 'unexpected_state' WHERE trace_id = 'trace-retry'"
        )
        claimed, reason = await guardian._claim_delivery_step(
            pool,
            trace_id="trace-retry",
            uid="uid-1",
            step=_step(),
            claimed_status="pending",
        )
        assert (claimed, reason) == (False, "already_unexpected_state")

        await pool.execute("UPDATE guardian_delivery_log SET status = 'error' WHERE trace_id = 'trace-retry'")
        claimed, reason = await guardian._claim_delivery_step(
            pool,
            trace_id="trace-retry",
            uid="uid-2",
            step=_step(),
            claimed_status="pending",
        )
        assert (claimed, reason) == (False, "owner_mismatch")

        restarted_pool = await asyncpg.create_pool(
            TEST_DSN,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema},
        )
        try:
            claimed, reason = await guardian._claim_delivery_step(
                restarted_pool,
                trace_id="trace-retry",
                uid="uid-1",
                step=_step(),
                claimed_status="pending",
            )
            assert (claimed, reason) == (True, None)
            claimed, reason = await guardian._claim_delivery_step(
                restarted_pool,
                trace_id="trace-retry",
                uid="uid-1",
                step=_step(),
                claimed_status="pending",
            )
            assert (claimed, reason) == (False, "already_pending")
        finally:
            await restarted_pool.close()

    asyncio.run(_run_with_database(scenario))


def test_concurrent_direct_email_uses_one_database_winner(monkeypatch):
    class _SMTP:
        sent = []

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            return None

        def login(self, *_args):
            return None

        def send_message(self, message):
            self.sent.append(message)

    async def scenario(pool, _schema):
        previous_pool = guardian._pool
        previous_key = guardian.GUARDIAN_WEBHOOK_KEY
        guardian._pool = pool
        guardian.GUARDIAN_WEBHOOK_KEY = "guardian-claim-test-key"
        _SMTP.sent = []
        monkeypatch.setattr(guardian.smtplib, "SMTP", _SMTP)
        try:
            request = guardian.EmailSendRequest(
                to="synthetic@example.test",
                subject="Guardian delivery claim test",
                body="Synthetic body",
                trace_id="trace-email",
                uid="uid-1",
                target="caregiver",
            )
            results = await asyncio.gather(
                guardian.email_send(
                    request.model_copy(deep=True),
                    x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
                    subject_uid="uid-1",
                ),
                guardian.email_send(
                    request.model_copy(deep=True),
                    x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
                    subject_uid="uid-1",
                ),
            )
        finally:
            guardian._pool = previous_pool
            guardian.GUARDIAN_WEBHOOK_KEY = previous_key

        assert sorted(result["sent"] for result in results) == [False, True]
        assert len(_SMTP.sent) == 1
        row = await pool.fetchrow("SELECT uid, status FROM guardian_delivery_log WHERE trace_id = 'trace-email'")
        assert dict(row) == {"uid": "uid-1", "status": "sent"}

    asyncio.run(_run_with_database(scenario))
