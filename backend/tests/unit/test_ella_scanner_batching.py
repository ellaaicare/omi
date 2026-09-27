import pytest

from utils.ella import scanner


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


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
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0]["scanner_batch"]["flush_reason"] == "immediate_wake"
    assert posts[0]["scanner_batch"]["rate_limit_status"] == "bypassed_for_immediate"


def test_emergency_bypasses_ambient_batching(monkeypatch):
    posts = []

    def fake_post(url, json, timeout, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(
                200,
                payload={
                    "ok": True,
                    "trace_id": "conversation-1",
                    "decision": "notify_now",
                    "delivery_plan": [
                        {
                            "target": "user",
                            "channel": "guardian_audio",
                            "priority": "urgent",
                            "untrusted": "drop-me",
                        }
                    ],
                },
            )
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 100)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-1",
        [{"text": "I have chest pain and cannot breathe", "speaker": "SPEAKER_1"}],
    )

    assert status == 200
    assert len(posts) == 2
    policy_url, policy_payload, policy_kwargs = posts[0]
    assert policy_url == scanner.ESCALATION_EVALUATE_URL
    assert policy_payload["event_type"] == "emergency"
    assert policy_payload["severity"] == "critical"
    assert policy_payload["evidence"]["scanner_category"] == "credible_emergency"
    assert policy_payload["evidence"]["deterministic_reason"] == "chest_pain"
    assert "chest pain" not in str(policy_payload).lower()
    assert policy_kwargs["headers"]["X-Escalation-Key"] == "configured-escalation-key"
    scanner_payload = posts[1][1]
    assert scanner_payload["scanner_batch"]["flush_reason"] == "immediate_emergency"
    assert scanner_payload["deterministic_emergency"]["routed"] is True
    assert scanner_payload["deterministic_emergency"]["status"] == "planned"
    assert scanner_payload["deterministic_emergency"]["plan"]["decision"] == "notify_now"
    assert scanner_payload["deterministic_emergency"]["plan"]["delivery_plan"] == [
        {"target": "user", "channel": "guardian_audio", "priority": "urgent"}
    ]


def test_credible_emergency_dry_run_reaches_policy_without_user_content(monkeypatch):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(
                200,
                payload={
                    "ok": True,
                    "trace_id": "conversation-dry-emergency",
                    "decision": "log_only",
                    "delivery_plan": [],
                },
            )
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-dry-emergency",
        [{"text": "Someone call 911 right now", "speaker": "SPEAKER_1"}],
        dry_run=True,
    )

    assert status == 200
    policy_payload = posts[0][1]
    assert policy_payload["evidence"]["dry_run"] is True
    assert policy_payload["evidence"]["traffic_class"] == "dry_run"
    assert "911" not in str(policy_payload)
    scanner_payload = posts[1][1]
    assert scanner_payload["dry_run"] is True
    assert scanner_payload["deterministic_emergency"]["dry_run"] is True


@pytest.mark.parametrize(
    "text",
    [
        "Ella, help me find my glasses",
        "I fell in love with that song",
        "I have fallen behind on email",
        "Help me open settings",
        "I need help setting up my phone",
    ],
)
def test_ambiguous_routine_language_stays_on_classifier_path(monkeypatch, text):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-routine-help",
        [{"text": text, "speaker": "SPEAKER_1"}],
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0][0] == scanner.ELLA_CONFIG.scanner_url
    assert "deterministic_emergency" not in posts[0][1]


def test_synthetic_wake_traffic_never_enqueues_audible_ack(monkeypatch):
    posts = []
    acks = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        return _FakeResponse(200)

    monkeypatch.setattr(scanner, "_log_trace_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: acks.append((args, kwargs)))
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-synthetic-wake",
        [{"text": "Hey Ella, call 911", "speaker": "SPEAKER_1"}],
        traffic_class="synthetic",
    )

    assert status == 200
    assert acks == []
    assert posts[-1][1]["traffic_class"] == "synthetic"
    assert posts[-1][1]["dry_run"] is True


def test_live_and_synthetic_ambient_batches_are_partitioned_before_buffering(monkeypatch):
    posts = []

    def fake_post(_url, json, **_kwargs):
        posts.append(json)
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner.requests, "post", fake_post)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_SECONDS", 999)
    monkeypatch.setattr(scanner, "SCANNER_AMBIENT_BATCH_WORDS", 4)

    live_first = scanner.send_to_scanner(
        "uid-1",
        "conversation-mixed-traffic",
        [{"text": "live one", "speaker": "SPEAKER_1"}],
    )
    synthetic = scanner.send_to_scanner(
        "uid-1",
        "conversation-mixed-traffic",
        [{"text": "synthetic two three", "speaker": "SPEAKER_1"}],
        traffic_class="synthetic",
    )
    live_second = scanner.send_to_scanner(
        "uid-1",
        "conversation-mixed-traffic",
        [{"text": "live four", "speaker": "SPEAKER_1"}],
    )

    assert live_first is None
    assert synthetic is None
    assert live_second == 200
    assert len(posts) == 1
    assert posts[0]["traffic_class"] == "live"
    assert posts[0]["dry_run"] is False
    assert [segment["text"] for segment in posts[0]["segments"]] == ["live one", "live four"]


@pytest.mark.parametrize("text", ["Hey Ella, help me", "Ella, I need help now"])
def test_wake_prefixed_explicit_help_keeps_deterministic_policy_path(monkeypatch, text):
    posts = []

    def fake_post(url, json, **_kwargs):
        posts.append((url, json))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(
                200,
                payload={
                    "ok": True,
                    "trace_id": "conversation-wake-help",
                    "decision": "log_only",
                    "delivery_plan": [],
                },
            )
        return _FakeResponse(503)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-wake-help",
        [{"text": text, "speaker": "SPEAKER_1"}],
    )

    assert status == 503
    assert posts[0][0] == scanner.ESCALATION_EVALUATE_URL
    assert posts[1][1]["deterministic_emergency"]["reason"] == "explicit_help"
    assert posts[1][1]["deterministic_emergency"]["status"] == "planned"


@pytest.mark.parametrize(
    "policy_response",
    [
        {
            "ok": True,
            "trace_id": "conversation-invalid-plan",
            "decision": "execute_arbitrary_action",
            "delivery_plan": [],
        },
        {
            "ok": True,
            "trace_id": "conversation-invalid-plan",
            "decision": "notify_now",
            "delivery_plan": [{"target": "user", "channel": "webhook", "priority": "critical"}],
        },
    ],
)
def test_unallowlisted_policy_plan_is_not_carried_or_logged(monkeypatch, policy_response):
    posts = []
    trace_events = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(200, payload=policy_response)
        return _FakeResponse(200)

    monkeypatch.setattr(scanner, "_log_trace_event", lambda *args, **kwargs: trace_events.append((args, kwargs)))
    monkeypatch.setattr(scanner, "_enqueue_wake_ack", lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-invalid-plan",
        [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
    )

    assert status == 200
    result = posts[-1][1]["deterministic_emergency"]
    assert result["routed"] is False
    assert result["status"] == "policy_invalid_response"
    assert "plan" not in result
    assert policy_response["decision"] not in str(trace_events)


def test_emergency_phrase_is_never_joined_across_speaker_segments(monkeypatch):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-speaker-boundary",
        [
            {"text": "I cannot", "speaker": "SPEAKER_1"},
            {"text": "breathe", "speaker": "SPEAKER_2"},
        ],
    )

    assert status == 200
    assert len(posts) == 1
    assert posts[0][0] == scanner.ELLA_CONFIG.scanner_url
    assert "deterministic_emergency" not in posts[0][1]


def test_emergency_phrase_is_joined_across_contiguous_same_speaker_segments(monkeypatch):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(
                200,
                payload={
                    "ok": True,
                    "trace_id": "conversation-same-speaker",
                    "decision": "log_only",
                    "delivery_plan": [],
                },
            )
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-same-speaker",
        [
            {"text": "I cannot", "speaker": "SPEAKER_1"},
            {"text": "breathe", "speaker": "SPEAKER_1"},
        ],
    )

    assert status == 200
    assert posts[0][0] == scanner.ESCALATION_EVALUATE_URL
    assert posts[-1][1]["deterministic_emergency"]["reason"] == "breathing"


def test_policy_plan_accepts_shared_boundary_and_rejects_one_step_over():
    trace_id = "conversation-policy-boundary"
    bounded_steps = [
        {"target": "user", "channel": "email", "priority": "critical"},
        *[
            {
                "target": "emergency_caregiver",
                "caregiver_id": f"caregiver-{index}",
                "channel": "email",
                "priority": "critical",
            }
            for index in range(scanner.MAX_DELIVERY_PLAN_STEPS - 1)
        ],
    ]
    response = {
        "ok": True,
        "trace_id": trace_id,
        "decision": "notify_now",
        "delivery_plan": bounded_steps,
    }

    assert scanner._validated_policy_plan(response, trace_id=trace_id) is not None
    response["delivery_plan"] = [
        *bounded_steps,
        {
            "target": "emergency_caregiver",
            "caregiver_id": "caregiver-overflow",
            "channel": "email",
            "priority": "critical",
        },
    ]
    assert scanner._validated_policy_plan(response, trace_id=trace_id) is None


def test_policy_failure_does_not_block_scanner_and_exposes_only_error_class(monkeypatch):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            raise scanner.requests.Timeout("private transport detail")
        return _FakeResponse(200)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-policy-timeout",
        [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
    )

    assert status == 200
    assert len(posts) == 2
    scanner_payload = posts[1][1]
    assert scanner_payload["deterministic_emergency"]["routed"] is False
    assert scanner_payload["deterministic_emergency"]["status"] == "policy_timeout"
    assert "private transport detail" not in str(scanner_payload)


def test_classifier_failure_cannot_prevent_deterministic_emergency_policy_plan(monkeypatch):
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json, kwargs))
        if url == scanner.ESCALATION_EVALUATE_URL:
            return _FakeResponse(
                200,
                payload={
                    "ok": True,
                    "trace_id": "conversation-classifier-outage",
                    "decision": "notify_now",
                    "delivery_plan": [{"target": "user", "channel": "email", "priority": "critical"}],
                },
            )
        return _FakeResponse(503)

    _disable_trace(monkeypatch)
    monkeypatch.setattr(scanner.ELLA_CONFIG, "scanner_enabled", True)
    monkeypatch.setattr(scanner, "ESCALATION_WEBHOOK_KEY", "configured-escalation-key")
    monkeypatch.setattr(scanner.requests, "post", fake_post)

    status = scanner.send_to_scanner(
        "uid-1",
        "conversation-classifier-outage",
        [{"text": "I fell and cannot get up", "speaker": "SPEAKER_1"}],
    )

    assert status == 503
    assert posts[0][0] == scanner.ESCALATION_EVALUATE_URL
    assert posts[1][0] == scanner.ELLA_CONFIG.scanner_url
    assert posts[1][1]["deterministic_emergency"]["routed"] is True
    assert posts[1][1]["deterministic_emergency"]["plan"]["decision"] == "notify_now"


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
    )
    second = scanner.send_to_scanner(
        "uid-1",
        "conversation-ambient",
        [{"text": "table order ready", "speaker": "SPEAKER_1"}],
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
    )
    deferred = scanner.send_to_scanner(
        "uid-1",
        "conversation-rate-limit",
        [{"text": "more ambient", "speaker": "SPEAKER_1"}],
    )
    wake = scanner.send_to_scanner(
        "uid-1",
        "conversation-rate-limit",
        [{"text": "Hey Ella, are you there?", "speaker": "SPEAKER_1"}],
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
    )

    assert status == 200
    assert posts[0]["segments"][0]["stt_source"] == "soniox"
    assert posts[0]["segments"][0]["is_user"] is True
    assert posts[0]["segments"][0]["person_id"] == "person-1"
    assert posts[0]["segments"][0]["speech_profile_processed"] is True
    assert posts[0]["latency"]["first_audio_frame_at"] == "2026-05-09T18:00:00+00:00"
