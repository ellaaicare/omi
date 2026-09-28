import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from unittest import mock

import asyncpg
import pytest

from ella.services import guardian_playback_ledger as ledger
from utils.ella import scanner as scanner_module

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "018_create_guardian_playback_ledger.sql"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for guardian playback ledger PostgreSQL tests",
)


async def _run_with_database(scenario: Callable[[asyncpg.Pool], Awaitable[None]]) -> None:
    pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)
    try:
        async with pool.acquire() as conn:
            await conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
            await conn.execute("TRUNCATE TABLE guardian_playback_ledger RESTART IDENTITY")
        await scenario(pool)
    finally:
        await pool.close()


def test_generated_queued_fetched_are_not_evidence_of_playback():
    """generated/queued/fetched-only rows must never surface as played candidates."""

    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(
            pool,
            uid="uid-1",
            playback_id="pb-1",
            queue_item_id="pb-1",
            purpose="wake_word",
            playback_text="Hi Greg, I heard my name.",
            text_provenance="tts_generated",
        )
        await ledger.record_queued(pool, uid="uid-1", playback_id="pb-1")
        await ledger.record_fetched(pool, uid="uid-1", playback_id="pb-1", route="Speaker", device_class="high")

        candidates = await ledger.get_played_candidates(pool, "uid-1", window_seconds=3600)
        assert candidates == []

        row = await pool.fetchrow(
            "SELECT status FROM guardian_playback_ledger WHERE uid = $1 AND playback_id = $2", "uid-1", "pb-1"
        )
        assert row["status"] == "fetched"

    asyncio.run(_run_with_database(scenario))


def test_started_receipt_makes_a_played_candidate_visible():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(
            pool,
            uid="uid-1",
            playback_id="pb-2",
            playback_text="Hi Greg, I heard my name.",
        )
        await ledger.record_playback_receipt(
            pool,
            uid="uid-1",
            playback_id="pb-2",
            event_type="started",
            route="Speaker",
            device_class="high",
        )

        candidates = await ledger.get_played_candidates(pool, "uid-1", window_seconds=3600)
        assert len(candidates) == 1
        assert candidates[0].playback_id == "pb-2"
        assert candidates[0].playback_text == "Hi Greg, I heard my name."

    asyncio.run(_run_with_database(scenario))


def test_persists_across_separate_pool_instances_simulating_app_restart():
    """Two asyncpg pools against the same disposable database, like two app instances."""

    async def scenario(pool_a: asyncpg.Pool) -> None:
        await ledger.record_generated(pool_a, uid="uid-restart", playback_id="pb-restart", playback_text="hello")
        await ledger.record_playback_receipt(pool_a, uid="uid-restart", playback_id="pb-restart", event_type="started")

        pool_b = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2)
        try:
            candidates = await ledger.get_played_candidates(pool_b, "uid-restart", window_seconds=3600)
            assert len(candidates) == 1
            assert candidates[0].playback_id == "pb-restart"
        finally:
            await pool_b.close()

    asyncio.run(_run_with_database(scenario))


def test_expired_rows_are_deleted_by_cleanup_and_recent_rows_survive():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-1", playback_id="pb-old", playback_text="old")
        await ledger.record_generated(pool, uid="uid-1", playback_id="pb-new", playback_text="new")

        old_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        await pool.execute(
            "UPDATE guardian_playback_ledger SET created_at = $1 WHERE playback_id = 'pb-old'",
            old_cutoff,
        )

        deleted = await ledger.cleanup_expired(pool, retention_days=7)
        assert deleted == 1

        remaining = await pool.fetch("SELECT playback_id FROM guardian_playback_ledger")
        assert {row["playback_id"] for row in remaining} == {"pb-new"}

    asyncio.run(_run_with_database(scenario))


def test_cross_owner_mismatched_uid_is_denied_not_silently_written():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-owner", playback_id="pb-shared", playback_text="owner's whisper")

        with pytest.raises(ledger.PlaybackLedgerOwnershipError):
            await ledger.record_fetched(pool, uid="uid-attacker", playback_id="pb-shared")

        with pytest.raises(ledger.PlaybackLedgerOwnershipError):
            await ledger.record_playback_receipt(
                pool, uid="uid-attacker", playback_id="pb-shared", event_type="started"
            )

        row = await pool.fetchrow("SELECT uid, status FROM guardian_playback_ledger WHERE playback_id = 'pb-shared'")
        assert row["uid"] == "uid-owner"
        assert row["status"] == "generated"

        candidates = await ledger.get_played_candidates(pool, "uid-attacker", window_seconds=3600)
        assert candidates == []

    asyncio.run(_run_with_database(scenario))


def test_duplicate_and_out_of_order_receipts_are_idempotent():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-1", playback_id="pb-3", playback_text="hi")

        status = await ledger.record_playback_receipt(pool, uid="uid-1", playback_id="pb-3", event_type="completed")
        assert status == "completed"

        # A duplicate completed receipt (network retry) must be a no-op.
        status = await ledger.record_playback_receipt(pool, uid="uid-1", playback_id="pb-3", event_type="completed")
        assert status == "completed"

        # An out-of-order "started" arriving after "completed" must not roll status back.
        status = await ledger.record_playback_receipt(pool, uid="uid-1", playback_id="pb-3", event_type="started")
        assert status == "completed"

        # A conflicting "failed" after "completed" must never overwrite the earlier terminal receipt.
        status = await ledger.record_playback_receipt(pool, uid="uid-1", playback_id="pb-3", event_type="failed")
        assert status == "completed"

        row = await pool.fetchrow(
            "SELECT status, completed_at, failed_at FROM guardian_playback_ledger WHERE playback_id = 'pb-3'"
        )
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["failed_at"] is None

    asyncio.run(_run_with_database(scenario))


def test_confirmed_echo_replay_never_creates_a_second_ledger_row():
    """Re-recording generated/queued for the same playback_id must stay one row (idempotent upsert)."""

    async def scenario(pool: asyncpg.Pool) -> None:
        for _ in range(3):
            await ledger.record_generated(pool, uid="uid-1", playback_id="pb-4", playback_text="hi")
            await ledger.record_queued(pool, uid="uid-1", playback_id="pb-4")

        count = await pool.fetchval(
            "SELECT COUNT(*) FROM guardian_playback_ledger WHERE uid = $1 AND playback_id = $2",
            "uid-1",
            "pb-4",
        )
        assert count == 1

    asyncio.run(_run_with_database(scenario))


def test_get_played_candidates_is_owner_scoped():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-a", playback_id="pb-a", playback_text="a's whisper")
        await ledger.record_playback_receipt(pool, uid="uid-a", playback_id="pb-a", event_type="started")
        await ledger.record_generated(pool, uid="uid-b", playback_id="pb-b", playback_text="b's whisper")
        await ledger.record_playback_receipt(pool, uid="uid-b", playback_id="pb-b", event_type="started")

        candidates_a = await ledger.get_played_candidates(pool, "uid-a", window_seconds=3600)
        candidates_b = await ledger.get_played_candidates(pool, "uid-b", window_seconds=3600)

        assert [c.playback_id for c in candidates_a] == ["pb-a"]
        assert [c.playback_id for c in candidates_b] == ["pb-b"]

    asyncio.run(_run_with_database(scenario))


def test_played_candidate_outside_window_is_excluded():
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-1", playback_id="pb-far", playback_text="old whisper")
        await ledger.record_playback_receipt(pool, uid="uid-1", playback_id="pb-far", event_type="started")
        await pool.execute(
            "UPDATE guardian_playback_ledger SET started_at = NOW() - INTERVAL '10 minutes' WHERE playback_id = 'pb-far'"
        )

        candidates = await ledger.get_played_candidates(pool, "uid-1", window_seconds=45)
        assert candidates == []

        candidates_wide = await ledger.get_played_candidates(pool, "uid-1", window_seconds=3600)
        assert len(candidates_wide) == 1

    asyncio.run(_run_with_database(scenario))


def test_select_playback_ledger_candidates_survives_repeated_calls_thread_and_running_loop():
    """Regression for the `asyncio.run()`-per-call bug in
    `utils.ella.scanner.select_playback_ledger_candidates`.

    That selector wraps its async ledger fetch in a fresh `asyncio.run(...)`
    on every call, sharing the module-level asyncpg pool from
    `database.ella_postgres.get_ella_postgres_pool`. Reproduced against a
    real ledger row: the first call in a process works, but asyncpg pools
    are bound to the loop that created them, so a second `asyncio.run()`
    call either reuses a pool whose connections belong to an
    already-closed loop, or raises outright when called from a thread that
    already has a running loop — silently degrading every later lookup (or
    any lookup from a worker thread or a running loop) to `[]`, which
    detaches candidates from the classifier without ever surfacing an
    error.

    Exercises the real, unmocked sync entry point end-to-end: three calls
    in a row, one from a worker thread (matching how `send_to_scanner` is
    actually invoked via `run_in_threadpool`), and one from inside the
    caller's own running event loop — each must still return the played
    candidate.
    """

    async def _setup(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-thread", playback_id="pb-thread", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-thread",
            playback_id="pb-thread",
            event_type="started",
            route="Speaker",
            device_class="high",
        )

    asyncio.run(_run_with_database(_setup))

    async def _test_dedicated_pool(**_kwargs):
        return await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)

    def _select():
        return scanner_module.select_playback_ledger_candidates("uid-thread", window_seconds=3600, limit=5)

    original_pool = scanner_module._candidate_pool
    with mock.patch.object(scanner_module, "create_dedicated_ella_postgres_pool", _test_dedicated_pool):
        scanner_module._candidate_pool = None
        try:
            for _ in range(3):
                result = _select()
                assert [c["playback_id"] for c in result] == ["pb-thread"]

            thread_result: dict = {}
            worker = threading.Thread(target=lambda: thread_result.__setitem__("value", _select()))
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert [c["playback_id"] for c in thread_result["value"]] == ["pb-thread"]

            async def _call_from_running_loop():
                # Calls the sync selector directly from inside a running
                # loop on this thread — previously this raised
                # "asyncio.run() cannot be called from a running event loop".
                return _select()

            result_from_loop = asyncio.run(_call_from_running_loop())
            assert [c["playback_id"] for c in result_from_loop] == ["pb-thread"]
        finally:
            pool_to_close = scanner_module._candidate_pool
            scanner_module._candidate_pool = original_pool
            if pool_to_close is not None and scanner_module._candidate_loop is not None:
                close_future = asyncio.run_coroutine_threadsafe(pool_to_close.close(), scanner_module._candidate_loop)
                close_future.result(timeout=10)
