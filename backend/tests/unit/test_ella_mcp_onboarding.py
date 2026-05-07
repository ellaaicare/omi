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
