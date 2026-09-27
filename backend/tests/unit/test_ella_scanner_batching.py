import sys
import types

import pytest

from utils.ella import scanner


class _FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _disable_trace(monkeypatch):
    monkeypatch.setattr(scanner, "_log_trace_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: None)


def setup_function():
    scanner.reset_scanner_batch_state()


def teardown_function():
    scanner.reset_scanner_batch_state()


def test_guardian_trace_service_caller_uses_scoped_key(monkeypatch):
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return _FakeResponse(200)

    monkeypatch.setattr(scanner, "GUARDIAN_WEBHOOK_KEY", "configured-guardian-service-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    scanner._log_trace_event("trace-a", "uid-a", "scanner_dispatched", "success")

    assert len(posts) == 1
    assert posts[0][0] == scanner.GUARDIAN_TRACE_LOG_URL
    assert posts[0][1]["headers"] == {
        "X-Guardian-Key": "configured-guardian-service-key",
        "X-Ella-Subject-Uid": "uid-a",
    }
    assert posts[0][1]["timeout"] == scanner.GUARDIAN_TRACE_LOG_TIMEOUT_S


def test_guardian_trace_service_caller_fails_closed_without_configured_key(monkeypatch):
    posts = []
    monkeypatch.setattr(scanner, "GUARDIAN_WEBHOOK_KEY", "")
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))

    scanner._log_trace_event("trace-a", "uid-a", "scanner_dispatched", "success")

    assert posts == []


def test_wake_word_bypasses_ambient_batching(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-1",
        [{"text": "Hey Ella, did you catch that morning conversation?", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "immediate_wake"
    assert posts[0]["scanner_batch"]["rate_limit_status"] == "bypassed_for_immediate"


def test_emergency_bypasses_ambient_batching(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-1",
        [{"text": "I have chest pain and cannot breathe", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "immediate_emergency"


def test_ambient_chunks_batch_until_word_threshold(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_SECONDS", 999)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 5)

    first = scanner.send_to_scanner(
        "uid-1",
        "conversation-ambient",
        [{"text": "coffee shop", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )
    second = scanner.send_to_scanner(
        "uid-1",
        "conversation-ambient",
        [{"text": "table order ready", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert first is None
    assert second == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "word_threshold"
    assert posts[0]["scanner_batch"]["batch_size"] == 2
    assert [segment["text"] for segment in posts[0]["segments"]] == ["coffee shop", "table order ready"]


def test_rate_limit_defers_ambient_but_not_wake(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        if len(posts) == 1:
            return _FakeResponse(429, {"Retry-After": "30", "x-ratelimit-remaining-requests": "0"})
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_SECONDS", 999)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 1)

    first = scanner.send_to_scanner(
        "uid-1",
        "conversation-rate-limit",
        [{"text": "ambient", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )
    deferred = scanner.send_to_scanner(
        "uid-1",
        "conversation-rate-limit",
        [{"text": "more ambient", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )
    wake = scanner.send_to_scanner(
        "uid-1",
        "conversation-rate-limit",
        [{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert first == 429
    assert deferred is None
    assert wake == 200
    assert len(posts) == 2
    assert posts[1]["scanner_batch"]["flush_reason"] == "immediate_wake"


def test_rate_limit_status_parses_groq_duration_headers():
    response = _FakeResponse(
        429,
        {
            "retry-after": "1.5",
            "x-ratelimit-reset-requests": "1m2.5s",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-limit-requests": "30",
        },
    )

    status = scanner.rate_limit_status_from_response(response)

    assert status["limited"] is True
    assert status["retry_after_s"] == 1.5
    assert status["reset_requests_s"] == 62.5
    assert status["remaining_requests"] == "0"
    assert status["limit_requests"] == "30"


def test_scanner_payload_preserves_stt_identity_and_latency_metadata(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-identity",
        [
            {
                "text": "Hey Ella, what did you hear?",
                "speaker": "SPEAKER_0",
                "speaker_id": 0,
                "is_user": True,
                "person_id": "person-1",
                "speech_profile_processed": True,
                "stt_provider": "soniox",
            }
        ],
        latency_metadata={"first_audio_frame_at": "2026-05-09T18:00:00+00:00"},
        guardian_mode="active_support",
    )

    assert status == 200
    assert posts[0]["segments"][0]["stt_source"] == "soniox"
    assert posts[0]["segments"][0]["is_user"] is True
    assert posts[0]["segments"][0]["person_id"] == "person-1"
    assert posts[0]["segments"][0]["speech_profile_processed"] is True
    assert posts[0]["latency"]["first_audio_frame_at"] == "2026-05-09T18:00:00+00:00"
    assert posts[0]["guardian_mode"] == "active_support"
    assert posts[0]["guardian_mode_source"] == "users.guardian_mode"
    assert posts[0]["guardian_mode_enabled"] is True
    assert posts[0]["emergency_only_dispatch"] is False


@pytest.mark.parametrize("mode", [None, "", "OFF", "none", "disabled", "null", "guardian_off"])
def test_scanner_suppresses_all_off_equivalent_modes(monkeypatch, mode):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-off",
        [{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
        guardian_mode=mode,
    )

    assert status is None
    assert posts == []


def test_scanner_preserves_emergency_only_dispatch_when_guardian_is_off(monkeypatch):
    posts = []

    def fake_post(_url, json, timeout):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-emergency-off",
        [{"text": "I cannot breathe and need an ambulance", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0]["guardian_mode"] == "off"
    assert posts[0]["guardian_mode_enabled"] is False
    assert posts[0]["emergency_only_dispatch"] is True


def test_scanner_fails_closed_when_mode_authority_is_unavailable(monkeypatch):
    posts = []
    trace_events = []
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_log_trace_event", lambda **kwargs: trace_events.append(kwargs))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-unavailable",
        [{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
    )

    assert status is None
    assert posts == []
    assert trace_events[-1]["stage"] == "scanner_mode_authority"
    assert trace_events[-1]["status"] == "error"
    assert trace_events[-1]["metadata"] == {"reason": "guardian_mode_required"}


@pytest.mark.parametrize(
    "text",
    [
        "Help me find my glasses",
        "Urgent, remind me to call tomorrow",
        "That movie was about a fire",
        "I fell in love with that song",
        "Help me open settings",
    ],
)
def test_scanner_off_mode_does_not_leak_ambiguous_routine_speech(monkeypatch, text):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-routine-off",
        [{"text": text, "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert status is None
    assert posts == []


def test_scanner_off_mode_does_not_join_emergency_phrase_across_segments(monkeypatch):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-cross-speaker-off",
        [
            {"text": "I cannot", "speaker": "SPEAKER_1"},
            {"text": "breathe", "speaker": "SPEAKER_2"},
        ],
        guardian_mode="off",
    )

    assert status is None
    assert posts == []


@pytest.mark.parametrize("mode", [None, "", "OFF", "none", "disabled", "null", "guardian_off"])
def test_direct_wake_ack_skips_all_off_equivalent_modes(monkeypatch, mode):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

        def fetchone(self):
            return (mode,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=lambda **_kwargs: Connection()))

    result = scanner._insert_wake_ack_direct(
        "uid-1",
        "trace-1",
        {
            "id": "wake-1",
            "url": "https://example.invalid/wake.mp3",
            "metadata": {},
        },
    )

    assert result == {"method": "direct_db", "status": "skipped", "reason": "guardian_mode_off"}
