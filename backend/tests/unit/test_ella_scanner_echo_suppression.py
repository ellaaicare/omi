from utils.ella.scanner import should_suppress_guardian_echo


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
