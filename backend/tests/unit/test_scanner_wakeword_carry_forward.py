from utils.ella.scanner import (
    contains_wake_phrase,
    is_short_wake_prefix_only,
    prepare_scanner_segments_for_dispatch,
)


def _segment(text: str, speaker: str = "SPEAKER_00") -> dict:
    return {"text": text, "speaker": speaker}


def test_short_wake_prefix_is_held_for_next_dispatch():
    dispatch, pending, pending_since, meta = prepare_scanner_segments_for_dispatch(
        [_segment("Hey Ella", "SPEAKER_00")],
        now=100.0,
    )

    assert dispatch == []
    assert [segment["text"] for segment in pending] == ["Hey Ella"]
    assert pending_since == 100.0
    assert meta["action"] == "hold_wake_prefix"


def test_pending_wake_prefix_is_prepended_to_next_batch():
    dispatch, pending, pending_since, meta = prepare_scanner_segments_for_dispatch(
        [_segment("What time is my appointment today?", "SPEAKER_02")],
        pending_wake_prefix_segments=[_segment("Hey Ella", "SPEAKER_00")],
        pending_wake_prefix_since=100.0,
        now=101.0,
    )

    assert [segment["text"] for segment in dispatch] == [
        "Hey Ella",
        "What time is my appointment today?",
    ]
    assert pending == []
    assert pending_since is None
    assert meta["action"] == "prepend_pending_wake_prefix"
    assert meta["prepended_count"] == 1


def test_expired_pending_wake_prefix_is_dropped():
    dispatch, pending, pending_since, meta = prepare_scanner_segments_for_dispatch(
        [_segment("What time is my appointment today?", "SPEAKER_02")],
        pending_wake_prefix_segments=[_segment("Hey Ella", "SPEAKER_00")],
        pending_wake_prefix_since=100.0,
        now=103.5,
    )

    assert [segment["text"] for segment in dispatch] == ["What time is my appointment today?"]
    assert pending == []
    assert pending_since is None
    assert meta["action"] == "direct_dispatch"


def test_direct_wake_phrase_in_current_batch_dispatches_normally():
    dispatch, pending, pending_since, meta = prepare_scanner_segments_for_dispatch(
        [_segment("Hey Ella what time is my appointment today?", "SPEAKER_00")],
        now=100.0,
    )

    assert [segment["text"] for segment in dispatch] == ["Hey Ella what time is my appointment today?"]
    assert pending == []
    assert pending_since is None
    assert meta["action"] == "direct_dispatch"


def test_ela_variant_is_treated_as_short_wake_prefix():
    assert contains_wake_phrase("Ela")
    assert is_short_wake_prefix_only([_segment("Ela", "SPEAKER_00")])
