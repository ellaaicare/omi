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


def test_memory_scoped_voice_session_resolves_server_context_and_signs_ids(monkeypatch):
    async def ready_runtime(uid):
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
