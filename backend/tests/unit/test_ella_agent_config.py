import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.routers import agent_config as router_module
from ella.services import agent_config as service
from utils.other import endpoints as auth


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _client():
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[auth.get_current_user_uid] = lambda: "uid-1"
    return TestClient(app)


def test_get_agent_config_reads_hermes_runtime_first(monkeypatch):
    calls = []

    async def fake_resolve(uid):
        assert uid == "uid-1"
        return service.AgentRouting(
            platform="hermes",
            agent_id="hermes",
            provision_url="http://hermes-provision",
            provision_token="secret",
        )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return _FakeResponse(
                payload={
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "profile": "plato-eval",
                    "override": "profile",
                    "config_path": "/Users/ellaai/.hermes/profiles/plato-eval/config.yaml",
                }
            )

    monkeypatch.setattr(service, "resolve_agent_routing", fake_resolve)
    monkeypatch.setattr(service.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(service.get_agent_config("uid-1"))

    assert result["platform"] == "hermes"
    assert result["provider"] == "openai-codex"
    assert result["model"] == "gpt-5.5"
    assert result["editable"] == {"platform": False, "provider": True, "model": True}
    assert result["source"]["runtime"] == "hermes"
    assert calls[0][0] == "http://hermes-provision/agent-config"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_patch_agent_config_rejects_read_only_platform_field(monkeypatch):
    client = _client()

    response = client.patch(
        "/v1/ella/agent-config",
        json={"platform": "openclaw", "provider": "openai-codex", "model": "gpt-5.5"},
    )

    assert response.status_code == 422


def test_patch_agent_config_validates_provider_model_allowlist(monkeypatch):
    async def fake_resolve(uid):
        return service.AgentRouting(
            platform="hermes",
            agent_id="hermes",
            provision_url="http://hermes-provision",
            provision_token="secret",
        )

    monkeypatch.setattr(service, "resolve_agent_routing", fake_resolve)

    with pytest.raises(Exception) as exc:
        asyncio.run(service.patch_agent_config("uid-1", "openai-codex", "not-real"))

    assert getattr(exc.value, "status_code", None) == 422
    assert exc.value.detail["error"] == "model_not_allowed_for_provider"


def test_patch_agent_config_scopes_to_hermes_profile_and_invalidates_cache(monkeypatch):
    calls = []

    async def fake_resolve(uid):
        assert uid == "uid-1"
        return service.AgentRouting(
            platform="hermes",
            agent_id="hermes",
            provision_url="http://hermes-provision",
            provision_token="secret",
        )

    async def fake_invalidate(uid, agent_id):
        return {"invalidated": True, "store": "postgres.agent_configs", "agent_id": agent_id}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def patch(self, url, **kwargs):
            calls.append((url, kwargs))
            return _FakeResponse(
                payload={
                    "provider": "openai-codex",
                    "model": "gpt-5.5-mini",
                    "profile": "plato-eval",
                    "override": "profile",
                    "config_path": "/Users/ellaai/.hermes/profiles/plato-eval/config.yaml",
                    "reload": {"status": "requested", "scope": "profile"},
                }
            )

    monkeypatch.setattr(service, "resolve_agent_routing", fake_resolve)
    monkeypatch.setattr(service, "invalidate_model_cache", fake_invalidate)
    monkeypatch.setattr(service.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(service.patch_agent_config("uid-1", "openai-codex", "gpt-5.5-mini"))

    assert result["provider"] == "openai-codex"
    assert result["model"] == "gpt-5.5-mini"
    assert result["cache"]["invalidated"] is True
    assert result["reload"]["scope"] == "profile"
    assert calls[0][1]["params"] == {"uid": "uid-1", "agent_id": "hermes"}
    assert calls[0][1]["json"] == {"provider": "openai-codex", "model": "gpt-5.5-mini"}


def test_patch_agent_config_blocks_non_hermes_platform(monkeypatch):
    async def fake_resolve(uid):
        return service.AgentRouting(
            platform="openclaw",
            agent_id="openclaw-agent",
            provision_url="http://openclaw-provision",
            provision_token="secret",
        )

    monkeypatch.setattr(service, "resolve_agent_routing", fake_resolve)

    with pytest.raises(Exception) as exc:
        asyncio.run(service.patch_agent_config("uid-1", "openai-codex", "gpt-5.5"))

    assert getattr(exc.value, "status_code", None) == 409
    assert exc.value.detail["error"] == "active_platform_not_editable"
