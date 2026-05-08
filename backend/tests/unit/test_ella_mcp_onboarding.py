import importlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_module(monkeypatch):
    monkeypatch.setenv("ELLA_MCP_TOKEN", "test-token")
    monkeypatch.setenv("ELLA_MCP_DEFAULT_PROFILE_UID", "user-1")
    monkeypatch.setenv("ELLA_MCP_DEFAULT_PROFILE_LABEL", "Test User")
    monkeypatch.setenv("ELLA_MCP_ALLOWED_TOOLS", "companion_start_here,companion_recent_context")
    sys.modules.pop("ella.routers.mcp_onboarding", None)
    return importlib.import_module("ella.routers.mcp_onboarding")


def _client(module):
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_mcp_onboarding_rejects_missing_or_invalid_static_token(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    assert client.get("/v1/ella/mcp/onboarding").status_code == 401
    assert client.get("/v1/ella/mcp/onboarding", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_mcp_onboarding_maps_static_token_to_generic_profile_claims(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.get("/v1/ella/mcp/onboarding", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "authenticated_mapped"
    assert payload["selected_profile"]["profile_uid"] == "user-1"
    assert payload["selected_profile"]["role"] == "self"
    assert payload["session_claims"]["profile_uid"] == "user-1"
    assert payload["session_claims"]["role"] == "self"
    assert payload["session_claims"]["allowed_tools"] == ["companion_recent_context", "companion_start_here"]
    assert "startup:read" in payload["session_claims"]["scopes"]


def test_mcp_onboarding_without_default_profile_returns_invite_state(monkeypatch):
    module = _load_module(monkeypatch)
    monkeypatch.delenv("ELLA_MCP_DEFAULT_PROFILE_UID", raising=False)
    monkeypatch.delenv("ELLA_PLATO_MCP_UID", raising=False)
    monkeypatch.delenv("ELLA_PLATO_UID", raising=False)
    client = _client(module)

    response = client.get("/v1/ella/mcp/onboarding", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "authenticated_needs_invite"
    assert payload["selected_profile"] is None
    assert "session_claims" not in payload


def test_mcp_start_here_returns_canonical_startup_packet(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_timeline(uid, *, limit, channels, since=None, before=None, timeout=None):
        assert uid == "user-1"
        assert limit == 2
        assert channels == ["omi", "ios_chat"]
        return [
            {
                "event_id": "evt-omi",
                "channel": "omi",
                "provider": "omi",
                "role": "user",
                "started_at": "2026-05-07T18:00:00Z",
                "title": "Cafe stop",
                "text": "Plato ordered a waffle and coffee.",
                "source_identity": "omi:evt-omi",
            },
            {
                "event_id": "evt-chat",
                "channel": "ios_chat",
                "provider": "hermes",
                "role": "assistant",
                "started_at": "2026-05-07T19:00:00Z",
                "title": "Chat",
                "text": "The assistant remembered the cafe stop.",
                "source_identity": "chat:evt-chat",
            },
        ]

    startup = importlib.import_module("ella.services.mcp_startup")
    monkeypatch.setattr(startup, "fetch_canonical_timeline", fake_timeline)

    response = client.get(
        "/v1/ella/mcp/start_here?limit=2&channels=omi,ios_chat",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "ella.mcp.start_here.v1"
    assert payload["startup_ready"] is True
    assert payload["account"]["profile_uid"] == "user-1"
    assert payload["memory"]["source"] == "canonical_timeline"
    assert payload["memory"]["channel_counts"] == {"omi": 1, "ios_chat": 1}
    assert payload["memory"]["latest_by_channel"]["omi"]["title"] == "Cafe stop"
    assert payload["writeback_policy"]["mode"] == "read_only"
    assert payload["escalation_boundaries"]["emergency_actions"] == "not_available_from_mcp"


def test_mcp_start_here_does_not_leak_context_when_unmapped(monkeypatch):
    module = _load_module(monkeypatch)
    monkeypatch.delenv("ELLA_MCP_DEFAULT_PROFILE_UID", raising=False)
    monkeypatch.delenv("ELLA_PLATO_MCP_UID", raising=False)
    monkeypatch.delenv("ELLA_PLATO_UID", raising=False)
    client = _client(module)

    response = client.get("/v1/ella/mcp/start_here", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["startup_ready"] is False
    assert payload["reason"] == "identity_not_mapped"
    assert "memory" not in payload
