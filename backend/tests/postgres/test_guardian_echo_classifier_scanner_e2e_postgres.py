"""End-to-end regression: the backend, not n8n, performs the echo judgment.

Exercises the real, unmocked call chain the scanner actually runs on every
dispatch — `send_to_scanner` -> `select_playback_ledger_candidates` (real
Postgres-backed playback ledger) -> `classify_playback_source` (real
validation/fail-open logic) -> the confirmed-echo / mixed / fail-open
branching in `send_to_scanner` itself. Only the two genuine network
boundaries are mocked: the LLM provider's HTTP call and the scanner/n8n
webhook POST — proving gate C is an executable backend integration, not
documentation for n8n to implement.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest

from ella.services import guardian_echo_classifier as classifier
from ella.services import guardian_playback_ledger as ledger
from utils.ella import scanner as scanner_module

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "018_create_guardian_playback_ledger.sql"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for the scanner echo classifier end-to-end PostgreSQL test",
)

# `guardian_queue` is not defined by a migration in this (public) repo — its
# schema here mirrors the one other Postgres regressions in this suite use
# (see tests/postgres/test_authority_advisory_lock_postgres.py), which in
# turn mirrors the INSERT in ella.routers.guardian.enqueue.
GUARDIAN_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_queue (
    id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL,
    message TEXT,
    trigger_type TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);
"""

# Minimal mirror of the columns `_insert_wake_ack_direct` actually reads
# (see tests/postgres/test_authority_advisory_lock_postgres.py for the
# fuller schema other Postgres regressions in this suite use).
USERS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    omi_uid TEXT PRIMARY KEY,
    guardian_mode TEXT
);
"""


def _postgres_connection_kwargs_from_dsn(dsn: str) -> dict:
    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "database": (parsed.path or "/").lstrip("/"),
    }


class _FakeHttpResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    queued_responses: list = []
    raise_on_post = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, **_kwargs):
        if _FakeAsyncClient.raise_on_post is not None:
            raise _FakeAsyncClient.raise_on_post
        return _FakeAsyncClient.queued_responses.pop(0)


class _FakeScannerWebhookResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.headers = {}


def _decision_response(**overrides):
    body = {
        "schema_version": "guardian_playback_source_v1",
        "is_ella_playback": True,
        "source": "ella_playback",
        "contains_additional_live_speech": False,
        "matched_playback_ids": ["pb-echo"],
        "live_speech_spans": [],
        "confidence": 0.95,
        "reason_code": "test_fixture",
    }
    body.update(overrides)
    return _FakeHttpResponse(json_body={"decision": body})


async def _run_with_database(scenario: Callable[[asyncpg.Pool], Awaitable[None]]) -> None:
    pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)
    try:
        async with pool.acquire() as conn:
            await conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
            await conn.execute("TRUNCATE TABLE guardian_playback_ledger RESTART IDENTITY")
            await conn.execute(GUARDIAN_QUEUE_SCHEMA)
            await conn.execute("TRUNCATE TABLE guardian_queue")
            await conn.execute(USERS_TABLE_SCHEMA)
            await conn.execute("TRUNCATE TABLE users")
        await scenario(pool)
    finally:
        await pool.close()


async def _insert_active_guardian_user(pool: asyncpg.Pool, uid: str, *, guardian_mode: str = "active_support") -> None:
    await pool.execute(
        "INSERT INTO users (omi_uid, guardian_mode) VALUES ($1, $2) "
        "ON CONFLICT (omi_uid) DO UPDATE SET guardian_mode = EXCLUDED.guardian_mode",
        uid,
        guardian_mode,
    )


async def _has_any_queue_row(pool: asyncpg.Pool) -> bool:
    return bool(await pool.fetchval("SELECT COUNT(*) FROM guardian_queue"))


async def _wait_until(
    condition: Callable[[], Awaitable[bool]], *, timeout: float = 3.0, interval: float = 0.05
) -> bool:
    """Poll a fire-and-forget background write until it lands or the
    timeout is spent — `_enqueue_wake_ack`'s direct-DB insert runs on its
    own daemon thread, so it is not guaranteed to have landed the instant
    `send_to_scanner` returns."""
    deadline = time.monotonic() + timeout
    while True:
        if await condition():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


def _drive_send_to_scanner(monkeypatch, *, uid, segments, decision_response, raise_on_post=None):
    """Point the scanner's dedicated candidate pool at the disposable test
    database, mock only the LLM provider HTTP call and the scanner/n8n
    webhook, and run the real `send_to_scanner` synchronously."""
    posts = []

    def fake_webhook_post(_url, json, headers, timeout):
        posts.append(json)
        return _FakeScannerWebhookResponse(200)

    _FakeAsyncClient.queued_responses = [decision_response] if decision_response is not None else []
    _FakeAsyncClient.raise_on_post = raise_on_post

    async def _test_dedicated_pool(**_kwargs):
        return await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(classifier, "_JEV_API_KEY", "test-jev-key")
    monkeypatch.setattr(classifier, "_FALLBACK_API_KEY", "")
    monkeypatch.setattr(scanner_module, "_log_trace_event", lambda *a, **k: None)
    monkeypatch.setattr(scanner_module, "_enqueue_wake_ack", lambda *a, **k: None)
    monkeypatch.setattr(scanner_module, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")
    monkeypatch.setattr(scanner_module.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner_module, "_post_scanner_webhook", fake_webhook_post)
    monkeypatch.setattr(scanner_module, "create_dedicated_ella_postgres_pool", _test_dedicated_pool)
    scanner_module._candidate_pool = None

    try:
        status = scanner_module.send_to_scanner(uid, "conversation-e2e", segments, guardian_mode="active_support")
        return status, posts
    finally:
        pool_to_close = scanner_module._candidate_pool
        scanner_module._candidate_pool = None
        if pool_to_close is not None and scanner_module._candidate_loop is not None:
            asyncio.run_coroutine_threadsafe(pool_to_close.close(), scanner_module._candidate_loop).result(timeout=10)


def _drive_send_to_scanner_with_real_wake_ack(
    monkeypatch, *, uid, segments, decision_response, guardian_mode="active_support"
):
    """Same as `_drive_send_to_scanner`, except `_enqueue_wake_ack` is left
    unmocked: this runs the actual, direct-DB wake-ack insert path
    (`_insert_wake_ack_direct`) against the disposable test Postgres
    instance, so a regression that enqueues (or fails to enqueue) a
    `wake_word_ack` row shows up as a real `guardian_queue` row count."""
    posts = []

    def fake_webhook_post(_url, json, headers, timeout):
        posts.append(json)
        return _FakeScannerWebhookResponse(200)

    _FakeAsyncClient.queued_responses = [decision_response] if decision_response is not None else []
    _FakeAsyncClient.raise_on_post = None

    async def _test_dedicated_pool(**_kwargs):
        return await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=5)

    pg_kwargs = _postgres_connection_kwargs_from_dsn(TEST_DSN)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(classifier, "_JEV_API_KEY", "test-jev-key")
    monkeypatch.setattr(classifier, "_FALLBACK_API_KEY", "")
    monkeypatch.setattr(scanner_module, "_log_trace_event", lambda *a, **k: None)
    monkeypatch.setattr(scanner_module, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")
    monkeypatch.setattr(scanner_module, "GUARDIAN_WEBHOOK_KEY", "configured-guardian-webhook-key")
    monkeypatch.setattr(scanner_module, "GUARDIAN_WAKE_ACK_DIRECT_DB", True)
    monkeypatch.setattr(scanner_module, "ELLA_POSTGRES_HOST", pg_kwargs["host"])
    monkeypatch.setattr(scanner_module, "ELLA_POSTGRES_PORT", pg_kwargs["port"])
    monkeypatch.setattr(scanner_module, "ELLA_POSTGRES_USER", pg_kwargs["user"])
    monkeypatch.setattr(scanner_module, "ELLA_POSTGRES_PASSWORD", pg_kwargs["password"])
    monkeypatch.setattr(scanner_module, "ELLA_POSTGRES_DATABASE", pg_kwargs["database"])
    monkeypatch.setattr(scanner_module.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner_module, "_post_scanner_webhook", fake_webhook_post)
    monkeypatch.setattr(scanner_module, "create_dedicated_ella_postgres_pool", _test_dedicated_pool)
    scanner_module._candidate_pool = None

    try:
        status = scanner_module.send_to_scanner(uid, "conversation-e2e", segments, guardian_mode=guardian_mode)
        return status, posts
    finally:
        pool_to_close = scanner_module._candidate_pool
        scanner_module._candidate_pool = None
        if pool_to_close is not None and scanner_module._candidate_loop is not None:
            asyncio.run_coroutine_threadsafe(pool_to_close.close(), scanner_module._candidate_loop).result(timeout=10)


def test_confirmed_echo_dispatches_nothing_and_produces_zero_new_guardian_queue_rows(monkeypatch):
    """The literal contract-C requirement: a confirmed echo must be tagged
    as Ella output and never dispatched to the scanner webhook — dispatch is
    the only path in this pipeline that could ever create a Guardian queue
    row from a scanner window, so a webhook call count of zero guarantees a
    `guardian_queue` row count of zero too.

    GUARDIAN-ECHO-006: this drives the *actual* wake-ack path
    (`_enqueue_wake_ack` is not stubbed) with a "Hey Ella..." echo input,
    so a regression that enqueues the ack before the echo judgment is
    caught as a real `wake_word_ack` row, not masked by a mock."""

    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-echo-e2e", playback_id="pb-echo", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-echo-e2e",
            playback_id="pb-echo",
            event_type="started",
            route="Speaker",
            device_class="high",
        )
        await _insert_active_guardian_user(pool, "uid-echo-e2e")

        queue_count_before = await pool.fetchval("SELECT COUNT(*) FROM guardian_queue")
        assert queue_count_before == 0

        status, posts = _drive_send_to_scanner_with_real_wake_ack(
            monkeypatch,
            uid="uid-echo-e2e",
            segments=[{"text": "Hey Ella, I heard my name.", "speaker": "SPEAKER_1"}],
            decision_response=_decision_response(),
        )

        assert status is None
        assert posts == []

        # Give the fire-and-forget wake-ack thread a chance to land a row
        # if the suppression regressed — waiting out the full timeout here
        # (nothing should ever land) is what lets this catch a regression
        # instead of racing it.
        await _wait_until(lambda: _has_any_queue_row(pool), timeout=0.5)
        queue_count_after = await pool.fetchval("SELECT COUNT(*) FROM guardian_queue")
        assert queue_count_after == 0
        wake_ack_count_after = await pool.fetchval(
            "SELECT COUNT(*) FROM guardian_queue WHERE trigger_type = 'wake_word_ack'"
        )
        assert wake_ack_count_after == 0

    asyncio.run(_run_with_database(scenario))


def test_mixed_echo_dispatches_only_the_live_span(monkeypatch):
    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-mixed-e2e", playback_id="pb-echo", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-mixed-e2e",
            playback_id="pb-echo",
            event_type="started",
            route="Speaker",
            device_class="high",
        )

        status, posts = _drive_send_to_scanner(
            monkeypatch,
            uid="uid-mixed-e2e",
            segments=[
                {"text": "Hey Ella, I heard my name.", "speaker": "SPEAKER_1"},
                {"text": "what did you just say about dinner", "speaker": "SPEAKER_1"},
            ],
            decision_response=_decision_response(
                source="mixed",
                contains_additional_live_speech=True,
                live_speech_spans=["what did you just say about dinner"],
            ),
        )

        assert status == 200
        assert len(posts) == 1
        assert [s["text"] for s in posts[0]["segments"]] == ["what did you just say about dinner"]

    asyncio.run(_run_with_database(scenario))


def test_mixed_echo_wake_ack_fires_exactly_once_when_wake_phrase_is_in_the_live_span(monkeypatch):
    """GUARDIAN-ECHO-006 mixed case, real wake-ack path: the wake phrase
    survives inside the validated live span, so exactly one `wake_word_ack`
    row must be produced — no more (it must not also fire for the dropped
    echo portion) and no fewer (a live "Hey Ella" is a real wake event)."""

    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-mixed-wake-e2e", playback_id="pb-echo", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-mixed-wake-e2e",
            playback_id="pb-echo",
            event_type="started",
            route="Speaker",
            device_class="high",
        )
        await _insert_active_guardian_user(pool, "uid-mixed-wake-e2e")

        status, posts = _drive_send_to_scanner_with_real_wake_ack(
            monkeypatch,
            uid="uid-mixed-wake-e2e",
            segments=[
                {"text": "Hi Greg, I heard my name.", "speaker": "SPEAKER_1"},
                {"text": "Hey Ella what did you just say about dinner", "speaker": "SPEAKER_1"},
            ],
            decision_response=_decision_response(
                source="mixed",
                contains_additional_live_speech=True,
                live_speech_spans=["Hey Ella what did you just say about dinner"],
            ),
        )

        assert status == 200
        assert len(posts) == 1
        assert [s["text"] for s in posts[0]["segments"]] == ["Hey Ella what did you just say about dinner"]

        landed = await _wait_until(lambda: _has_any_queue_row(pool))
        assert landed
        queue_count_after = await pool.fetchval("SELECT COUNT(*) FROM guardian_queue")
        assert queue_count_after == 1
        wake_ack_count_after = await pool.fetchval(
            "SELECT COUNT(*) FROM guardian_queue WHERE trigger_type = 'wake_word_ack' AND uid = $1",
            "uid-mixed-wake-e2e",
        )
        assert wake_ack_count_after == 1

    asyncio.run(_run_with_database(scenario))


def test_mixed_echo_in_the_same_segment_dispatches_only_the_live_text(monkeypatch):
    """GUARDIAN-ECHO-007 end-to-end: a single ASR segment carrying both the
    echoed playback and live speech must forward only the validated live
    substring — never the whole segment (which would still leak the
    playback portion)."""

    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-same-segment-e2e", playback_id="pb-echo", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-same-segment-e2e",
            playback_id="pb-echo",
            event_type="started",
            route="Speaker",
            device_class="high",
        )

        status, posts = _drive_send_to_scanner(
            monkeypatch,
            uid="uid-same-segment-e2e",
            segments=[
                {
                    "text": "Hi Greg, I heard my name. Hey Ella what did you just say about dinner",
                    "speaker": "SPEAKER_1",
                }
            ],
            decision_response=_decision_response(
                source="mixed",
                contains_additional_live_speech=True,
                live_speech_spans=["Hey Ella what did you just say about dinner"],
            ),
        )

        assert status == 200
        assert len(posts) == 1
        assert [s["text"] for s in posts[0]["segments"]] == ["Hey Ella what did you just say about dinner"]

    asyncio.run(_run_with_database(scenario))


def test_classifier_timeout_fails_open_and_dispatches_original_window(monkeypatch):
    """A real provider timeout (through the actual `classify_playback_source`
    fail-open path, not a mocked classifier) must still dispatch the
    original, untouched window."""

    async def scenario(pool: asyncpg.Pool) -> None:
        await ledger.record_generated(pool, uid="uid-timeout-e2e", playback_id="pb-echo", playback_text="Hi Greg.")
        await ledger.record_playback_receipt(
            pool,
            uid="uid-timeout-e2e",
            playback_id="pb-echo",
            event_type="started",
            route="Speaker",
            device_class="high",
        )

        status, posts = _drive_send_to_scanner(
            monkeypatch,
            uid="uid-timeout-e2e",
            segments=[{"text": "Hey Ella, did you catch that?", "speaker": "SPEAKER_1"}],
            decision_response=None,
            raise_on_post=httpx.TimeoutException("provider timed out"),
        )

        assert status == 200
        assert len(posts) == 1
        assert [s["text"] for s in posts[0]["segments"]] == ["Hey Ella, did you catch that?"]

    asyncio.run(_run_with_database(scenario))
