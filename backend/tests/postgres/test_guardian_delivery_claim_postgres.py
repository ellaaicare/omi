"""Real-PostgreSQL concurrency proof for the Guardian atomic delivery claim.

`_reserve_delivery_steps` and `email_send` must share ONE atomic claim path:
a single `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE status = ANY(retryable)
RETURNING ...` against the live unique (trace_id, channel, target) index. A
caller may dispatch only when that statement's RETURNING clause yields a row;
a no-row result is a skip, never a read-then-write race. These tests exercise
that guarantee against a real Postgres server (two live connections, not a
fake pool), because the property under test — "exactly one statement wins the
unique-index race" — is exactly the thing an in-process fake cannot prove.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import asyncpg
import pytest

from ella.routers import guardian

T = TypeVar("T")

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for Guardian delivery claim PostgreSQL tests",
)

MINIMAL_USERS_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE
);
"""


async def _create_schema() -> tuple[asyncpg.Connection, asyncpg.Pool, str]:
    schema = f"guardian_claim_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=2,
        max_size=8,
        server_settings={"search_path": schema},
    )
    async with pool.acquire() as conn:
        await conn.execute(MINIMAL_USERS_SCHEMA)
        await conn.execute((MIGRATIONS / "006_create_guardian_delivery_log.sql").read_text(encoding="utf-8"))
    return admin, pool, schema


async def _drop_schema(admin: asyncpg.Connection, pool: asyncpg.Pool, schema: str) -> None:
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


async def _run_with_database(scenario: Callable[[asyncpg.Pool], Awaitable[None]]) -> None:
    admin, pool, schema = await _create_schema()
    previous_pool = guardian._pool
    guardian._pool = pool
    try:
        await scenario(pool)
    finally:
        guardian._pool = previous_pool
        await _drop_schema(admin, pool, schema)


async def _release_together(coro_a: Awaitable[T], coro_b: Awaitable[T]) -> tuple[T, T]:
    """Start both coroutines, hold them at a shared barrier, then release together.

    This is the "two connections released from a barrier" shape: both sides
    reach the same `asyncio.Event().wait()` before either issues its claim
    statement, so the statements race against the real unique index rather
    than running one strictly after the other.
    """
    barrier = asyncio.Event()
    results: dict[str, T] = {}

    async def runner(name: str, coro: Awaitable[T]) -> None:
        await barrier.wait()
        results[name] = await coro

    task_a = asyncio.create_task(runner("a", coro_a))
    task_b = asyncio.create_task(runner("b", coro_b))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    barrier.set()
    await asyncio.gather(task_a, task_b)
    return results["a"], results["b"]


async def _delivery_row(pool: asyncpg.Pool, trace_id: str, channel: str, target: str):
    return await pool.fetchrow(
        "SELECT * FROM guardian_delivery_log WHERE trace_id = $1 AND channel = $2 AND target = $3",
        trace_id,
        channel,
        target,
    )


async def _row_count(pool: asyncpg.Pool, trace_id: str, channel: str, target: str) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM guardian_delivery_log WHERE trace_id = $1 AND channel = $2 AND target = $3",
        trace_id,
        channel,
        target,
    )


def test_reserve_delivery_steps_concurrent_claim_is_exactly_one_winner_one_skip_one_row():
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        step = {"channel": "imessage", "target": "user", "recipient_phone": "+15550000001"}
        downstream_calls = 0

        async def attempt() -> tuple[list[dict], list[dict]]:
            return await guardian._reserve_delivery_steps(trace_id, "uid-race", [dict(step)])

        result_a, result_b = await _release_together(attempt(), attempt())

        for pending, _skipped in (result_a, result_b):
            if pending:
                downstream_calls += 1

        winners = [pending for pending, _skipped in (result_a, result_b) if pending]
        skippers = [skipped for _pending, skipped in (result_a, result_b) if skipped]
        assert len(winners) == 1
        assert len(skippers) == 1
        assert winners[0] == [step]
        assert skippers[0][0]["skip_reason"] == "already_pending"
        assert downstream_calls == 1

        assert await _row_count(pool, trace_id, "imessage", "user") == 1
        row = await _delivery_row(pool, trace_id, "imessage", "user")
        assert row["status"] == "pending"
        assert row["uid"] == "uid-race"
        assert row["channel"] == "imessage"
        assert row["target"] == "user"

    asyncio.run(_run_with_database(scenario))


def test_email_send_concurrent_claim_is_exactly_one_mocked_send(monkeypatch):
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        sends: list[str] = []

        class _FakeSMTP:
            def __init__(self, _host, _port):
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
                sends.append(message["To"])

        monkeypatch.setattr(guardian.smtplib, "SMTP", _FakeSMTP)

        async def attempt():
            return await guardian.email_send(
                guardian.EmailSendRequest(
                    to="caregiver@example.test",
                    subject="Alert",
                    body="Body",
                    trace_id=trace_id,
                    uid="uid-race",
                    target="caregiver",
                ),
                x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
                subject_uid="uid-race",
            )

        result_a, result_b = await _release_together(attempt(), attempt())

        sent_results = [result for result in (result_a, result_b) if result["sent"] is True]
        skipped_results = [result for result in (result_a, result_b) if result["sent"] is False]
        assert len(sent_results) == 1
        assert len(skipped_results) == 1
        # The descriptive follow-up read is inherently timing-dependent (by
        # design — see `_describe_delivery_status`): the loser may observe
        # the winner mid-flight ("sending") or already finished ("sent").
        # Either way it never authorized a second dispatch, which is the
        # property this test actually proves via `sends`/row-count below.
        assert skipped_results[0]["reason"] in ("already_sending", "already_sent")
        assert len(sends) == 1

        assert await _row_count(pool, trace_id, "email", "caregiver") == 1
        row = await _delivery_row(pool, trace_id, "email", "caregiver")
        assert row["status"] == "sent"

    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "postgres-test-guardian-key")
    asyncio.run(_run_with_database(scenario))


def test_reclaims_after_retryable_dispatch_failed_but_not_after_sent():
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        step = {"channel": "sms", "target": "caregiver", "recipient_phone": "+15550000002"}

        first_pending, first_skipped = await guardian._reserve_delivery_steps(trace_id, "uid-1", [dict(step)])
        assert first_pending == [step]
        assert first_skipped == []

        await guardian._mark_reserved_steps_dispatch_failed(trace_id, [dict(step)], "n8n timeout")
        row = await _delivery_row(pool, trace_id, "sms", "caregiver")
        assert row["status"] == "dispatch_failed"

        retried_pending, retried_skipped = await guardian._reserve_delivery_steps(trace_id, "uid-1", [dict(step)])
        assert retried_pending == [step]
        assert retried_skipped == []
        row = await _delivery_row(pool, trace_id, "sms", "caregiver")
        assert row["status"] == "pending"

        await pool.execute(
            "UPDATE guardian_delivery_log SET status = 'sent' WHERE trace_id = $1 AND channel = $2 AND target = $3",
            trace_id,
            "sms",
            "caregiver",
        )
        final_pending, final_skipped = await guardian._reserve_delivery_steps(trace_id, "uid-1", [dict(step)])
        assert final_pending == []
        assert final_skipped[0]["skip_reason"] == "already_sent"

    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize(
    "blocking_status",
    ["pending", "sending", "sent", "success", "delivered"],
)
def test_blocking_statuses_are_never_reclaimed(blocking_status):
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        await pool.execute(
            """
            INSERT INTO guardian_delivery_log (trace_id, uid, channel, target, status)
            VALUES ($1, 'uid-seed', 'imessage', 'user', $2)
            """,
            trace_id,
            blocking_status,
        )

        pending, skipped = await guardian._reserve_delivery_steps(
            trace_id, "uid-1", [{"channel": "imessage", "target": "user"}]
        )
        assert pending == []
        assert skipped[0]["skip_reason"] == f"already_{blocking_status}"
        row = await _delivery_row(pool, trace_id, "imessage", "user")
        assert row["status"] == blocking_status

    asyncio.run(_run_with_database(scenario))


def test_unrecognized_status_is_not_inferred_as_retryable():
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        await pool.execute(
            """
            INSERT INTO guardian_delivery_log (trace_id, uid, channel, target, status)
            VALUES ($1, 'uid-seed', 'imessage', 'user', 'quarantined')
            """,
            trace_id,
        )

        pending, skipped = await guardian._reserve_delivery_steps(
            trace_id, "uid-1", [{"channel": "imessage", "target": "user"}]
        )
        assert pending == []
        assert skipped[0]["skip_reason"] == "already_quarantined"
        row = await _delivery_row(pool, trace_id, "imessage", "user")
        assert row["status"] == "quarantined"

    asyncio.run(_run_with_database(scenario))


def test_claim_inside_rolled_back_transaction_leaves_no_residue():
    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"

        class _SentinelFailure(Exception):
            pass

        with pytest.raises(_SentinelFailure):
            async with pool.acquire() as conn:
                async with conn.transaction():
                    claimed = await guardian._claim_delivery_row(
                        conn,
                        trace_id=trace_id,
                        uid="uid-rollback",
                        channel="email",
                        target="user",
                        status="pending",
                        provider_response={},
                    )
                    assert claimed is not None
                    raise _SentinelFailure("simulated failure after claim, before commit")

        assert await _row_count(pool, trace_id, "email", "user") == 0

        recovered = await guardian._claim_delivery_row(
            pool,
            trace_id=trace_id,
            uid="uid-rollback",
            channel="email",
            target="user",
            status="pending",
            provider_response={},
        )
        assert recovered is not None
        assert await _row_count(pool, trace_id, "email", "user") == 1

    asyncio.run(_run_with_database(scenario))


def test_claim_survives_restart_and_blocks_reclaim_of_interrupted_pending():
    """A crash after claiming (before dispatch completes/marks failure) must not
    let a fresh process instance double-dispatch — only explicit reconciliation
    (marking the row a retryable terminal failure) reopens the claim."""

    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        step = {"channel": "imessage", "target": "user"}

        pending, skipped = await guardian._reserve_delivery_steps(trace_id, "uid-1", [dict(step)])
        assert pending == [step]
        assert skipped == []

        # Simulate an application restart: a brand-new pool/connection set
        # against the same database, standing in for a fresh process with no
        # in-memory knowledge of the in-flight dispatch.
        restarted_pool = await asyncpg.create_pool(
            TEST_DSN,
            min_size=1,
            max_size=2,
            server_settings={"search_path": (await pool.fetchval("SELECT current_schema()"))},
        )
        previous_pool = guardian._pool
        guardian._pool = restarted_pool
        try:
            reclaim_pending, reclaim_skipped = await guardian._reserve_delivery_steps(trace_id, "uid-1", [dict(step)])
            assert reclaim_pending == []
            assert reclaim_skipped[0]["skip_reason"] == "already_pending"

            await guardian._mark_reserved_steps_dispatch_failed(trace_id, [dict(step)], "process restarted mid-flight")
            recovered_pending, recovered_skipped = await guardian._reserve_delivery_steps(
                trace_id, "uid-1", [dict(step)]
            )
            assert recovered_pending == [step]
            assert recovered_skipped == []
        finally:
            guardian._pool = previous_pool
            await restarted_pool.close()

    asyncio.run(_run_with_database(scenario))


def test_receiver_replay_with_same_idempotency_key_has_no_second_side_effect(monkeypatch):
    """The (trace_id, channel, target) tuple is the idempotency key for the
    email receiver. Replaying the same request (e.g. a retried webhook) must
    observe the first outcome, not repeat the SMTP side effect."""

    async def scenario(pool: asyncpg.Pool) -> None:
        trace_id = f"trace-{uuid.uuid4().hex}"
        sends: list[str] = []

        class _FakeSMTP:
            def __init__(self, _host, _port):
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
                sends.append(message["To"])

        monkeypatch.setattr(guardian.smtplib, "SMTP", _FakeSMTP)

        def build_request():
            return guardian.EmailSendRequest(
                to="caregiver@example.test",
                subject="Alert",
                body="Body",
                trace_id=trace_id,
                uid="uid-replay",
                target="caregiver",
            )

        first = await guardian.email_send(
            build_request(),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
            subject_uid="uid-replay",
        )
        assert first["sent"] is True
        assert len(sends) == 1

        replay = await guardian.email_send(
            build_request(),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
            subject_uid="uid-replay",
        )
        assert replay["sent"] is False
        assert replay["reason"] == "already_sent"
        assert len(sends) == 1

        assert await _row_count(pool, trace_id, "email", "caregiver") == 1

    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "postgres-test-guardian-key")
    asyncio.run(_run_with_database(scenario))
