from pathlib import Path

import pytest

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

    persistence_index = stream_source.index("_update_in_progress_conversation(")
    acknowledge_index = stream_source.index("acknowledge_capture_persistence_batch(")
    assert acknowledge_index > persistence_index
    assert "realtime_segment_buffers = []" not in stream_source
    assert "realtime_photo_buffers = []" not in stream_source
    assert "First segment:" not in stream_source
