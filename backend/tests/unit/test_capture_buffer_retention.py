import importlib.util
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import sys
from unittest.mock import MagicMock

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

    durable_index = stream_source.index("persist_capture_persistence_batch(")
    persistence_index = stream_source.index("commit_capture_persistence_batch(", durable_index)
    acknowledge_index = stream_source.index("acknowledge_capture_persistence_batch(")
    assert durable_index < persistence_index
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
    photo_block = stream_source.split("if photos_to_process:", maxsplit=1)[1].split("if removed_ids:", maxsplit=1)[0]

    assert "except Exception:" in photo_block
    assert "continue" not in photo_block
    assert "else:" in photo_block
    assert "segments=True" in stream_source.split("if photos_to_process:", maxsplit=1)[0]
    assert stream_source.index("if transcript_segments:", stream_source.index("if removed_ids:")) > stream_source.index(
        "if photos_to_process:"
    )


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
        "data_protection_level": "standard",
        "transcript_segments": [segment],
    }
    payload = {
        "conversation_id": "conversation-a",
        "segments": [segment],
        "finished_at": datetime.now(timezone.utc).isoformat(),
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
    )

    assert result["status"] == "committed"
    assert result["updated_segments"] == []
    assert len(transaction.updates[0]["transcript_segments"]) == 1
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
    assert "drain_capture_persistence_batches(uid, conversation_id)" in process_source
    assert "if not await _wait_for_capture_buffers_to_drain(conversation_id):" in process_source
    assert "return False" in process_source
    assert "conversation_id_to_process = current_conversation_id" in lifecycle_source
    assert "await _create_new_in_progress_conversation()" in lifecycle_source
    assert "await _process_conversation_after_rotation(conversation_id_to_process)" in lifecycle_source
    assert lifecycle_source.index("await _create_new_in_progress_conversation()") < lifecycle_source.index(
        "await _process_conversation_after_rotation(conversation_id_to_process)"
    )
    assert "conversation.get('status') != ConversationStatus.in_progress" in disconnect_finalize_source
    assert "_process_conversation_after_rotation(conversation_id)" in disconnect_finalize_source
    assert "timeout=5.0" in disconnect_finalize_source
    assert '"capture_disconnect_finalize_deferred"' in disconnect_finalize_source
    assert '"capture_disconnect_finalized"' in disconnect_finalize_source
    assert "await _finalize_current_conversation_on_disconnect()" in shutdown_source
    assert '"capture_disconnect_finalize_error"' in shutdown_source
    assert "while True:" in process_source
    assert "while websocket_active:" not in process_source
    assert "queue_pusher_transcript_batch(" in pusher_source
    assert "deliver_all_pusher_transcript_batches(" in pusher_source
    assert "batch_conversation_id," in stream_source

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
