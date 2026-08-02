import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType, SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException, Request

sys.modules.setdefault("websockets", ModuleType("websockets"))
sys.modules.setdefault("database.proposals", ModuleType("database.proposals"))
conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value, uid=None: value
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import voice
from ella.services import correction_honcho_contract as honcho_contract

RUNTIME_AUTHORITY_DIGEST = "a" * 64


def _request(body: dict, *, token: str = "", service_token: str = "") -> Request:
    raw = json.dumps(body).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode("utf-8")))
    if service_token:
        headers.append((b"x-ella-voice-proxy-token", service_token.encode("utf-8")))

    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/v1/voice/test", "headers": headers},
        receive,
    )


def _token(
    uid: str,
    *,
    isolated: bool = True,
    expired: bool = False,
    scope: dict | None = None,
    provider: str = "grok-voice",
    voice_mode: str = "v4",
    runtime_authority_digest: str = RUNTIME_AUTHORITY_DIGEST,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": uid,
        "uid": uid,
        "firebase_uid": uid,
        "provider": provider,
        "voice_mode": voice_mode,
        "isolated_runtime": isolated,
        "jti": f"session-{uid}",
        "correlation_id": f"correlation-{uid}",
        "entitlement_revision": 3,
        "runtime_authority_digest": runtime_authority_digest if isolated else "",
        "aud": voice.VOICE_SESSION_AUDIENCE,
        "iss": "omi-backend",
        "iat": now - timedelta(minutes=2),
        "exp": now - timedelta(minutes=1) if expired else now + timedelta(minutes=10),
    }
    if scope:
        claims.update(scope)
    return jwt.encode(
        claims,
        voice.ELLA_SESSION_SECRET,
        algorithm="HS256",
    )


def _legacy_token(uid: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "uid": uid,
            "firebase_uid": uid,
            "iss": "omi-backend",
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        voice.ELLA_SESSION_SECRET,
        algorithm="HS256",
    )


@pytest.fixture(autouse=True)
def voice_auth(monkeypatch):
    monkeypatch.setattr(voice, "ELLA_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setattr(voice, "VOICE_PROXY_SERVICE_TOKEN", "test-proxy-secret")
    monkeypatch.setattr(voice, "HERMES_PROVISION_API_URL", "http://hermes-8210")
    monkeypatch.setattr(voice, "HERMES_PROVISION_API_TOKEN", "test-hermes-secret")
    monkeypatch.setattr(voice, "ALLOW_LEGACY_VOICE_SESSION_TOKENS", True)
    monkeypatch.setattr(
        voice,
        "runtime_authority_identity",
        lambda _runtime: SimpleNamespace(digest=RUNTIME_AUTHORITY_DIGEST),
    )


def test_voice_session_token_has_firebase_subject_and_proxy_audience():
    encoded = voice.create_session_token(
        uid="uid-a",
        firebase_uid="uid-a",
        voice_mode="v4",
        provider="grok-voice",
        isolated_runtime=True,
        runtime_authority_digest=RUNTIME_AUTHORITY_DIGEST,
    )

    claims = jwt.decode(
        encoded,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )

    assert claims["sub"] == "uid-a"
    assert claims["uid"] == "uid-a"
    assert claims["isolated_runtime"] is True
    assert claims["jti"]
    assert claims["correlation_id"]
    assert claims["entitlement_revision"] == 1


def test_voice_session_token_binds_memory_scope_without_memory_content():
    encoded = voice.create_session_token(
        uid="uid-a",
        firebase_uid="uid-a",
        voice_mode="v4",
        provider="grok-voice",
        isolated_runtime=True,
        runtime_authority_digest=RUNTIME_AUTHORITY_DIGEST,
        session_id="session-a",
        session_scope={
            "kind": "memory",
            "conversation_id": "memory-a",
            "active_summary_version_id": "version-3",
            "can_reinterpret": True,
            "title": "Must not enter token",
        },
    )
    claims = jwt.decode(
        encoded,
        voice.ELLA_SESSION_SECRET,
        algorithms=["HS256"],
        issuer="omi-backend",
        audience=voice.VOICE_SESSION_AUDIENCE,
    )

    assert claims["jti"] == "session-a"
    assert claims["scope_kind"] == "memory"
    assert claims["conversation_id"] == "memory-a"
    assert claims["active_summary_version_id"] == "version-3"
    assert claims["can_reinterpret"] is True
    assert "title" not in claims


def test_voice_session_token_rejects_empty_memory_version():
    with pytest.raises(ValueError, match="active summary version"):
        voice.create_session_token(
            uid="uid-a",
            firebase_uid="uid-a",
            voice_mode="v4",
            provider="grok-voice",
            isolated_runtime=True,
            runtime_authority_digest=RUNTIME_AUTHORITY_DIGEST,
            session_scope={
                "kind": "memory",
                "conversation_id": "legacy-memory",
                "active_summary_version_id": "",
                "can_reinterpret": False,
            },
        )


@pytest.mark.parametrize("legacy_mode", ["v1", "v2", "v3-fast"])
def test_voice_session_token_rejects_legacy_memory_scope(legacy_mode):
    with pytest.raises(ValueError, match="memory_scoped_voice_mode_required"):
        voice.create_session_token(
            uid="uid-a",
            firebase_uid="uid-a",
            voice_mode=legacy_mode,
            provider="grok-voice",
            isolated_runtime=False,
            session_scope={
                "kind": "memory",
                "conversation_id": "memory-a",
                "active_summary_version_id": "version-3",
                "can_reinterpret": False,
            },
        )


@pytest.mark.parametrize(
    ("provider", "mode", "expected_code"),
    [
        (
            "openai-native-realtime",
            "openai-native-realtime-v1",
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
def test_voice_session_token_rejects_invalid_memory_scoped_provider_mode_pair(
    provider,
    mode,
    expected_code,
):
    with pytest.raises(ValueError, match=expected_code):
        voice.create_session_token(
            uid="uid-a",
            firebase_uid="uid-a",
            voice_mode=mode,
            provider=provider,
            isolated_runtime=False,
            session_scope={
                "kind": "memory",
                "conversation_id": "memory-a",
                "active_summary_version_id": "version-3",
                "can_reinterpret": False,
            },
        )


def test_voice_proxy_rejects_partial_scope_claims():
    token = _token(
        "uid-a",
        scope={"scope_kind": "memory", "conversation_id": "memory-a"},
    )

    with pytest.raises(HTTPException) as error:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=token,
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "voice_session_invalid"}


def test_voice_proxy_rejects_empty_memory_version_claim():
    token = _token(
        "uid-a",
        scope={
            "scope_kind": "memory",
            "conversation_id": "memory-a",
            "active_summary_version_id": "",
            "can_reinterpret": False,
        },
    )

    with pytest.raises(HTTPException) as error:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=token,
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "voice_session_invalid"}


def test_voice_proxy_accepts_complete_memory_scope_claims():
    token = _token(
        "uid-a",
        scope={
            "scope_kind": "memory",
            "conversation_id": "memory-a",
            "active_summary_version_id": "version-3",
            "can_reinterpret": False,
        },
    )

    principal = voice.authenticate_voice_proxy_request(
        _request(
            {"uid": "uid-a"},
            token=token,
            service_token="test-proxy-secret",
        ),
        "uid-a",
    )

    assert principal.scope_kind == "memory"
    assert principal.conversation_id == "memory-a"
    assert principal.active_summary_version_id == "version-3"
    assert principal.can_reinterpret is False


def test_voice_proxy_rechecks_current_consent_after_token_issuance(monkeypatch):
    def reject_revoked(uid):
        assert uid == "uid-a"
        raise HTTPException(
            status_code=403,
            detail={"code": "ai_consent_required", "decision": "revoked"},
        )

    monkeypatch.setattr(voice, "assert_current_ai_consent", reject_revoked)

    with pytest.raises(HTTPException) as error:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=_token("uid-a"),
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )

    assert error.value.status_code == 403
    assert error.value.detail == {"code": "ai_consent_required", "decision": "revoked"}


def test_self_hosted_voice_proxy_re_resolves_exact_voice_target(monkeypatch):
    runtime = SimpleNamespace(
        agent_id="isolated-agent-a",
        revision=7,
        runtime_target_mode="hermes-voice",
    )
    resolved = []
    provider_http_calls = []

    async def resolve(uid, **kwargs):
        resolved.append((uid, kwargs))
        return runtime

    monkeypatch.setattr(voice, "self_hosted_provisioning_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "cloud_provisioning_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "runtime_authority_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)

    with pytest.raises(HTTPException) as unpinned:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=_token(
                    "uid-a",
                    provider="hermes",
                    voice_mode="hermes-voice",
                    runtime_authority_digest="",
                ),
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )
    assert unpinned.value.status_code == 401
    assert resolved == []

    principal = voice.authenticate_voice_proxy_request(
        _request(
            {"uid": "uid-a"},
            token=_token("uid-a", provider="hermes", voice_mode="hermes-voice"),
            service_token="test-proxy-secret",
        ),
        "uid-a",
    )
    current = asyncio.run(voice._resolve_voice_runtime(principal))

    assert current is runtime
    assert resolved == [("uid-a", {"target_mode": "hermes-voice"})]

    class ForbiddenAsyncClient:
        def __init__(self, *args, **kwargs):
            provider_http_calls.append(("forbidden", args, kwargs))
            raise AssertionError("provider HTTP must not be constructed for a session mismatch")

    monkeypatch.setattr(voice.httpx, "AsyncClient", ForbiddenAsyncClient)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(
            voice.execute_voice_tool(
                _request(
                    {
                        "uid": "uid-a",
                        "session_id": "attacker-selected-session",
                        "tool_name": "ask_ella",
                        "arguments": {"query": "hello"},
                    },
                    token=_token("uid-a", provider="hermes", voice_mode="hermes-voice"),
                    service_token="test-proxy-secret",
                )
            )
        )
    assert mismatch.value.status_code == 403
    assert mismatch.value.detail == {"code": "voice_session_claim_mismatch"}
    assert provider_http_calls == []
    assert resolved == [("uid-a", {"target_mode": "hermes-voice"})]

    class ProviderResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"answer": "synthetic answer"}

    class RecordingAsyncClient:
        def __init__(self, *args, **kwargs):
            provider_http_calls.append(("init", args, kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            provider_http_calls.append(("post", url, headers, json))
            return ProviderResponse()

    monkeypatch.setattr(voice.httpx, "AsyncClient", RecordingAsyncClient)
    result = asyncio.run(
        voice.execute_voice_tool(
            _request(
                {
                    "uid": "uid-a",
                    "session_id": "session-uid-a",
                    "tool_name": "ask_ella",
                    "arguments": {"query": "hello"},
                },
                token=_token("uid-a", provider="hermes", voice_mode="hermes-voice"),
                service_token="test-proxy-secret",
            )
        )
    )
    assert result["answer"] == "synthetic answer"
    posts = [call for call in provider_http_calls if call[0] == "post"]
    assert len(posts) == 1
    assert posts[0][3]["session_id"] == "session-uid-a"


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("authority_digest", "voice_runtime_authority_changed"),
        ("provider", "self_hosted_voice_claim_stale"),
        ("mode", "self_hosted_voice_claim_stale"),
        ("model", "self_hosted_voice_claim_stale"),
        ("session", "voice_session_claim_mismatch"),
        ("correlation", "voice_session_claim_mismatch"),
    ],
)
def test_self_hosted_voice_accept_drift_fails_before_session_or_provider_call(monkeypatch, drift, expected_code):
    runtime = SimpleNamespace(runtime_target_mode="hermes-voice")
    accept_calls = []

    async def resolve(_uid, **_kwargs):
        return runtime

    async def forbidden_accept(**kwargs):
        accept_calls.append(kwargs)
        raise AssertionError("provider/session acceptance must not run")

    provider = "gemini-live" if drift == "provider" else "hermes"
    mode = "v4" if drift == "mode" else "hermes-voice"
    model = "drifted-model" if drift == "model" else voice.SELF_HOSTED_RUNTIME_MODEL
    if drift == "authority_digest":
        monkeypatch.setattr(
            voice,
            "runtime_authority_identity",
            lambda _runtime: SimpleNamespace(digest="b" * 64),
        )
    monkeypatch.setattr(voice, "VOICE_CANARY_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(voice, "self_hosted_provisioning_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "cloud_provisioning_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "runtime_authority_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice.voice_canary_db, "accept_session", forbidden_accept)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.accept_voice_canary_session(
                voice.VoiceCanaryAcceptRequest(
                    uid="uid-a",
                    session_id="drifted-session" if drift == "session" else "session-uid-a",
                    correlation_id="drifted-correlation" if drift == "correlation" else "correlation-uid-a",
                    model=model,
                ),
                _request(
                    {"uid": "uid-a"},
                    token=_token("uid-a", provider=provider, voice_mode=mode),
                    service_token="test-proxy-secret",
                ),
            )
        )

    assert error.value.status_code == (403 if drift in {"session", "correlation"} else 409)
    assert error.value.detail == {"code": expected_code}
    assert accept_calls == []


@pytest.mark.parametrize("endpoint", ["context", "search", "tool"])
def test_two_uid_request_is_denied_before_any_data_lookup(endpoint):
    bodies = {
        "context": {"uid": "uid-b"},
        "search": {"uid": "uid-b", "query": "morning", "sources": ["timeline"]},
        "tool": {"uid": "uid-b", "tool_name": "ask_ella", "arguments": {"query": "hello"}},
    }
    handlers = {
        "context": voice.get_voice_context,
        "search": voice.unified_search,
        "tool": voice.execute_voice_tool,
    }
    request = _request(
        bodies[endpoint],
        token=_token("uid-a"),
        service_token="test-proxy-secret",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(handlers[endpoint](request))

    assert error.value.status_code == 403
    assert error.value.detail == {"code": "voice_session_ownership_mismatch"}


def test_voice_proxy_requires_service_secret_and_unexpired_session():
    with pytest.raises(HTTPException) as missing_service:
        voice.authenticate_voice_proxy_request(
            _request({"uid": "uid-a"}, token=_token("uid-a")),
            "uid-a",
        )
    assert missing_service.value.status_code == 403

    with pytest.raises(HTTPException) as expired:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=_token("uid-a", expired=True),
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )
    assert expired.value.status_code == 401
    assert expired.value.detail == {"code": "voice_session_expired"}


def test_voice_proxy_legacy_bridge_is_bounded_to_nonisolated_tokens(monkeypatch):
    legacy = voice.authenticate_voice_proxy_request(
        _request(
            {"uid": "uid-a"},
            token=_legacy_token("uid-a"),
            service_token="test-proxy-secret",
        ),
        "uid-a",
    )
    assert legacy.uid == "uid-a"
    assert legacy.isolated_runtime is False

    monkeypatch.setattr(voice, "ALLOW_LEGACY_VOICE_SESSION_TOKENS", False)
    with pytest.raises(HTTPException) as disabled:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=_legacy_token("uid-a"),
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )
    assert disabled.value.status_code == 401

    now = datetime.now(timezone.utc)
    partial_modern = jwt.encode(
        {
            "uid": "uid-a",
            "sub": "uid-a",
            "iss": "omi-backend",
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        voice.ELLA_SESSION_SECRET,
        algorithm="HS256",
    )
    monkeypatch.setattr(voice, "ALLOW_LEGACY_VOICE_SESSION_TOKENS", True)
    with pytest.raises(HTTPException) as incomplete:
        voice.authenticate_voice_proxy_request(
            _request(
                {"uid": "uid-a"},
                token=partial_modern,
                service_token="test-proxy-secret",
            ),
            "uid-a",
        )
    assert incomplete.value.status_code == 401


@pytest.mark.parametrize(
    ("bindings_enabled", "voice_enabled", "isolated_claim"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
def test_voice_runtime_rechecks_rollout_gate_on_every_request(
    monkeypatch,
    bindings_enabled,
    voice_enabled,
    isolated_claim,
):
    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: bindings_enabled)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: voice_enabled)
    principal = voice.VoiceProxyPrincipal(
        uid="uid-a",
        session_id="session-uid-a",
        provider="grok-voice",
        voice_mode="v4",
        isolated_runtime=isolated_claim,
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(voice._resolve_voice_runtime(principal))

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "voice_runtime_claim_stale"}


def test_isolated_context_uses_active_8210_agent_and_redacts_credentials(monkeypatch):
    runtime = SimpleNamespace(
        uid="uid-a",
        agent_id="ella-uid-a",
        revision=7,
        honcho_workspace="workspace-a",
        observer_peer="ella-a",
        observed_peer="user-a",
    )
    queries = []
    requests = []

    async def resolve(uid, **kwargs):
        assert uid == "uid-a"
        return runtime

    class Pool:
        async def fetchrow(self, query, uid):
            queries.append(query)
            return {
                "id": "db-user-a",
                "name": "User A",
                "conditions": [],
                "medications": [],
                "guardian_mode": "off",
            }

        async def fetch(self, *args):
            return []

    class Response:
        status_code = 200

        def json(self):
            return {"files": [{"name": "SOUL.md", "content": "Bound companion."}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None, **kwargs):
            requests.append((url, headers))
            return Response()

    async def empty(*args, **kwargs):
        return ""

    async def honcho_context(target, *, query, top_k):
        assert target.uid == "uid-a"
        assert target.honcho_workspace == "workspace-a"
        assert target.observer_peer == "ella-a"
        assert target.observed_peer == "user-a"
        assert target.source == "isolated_runtime_receipt"
        assert "Recent themes" in query
        assert top_k == 12
        return {
            "available": True,
            "reason": "ok",
            "context": "Honcho remembers the user's gardening plans.",
            "latency_ms": 42,
            "source": "honcho",
        }

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(voice, "_fetch_recent_conversations", empty)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", empty)
    monkeypatch.setattr(voice, "fetch_voice_honcho_context", honcho_context)
    monkeypatch.setattr(voice.httpx, "AsyncClient", Client)

    result = asyncio.run(
        voice.get_voice_context(
            _request(
                {"uid": "uid-a"},
                token=_token("uid-a"),
                service_token="test-proxy-secret",
            )
        )
    )

    assert all("agent_clusters" not in query for query in queries)
    assert result["user_agent_id"] == "ella-uid-a"
    assert result["runtime"] == {
        "provider": "hermes",
        "agent_id": "ella-uid-a",
        "binding_revision": 7,
        "workspace_residency": "retained_workspace_api",
    }
    assert "gateway_token" not in result
    assert "gateway_url" not in result
    assert result["honcho_context"] == "Honcho remembers the user's gardening plans."
    assert result["honcho_status"]["available"] is True
    assert requests[0][0] == "http://hermes-8210/workspace/ella-uid-a/files"
    assert requests[0][1]["X-Ella-Owner-Uid"] == "uid-a"


def test_cloud_context_never_calls_mini_workspace_api(monkeypatch):
    runtime = SimpleNamespace(
        uid="synthetic-user",
        provider="hermes_cloud",
        agent_id="hermes-cloud",
        revision=2,
        honcho_workspace="cloud-workspace",
        observer_peer="companion-a",
        observed_peer="user-a",
    )

    async def resolve(uid, **kwargs):
        assert uid == "synthetic-user"
        return runtime

    class Pool:
        async def fetchrow(self, query, uid):
            return {
                "id": "db-user-a",
                "name": "Synthetic User",
                "conditions": [],
                "medications": [],
                "guardian_mode": "off",
            }

        async def fetch(self, *args):
            return []

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("cloud context must not construct a Mini workspace client")

    async def empty(*args, **kwargs):
        return ""

    async def honcho_context(target, *, query, top_k):
        return {
            "available": True,
            "reason": "ok",
            "context": "Cloud memory context.",
            "source": "honcho",
        }

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(voice, "_fetch_recent_conversations", empty)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", empty)
    monkeypatch.setattr(voice, "fetch_voice_honcho_context", honcho_context)
    monkeypatch.setattr(voice.httpx, "AsyncClient", ForbiddenClient)

    result = asyncio.run(
        voice.get_voice_context(
            _request(
                {"uid": "synthetic-user"},
                token=_token("synthetic-user"),
                service_token="test-proxy-secret",
            )
        )
    )

    assert result["runtime"]["provider"] == "hermes_cloud"
    assert (
        result["runtime"]["workspace_residency"] == "canonical_postgres+hermes_cloud_profile_memory+hermes_cloud_policy"
    )
    assert result["soul"] == ""
    assert result["user_profile"] == ""
    assert result["honcho_context"] == ""
    assert result["honcho_status"]["reason"] == "hermes_cloud_profile_memory_builtin"


def test_retained_context_loads_uid_mapped_honcho_without_runtime_receipt(monkeypatch):
    target = SimpleNamespace(
        uid="uid-a",
        honcho_workspace="workspace-a",
        observer_peer="ella-a",
        observed_peer="user-a",
        source="profile_map",
    )

    class Pool:
        async def fetchrow(self, query, uid):
            return {
                "id": "db-user-a",
                "name": "User A",
                "conditions": [],
                "medications": [],
                "guardian_mode": "off",
                "agents": {},
            }

        async def fetch(self, *args):
            return []

    async def empty(*args, **kwargs):
        return ""

    async def honcho_context(target_arg, *, query, top_k):
        assert target_arg is target
        return {
            "available": True,
            "reason": "ok",
            "context": "Mapped retained context.",
            "source": "honcho",
        }

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(voice, "_fetch_recent_conversations", empty)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", empty)
    monkeypatch.setattr(voice, "_fetch_memory_context", empty)
    monkeypatch.setattr(
        voice,
        "resolve_voice_honcho_target",
        lambda uid, runtime=None, **kwargs: (target, ""),
    )
    monkeypatch.setattr(voice, "fetch_voice_honcho_context", honcho_context)

    result = asyncio.run(
        voice.get_voice_context(
            _request(
                {"uid": "uid-a"},
                token=_token("uid-a", isolated=False),
                service_token="test-proxy-secret",
            )
        )
    )

    assert result["honcho_context"] == "Mapped retained context."
    assert result["honcho_status"]["available"] is True


def test_retained_context_profile_resolution_timeout_degrades_within_cap(monkeypatch):
    class Pool:
        async def fetchrow(self, query, uid):
            return {
                "id": "db-user-a",
                "name": "User A",
                "conditions": [],
                "medications": [],
                "guardian_mode": "off",
                "agents": {},
            }

        async def fetch(self, *args):
            return []

    async def empty(*args, **kwargs):
        return ""

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.25)
            body = b'{"entries": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "VOICE_HONCHO_PROFILE_RESOLUTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(voice, "VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS", 30)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(voice, "_fetch_recent_conversations", empty)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", empty)
    monkeypatch.setattr(voice, "_fetch_memory_context", empty)
    monkeypatch.setattr(voice, "_VOICE_HONCHO_PROFILE_NEGATIVE_CACHE", {})
    monkeypatch.setattr(honcho_contract, "HONCHO_PROFILE_MAP_JSON", "")
    monkeypatch.setattr(honcho_contract, "HONCHO_PROFILE_MAP_PATH", "")
    monkeypatch.setattr(
        honcho_contract,
        "HONCHO_PROFILE_MAP_URL",
        f"http://127.0.0.1:{server.server_port}/profile-map",
    )
    monkeypatch.setattr(honcho_contract, "HONCHO_PROFILE_UID", "")
    monkeypatch.setattr(honcho_contract, "HONCHO_PROFILE_CONFIG_PATH", "")
    monkeypatch.setattr(
        honcho_contract,
        "_PROFILE_MAP_URL_CACHE",
        {"url": "", "fetched_at": 0.0, "data": None},
    )

    async def load_context():
        started = time.monotonic()
        result = await voice.get_voice_context(
            _request(
                {"uid": "uid-a"},
                token=_token("uid-a", isolated=False),
                service_token="test-proxy-secret",
            )
        )
        return result, time.monotonic() - started

    try:
        result, elapsed = asyncio.run(load_context())
        cached_result, cached_elapsed = asyncio.run(load_context())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=0.2)

    assert elapsed < 0.15
    assert cached_elapsed < 0.15
    assert result["honcho_context"] == ""
    assert result["honcho_status"]["reason"] in {
        "honcho_profile_resolution_timeout",
        "missing_companion_honcho_target",
    }
    assert cached_result["honcho_status"]["reason"] == "honcho_profile_resolution_cached_unavailable"
    assert voice._VOICE_HONCHO_PROFILE_NEGATIVE_CACHE["uid-a"] > time.monotonic()


def test_isolated_search_forces_receipt_agent_and_owner_header(monkeypatch):
    runtime = SimpleNamespace(agent_id="ella-uid-a", revision=8)
    calls = []

    async def resolve(uid, **kwargs):
        return runtime

    async def workspace(uid, agent_id, query, limit, **kwargs):
        calls.append((uid, agent_id, kwargs))
        return [{"source": "workspace", "content": "private-a", "score": 10, "metadata": {}}]

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice, "_search_workspace", workspace)

    result = asyncio.run(
        voice.unified_search(
            _request(
                {"uid": "uid-a", "query": "private", "sources": ["workspace"]},
                token=_token("uid-a"),
                service_token="test-proxy-secret",
            )
        )
    )

    assert result["results"][0]["content"] == "private-a"
    assert calls == [
        (
            "uid-a",
            "ella-uid-a",
            {
                "provision_url": "http://hermes-8210",
                "provision_token": "test-hermes-secret",
                "owner_uid": "uid-a",
            },
        )
    ]


def test_isolated_search_includes_receipt_bound_honcho_source(monkeypatch):
    runtime = SimpleNamespace(
        uid="uid-a",
        agent_id="ella-uid-a",
        revision=8,
        honcho_workspace="workspace-a",
        observer_peer="ella-a",
        observed_peer="user-a",
    )
    calls = []

    async def resolve(uid, **kwargs):
        return runtime

    async def honcho_search(target, query, limit):
        calls.append((target, query, limit))
        return [
            {
                "source": "honcho",
                "title": "Related user context",
                "content": "User A discussed tomato plants.",
                "score": 90,
                "metadata": {"provenance": "honcho_conclusion"},
            }
        ]

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice, "search_voice_honcho", honcho_search)

    result = asyncio.run(
        voice.unified_search(
            _request(
                {
                    "uid": "uid-a",
                    "query": "tomato plants",
                    "sources": ["honcho"],
                },
                token=_token("uid-a"),
                service_token="test-proxy-secret",
            )
        )
    )

    assert result["sources_searched"] == ["honcho"]
    assert result["sources_denied"] == []
    assert result["results"][0]["content"] == "User A discussed tomato plants."
    target, query, limit = calls[0]
    assert query == "tomato plants"
    assert limit == 5
    assert target.uid == "uid-a"
    assert target.honcho_workspace == "workspace-a"
    assert target.observer_peer == "ella-a"
    assert target.observed_peer == "user-a"
    assert target.source == "isolated_runtime_receipt"


def test_retained_search_uses_uid_mapped_honcho_and_unmapped_user_fails_open(monkeypatch):
    target = SimpleNamespace(
        uid="uid-a",
        honcho_workspace="workspace-a",
        observer_peer="ella-a",
        observed_peer="user-a",
        source="profile_map",
    )
    calls = []

    async def binding(uid, runtime=None):
        if uid == "uid-a":
            return target, ""
        return None, "missing_companion_honcho_target"

    async def honcho_search(target_arg, query, limit):
        calls.append((target_arg, query, limit))
        return [
            {
                "source": "honcho",
                "title": "Related user context",
                "content": "Mapped retained result.",
                "score": 90,
                "metadata": {"provenance": "honcho_conclusion"},
            }
        ]

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: False)
    monkeypatch.setattr(voice, "_resolve_voice_honcho_binding", binding)
    monkeypatch.setattr(voice, "search_voice_honcho", honcho_search)

    mapped = asyncio.run(
        voice.unified_search(
            _request(
                {
                    "uid": "uid-a",
                    "agent_id": "ella-uid-a",
                    "query": "garden",
                    "sources": ["honcho"],
                },
                token=_token("uid-a", isolated=False),
                service_token="test-proxy-secret",
            )
        )
    )
    unmapped = asyncio.run(
        voice.unified_search(
            _request(
                {
                    "uid": "uid-b",
                    "agent_id": "ella-uid-b",
                    "query": "garden",
                    "sources": ["honcho"],
                },
                token=_token("uid-b", isolated=False),
                service_token="test-proxy-secret",
            )
        )
    )

    assert mapped["sources_searched"] == ["honcho"]
    assert mapped["results"][0]["content"] == "Mapped retained result."
    assert calls == [(target, "garden", 5)]
    assert unmapped["results"] == []
    assert unmapped["sources_searched"] == []
    assert unmapped["sources_denied"] == ["honcho"]


def test_canonical_startup_and_search_require_exact_uid_and_matching_signed_memory_scope(monkeypatch):
    rows = [
        {
            "uid": "UserA",
            "channel": "ios_voice",
            "provider": "grok-realtime",
            "role": "user",
            "text": "General garden plans.",
            "started_at": datetime(2026, 7, 24, 10, 0),
            "session_id": "general-session",
            "source_ref": {},
            "metadata": {},
        },
        {
            "uid": "UserA",
            "channel": "ios_voice",
            "provider": "grok-realtime",
            "role": "user",
            "text": "Memory A garden detail.",
            "started_at": datetime(2026, 7, 24, 10, 1),
            "session_id": "scoped-session",
            "source_ref": {"scope_kind": "memory", "conversation_id": "memory-a"},
            "metadata": {"scope_kind": "memory", "conversation_id": "memory-a"},
        },
        {
            "uid": "UserA",
            "channel": "ios_voice",
            "provider": "grok-realtime",
            "role": "user",
            "text": "Memory B private garden detail.",
            "started_at": datetime(2026, 7, 24, 10, 2),
            "session_id": "other-scoped-session",
            "source_ref": {"scope_kind": "memory", "conversation_id": "memory-b"},
            "metadata": {"scope_kind": "memory", "conversation_id": "memory-b"},
        },
        {
            "uid": "usera",
            "channel": "ios_voice",
            "provider": "grok-realtime",
            "role": "user",
            "text": "Case-colliding general garden detail.",
            "started_at": datetime(2026, 7, 24, 10, 3),
            "session_id": "case-collision-general",
            "source_ref": {},
            "metadata": {},
        },
        {
            "uid": "usera",
            "channel": "ios_voice",
            "provider": "grok-realtime",
            "role": "user",
            "text": "Case-colliding private garden detail.",
            "started_at": datetime(2026, 7, 24, 10, 4),
            "session_id": "case-collision-scoped",
            "source_ref": {"scope_kind": "memory", "conversation_id": "memory-a"},
            "metadata": {"scope_kind": "memory", "conversation_id": "memory-a"},
        },
    ]
    queries = []

    class Pool:
        async def fetch(self, query, uid, scope_kind, conversation_id, *rest):
            queries.append((query, uid, scope_kind, conversation_id, rest))
            visible = []
            for row in rows:
                if row["uid"] != uid:
                    continue
                row_scope = row["source_ref"].get("scope_kind")
                row_conversation = row["source_ref"].get("conversation_id")
                if row_scope != "memory" or (scope_kind == "memory" and row_conversation == conversation_id):
                    visible.append(row)
            return visible

    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))

    general_startup = asyncio.run(voice._fetch_recent_canonical_timeline("UserA"))
    scoped_startup = asyncio.run(
        voice._fetch_recent_canonical_timeline(
            "UserA",
            scope_kind="memory",
            conversation_id="memory-a",
        )
    )
    general_search = asyncio.run(voice._search_canonical_timeline("UserA", "garden", 10))
    scoped_search = asyncio.run(
        voice._search_canonical_timeline(
            "UserA",
            "garden",
            10,
            scope_kind="memory",
            conversation_id="memory-a",
        )
    )

    assert "General garden plans." in general_startup
    assert "Memory A garden detail." not in general_startup
    assert "Memory B private garden detail." not in general_startup
    assert "Case-colliding general garden detail." not in general_startup
    assert "Case-colliding private garden detail." not in general_startup
    assert "General garden plans." in scoped_startup
    assert "Memory A garden detail." in scoped_startup
    assert "Memory B private garden detail." not in scoped_startup
    assert "Case-colliding general garden detail." not in scoped_startup
    assert "Case-colliding private garden detail." not in scoped_startup
    assert [result["content"] for result in general_search] == ["General garden plans."]
    assert {result["content"] for result in scoped_search} == {
        "General garden plans.",
        "Memory A garden detail.",
    }
    assert all("source_ref ->> 'scope_kind'" in query for query, *_ in queries)
    assert all("WHERE uid = $1" in query for query, *_ in queries)
    assert all("lower(uid)" not in query for query, *_ in queries)


def test_isolated_workspace_search_uses_hermes_post_contract(monkeypatch):
    requests = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "path": "memory/user-a.md",
                        "heading": "User A",
                        "excerpt": "Private User A context.",
                        "score": 12,
                    }
                ]
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            requests.append((url, headers, json))
            return Response()

    monkeypatch.setattr(voice.httpx, "AsyncClient", Client)

    result = asyncio.run(
        voice._search_workspace(
            "uid-a",
            "ella-uid-a",
            "private context",
            5,
            provision_url="http://hermes-8210",
            provision_token="test-hermes-secret",
            owner_uid="uid-a",
        )
    )

    assert result[0]["content"] == "Private User A context."
    assert result[0]["metadata"]["provenance"] == "hermes_workspace"
    assert requests == [
        (
            "http://hermes-8210/workspace/ella-uid-a/search",
            {
                "Authorization": "Bearer test-hermes-secret",
                "Content-Type": "application/json",
                "X-Ella-Owner-Uid": "uid-a",
            },
            {"query": "private context", "limit": 5},
        )
    ]


def test_isolated_tool_calls_exact_runtime_without_returning_credentials(monkeypatch):
    runtime = SimpleNamespace(agent_id="ella-uid-a", revision=9)
    requests = []

    async def resolve(uid, **kwargs):
        return runtime

    class Response:
        status_code = 200

        def json(self):
            return {"answer": "Only User A context."}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            requests.append((url, headers, json))
            return Response()

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "isolated_voice_routing_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice.httpx, "AsyncClient", Client)

    result = asyncio.run(
        voice.execute_voice_tool(
            _request(
                {"uid": "uid-a", "tool_name": "ask_ella", "arguments": {"query": "Who am I?"}},
                token=_token("uid-a"),
                service_token="test-proxy-secret",
            )
        )
    )

    assert result == {
        "answer": "Only User A context.",
        "runtime": "hermes",
        "agent_id": "ella-uid-a",
        "binding_revision": 9,
    }
    assert requests[0][0] == "http://hermes-8210/runtime/ella-uid-a/chat"
    assert requests[0][1]["X-Ella-Owner-Uid"] == "uid-a"
    assert "test-hermes-secret" not in str(result)


def test_cloud_voice_tool_never_calls_mini_provisioning_shim(monkeypatch):
    runtime = SimpleNamespace(
        provider="hermes_cloud",
        agent_id="hermes-cloud",
        revision=2,
    )

    async def resolve_runtime(principal):
        return runtime

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Hermes Cloud voice tool must not call the Mini")

    monkeypatch.setattr(voice, "_resolve_voice_runtime", resolve_runtime)
    monkeypatch.setattr(voice.httpx, "AsyncClient", ForbiddenClient)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            voice.execute_voice_tool(
                _request(
                    {
                        "uid": "uid-a",
                        "tool_name": "ask_ella",
                        "arguments": {"query": "Synthetic question"},
                    },
                    token=_token("uid-a"),
                    service_token="test-proxy-secret",
                )
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "hermes_cloud_voice_tool_not_enabled"}
