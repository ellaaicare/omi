import json
from datetime import datetime, timezone

from ella.services.guardian_echo_classifier import EchoClassification
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


def _echo_classification(**overrides):
    fields = dict(
        schema_version="guardian_playback_source_v1",
        is_ella_playback=True,
        source="ella_playback",
        contains_additional_live_speech=False,
        matched_playback_ids=["guardian_abc123"],
        live_speech_spans=[],
        confidence=0.9,
        reason_code="test_fixture",
        fail_open=False,
    )
    fields.update(overrides)
    return EchoClassification(**fields)


def test_select_playback_ledger_candidates_is_owner_scoped_selection_only(monkeypatch):
    """Selection returns the classifier-ready shape and never suppresses anything itself."""
    import utils.ella.scanner as scanner

    seen_calls = []

    async def fake_create_pool(**_kwargs):
        return "fake-pool"

    async def fake_get_played_candidates(pool, uid, *, window_seconds, limit):
        seen_calls.append((pool, uid, window_seconds, limit))
        return [_candidate()]

    # The selector routes lookups through its own dedicated pool (never the
    # shared `guardian_playback_ledger.get_pool()` singleton, which is bound
    # to the app's main event loop) — see `select_playback_ledger_candidates`.
    monkeypatch.setattr(scanner, "_candidate_pool", None)
    monkeypatch.setattr(scanner, "create_dedicated_ella_postgres_pool", fake_create_pool)
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

    async def failing_create_pool(**_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(scanner, "_candidate_pool", None)
    monkeypatch.setattr(scanner, "create_dedicated_ella_postgres_pool", failing_create_pool)

    assert select_playback_ledger_candidates("uid-1") == []


class _FakeWebhookResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.headers = {}


def _send_to_scanner_with_mocks(monkeypatch, *, segments, candidates, classification=None, classify_fn=None):
    """Drive the real `send_to_scanner` with only the two external
    boundaries mocked: the ledger candidate lookup (already covered by its
    own tests above) and the classifier call itself. Everything in between
    — the echo/mixed/fail-open branching in `send_to_scanner` — runs for
    real."""
    import utils.ella.scanner as scanner

    posts = []

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        return _FakeWebhookResponse(200)

    monkeypatch.setattr(scanner, "_log_trace_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)
    monkeypatch.setattr(scanner, "select_playback_ledger_candidates", lambda uid, **kwargs: candidates)
    monkeypatch.setattr(
        scanner,
        "_classify_playback_source_for_dispatch",
        classify_fn or (lambda transcript, cands, *, uid: classification),
    )

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-1",
        segments,
        guardian_mode="active_support",
    )
    return status, posts


def test_confirmed_ella_playback_echo_is_not_dispatched(monkeypatch):
    """A confirmed pure-echo classification must never reach the scanner
    webhook: dispatch is the only path that could create a Guardian
    queue row from this window, so skipping it is what makes a confirmed
    echo unable to create one."""
    status, posts = _send_to_scanner_with_mocks(
        monkeypatch,
        segments=[{"text": "Hey Ella, I heard my name.", "speaker": "SPEAKER_1"}],
        candidates=[_candidate().to_classifier_candidate()],
        classification=_echo_classification(source="ella_playback"),
    )

    assert status is None
    assert posts == []


def test_mixed_confirmed_echo_dispatches_only_the_validated_live_span(monkeypatch):
    segments = [
        {"text": "Hey Ella, I heard my name.", "speaker": "SPEAKER_1"},
        {"text": "what did you just say about dinner", "speaker": "SPEAKER_1"},
    ]
    status, posts = _send_to_scanner_with_mocks(
        monkeypatch,
        segments=segments,
        candidates=[_candidate().to_classifier_candidate()],
        classification=_echo_classification(
            source="mixed",
            contains_additional_live_speech=True,
            live_speech_spans=["what did you just say about dinner"],
        ),
    )

    assert status == 200
    assert len(posts) == 1
    assert [s["text"] for s in posts[0]["segments"]] == ["what did you just say about dinner"]


def test_classifier_fail_open_dispatches_original_window_unchanged(monkeypatch):
    """Timeout, provider error, or any other fail-open result must never
    suppress or alter the window — the untouched transcript continues."""
    segments = [{"text": "Hey Ella, did you catch that?", "speaker": "SPEAKER_1"}]
    status, posts = _send_to_scanner_with_mocks(
        monkeypatch,
        segments=segments,
        candidates=[_candidate().to_classifier_candidate()],
        classification=None,  # timeout / unexpected classification failure
    )

    assert status == 200
    assert len(posts) == 1
    assert [s["text"] for s in posts[0]["segments"]] == ["Hey Ella, did you catch that?"]


def test_non_echo_classification_dispatches_unchanged(monkeypatch):
    """A confident, schema-valid classification that simply isn't an echo
    (live_user/other_person/tv_media) must dispatch the window unchanged —
    only a confirmed echo ever changes what gets dispatched."""
    segments = [{"text": "Hey Ella, remind me to call my daughter.", "speaker": "SPEAKER_1"}]
    status, posts = _send_to_scanner_with_mocks(
        monkeypatch,
        segments=segments,
        candidates=[_candidate().to_classifier_candidate()],
        classification=_echo_classification(
            is_ella_playback=False,
            source="live_user",
            matched_playback_ids=[],
            fail_open=False,
        ),
    )

    assert status == 200
    assert len(posts) == 1
    assert [s["text"] for s in posts[0]["segments"]] == ["Hey Ella, remind me to call my daughter."]


def test_no_playback_candidates_never_calls_the_classifier(monkeypatch):
    """No candidates means nothing to compare against — the classifier must
    not even be invoked, matching `classify_playback_source`'s own
    REASON_NO_CANDIDATES fail-open, just without the wasted call."""
    calls = []

    def spy_classify(*args, **kwargs):
        calls.append((args, kwargs))
        return _echo_classification()

    status, posts = _send_to_scanner_with_mocks(
        monkeypatch,
        segments=[{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
        candidates=[],
        classify_fn=spy_classify,
    )

    assert status == 200
    assert len(posts) == 1
    assert calls == []


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
    # Content-free only: this metadata is persisted into `guardian_queue`,
    # so it must never carry transcript text — only ids/counts/statuses.
    assert payload["metadata"]["segments_summary"] == {
        "segment_count": 1,
        "speakers": ["SPEAKER_1"],
        "text_lengths": [len("Hey Ella, are cats clean animals?")],
    }
    assert "Hey Ella" not in json.dumps(payload)


def test_wake_ack_payload_ignores_non_wake_ambient_text():
    segments = [{"speaker": "SPEAKER_1", "text": "The cats on the video are clean animals."}]

    assert _build_wake_ack_payload("uid-1", "conv-1", "trace-1", segments) is None
