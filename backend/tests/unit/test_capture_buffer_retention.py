import importlib.util
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import sys
from types import ModuleType
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
sys.modules.setdefault("database.users", MagicMock())
sys.modules.setdefault("database.redis_db", MagicMock())
sys.modules.setdefault("utils.encryption", MagicMock())
sys.modules.setdefault("utils.other.storage", MagicMock())
_CONVERSATIONS_SPEC = importlib.util.spec_from_file_location(
    "database.capture_buffer_real_conversations",
    Path(__file__).parents[2] / "database" / "conversations.py",
)
assert _CONVERSATIONS_SPEC and _CONVERSATIONS_SPEC.loader
conversations_db = importlib.util.module_from_spec(_CONVERSATIONS_SPEC)
_CONVERSATIONS_SPEC.loader.exec_module(conversations_db)
from utils.capture_buffer import (
    acknowledge_capture_persistence_batch,
    capture_buffer_contains_conversation,
    deliver_all_pusher_transcript_batches,
    deliver_next_pusher_transcript_batch,
    prepare_capture_persistence_batch,
    prepare_conversation_bound_capture_batch,
    queue_pusher_transcript_batch,
)


class InMemoryOwnershipRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def eval(self, script, key_count, *args):
        if "if active_id == ARGV[1] then" in script:
            assert key_count == 3
            active_key, owner_key, lease_key = args[:key_count]
            conversation_id, processing_token, ttl = args[key_count:]
            if lease_key in self.values:
                return 0
            if self.values.get(active_key) == conversation_id:
                if owner_key in self.values:
                    return 0
                self.delete(active_key)
            self._set(lease_key, processing_token, ttl)
            return 1

        if "redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[3]) == 1" in script:
            assert key_count == 3
            active_key, owner_key, lease_key = args[:key_count]
            (expected_id,) = args[key_count:]
            if self.values.get(active_key) != expected_id or owner_key in self.values or lease_key in self.values:
                return 0
            self.delete(active_key)
            return 1

        if "redis.call('SET', KEYS[3], ARGV[2]" in script:
            assert key_count == 3
            active_key, owner_key, lease_key = args[:key_count]
            conversation_id, owner_id, ttl = args[key_count:]
            if (
                self.values.get(active_key) != conversation_id
                or self.values.get(owner_key) != owner_id
                or lease_key in self.values
            ):
                return 0
            self._set(lease_key, owner_id, ttl)
            return 1

        if "redis.call('DEL', KEYS[1])" in script:
            assert key_count == 1
            lease_key = args[0]
            owner_id = args[1]
            if self.values.get(lease_key) != owner_id:
                return 0
            self.delete(lease_key)
            return 1

        assert key_count == 2
        active_key, owner_key = args[:key_count]
        argv = args[key_count:]
        if "KEYS[1] .. ':capture_commit'" in script and active_key + ":capture_commit" in self.values:
            return 0

        if "local active_id = redis.call('GET', KEYS[1])" in script:
            conversation_id, owner_id, ttl = argv
            active_id = self.values.get(active_key)
            if active_id is not None and active_id != conversation_id:
                return 0
            self._set(active_key, conversation_id, ttl)
            self._set(owner_key, owner_id, ttl)
            return 1

        if "redis.call('SET', KEYS[1], ARGV[2]" in script:
            expected_id, new_id, new_owner, ttl = argv
            if self.values.get(active_key) != expected_id:
                return 0
            self._set(active_key, new_id, ttl)
            self._set(owner_key, new_owner, ttl)
            return 1

        if "redis.call('SET', KEYS[1], ARGV[1]" in script:
            conversation_id, owner_id, ttl = argv
            self._set(active_key, conversation_id, ttl)
            if owner_id:
                self._set(owner_key, owner_id, ttl)
            else:
                self.delete(owner_key)
            return 1

        if "redis.call('SET', KEYS[2], ARGV[2]" in script:
            conversation_id, owner_id, ttl = argv
            if self.values.get(active_key) != conversation_id:
                return 0
            self._set(owner_key, owner_id, ttl)
            self.ttls[active_key] = int(ttl)
            return 1

        if "redis.call('EXPIRE', KEYS[2], ARGV[3]" in script:
            conversation_id, owner_id, ttl = argv
            if self.values.get(active_key) != conversation_id or self.values.get(owner_key) != owner_id:
                return 0
            self.ttls[active_key] = int(ttl)
            self.ttls[owner_key] = int(ttl)
            return 1

        if "redis.call('SET', KEYS[1], ARGV[3]" in script:
            expected_id, expected_owner, new_id, new_owner, ttl = argv
            if self.values.get(active_key) != expected_id or self.values.get(owner_key) != expected_owner:
                return 0
            self._set(active_key, new_id, ttl)
            if new_owner:
                self._set(owner_key, new_owner, ttl)
            else:
                self.delete(owner_key)
            return 1

        raise AssertionError("unexpected Redis script")

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.ttls.pop(key, None)

    def get(self, key):
        value = self.values.get(key)
        return value.encode() if value is not None else None

    def _set(self, key, value, ttl):
        self.values[key] = str(value)
        self.ttls[key] = int(ttl)


def load_ownership_redis_db():
    redis_module = ModuleType("redis")
    redis_module.Redis = lambda **_kwargs: InMemoryOwnershipRedis()
    attestation_module = ModuleType("database.honcho_attestation")
    attestation_module.authority_credential = lambda *_args, **_kwargs: ""
    spec = importlib.util.spec_from_file_location(
        "database.transcription_ownership_redis_db",
        Path(__file__).parents[2] / "database" / "redis_db.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "redis": redis_module,
            "database.honcho_attestation": attestation_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


def test_capture_batch_waits_for_all_persistence_guards_without_clearing_buffers():
    segments = [{"id": "segment-1", "text": "content-not-logged"}]
    photos = [{"id": "photo-1"}]

    assert (
        prepare_capture_persistence_batch(
            segments,
            photos,
            conversation_ready=False,
            timestamp_ready=True,
        )
        is None
    )
    assert (
        prepare_capture_persistence_batch(
            segments,
            photos,
            conversation_ready=True,
            timestamp_ready=False,
        )
        is None
    )
    assert segments == [{"id": "segment-1", "text": "content-not-logged"}]
    assert photos == [{"id": "photo-1"}]


def test_capture_batch_acknowledges_once_after_persistence_and_preserves_new_arrivals():
    original_segment = {"id": "segment-1", "start": 0.0}
    original_photo = {"id": "photo-1"}
    segments = [original_segment]
    photos = [original_photo]
    batch = prepare_capture_persistence_batch(
        segments,
        photos,
        conversation_ready=True,
        timestamp_ready=True,
    )
    assert batch is not None

    batch.segments[0]["start"] = 3.5
    assert original_segment["start"] == 0.0

    new_segment = {"id": "segment-2", "start": 0.0}
    new_photo = {"id": "photo-2"}
    segments.append(new_segment)
    photos.append(new_photo)
    acknowledge_capture_persistence_batch(segments, photos, batch)

    assert segments == [new_segment]
    assert photos == [new_photo]
    with pytest.raises(RuntimeError, match="capture_buffer_changed_before_persistence_ack"):
        acknowledge_capture_persistence_batch(segments, photos, batch)

    bound_segments = [
        {"id": "segment-a", "_capture_conversation_id": "conversation-a"},
        {"id": "segment-b", "_capture_conversation_id": "conversation-b"},
    ]
    bound_photos = [
        {
            "_capture_conversation_id": "conversation-a",
            "photo": {"id": "photo-a"},
        },
        {
            "_capture_conversation_id": "conversation-b",
            "photo": {"id": "photo-b"},
        },
    ]
    bound_batch = prepare_conversation_bound_capture_batch(
        bound_segments,
        bound_photos,
        conversation_key="_capture_conversation_id",
        timestamp_ready=True,
    )

    assert bound_batch is not None
    assert bound_batch.conversation_id == "conversation-a"
    assert [item["id"] for item in bound_batch.segments] == ["segment-a"]
    assert [item["photo"]["id"] for item in bound_batch.photos] == ["photo-a"]
    acknowledge_capture_persistence_batch(bound_segments, bound_photos, bound_batch)
    assert [item["id"] for item in bound_segments] == ["segment-b"]
    assert [item["photo"]["id"] for item in bound_photos] == ["photo-b"]
    assert not capture_buffer_contains_conversation(
        bound_segments,
        bound_photos,
        conversation_key="_capture_conversation_id",
        conversation_id="conversation-a",
    )
    assert capture_buffer_contains_conversation(
        bound_segments,
        bound_photos,
        conversation_key="_capture_conversation_id",
        conversation_id="conversation-b",
    )

    pusher_queue = []
    queue_pusher_transcript_batch(
        pusher_queue,
        [{"id": "segment-a"}],
        "conversation-a",
    )
    sent_payloads = []

    async def fail_send(payload):
        sent_payloads.append(payload)
        raise ConnectionError("synthetic pusher failure")

    with pytest.raises(ConnectionError, match="synthetic pusher failure"):
        asyncio.run(deliver_next_pusher_transcript_batch(pusher_queue, fail_send))
    assert len(pusher_queue) == 1
    assert sent_payloads == [
        {
            "segments": [{"id": "segment-a"}],
            "memory_id": "conversation-a",
        }
    ]

    async def succeed_send(payload):
        sent_payloads.append(payload)

    assert asyncio.run(deliver_next_pusher_transcript_batch(pusher_queue, succeed_send))
    assert pusher_queue == []
    assert sent_payloads[-1]["memory_id"] == "conversation-a"

    queue = []
    for suffix in ("a", "b", "c"):
        queue_pusher_transcript_batch(
            queue,
            [{"id": f"segment-{suffix}"}],
            f"conversation-{suffix}",
        )
    sent = []

    async def fail_second(payload):
        sent.append(payload["memory_id"])
        if payload["memory_id"] == "conversation-b":
            raise ConnectionError("synthetic ordered failure")

    with pytest.raises(ConnectionError, match="synthetic ordered failure"):
        asyncio.run(deliver_all_pusher_transcript_batches(queue, fail_second))
    assert sent == ["conversation-a", "conversation-b"]
    assert [batch.conversation_id for batch in queue] == ["conversation-b", "conversation-c"]

    async def succeed(payload):
        sent.append(payload["memory_id"])

    assert asyncio.run(deliver_all_pusher_transcript_batches(queue, succeed)) == 2
    assert sent == ["conversation-a", "conversation-b", "conversation-b", "conversation-c"]
    assert queue == []


def test_transcribe_acknowledges_only_after_database_persistence_and_never_logs_content():
    source = (Path(__file__).parents[2] / "routers" / "transcribe.py").read_text()
    stream_source = source.split("async def stream_transcript_process():", maxsplit=1)[1].split(
        "async def conversation_timeout_task():", maxsplit=1
    )[0]

    persistence_index = stream_source.index("persist_and_commit_capture_persistence_batch(")
    acknowledge_index = stream_source.index("acknowledge_capture_persistence_batch(")
    assert acknowledge_index > persistence_index
    assert "realtime_segment_buffers = []" not in stream_source
    assert "realtime_photo_buffers = []" not in stream_source
    assert "First segment:" not in stream_source
    assert "first_text=" not in source
    assert "except Exception:" in stream_source[persistence_index:acknowledge_index]
    assert "capture_persistence_retry" in stream_source[persistence_index:acknowledge_index]

    assert "for recovery_conversation_id in tuple(capture_recovery_conversation_ids):" in stream_source
    assert "capture_recovery_conversation_ids.add(batch_conversation_id)" in stream_source
    assert "capture_recovery_conversation_ids.discard(recovery_conversation_id)" in stream_source
    assert (
        "list_capture_persistence_batches(\n                        uid,\n                        current_conversation_id"
        not in stream_source
    )
    fence_source = source.split("def _capture_buffers_contain_conversation", maxsplit=1)[1].split(
        "async def _wait_for_capture_buffers_to_drain", maxsplit=1
    )[0]
    image_source = source.split("async def handle_image_chunk", maxsplit=1)[1].split(
        "# Initialize decoders", maxsplit=1
    )[0]
    receive_finally = source.split("async def receive_data", maxsplit=1)[1].split("# Start", maxsplit=1)[0]

    assert "for upload in image_chunks.values()" in fence_source
    assert "for bound_conversation_id, task in photo_processing_tasks.values()" in fence_source
    assert "and not task.done()" in fence_source
    assert "photo_processing_tasks[temp_id] = (bound_conversation_id, task)" in image_source
    assert "task.add_done_callback(photo_processing_done)" in image_source
    assert "image_chunks.clear()" in receive_finally
    assert "capture_buffers_changed.set()" in receive_finally
    atomic_capture_block = stream_source.split("if transcript_segments or photos_to_process:", maxsplit=1)[1].split(
        "if removed_ids:", maxsplit=1
    )[0]

    assert "photos=photos_to_process" in atomic_capture_block
    assert "store_conversation_photos" not in stream_source
    assert "segments=bool(transcript_segments)" in atomic_capture_block
    assert "photos=bool(photos_to_process)" in atomic_capture_block
    assert 'phase="live"' in atomic_capture_block


def test_photo_only_capture_does_not_require_audio_timestamp():
    photo = {
        "_capture_conversation_id": "conversation-a",
        "photo": {"id": "photo-a"},
    }
    batch = prepare_conversation_bound_capture_batch(
        [],
        [photo],
        conversation_key="_capture_conversation_id",
        timestamp_ready=False,
    )

    assert batch is not None
    assert batch.conversation_id == "conversation-a"
    assert batch.photos == (photo,)


def test_capture_batch_can_acknowledge_segments_without_dropping_photos():
    segment = {"id": "segment-1"}
    photo = {"id": "photo-1"}
    segments = [segment]
    photos = [photo]
    batch = prepare_capture_persistence_batch(
        segments,
        photos,
        conversation_ready=True,
        timestamp_ready=True,
    )
    assert batch is not None

    acknowledge_capture_persistence_batch(
        segments,
        photos,
        batch,
        segments=True,
        photos=False,
    )
    assert segments == []
    assert photos == [photo]


def test_atomic_capture_commit_deduplicates_replay_and_deletes_durable_batch(monkeypatch):
    class OrderingSnapshot:
        def __init__(self, batch_id, created_at):
            self.id = batch_id
            self._created_at = created_at

        def to_dict(self):
            return {"created_at": self._created_at}

    later = OrderingSnapshot(
        "000-hash-sorts-first",
        datetime(2026, 8, 12, 20, 0, 2, tzinfo=timezone.utc),
    )
    earlier = OrderingSnapshot(
        "fff-hash-sorts-last",
        datetime(2026, 8, 12, 20, 0, 1, tzinfo=timezone.utc),
    )
    ordered = sorted(
        [later, earlier],
        key=conversations_db._capture_persistence_batch_sort_key,
    )
    assert [snapshot.id for snapshot in ordered] == [
        "fff-hash-sorts-last",
        "000-hash-sorts-first",
    ]

    segment = {
        "id": "segment-1",
        "text": "synthetic",
        "speaker": "SPEAKER_00",
        "is_user": True,
        "start": 0.0,
        "end": 1.0,
    }
    conversation = {
        "id": "conversation-a",
        "capture_owner_id": "socket-a",
        "data_protection_level": "standard",
        "transcript_segments": [segment],
    }
    payload = {
        "conversation_id": "conversation-a",
        "segments": [segment],
        "photos": [],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "capture_owner_id": "socket-a",
    }

    class Snapshot:
        def __init__(self, exists, data):
            self.exists = exists
            self._data = data

        def to_dict(self):
            return self._data

    class Ref:
        def __init__(self, snapshot, ref_id="ref"):
            self.snapshot = snapshot
            self.id = ref_id

        def get(self, transaction=None):
            return self.snapshot

    class Transaction:
        def __init__(self):
            self.updates = []
            self.deletes = []

        def update(self, ref, update):
            self.updates.append(update)

        def delete(self, ref):
            self.deletes.append(ref)

    transaction = Transaction()
    conversation_ref = Ref(Snapshot(True, conversation))
    batch_ref = Ref(Snapshot(True, {"batch_id": "batch-a", "payload": "encrypted"}), "batch-a")
    monkeypatch.setattr(
        conversations_db,
        "_decode_capture_persistence_batch",
        lambda uid, data: payload,
    )
    monkeypatch.setattr(
        conversations_db,
        "_prepare_conversation_for_read",
        lambda data, uid: data,
    )
    monkeypatch.setattr(
        conversations_db,
        "_prepare_conversation_for_write",
        lambda data, uid, level: data,
    )

    result = conversations_db._commit_capture_persistence_batch_transaction(
        transaction,
        conversation_ref,
        batch_ref,
        "uid-a",
        "conversation-a",
        "socket-a",
    )

    assert result["status"] == "committed"
    assert result["updated_segments"] == []
    assert "transcript_segments" not in transaction.updates[0]
    assert transaction.updates[0]["capture_persistence_applied_batch_ids"] == ["batch-a"]
    assert transaction.deletes == [batch_ref]


def test_capture_commit_rejects_conversation_rotation_before_write(monkeypatch):
    source = (Path(__file__).parents[2] / "routers" / "transcribe.py").read_text()
    stream_source = source.split("async def stream_transcript_process():", maxsplit=1)[1].split(
        "async def conversation_timeout_task():", maxsplit=1
    )[0]
    process_source = source.split("async def _process_conversation", maxsplit=1)[1].split(
        "async def _prepare_in_progess_conversations", maxsplit=1
    )[0]
    disconnect_finalize_source = source.split("async def _finalize_current_conversation_on_disconnect", maxsplit=1)[
        1
    ].split("# Process existing conversations", maxsplit=1)[0]
    prepare_source = source.split("async def _prepare_in_progess_conversations", maxsplit=1)[1].split(
        "_send_message_event(", maxsplit=1
    )[0]
    heartbeat_source = source.split("async def send_heartbeat", maxsplit=1)[1].split("# Start heart beat", maxsplit=1)[
        0
    ]
    lifecycle_source = source.split("async def conversation_lifecycle_manager", maxsplit=1)[1].split(
        "async def speaker_identification_task", maxsplit=1
    )[0]
    pusher_source = source.split("def create_pusher_task_handler", maxsplit=1)[1].split("# Translate", maxsplit=1)[0]
    shutdown_source = source.split("finally:\n        if not use_custom_stt", maxsplit=1)[1].split(
        "# STT sockets", maxsplit=1
    )[0]

    assert "bind_capture_conversation(segment)" in source
    assert "CAPTURE_CONVERSATION_ID_KEY: conversation_id" in source
    assert "prepare_conversation_bound_capture_batch(" in stream_source
    assert "conversation_id=batch_conversation_id" in stream_source
    assert "drain_capture_persistence_batches(uid, conversation_id, session_id)" in disconnect_finalize_source
    assert "drain_capture_persistence_batches(uid, conversation_id_to_process, session_id)" in lifecycle_source
    assert "if wait_for_buffers and not await _wait_for_capture_buffers_to_drain(conversation_id):" in process_source
    assert "return False" in process_source
    assert "processing_result = await request_conversation_processing(conversation_id)" in process_source
    assert "if processing_result == 'unavailable':" in process_source
    assert "await _create_conversation_fallback(conversation)" in process_source
    assert "conversation_id_to_process = current_conversation_id" in lifecycle_source
    assert "expected_conversation_id=conversation_id_to_process" in lifecycle_source
    assert "expected_owner_id=session_id" in lifecycle_source
    assert lifecycle_source.count("expected_conversation_id=current_conversation_id") == 2
    assert lifecycle_source.count('reason="socket_ownership_lost"') == 3
    assert "_schedule_conversation_processing_after_rotation(conversation_id_to_process)" in lifecycle_source
    assert lifecycle_source.index("expected_conversation_id=conversation_id_to_process") < lifecycle_source.index(
        "_schedule_conversation_processing_after_rotation(conversation_id_to_process)"
    )
    assert lifecycle_source.index(
        "drain_capture_persistence_batches(uid, conversation_id_to_process, session_id)"
    ) < lifecycle_source.index("expected_conversation_id=conversation_id_to_process")
    assert "await _process_conversation_after_rotation(conversation_id_to_process)" not in lifecycle_source
    assert "conversation_finalize_tasks.add(task)" in process_source
    assert "conversation_finalize_tasks.discard(completed)" in process_source
    assert "async def _await_conversation_finalize_tasks" in process_source
    assert "conversation.get('status') != ConversationStatus.in_progress" in disconnect_finalize_source
    assert "_wait_for_capture_buffers_to_drain(conversation_id, timeout_seconds=5.0)" in disconnect_finalize_source
    assert "expected_conversation_id=conversation_id" in disconnect_finalize_source
    assert "expected_owner_id=session_id" in disconnect_finalize_source
    assert 'reason="socket_ownership_lost"' in disconnect_finalize_source
    assert disconnect_finalize_source.index(
        "drain_capture_persistence_batches(uid, conversation_id, session_id)"
    ) < disconnect_finalize_source.index("expected_conversation_id=conversation_id")
    assert disconnect_finalize_source.index(
        "expected_conversation_id=conversation_id"
    ) < disconnect_finalize_source.index("_process_conversation(conversation_id, wait_for_buffers=False)")
    assert "_process_conversation(conversation_id, wait_for_buffers=False)" in disconnect_finalize_source
    assert '"capture_disconnect_finalize_deferred"' in disconnect_finalize_source
    assert '"capture_disconnect_finalized"' in disconnect_finalize_source
    assert "claim_in_progress_conversation_id(" in prepare_source
    assert "uid, candidate_id, session_id" in prepare_source
    assert "active_conversation_id != candidate_id" in prepare_source
    assert "replace_stale_in_progress_conversation_id(" in prepare_source
    assert "active_conversation_id," in prepare_source
    assert "candidate_id," in prepare_source
    assert "replace_stale_conversation_id=active_conversation_id or None" in prepare_source
    assert 'raise RuntimeError("active conversation ownership changed during reconnect")' in prepare_source
    assert "refresh_in_progress_conversation_id(" in heartbeat_source
    assert '"capture_socket_ownership_lost"' in heartbeat_source
    assert "await _finalize_current_conversation_on_disconnect()" in shutdown_source
    assert "await _await_conversation_finalize_tasks()" in shutdown_source
    assert shutdown_source.index("await _finalize_current_conversation_on_disconnect()") < shutdown_source.index(
        "await _await_conversation_finalize_tasks()"
    )
    assert '"capture_disconnect_finalize_error"' in shutdown_source
    assert "while True:" in process_source
    assert "while websocket_active:" not in process_source
    assert "queue_pusher_transcript_batch(" in pusher_source
    assert "deliver_all_pusher_transcript_batches(" in pusher_source
    assert "batch_conversation_id," in stream_source
    assert "await translate(updated_segments, batch_conversation_id)" in stream_source
    assert "await translate(updated_segments, conversation.id)" not in stream_source

    class Snapshot:
        exists = True

        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return self._data

    class Ref:
        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return Snapshot(self.data)

    class Transaction:
        def update(self, *_args):
            pytest.fail("rotation mismatch must not write")

        def delete(self, *_args):
            pytest.fail("rotation mismatch must retain the durable batch")

    monkeypatch.setattr(
        conversations_db,
        "_decode_capture_persistence_batch",
        lambda uid, data: {
            "conversation_id": "conversation-b",
            "segments": [],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    with pytest.raises(ValueError, match="conversation_mismatch"):
        conversations_db._commit_capture_persistence_batch_transaction(
            Transaction(),
            Ref({"transcript_segments": []}),
            Ref({"batch_id": "batch-a", "payload": "encrypted"}),
            "uid-a",
            "conversation-a",
        )


def test_active_conversation_lease_fences_reconnect_overlap_and_restores_expired_id():
    redis_db = load_ownership_redis_db()
    redis_db.set_in_progress_conversation_id("uid-a", "conversation-a", owner_id="socket-old")

    assert redis_db.claim_in_progress_conversation_id("uid-a", "conversation-a", "socket-replacement")
    assert not redis_db.rotate_in_progress_conversation_id(
        "uid-a",
        "conversation-a",
        "socket-old",
        "conversation-stale-replacement",
    )
    assert redis_db.get_in_progress_conversation_id("uid-a") == "conversation-a"

    assert redis_db.rotate_in_progress_conversation_id(
        "uid-a",
        "conversation-a",
        "socket-replacement",
        "conversation-b",
    )
    assert not redis_db.refresh_in_progress_conversation_id("uid-a", "conversation-a", "socket-old")
    assert redis_db.claim_in_progress_conversation_id("uid-a", "conversation-b", "socket-next")

    assert not redis_db.release_unowned_in_progress_conversation_id("uid-a", "conversation-b")
    redis_db.r.delete("users:uid-a:in_progress_memory_owner")
    assert redis_db.release_unowned_in_progress_conversation_id("uid-a", "conversation-b")
    assert redis_db.claim_in_progress_conversation_id("uid-a", "conversation-recovered", "socket-recovered")
    assert not redis_db.claim_in_progress_conversation_id("uid-a", "conversation-stale", "socket-stale")
    assert redis_db.get_in_progress_conversation_id("uid-a") == "conversation-recovered"


def test_capture_commit_lease_blocks_ownership_transfer_until_release():
    redis_db = load_ownership_redis_db()
    redis_db.set_in_progress_conversation_id("uid-a", "conversation-a", owner_id="socket-old")

    assert redis_db.acquire_capture_commit_lease("uid-a", "conversation-a", "socket-old")
    assert not redis_db.claim_in_progress_conversation_id("uid-a", "conversation-a", "socket-new")
    assert not redis_db.rotate_in_progress_conversation_id(
        "uid-a",
        "conversation-a",
        "socket-old",
        "conversation-b",
    )
    assert redis_db.release_capture_commit_lease("uid-a", "socket-old")
    assert redis_db.claim_in_progress_conversation_id("uid-a", "conversation-a", "socket-new")
    assert not redis_db.acquire_capture_commit_lease("uid-a", "conversation-a", "socket-old")


def test_manual_processing_cannot_release_active_or_committing_capture():
    redis_db = load_ownership_redis_db()
    redis_db.set_in_progress_conversation_id("uid-a", "conversation-a", owner_id="socket-old")

    assert redis_db.get_in_progress_conversation_owner("uid-a") == "socket-old"
    assert not redis_db.release_unowned_in_progress_conversation_id("uid-a", "conversation-a")

    redis_db.r.delete("users:uid-a:in_progress_memory_owner")
    redis_db.r._set("users:uid-a:in_progress_memory_id:capture_commit", "socket-old", 120)
    assert not redis_db.release_unowned_in_progress_conversation_id("uid-a", "conversation-a")

    redis_db.r.delete("users:uid-a:in_progress_memory_id:capture_commit")
    assert redis_db.release_unowned_in_progress_conversation_id("uid-a", "conversation-a")


def test_manual_processing_fence_blocks_exact_reconnect_until_processing_claim():
    redis_db = load_ownership_redis_db()
    redis_db.set_in_progress_conversation_id("uid-a", "replacement", owner_id="socket-current")

    assert redis_db.acquire_in_progress_processing_fence("uid-a", "conversation-closed", "processing-token")
    assert not redis_db.replace_stale_in_progress_conversation_id(
        "uid-a",
        "replacement",
        "conversation-closed",
        "socket-reconnect",
    )
    assert not redis_db.claim_in_progress_conversation_id(
        "uid-a",
        "conversation-closed",
        "socket-reconnect",
    )

    assert redis_db.release_capture_commit_lease("uid-a", "processing-token")
    assert redis_db.replace_stale_in_progress_conversation_id(
        "uid-a",
        "replacement",
        "conversation-closed",
        "socket-reconnect",
    )


def test_stale_active_conversation_id_can_only_be_replaced_by_exact_cas():
    redis_db = load_ownership_redis_db()
    redis_db.set_in_progress_conversation_id("uid-a", "conversation-stale", owner_id="socket-old")

    assert not redis_db.replace_stale_in_progress_conversation_id(
        "uid-a",
        "conversation-other",
        "conversation-new",
        "socket-new",
    )
    assert redis_db.get_in_progress_conversation_id("uid-a") == "conversation-stale"

    assert redis_db.replace_stale_in_progress_conversation_id(
        "uid-a",
        "conversation-stale",
        "conversation-new",
        "socket-new",
    )
    assert redis_db.get_in_progress_conversation_id("uid-a") == "conversation-new"
    assert not redis_db.replace_stale_in_progress_conversation_id(
        "uid-a",
        "conversation-stale",
        "conversation-loser",
        "socket-loser",
    )
