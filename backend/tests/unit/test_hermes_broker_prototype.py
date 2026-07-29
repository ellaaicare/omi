"""Focused tests for the allowlisted Hermes broker prototype transport."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from ella.services import hermes_broker_client as client_mod
from ella.services import hermes_broker_prototype as proto
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

# Distinct identities: auth uid != broker owner UUIDs.
AUTH_UID = "omi-auth-uid-synth-01"
ACCOUNT_UUID = "11111111-1111-4111-8111-111111111111"
PROFILE_UUID = "22222222-2222-4222-8222-222222222222"
BINDING_ID = "33333333-3333-4333-8333-333333333333"


def _runtime(
    *,
    uid: str = AUTH_UID,
    account_user_id: str = ACCOUNT_UUID,
    profile_user_id: str = PROFILE_UUID,
    binding_id: str = BINDING_ID,
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
        account_user_id=account_user_id,
        profile_user_id=profile_user_id,
    )


def _enable(monkeypatch, **overrides):
    env = {
        "ELLA_HERMES_BROKER_PROTOTYPE_ENABLED": "true",
        "ELLA_HERMES_BROKER_PROTOTYPE_ACCOUNT_ID": ACCOUNT_UUID,
        "ELLA_HERMES_BROKER_PROTOTYPE_PROFILE_ID": PROFILE_UUID,
        "ELLA_HERMES_BROKER_PROTOTYPE_BINDING_ID": BINDING_ID,
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


async def _noop_sleep(_seconds):
    return None


def _completed_result(**overrides):
    body = {
        "status": "completed",
        "request_id": "hwb_req1",
        "correlation_id": "hwb:corr1",
        "account_id": ACCOUNT_UUID,
        "profile_id": PROFILE_UUID,
        "lane": "chat_turn",
        "outcome": "success",
        "result": {
            "answer": "proto answer",
            "session_key": "sk",
            "session_id": "sid",
            "canonical_user_event_id": "evt-1",
            "model": "gpt-test",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    }
    body.update(overrides)
    if "result" in overrides and isinstance(overrides["result"], dict):
        merged = {
            "answer": "proto answer",
            "session_key": "sk",
            "session_id": "sid",
            "canonical_user_event_id": "evt-1",
            "model": "gpt-test",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        merged.update(overrides["result"])
        body["result"] = merged
    return body


def test_default_off_does_not_select_broker(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_BROKER_PROTOTYPE_ENABLED", raising=False)
    assert proto.load_prototype_config() is None
    assert proto.runtime_uses_broker_prototype(_runtime()) is False


def test_truthy_alias_not_enabled(monkeypatch):
    _enable(monkeypatch, ELLA_HERMES_BROKER_PROTOTYPE_ENABLED="TRUE")
    assert proto.load_prototype_config() is None


def test_allowlist_uses_owner_uuids_not_auth_uid(monkeypatch):
    _enable(monkeypatch)
    # Same auth uid but wrong owner UUIDs → not selected.
    assert (
        proto.runtime_uses_broker_prototype(
            _runtime(
                uid=AUTH_UID,
                account_user_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                profile_user_id=PROFILE_UUID,
            )
        )
        is False
    )
    # Distinct uid vs owner UUIDs matching allowlist → selected.
    assert proto.runtime_uses_broker_prototype(_runtime()) is True


def test_missing_owner_coordinates_not_selected(monkeypatch):
    _enable(monkeypatch)
    assert proto.runtime_uses_broker_prototype(_runtime(account_user_id="", profile_user_id="")) is False


def test_non_allowlisted_user_keeps_direct_path(monkeypatch):
    _enable(monkeypatch)
    other = _runtime(uid="realcryptoplato", profile_class="real")
    assert proto.runtime_uses_broker_prototype(other) is False
    assert (
        proto.runtime_uses_broker_prototype(
            _runtime(
                uid="other-synth",
                account_user_id="99999999-9999-4999-8999-999999999999",
                profile_user_id="99999999-9999-4999-8999-999999999999",
                profile_class="synthetic",
                binding_id=BINDING_ID,
            )
        )
        is False
    )


def test_binding_pin_required_when_set(monkeypatch):
    _enable(monkeypatch)
    assert proto.runtime_uses_broker_prototype(_runtime(binding_id="wrong-binding")) is False
    assert proto.runtime_uses_broker_prototype(_runtime()) is True


def test_success_chat_maps_answer_with_owner_uuids(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()
    assert cfg is not None

    def handler(method, url, kwargs):
        if method == "POST":
            body = kwargs["json"]
            assert body["lane"] == "chat_turn"
            assert body["account_id"] == ACCOUNT_UUID
            assert body["profile_id"] == PROFILE_UUID
            assert body["account_id"] != AUTH_UID
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
        assert params["account_id"] == ACCOUNT_UUID
        assert params["profile_id"] == PROFILE_UUID
        return FakeResponse(200, _completed_result())

    fake = FakeHttp(handler)
    client = HermesBrokerClient(cfg, http_client_factory=fake.factory, sleep=_noop_sleep)
    turn = asyncio.run(
        client.run_chat_turn(
            account_id=ACCOUNT_UUID,
            profile_id=PROFILE_UUID,
            runtime_binding_ref=BINDING_ID,
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


def test_correlation_mismatch_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(200, _completed_result(correlation_id="hwb:OTHER"))

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
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
            _completed_result(account_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_cross_account_result"


def test_omitted_account_on_terminal_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        body = _completed_result()
        del body["account_id"]
        return FakeResponse(200, body)

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_account_omitted"


def test_omitted_request_id_on_terminal_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        body = _completed_result()
        del body["request_id"]
        return FakeResponse(200, body)

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_request_id_omitted"


def test_cross_request_id_mismatch_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        return FakeResponse(200, _completed_result(request_id="hwb_OTHER"))

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_request_id_mismatch"


def test_omitted_session_key_in_result_fails_closed(monkeypatch):
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        body = _completed_result(result={"answer": "x", "session_id": "sid", "canonical_user_event_id": "evt-1"})
        # remove session_key from result
        del body["result"]["session_key"]
        return FakeResponse(200, body)

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_result_identity_omitted"


def test_answer_only_projection_rejected(monkeypatch):
    """Projection with only status+answer must fail (review P1-2)."""
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
            {"status": "completed", "result": {"answer": "sneaky"}},
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code in {
        "hermes_broker_prototype_request_id_omitted",
        "hermes_broker_prototype_account_omitted",
        "hermes_broker_prototype_profile_omitted",
        "hermes_broker_prototype_correlation_omitted",
        "hermes_broker_prototype_lane_omitted",
    }


def test_omitted_lane_on_terminal_fails_closed(monkeypatch):
    """Terminal projection without lane must fail (review 4812248201)."""
    _enable(monkeypatch)
    cfg = proto.load_prototype_config()

    def handler(method, url, kwargs):
        if method == "POST":
            return FakeResponse(
                200,
                {"status": "pending", "request_id": "hwb_req1", "correlation_id": "hwb:corr1"},
            )
        body = _completed_result()
        del body["lane"]
        return FakeResponse(200, body)

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_lane_omitted"


def test_cross_lane_result_fails_closed(monkeypatch):
    """Chat turn must reject terminal projection for the enrichment lane."""
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
            _completed_result(lane="transcript_summary_enrichment"),
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    with pytest.raises(ProvisioningError) as exc:
        asyncio.run(
            client.run_chat_turn(
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
                consent_epoch="consent.v1",
                message="hello",
                session_key="sk",
                session_id="sid",
                source_event_id="evt-1",
                expected_model="gpt-test",
            )
        )
    assert exc.value.code == "hermes_broker_prototype_cross_lane_result"


def test_timeout_fails_closed(monkeypatch):
    _enable(monkeypatch, ELLA_HERMES_BROKER_POLL_TIMEOUT_SECONDS="1.0")
    cfg = proto.load_prototype_config()
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    async def sleep(seconds):
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
                "account_id": ACCOUNT_UUID,
                "profile_id": PROFILE_UUID,
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
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
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
            _completed_result(
                duplicate=True,
                result={
                    "answer": "replayed",
                    "session_key": "sk",
                    "session_id": "sid",
                    "canonical_user_event_id": "evt-1",
                },
            ),
        )

    client = HermesBrokerClient(cfg, http_client_factory=FakeHttp(handler).factory, sleep=_noop_sleep)
    turn = asyncio.run(
        client.run_chat_turn(
            account_id=ACCOUNT_UUID,
            profile_id=PROFILE_UUID,
            runtime_binding_ref=BINDING_ID,
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
                account_id=ACCOUNT_UUID,
                profile_id=PROFILE_UUID,
                runtime_binding_ref=BINDING_ID,
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
                uid=AUTH_UID,
                client_interaction_id="evt-1",
                correlation_id="c1",
                channel="ios_chat",
                user_input="hi",
                instructions="sys",
                started_at=datetime.now(timezone.utc),
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


def test_provider_turn_submits_owner_uuids_not_auth_uid(monkeypatch):
    _enable(monkeypatch)
    seen = {}

    class DirectClient:
        async def create_response(self, *args, **kwargs):
            raise AssertionError("direct path must not run for allowlisted prototype")

    class FakeBroker(HermesBrokerClient):
        def __init__(self, config):
            super().__init__(config, sleep=_noop_sleep)

        async def run_chat_turn(self, **kwargs):
            seen.update(kwargs)
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
        return ("https://h", "t")

    turn = asyncio.run(
        service._provider_turn(
            runtime=_runtime(),
            request=HermesCloudTurnRequest(
                uid=AUTH_UID,
                client_interaction_id="evt-1",
                correlation_id="c1",
                channel="ios_chat",
                user_input="hi",
                instructions="sys",
                started_at=datetime.now(timezone.utc),
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
    assert seen["account_id"] == ACCOUNT_UUID
    assert seen["profile_id"] == PROFILE_UUID
    assert seen["account_id"] != AUTH_UID
    assert seen["runtime_binding_ref"] == BINDING_ID


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
                uid=AUTH_UID,
                client_interaction_id="evt-enrich",
                correlation_id="c-enrich",
                channel=HERMES_CLOUD_ENRICHMENT_CHANNEL,
                user_input="transcript text",
                instructions="enrich",
                started_at=datetime.now(timezone.utc),
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
    assert seen["account_id"] == ACCOUNT_UUID
    assert seen["source_event_id"] == "evt-enrich"


def test_sse_mapping_preserves_answer_text():
    answer = "line1\nline2"
    data_line = f"data: {answer.replace(chr(10), '__CRLF__')}\n\n"
    assert "__CRLF__" in data_line
    assert "data: line1" in data_line
