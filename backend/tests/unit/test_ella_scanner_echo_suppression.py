from utils.ella.scanner import (
    _build_wake_ack_payload,
    prepare_scanner_segments_for_dispatch,
    should_suppress_guardian_echo,
)


def test_suppresses_recent_high_risk_guardian_playback_echo():
    segments = [
        {
            "speaker": "SPEAKER_2",
            "text": "Hi, Greg. I heard my name. I'm here with you. Tell me what you need.",
        }
    ]

    assert should_suppress_guardian_echo(
        "uid-1",
        segments,
        playback_event={"echo_risk": "high", "recorded_at": 1},
    )


def test_does_not_suppress_real_wake_word_question():
    segments = [{"speaker": "SPEAKER_1", "text": "Hey Ella, where did I put my glasses?"}]

    assert not should_suppress_guardian_echo(
        "uid-1",
        segments,
        playback_event={"echo_risk": "high", "recorded_at": 1},
    )


def test_does_not_suppress_echo_like_text_without_risky_playback_event():
    segments = [{"speaker": "SPEAKER_1", "text": "Why did it say hi Greg I heard my name?"}]

    assert not should_suppress_guardian_echo("uid-1", segments, playback_event={})


def test_short_wake_prefix_dispatches_immediately():
    segments = [{"speaker": "SPEAKER_1", "text": "Hey, Ella."}]

    dispatch_segments, pending_segments, pending_since, metadata = prepare_scanner_segments_for_dispatch(
        segments,
        now=100.0,
    )

    assert dispatch_segments == segments
    assert pending_segments == []
    assert pending_since is None
    assert metadata["action"] == "direct_wake_prefix_dispatch"


def test_existing_pending_wake_prefix_still_prepends_to_followup():
    pending = [{"speaker": "SPEAKER_1", "text": "Hey, Ella."}]
    current = [{"speaker": "SPEAKER_1", "text": "What did you hear last?"}]

    dispatch_segments, pending_segments, pending_since, metadata = prepare_scanner_segments_for_dispatch(
        current,
        pending_wake_prefix_segments=pending,
        pending_wake_prefix_since=100.0,
        now=101.0,
    )

    assert dispatch_segments == pending + current
    assert pending_segments == []
    assert pending_since is None
    assert metadata["action"] == "prepend_pending_wake_prefix"


def test_wake_ack_payload_is_built_for_wake_question():
    segments = [{"speaker": "SPEAKER_1", "text": "Hey Ella, are cats clean animals?"}]

    payload = _build_wake_ack_payload("uid-1", "conv-1", "trace-1", segments)

    assert payload is not None
    assert payload["id"].startswith("wake_ack_trace-1_wake_")
    assert payload["trigger"] == "wake_word_ack"
    assert payload["metadata"]["ack_only"] is True
    assert payload["metadata"]["parent_conversation_id"] == "conv-1"
    assert payload["metadata"]["segments_preview"][0]["text"] == "Hey Ella, are cats clean animals?"


def test_wake_ack_payload_ignores_non_wake_ambient_text():
    segments = [{"speaker": "SPEAKER_1", "text": "The cats on the video are clean animals."}]

    assert _build_wake_ack_payload("uid-1", "conv-1", "trace-1", segments) is None
