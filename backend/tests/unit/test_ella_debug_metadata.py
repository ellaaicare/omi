import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from utils.ella import exact_firebase_auth

sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("database._client", MagicMock())

ella_module = types.ModuleType("ella")
routers_module = types.ModuleType("ella.routers")
resolve_module = types.ModuleType("ella.routers.resolve")
resolve_module.PROVISION_API_KEY = ""
resolve_module.PROVISION_API_URL = "http://provision.test"
resolve_module.resolve_user_routing = MagicMock()
sys.modules["ella"] = ella_module
sys.modules["ella.routers"] = routers_module
sys.modules["ella.routers.resolve"] = resolve_module

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_module_path = Path(__file__).resolve().parents[2] / "ella" / "routers" / "debug_metadata.py"
_spec = importlib.util.spec_from_file_location("ella_debug_metadata_test_module", _module_path)
debug_metadata = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(debug_metadata)


def test_list_conversation_metadata_proxies_by_resolved_agent(monkeypatch):
    async def fake_resolve_user_routing(uid):
        return {"routing": {"agentId": "ella-omi-test"}}

    async def fake_fetch(path, params=None):
        return {"count": 1, "items": [{"conversation_id": "conv-123"}], "path": path, "params": params}

    monkeypatch.setattr(debug_metadata, "resolve_user_routing", fake_resolve_user_routing)
    monkeypatch.setattr(debug_metadata, "_fetch_provision_metadata", fake_fetch)
    monkeypatch.setattr(debug_metadata, "DEBUG_METADATA_ENABLED", True)

    response = Response()
    result = asyncio.run(
        debug_metadata.list_conversation_metadata(
            response,
            uid="user-123",
            limit=25,
            authenticated_uid="user-123",
        )
    )

    assert result["uid"] == "user-123"
    assert result["agent_id"] == "ella-omi-test"
    assert result["path"] == "/workspace/ella-omi-test/metadata/conversations"
    assert result["params"] == {"limit": 25}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Ella-Visibility"] == "internal"


def test_read_conversation_metadata_requires_resolved_agent(monkeypatch):
    async def fake_resolve_user_routing(uid):
        return {"routing": {}}

    monkeypatch.setattr(debug_metadata, "resolve_user_routing", fake_resolve_user_routing)
    monkeypatch.setattr(debug_metadata, "DEBUG_METADATA_ENABLED", True)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            debug_metadata.read_conversation_metadata(
                "conv-123",
                Response(),
                uid="user-123",
                authenticated_uid="user-123",
            )
        )

    assert excinfo.value.status_code == 404


def test_mounted_debug_metadata_defaults_off_and_rejects_cross_owner_before_resolution(monkeypatch):
    effects = []

    def verify(token):
        if token == "token-a":
            return {"uid": "uid-a"}
        raise ValueError("invalid")

    async def forbidden_resolve(_uid):
        effects.append("resolve")
        raise AssertionError("unauthorized debug request reached routing resolution")

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify)
    monkeypatch.setattr(debug_metadata, "resolve_user_routing", forbidden_resolve)
    app = FastAPI()
    app.include_router(debug_metadata.router)
    client = TestClient(app)

    monkeypatch.setattr(debug_metadata, "DEBUG_METADATA_ENABLED", False)
    assert (
        client.get(
            "/v1/ella/debug/conversations/metadata?uid=uid-a",
            headers={"Authorization": "Bearer token-a"},
        ).status_code
        == 404
    )
    monkeypatch.setattr(debug_metadata, "DEBUG_METADATA_ENABLED", True)
    assert client.get("/v1/ella/debug/conversations/metadata?uid=uid-a").status_code == 401
    assert (
        client.get(
            "/v1/ella/debug/conversations/metadata?uid=uid-b",
            headers={"Authorization": "Bearer token-a"},
        ).status_code
        == 403
    )
    assert effects == []
