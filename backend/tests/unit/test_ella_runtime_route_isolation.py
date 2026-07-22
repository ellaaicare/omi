import asyncio
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

sys.modules.setdefault("websockets", ModuleType("websockets"))
conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value: value
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import chat, resolve, voice
from ella.services.provisioning import ProvisioningError


def _request():
    return Request({"type": "http", "method": "POST", "path": "/v1/ella/chat/stream", "headers": []})


def test_chat_rejects_body_uid_that_differs_from_firebase_subject():
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


def test_chat_history_rejects_query_uid_that_differs_from_firebase_subject():
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.ella_chat_history("user-b", authenticated_uid="user-a"))
    assert error.value.status_code == 403


def test_isolated_history_never_uses_openclaw_fallback(monkeypatch):
    async def fake_runtime(uid):
        assert uid == "user-a"
        return SimpleNamespace(profile_name="omi-user-a")

    async def no_events(uid, *, limit, before=None):
        return []

    async def forbidden_legacy(uid):
        raise AssertionError("OpenClaw fallback must not run in isolated mode")

    monkeypatch.setattr(chat, "runtime_bindings_enabled", lambda uid=None: True)
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


def test_isolated_history_fails_closed_without_binding(monkeypatch):
    async def missing_runtime(uid):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)

    monkeypatch.setattr(chat, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", missing_runtime)

    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.ella_chat_history("user-a", authenticated_uid="user-a"))
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "hermes_not_provisioned"}


def test_public_resolver_is_authenticated_and_redacts_internal_runtime(monkeypatch):
    async def fake_resolve(uid):
        assert uid == "user-a"
        return {
            "user": {"id": "1", "omiUid": uid, "name": "A"},
            "routing": {
                "agentId": "hermes",
                "gatewayUrl": "http://100.76.138.56:8701",
                "token": "secret",
                "profileName": "omi-user-a",
                "bindingRevision": 3,
                "modelPolicyVersion": "frontier-v1",
                "voicePolicyVersion": "ella-voice-v1",
            },
        }

    monkeypatch.setattr(resolve, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(resolve, "resolve_user_routing", fake_resolve)

    result = asyncio.run(resolve.resolve_endpoint(uid="user-a", email=None, phone=None, authenticated_uid="user-a"))

    assert result["routing"] == {
        "agentId": "hermes",
        "historyUrl": "/v1/ella/chat/history",
        "platform": "hermes",
        "bindingRevision": 3,
        "modelPolicyVersion": "frontier-v1",
        "voicePolicyVersion": "ella-voice-v1",
    }
    assert "secret" not in str(result)
    assert "gatewayUrl" not in result["routing"]
    assert "profileName" not in result["routing"]


def test_public_resolver_rejects_cross_user_and_email_lookup():
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(resolve.resolve_endpoint(uid="user-b", email=None, phone=None, authenticated_uid="user-a"))
    assert mismatch.value.status_code == 403

    with pytest.raises(HTTPException) as unsupported:
        asyncio.run(resolve.resolve_endpoint(uid=None, email="a@example.com", phone=None, authenticated_uid="user-a"))
    assert unsupported.value.status_code == 400


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
    async def missing_runtime(uid):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
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
    async def ready_runtime(uid):
        return object()

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", ready_runtime)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.create_voice_session(
                body=voice.VoiceSessionRequest(uid="user-a", provider="grok-voice"),
                authenticated_uid="user-a",
            )
        )
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "isolated_voice_not_ready"}


def test_voice_session_issues_isolated_token_for_enabled_uid_canary(monkeypatch):
    async def ready_runtime(uid):
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
