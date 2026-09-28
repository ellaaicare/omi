"""Contract tests for the `/v2/sync-local-files` and `/v2/sync-capture-manifest` routes.

These cover what BasedHardware/omi's real backend (`backend/routers/sync.py` at commit
f16699aea7fe9ba089baceb628922f2882c51153) and the vendored client
(`app/lib/upstream_capture/backend/http/api/conversations.dart::uploadLocalFilesV2` /
`_createSyncCaptureManifest` on `origin/release/testflight-850-necklace-recovery`) actually
require of the wire contract: response shape, auth/consent fail-closed behavior, idempotent
segment replay, that a conversation_id can never resolve another account's conversation, that a
cache outage or a lost claim race fails a segment closed rather than reporting false success, and
that parallel segments targeting one explicit conversation never drop each other's writes.

The STT/LLM layer below `process_segment` (Deepgram, `process_conversation`) is stubbed so these
tests exercise only this fork's `/v2` route logic — the same boundary `/v1/sync-local-files`
already crosses untested at this layer.
"""

import copy
import hashlib
import io
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# routers/sync.py pulls in the real Firestore/GCS clients (via database.conversations ->
# utils.other.storage), a torch-backed VAD module, and the opus/pydub audio-decoding libraries,
# none of which this file needs — every function that would touch them is monkeypatched per-test
# below. Stub them the same way tests/unit/test_ella_incident_1182_route_isolation.py does for
# other routers, so importing routers.sync here doesn't require real GCP credentials, a working
# torch install, or the libopus system library the CI runner doesn't provide.
for _module_name in (
    "database._client",
    "database.conversations",
    "database.memories",
    "database.users",
    "database.ella_contacts",
    "utils.notifications",
    "utils.other.storage",
    "utils.conversations.process_conversation",
    "utils.stt.vad",
    "opuslib",
    "pydub",
):
    sys.modules.setdefault(_module_name, MagicMock(db=MagicMock()))

import ella.services.ai_consent as ai_consent
import routers.sync as sync
import utils.sync_capture_manifest as capture_manifest
from ella.services.ai_consent import require_current_ai_consent
from models.transcript_segment import TranscriptSegment
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

BIN_TIMESTAMP = 1735689600  # 2025-01-01T00:00:00Z — well inside retrieve_file_paths' valid window


class FakeRedis:
    """Minimal stand-in for `database.redis_db.r`'s subset used by utils/sync_capture_manifest.py.
    Real redis-server semantics for SET ... NX: returns falsy when the key already exists."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)

    def get(self, key):
        return self._store.get(key)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(capture_manifest, "redis_client", fake)
    return fake


@pytest.fixture(autouse=True)
def _bypass_process_segment_internals(monkeypatch):
    # process_segment() independently calls assert_current_ai_consent(uid) and spawns a
    # background thread that sleeps 480s before touching temporal storage. Neutralize both so
    # tests don't hit real Firestore or leave a long-lived thread behind.
    monkeypatch.setattr(sync, "assert_current_ai_consent", lambda uid: uid)
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sync, "get_syncing_file_temporal_signed_url", lambda path: f"file://{path}")
    monkeypatch.setattr(sync, "delete_syncing_temporal_file", lambda path: None)


def _app_with_uid_override(uid: str) -> FastAPI:
    """Bypasses both auth and consent in one step, for tests not focused on that boundary."""
    app = FastAPI()
    app.include_router(sync.router)
    app.dependency_overrides[require_current_ai_consent] = lambda: uid
    return app


def _stub_stt_and_llm(monkeypatch, *, closest_conversation=None, transcript_text="hello there"):
    def fake_deepgram_prerecorded(url, speakers_count=3, attempts=0, return_language=True):
        return (["stub-words"], "en")

    def fake_postprocess_words(words, offset):
        return [TranscriptSegment(text=transcript_text, is_user=False, start=0.0, end=3.0)]

    created = {"calls": 0, "conversations": []}

    def fake_process_conversation(uid, language, create_memory):
        created["calls"] += 1
        conversation_id = f"conv-{created['calls']}"
        created["conversations"].append((uid, create_memory))
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(sync, "deepgram_prerecorded", fake_deepgram_prerecorded)
    monkeypatch.setattr(sync, "postprocess_words", fake_postprocess_words)
    monkeypatch.setattr(sync, "get_closest_conversation_to_timestamps", lambda uid, s, e: closest_conversation)
    monkeypatch.setattr(sync, "process_conversation", fake_process_conversation)
    return created


def _stub_vad_with_fixed_segment(monkeypatch, segment_path: str):
    """Skip real opus decode + VAD: pretend the single uploaded file produced exactly one VAD
    segment at [segment_path] (which the test writes to disk itself, with known bytes, so
    idempotency tests can replay the *same* segment content across two requests)."""
    monkeypatch.setattr(sync, "decode_files_to_wav", lambda paths: ["/tmp/stub-not-a-real-wav.wav"])

    def fake_retrieve_vad_segments(path, segmented_paths, errors=None):
        segmented_paths.add(segment_path)

    monkeypatch.setattr(sync, "retrieve_vad_segments", fake_retrieve_vad_segments)


def _bin_upload(name: str, content: bytes):
    return {"files": (name, io.BytesIO(content), "application/octet-stream")}


def test_v2_sync_local_files_response_matches_vendored_client_shape(monkeypatch, tmp_path):
    """The vendored client's `GeneratedSyncLocalFilesResultResponse.fromJson` (conversation_wire.g.dart)
    requires exactly these keys on 200: new_memories, updated_memories, failed_segments,
    total_segments, errors."""
    # Segment filenames must be the epoch-seconds basename real `retrieve_vad_segments` writes
    # (`get_timestamp_from_path` parses the trailing int); the content stubs never see the real
    # `.bin`/`.wav`, only this fixed segment path.
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 10}.wav")
    Path(segment_path).write_bytes(b"segment-one-audio-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    app = _app_with_uid_override("uid-shape")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"new_memories", "updated_memories", "failed_segments", "total_segments", "errors"}
    assert body["new_memories"] == ["conv-1"]
    assert body["updated_memories"] == []
    assert body["failed_segments"] == 0
    assert body["total_segments"] == 1
    assert body["errors"] == []
    assert created["calls"] == 1


def test_v2_sync_local_files_replay_of_same_segment_is_idempotent(monkeypatch, tmp_path):
    """Requirement: replaying the same manifest/audio content must not double-process or
    double-create a conversation — it returns the same outcome as the first successful call."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 20}.wav")
    segment_bytes = b"identical-segment-bytes-across-both-uploads"
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    app = _app_with_uid_override("uid-replay")
    client = TestClient(app)

    Path(segment_path).write_bytes(segment_bytes)
    first = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes-1"),
    )
    assert first.status_code == 200
    assert first.json()["new_memories"] == ["conv-1"]
    assert created["calls"] == 1

    # The route's `finally` cleans up the segment file after the first call (as it would after a
    # real VAD run); recreate the *same* bytes at the *same* path to simulate the client replaying
    # the same WAL content after a dropped response / app relaunch.
    Path(segment_path).write_bytes(segment_bytes)
    second = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP + 1}.bin", b"raw-bin-bytes-2"),
    )
    assert second.status_code == 200
    # Same segment content -> same segment id -> cached outcome replayed, no second STT/LLM call.
    assert second.json()["new_memories"] == ["conv-1"]
    assert second.json()["updated_memories"] == []
    assert created["calls"] == 1


def test_v2_sync_local_files_conversation_id_is_uid_scoped_and_never_cross_account(monkeypatch, tmp_path):
    """A conversation_id that resolves for a *different* uid must never be attached to; the route
    falls back to creating a fresh conversation for the authenticated uid instead."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 30}.wav")
    Path(segment_path).write_bytes(b"cross-account-segment-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    lookups = []

    def fake_get_conversation(uid, conversation_id):
        lookups.append((uid, conversation_id))
        if uid == "uid-b":
            return {"id": conversation_id, "owner": "uid-b"}
        return None  # Firestore-scoped read: a foreign conversation_id never resolves for uid-a.

    monkeypatch.setattr(sync.conversations_db, "get_conversation", fake_get_conversation)

    app = _app_with_uid_override("uid-a")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        params={"conversation_id": "conv-owned-by-uid-b"},
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    assert resp.status_code == 200
    body = resp.json()
    # Never merged into uid-b's conversation: a brand new conversation was created for uid-a instead.
    assert body["new_memories"] == ["conv-1"]
    assert body["updated_memories"] == []
    assert created["calls"] == 1
    assert created["conversations"][0][0] == "uid-a"
    # The lookup was always uid-scoped to the authenticated caller, never to uid-b.
    assert all(uid == "uid-a" for uid, _ in lookups)


def test_v2_sync_capture_manifest_issue_verify_and_replay(monkeypatch):
    app = _app_with_uid_override("uid-manifest")
    client = TestClient(app)
    file_claim = {"name": "a.bin", "sha256": hashlib.sha256(b"file-a-bytes").hexdigest()}

    resp1 = client.post(
        "/v2/sync-capture-manifest",
        json={"conversation_id": "conv-live-1", "files": [file_claim]},
    )
    assert resp1.status_code == 200
    manifest_1 = resp1.json()["manifest"]

    # Replaying the exact same claim for the same conversation is idempotent, not a conflict.
    resp2 = client.post(
        "/v2/sync-capture-manifest",
        json={"conversation_id": "conv-live-1", "files": [file_claim]},
    )
    assert resp2.status_code == 200

    # A *different* file set for the same conversation is a claim conflict.
    other_claim = {"name": "b.bin", "sha256": hashlib.sha256(b"file-b-bytes").hexdigest()}
    resp3 = client.post(
        "/v2/sync-capture-manifest",
        json={"conversation_id": "conv-live-1", "files": [other_claim]},
    )
    assert resp3.status_code == 409

    # The issued token verifies for the uid/conversation/filenames it was minted for...
    claims = capture_manifest.verify_capture_manifest(manifest_1, "uid-manifest", "conv-live-1", ["a.bin"])
    assert claims == [{"name": "a.bin", "sha256": file_claim["sha256"]}]
    # ...and fails closed for a different uid, a different conversation, or a mismatched filename.
    assert capture_manifest.verify_capture_manifest(manifest_1, "uid-other", "conv-live-1", ["a.bin"]) is None
    assert capture_manifest.verify_capture_manifest(manifest_1, "uid-manifest", "conv-live-2", ["a.bin"]) is None
    assert capture_manifest.verify_capture_manifest(manifest_1, "uid-manifest", "conv-live-1", ["other.bin"]) is None


def test_v2_sync_local_files_rejects_upload_that_does_not_match_its_manifest(monkeypatch, tmp_path):
    segment_path = str(tmp_path / "seg-mismatch.wav")
    Path(segment_path).write_bytes(b"unused-because-request-is-rejected")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    _stub_stt_and_llm(monkeypatch)

    app = _app_with_uid_override("uid-mismatch")
    client = TestClient(app)
    filename = f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin"

    manifest = capture_manifest.issue_capture_manifest(
        "uid-mismatch",
        "conv-live-1",
        [{"name": filename, "sha256": hashlib.sha256(b"the-bytes-the-manifest-promised").hexdigest()}],
    )

    resp = client.post(
        "/v2/sync-local-files",
        params={"conversation_id": "conv-live-1"},
        files=_bin_upload(filename, b"different-bytes-than-the-manifest-claimed"),
        headers={"X-Omi-Sync-Capture-Manifest": manifest},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "capture_manifest_mismatch"


class _FakeConsentService:
    def __init__(self, status):
        self._status = status

    def status(self, _uid):
        return self._status


@pytest.mark.parametrize("path", ["/v2/sync-local-files", "/v2/sync-capture-manifest"])
def test_v2_routes_fail_closed_when_consent_is_missing(monkeypatch, path):
    app = FastAPI()
    app.include_router(sync.router)
    app.add_exception_handler(ai_consent.AiConsentHTTPException, ai_consent.ai_consent_http_exception_handler)
    app.dependency_overrides[get_exact_firebase_uid] = lambda: "uid-no-consent"
    monkeypatch.setattr(
        ai_consent,
        "get_ai_consent_service",
        lambda: _FakeConsentService({"authorized": False, "authority_state": "not_accepted"}),
    )
    client = TestClient(app)

    kwargs = {"json": {"conversation_id": "c", "files": [{"name": "a.bin", "sha256": "0" * 64}]}}
    if path == "/v2/sync-local-files":
        kwargs = {"files": _bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"x")}

    resp = client.post(path, **kwargs)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ai_consent_required"


@pytest.mark.parametrize("path", ["/v2/sync-local-files", "/v2/sync-capture-manifest"])
def test_v2_routes_fail_closed_retryable_when_consent_authority_is_unavailable(monkeypatch, path):
    app = FastAPI()
    app.include_router(sync.router)
    app.add_exception_handler(ai_consent.AiConsentHTTPException, ai_consent.ai_consent_http_exception_handler)
    app.dependency_overrides[get_exact_firebase_uid] = lambda: "uid-unavailable"
    monkeypatch.setattr(
        ai_consent,
        "get_ai_consent_service",
        lambda: _FakeConsentService({"authorized": False, "authority_state": "unavailable"}),
    )
    client = TestClient(app)

    kwargs = {"json": {"conversation_id": "c", "files": [{"name": "a.bin", "sha256": "0" * 64}]}}
    if path == "/v2/sync-local-files":
        kwargs = {"files": _bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"x")}

    resp = client.post(path, **kwargs)
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "ai_consent_authority_unavailable"
    assert body["retryable"] is True


@pytest.mark.parametrize("path", ["/v2/sync-local-files", "/v2/sync-capture-manifest"])
def test_v2_routes_reject_unauthenticated_requests(path):
    app = FastAPI()
    app.include_router(sync.router)

    def _raise_unauthenticated():
        raise HTTPException(status_code=401, detail="missing or invalid token")

    app.dependency_overrides[get_exact_firebase_uid] = _raise_unauthenticated
    client = TestClient(app)

    kwargs = {"json": {"conversation_id": "c", "files": [{"name": "a.bin", "sha256": "0" * 64}]}}
    if path == "/v2/sync-local-files":
        kwargs = {"files": _bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"x")}

    resp = client.post(path, **kwargs)
    assert resp.status_code == 401


# **********************************************
# SYNC-V2-001/002/003 regressions (review on PR #594)
# **********************************************


def test_v2_sync_local_files_cache_read_outage_fails_closed_not_false_success(monkeypatch, tmp_path):
    """SYNC-V2-001: a Redis outage on the idempotency-cache *read* must not be reported back as a
    false-success 200 with failed_segments 0 — the segment was never actually processed."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 60}.wav")
    Path(segment_path).write_bytes(b"cache-read-outage-segment-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    def broken_get_cached(segment_id):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(sync, "get_cached_sync_segment_result", broken_get_cached)

    app = _app_with_uid_override("uid-cache-read-outage")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    # Fail-closed (retryable, per this route's existing "all segments failed" contract) — never a
    # 200 claiming success.
    assert resp.status_code == 500
    assert created["calls"] == 0  # the outage was caught before STT ever ran


def test_v2_sync_local_files_cache_write_outage_fails_closed_not_false_success(monkeypatch, tmp_path):
    """SYNC-V2-001: a Redis outage writing the idempotency result — even though processing itself
    already succeeded — must still be reported as a failed segment, never a false-success 200.
    Otherwise a later replay of this exact audio has no cached result to dedupe against."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 61}.wav")
    Path(segment_path).write_bytes(b"cache-write-outage-segment-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    def broken_cache_write(segment_id, kind, conversation_id):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(sync, "cache_sync_segment_result", broken_cache_write)

    app = _app_with_uid_override("uid-cache-write-outage")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    assert resp.status_code == 500
    # Processing itself succeeded (STT ran) — it's specifically the idempotency-cache write that
    # failed, and that alone must still fail the segment closed.
    assert created["calls"] == 1


def test_v2_sync_local_files_concurrent_retries_of_same_segment_process_once(monkeypatch, tmp_path):
    """SYNC-V2-002: two concurrent requests replaying the exact same segment content must result
    in exactly one STT/LLM/persistence execution, never two racing on the old non-atomic
    get/process/set idempotency check."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 70}.wav")
    Path(segment_path).write_bytes(b"concurrent-retry-segment-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    entered_processing = threading.Event()
    release_processing = threading.Event()
    original_deepgram = sync.deepgram_prerecorded

    def blocking_deepgram(*args, **kwargs):
        entered_processing.set()
        assert release_processing.wait(timeout=5), "never released — test deadlocked"
        return original_deepgram(*args, **kwargs)

    monkeypatch.setattr(sync, "deepgram_prerecorded", blocking_deepgram)

    app = _app_with_uid_override("uid-concurrent-retry")
    client = TestClient(app)
    results = {}

    def do_request(key):
        results[key] = client.post(
            "/v2/sync-local-files",
            files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
        )

    first = threading.Thread(target=do_request, args=("first",))
    first.start()
    assert entered_processing.wait(timeout=5), "first request never reached STT"

    second = threading.Thread(target=do_request, args=("second",))
    second.start()
    second.join(timeout=5)
    release_processing.set()
    first.join(timeout=5)

    # Exactly one execution ran STT/LLM/persistence for this segment id.
    assert created["calls"] == 1
    assert results["first"].status_code == 200
    assert results["first"].json()["new_memories"] == ["conv-1"]
    # The second request lost the atomic claim race; it reports retryable failure instead of
    # racing the first execution.
    assert results["second"].status_code == 500


def test_v2_sync_local_files_parallel_segments_for_one_explicit_conversation_do_not_lose_writes(monkeypatch, tmp_path):
    """SYNC-V2-003: two VAD segments explicitly targeting the same conversation_id, processed
    concurrently, must both survive in the final segment list — not last-writer-wins."""
    seg_a = str(tmp_path / f"{BIN_TIMESTAMP + 80}.wav")
    seg_b = str(tmp_path / f"{BIN_TIMESTAMP + 81}.wav")
    Path(seg_a).write_bytes(b"segment-a-bytes")
    Path(seg_b).write_bytes(b"segment-b-bytes")

    monkeypatch.setattr(sync, "decode_files_to_wav", lambda paths: ["/tmp/stub-not-a-real-wav.wav"])

    def fake_retrieve_vad_segments(path, segmented_paths, errors=None):
        segmented_paths.add(seg_a)
        segmented_paths.add(seg_b)

    monkeypatch.setattr(sync, "retrieve_vad_segments", fake_retrieve_vad_segments)
    monkeypatch.setattr(sync, "get_closest_conversation_to_timestamps", lambda uid, s, e: None)

    barrier = threading.Barrier(2, timeout=5)

    def fake_deepgram_prerecorded(url, speakers_count=3, attempts=0, return_language=True):
        barrier.wait()
        return ([url], "en")  # smuggle the path through so postprocess_words can tell segments apart

    def fake_postprocess_words(words, offset):
        url = words[0]
        text = "segment a text" if seg_a in url else "segment b text"
        return [TranscriptSegment(text=text, is_user=False, start=0.0, end=3.0)]

    monkeypatch.setattr(sync, "deepgram_prerecorded", fake_deepgram_prerecorded)
    monkeypatch.setattr(sync, "postprocess_words", fake_postprocess_words)

    conversation_store = {
        "id": "conv-target",
        "started_at": datetime.fromtimestamp(BIN_TIMESTAMP, tz=timezone.utc),
        "finished_at": datetime.fromtimestamp(BIN_TIMESTAMP + 5, tz=timezone.utc),
        "transcript_segments": [],
        "discarded": False,
    }
    update_calls = []
    store_lock = threading.Lock()

    def fake_get_conversation(uid, conversation_id):
        if conversation_id != "conv-target":
            return None
        with store_lock:
            return copy.deepcopy(conversation_store)

    def fake_update_conversation_segments(uid, conversation_id, segments, finished_at=None):
        with store_lock:
            conversation_store["transcript_segments"] = copy.deepcopy(segments)
            conversation_store["finished_at"] = finished_at
            update_calls.append(copy.deepcopy(segments))

    monkeypatch.setattr(sync.conversations_db, "get_conversation", fake_get_conversation)
    monkeypatch.setattr(sync, "update_conversation_segments", fake_update_conversation_segments)

    app = _app_with_uid_override("uid-parallel-conversation")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        params={"conversation_id": "conv-target"},
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["updated_memories"] == ["conv-target"]
    assert body["failed_segments"] == 0

    final_texts = {segment["text"] for segment in conversation_store["transcript_segments"]}
    assert final_texts == {"segment a text", "segment b text"}
    # Both segments were merged one at a time (serialized), never overwriting each other.
    assert len(update_calls) == 2
    assert len(update_calls[-1]) == 2
