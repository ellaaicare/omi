from datetime import datetime, timezone

from ella.services.guardian_playback_ledger import PlaybackCandidate
from utils.ella.scanner import (
    _build_wake_ack_payload,
    prepare_scanner_segments_for_dispatch,
    select_playback_ledger_candidates,
)


def _candidate(playback_id="guardian_abc123", text="Hi, Greg. I heard my name."):
    return PlaybackCandidate(
        playback_id=playback_id,
        queue_item_id=playback_id,
        trace_id="trace-1",
        purpose="wake_word",
        playback_text=text,
        route="Speaker",
        device_class="high",
        duration_ms=2500,
        started_at=datetime(2026, 9, 28, 15, 4, 5, tzinfo=timezone.utc),
        completed_at=datetime(2026, 9, 28, 15, 4, 7, 500000, tzinfo=timezone.utc),
    )


def test_select_playback_ledger_candidates_is_owner_scoped_selection_only(monkeypatch):
    """Selection returns the classifier-ready shape and never suppresses anything itself."""
    import utils.ella.scanner as scanner

    seen_calls = []

    async def fake_get_pool():
        return "fake-pool"

    async def fake_get_played_candidates(pool, uid, *, window_seconds, limit):
        seen_calls.append((pool, uid, window_seconds, limit))
        return [_candidate()]

    monkeypatch.setattr(scanner.guardian_playback_ledger, "get_pool", fake_get_pool)
    monkeypatch.setattr(scanner.guardian_playback_ledger, "get_played_candidates", fake_get_played_candidates)

    candidates = select_playback_ledger_candidates("uid-1", window_seconds=45, limit=5)

    assert seen_calls == [("fake-pool", "uid-1", 45, 5)]
    assert candidates == [
        {
            "playback_id": "guardian_abc123",
            "text": "Hi, Greg. I heard my name.",
            "started_at": "2026-09-28T15:04:05+00:00",
            "completed_at": "2026-09-28T15:04:07.500000+00:00",
            "duration_ms": 2500,
        }
    ]


def test_select_playback_ledger_candidates_fails_open_to_empty_list(monkeypatch):
    """A ledger outage must never block scanner dispatch — it just yields no candidates."""
    import utils.ella.scanner as scanner

    async def failing_get_pool():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(scanner.guardian_playback_ledger, "get_pool", failing_get_pool)

    assert select_playback_ledger_candidates("uid-1") == []


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
