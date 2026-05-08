import importlib
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.services.mcp_identity import MCPProfileGrant


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


def _install_firebase_stub(monkeypatch, decoded=None, error=None):
    firebase_admin = types.ModuleType("firebase_admin")
    firebase_auth = types.ModuleType("firebase_admin.auth")

    def verify_id_token(token):
        if error:
            raise error
        assert token == "firebase-token"
        return decoded or {
            "uid": "firebase-uid",
            "email": "person@example.com",
            "email_verified": True,
            "name": "Test Person",
            "firebase": {
                "sign_in_provider": "google.com",
                "identities": {"google.com": ["google-sub-1"]},
            },
        }

    firebase_auth.verify_id_token = verify_id_token
    firebase_admin.auth = firebase_auth
    monkeypatch.setitem(sys.modules, "firebase_admin", firebase_admin)
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", firebase_auth)


def _grant(profile_uid="user-1", role="self"):
    return MCPProfileGrant.from_mapping(
        {
            "grant_id": f"grant-{profile_uid}",
            "profile_uid": profile_uid,
            "role": role,
            "profile_label": profile_uid,
            "scopes": ["context:read", "startup:read", "proposals:write"],
            "allowed_tools": ["companion_start_here", "companion_recent_context"],
            "status": "active",
        }
    )


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


def test_oauth_onboarding_maps_verified_google_identity_to_grant(monkeypatch):
    module = _load_module(monkeypatch)
    _install_firebase_stub(monkeypatch)

    service = importlib.import_module("ella.services.mcp_identity")

    def fake_grants(identity):
        assert identity.provider == "google"
        assert identity.subject == "google-sub-1"
        assert identity.email == "person@example.com"
        assert identity.email_verified is True
        return [_grant()]

    monkeypatch.setattr(service, "load_identity_grants", fake_grants)
    client = _client(module)

    response = client.post(
        "/v1/ella/mcp/onboarding/oauth",
        json={"firebase_id_token": "firebase-token", "ttl_seconds": 600},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "authenticated_mapped"
    assert payload["identity"]["provider"] == "google"
    assert payload["selected_profile"]["profile_uid"] == "user-1"
    claims = payload["session_claims"]
    assert claims["sub"] == "google:google-sub-1"
    assert claims["profile_uid"] == "user-1"
    assert claims["role"] == "self"
    assert "startup:read" in claims["scopes"]
    assert "proposals:write" not in claims["scopes"]


def test_oauth_onboarding_unknown_identity_needs_invite(monkeypatch):
    module = _load_module(monkeypatch)
    _install_firebase_stub(monkeypatch)
    service = importlib.import_module("ella.services.mcp_identity")
    monkeypatch.setattr(service, "load_identity_grants", lambda _identity: [])
    client = _client(module)

    response = client.post("/v1/ella/mcp/onboarding/oauth", json={"firebase_id_token": "firebase-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "authenticated_needs_invite"
    assert payload["selected_profile"] is None
    assert "session_claims" not in payload


def test_oauth_onboarding_requires_profile_selection_for_multiple_grants(monkeypatch):
    module = _load_module(monkeypatch)
    _install_firebase_stub(monkeypatch)
    service = importlib.import_module("ella.services.mcp_identity")
    monkeypatch.setattr(
        service, "load_identity_grants", lambda _identity: [_grant("user-1"), _grant("mom-1", "caregiver")]
    )
    client = _client(module)

    response = client.post("/v1/ella/mcp/onboarding/oauth", json={"firebase_id_token": "firebase-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "authenticated_needs_profile_selection"
    assert "session_claims" not in payload
    assert [item["profile_uid"] for item in payload["available_profiles"]] == ["user-1", "mom-1"]

    selected = client.post(
        "/v1/ella/mcp/onboarding/oauth",
        json={"firebase_id_token": "firebase-token", "selected_profile_uid": "mom-1"},
    )

    assert selected.status_code == 200
    selected_payload = selected.json()
    assert selected_payload["state"] == "authenticated_mapped"
    assert selected_payload["session_claims"]["profile_uid"] == "mom-1"
    assert selected_payload["session_claims"]["role"] == "caregiver"


def test_oauth_onboarding_rejects_invalid_firebase_token(monkeypatch):
    module = _load_module(monkeypatch)
    _install_firebase_stub(monkeypatch, error=ValueError("bad token"))
    client = _client(module)

    response = client.post("/v1/ella/mcp/onboarding/oauth", json={"firebase_id_token": "firebase-token"})

    assert response.status_code == 401


def test_mcp_start_here_returns_canonical_startup_packet(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    async def fake_timeline(uid, *, limit, channels, since=None, before=None, user_timezone=None, timeout=None):
        assert uid == "user-1"
        assert limit == 2
        assert channels == ["omi", "ios_chat"]
        assert user_timezone == "America/Los_Angeles"
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
    assert payload["time_context"]["user_timezone"] == "America/Los_Angeles"
    assert payload["account"]["profile_uid"] == "user-1"
    assert payload["memory"]["source"] == "canonical_timeline"
    assert payload["memory"]["channel_counts"] == {"omi": 1, "ios_chat": 1}
    assert payload["memory"]["latest_by_channel"]["omi"]["title"] == "Cafe stop"
    assert payload["memory"]["latest_by_channel"]["omi"]["started_at_local"].startswith("2026-05-07T11:00:00")
    assert "Current user-local time" in payload["memory"]["summary"]
    assert payload["writeback_policy"]["mode"] == "read_only"
    assert payload["escalation_boundaries"]["emergency_actions"] == "not_available_from_mcp"


def test_mcp_surface_prompt_returns_bootstrap_without_secret(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.get(
        "/v1/ella/mcp/surface-prompt?surface=hosted-gpt",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["surface"] == "hosted-gpt"
    assert payload["profile_uid"] == "user-1"
    assert payload["auth_policy"]["prompt_contains_secrets"] is False
    assert "companion_start_here" in payload["prompt"]
    assert "test-token" not in payload["prompt"]


def test_public_mcp_surface_prompt_is_unauthenticated_and_secret_free(monkeypatch):
    module = _load_module(monkeypatch)
    client = _client(module)

    response = client.get("/v1/ella/mcp/surface-prompt/public?surface=grok")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "ella.mcp.surface_prompt.public.v1"
    assert payload["public"] is True
    assert payload["surface"] == "grok"
    assert payload["profile_uid"] == "selected-after-auth"
    assert payload["auth_policy"]["prompt_contains_secrets"] is False
    assert payload["auth_policy"]["runtime_auth_required"] is True
    assert "companion_start_here" in payload["prompt"]
    assert "test-token" not in payload["prompt"]


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
