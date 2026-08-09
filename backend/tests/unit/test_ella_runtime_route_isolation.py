import asyncio
import inspect
import sys
from dataclasses import replace
from types import ModuleType
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

sys.modules.setdefault("websockets", ModuleType("websockets"))
conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value: value
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import chat, resolve, voice
from ella.services import runtime_resolver
from ella.services.provisioning import ProvisioningError
from utils.ella import exact_firebase_auth

RUNTIME_AUTHORITY_DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def voice_authority_defaults(monkeypatch):
    async def self_hosted_disabled(_uid):
        return False

    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", False)
    monkeypatch.setattr(voice, "_self_hosted_voice_required", self_hosted_disabled)
    monkeypatch.setattr(
        voice,
        "runtime_authority_identity",
        lambda _runtime: SimpleNamespace(digest=RUNTIME_AUTHORITY_DIGEST),
    )


def _request():
    return Request({"type": "http", "method": "POST", "path": "/v1/ella/chat/stream", "headers": []})


def _invitation_chat_runtime(**overrides):
    values = {
        "uid": "invited-user",
        "binding_id": "binding-1",
        "provider": "hermes",
        "status": "active",
        "profile_name": "omi-invited-user",
        "agent_id": "hermes",
        "runtime_instance_id": "instance-1",
        "gateway_url": "http://hermes.internal.test:8642",
        "gateway_token": "test-hermes-token",
        "workspace_root": "/srv/ella/invited-user",
        "honcho_workspace": "honcho-invited-user",
        "observed_peer": "invited-user",
        "observer_peer": "ella-invited-user",
        "prompt_pack_version": "hermes-user-v1",
        "expected_model": "gpt-5.6-terra",
        "model_context_window_tokens": 128000,
        "allowed_tools": (),
        "required_capabilities": (),
        "model_policy_version": "frontier-v1",
        "voice_policy_version": "ella-voice-v1",
        "revision": 7,
        "profile_class": "real",
        "runtime_target_id": "target-chat-1",
        "runtime_target_mode": "hermes-chat",
        "runtime_target_updated_at": "2026-08-02T18:00:00+00:00",
        "target_endpoint_ref": "",
        "target_credential_ref": "",
        "target_entitlement_revision": 11,
        "consent_authority_epoch": "authority-epoch-1",
        "account_user_id": "account-1",
        "profile_user_id": "account-1",
    }
    values.update(overrides)
    return chat.IsolatedRuntime(**values)


async def _collect_stream(stream):
    return [chunk async for chunk in stream]


async def _runtime_authority_enabled(_uid=None):
    return True


def test_chat_rejects_body_uid_that_differs_from_firebase_subject(monkeypatch):
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            chat.ella_chat_stream(
                chat.EllaChatRequest(uid="user-b", message="hello"),
                _request(),
                authenticated_uid="user-a",
            )
        )
    assert error.value.status_code == 403
    assert error.value.detail == {"code": "ownership_mismatch"}

    async def no_events(*_args, **_kwargs):
        return []

    async def no_temporal(*_args, **_kwargs):
        return "recent context", []

    async def canonical_write(*_args, **_kwargs):
        return None

    class ForbiddenProviderClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("authority drift must fail before any provider HTTP client is created")

    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)
    monkeypatch.setattr(chat, "_write_ios_chat_canonical_event", canonical_write)
    monkeypatch.setattr(chat.httpx, "AsyncClient", ForbiddenProviderClient)
    admitted = _invitation_chat_runtime()
    drifted = (
        ProvisioningError("self_hosted_invitation_runtime_not_provisioned", retryable=False),
        replace(admitted, target_entitlement_revision=12),
        replace(admitted, runtime_target_id="target-chat-drifted"),
        replace(admitted, consent_authority_epoch="authority-epoch-drifted"),
        replace(admitted, account_user_id="account-drifted", profile_user_id="profile-drifted"),
    )
    for current in drifted:

        async def resolve_current(*_args, _current=current, **_kwargs):
            if isinstance(_current, Exception):
                raise _current
            return _current

        monkeypatch.setattr(runtime_resolver, "resolve_isolated_runtime", resolve_current)
        chunks = asyncio.run(
            _collect_stream(
                chat._stream_hermes_chat(
                    "content-free test",
                    admitted.uid,
                    runtime=admitted,
                )
            )
        )
        assert any(
            code in "".join(chunks)
            for code in (
                "self_hosted_invitation_runtime_not_provisioned",
                "hermes_runtime_authority_changed",
            )
        )

    provider_effects = []
    revalidation_calls = []

    class EmptyStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

    class EmptyStreamClient:
        def __init__(self, *_args, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            provider_effects.append("initial_stream")
            return EmptyStreamResponse()

        async def post(self, *_args, **_kwargs):
            provider_effects.append("recovery_send")
            raise AssertionError("recovery provider call must be denied after authority drift")

    async def revalidate_for_recovery(_identity):
        revalidation_calls.append("revalidate")
        if len(revalidation_calls) == 1:
            return admitted
        raise ProvisioningError("hermes_runtime_authority_changed", retryable=False)

    monkeypatch.setattr(chat, "revalidate_runtime_authority", revalidate_for_recovery)
    monkeypatch.setattr(chat.httpx, "AsyncClient", EmptyStreamClient)
    recovery_chunks = asyncio.run(
        _collect_stream(
            chat._stream_hermes_chat(
                "content-free recovery test",
                admitted.uid,
                runtime=admitted,
            )
        )
    )
    assert revalidation_calls == ["revalidate", "revalidate"]
    assert provider_effects == ["initial_stream"]
    assert "hermes_runtime_authority_changed" in "".join(recovery_chunks)


def test_chat_history_rejects_query_uid_that_differs_from_firebase_subject():
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.ella_chat_history("user-b", authenticated_uid="user-a"))
    assert error.value.status_code == 403


def test_isolated_hermes_chat_has_a_bounded_provider_timeout(monkeypatch):
    captured = {}

    async def no_events(*_args, **_kwargs):
        return []

    async def no_temporal(*_args, **_kwargs):
        return "recent context", []

    async def canonical_write(*_args, **_kwargs):
        return None

    async def stable_runtime(_identity):
        return _invitation_chat_runtime()

    class TimedOutStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            raise chat.httpx.ReadTimeout("content-free provider timeout")
            yield

    class TimedClient:
        def __init__(self, *_args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return TimedOutStreamResponse()

    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)
    monkeypatch.setattr(chat, "_write_ios_chat_canonical_event", canonical_write)
    monkeypatch.setattr(chat, "revalidate_runtime_authority", stable_runtime)
    monkeypatch.setattr(chat.httpx, "AsyncClient", TimedClient)

    chunks = asyncio.run(
        _collect_stream(
            chat._stream_hermes_chat(
                "content-free timeout test",
                "invited-user",
                runtime=_invitation_chat_runtime(),
            )
        )
    )

    assert captured["timeout"] == chat.HERMES_CHAT_REQUEST_TIMEOUT_SECONDS == 60.0
    assert chunks == ["data: Error: Hermes request timed out\n\n"]


def test_mounted_chat_history_authenticates_before_runtime_or_history_work(monkeypatch):
    downstream_calls = []

    def verify_token(token):
        if token == "valid-a":
            return {"uid": "user-a"}
        raise ValueError("expired or invalid")

    async def authority_enabled(uid):
        downstream_calls.append(("authority", uid))
        return True

    async def runtime(uid, **_kwargs):
        downstream_calls.append(("runtime", uid))
        return SimpleNamespace(provider="hermes_cloud")

    async def no_events(uid, *, limit, before=None):
        downstream_calls.append(("history", uid, limit, before))
        return []

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify_token)
    monkeypatch.setattr(chat, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", runtime)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    app = FastAPI()
    app.include_router(chat.router)
    client = TestClient(app)

    for headers in ({}, {"Authorization": "Basic valid-a"}, {"Authorization": "Bearer expired"}):
        assert client.get("/v1/ella/chat/history", headers=headers).status_code == 401
    assert (
        client.get(
            "/v1/ella/chat/history?uid=user-b",
            headers={"Authorization": "Bearer valid-a"},
        ).status_code
        == 403
    )
    assert downstream_calls == []

    response = client.get(
        "/v1/ella/chat/history?limit=17",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "messages": [],
        "hasMore": False,
        "source": "canonical_timeline_empty",
        "fallback": False,
    }
    assert downstream_calls == [
        ("authority", "user-a"),
        ("runtime", "user-a"),
        ("history", "user-a", 17, None),
    ]

    async def valid_events(uid, *, limit, before=None):
        downstream_calls.append(("valid-history", uid, limit, before))
        return [
            {
                "uid": uid,
                "event_id": "history-a",
                "channel": "ios_chat",
                "provider": "omi-backend",
                "role": "assistant",
                "text": "A valid first-party history turn.",
                "started_at": "2026-08-03T12:00:00+00:00",
                "metadata": {},
            }
        ]

    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", valid_events)
    valid = client.get(
        "/v1/ella/chat/history?limit=5",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert valid.status_code == 200
    assert valid.json()["source"] == "canonical_timeline"
    assert valid.json()["messages"][0]["text"] == "A valid first-party history turn."
    assert downstream_calls[-3:] == [
        ("authority", "user-a"),
        ("runtime", "user-a"),
        ("valid-history", "user-a", 5, None),
    ]


def test_isolated_history_never_uses_openclaw_fallback(monkeypatch):
    async def fake_runtime(uid, **kwargs):
        assert uid == "user-a"
        return SimpleNamespace(profile_name="omi-user-a")

    async def no_events(uid, *, limit, before=None):
        return []

    async def forbidden_legacy(uid):
        raise AssertionError("OpenClaw fallback must not run in isolated mode")

    monkeypatch.setattr(chat, "runtime_authority_enabled", _runtime_authority_enabled)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "resolve_user_routing", forbidden_legacy)

    result = asyncio.run(chat.ella_chat_history("user-a", authenticated_uid="user-a"))

    assert result == {
        "messages": [],
        "hasMore": False,
        "source": "canonical_timeline_empty",
        "fallback": False,
    }


def test_cloud_history_never_uses_openclaw_fallback(monkeypatch):
    async def fake_runtime(uid, **kwargs):
        assert uid == "user-a"
        return SimpleNamespace(provider="hermes_cloud")

    async def no_events(uid, *, limit, before=None):
        return []

    async def forbidden_legacy(uid):
        raise AssertionError("OpenClaw fallback must not run for a cloud-bound user")

    monkeypatch.setattr(chat, "runtime_authority_enabled", _runtime_authority_enabled)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "resolve_user_routing", forbidden_legacy)

    result = asyncio.run(chat.ella_chat_history("user-a", authenticated_uid="user-a"))

    assert result == {
        "messages": [],
        "hasMore": False,
        "source": "canonical_timeline_empty",
        "fallback": False,
    }


def test_legacy_history_unexpected_failure_logs_fixed_content_free_classification(monkeypatch, caplog):
    class HostileHistoryError(RuntimeError):
        def __init__(self):
            super().__init__(
                "endpoint=https://secret token=secret session=secret-session workspace=/secret/workspace "
                "provider_payload=SECRET"
            )
            self.endpoint = "https://secret"
            self.token = "secret"
            self.session = "secret-session"
            self.workspace = "/secret/workspace"
            self.provider_payload = {"private": "SECRET"}

    class HostileAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            raise HostileHistoryError()

    async def authority_disabled(uid):
        assert uid == "user-a"
        return False

    async def no_events(uid, *, limit, before=None):
        assert uid == "user-a"
        return []

    async def owned_legacy_routing(uid):
        assert uid == "user-a"
        return {"routing": {"agentId": "ella-user-a", "workspace": "/legacy/user-a"}}

    monkeypatch.setattr(chat, "runtime_authority_enabled", authority_disabled)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "resolve_user_routing", owned_legacy_routing)
    monkeypatch.setattr(chat.httpx, "AsyncClient", HostileAsyncClient)

    with caplog.at_level("ERROR", logger=chat.__name__):
        result = asyncio.run(chat.ella_chat_history("user-a", authenticated_uid="user-a"))

    assert result == {
        "messages": [],
        "hasMore": False,
        "source": "provision_openclaw_history_migration",
        "fallback": True,
    }
    assert len(caplog.records) == 1
    log_message = caplog.records[0].getMessage()
    log_prefix = "[FLOW:HISTORY] code=ella_legacy_history_unavailable classification=unexpected latency="
    assert log_message.startswith(log_prefix) and log_message.endswith("ms")
    assert int(log_message.removeprefix(log_prefix).removesuffix("ms")) >= 0
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    serialized = f"{caplog.text} {result}"
    for forbidden in (
        "https://secret",
        "token=secret",
        "secret-session",
        "/secret/workspace",
        "provider_payload",
        "SECRET",
    ):
        assert forbidden not in serialized


def test_isolated_history_fails_closed_without_binding(monkeypatch):
    async def missing_runtime(uid, **kwargs):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)

    monkeypatch.setattr(chat, "runtime_authority_enabled", _runtime_authority_enabled)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", missing_runtime)

    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.ella_chat_history("user-a", authenticated_uid="user-a"))
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "hermes_not_provisioned"}

    class InvitationOwnedRepository:
        async def has_invitation_owned_self_hosted_runtime(self, uid):
            assert uid == "invited-user"
            return True

        async def get_self_hosted_invitation_admission(self, _uid):
            raise AssertionError("master-off quarantine must not treat invitation authority as enabled")

    repository = InvitationOwnedRepository()

    async def actual_authority(uid):
        return await runtime_resolver.runtime_authority_enabled(uid, repository=repository)

    async def actual_runtime(uid, **kwargs):
        return await runtime_resolver.resolve_isolated_runtime(uid, repository=repository, **kwargs)

    async def forbidden_legacy(_uid):
        raise AssertionError("master-off invitation owner must never reach OpenClaw fallback")

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setattr(chat, "runtime_authority_enabled", actual_authority)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", actual_runtime)
    monkeypatch.setattr(chat, "resolve_user_routing", forbidden_legacy)

    with pytest.raises(HTTPException) as disabled:
        asyncio.run(chat.ella_chat_history("invited-user", authenticated_uid="invited-user"))
    assert disabled.value.status_code == 503
    assert disabled.value.detail == {"code": "self_hosted_invitation_runtime_disabled"}


def test_public_resolver_is_authenticated_and_redacts_internal_runtime(monkeypatch):
    class Pool:
        async def fetchrow(self, _query, uid):
            assert uid == "user-a"
            return {
                "omi_uid": uid,
                "status": "active",
                "agents": {
                    "userAgentId": "hermes",
                    "gatewayToken": "secret",
                    "workspace": "/private/user-a",
                },
                "cluster_status": "ready",
            }

    monkeypatch.setattr(resolve, "_pool", Pool())
    monkeypatch.setattr(resolve, "CHAT_PLATFORM", "hermes")
    runtime_calls = []

    async def active_runtime(uid, repository, target_mode):
        runtime_calls.append((uid, repository.pool, target_mode))
        return SimpleNamespace(provider="hermes", status="active")

    monkeypatch.setattr(resolve, "resolve_isolated_runtime", active_runtime)

    result = asyncio.run(resolve.resolve_endpoint(uid="user-a", authenticated_uid="user-a"))

    assert result == {
        "user": {"omiUid": "user-a", "status": "active"},
        "routing": {"available": True, "clusterStatus": "active", "platform": "hermes"},
    }
    assert runtime_calls == [("user-a", resolve._pool, "hermes-cloud-chat")]
    assert "secret" not in str(result)
    assert "gatewayUrl" not in result["routing"]
    assert "workspace" not in result["routing"]


def test_public_resolver_rejects_cross_user_and_email_lookup():
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(resolve.resolve_endpoint(uid="user-b", authenticated_uid="user-a"))
    assert mismatch.value.status_code == 403

    parameters = inspect.signature(resolve.resolve_endpoint).parameters
    assert "email" not in parameters
    assert "phone" not in parameters


def test_voice_session_rejects_cross_user_before_issuing_token():
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="user-b", provider="grok-voice"),
                authenticated_uid="user-a",
            )
        )
    assert error.value.status_code == 403


def test_voice_session_requires_active_runtime_when_isolation_enabled(monkeypatch):
    async def missing_runtime(uid, **kwargs):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", missing_runtime)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="user-a", provider="grok-voice"),
                authenticated_uid="user-a",
            )
        )
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "hermes_not_provisioned"}


def test_voice_session_stays_closed_while_isolated_voice_flag_is_disabled(monkeypatch):
    async def self_hosted_required(uid):
        return uid == "invited-user"

    async def forbidden_runtime(*_args, **_kwargs):
        raise AssertionError("voice-off account must stop before runtime/provider resolution")

    stale_uid = "test-pr835-dryrun-20260504000956"
    monkeypatch.setenv("ELLA_ISOLATED_VOICE_ROUTING_ENABLED", "false")
    monkeypatch.setenv("ELLA_ISOLATED_VOICE_ROUTING_ENABLED_UIDS", f"invited-user,{stale_uid}")
    monkeypatch.setattr(voice, "_self_hosted_voice_required", self_hosted_required)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: uid == stale_uid)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", forbidden_runtime)
    monkeypatch.setattr(
        voice,
        "resolve_processor",
        lambda _provider: (_ for _ in ()).throw(AssertionError("legacy provider lookup must not run")),
    )

    for uid in ("invited-user", stale_uid):
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                voice.create_voice_session(
                    body=voice.VoiceSessionRequest(uid=uid, provider="grok-voice"),
                    authenticated_uid=uid,
                )
            )
        assert voice.isolated_voice_routing_enabled(uid) is False
        assert error.value.status_code == 503
        assert error.value.detail == {"code": "isolated_voice_not_ready"}


def test_voice_session_issues_isolated_token_for_enabled_uid_canary(monkeypatch):
    async def ready_runtime(uid, **kwargs):
        assert uid == "user-a"
        return object()

    class Pool:
        async def fetchrow(self, *args):
            return None

    async def pool():
        return Pool()

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: uid == "user-a")
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: uid == "user-a")
    monkeypatch.setattr(voice, "resolve_isolated_runtime", ready_runtime)
    monkeypatch.setattr(voice, "_get_pool", pool)

    result = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(uid="user-a", provider="grok-voice"),
            authenticated_uid="user-a",
        )
    )
    claims = voice.jwt.decode(
        result.session_token,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )

    assert result.voice_endpoint.endswith("?mode=v4")
    assert claims["sub"] == "user-a"
    assert claims["uid"] == "user-a"
    assert claims["voice_mode"] == "v4"
    assert claims["provider"] == "grok-voice"
    assert claims["isolated_runtime"] is True


def test_self_hosted_voice_session_uses_exact_invitation_authority_without_legacy_registry(monkeypatch):
    runtime = SimpleNamespace(target_entitlement_revision=7)
    policy_calls = []

    async def resolve_runtime(uid, **kwargs):
        assert uid == "invited-user"
        assert kwargs == {"target_mode": "hermes-voice"}
        return runtime

    async def evaluate_issuance(**kwargs):
        policy_calls.append(kwargs)
        return SimpleNamespace(
            allowed=True,
            code="ok",
            entitlement={"revision": 7},
            quota={"daily_used_s": 0},
        )

    class Pool:
        async def fetchrow(self, *args):
            return None

    async def pool():
        return Pool()

    async def self_hosted_required(uid):
        return uid == "invited-user"

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(voice, "_self_hosted_voice_required", self_hosted_required)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "cloud_provisioning_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: uid == "invited-user")
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve_runtime)
    monkeypatch.setattr(voice.voice_canary_db, "evaluate_issuance", evaluate_issuance)
    monkeypatch.setattr(voice, "resolve_processor", lambda _provider: (_ for _ in ()).throw(AssertionError("legacy")))
    monkeypatch.setattr(voice, "V2V_PROVIDERS", {})
    monkeypatch.setattr(voice, "_get_pool", pool)

    result = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(uid="invited-user", provider="grok-voice"),
            authenticated_uid="invited-user",
        )
    )

    assert result.provider == "hermes"
    assert result.voice_mode == "hermes-voice"
    assert result.voice_endpoint.endswith("?mode=hermes-voice")
    assert len(policy_calls) == 1
    assert {key: value for key, value in policy_calls[0].items() if key != "correlation_id"} == {
        "uid": "invited-user",
        "provider": "hermes",
        "model": voice.SELF_HOSTED_RUNTIME_MODEL,
        "mode": "hermes-voice",
    }
    assert policy_calls[0]["correlation_id"]
    claims = voice.jwt.decode(
        result.session_token,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )
    assert claims["provider"] == "hermes"
    assert claims["voice_mode"] == "hermes-voice"
    assert claims["runtime_authority_digest"] == RUNTIME_AUTHORITY_DIGEST
    assert claims["entitlement_revision"] == 7


def test_self_hosted_voice_session_activation_race_drift_fails_before_token_or_provider(monkeypatch):
    runtimes = [
        SimpleNamespace(target_entitlement_revision=7, marker="issued"),
        SimpleNamespace(target_entitlement_revision=7, marker="drifted"),
    ]
    policy_calls = []

    async def resolve_runtime(_uid, **kwargs):
        assert kwargs == {"target_mode": "hermes-voice"}
        return runtimes.pop(0)

    async def evaluate_issuance(**kwargs):
        policy_calls.append(kwargs)
        return SimpleNamespace(
            allowed=True,
            code="ok",
            entitlement={"revision": 7},
            quota={},
        )

    class Pool:
        async def fetchrow(self, *args):
            return None

    async def pool():
        return Pool()

    async def self_hosted_required(_uid):
        return True

    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(voice, "_self_hosted_voice_required", self_hosted_required)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "cloud_provisioning_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve_runtime)
    monkeypatch.setattr(
        voice,
        "runtime_authority_identity",
        lambda runtime: SimpleNamespace(digest=("a" if runtime.marker == "issued" else "b") * 64),
    )
    monkeypatch.setattr(voice.voice_canary_db, "evaluate_issuance", evaluate_issuance)
    monkeypatch.setattr(voice, "V2V_PROVIDERS", {})
    monkeypatch.setattr(voice, "_get_pool", pool)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="invited-user", provider="openai-native-realtime"),
                authenticated_uid="invited-user",
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "voice_runtime_authority_changed"}
    assert len(policy_calls) == 1
    assert runtimes == []


@pytest.mark.parametrize(
    "authority_code",
    [
        "self_hosted_invitation_runtime_not_provisioned",
        "runtime_admission_no_entitlement",
        "runtime_admission_entitlement_stale",
        "self_hosted_runtime_consent_authority_epoch_invalid",
        "self_hosted_runtime_target_lineage_stale",
        "runtime_admission_provider_not_allowed",
        "runtime_admission_model_not_allowed",
        "runtime_admission_mode_not_allowed",
        "invitation_runtime_fallback_enabled",
        "runtime_ownership_mismatch",
        "runtime_not_ready",
    ],
)
def test_self_hosted_voice_session_authority_denials_precede_provider_setup(monkeypatch, authority_code):
    provider_calls = []

    async def deny_runtime(uid, **kwargs):
        assert uid == "invited-user"
        assert kwargs == {"target_mode": "hermes-voice"}
        raise ProvisioningError(authority_code, retryable=False)

    async def forbidden_policy(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("policy/provider setup must not run after authority denial")

    async def self_hosted_required(_uid):
        return True

    monkeypatch.setattr(voice, "_self_hosted_voice_required", self_hosted_required)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "cloud_provisioning_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", deny_runtime)
    monkeypatch.setattr(voice.voice_canary_db, "evaluate_issuance", forbidden_policy)
    monkeypatch.setattr(voice, "V2V_PROVIDERS", {})

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="invited-user", provider="gemini-live"),
                authenticated_uid="invited-user",
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail == {"code": authority_code}
    assert provider_calls == []


def test_memory_scoped_voice_session_resolves_server_context_and_signs_ids(monkeypatch):
    async def ready_runtime(uid, **kwargs):
        assert uid == "user-a"
        return object()

    async def resolve_scope(uid, scope):
        assert uid == "user-a"
        assert scope.conversation_id == "memory-a"
        return {
            "kind": "memory",
            "conversation_id": "memory-a",
            "active_summary_version_id": "version-2",
            "can_reinterpret": True,
            "title": "Private title",
            "overview": "Private overview",
        }

    class Pool:
        async def fetchrow(self, *args):
            return {"name": "User A"}

    async def pool():
        return Pool()

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", ready_runtime)
    monkeypatch.setattr(voice, "_resolve_voice_memory_scope", resolve_scope)
    monkeypatch.setattr(voice, "_get_pool", pool)

    result = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(
                uid="user-a",
                provider="grok-voice",
                session_scope=voice.VoiceSessionScope(
                    kind="memory",
                    conversation_id="memory-a",
                ),
            ),
            authenticated_uid="user-a",
        )
    )
    claims = voice.jwt.decode(
        result.session_token,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )

    assert result.session_id == claims["jti"]
    assert result.session_scope == {
        "kind": "memory",
        "conversation_id": "memory-a",
        "active_summary_version_id": "version-2",
        "can_reinterpret": True,
    }
    assert claims["conversation_id"] == "memory-a"
    assert "title" not in claims
    assert "overview" not in claims


@pytest.mark.parametrize("legacy_mode", ["v1", "v2", "v3-fast"])
def test_memory_scoped_voice_session_rejects_legacy_mode_before_scope_resolution(
    monkeypatch,
    legacy_mode,
):
    async def forbidden_scope_resolution(*args, **kwargs):
        raise AssertionError("legacy scoped mode must fail before memory resolution")

    def forbidden_token_issuance(*args, **kwargs):
        raise AssertionError("legacy scoped mode must fail before token issuance")

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_resolve_voice_memory_scope", forbidden_scope_resolution)
    monkeypatch.setattr(voice, "create_session_token", forbidden_token_issuance)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(
                    uid="user-a",
                    provider="grok-voice",
                    voice_mode=legacy_mode,
                    session_scope=voice.VoiceSessionScope(
                        kind="memory",
                        conversation_id="memory-a",
                    ),
                ),
                authenticated_uid="user-a",
            )
        )

    assert error.value.status_code == 400
    assert error.value.detail == {"code": "memory_scoped_voice_mode_required"}


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("grok-voice", "v4"),
        ("gemini-live", "gemini-live"),
        ("gemini-native-live", "gemini-native-live-v1"),
        ("gemini-live", "gemini-zero-live-v1"),
        ("gemini-native-live", "gemini-lite-live-v1"),
        ("gemini-native-live", "gemini-full-live-v1"),
    ],
)
def test_memory_scoped_voice_provider_mode_allows_modern_proxy_pairs(provider, mode):
    assert voice._memory_scoped_voice_provider_mode_error(provider, mode) is None


@pytest.mark.parametrize(
    ("provider", "mode", "expected_code"),
    [
        (
            "openai-native-realtime",
            None,
            "memory_scoped_voice_provider_unsupported",
        ),
        (
            "openai-native-realtime",
            "v4",
            "memory_scoped_voice_provider_unsupported",
        ),
        (
            "grok-voice",
            "gemini-live",
            "memory_scoped_voice_provider_mode_mismatch",
        ),
        (
            "gemini-live",
            "v4",
            "memory_scoped_voice_provider_mode_mismatch",
        ),
    ],
)
def test_memory_scoped_voice_session_rejects_unsupported_provider_mode_pair_before_scope_resolution(
    monkeypatch,
    provider,
    mode,
    expected_code,
):
    async def forbidden_scope_resolution(*args, **kwargs):
        raise AssertionError("invalid scoped pair must fail before memory resolution")

    def forbidden_token_issuance(*args, **kwargs):
        raise AssertionError("invalid scoped pair must fail before token issuance")

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_resolve_voice_memory_scope", forbidden_scope_resolution)
    monkeypatch.setattr(voice, "create_session_token", forbidden_token_issuance)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(
                    uid="user-a",
                    provider=provider,
                    voice_mode=mode,
                    session_scope=voice.VoiceSessionScope(
                        kind="memory",
                        conversation_id="memory-a",
                    ),
                ),
                authenticated_uid="user-a",
            )
        )

    assert error.value.status_code == 400
    assert error.value.detail == {"code": expected_code}


@pytest.mark.parametrize(
    ("provider", "requested_mode", "expected_mode"),
    [
        ("grok-voice", None, "v4"),
        ("grok-voice", "v4", "v4"),
        ("gemini-live", None, "gemini-live"),
        ("gemini-live", "gemini-zero-live-v1", "gemini-zero-live-v1"),
        ("gemini-native-live", None, "gemini-native-live-v1"),
        ("gemini-native-live", "gemini-full-live-v1", "gemini-full-live-v1"),
    ],
)
def test_retained_memory_scoped_voice_session_accepts_supported_provider_mode_pair(
    monkeypatch,
    provider,
    requested_mode,
    expected_mode,
):
    async def resolve_scope(uid, scope):
        assert uid == "user-a"
        return {
            "kind": "memory",
            "conversation_id": scope.conversation_id,
            "active_summary_version_id": "version-2",
            "can_reinterpret": True,
        }

    class Pool:
        async def fetchrow(self, *args):
            return None

    async def pool():
        return Pool()

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_resolve_voice_memory_scope", resolve_scope)
    monkeypatch.setattr(voice, "_get_pool", pool)

    result = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(
                uid="user-a",
                provider=provider,
                voice_mode=requested_mode,
                session_scope=voice.VoiceSessionScope(
                    kind="memory",
                    conversation_id="memory-a",
                ),
            ),
            authenticated_uid="user-a",
        )
    )
    claims = voice.jwt.decode(
        result.session_token,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )

    assert result.provider == provider
    assert result.voice_mode == expected_mode
    assert claims["provider"] == provider
    assert claims["voice_mode"] == expected_mode
    assert claims["isolated_runtime"] is False


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("grok-voice", "v1"),
        ("grok-voice", "gemini-live"),
        ("gemini-live", "v4"),
        ("openai-native-realtime", "v4"),
    ],
)
def test_unscoped_voice_session_preserves_existing_provider_mode_behavior(
    monkeypatch,
    provider,
    mode,
):
    class Pool:
        async def fetchrow(self, *args):
            return None

    async def pool():
        return Pool()

    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_get_pool", pool)

    result = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(
                uid="user-a",
                provider=provider,
                voice_mode=mode,
            ),
            authenticated_uid="user-a",
        )
    )

    assert result.provider == provider
    assert result.voice_mode == mode


def test_unversionable_memory_scope_returns_defined_nonwriteable_state(monkeypatch):
    def unavailable(uid, scope):
        raise ValueError("voice_session_scope_version_unavailable")

    monkeypatch.setattr(voice, "_load_voice_memory_scope", unavailable)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice._resolve_voice_memory_scope(
                "user-a",
                voice.VoiceSessionScope(kind="memory", conversation_id="memory-empty"),
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "voice_session_scope_version_unavailable"}


def test_voice_session_rejects_voice_canary_without_runtime_binding(monkeypatch):
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: uid == "user-a")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="user-a", provider="grok-voice"),
                authenticated_uid="user-a",
            )
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"code": "isolated_voice_runtime_required"}


def test_cloud_resolver_returns_first_party_route_without_vendor_credentials(monkeypatch):
    class Pool:
        async def fetchrow(self, *args):
            return {
                "id": "db-user-a",
                "name": "A",
                "omi_uid": "user-a",
                "status": "ACTIVE",
                "guardian_mode": "off",
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
                "agents": None,
                "cluster_status": None,
            }

    runtime = SimpleNamespace(
        provider="hermes_cloud",
        agent_id="hermes-cloud",
        profile_name="cloud-user-a",
        revision=2,
        model_policy_version="models-v1",
        voice_policy_version="voice-v1",
        gateway_url="https://vendor.example.test",
        gateway_token="must-not-leak",
    )

    async def resolve_runtime(uid, repository, **kwargs):
        assert uid == "user-a"
        return runtime

    monkeypatch.setattr(resolve, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(resolve, "resolve_isolated_runtime", resolve_runtime)

    result = asyncio.run(resolve.resolve_user_routing("user-a"))

    assert result["routing"]["platform"] == "hermes_cloud"
    assert result["routing"]["chatUrl"] == "/v1/ella/chat/stream"
    assert result["routing"]["runtimeBound"] is True
    assert "gatewayUrl" not in result["routing"]
    assert "token" not in result["routing"]


def test_cloud_chat_path_does_not_call_openclaw_or_mini(monkeypatch):
    class Repository:
        @classmethod
        async def create(cls):
            return object()

    class Service:
        def __init__(self, **kwargs):
            pass

        async def run_turn(self, runtime, request):
            return SimpleNamespace(
                text="Cloud response",
                response_id="response-a",
                duplicate=False,
                canonical_assistant_event_id="assistant-event-a",
            )

    async def no_context(*args, **kwargs):
        return []

    async def no_temporal(*args, **kwargs):
        return ("requested window", [])

    async def forbidden(*args, **kwargs):
        raise AssertionError("legacy resolver must not run")

    runtime = SimpleNamespace(provider="hermes_cloud", binding_id="binding-a")
    monkeypatch.setattr(chat, "EllaProvisioningRepository", Repository)
    monkeypatch.setattr(chat, "HermesCloudRuntimeService", Service)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_context)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)
    monkeypatch.setattr(chat, "resolve_user_routing", forbidden)

    async def collect():
        return [
            item
            async for item in chat._stream_hermes_cloud_chat(
                "Synthetic hello",
                "user-a",
                {"synthetic": True},
                turn_id="turn-a",
                client_sent_at=chat.datetime.now(chat.timezone.utc),
                runtime=runtime,
            )
        ]

    output = asyncio.run(collect())
    assert output[0] == "data: Cloud response\n\n"
    assert output[1].startswith("done: ")


def test_cloud_chat_failure_still_emits_one_terminal_marker(monkeypatch):
    class Repository:
        @classmethod
        async def create(cls):
            return object()

    class Service:
        def __init__(self, **kwargs):
            pass

        async def run_turn(self, runtime, request):
            raise ProvisioningError(
                "hermes_broker_prototype_auth_failed",
                retryable=False,
            )

    async def no_context(*args, **kwargs):
        return []

    async def no_temporal(*args, **kwargs):
        return ("requested window", [])

    runtime = SimpleNamespace(provider="hermes_cloud", binding_id="binding-a")
    monkeypatch.setattr(chat, "EllaProvisioningRepository", Repository)
    monkeypatch.setattr(chat, "HermesCloudRuntimeService", Service)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_context)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)

    async def collect():
        return [
            item
            async for item in chat._stream_hermes_cloud_chat(
                "Synthetic hello",
                "user-a",
                {"synthetic": True},
                turn_id="turn-error-a",
                client_sent_at=chat.datetime.now(chat.timezone.utc),
                runtime=runtime,
            )
        ]

    output = asyncio.run(collect())
    assert output[0] == ("data: Ella is temporarily unavailable. Please try again.\n\n")
    assert len([item for item in output if item.startswith("done: ")]) == 1
