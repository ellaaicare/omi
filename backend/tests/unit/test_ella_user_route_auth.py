"""Production-route authentication tests for Ella user and service boundaries."""

import asyncio
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault("websockets", types.SimpleNamespace())
sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, create_pool=None))
conversations_module = types.ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda conversation, _uid: conversation
sys.modules.setdefault("database.conversations", conversations_module)
app_settings_module = types.ModuleType("database.app_settings")
app_settings_module.get_voice_settings = lambda _uid: {}
app_settings_module.save_voice_settings = lambda _uid, voice_settings: voice_settings
sys.modules.setdefault("database.app_settings", app_settings_module)

from ella.routers import chat, guardian, resolve, voice
from ella.services.hermes_session import canonical_omi_session_key
from utils.ella import exact_firebase_auth


class _VoicePool:
    def __init__(self):
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return None


class _VoiceContextPool(_VoicePool):
    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return {
            "id": "user-row-a",
            "name": "Owner A",
            "conditions": ["owner-condition"],
            "medications": ["owner-medication"],
            "guardian_mode": "OFF",
            "agents": {
                "userAgentId": "private-agent",
                "gatewayUrl": "https://private-gateway.invalid",
                "gatewayToken": "private-routing-value",
                "workspace": "/profiles/uid-a/workspace",
            },
        }

    async def fetch(self, query, *args):
        return []


class _ResolvePool(_VoicePool):
    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return {
            "omi_uid": "uid-a",
            "status": "active",
            "agents": {
                "userAgentId": "private-agent",
                "gatewayToken": "private-routing-value",
                "workspace": "/profiles/uid-a/workspace",
            },
            "cluster_status": "ready",
        }


class _ResolveMissingWorkspacePool(_ResolvePool):
    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return {
            "omi_uid": "uid-a",
            "status": "active",
            "agents": {"userAgentId": "private-agent", "gatewayToken": "private-routing-value"},
            "cluster_status": "ready",
        }


class _GuardianPool(_VoicePool):
    def __init__(self):
        super().__init__()
        self.fetch_calls = []
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return 0

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"

    @property
    def call_count(self):
        return len(self.fetchrow_calls) + len(self.fetch_calls) + len(self.fetchval_calls) + len(self.execute_calls)


def _client(monkeypatch):
    def verify_token(token):
        if token == "valid-a":
            return {"uid": "uid-a"}
        raise ValueError("expired or invalid")

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify_token)
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(voice.router)
    app.include_router(resolve.router)
    app.include_router(guardian.router)
    return TestClient(app)


def test_chat_owner_token_supplies_subject_without_caller_uid(monkeypatch):
    seen_uids = []

    async def stream(_message, uid, *_args, **_kwargs):
        seen_uids.append(uid)
        yield "done: e30=\n\n"

    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(chat, "CHAT_PLATFORM", "hermes")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/ella/chat/stream",
        headers={"Authorization": "Bearer valid-a"},
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert seen_uids == ["uid-a"]


def test_voice_owner_token_supplies_subject_without_caller_uid(monkeypatch):
    pool = _VoicePool()
    token_claims = []

    def create_token(**kwargs):
        token_claims.append(kwargs)
        return f"token-for-{kwargs['uid']}"

    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "create_session_token", create_token)
    monkeypatch.setitem(voice.V2V_PROVIDERS["grok-voice"], "key_check", lambda: True)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/session",
        headers={"Authorization": "Bearer valid-a"},
        json={"provider": "grok-voice"},
    )

    assert response.status_code == 200
    assert response.json()["session_token"] == "token-for-uid-a"
    assert response.json()["session_id"]
    assert token_claims[0]["session_id"] == response.json()["session_id"]
    assert pool.fetchrow_calls[0][1] == ("uid-a",)


def test_voice_session_token_matches_active_proxy_required_claims_and_returns_jti(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "proxy-contract-secret-32-bytes-minimum")
    monkeypatch.setitem(voice.V2V_PROVIDERS["grok-voice"], "key_check", lambda: True)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/session",
        headers={"Authorization": "Bearer valid-a"},
        json={"provider": "grok-voice"},
    )

    assert response.status_code == 200
    payload = response.json()
    claims = voice.jwt.decode(
        payload["session_token"],
        "proxy-contract-secret-32-bytes-minimum",
        algorithms=["HS256"],
        issuer="omi-backend",
        audience="ella-voice-proxy",
        options={
            "require": [
                "exp",
                "iat",
                "iss",
                "aud",
                "sub",
                "uid",
                "jti",
                "voice_mode",
                "provider",
                "isolated_runtime",
                "correlation_id",
                "entitlement_revision",
            ]
        },
    )
    assert payload["session_id"] == claims["jti"]
    assert claims["sub"] == claims["uid"] == "uid-a"
    assert claims["aud"] == "ella-voice-proxy"
    assert claims["voice_mode"] == "v4"
    assert claims["provider"] == "grok-voice"
    assert claims["isolated_runtime"] is False
    assert claims["entitlement_revision"] == 1
    assert isinstance(claims["correlation_id"], str) and claims["correlation_id"]
    assert "session_id" not in claims


def test_user_routes_reject_uid_mismatch_before_provider_or_context_work(monkeypatch):
    pool = _VoicePool()
    chat_calls = []

    async def stream(*_args, **_kwargs):
        chat_calls.append(True)
        yield "done: e30=\n\n"

    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(voice, "_pool", pool)
    client = _client(monkeypatch)

    chat_response = client.post(
        "/v1/ella/chat/stream",
        headers={"Authorization": "Bearer valid-a"},
        json={"uid": "uid-b", "message": "hello"},
    )
    history_response = client.get(
        "/v1/ella/chat/history?uid=uid-b",
        headers={"Authorization": "Bearer valid-a"},
    )
    voice_body_response = client.post(
        "/v1/voice/session",
        headers={"Authorization": "Bearer valid-a"},
        json={"uid": "uid-b", "provider": "grok-voice"},
    )
    voice_query_response = client.post(
        "/v1/voice/session?uid=uid-b",
        headers={"Authorization": "Bearer valid-a"},
        json={"provider": "grok-voice"},
    )

    assert [
        chat_response.status_code,
        history_response.status_code,
        voice_body_response.status_code,
        voice_query_response.status_code,
    ] == [403, 403, 403, 403]
    assert chat_calls == []
    assert pool.fetchrow_calls == []


def test_user_routes_reject_missing_or_expired_token_before_work(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    client = _client(monkeypatch)

    for headers in ({}, {"Authorization": "Bearer expired"}):
        assert client.post("/v1/ella/chat/stream", headers=headers, json={"message": "hello"}).status_code == 401
        assert client.get("/v1/ella/chat/history", headers=headers).status_code == 401
        assert client.post("/v1/voice/session", headers=headers, json={"provider": "grok-voice"}).status_code == 401
        assert client.post("/v1/voice/tts", headers=headers, json={"text": "hello"}).status_code == 401

    assert pool.fetchrow_calls == []


def test_voice_tts_allows_only_explicit_internal_guardian_service_key(monkeypatch):
    monkeypatch.setattr(voice, "ELLA_INTERNAL_TTS_KEY", "internal-key")
    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "guardian-key")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/tts",
        headers={"X-Ella-TTS-Key": "internal-key", "X-TTS-Provider": "grok-voice"},
        json={"text": "hello"},
    )
    broad_guardian_denied = client.post(
        "/v1/voice/tts",
        headers={"X-Guardian-Key": "guardian-key", "X-TTS-Provider": "grok-voice"},
        json={"text": "hello"},
    )

    assert response.status_code == 422
    assert broad_guardian_denied.status_code == 401


def test_voice_tts_internal_service_key_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(voice, "ELLA_INTERNAL_TTS_KEY", "")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/tts",
        headers={"X-Ella-TTS-Key": "unconfigured-key", "X-TTS-Provider": "grok-voice"},
        json={"text": "hello"},
    )

    assert response.status_code == 401


def test_resolve_requires_exact_owner_before_lookup_and_returns_no_private_routing(monkeypatch):
    pool = _ResolvePool()
    monkeypatch.setattr(resolve, "_pool", pool)
    client = _client(monkeypatch)

    assert client.get("/v1/ella/resolve?uid=uid-a").status_code == 401
    assert (
        client.get(
            "/v1/ella/resolve?uid=uid-b",
            headers={"Authorization": "Bearer valid-a"},
        ).status_code
        == 403
    )
    assert pool.fetchrow_calls == []

    response = client.get(
        "/v1/ella/resolve?uid=uid-a",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert pool.fetchrow_calls[0][1] == ("uid-a",)
    payload = response.json()
    assert payload == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": True, "clusterStatus": "ready", "platform": "openclaw"},
    }
    serialized = str(payload).lower()
    for forbidden in ("token", "session", "agentid", "workspace", "condition", "medication", "provision"):
        assert forbidden not in serialized


def test_resolve_missing_workspace_fails_closed_without_retained_fallback_or_secrets(monkeypatch):
    pool = _ResolveMissingWorkspacePool()
    monkeypatch.setattr(resolve, "_pool", pool)
    monkeypatch.setattr(resolve, "CHAT_PLATFORM", "hermes")
    client = _client(monkeypatch)

    response = client.get(
        "/v1/ella/resolve?uid=uid-a",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": False, "clusterStatus": "ready", "platform": "hermes"},
    }
    serialized = str(response.json()).lower()
    for forbidden in ("token", "session", "agentid", "workspace", "condition", "medication", "provision"):
        assert forbidden not in serialized


def test_resolved_hermes_routing_uses_exact_v2_owner_key_without_legacy_fallback(monkeypatch):
    class CaseResolvePool:
        async def fetchrow(self, _query, *args):
            assert args == ("CaseUID",)
            return {
                "id": "case-owner-row",
                "name": "Case Owner",
                "omi_uid": "CaseUID",
                "status": "active",
                "guardian_mode": "OFF",
                "timezone": "America/Los_Angeles",
                "conditions": [],
                "medications": [],
                "agents": {"userAgentId": "case-agent", "workspace": "/profiles/CaseUID/workspace"},
                "cluster_status": "ready",
            }

    monkeypatch.setattr(resolve, "_pool", CaseResolvePool())
    monkeypatch.setattr(resolve, "CHAT_PLATFORM", "hermes")

    resolved = asyncio.run(resolve.resolve_user_routing("CaseUID"))

    assert resolved["routing"]["sessionKey"] == canonical_omi_session_key("CaseUID")
    assert resolved["routing"]["sessionKey"] != "ella:omi:caseuid:canonical"
    assert "CaseUID" not in resolved["routing"]["sessionKey"]


def test_voice_alternate_routes_reject_missing_and_wrong_subject_before_downstream_work(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "ELLA_INTERNAL_VOICE_CONTEXT_KEY", "context-service")
    monkeypatch.setattr(voice, "ELLA_INTERNAL_VOICE_SEARCH_KEY", "search-service")
    client = _client(monkeypatch)
    cases = [
        ("/v1/voice/context", {"uid": "uid-b"}),
        ("/v1/voice/search-omi", {"uid": "uid-b", "query": "private"}),
        (
            "/v1/voice/search",
            {"uid": "uid-b", "query": "private", "agent_role": "caregiver", "agent_id": "chosen-agent"},
        ),
    ]

    for path, body in cases:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers={"Authorization": "Bearer valid-a"}, json=body).status_code == 403
        assert pool.fetchrow_calls == []


def test_voice_internal_route_credentials_are_narrow_and_fail_closed_before_work(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "ELLA_INTERNAL_VOICE_CONTEXT_KEY", "context-service")
    monkeypatch.setattr(voice, "ELLA_INTERNAL_VOICE_SEARCH_KEY", "search-service")
    client = _client(monkeypatch)

    wrong_scope = client.post(
        "/v1/voice/context",
        headers={"X-Ella-Voice-Context-Key": "search-service"},
        json={"uid": "uid-a"},
    )
    unset_scope = client.post(
        "/v1/voice/search-omi",
        headers={"X-Ella-Caregiver-Search-Key": "not-configured"},
        json={"uid": "uid-a", "query": "private"},
    )

    assert wrong_scope.status_code == 403
    assert unset_scope.status_code == 403
    assert pool.fetchrow_calls == []


def test_voice_context_owner_response_never_contains_runtime_credentials(monkeypatch):
    pool = _VoiceContextPool()

    async def no_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "PROVISION_API_TOKEN", "")
    monkeypatch.setattr(voice, "_fetch_recent_conversations", no_context)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", no_context)
    monkeypatch.setattr(voice, "_fetch_memory_context", no_context)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/context",
        headers={"Authorization": "Bearer valid-a"},
        json={"uid": "uid-a"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["conditions"] == ["owner-condition"]
    assert payload["medications"] == ["owner-medication"]
    assert "gateway_token" not in payload
    assert "private-routing-value" not in str(payload)


def test_unified_voice_search_success_logs_never_include_query_content(monkeypatch, caplog):
    sentinel = "SENTINEL private transcript phrase success"

    async def search_timeline(_uid, _query, _limit):
        return [{"source": "timeline", "content": "safe result", "score": 1, "metadata": {}}]

    monkeypatch.setattr(voice, "_pool", _VoicePool())
    monkeypatch.setattr(voice, "_search_canonical_timeline", search_timeline)
    caplog.set_level("INFO", logger=voice.__name__)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/search",
        headers={"Authorization": "Bearer valid-a"},
        json={"query": sentinel, "sources": ["timeline"]},
    )

    assert response.status_code == 200
    assert response.json()["total_results"] == 1
    assert sentinel not in caplog.text


def test_unified_voice_search_failure_logs_never_include_query_or_exception_content(monkeypatch, caplog):
    sentinel = "SENTINEL private transcript phrase failure"

    async def failing_timeline(_uid, _query, _limit):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(voice, "_pool", _VoicePool())
    monkeypatch.setattr(voice, "_search_canonical_timeline", failing_timeline)
    caplog.set_level("INFO", logger=voice.__name__)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/search",
        headers={"Authorization": "Bearer valid-a"},
        json={"query": sentinel, "sources": ["timeline"]},
    )

    assert response.status_code == 200
    assert response.json()["total_results"] == 0
    assert "error_class=RuntimeError" in caplog.text
    assert sentinel not in caplog.text


def test_guardian_alternate_routes_reject_missing_and_wrong_subject_without_side_effect(monkeypatch):
    pool = _GuardianPool()
    monkeypatch.setattr(guardian, "_pool", pool)
    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "guardian-service")
    monkeypatch.setattr(guardian, "_playback_events", {})
    client = _client(monkeypatch)
    cases = [
        ("GET", "/v1/ella/guardian/queue?uid=uid-b", None),
        ("POST", "/v1/ella/guardian/activate", {"uid": "uid-b"}),
        ("GET", "/v1/ella/guardian/trace/trace-a?uid=uid-b", None),
        (
            "POST",
            "/v1/ella/guardian/playback-debug",
            {"uid": "uid-b", "event_name": "received", "trace_id": "trace-a"},
        ),
    ]

    for method, path, body in cases:
        missing = client.request(method, path, json=body)
        mismatch = client.request(method, path, headers={"Authorization": "Bearer valid-a"}, json=body)
        assert missing.status_code == 401, path
        assert mismatch.status_code == 403, path
        assert pool.call_count == 0, path
    assert guardian._playback_events == {}


def test_guardian_trace_log_requires_configured_service_key_before_state_work(monkeypatch):
    pool = _GuardianPool()
    monkeypatch.setattr(guardian, "_pool", pool)
    client = _client(monkeypatch)
    payload = {"trace_id": "trace-a", "uid": "uid-a", "stage": "scanner_classified"}

    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "")
    assert client.post("/v1/ella/guardian/trace/log", json=payload).status_code == 403
    monkeypatch.setattr(guardian, "GUARDIAN_WEBHOOK_KEY", "guardian-service")
    assert (
        client.post(
            "/v1/ella/guardian/trace/log",
            headers={"X-Guardian-Key": "wrong-service"},
            json=payload,
        ).status_code
        == 403
    )
    assert pool.call_count == 0
