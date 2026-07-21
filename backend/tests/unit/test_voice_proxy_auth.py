import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException, Request

sys.modules.setdefault("websockets", ModuleType("websockets"))
conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value, uid=None: value
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import voice


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


def _token(uid: str, *, isolated: bool = True, expired: bool = False) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": uid,
            "uid": uid,
            "firebase_uid": uid,
            "provider": "grok-voice",
            "voice_mode": "v4",
            "isolated_runtime": isolated,
            "jti": f"session-{uid}",
            "aud": voice.VOICE_SESSION_AUDIENCE,
            "iss": "omi-backend",
            "iat": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1) if expired else now + timedelta(minutes=10),
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


def test_voice_session_token_has_firebase_subject_and_proxy_audience():
    encoded = voice.create_session_token(
        uid="uid-a",
        firebase_uid="uid-a",
        voice_mode="v4",
        provider="grok-voice",
        isolated_runtime=True,
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


def test_isolated_context_uses_active_8210_agent_and_redacts_credentials(monkeypatch):
    runtime = SimpleNamespace(agent_id="ella-uid-a", revision=7)
    queries = []
    requests = []

    async def resolve(uid):
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

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(voice, "resolve_isolated_runtime", resolve)
    monkeypatch.setattr(voice, "_get_pool", lambda: asyncio.sleep(0, result=Pool()))
    monkeypatch.setattr(voice, "_fetch_recent_conversations", empty)
    monkeypatch.setattr(voice, "_fetch_recent_canonical_timeline", empty)
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
    assert result["runtime"] == {"provider": "hermes", "agent_id": "ella-uid-a", "binding_revision": 7}
    assert "gateway_token" not in result
    assert "gateway_url" not in result
    assert requests[0][0] == "http://hermes-8210/workspace/ella-uid-a/files"
    assert requests[0][1]["X-Ella-Owner-Uid"] == "uid-a"


def test_isolated_search_forces_receipt_agent_and_owner_header(monkeypatch):
    runtime = SimpleNamespace(agent_id="ella-uid-a", revision=8)
    calls = []

    async def resolve(uid):
        return runtime

    async def workspace(uid, agent_id, query, limit, **kwargs):
        calls.append((uid, agent_id, kwargs))
        return [{"source": "workspace", "content": "private-a", "score": 10, "metadata": {}}]

    monkeypatch.setattr(voice, "runtime_bindings_enabled", lambda uid=None: True)
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

    async def resolve(uid):
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
