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


def test_plato_mcp_lists_default_safe_tools(monkeypatch):
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
        "companion_submit_observation",
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
    assert "companion_get_proposal_status" not in tool_names


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


def test_plato_mcp_keeps_deprecated_proposal_tools_hidden_when_enabled(monkeypatch):
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
    assert "companion_submit_observation" in tool_names
    assert "companion_propose_change" not in tool_names
    assert "companion_get_proposal_status" not in tool_names


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


def test_companion_get_proposal_status_tool_is_hidden(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={"name": "companion_get_proposal_status", "arguments": {"proposal_id": "proposal-1"}},
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32601


def test_companion_propose_change_is_hidden_when_enabled(monkeypatch):
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
    payload = response.json()
    assert payload["error"]["code"] == -32601


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
    assert payload["error"]["code"] == -32601


def test_companion_submit_observation_creates_memory_proposal(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)
    captured = {"events": [], "proposal": {}}

    class FakeCanonicalStore:
        async def write_batch(self, events):
            captured["events"].extend(events)
            return {"ok": True, "inserted": len(events), "duplicates": 0}

    def fake_create(*, session_claims, tool_name, proposal_type, payload, idempotency_key):
        captured["proposal"] = {
            "session_claims": session_claims,
            "tool_name": tool_name,
            "proposal_type": proposal_type,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        return {
            "created": True,
            "deduped": False,
            "proposal": {"proposal_id": "proposal-memory-1", "status": "submitted"},
        }

    monkeypatch.setattr(module, "_canonical_store", FakeCanonicalStore())
    monkeypatch.setattr(module.proposal_ingest, "create_proposal", fake_create)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "companion_submit_observation",
                "arguments": {
                    "channel": "grok_conversation",
                    "title": "Grok MCP sentinel",
                    "text": "Live Grok MCP test phrase is copper sailboat.",
                    "idempotency_key": "MCP-WRITE-PROBE-4417",
                },
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["accepted"] is True
    assert result["event_id"] == "MCP-WRITE-PROBE-4417"
    assert result["channel"] == "grok_conversation"
    assert result["memory_proposal"]["proposal"]["proposal_id"] == "proposal-memory-1"

    assert len(captured["events"]) == 1
    raw_event = captured["events"][0]
    assert raw_event.channel == "grok_conversation"
    assert raw_event.provider == "mcp_companion"
    assert raw_event.role == "companion"
    assert raw_event.text == "Live Grok MCP test phrase is copper sailboat."
    assert raw_event.source_ref == {"mcp_tool": "companion_submit_observation"}

    proposal = captured["proposal"]
    assert proposal["tool_name"] == "companion_propose_change"
    assert proposal["proposal_type"] == "memory_note"
    assert proposal["idempotency_key"] == "mcp-observation:MCP-WRITE-PROBE-4417:memory_note"
    assert "proposals:write" in proposal["session_claims"]["scopes"]
    assert "companion_propose_change" in proposal["session_claims"]["allowed_tools"]
    assert proposal["payload"]["source"] == "plato_mcp"
    assert proposal["payload"]["target"]["canonical_identity"] == module._plato_canonical_identity()
    assert proposal["payload"]["requested_change"]["memory"] == "Live Grok MCP test phrase is copper sailboat."
    assert proposal["payload"]["evidence"][0]["event_id"] == "MCP-WRITE-PROBE-4417"


def test_companion_submit_observation_rejects_read_only_session_token(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)
    captured = {"write_called": False, "proposal_called": False}

    class FakeCanonicalStore:
        async def write_batch(self, events):
            captured["write_called"] = True
            return {"ok": True, "inserted": len(events), "duplicates": 0}

    def fake_validate(token):
        assert token == "read-only-session-token"
        return {
            "profile_uid": module._plato_uid(),
            "role": "self",
            "scopes": ["tools:read", "startup:read", "timeline:read", "memory:read"],
            "allowed_tools": ["companion_start_here", "plato_search_memory", "companion_submit_observation"],
            "grant_id": "read-only-grant",
            "external_provider": "grok",
        }

    def fake_create(**kwargs):
        captured["proposal_called"] = True
        return {}

    monkeypatch.setattr(module, "validate_mcp_session_token", fake_validate)
    monkeypatch.setattr(module, "_canonical_store", FakeCanonicalStore())
    monkeypatch.setattr(module.proposal_ingest, "create_proposal", fake_create)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer read-only-session-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "companion_submit_observation",
                "arguments": {
                    "channel": "grok_conversation",
                    "title": "Read-only probe",
                    "text": "This read-only MCP session must not write a durable memory proposal.",
                    "idempotency_key": "READ-ONLY-MCP-WRITE-PROBE",
                },
            },
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32003
    assert "observations:write" in payload["error"]["message"]
    assert captured["write_called"] is False
    assert captured["proposal_called"] is False


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


def test_plato_omi_activity_window_supports_local_morning_window(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)
    captured = {}

    async def fake_recent_context(arguments):
        captured.update(arguments)
        return {
            "uid": module._plato_uid(),
            "source": "canonical_timeline",
            "events": [
                {
                    "event_id": "morning-cafe",
                    "channel": "omi",
                    "title": "Cafe Visit - Ordering Food and Drinks",
                    "text": "Ordered food and drinks during the cafe visit.",
                    "started_at": "2026-05-11T17:49:30Z",
                    "ended_at": "2026-05-11T18:05:00Z",
                    "metadata": {"ella_signal": {"salience": "medium"}},
                },
                {
                    "event_id": "noon-fragment",
                    "channel": "omi",
                    "title": "Caffeine Fragment",
                    "text": "Caffeine.",
                    "started_at": "2026-05-11T19:01:05Z",
                    "ended_at": "2026-05-11T19:01:08Z",
                    "metadata": {"ella_signal": {"salience": "low"}, "segment_count": 1},
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
                    "local_date": "2026-05-11",
                    "part_of_day": "morning",
                    "timezone": "America/Los_Angeles",
                },
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert captured["_allow_large_window"] is True
    assert captured["limit"] == 200
    assert captured["since"].startswith("2026-05-11T10:00:00Z")
    assert result["counts"]["window_events"] == 1
    assert result["meaningful_moments"][0]["event_id"] == "morning-cafe"
    assert result["window"]["since_local"].startswith("2026-05-11T05:00:00")
    assert result["window"]["until_local"].startswith("2026-05-11T12:00:00")


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


def test_plato_search_memory_prefers_deep_hermes_workspace_matches(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def recent_meta_chat(arguments):
        return {
            "uid": module._plato_uid(),
            "source": "canonical_timeline",
            "events": [
                {
                    "event_id": "today-meta-chat",
                    "channel": "imessage",
                    "role": "assistant",
                    "title": "Follow-Up on Meisheng 504 Plan Meeting",
                    "text": "I do not have the full kickoff meeting transcript.",
                    "started_at": "2026-05-27T20:33:47Z",
                }
            ],
        }

    async def workspace_search(query, max_results):
        assert "504" in query
        return [
            {
                "event_id": "hermes-workspace:TIMELINE.md:121",
                "channel": "hermes_workspace",
                "provider": "hermes-provision-search",
                "role": "memory",
                "title": "2026-04-24 15:00 — Initial 504 meeting for Meisheng",
                "text": "The school team walked through the first Section 504 meeting with Plato, Meisheng, and staff.",
                "source_ref": {"source": "hermes_workspace", "file": "TIMELINE.md", "line": 121},
            }
        ]

    monkeypatch.setattr(module, "_recent_context", recent_meta_chat)
    monkeypatch.setattr(module, "_fetch_workspace_search", workspace_search)

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "plato_search_memory",
                "arguments": {
                    "query": "Meisheng 504 plan administrators kickoff meeting last month",
                    "max_results": 5,
                },
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["source"] == "canonical_timeline_with_hermes_workspace_search"
    assert result["results"][0]["channel"] == "hermes_workspace"
    assert result["results"][0]["source_ref"]["file"] == "TIMELINE.md"
    assert result["results"][1]["event_id"] == "today-meta-chat"


def test_plato_search_memory_temporal_query_returns_morning_window_without_keyword_match(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_recent_context(arguments):
        assert arguments["_allow_large_window"] is True
        assert arguments["since"]
        return {
            "uid": module._plato_uid(),
            "source": "canonical_timeline",
            "events": [
                {
                    "event_id": "morning-cafe",
                    "channel": "omi",
                    "title": "Cafe Visit - Ordering Food and Drinks",
                    "text": "Ordered food and drinks during the cafe visit.",
                    "started_at": "2026-05-11T17:49:30Z",
                    "ended_at": "2026-05-11T18:05:00Z",
                }
            ],
        }

    monkeypatch.setattr(module, "_recent_context", fake_recent_context)
    monkeypatch.setattr(
        module,
        "_infer_query_time_window",
        lambda query, tz_name: (
            module.datetime(2026, 5, 11, 12, 0, tzinfo=module.timezone.utc),
            module.datetime(2026, 5, 11, 19, 0, tzinfo=module.timezone.utc),
            "morning",
        ),
    )

    response = client.post(
        "/v1/ella/plato/mcp",
        headers={"Authorization": "Bearer test-token"},
        json=_rpc(
            "tools/call",
            params={
                "name": "plato_search_memory",
                "arguments": {"query": "what happened this morning", "max_results": 5, "channels": ["omi"]},
            },
        ),
    )

    assert response.status_code == 200
    result = _tool_result(response)
    assert result["inferred_time_window"] == "morning"
    assert result["results"][0]["event_id"] == "morning-cafe"


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
    monkeypatch.setattr(module, "_fetch_workspace_search", lambda prompt, max_results: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(module._consult_plato({"prompt": "Did I order at a cafe today?", "mode": "normal"}))

    user_message = captured["json"]["messages"][1]["content"]
    assert "Current MCP context" in user_message
    assert "Cafe Coffee and Waffle Stop" in user_message
    assert "Ordered a noah drink and a waffle" in user_message
    assert "freshest available evidence" in captured["json"]["messages"][0]["content"]
    assert result["context_source"] == "canonical_timeline_empty_omi_firestore_fallback"
    assert result["context_events"] == 1


def test_plato_consult_includes_deep_workspace_search(monkeypatch):
    module = _load_module(monkeypatch)
    monkeypatch.setenv("HERMES_API_SERVER_KEY", "hermes-test-token")
    captured = {}

    async def fake_recent_context(arguments):
        return {"uid": "test-uid", "source": "canonical_timeline", "events": []}

    async def workspace_search(prompt, max_results):
        return [
            {
                "event_id": "hermes-workspace:TIMELINE.md:121",
                "channel": "hermes_workspace",
                "provider": "hermes-provision-search",
                "role": "memory",
                "title": "2026-04-24 15:00 — Initial 504 meeting for Meisheng",
                "text": "The school team walked through the first Section 504 meeting with Plato, Meisheng, and staff.",
                "source_ref": {"source": "hermes_workspace", "file": "TIMELINE.md", "line": 121},
            }
        ]

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "The 504 meeting is in the deep workspace context."}}]}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(module, "_recent_context", fake_recent_context)
    monkeypatch.setattr(module, "_fetch_workspace_search", workspace_search)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(module._consult_plato({"prompt": "What happened in Meisheng's 504 meeting?", "mode": "deep"}))

    user_message = captured["json"]["messages"][1]["content"]
    assert "Deep Hermes workspace search" in user_message
    assert "Initial 504 meeting for Meisheng" in user_message
    assert result["context_source"] == "canonical_timeline_with_hermes_workspace_search"
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
