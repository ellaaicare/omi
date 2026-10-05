import ast
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable, Optional
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
from utils.ella import scanner
from utils.conversations import spoken_diagnostic as diagnostic


class Snapshot:
    def __init__(self, data):
        self.data = data
        self.exists = data is not None

    def to_dict(self):
        return self.data


class Ref:
    def __init__(self, store, key):
        self.store = store
        self.key = key

    def get(self, transaction=None):
        return Snapshot(self.store.get(self.key))

    def collection(self, name):
        return Collection(self.store, f"{self.key}/{name}")


class Collection:
    def __init__(self, store, prefix):
        self.store = store
        self.prefix = prefix

    def document(self, name):
        return Ref(self.store, f"{self.prefix}/{name}")


class Transaction:
    def create(self, ref, value):
        assert ref.key not in ref.store
        ref.store[ref.key] = value

    def update(self, ref, value):
        ref.store[ref.key] = {**ref.store[ref.key], **value}


def fixture(monkeypatch):
    now = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)
    uid = "owner-test"
    conversation_id = "conversation-original"
    store = {
        f"{uid}/authority": {
            "protocol_version": 2,
            "state": "active",
            "generation": "generation-1",
            "owner_token": "owner-token",
            "conversation_id": conversation_id,
            "lease_expires_at": now + timedelta(seconds=30),
        },
        f"{uid}/{conversation_id}": {
            "id": conversation_id,
            "capture_protocol_version": 2,
            "capture_state": "active",
            "status": "in_progress",
            "capture_owner_id": "owner-token",
            "capture_generation": "generation-1",
            "capture_owner_token": "owner-token",
            "capture_lease_expires_at": now + timedelta(seconds=30),
        },
    }
    monkeypatch.setattr(diagnostic, "_authority_ref", lambda owner: Ref(store, f"{owner}/authority"))
    monkeypatch.setattr(
        diagnostic, "_conversation_ref", lambda owner, conversation: Ref(store, f"{owner}/{conversation}")
    )
    monkeypatch.setattr(
        diagnostic,
        "_capture_protocol",
        lambda: SimpleNamespace(
            CAPTURE_PROTOCOL_VERSION=2,
            _authority_tuple_matches=lambda a, c, g, o: (
                a["protocol_version"],
                a["conversation_id"],
                a["generation"],
                a["owner_token"],
            )
            == (2, c, g, o),
            _conversation_tuple_matches=lambda a, c, g, o: (
                a["id"],
                a["capture_protocol_version"],
                a["capture_generation"],
                a["capture_owner_token"],
            )
            == (c, 2, g, o),
        ),
    )
    config = diagnostic.DiagnosticConfig("0" * 64, "canary-01", now, now + timedelta(minutes=5))
    monkeypatch.setattr(diagnostic, "_utc_now", lambda: now)
    monkeypatch.setattr(
        diagnostic,
        "_config_for",
        lambda owner, at: config if owner == uid and config.starts_at <= at < config.ends_at else None,
    )
    return now, uid, conversation_id, store, config


def _scanner_fixture(monkeypatch, posts):
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setattr(
        diagnostic, "_reserve", lambda _transaction, *args: diagnostic._reserve_tx(Transaction(), *args)
    )
    monkeypatch.setattr(diagnostic, "_database", lambda: SimpleNamespace(transaction=lambda: Transaction()))
    monkeypatch.setattr(scanner, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCHING_ENABLED", True)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 70)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_SECONDS", 10.0)
    monkeypatch.setattr(scanner, "select_playback_ledger_candidates", lambda *_args: [])
    monkeypatch.setattr(scanner, "_log_trace_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *_args: None)
    monkeypatch.setattr(
        scanner,
        "_post_scanner_webhook",
        lambda _url, json, **_kwargs: (posts.append(json), SimpleNamespace(status_code=200, headers={}))[1],
    )
    scanner.reset_scanner_batch_state()


def _send_diagnostic(
    uid, conversation_id, *, origin_generation="generation-1", origin_owner_token="owner-token", **kwargs
):
    return scanner.send_to_scanner(
        uid,
        conversation_id,
        [{"text": diagnostic.PHRASE, "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
        origin_generation=origin_generation,
        origin_owner_token=origin_owner_token,
        **kwargs,
    )


def test_exact_live_diagnostic_bypasses_default_ambient_batch_once(monkeypatch):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)

    assert _send_diagnostic(uid, conversation_id) == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "diagnostic_candidate"
    assert posts[0]["scanner_batch"]["batch_word_count"] == 4
    assert [segment["text"] for segment in posts[0]["segments"]] == [diagnostic.PHRASE]
    assert posts[0]["spoken_diagnostic"]["response_version"] == diagnostic.RESPONSE_VERSION
    assert _send_diagnostic(uid, conversation_id) is None
    assert len(posts) == 1
    assert len([key for key in store if "spoken_diagnostic_claims" in key]) == 1
    scanner.reset_scanner_batch_state()


def test_diagnostic_does_not_flush_or_join_existing_ambient_buffer(monkeypatch):
    _now, uid, conversation_id, _store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_SECONDS", 999.0)
    assert (
        scanner.send_to_scanner(
            uid, conversation_id, [{"text": "quiet afternoon coffee table"}], guardian_mode="active_support"
        )
        is None
    )
    assert len(scanner._SCANNER_BATCHES) == 1
    key = next(iter(scanner._SCANNER_BATCHES))
    buffered = list(scanner._SCANNER_BATCHES[key]["segments"])

    assert _send_diagnostic(uid, conversation_id) == 200
    assert scanner._SCANNER_BATCHES[key]["segments"] == buffered
    assert [segment["text"] for segment in posts[0]["segments"]] == [diagnostic.PHRASE]

    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 5)
    assert scanner.send_to_scanner(uid, conversation_id, [{"text": "later"}], guardian_mode="active_support") == 200
    assert [segment["text"] for segment in posts[1]["segments"]] == ["quiet afternoon coffee table", "later"]
    assert "spoken_diagnostic" not in posts[1]
    scanner.reset_scanner_batch_state()


def test_ordinary_four_words_still_defer_at_default_threshold(monkeypatch):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)

    assert (
        scanner.send_to_scanner(
            uid, conversation_id, [{"text": "quiet afternoon coffee table"}], guardian_mode="active_support"
        )
        is None
    )
    assert posts == []
    assert len(scanner._SCANNER_BATCHES) == 1
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


@pytest.mark.parametrize(
    "segments",
    [
        [{"text": "silver lantern"}, {"text": "check in"}],
        [{"text": diagnostic.PHRASE}, {"text": "more speech"}],
    ],
)
def test_split_or_mixed_phrase_never_bypasses_ambient_batch(monkeypatch, segments):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)

    assert (
        scanner.send_to_scanner(
            uid,
            conversation_id,
            segments,
            guardian_mode="active_support",
            origin_generation="generation-1",
            origin_owner_token="owner-token",
        )
        is None
    )
    assert posts == []
    assert len(scanner._SCANNER_BATCHES) == 1
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


@pytest.mark.parametrize("condition", ["disabled", "wrong_owner", "expired", "missing_origin", "stale_origin"])
def test_noneligible_diagnostic_never_claims_or_dispatches(monkeypatch, condition):
    now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)
    if condition == "disabled":
        monkeypatch.delenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED")
    elif condition == "wrong_owner":
        uid = "different-owner"
    elif condition == "expired":
        monkeypatch.setattr(diagnostic, "_utc_now", lambda: now + timedelta(minutes=6))
    kwargs = {}
    if condition == "missing_origin":
        kwargs["origin_generation"] = ""
    elif condition == "stale_origin":
        kwargs["origin_generation"] = "stale-generation"

    assert _send_diagnostic(uid, conversation_id, **kwargs) is None
    assert posts == []
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


def test_forced_ordinary_flush_does_not_create_diagnostic_claim(monkeypatch):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 1)

    assert _send_diagnostic("different-owner", conversation_id) == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "word_threshold"
    assert "spoken_diagnostic" not in posts[0]
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


def test_window_expiring_after_batch_bypass_fails_closed(monkeypatch):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)
    checks = iter([True, False])
    monkeypatch.setattr(diagnostic, "is_diagnostic_window", lambda *_args: next(checks))

    assert _send_diagnostic(uid, conversation_id) is None
    assert posts == []
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


def test_confirmed_echo_suppresses_diagnostic_before_claim(monkeypatch):
    _now, uid, conversation_id, store, _config = fixture(monkeypatch)
    posts = []
    _scanner_fixture(monkeypatch, posts)
    monkeypatch.setattr(scanner, "select_playback_ledger_candidates", lambda *_args: [{"playback_id": "played"}])
    monkeypatch.setattr(
        scanner,
        "_classify_playback_source_for_dispatch",
        lambda *_args, **_kwargs: SimpleNamespace(
            fail_open=False,
            is_confirmed_echo=True,
            source="ella_playback",
            matched_playback_ids=["played"],
            reason_code="confirmed_echo",
            confidence=1.0,
        ),
    )

    assert _send_diagnostic(uid, conversation_id) is None
    assert posts == []
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()


def test_default_disabled_and_invalid_window(monkeypatch):
    monkeypatch.delenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", raising=False)
    assert diagnostic.current_config() is None
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_OWNER_SHA256", "0" * 64)
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_RUN_ID", "canary-01")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_START_UTC", "2026-10-04T17:00:00Z")
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_END_UTC", "2026-10-04T17:11:00Z")
    assert diagnostic.current_config() is None
    assert diagnostic._phrase_in_segments([{"text": "silver lantern check in"}])
    assert not diagnostic._phrase_in_segments([{"text": "help me, silver lantern check in"}])
    assert not diagnostic._phrase_in_segments(
        [
            {"text": "silver lantern check in"},
            {"text": "I have chest pain"},
        ]
    )


def test_default_disabled_never_opens_capture_or_claim_store(monkeypatch):
    monkeypatch.delenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", raising=False)
    touched = []
    monkeypatch.setattr(diagnostic, "_authority_ref", lambda *_args: touched.append("authority"))
    monkeypatch.setattr(diagnostic, "_conversation_ref", lambda *_args: touched.append("conversation"))
    monkeypatch.setattr(diagnostic, "_database", lambda: touched.append("transaction"))
    assert diagnostic.reserve_for_segments("uid-test", "conversation-test", [{"text": diagnostic.PHRASE}]) is None
    assert diagnostic.transition("uid-test", "a" * 64, "claimed", "rendering") is None
    assert diagnostic.current_claim("uid-test", "a" * 64, "queued") is None
    assert not diagnostic.is_diagnostic_window("uid-test", [{"text": diagnostic.PHRASE}])
    assert touched == []


def test_claim_once_across_rollover_and_render_once(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    tx = Transaction()
    claim = diagnostic._reserve_tx(tx, uid, conversation_id, config, "generation-1", "owner-token")
    assert claim and claim["response_version"] == diagnostic.RESPONSE_VERSION
    assert diagnostic._reserve_tx(tx, uid, conversation_id, config, "generation-1", "owner-token") is None
    store[f"{uid}/rollover"] = {**store[f"{uid}/{conversation_id}"]}
    assert diagnostic._reserve_tx(tx, uid, "rollover", config, "generation-1", "owner-token") is None
    claim_id = claim["claim_id"]
    rendering = diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", config)
    assert rendering and rendering["state"] == "rendering"
    assert diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", config) is None
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "rendering", config)


def test_revoked_expired_or_replaced_capture_fails_closed(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    tx = Transaction()
    claim = diagnostic._reserve_tx(tx, uid, conversation_id, config, "generation-1", "owner-token")
    assert claim
    claim_id = claim["claim_id"]
    store[f"{uid}/authority"]["state"] = "drained"
    assert diagnostic._transition_tx(tx, uid, claim_id, "claimed", "rendering", config) is None
    store[f"{uid}/authority"]["state"] = "active"
    store[f"{uid}/authority"]["generation"] = "generation-2"
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "claimed", config) is None
    store[f"{uid}/authority"]["generation"] = "generation-1"
    monkeypatch.setattr(diagnostic, "_utc_now", lambda: now + timedelta(seconds=61))
    assert diagnostic._current_claim_tx(tx, uid, claim_id, "claimed", config) is None


def test_old_generation_cannot_claim_under_replacement_same_conversation(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    store[f"{uid}/authority"]["generation"] = "generation-b"
    store[f"{uid}/authority"]["owner_token"] = "owner-b"
    store[f"{uid}/{conversation_id}"]["capture_generation"] = "generation-b"
    store[f"{uid}/{conversation_id}"]["capture_owner_token"] = "owner-b"
    store[f"{uid}/{conversation_id}"]["capture_owner_id"] = "owner-b"
    assert diagnostic._reserve_tx(Transaction(), uid, conversation_id, config, "generation-1", "owner-token") is None
    assert not any("spoken_diagnostic_claims" in key for key in store)


def test_transaction_retry_time_expiry_fails_closed(monkeypatch):
    now, uid, conversation_id, store, config = fixture(monkeypatch)
    clock = [now]
    monkeypatch.setattr(diagnostic, "_utc_now", lambda: clock[0])

    class ExpiringRef(Ref):
        def get(self, transaction=None):
            clock[0] = now + timedelta(minutes=5)
            return super().get(transaction=transaction)

    monkeypatch.setattr(
        diagnostic, "_conversation_ref", lambda owner, conversation: ExpiringRef(store, f"{owner}/{conversation}")
    )
    assert diagnostic._reserve_tx(Transaction(), uid, conversation_id, config, "generation-1", "owner-token") is None
    assert not any("spoken_diagnostic_claims" in key for key in store)


def test_persisted_stt_queue_item_cannot_claim_replaced_same_conversation(monkeypatch):
    now, uid, conversation_id, store, _config = fixture(monkeypatch)
    source_path = Path(__file__).resolve().parents[2] / "routers" / "transcribe.py"
    source = ast.parse(source_path.read_text())
    builder = next(
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_scanner_dispatch_item"
    )
    queue_class = next(
        node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "ScannerDispatchQueue"
    )
    namespace = {
        "asyncio": asyncio,
        "Awaitable": Awaitable,
        "Callable": Callable,
        "Optional": Optional,
        "SCANNER_DISPATCH_QUEUE_MAXSIZE": 32,
        "SCANNER_DISPATCH_DRAIN_TIMEOUT_SECONDS": 1.0,
        "SCANNER_EMERGENCY_CONTEXT_MAX_AGE_SECONDS": 10.0,
        "_SCANNER_DISPATCH_STOP": object(),
        "time": __import__("time"),
    }
    exec(compile(ast.Module(body=[builder, queue_class], type_ignores=[]), str(source_path), "exec"), namespace)
    queued_item = namespace["_scanner_dispatch_item"](
        uid,
        conversation_id,
        [SimpleNamespace(dict=lambda: {"text": diagnostic.PHRASE, "speaker": "SPEAKER_1"})],
        "generation-1",
        "owner-token",
        {},
    )
    persistence_path = Path(__file__).resolve().parents[2] / "database" / "conversations.py"
    persistence_source = ast.parse(persistence_path.read_text())
    coordinator = next(
        node
        for node in persistence_source.body
        if isinstance(node, ast.FunctionDef) and node.name == "persist_and_commit_capture_persistence_batch"
    )
    persistence_namespace = {
        "List": list,
        "Optional": Optional,
        "ConversationPhoto": object,
        "datetime": datetime,
        "redis_db": SimpleNamespace(
            acquire_capture_commit_lease=lambda *_args: True,
            release_capture_commit_lease=lambda *_args: True,
        ),
        "persist_capture_persistence_batch": lambda _uid, _conversation, segments, *_args, **_kwargs: store.update(
            {f"{uid}/{conversation_id}/persisted_segments": segments}
        )
        or "batch-a",
        "_commit_capture_persistence_batch": lambda *_args: {
            "status": "committed",
            "updated_segments": queued_item["segments"],
            "removed_ids": [],
        },
        "db": SimpleNamespace(collection=lambda _name: Collection(store, "users"), transaction=lambda: Transaction()),
        "conversations_collection": "conversations",
        "capture_persistence_batches_collection": "capture_persistence_batches",
    }
    exec(compile(ast.Module(body=[coordinator], type_ignores=[]), str(persistence_path), "exec"), persistence_namespace)
    assert (
        persistence_namespace["persist_and_commit_capture_persistence_batch"](
            uid,
            conversation_id,
            queued_item["segments"],
            now,
            "owner-token",
            capture_generation="generation-1",
        )["status"]
        == "committed"
    )
    assert store[f"{uid}/{conversation_id}/persisted_segments"] == queued_item["segments"]

    posts = []
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setattr(diagnostic, "_reserve", lambda _db_tx, *args: diagnostic._reserve_tx(Transaction(), *args))
    monkeypatch.setattr(diagnostic, "_database", lambda: SimpleNamespace(transaction=lambda: Transaction()))
    monkeypatch.setattr(scanner, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 1)
    monkeypatch.setattr(scanner, "select_playback_ledger_candidates", lambda *_args: [])
    monkeypatch.setattr(scanner, "_log_trace_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *_args, **_kwargs: posts.append(1))
    scanner.reset_scanner_batch_state()

    async def dispatch(item):
        scanner.send_to_scanner(guardian_mode="active_support", **item)

    async def scenario():
        queue = namespace["ScannerDispatchQueue"](dispatch)
        assert queue.enqueue(queued_item)
        store[f"{uid}/authority"].update(generation="generation-2", owner_token="owner-2")
        store[f"{uid}/{conversation_id}"].update(
            capture_generation="generation-2", capture_owner_token="owner-2", capture_owner_id="owner-2"
        )
        queue.start()
        await queue.close()

    asyncio.run(scenario())
    assert posts == []
    assert not any("spoken_diagnostic_claims" in key for key in store)
    scanner.reset_scanner_batch_state()
