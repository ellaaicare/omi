import importlib
import asyncio
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

    proposals = types.ModuleType("database.proposals")
    proposals.get_proposal = MagicMock(return_value=None)
    proposals.get_proposal_by_idempotency_key = MagicMock(return_value=None)
    proposals.save_proposal = MagicMock(side_effect=lambda proposal: proposal)

    database = sys.modules.setdefault("database", types.ModuleType("database"))
    setattr(database, "conversations", conversations)
    setattr(database, "memories", memories)
    setattr(database, "proposals", proposals)
    sys.modules["database.conversations"] = conversations
    sys.modules["database.memories"] = memories
    sys.modules["database.proposals"] = proposals


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
        "companion_start_here",
        "companion_surface_prompt",
        "companion_get_proposal_status",
        "plato_recent_context",
        "plato_search_memory",
        "plato_latest_omi",
        "plato_omi_activity_window",
        "plato_get_scanner_rules",
        "plato_consult",
    }
    assert "create_memory" not in tool_names
    assert "delete_memory" not in tool_names
    assert "update_conversation_summary" not in tool_names
    assert "companion_propose_change" not in tool_names


def test_companion_surface_prompt_returns_no_secret_bootstrap(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "companion_surface_prompt", "arguments": {"surface": "grok"}},
        ),
    )

    assert response.status_code == 200
    payload = _tool_result(response)
    assert payload["surface"] == "grok"
    assert payload["auth_policy"]["prompt_contains_secrets"] is False
    assert "companion_start_here" in payload["prompt"]
    assert "Never claim" in payload["prompt"]
    assert "test-token" not in payload["prompt"]


def test_plato_mcp_lists_proposal_tool_only_when_enabled(monkeypatch):
    monkeypatch.setenv("ELLA_PLATO_MCP_ENABLE_PROPOSALS", "true")
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc("tools/list"),
    )

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "companion_propose_change" in tool_names


def test_streamable_http_prefers_json_when_client_accepts_json_and_sse(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={
            "Authorization": "Bearer test-token",
            "Accept": "application/json, text/event-stream",
        },
        json=_rpc("tools/list"),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "plato_consult" in tool_names
    assert "companion_start_here" in tool_names


def test_companion_start_here_tool_returns_generic_startup_packet(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_startup_context(*, onboarding, limit, channels):
        assert onboarding["state"] == "authenticated_mapped"
        assert onboarding["selected_profile"]["profile_uid"] == module._plato_uid()
        assert limit == 4
        assert channels == ["omi"]
        return {
            "schema_version": "ella.mcp.start_here.v1",
            "startup_ready": True,
            "account": {"profile_uid": module._plato_uid(), "role": "self"},
            "memory": {"source": "canonical_timeline", "event_count": 1},
            "writeback_policy": {"mode": "read_only"},
        }

    monkeypatch.setattr(module, "build_startup_context", fake_startup_context)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "companion_start_here", "arguments": {"limit": 4, "channels": ["omi"]}},
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["schema_version"] == "ella.mcp.start_here.v1"
    assert result["startup_ready"] is True
    assert result["account"]["role"] == "self"


def test_companion_get_proposal_status_tool_is_read_only(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    def fake_status(*, session_claims, proposal_id):
        assert session_claims["profile_uid"] == module._plato_uid()
        assert proposal_id == "proposal-1"
        return {"proposal_id": "proposal-1", "status": "submitted", "profile_uid": module._plato_uid()}

    monkeypatch.setattr(module.proposal_ingest, "get_proposal_status", fake_status)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "companion_get_proposal_status", "arguments": {"proposal_id": "proposal-1"}},
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["proposal_id"] == "proposal-1"
    assert result["status"] == "submitted"


def test_companion_propose_change_creates_proposal_when_enabled(monkeypatch):
    monkeypatch.setenv("ELLA_PLATO_MCP_ENABLE_PROPOSALS", "true")
    module = _load_module(monkeypatch)
    client = _client(module)
    captured = {}

    def fake_create(*, session_claims, tool_name, proposal_type, payload, idempotency_key):
        captured["session_claims"] = session_claims
        captured["tool_name"] = tool_name
        captured["proposal_type"] = proposal_type
        captured["payload"] = payload
        captured["idempotency_key"] = idempotency_key
        return {
            "created": True,
            "deduped": False,
            "proposal": {"proposal_id": "proposal-1", "status": "submitted"},
        }

    monkeypatch.setattr(module.proposal_ingest, "create_proposal", fake_create)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "companion_propose_change",
                "arguments": {
                    "proposal_type": "scanner_rule_change",
                    "title": "Temporarily watch for glasses",
                    "description": "User asked Ella to remember where glasses are usually kept.",
                    "target": {"file": "scanner-tuning.md"},
                    "requested_change": {"add_phrase": "where are my glasses"},
                    "idempotency_key": "glasses-1",
                },
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["created"] is True
    assert result["proposal"]["proposal_id"] == "proposal-1"
    assert captured["tool_name"] == "companion_propose_change"
    assert captured["proposal_type"] == "scanner_rule_change"
    assert "proposals:write" in captured["session_claims"]["scopes"]
    assert "companion_propose_change" in captured["session_claims"]["allowed_tools"]
    assert captured["payload"]["write_policy"] == "proposal_only"
    assert captured["idempotency_key"] == "glasses-1"


def test_companion_propose_change_is_unknown_when_disabled(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "companion_propose_change",
                "arguments": {
                    "proposal_type": "scanner_rule_change",
                    "title": "Nope",
                    "description": "Disabled",
                },
            },
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32601


def test_companion_propose_change_rejects_unsupported_type(monkeypatch):
    monkeypatch.setenv("ELLA_PLATO_MCP_ENABLE_PROPOSALS", "true")
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "companion_propose_change",
                "arguments": {
                    "proposal_type": "direct_mutation",
                    "title": "Bad",
                    "description": "Should not be accepted.",
                },
            },
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32602
    assert "proposal_type must be one of" in payload["error"]["message"]


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


def test_plato_omi_activity_window_splits_meaningful_from_fragments(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_recent_context(arguments):
        assert arguments["channels"] == ["omi"]
        return {
            "uid": module._plato_uid(),
            "source": "canonical_timeline_with_omi_firestore_fallback",
            "events": [
                {
                    "event_id": "brief-color",
                    "channel": "omi",
                    "title": "Brief Color Comment",
                    "text": "Not white.",
                    "started_at": "2026-05-08T03:21:53Z",
                    "ended_at": "2026-05-08T03:21:57Z",
                    "metadata": {"ella_signal": {"salience": "low"}, "segment_count": 1},
                },
                {
                    "event_id": "ai-music",
                    "channel": "omi",
                    "title": "AI Music and U.S. Government Question",
                    "text": "AI music discussion, pink one, and repeated U.S. government question.",
                    "started_at": "2026-05-08T03:14:03Z",
                    "ended_at": "2026-05-08T03:18:48Z",
                    "metadata": {"ella_signal": {"salience": "low"}, "segment_count": 10},
                },
                {
                    "event_id": "old",
                    "channel": "omi",
                    "title": "Older event",
                    "text": "Outside the requested window.",
                    "started_at": "2026-05-08T02:00:00Z",
                    "ended_at": "2026-05-08T02:05:00Z",
                    "metadata": {"ella_signal": {"salience": "medium"}},
                },
            ],
        }

    monkeypatch.setattr(module, "_recent_context", fake_recent_context)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "plato_omi_activity_window",
                "arguments": {
                    "time_range": "last 30 minutes",
                    "until": "2026-05-08T03:36:18Z",
                    "timezone": "America/Los_Angeles",
                },
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["window"]["since_local"].startswith("2026-05-07T20:06:18")
    assert result["counts"] == {"window_events": 2, "meaningful_moments": 1, "low_salience_fragments": 1}
    assert result["meaningful_moments"][0]["event_id"] == "ai-music"
    assert result["meaningful_moments"][0]["salience"] == "low"
    assert result["meaningful_moments"][0]["is_low_salience_fragment"] is False
    assert result["low_salience_fragments"][0]["event_id"] == "brief-color"


def test_plato_recent_context_merges_omi_fallback_when_canonical_has_other_channels(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def voice_only_timeline(limit, channels, since):
        assert channels == []
        return [
            {
                "event_id": "voice-1",
                "channel": "ios_voice",
                "text": "Older voice turn about bookkeeping.",
                "started_at": "2026-05-07T01:05:00Z",
            }
        ]

    monkeypatch.setattr(module, "_fetch_canonical_timeline", voice_only_timeline)
    monkeypatch.setattr(
        module.conversations_db,
        "get_conversations",
        MagicMock(
            return_value=[
                {
                    "id": "cafe-1",
                    "started_at": "2026-05-07T18:56:59Z",
                    "created_at": "2026-05-07T18:56:59Z",
                    "structured": {
                        "title": "Cafe Coffee and Waffle Stop",
                        "overview": "Ordered a noah drink and a waffle with oat.",
                    },
                }
            ]
        ),
    )

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc("tools/call", params={"name": "plato_recent_context", "arguments": {"limit": 10}}),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["source"] == "canonical_timeline_with_omi_firestore_fallback"
    assert result["events"][0]["event_id"] == "cafe-1"
    assert result["events"][1]["event_id"] == "voice-1"


def test_plato_search_memory_uses_merged_omi_fallback(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def voice_only_timeline(limit, channels, since):
        return [
            {
                "event_id": "voice-1",
                "channel": "ios_voice",
                "text": "Older voice turn about bookkeeping.",
                "started_at": "2026-05-07T01:05:00Z",
            }
        ]

    monkeypatch.setattr(module, "_fetch_canonical_timeline", voice_only_timeline)
    monkeypatch.setattr(
        module.conversations_db,
        "get_conversations",
        MagicMock(
            return_value=[
                {
                    "id": "cafe-1",
                    "started_at": "2026-05-07T18:56:59Z",
                    "structured": {
                        "title": "Cafe Coffee and Waffle Stop",
                        "overview": "Ordered a noah drink and a waffle with oat.",
                    },
                }
            ]
        ),
    )

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "plato_search_memory",
                "arguments": {"query": "cafe waffle order", "max_results": 5},
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["source"] == "canonical_timeline_with_omi_firestore_fallback"
    assert result["results"][0]["event_id"] == "cafe-1"


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


def test_plato_consult_includes_fresh_mcp_context(monkeypatch):
    module = _load_module(monkeypatch)
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "hermes-test-token")
    captured = {}

    async def fake_recent_context(arguments):
        assert arguments["limit"] == 15
        return {
            "uid": "test-uid",
            "source": "canonical_timeline_empty_omi_firestore_fallback",
            "events": [
                {
                    "channel": "omi",
                    "started_at": "2026-05-07T18:56:59Z",
                    "title": "Cafe Coffee and Waffle Stop",
                    "text": "Ordered a noah drink and a waffle with oat.",
                }
            ],
        }

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "The cafe order is in the fresh MCP context."}}]}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(module, "_recent_context", fake_recent_context)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(module._consult_plato({"prompt": "Did I order at a cafe today?", "mode": "normal"}))

    user_message = captured["json"]["messages"][1]["content"]
    assert "Current MCP context" in user_message
    assert "Cafe Coffee and Waffle Stop" in user_message
    assert "Ordered a noah drink and a waffle" in user_message
    assert "freshest available evidence" in captured["json"]["messages"][0]["content"]
    assert result["context_source"] == "canonical_timeline_empty_omi_firestore_fallback"
    assert result["context_events"] == 1


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
