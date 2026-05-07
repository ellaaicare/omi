import importlib
import json
import sys
import types
import base64
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _install_stubs():
    conversations = types.ModuleType("database.conversations")
    conversations.get_conversations = MagicMock(return_value=[])

    memories = types.ModuleType("database.memories")
    memories.get_memories = MagicMock(return_value=[])

    database = sys.modules.setdefault("database", types.ModuleType("database"))
    setattr(database, "conversations", conversations)
    setattr(database, "memories", memories)
    sys.modules["database.conversations"] = conversations
    sys.modules["database.memories"] = memories


def _load_module(monkeypatch):
    _install_stubs()
    monkeypatch.setenv("ELLA_PLATO_MCP_TOKEN", "test-token")
    monkeypatch.setenv("ELLA_PLATO_MCP_RATE_LIMIT_PER_MINUTE", "0")
    sys.modules.pop("ella.routers.plato_mcp", None)
    module = importlib.import_module("ella.routers.plato_mcp")
    module._rate_limits.clear()
    return module


def _client(module):
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def _rpc(method, msg_id=1, params=None):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def _tool_result(response):
    payload = response.json()
    text = payload["result"]["content"][0]["text"]
    return json.loads(text)


def test_plato_mcp_rejects_missing_and_invalid_token(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    missing = client.post("/v1/ella/plato/mcp", json=_rpc("ping"))
    assert missing.status_code == 401

    invalid = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer wrong"},
        json=_rpc("ping"),
    )
    assert invalid.status_code == 401


def test_plato_mcp_lists_only_read_only_tools(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc("tools/list"),
    )

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert tool_names == {
        "plato_recent_context",
        "plato_search_memory",
        "plato_latest_omi",
        "plato_get_scanner_rules",
        "plato_consult",
    }
    assert "create_memory" not in tool_names
    assert "delete_memory" not in tool_names
    assert "update_conversation_summary" not in tool_names


def test_plato_recent_context_uses_canonical_timeline(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_timeline(limit, channels, since):
        assert limit == 3
        assert channels == ["omi"]
        assert since is None
        return [
            {
                "event_id": "evt-1",
                "channel": "omi",
                "text": "Latest OMI conversation about Whole Foods.",
                "started_at": "2026-05-07T01:00:00Z",
            }
        ]

    monkeypatch.setattr(module, "_fetch_canonical_timeline", fake_timeline)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "plato_recent_context", "arguments": {"limit": 3, "channels": ["omi"]}},
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["source"] == "canonical_timeline"
    assert result["events"][0]["event_id"] == "evt-1"
    assert result["trace_id"]


def test_plato_recent_context_falls_back_when_canonical_is_empty(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def empty_timeline(limit, channels, since):
        return []

    monkeypatch.setattr(module, "_fetch_canonical_timeline", empty_timeline)
    monkeypatch.setattr(
        module.conversations_db,
        "get_conversations",
        MagicMock(
            return_value=[
                {
                    "id": "conv-1",
                    "created_at": "2026-05-07T01:00:00Z",
                    "structured": {"title": "Latest OMI", "overview": "Discussed the last OMI conversation."},
                }
            ]
        ),
    )

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "plato_latest_omi", "arguments": {"limit": 3}},
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["source"] == "canonical_timeline_empty_omi_firestore_fallback"
    assert result["latest"]["event_id"] == "conv-1"


def test_plato_search_memory_rejects_missing_query(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc("tools/call", params={"name": "plato_search_memory", "arguments": {}}),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32602
    assert "query is required" in payload["error"]["message"]


def test_initialize_returns_session_header(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc("initialize"),
    )

    assert response.status_code == 200
    assert response.headers["mcp-session-id"]
    assert response.json()["result"]["serverInfo"]["name"] == "ella-plato-hermes-mcp"


def test_oauth_token_exchanges_client_secret_for_bearer(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    token_response = client.post(
        "/v1/ella/plato/mcp/token",
        data={
            "client_id": "plato-grok",
            "client_secret": "test-token",
            "grant_type": "authorization_code",
            "code": "plato_mcp",
        },
    )

    assert token_response.status_code == 200
    payload = token_response.json()
    assert payload["access_token"] == "test-token"
    assert payload["token_type"] == "Bearer"
    assert payload["scope"] == "plato:read"


def test_oauth_token_accepts_basic_client_auth(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)
    credentials = base64.b64encode(b"plato-grok:test-token").decode()

    token_response = client.post(
        "/v1/ella/plato/mcp/token",
        headers={"Authorization": f"Basic {credentials}"},
        data={"grant_type": "authorization_code", "code": "plato_mcp"},
    )

    assert token_response.status_code == 200
    assert token_response.json()["access_token"] == "test-token"


def test_oauth_authorize_redirects_with_code_and_state(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.get(
        "/v1/ella/plato/mcp/authorize",
        params={
            "response_type": "code",
            "client_id": "plato-grok",
            "redirect_uri": "https://grok.example/callback",
            "state": "abc",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://grok.example/callback?code=plato_mcp&state=abc"
