"""Contract tests for the `/v2/sync-local-files` and `/v2/sync-capture-manifest` routes.

These cover what BasedHardware/omi's real backend (`backend/routers/sync.py` at commit
f16699aea7fe9ba089baceb628922f2882c51153) and the vendored client
(`app/lib/upstream_capture/backend/http/api/conversations.dart::uploadLocalFilesV2` /
`_createSyncCaptureManifest` on `origin/release/testflight-850-necklace-recovery`) actually
require of the wire contract: response shape, auth/consent fail-closed behavior, idempotent
segment replay backed by a durable record (not a cache that can silently lose it), that a
conversation_id can never resolve another account's conversation, that a claim-store outage or a
lost claim race fails a segment closed rather than reporting false success, that a segment whose
persisting transaction fails outright and is retried is written exactly once (never duplicated),
and that parallel segments targeting one explicit conversation never drop each other's writes.

The STT/LLM layer below `process_segment` (Deepgram, `process_conversation`) is stubbed, and the
durable idempotency/merge layer (`database.sync_segments`, `database.conversations`) is replaced
by an in-memory fake faithful to its atomicity contract (see `FakeConversationStore` /
`FakeSyncSegmentStore` below), so these tests exercise only this fork's `/v2` route logic — the
same boundary `/v1/sync-local-files` already crosses untested at this layer. The real Firestore
transaction machinery itself is out of scope here (it needs `FIRESTORE_EMULATOR_HOST`, which this
focused contract suite deliberately avoids for speed), the same boundary this file already drew
for Redis before this revision and continues to draw for `database.conversations`.
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
# utils.other.storage), the real `redis` client (via database.redis_db, imported at module level
# by utils/sync_capture_manifest.py), a torch-backed VAD module, and the opus/pydub
# audio-decoding libraries, none of which this file needs or which this focused CI job installs
# -- every function that would touch them is monkeypatched per-test below. Stub them the same way
# tests/unit/test_ella_incident_1182_route_isolation.py does for other routers, so importing
# routers.sync here doesn't require real GCP credentials, a working torch install, the libopus
# system library, or the `redis` package, none of which this CI job provides.
for _module_name in (
    "database._client",
    "database.conversations",
    "database.sync_segments",
    "database.redis_db",
    "database.memories",
    "database.users",
    "database.ella_contacts",
    "utils.notifications",
    "utils.other.storage",
    "utils.conversations.process_conversation",
    "utils.stt.vad",
    "utils.stt.pre_recorded",
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
    """Minimal stand-in for `database.redis_db.r`'s subset used by utils/sync_capture_manifest.py
    for capture-manifest claims (unrelated to segment idempotency, which no longer uses Redis at
    all -- see `database/sync_segments.py`). Real redis-server semantics for SET ... NX: returns
    falsy when the key already exists."""

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


class FakeConversationStore:
    """In-memory stand-in for the conversation documents `database.sync_segments`'s real
    Firestore transaction merges into. Reproduces the merge algorithm in
    `database.conversations.merge_transcript_segments_into_conversation_transaction` closely
    enough to prove the *calling* code (the route + `process_segment`) is correct: existing
    segments are placed on an absolute timeline via `started_at`, the incoming segment via
    [segment_timestamp], the combined list is sorted and re-relativized.

    [atomic=False] reproduces the pre-fix race (the read-merge-write is no longer serialized per
    conversation) purely for a manual mutation check proving the regression test below actually
    depends on the fix -- it is never used by a real test in this suite.
    """

    def __init__(self, *, atomic: bool = True):
        self._lock = threading.Lock()
        self._atomic = atomic
        self.conversations: dict[str, dict] = {}
        self.update_calls: list[list[dict]] = []

    def seed(self, conversation_id: str, *, started_at, finished_at, segments=None, discarded=False):
        self.conversations[conversation_id] = {
            "id": conversation_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "transcript_segments": list(segments or []),
            "discarded": discarded,
            "_version": 0,
        }

    def merge(self, conversation_id: str, new_segments: list, segment_timestamp: float, *, barrier=None):
        """Models a real Firestore transaction: reads are unlocked (so two concurrent merges can
        genuinely both be mid-read at once -- [barrier], if given, forces that overlap on each
        caller's *first* attempt), and the write only commits if the document hasn't changed since
        the read (a version counter standing in for Firestore's real conflict detection). A
        writer that loses the race doesn't corrupt anything -- with [atomic=True] (the default,
        matching the real transactional merge and its `@transactional` auto-retry) it re-reads the
        now-current state and recomputes the merge, exactly like Firestore retrying the whole
        transaction function under contention. [atomic=False] reproduces the pre-fix plain
        get+merge+set instead: no version check, so a losing writer just blindly overwrites."""
        attempt = 0
        while True:
            attempt += 1
            with self._lock:
                conversation = self.conversations.get(conversation_id)
                if conversation is None:
                    return None
                read_version = conversation.get("_version", 0)
                existing = copy.deepcopy(conversation["transcript_segments"])
                started_at_ts = conversation["started_at"].timestamp()
                finished_at = conversation["finished_at"]
                discarded = conversation["discarded"]

            for segment in existing:
                segment["timestamp"] = started_at_ts + segment["start"]
            incoming = copy.deepcopy(new_segments)
            for segment in incoming:
                segment["timestamp"] = segment_timestamp + segment["start"]

            if barrier and attempt == 1:
                # Only the first attempt waits -- a retry after losing the race shouldn't make its
                # rival wait for it a second time.
                barrier()

            segments = existing + incoming
            segments.sort(key=lambda item: item["timestamp"])
            for segment in segments:
                duration = segment["end"] - segment["start"]
                segment["start"] = segment["timestamp"] - started_at_ts
                segment["end"] = segment["start"] + duration
                segment.pop("timestamp", None)
            last_end = segments[-1]["end"] if segments else 0
            new_finished_at = datetime.fromtimestamp(started_at_ts + last_end, tz=timezone.utc)
            if new_finished_at < finished_at:
                new_finished_at = finished_at

            with self._lock:
                conversation = self.conversations.get(conversation_id)
                if conversation is None:
                    return None
                if self._atomic and conversation.get("_version", 0) != read_version:
                    # Contention: someone else committed since our read. A real Firestore
                    # transaction would retry the whole function automatically -- loop and
                    # recompute the merge against the now-current state.
                    continue
                conversation["transcript_segments"] = segments
                conversation["finished_at"] = new_finished_at
                conversation["_version"] = read_version + 1
                self.update_calls.append(copy.deepcopy(segments))
                return {"discarded": discarded}


class FakeSyncSegmentStore:
    """In-memory stand-in for `database.sync_segments`'s durable claim/idempotency record plus
    transactional conversation merge. Faithful to its outcome contract (claimed/busy/done/lost)
    so these tests prove the route's claim -> process -> complete/release call sequence is
    correct, without needing a real Firestore (emulator-backed suites elsewhere in this repo cover
    the transaction machinery itself)."""

    def __init__(self, conversations: FakeConversationStore):
        self._lock = threading.Lock()
        self._segments: dict[tuple, dict] = {}
        self.conversations = conversations

    def claim_or_get(self, uid, segment_id, claimant, **_kwargs):
        with self._lock:
            key = (uid, segment_id)
            receipt = self._segments.get(key)
            if receipt and receipt["state"] == "done":
                return {"outcome": "done", "kind": receipt["kind"], "conversation_id": receipt["conversation_id"]}
            if receipt and receipt["state"] == "processing":
                return {"outcome": "busy"}
            # SYNC-V2-002 (new-conversation branch): the reserved id is deterministic from
            # (uid, segment_id) alone, same as the real `reserved_new_conversation_id`. On a
            # (re)claim -- no prior receipt, or a prior attempt that failed/expired -- check
            # whether an earlier attempt's `process_conversation` already durably committed a
            # conversation at that exact id even though this segment's own completion write
            # never landed (or its claimant just never got that far). If so, resume that result
            # instead of letting the caller re-run STT/LLM/persistence.
            reserved_id = (receipt or {}).get("reserved_conversation_id") or f"reserved-{uid}-{segment_id}"
            if reserved_id in self.conversations.conversations:
                self._segments[key] = {
                    "state": "done",
                    "claimant": claimant,
                    "kind": "new_memories",
                    "conversation_id": reserved_id,
                    "reserved_conversation_id": reserved_id,
                }
                return {"outcome": "done", "kind": "new_memories", "conversation_id": reserved_id}
            self._segments[key] = {"state": "processing", "claimant": claimant, "reserved_conversation_id": reserved_id}
            return {"outcome": "claimed", "claimant": claimant, "reserved_conversation_id": reserved_id}

    def complete_new_conversation(self, uid, segment_id, claimant, conversation_id, **_kwargs):
        with self._lock:
            key = (uid, segment_id)
            receipt = self._segments.get(key)
            if receipt and receipt["state"] == "done":
                return {"outcome": "done", "kind": receipt["kind"], "conversation_id": receipt["conversation_id"]}
            if not receipt or receipt.get("claimant") != claimant:
                return {"outcome": "lost"}
            receipt.update(state="done", kind="new_memories", conversation_id=conversation_id)
            return {"outcome": "done", "kind": "new_memories", "conversation_id": conversation_id}

    def append_and_complete(
        self, uid, segment_id, claimant, conversation_id, new_segments, segment_timestamp, **_kwargs
    ):
        key = (uid, segment_id)
        with self._lock:
            receipt = self._segments.get(key)
            if receipt and receipt["state"] == "done":
                return {"outcome": "done", "kind": receipt["kind"], "conversation_id": receipt["conversation_id"]}
        # The conversation merge is a separate document/transaction from the segment claim in the
        # real design (see database/sync_segments.py) -- don't hold this store's own lock across
        # it; FakeConversationStore.merge has its own lock standing in for that transaction.
        merge_result = self.conversations.merge(conversation_id, new_segments, segment_timestamp)
        if merge_result is None:
            return {"outcome": "conversation_missing"}
        with self._lock:
            self._segments[key] = {
                "state": "done",
                "claimant": claimant,
                "kind": "updated_memories",
                "conversation_id": conversation_id,
            }
        return {
            "outcome": "done",
            "kind": "updated_memories",
            "conversation_id": conversation_id,
            "discarded": merge_result["discarded"],
        }

    def release(self, uid, segment_id, claimant, **_kwargs):
        with self._lock:
            key = (uid, segment_id)
            receipt = self._segments.get(key)
            if receipt and receipt.get("claimant") == claimant and receipt.get("state") == "processing":
                receipt["state"] = "failed"
            return {"outcome": "released"}


@pytest.fixture(autouse=True)
def _fake_sync_segments(monkeypatch):
    """Wires fresh, correctly-atomic fakes for every test. Individual tests may further
    monkeypatch `sync.claim_or_get_sync_segment` / `sync.append_segment_to_conversation_and_complete`
    / etc. afterward to inject failures, and may reach into `.conversations` to seed/inspect the
    fake conversation store."""
    conversations = FakeConversationStore()
    segments = FakeSyncSegmentStore(conversations)
    monkeypatch.setattr(sync, "claim_or_get_sync_segment", segments.claim_or_get)
    monkeypatch.setattr(sync, "complete_new_conversation_sync_segment", segments.complete_new_conversation)
    monkeypatch.setattr(sync, "append_segment_to_conversation_and_complete", segments.append_and_complete)
    monkeypatch.setattr(sync, "release_sync_segment", segments.release)
    return SimpleNamespace(conversations=conversations, segments=segments)


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
    double-create a conversation — it returns the same outcome as the first successful call,
    found via the durable claim record (not re-run STT/LLM/persistence)."""
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
    # the same WAL content after a dropped response / app relaunch -- i.e. a "post-write/
    # pre-response failure": the first attempt's transaction fully committed (durable record
    # 'done'), but the client never saw the 200 and retries.
    Path(segment_path).write_bytes(segment_bytes)
    second = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP + 1}.bin", b"raw-bin-bytes-2"),
    )
    assert second.status_code == 200
    # Same segment content -> same segment id -> durable record replayed, no second STT/LLM call.
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


def test_v2_sync_local_files_claim_outage_fails_closed_not_false_success(monkeypatch, tmp_path):
    """SYNC-V2-001: an outage reading/claiming the durable idempotency record must not be reported
    back as a false-success 200 with failed_segments 0 — the segment was never actually
    processed."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 60}.wav")
    Path(segment_path).write_bytes(b"claim-outage-segment-bytes")
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    created = _stub_stt_and_llm(monkeypatch)

    def broken_claim(uid, segment_id, claimant, **_kwargs):
        raise ConnectionError("firestore unavailable")

    monkeypatch.setattr(sync, "claim_or_get_sync_segment", broken_claim)

    app = _app_with_uid_override("uid-claim-outage")
    client = TestClient(app)
    resp = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes"),
    )

    # Fail-closed (retryable, per this route's existing "all segments failed" contract) — never a
    # 200 claiming success.
    assert resp.status_code == 500
    assert created["calls"] == 0  # the outage was caught before STT ever ran


def test_v2_sync_local_files_new_conversation_completion_outage_fails_closed(monkeypatch, tmp_path):
    """SYNC-V2-001 (new-conversation branch): if the durable-completion write after creating a
    brand-new conversation fails, the segment must still be reported failed, never a false-success
    200 — otherwise a later replay has no durable record to dedupe against.

    SYNC-V2-002 (round 3 review): that "later replay" is exactly what happens next in real life —
    the client retries the same segment. `process_segment` persists the new conversation via
    `process_conversation` *before* this best-effort completion write, so a retry after this exact
    failure must not re-run STT/LLM/persistence and must not create a second conversation: it must
    recognize the reserved deterministic id already has a durable conversation sitting at it and
    resume that result. Wires its own claim/conversation stores (rather than the shared
    `_stub_stt_and_llm` helper) so the fake `process_conversation` can honor
    `CreateConversation.explicit_id` — the actual mechanism this fix depends on — and so the test
    can inspect exactly what got durably created."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 61}.wav")
    segment_bytes = b"completion-outage-segment-bytes"
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)

    conversations = FakeConversationStore()
    segments = FakeSyncSegmentStore(conversations)
    monkeypatch.setattr(sync, "claim_or_get_sync_segment", segments.claim_or_get)
    monkeypatch.setattr(sync, "release_sync_segment", segments.release)
    monkeypatch.setattr(sync, "get_closest_conversation_to_timestamps", lambda uid, s, e: None)

    stt_calls = {"n": 0}

    def counting_deepgram(url, speakers_count=3, attempts=0, return_language=True):
        stt_calls["n"] += 1
        return (["stub-words"], "en")

    def fake_postprocess_words(words, offset):
        return [TranscriptSegment(text="hello there", is_user=False, start=0.0, end=3.0)]

    created = {"calls": 0}

    def fake_process_conversation(uid, language, create_memory):
        created["calls"] += 1
        conversation_id = create_memory.explicit_id
        assert conversation_id, "process_segment must reserve and pass an explicit conversation id"
        # Mirrors what the real `process_conversation` durably commits: a conversation document
        # at exactly the reserved id.
        conversations.conversations[conversation_id] = {
            "id": conversation_id,
            "started_at": create_memory.started_at,
            "finished_at": create_memory.finished_at,
            "transcript_segments": [],
            "discarded": False,
            "_version": 0,
        }
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(sync, "deepgram_prerecorded", counting_deepgram)
    monkeypatch.setattr(sync, "postprocess_words", fake_postprocess_words)
    monkeypatch.setattr(sync, "process_conversation", fake_process_conversation)

    complete_calls = {"n": 0}

    def broken_complete(uid, segment_id, claimant, conversation_id, **_kwargs):
        complete_calls["n"] += 1
        raise ConnectionError("firestore unavailable")

    monkeypatch.setattr(sync, "complete_new_conversation_sync_segment", broken_complete)

    app = _app_with_uid_override("uid-completion-outage")
    client = TestClient(app)

    Path(segment_path).write_bytes(segment_bytes)
    first = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes-1"),
    )

    assert first.status_code == 500
    # Processing itself succeeded (STT ran, a conversation was created) — it's specifically the
    # durable-completion write that failed, and that alone must still fail the segment closed.
    assert created["calls"] == 1
    assert stt_calls["n"] == 1
    assert complete_calls["n"] == 1
    assert len(conversations.conversations) == 1
    reserved_id = next(iter(conversations.conversations))

    # The client retries with the exact same audio content after the failure. The completion
    # write is deliberately left broken: the retry must succeed WITHOUT ever needing it to work,
    # because the claim step itself recognizes the reserved id's conversation already exists.
    Path(segment_path).write_bytes(segment_bytes)
    second = client.post(
        "/v2/sync-local-files",
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP + 1}.bin", b"raw-bin-bytes-2"),
    )

    assert second.status_code == 200
    body = second.json()
    assert body["new_memories"] == [reserved_id]
    assert body["updated_memories"] == []
    assert body["failed_segments"] == 0
    # The pipeline ran exactly once in total: the retry replayed the reserved id's durable
    # conversation instead of re-running STT/LLM/persistence, and never called (let alone
    # depended on) the still-broken completion write again.
    assert created["calls"] == 1
    assert stt_calls["n"] == 1
    assert complete_calls["n"] == 1
    assert len(conversations.conversations) == 1


def test_v2_sync_local_files_append_transaction_outage_then_retry_writes_segment_exactly_once(monkeypatch, tmp_path):
    """Round-2 review regression: the prior design let persistence succeed and the *separate*
    idempotency-cache write fail independently, so a retry couldn't tell and reprocessed —
    duplicating the segment (2 provider/write executions for one logical segment). Here, the merge
    and the durable completion record are the same atomic operation
    (`append_segment_to_conversation_and_complete`); this proves that when that whole operation
    fails outright (nothing persisted) and the client retries, the segment is written to the
    conversation exactly once — never zero, never twice."""
    segment_path = str(tmp_path / f"{BIN_TIMESTAMP + 62}.wav")
    segment_bytes = b"append-transaction-outage-segment-bytes"
    _stub_vad_with_fixed_segment(monkeypatch, segment_path)
    _stub_stt_and_llm(monkeypatch)

    stt_calls = {"n": 0}
    original_deepgram = sync.deepgram_prerecorded

    def counting_deepgram(*args, **kwargs):
        stt_calls["n"] += 1
        return original_deepgram(*args, **kwargs)

    monkeypatch.setattr(sync, "deepgram_prerecorded", counting_deepgram)

    def fake_get_conversation(uid, conversation_id):
        if conversation_id != "conv-target":
            return None
        return {"id": "conv-target"}

    monkeypatch.setattr(sync.conversations_db, "get_conversation", fake_get_conversation)

    app = _app_with_uid_override("uid-append-outage")
    client = TestClient(app)

    # Wire a fresh fake conversation store directly (overriding the autouse fixture's default
    # object) so this test can seed a target conversation and inspect it afterward.
    conversations = FakeConversationStore()
    conversations.seed(
        "conv-target",
        started_at=datetime.fromtimestamp(BIN_TIMESTAMP, tz=timezone.utc),
        finished_at=datetime.fromtimestamp(BIN_TIMESTAMP + 5, tz=timezone.utc),
    )
    segments = FakeSyncSegmentStore(conversations)
    monkeypatch.setattr(sync, "claim_or_get_sync_segment", segments.claim_or_get)
    monkeypatch.setattr(sync, "release_sync_segment", segments.release)

    calls = {"n": 0}
    real_append = segments.append_and_complete

    def flaky_append(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the whole transaction failing outright (e.g. a Firestore outage during
            # commit) -- atomic, so nothing is persisted on this attempt.
            raise ConnectionError("firestore unavailable")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(sync, "append_segment_to_conversation_and_complete", flaky_append)

    Path(segment_path).write_bytes(segment_bytes)
    first = client.post(
        "/v2/sync-local-files",
        params={"conversation_id": "conv-target"},
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP}.bin", b"raw-bin-bytes-1"),
    )
    assert first.status_code == 500
    assert stt_calls["n"] == 1
    # Nothing persisted on the failed attempt.
    assert conversations.conversations["conv-target"]["transcript_segments"] == []

    # The client retries with the exact same audio content after the failure.
    Path(segment_path).write_bytes(segment_bytes)
    second = client.post(
        "/v2/sync-local-files",
        params={"conversation_id": "conv-target"},
        files=_bin_upload(f"audio_omibatch_opus_16000_1_fs160_{BIN_TIMESTAMP + 1}.bin", b"raw-bin-bytes-2"),
    )
    assert second.status_code == 200
    assert second.json()["updated_memories"] == ["conv-target"]

    # Exactly one write applied the segment to the conversation -- not lost, not duplicated.
    final_segments = conversations.conversations["conv-target"]["transcript_segments"]
    assert len(final_segments) == 1
    assert len([call for call in conversations.update_calls if call]) == 1


def test_v2_sync_local_files_concurrent_retries_of_same_segment_process_once(monkeypatch, tmp_path):
    """SYNC-V2-002: two concurrent requests replaying the exact same segment content must result
    in exactly one STT/LLM/persistence execution, never two racing on a non-atomic claim check."""
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


def test_v2_sync_local_files_release_never_stomps_a_successors_claim(monkeypatch):
    """The old Redis releasers did GET, compare in Python, then DEL -- if the claim they were
    releasing had already expired and been reclaimed by someone else, that non-atomic sequence
    could delete the *new* claimant's lease. `release_sync_segment` closes this by only releasing
    a claim it still atomically holds; prove the fake models that (a stale release does not affect
    a fresh claim held by a different claimant)."""
    conversations = FakeConversationStore()
    segments = FakeSyncSegmentStore(conversations)

    first_claim = segments.claim_or_get("uid-release", "seg-1", "claimant-a")
    assert first_claim["outcome"] == "claimed"

    # claimant-a's claim is released (e.g. after a failed attempt)...
    segments.release("uid-release", "seg-1", "claimant-a")

    # ...and a new claimant reclaims it.
    second_claim = segments.claim_or_get("uid-release", "seg-1", "claimant-b")
    assert second_claim["outcome"] == "claimed"

    # A late/stale release from claimant-a (e.g. a delayed retry of its own cleanup) must not
    # touch claimant-b's now-live claim.
    segments.release("uid-release", "seg-1", "claimant-a")
    third_claim = segments.claim_or_get("uid-release", "seg-1", "claimant-b")
    assert third_claim["outcome"] == "busy"  # claimant-b's claim is still intact


def test_v2_sync_local_files_parallel_segments_for_one_explicit_conversation_do_not_lose_writes(monkeypatch, tmp_path):
    """SYNC-V2-003: two VAD segments explicitly targeting the same conversation_id, processed
    concurrently, must both survive in the final segment list — not last-writer-wins. The barrier
    below sits *inside* the transactional merge, between reading the existing segments and writing
    the merged result back (the real read-modify-write window that must be atomic) rather than in
    the STT stub — a barrier placed in STT only proves the two segments overlap in time, not that
    the write itself is safe under that overlap, and would keep passing even with the
    per-conversation transaction removed."""
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
        return ([url], "en")  # smuggle the path through so postprocess_words can tell segments apart

    def fake_postprocess_words(words, offset):
        url = words[0]
        text = "segment a text" if seg_a in url else "segment b text"
        return [TranscriptSegment(text=text, is_user=False, start=0.0, end=3.0)]

    monkeypatch.setattr(sync, "deepgram_prerecorded", fake_deepgram_prerecorded)
    monkeypatch.setattr(sync, "postprocess_words", fake_postprocess_words)

    def fake_get_conversation(uid, conversation_id):
        return {"id": conversation_id} if conversation_id == "conv-target" else None

    monkeypatch.setattr(sync.conversations_db, "get_conversation", fake_get_conversation)

    conversations = FakeConversationStore()
    conversations.seed(
        "conv-target",
        started_at=datetime.fromtimestamp(BIN_TIMESTAMP, tz=timezone.utc),
        finished_at=datetime.fromtimestamp(BIN_TIMESTAMP + 5, tz=timezone.utc),
    )
    segments = FakeSyncSegmentStore(conversations)

    # Put the barrier *inside* the atomic merge window, between the read and the write, so both
    # segments are guaranteed to be mid-transaction at once -- and, protected by
    # FakeConversationStore's lock, one fully commits before the other's write proceeds. If
    # the underlying transaction/lock is removed, both threads read the same pre-merge snapshot
    # and the second write drops the first's segment; this is exactly what the fix prevents.
    real_merge = conversations.merge

    def barriered_merge(conversation_id, new_segments, segment_timestamp, **kwargs):
        return real_merge(conversation_id, new_segments, segment_timestamp, barrier=barrier.wait)

    monkeypatch.setattr(conversations, "merge", barriered_merge)

    monkeypatch.setattr(sync, "claim_or_get_sync_segment", segments.claim_or_get)
    monkeypatch.setattr(sync, "append_segment_to_conversation_and_complete", segments.append_and_complete)
    monkeypatch.setattr(sync, "release_sync_segment", segments.release)

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

    final_texts = {segment["text"] for segment in conversations.conversations["conv-target"]["transcript_segments"]}
    assert final_texts == {"segment a text", "segment b text"}
    # Both segments were merged one at a time (serialized), never overwriting each other.
    assert len(conversations.update_calls) == 2
    assert len(conversations.update_calls[-1]) == 2


def test_v2_sync_local_files_parallel_segments_lose_writes_without_the_transaction(tmp_path):
    """Mutation check for the regression above: with the per-conversation transaction disabled
    (FakeConversationStore(atomic=False), reproducing the pre-fix plain get+merge+set), the exact
    same barrier placement *does* lose a segment -- proving the previous test's guarantee comes
    from the transaction, not from incidental timing. This directly exercises
    `FakeConversationStore.merge`, not the route, since the route always goes through the atomic
    fake; it documents, in-suite, the failure this PR's fix closes."""
    conversations = FakeConversationStore(atomic=False)
    conversations.seed(
        "conv-target",
        started_at=datetime.fromtimestamp(BIN_TIMESTAMP, tz=timezone.utc),
        finished_at=datetime.fromtimestamp(BIN_TIMESTAMP + 5, tz=timezone.utc),
    )
    barrier = threading.Barrier(2, timeout=5)
    results = {}

    def run(key, text):
        results[key] = conversations.merge(
            "conv-target",
            [{"text": text, "is_user": False, "start": 0.0, "end": 3.0}],
            BIN_TIMESTAMP,
            barrier=barrier.wait,
        )

    t1 = threading.Thread(target=run, args=("a", "segment a text"))
    t2 = threading.Thread(target=run, args=("b", "segment b text"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    final_texts = {segment["text"] for segment in conversations.conversations["conv-target"]["transcript_segments"]}
    # Without the transaction, the second writer's read-modify-write silently drops the first's
    # segment -- only one of the two survives.
    assert final_texts != {"segment a text", "segment b text"}
    assert len(final_texts) == 1
