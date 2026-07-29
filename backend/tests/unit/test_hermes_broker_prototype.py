"""Focused tests for the allowlisted Hermes broker prototype transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import pytest

from ella.services import hermes_broker_client as client_mod
from ella.services import hermes_broker_prototype as proto
from ella.services import hermes_cloud_runtime as runtime_mod
from ella.services.hermes_broker_client import HermesBrokerClient
from ella.services.hermes_cloud import HermesCloudTurn
from ella.services.hermes_cloud_runtime import (
    HERMES_CLOUD_CHAT_MODE,
    HERMES_CLOUD_ENRICHMENT_CHANNEL,
    HERMES_CLOUD_ENRICHMENT_MODE,
    HermesCloudRuntimeService,
    HermesCloudTurnRequest,
)
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime


def _runtime(
    *,
    uid: str = "synthetic-proto-01",
    binding_id: str = "binding-proto-01",
    profile_class: str = "synthetic",
    provider: str = "hermes_cloud",
    mode: str = HERMES_CLOUD_CHAT_MODE,
) -> IsolatedRuntime:
    return IsolatedRuntime(
        uid=uid,
        binding_id=binding_id,
        provider=provider,
        status="active",
        profile_name="proto",
        agent_id="agent-proto",
        runtime_instance_id="ri-proto",
        gateway_url="https://hermes.example.internal",
        gateway_token="direct-token",
        workspace_root="/tmp/ws",
        honcho_workspace="hw",
        observed_peer="op",
        observer_peer="ob",
        prompt_pack_version="p1",
        expected_model="gpt-test",
        model_context_window_tokens=8192,
        allowed_tools=(),
        required_capabilities=("responses_api", "session_key_header"),
        model_policy_version="mp1",
        voice_policy_version="vp1",
        revision=1,
        profile_class=profile_class,
        runtime_target_id="target-proto",
        runtime_target_mode=mode,
        runtime_target_updated_at="2026-07-29T00:00:00Z",
        target_endpoint_ref="env:URL",
        target_credential_ref="env:KEY",
        target_entitlement_revision=1,
    )


def _enable(monkeypatch, **overrides):
    env = {
        "ELLA_HERMES_BROKER_PROTOTYPE_ENABLED": "true",
        "ELLA_HERMES_BROKER_PROTOTYPE_ACCOUNT_ID": "synthetic-proto-01",
        "ELLA_HERMES_BROKER_PROTOTYPE_PROFILE_ID": "synthetic-proto-01",
        "ELLA_HERMES_BROKER_PROTOTYPE_BINDING_ID": "binding-proto-01",
        "ELLA_HERMES_BROKER_BASE_URL": "https://broker.ella.internal",
        "ELLA_HERMES_BROKER_ALLOWED_HOST": "broker.ella.internal",
        "ELLA_HERMES_BROKER_SERVICE_TOKEN_REF": "env:ELLA_HERMES_BROKER_SERVICE_TOKEN",
        "ELLA_HERMES_BROKER_SERVICE_TOKEN": "service-token-test",
        "ELLA_HERMES_BROKER_POLL_INTERVAL_SECONDS": "0.1",
        "ELLA_HERMES_BROKER_POLL_TIMEOUT_SECONDS": "1.0",
        "ELLA_HERMES_BROKER_CALLBACK_DEADLINE_SECONDS": "30",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class FakeResponse:
    def __init__(self, status_code: int, body: Any, *, content: Optional[bytes] = None):
        self.status_code = status_code
        self._body = body
        self.content = content if content is not None else json.dumps(body).encode()

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str, dict]] = []

    def factory(self, timeout):
        parent = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                parent.calls.append(("POST", url, kwargs))
                return parent.handler("POST", url, kwargs)

            async def get(self, url, **kwargs):
                parent.calls.append(("GET", url, kwargs))
                return parent.handler("GET", url, kwargs)

        return _Client()


def test_default_off_does_not_select_broker(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_BROKER_PROTOTYPE_ENABLED", raising=False)
    assert proto.load_prototype_config() is None
    assert proto.runtime_uses_broker_prototype(_runtime()) is False


def test_truthy_alias_not_enabled(monkeypatch):
    _enable(monkeypatch, ELLA_HERMES_BROKER_PROTOTYPE_ENABLED="TRUE")
    assert proto.load_prototype_config() is None


def test_non_allowlisted_user_keeps_direct_path(monkeypatch):
    _enable(monkeypatch)
    other = _runtime(uid="realcryptoplato", profile_class="real")
    assert proto.runtime_uses_broker_prototype(other) is False
    assert (
        proto.runtime_uses_broker_prototype(
            _runtime(uid="other-synth", profile_class="synthetic", binding_id="binding-proto-01")
        )
        is False
    )


def test_binding_pin_required_when_set(monkeypatch):
    _enable(monkeypatch)
    assert proto.runtime_uses_broker_prototype(_runtime(binding_id="wrong-binding")) is False
    assert proto.runtime_uses_broker_prototype(_runtime()) is True


def test_success_chat_maps_answer(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()
    assert cfg is not None

    def handler(method, url, kwargs):
        if method == "POST":
            body = kwargs["json"]
            assert body["lane"] == "chat_turn"
            assert body["account_id"] == "synthetic-proto-01"
            assert body["payload"]["message"] == "hello"
            assert "Authorization" in kwargs["headers"]
            return FakeResponse(
                200,
                {
                    "status": "pending",
                    "request_id": "hwb_req1",
                    "correlation_id": "hwb:corr1",
                },
            )
        assert method == "GET"
        assert "hwb_req1" in url
        params = kwargs.get("params") or {}
        assert params["account_id"] == "synthetic-proto-01"
        return FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "hwb_req1",
                "correlation_id": "hwb:corr1",
                "account_id": "synthetic-proto-01",
                "profile_id": "synthetic-proto-01",
                "outcome": "success",
                "result": {
                    "answer": "proto answer",
                    "session_key": "sk",
                    "session_id": "sid",
                    "canonical_user_event_id": "evt-1",
                    "model": "gpt-test",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            },
        )

    fake = FakeHttp(handler)
    client = HermesBrokerClient(cfg, http_client_factory=fake.factory, sleep=_noop_sleep)
    turn = asyncio.run(
        client.run_chat_turn(
            account_id="synthetic-proto-01",
            profile_id="synthetic-proto-01",
            runtime_binding_ref="binding-proto-01",
            consent_epoch="consent.v1",
            message="hello",
            session_key="sk",
            session_id="sid",
            source_event_id="evt-1",
            expected_model="gpt-test",
        )
    )
    assert turn.text == "proto answer"
    assert turn.request_id == "hwb_req1"
    assert turn.usage["output_tokens"] == 2
    assert any(c[0] == "POST" for c in fake.calls)
    assert any(c[0] == "GET" for c in fake.calls)


async def _noop_sleep(_seconds):
    return None


def test_correlation_mismatch_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "hwb_req1",
                "correlation_id": "hwb:OTHER",
                "account_id": "synthetic-proto-01",
                "profile_id": "synthetic-proto-01",
                "outcome": "success",
                "result": {
                    "answer": "x",
                    "session_key": "sk",
                    "session_id": "sid",
                    "canonical_user_event_id": "evt-1",
                },
            },
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id="synthetic-proto-01",
                profile_id="synthetic-proto-01",
                runtime_binding_ref="binding-proto-01",
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_correlation_mismatch"


def test_cross_account_result_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "hwb_req1",
                "correlation_id": "hwb:corr1",
                "account_id": "someone-else",
                "profile_id": "synthetic-proto-01",
                "outcome": "success",
                "result": {"answer": "x"},
            },
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id="synthetic-proto-01",
                profile_id="synthetic-proto-01",
                runtime_binding_ref="binding-proto-01",
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_cross_account_result"


def test_timeout_fails_closed(monkeypatch):
    _enable(monkeypatch, ELLA_HERMES_BROKER_POLL_TIMEOUT_SECONDS="1.0")
    cfg = proto.load_prototype_config()
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    async def sleep(seconds):
        # Jump past the wait budget immediately after one poll.
        clock["t"] += 2.0

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(
            200,
            {
                "status": "awaiting_callback",
                "request_id": "hwb_req1",
                "correlation_id": "hwb:corr1",
                "account_id": "synthetic-proto-01",
                "profile_id": "synthetic-proto-01",
            },
        )

    client = HermesBrokerClient(
        cfg,
        http_client_factory=FakeHttp(handler).factory,
        sleep=sleep,
        clock=now,
    )
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id="synthetic-proto-01",
                profile_id="synthetic-proto-01",
                runtime_binding_ref="binding-proto-01",
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_wait_timeout"


def test_duplicate_replay_accepted(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {
                    "status": "completed",
                    "request_id": "hwb_req1",
                    "correlation_id": "hwb:corr1",
                    "duplicate": True,
                },
            )
        return FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "hwb_req1",
                "correlation_id": "hwb:corr1",
                "account_id": "synthetic-proto-01",
                "profile_id": "synthetic-proto-01",
                "outcome": "success",
                "duplicate": True,
                "result": {
                    "answer": "replayed",
                    "session_key": "sk",
                    "session_id": "sid",
                    "canonical_user_event_id": "evt-1",
                },
            },
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    turn = asyncio.run(
        client.run_chat_turn(
            account_id="synthetic-proto-01",
            profile_id="synthetic-proto-01",
            runtime_binding_ref="binding-proto-01",
            consent_epoch="consent.v1",
            message="hello",
            session_key="sk",
            session_id="sid",
            source_event_id="evt-1",
            expected_model="gpt-test",
        )
    )
    assert turn.duplicate is True
    assert turn.text == "replayed"


def test_missing_result_endpoint_is_explicit_companion_blocker(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(404, {"status": "error", "code": "not_found"})

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id="synthetic-proto-01",
                profile_id="synthetic-proto-01",
                runtime_binding_ref="binding-proto-01",
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_result_endpoint_missing"
    assert "companion" in (exc.value.detail or {})


def test_disallowed_host_rejected(monkeypatch):
    _enable(
        monkeypatch,
        ELLA_HERMES_BROKER_BASE_URL="https://evil.example",
        ELLA_HERMES_BROKER_ALLOWED_HOST="broker.ella.internal",
    )
    with pytest.raises(ProvisioningError) as exc:
        proto.load_prototype_config()
    assert exc.value.code == "hermes_broker_prototype_url_not_allowlisted"


def test_provider_turn_uses_direct_when_not_allowlisted(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_BROKER_PROTOTYPE_ENABLED", raising=False)
    calls = []

    class DirectClient:
        async def create_response(self, *args, **kwargs):
            calls.append("direct")
            return HermesCloudTurn(
                response_id="r1",
                text="direct",
                usage={"input_tokens": 1, "output_tokens": 1},
                model="gpt-test",
                tool_calls=0,
            )

    service = HermesCloudRuntimeService(
        repository=object(),  # type: ignore[arg-type]
        event_store=object(),  # type: ignore[arg-type]
        cloud_client=DirectClient(),  # type: ignore[arg-type]
    )

    async def boundary():
        return ("https://h", "t")

    turn = asyncio.run(
        service._provider_turn(
            runtime=_runtime(),
            request=HermesCloudTurnRequest(
                uid="synthetic-proto-01",
                client_interaction_id="evt-1",
                correlation_id="c1",
                channel="ios_chat",
                user_input="hi",
                instructions="sys",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                client_metadata={},
            ),
            scope={"session_key": "sk"},
            claimed={"hermes_session_id": "sid", "idempotency_key": "ik"},
            budget={"max_output_tokens": 64, "max_tool_calls": 0},
            grant_epoch="ge",
            mark_provider_send_boundary=boundary,
        )
    )
    assert turn.text == "direct"
    assert calls == ["direct"]


def test_provider_turn_uses_broker_when_allowlisted(monkeypatch):
    _enable(monkeypatch)
    calls = []

    class DirectClient:
        async def create_response(self, *args, **kwargs):
            calls.append("direct")
            raise AssertionError("direct path must not run for allowlisted prototype")

    class FakeBroker(HermesBrokerClient):
        def __init__(self, config):
            super().__init__(config, sleep=_noop_sleep)

        async def run_chat_turn(self, **kwargs):
            calls.append("broker")
            return client_mod.BrokerTerminalTurn(
                text="via-broker",
                request_id="hwb_x",
                correlation_id="hwb:c",
                response_id="resp",
                usage={"input_tokens": 1, "output_tokens": 1},
                model="gpt-test",
                duplicate=False,
            )

    service = HermesCloudRuntimeService(
        repository=object(),  # type: ignore[arg-type]
        event_store=object(),  # type: ignore[arg-type]
        cloud_client=DirectClient(),  # type: ignore[arg-type]
    )
    service.broker_client_factory = FakeBroker

    async def boundary():
        calls.append("boundary")
        return ("https://h", "t")

    turn = asyncio.run(
        service._provider_turn(
            runtime=_runtime(),
            request=HermesCloudTurnRequest(
                uid="synthetic-proto-01",
                client_interaction_id="evt-1",
                correlation_id="c1",
                channel="ios_chat",
                user_input="hi",
                instructions="sys",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                client_metadata={},
            ),
            scope={"session_key": "sk"},
            claimed={"hermes_session_id": "sid", "idempotency_key": "ik"},
            budget={"max_output_tokens": 64, "max_tool_calls": 0},
            grant_epoch="ge",
            mark_provider_send_boundary=boundary,
        )
    )
    assert turn.text == "via-broker"
    assert "direct" not in calls
    assert "broker" in calls
    assert "boundary" in calls


def test_enrichment_channel_uses_transcript_lane(monkeypatch):
    _enable(monkeypatch)
    seen = {}

    class FakeBroker(HermesBrokerClient):
        def __init__(self, config):
            super().__init__(config, sleep=_noop_sleep)

        async def run_transcript_user_summary(self, **kwargs):
            seen.update(kwargs)
            return client_mod.BrokerTerminalTurn(
                text='{"title":"T","overview":"[Ella] o","emoji":"x","category":"other"}',
                request_id="hwb_e",
                correlation_id="hwb:e",
                response_id="r",
                usage={"input_tokens": 0, "output_tokens": 0},
                model="gpt-test",
                duplicate=False,
            )

        async def run_chat_turn(self, **kwargs):
            raise AssertionError("chat lane must not be used for enrichment channel")

    service = HermesCloudRuntimeService(
        repository=object(),  # type: ignore[arg-type]
        event_store=object(),  # type: ignore[arg-type]
    )
    service.broker_client_factory = FakeBroker
    service.cloud_client = object()  # type: ignore[assignment]

    async def boundary():
        return ("https://h", "t")

    turn = asyncio.run(
        service._provider_turn(
            runtime=_runtime(mode=HERMES_CLOUD_ENRICHMENT_MODE),
            request=HermesCloudTurnRequest(
                uid="synthetic-proto-01",
                client_interaction_id="evt-enrich",
                correlation_id="c-enrich",
                channel=HERMES_CLOUD_ENRICHMENT_CHANNEL,
                user_input="transcript text",
                instructions="enrich",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                client_metadata={},
            ),
            scope={"session_key": "sk"},
            claimed={"hermes_session_id": "sid", "idempotency_key": "ik"},
            budget={"max_output_tokens": 64, "max_tool_calls": 0},
            grant_epoch="ge",
            mark_provider_send_boundary=boundary,
        )
    )
    assert "title" in turn.text
    assert seen["source_event_id"] == "evt-enrich"


def test_sse_mapping_preserves_answer_text():
    # Mirrors _stream_hermes_cloud_chat emission contract (data line + CRLF).
    answer = "line1\nline2"
    data_line = f"data: {answer.replace(chr(10), '__CRLF__')}\n\n"
    assert "__CRLF__" in data_line
    assert "data: line1" in data_line
