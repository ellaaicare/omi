from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.routers.mcp_well_known import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_protected_resource_advertises_observation_writes():
    response = _client().get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert "observations:write" in response.json()["scopes_supported"]


def test_authorization_server_advertises_observation_writes():
    response = _client().get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert "observations:write" in response.json()["scopes_supported"]
