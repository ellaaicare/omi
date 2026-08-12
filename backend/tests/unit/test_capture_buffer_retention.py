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
import database.conversations as conversations_db
from utils.capture_buffer import acknowledge_capture_persistence_batch, prepare_capture_persistence_batch


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


def test_photo_only_capture_does_not_require_audio_timestamp():
    source = (Path(__file__).parents[2] / "routers" / "transcribe.py").read_text()
    stream_source = source.split("async def stream_transcript_process():", maxsplit=1)[1].split(
        "async def conversation_timeout_task():", maxsplit=1
    )[0]

    assert "if realtime_segment_buffers and first_audio_byte_timestamp is None:" in stream_source
    assert "not realtime_segment_buffers" in stream_source


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
