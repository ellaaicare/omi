import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
from utils.ella import scanner
from utils.conversations import spoken_diagnostic


class _FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _disable_trace(monkeypatch):
    monkeypatch.setattr(scanner, "_log_trace_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _scanner_webhook_authority(monkeypatch):
    monkeypatch.setattr(scanner, "SCANNER_WEBHOOK_KEY", "configured-scanner-webhook-key")


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


def test_server_claim_marker_is_once_only_and_failed_claim_never_dispatches(monkeypatch):
    posts = []
    claims = [
        {"claim_id": "a" * 64, "queue_id": "diagnostic_a", "response_version": spoken_diagnostic.RESPONSE_VERSION},
        None,
    ]
    captured_origin = []
    _disable_trace(monkeypatch)
    monkeypatch.setenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setattr(spoken_diagnostic, "is_diagnostic_window", lambda *_args: True)
    monkeypatch.setattr(
        spoken_diagnostic,
        "reserve_for_segments",
        lambda *_args, **kwargs: (captured_origin.append(kwargs), claims.pop(0))[1],
    )
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 1)
    monkeypatch.setattr(scanner, "select_playback_ledger_candidates", lambda *_args: [])
    monkeypatch.setattr(
        scanner, "_post_scanner_webhook", lambda _url, json, **_kwargs: (posts.append(json), _FakeResponse())[1]
    )
    segments = [{"text": "silver lantern check in", "speaker": "SPEAKER_1"}]
    assert (
        scanner.send_to_scanner(
            "uid-1",
            "conversation-1",
            segments,
            guardian_mode="active_support",
            origin_generation="generation-a",
            origin_owner_token="owner-a",
        )
        == 200
    )
    assert posts[0]["spoken_diagnostic"]["claim_id"] == "a" * 64
    scanner.send_to_scanner(
        "uid-1",
        "conversation-1",
        segments,
        guardian_mode="active_support",
        origin_generation="generation-a",
        origin_owner_token="owner-a",
    )
    assert len(posts) == 1
    assert claims == []
    assert captured_origin == [
        {"origin_generation": "generation-a", "origin_owner_token": "owner-a"},
        {"origin_generation": "generation-a", "origin_owner_token": "owner-a"},
    ]


def test_guardian_trace_service_caller_fails_closed_without_configured_key(monkeypatch):
    posts = []
    monkeypatch.setattr(scanner, "GUARDIAN_WEBHOOK_KEY", "")
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: posts.append((args, kwargs)))

    scanner._log_trace_event("trace-a", "uid-a", "scanner_dispatched", "success")

    assert posts == []


def test_scanner_webhook_ignores_proxy_environment_and_rejects_redirect(monkeypatch):
    target_requests = []
    proxy_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            target_requests.append(
                {
                    "path": self.path,
                    "authority": self.headers.get(scanner.SCANNER_WEBHOOK_KEY_HEADER),
                    "body": body,
                }
            )
            self.send_response(307)
            self.send_header("Location", self.server.redirect_url)
            self.end_headers()

        def log_message(self, *_args):
            return None

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            proxy_requests.append(self.path)
            self.send_response(502)
            self.end_headers()

        def log_message(self, *_args):
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    target.redirect_url = f"http://127.0.0.1:{target.server_port}/redirect-target"
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    target_thread.start()
    proxy_thread.start()

    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    payload = {"segments": [{"text": "private scanner payload"}]}
    try:
        with pytest.raises(scanner.requests.RequestException, match="redirect rejected"):
            scanner._post_scanner_webhook(
                f"http://127.0.0.1:{target.server_port}/scanner",
                json=payload,
                headers={scanner.SCANNER_WEBHOOK_KEY_HEADER: "scoped-test-key"},
                timeout=2.0,
            )
    finally:
        target.shutdown()
        proxy.shutdown()
        target.server_close()
        proxy.server_close()
        target_thread.join(timeout=2.0)
        proxy_thread.join(timeout=2.0)

    assert proxy_requests == []
    assert len(target_requests) == 1
    assert target_requests[0]["path"] == "/scanner"
    assert target_requests[0]["authority"] == "scoped-test-key"
    assert json.loads(target_requests[0]["body"]) == payload


def test_wake_word_bypasses_ambient_batching(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: "configured-scanner-webhook-key"}
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)
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

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: "configured-scanner-webhook-key"}
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)
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

    intruder = scanner.send_to_scanner(
        "uid-1",
        "conversation-intruder",
        [{"text": "There is an intruder in my home", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )
    contextual = scanner.send_to_scanner(
        "uid-1",
        "conversation-split",
        [{"text": "breathe", "speaker": "SPEAKER_1"}],
        recent_segments=[{"text": "I cannot", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )
    fall = scanner.send_to_scanner(
        "uid-1",
        "conversation-fall",
        [{"text": "I've fallen and I can't get up", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )
    fall_injury = scanner.send_to_scanner(
        "uid-1",
        "conversation-fall-injury",
        [{"text": "I've fallen and I'm hurt", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert intruder == 200
    assert contextual == 200
    assert fall == 200
    assert fall_injury == 200
    assert posts[1]["scanner_batch"]["flush_reason"] == "immediate_credible_emergency"
    assert posts[2]["scanner_batch"]["flush_reason"] == "immediate_emergency"
    assert [segment["text"] for segment in posts[2]["segments"]] == ["I cannot", "breathe"]
    assert posts[3]["scanner_batch"]["flush_reason"] == "immediate_emergency"
    assert posts[4]["scanner_batch"]["flush_reason"] == "immediate_emergency"


def test_guardian_off_contextual_emergency_sends_only_authorized_suffix(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-context-boundary",
        [{"text": "breathe", "speaker": "SPEAKER_1"}],
        recent_segments=[
            {"text": "My bank PIN is 1234", "speaker": "SPEAKER_1"},
            {"text": "I cannot", "speaker": "SPEAKER_1"},
        ],
        guardian_mode="off",
    )

    assert status == 200
    assert len(posts) == 1
    assert [segment["text"] for segment in posts[0]["segments"]] == ["I cannot", "breathe"]
    assert [segment["text"] for segment in posts[0]["recent_segments"]] == ["I cannot"]


def test_guardian_off_direct_emergency_omits_unmatched_retained_history(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-direct-boundary",
        [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
        recent_segments=[{"text": "My bank PIN is 1234", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert status == 200
    assert len(posts) == 1
    assert [segment["text"] for segment in posts[0]["segments"]] == ["I cannot breathe"]
    assert posts[0]["recent_segments"] == []


def test_guardian_off_direct_emergency_sends_only_matching_current_speaker_group(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-current-boundary",
        [
            {"text": "My bank PIN is 1234", "speaker": "SPEAKER_1"},
            {"text": "I cannot breathe", "speaker": "SPEAKER_2"},
        ],
        guardian_mode="off",
    )

    assert status == 200
    assert len(posts) == 1
    assert [segment["text"] for segment in posts[0]["segments"]] == ["I cannot breathe"]


def test_guardian_enabled_context_does_not_rewrite_active_segments(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-active-boundary",
        [{"text": "breathe", "speaker": "SPEAKER_1"}],
        recent_segments=[{"text": "I cannot", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status == 200
    assert len(posts) == 1
    assert [segment["text"] for segment in posts[0]["segments"]] == ["breathe"]
    assert [segment["text"] for segment in posts[0]["recent_segments"]] == ["I cannot"]


def test_ambient_chunks_batch_until_word_threshold(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: "configured-scanner-webhook-key"}
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)
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

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: "configured-scanner-webhook-key"}
        if len(posts) == 1:
            return _FakeResponse(429, {"Retry-After": "30", "x-ratelimit-remaining-requests": "0"})
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)
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

    def fake_post(_url, json, headers, timeout):
        posts.append(json)
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: "configured-scanner-webhook-key"}
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

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
        typesafe_egress_authorized=True,
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
    assert posts[0]["typesafe_egress_authorized"] is True


@pytest.mark.parametrize("mode", [None, "", "OFF", "none", "disabled", "null", "guardian_off"])
def test_scanner_suppresses_all_off_equivalent_modes(monkeypatch, mode):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *args, **kwargs: posts.append((args, kwargs)))

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

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    for index, text in enumerate(
        (
            "I can't breathe, please call 911 now",
            "I am having a heart attack, call an ambulance right now",
        )
    ):
        status = scanner.send_to_scanner(
            "uid-1",
            f"conversation-emergency-off-{index}",
            [{"text": text, "speaker": "SPEAKER_1"}],
            guardian_mode="off",
        )

        assert status == 200
        assert posts[index]["guardian_mode"] == "off"
        assert posts[index]["guardian_mode_enabled"] is False
        assert posts[index]["emergency_only_dispatch"] is True

    assert len(posts) == 2
    assert all(post["typesafe_egress_authorized"] is False for post in posts)


def test_scanner_fails_closed_when_mode_authority_is_unavailable(monkeypatch):
    posts = []
    trace_events = []
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *args, **kwargs: posts.append((args, kwargs)))
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


def test_scanner_fails_before_webhook_egress_without_authority(monkeypatch):
    posts = []
    wake_acks = []
    candidate_lookups = []
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: wake_acks.append((args, kwargs)))
    monkeypatch.setattr(
        scanner,
        "select_playback_ledger_candidates",
        lambda *args, **kwargs: candidate_lookups.append((args, kwargs)) or [],
    )
    monkeypatch.setattr(
        scanner,
        "_apply_ambient_batching",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing scanner authority must fail before batching")
        ),
    )
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "SCANNER_WEBHOOK_KEY", "")
    monkeypatch.setattr(scanner, "GUARDIAN_WEBHOOK_KEY", "configured-guardian-trace-key")
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *args, **kwargs: posts.append((args, kwargs)))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-authority",
        [{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
    )

    assert status is None
    assert wake_acks == []
    assert candidate_lookups == []
    assert posts == []


@pytest.mark.parametrize(
    "text",
    [
        "Help me find my glasses",
        "Urgent, remind me to call tomorrow",
        "That movie was about a fire",
        "I fell in love with that song",
        "Help me open settings",
        "The documentary was about a seizure",
        "We watched a movie called The Intruder",
        "The article discussed chest pain",
        "The song was called Bleeding Out",
    ],
)
def test_scanner_off_mode_does_not_leak_ambiguous_routine_speech(monkeypatch, text):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *args, **kwargs: posts.append((args, kwargs)))

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-routine-off",
        [{"text": text, "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert status is None
    assert posts == []


def test_credible_emergency_bypass_requires_current_context_for_sensitive_terms():
    assert scanner.credible_emergency_reason("Please call 911") == "emergency_services"
    assert scanner.credible_emergency_reason("Someone please call 911") == "emergency_services"
    assert scanner.credible_emergency_reason("Call an ambulance") == "emergency_services"
    assert scanner.credible_emergency_reason("Please call an ambulance") == "emergency_services"
    assert scanner.credible_emergency_reason("Hey Ella, call 911") == "emergency_services"
    assert scanner.credible_emergency_reason("Hey Ella, I can't breathe") == "breathing"
    assert scanner.credible_emergency_reason("I am having chest pain") == "chest_pain"
    assert scanner.credible_emergency_reason("I'm having chest pain") == "chest_pain"
    assert scanner.credible_emergency_reason("I can’t breathe") == "breathing"
    assert scanner.credible_emergency_reason("She can't breathe") == "breathing"
    assert scanner.credible_emergency_reason("My husband cannot breathe") == "breathing"
    assert scanner.credible_emergency_reason("We can't breathe") == "breathing"
    assert scanner.credible_emergency_reason("You cannot breathe") == "breathing"
    assert scanner.credible_emergency_reason("She cannot breathe, please help me") == "breathing"
    assert scanner.credible_emergency_reason("I can't breathe, call 911") == "breathing"
    assert scanner.credible_emergency_reason("I can't breathe, please call 911 now") == "breathing"
    assert scanner.credible_emergency_reason("I am having a heart attack, call an ambulance") == "heart_attack"
    assert (
        scanner.credible_emergency_reason("I am having a heart attack, call an ambulance right now") == "heart_attack"
    )
    assert scanner.credible_emergency_reason("Dad is choking, please call 911") == "choking"
    assert scanner.credible_emergency_reason("She is having a seizure") == "seizure"
    assert scanner.credible_emergency_reason("She's having a seizure") == "seizure"
    assert scanner.credible_emergency_reason("My husband is having a seizure") == "seizure"
    assert scanner.credible_emergency_reason("Dad is bleeding out") == "severe_bleeding"
    assert scanner.credible_emergency_reason("Dad's bleeding out") == "severe_bleeding"
    assert scanner.credible_emergency_reason("They're bleeding out") == "severe_bleeding"
    assert scanner.credible_emergency_reason("I am bleeding out") == "severe_bleeding"
    assert scanner.credible_emergency_reason("There is an intruder in my home") == "intruder"
    assert scanner.credible_emergency_reason("I am having a heart attack") == "heart_attack"
    assert scanner.credible_emergency_reason("My wife is having a stroke") == "stroke"
    assert scanner.credible_emergency_reason("Dad's choking") == "choking"
    assert scanner.credible_emergency_reason("My friend is overdosing") == "overdose"
    assert scanner.credible_emergency_reason("I want to kill myself") == "self_harm"
    assert scanner.credible_emergency_reason("I am suicidal") == "self_harm"
    assert scanner.credible_emergency_reason("I'm suicidal") == "self_harm"
    assert scanner.credible_emergency_reason("I’m going to hurt myself") == "self_harm"
    assert scanner.credible_emergency_reason("I've fallen and I can't get up") == "fall"
    assert scanner.credible_emergency_reason("I’ve fallen and I can’t get up") == "fall"
    assert scanner.credible_emergency_reason("I've fallen and I'm hurt") == "fall"
    assert scanner.credible_emergency_reason("I’ve fallen and I’m unable to move") == "fall"


@pytest.mark.parametrize(
    "text",
    [
        "The documentary says she is having a seizure",
        "The article quotes I am having chest pain",
        "She said he is bleeding out",
        "Yesterday I was having chest pain",
        "The article says I am having a heart attack",
        "The movie says she is choking",
        "Yesterday I had a stroke",
        "She said I want to kill myself",
    ],
)
def test_credible_emergency_bypass_rejects_reported_quoted_and_historical_speech(text):
    assert scanner.credible_emergency_reason(text) is None


def test_credible_emergency_bypass_rejects_negative_or_absent_subjects():
    for text in (
        "Nobody is having a seizure",
        "No one is having a seizure",
        "Nobody here is bleeding out",
        "No person is having chest pain",
        "None of them are having a seizure",
        "Not anybody here is bleeding out",
        "Neither of them is having chest pain",
    ):
        assert scanner.credible_emergency_reason(text) is None

    for text in (
        "My computer is having a stroke",
        "Server is choking",
        "ChatGPT is overdosing",
        "She said he cannot breathe",
        "The article says my husband can't breathe",
        "I am having a stroke of luck",
        "Dad is choking back tears",
        "I cannot breathe, she said",
    ):
        assert scanner.credible_emergency_reason(text) is None

    assert (
        scanner.credible_emergency_reason_for_segments(
            [
                {"text": "I cannot", "speaker": "SPEAKER_1"},
                {"text": "breathe", "speaker": "SPEAKER_1"},
            ]
        )
        == "breathing"
    )
    assert (
        scanner.credible_emergency_reason_for_segments(
            [
                {"text": "I cannot", "speaker": "SPEAKER_1"},
                {"text": "breathe", "speaker": "SPEAKER_2"},
            ]
        )
        is None
    )

    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said", "speaker": "SPEAKER_1"}],
        [{"text": "I want to kill myself", "speaker": "SPEAKER_1"}],
    ) == (None, False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said.", "speaker": "SPEAKER_1"}],
        [{"text": "I want to kill myself", "speaker": "SPEAKER_1"}],
    ) == (None, False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said.", "speaker": "SPEAKER_1"}],
        [{"text": "I cannot breathe", "speaker": "SPEAKER_2"}],
    ) == ("breathing", False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "The article says that", "speaker": "SPEAKER_1"}],
        [{"text": "I am having a heart attack", "speaker": "SPEAKER_1"}],
    ) == (None, False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "We finished lunch", "speaker": "SPEAKER_1"}],
        [{"text": "I am having a heart attack", "speaker": "SPEAKER_1"}],
    ) == ("heart_attack", False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "I heard the doorbell", "speaker": "SPEAKER_1"}],
        [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
    ) == ("breathing", False)
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "Earlier I ate lunch", "speaker": "SPEAKER_1"}],
        [{"text": "Please call 911", "speaker": "SPEAKER_1"}],
    ) == ("emergency_services", False)
    assert scanner.credible_emergency_reason_with_context(
        [
            {"text": "We were discussing dinner", "speaker": "SPEAKER_1"},
            {"text": "I cannot", "speaker": "SPEAKER_1"},
        ],
        [{"text": "breathe", "speaker": "SPEAKER_1"}],
    ) == ("breathing", True)
    assert scanner.credible_emergency_reason_with_context(
        [
            {"text": "She said", "speaker": "SPEAKER_1"},
            {"text": "I cannot", "speaker": "SPEAKER_1"},
        ],
        [{"text": "breathe", "speaker": "SPEAKER_1"}],
    ) == (None, False)


def test_credible_emergency_match_with_context_scans_all_current_speaker_groups():
    # Disqualified same-speaker group first, then an independent qualifying group:
    # the second group must still be found instead of short-circuiting on the first.
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said.", "speaker": "SPEAKER_1"}],
        [
            {"text": "I cannot breathe", "speaker": "SPEAKER_1"},
            {"text": "I cannot breathe", "speaker": "SPEAKER_2"},
        ],
    ) == ("breathing", False)

    # All current groups disqualified by their own speaker's retained context => no match.
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said.", "speaker": "SPEAKER_1"}],
        [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
    ) == (None, False)

    # Qualifying group first: behavior is unchanged from before the fix.
    assert scanner.credible_emergency_reason_with_context(
        [{"text": "She said.", "speaker": "SPEAKER_1"}],
        [
            {"text": "I cannot breathe", "speaker": "SPEAKER_2"},
            {"text": "I cannot breathe", "speaker": "SPEAKER_1"},
        ],
    ) == ("breathing", False)


def test_scanner_off_mode_dispatches_independent_speaker_after_reported_speech_group(monkeypatch):
    posts = []

    def fake_post(_url, json, headers, timeout):
        assert headers == {scanner.SCANNER_WEBHOOK_KEY_HEADER: scanner.SCANNER_WEBHOOK_KEY}
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-multigroup-boundary",
        [
            {"text": "I cannot breathe", "speaker": "SPEAKER_1"},
            {"text": "I cannot breathe", "speaker": "SPEAKER_2"},
        ],
        recent_segments=[{"text": "She said.", "speaker": "SPEAKER_1"}],
        guardian_mode="off",
    )

    assert status == 200
    assert len(posts) == 1
    assert [segment["speaker"] for segment in posts[0]["segments"]] == ["SPEAKER_2"]
    assert [segment["text"] for segment in posts[0]["segments"]] == ["I cannot breathe"]


def test_scanner_off_mode_does_not_join_emergency_phrase_across_segments(monkeypatch):
    posts = []
    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *args, **kwargs: posts.append((args, kwargs)))

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


def test_scanner_dispatch_success_trace_and_logs_never_contain_transcript_text(monkeypatch, capsys):
    """P0 privacy regression: the success trace event and stdout log for a
    scanner dispatch must never carry transcript text — only content-free
    ids/counts/statuses (see `scanner_payload_metadata_summary`)."""
    trace_events = []
    monkeypatch.setattr(scanner, "_log_trace_event", lambda **kwargs: trace_events.append(kwargs))
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *a, **k: None)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", lambda *a, **k: _FakeResponse(200))
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    secret_phrase = "UNMISTAKABLE_SECRET_TRANSCRIPT_MARKER_SUCCESS"
    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-privacy-success",
        [{"text": f"Hey Ella, {secret_phrase}", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status == 200
    captured = capsys.readouterr()
    assert secret_phrase not in captured.out
    assert secret_phrase not in captured.err
    assert trace_events, "expected at least one trace event"
    for event in trace_events:
        assert secret_phrase not in json.dumps(event)


def test_scanner_dispatch_timeout_trace_and_logs_never_contain_transcript_text(monkeypatch, capsys):
    trace_events = []

    def raise_timeout(*_args, **_kwargs):
        raise scanner.requests.Timeout("boom")

    monkeypatch.setattr(scanner, "_log_trace_event", lambda **kwargs: trace_events.append(kwargs))
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *a, **k: None)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", raise_timeout)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    secret_phrase = "UNMISTAKABLE_SECRET_TRANSCRIPT_MARKER_TIMEOUT"
    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-privacy-timeout",
        [{"text": f"Hey Ella, {secret_phrase}", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status is None
    captured = capsys.readouterr()
    assert secret_phrase not in captured.out
    assert secret_phrase not in captured.err
    assert trace_events, "expected at least one trace event"
    for event in trace_events:
        assert secret_phrase not in json.dumps(event)


def test_scanner_dispatch_error_trace_and_logs_never_contain_transcript_text(monkeypatch, capsys):
    trace_events = []

    def raise_error(*_args, **_kwargs):
        raise RuntimeError("webhook exploded")

    monkeypatch.setattr(scanner, "_log_trace_event", lambda **kwargs: trace_events.append(kwargs))
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *a, **k: None)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "_post_scanner_webhook", raise_error)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    secret_phrase = "UNMISTAKABLE_SECRET_TRANSCRIPT_MARKER_ERROR"
    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-privacy-error",
        [{"text": f"Hey Ella, {secret_phrase}", "speaker": "SPEAKER_1"}],
        guardian_mode="active_support",
    )

    assert status is None
    captured = capsys.readouterr()
    assert secret_phrase not in captured.out
    assert secret_phrase not in captured.err
    assert trace_events, "expected at least one trace event"
    for event in trace_events:
        assert secret_phrase not in json.dumps(event)


def test_wake_detected_trace_and_persisted_queue_metadata_never_contain_transcript_text(monkeypatch, capsys):
    """`_enqueue_wake_ack` writes both a trace event and (via
    `_build_wake_ack_payload`) metadata persisted into `guardian_queue` —
    neither may ever carry the transcript."""
    trace_events = []
    monkeypatch.setattr(scanner, "_log_trace_event", lambda **kwargs: trace_events.append(kwargs))
    monkeypatch.setattr(scanner, "GUARDIAN_WEBHOOK_KEY", "")  # skip the fire-and-forget POST/DB write

    secret_phrase = "UNMISTAKABLE_SECRET_TRANSCRIPT_MARKER_WAKE"
    payload = scanner._build_wake_ack_payload(
        "uid-1", "conv-1", "trace-1", [{"speaker": "SPEAKER_1", "text": f"Hey Ella, {secret_phrase}"}]
    )
    scanner._enqueue_wake_ack(
        "uid-1", "conv-1", "trace-1", [{"speaker": "SPEAKER_1", "text": f"Hey Ella, {secret_phrase}"}]
    )

    assert secret_phrase not in json.dumps(payload)
    captured = capsys.readouterr()
    assert secret_phrase not in captured.out
    for event in trace_events:
        assert secret_phrase not in json.dumps(event)
