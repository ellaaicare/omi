"""Mounted contract tests for the reviewed Ella route integration."""

import ast
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from utils.ella import exact_firebase_auth

_BACKEND = Path(__file__).resolve().parents[2]


class _ResolvePool:
    def __init__(self):
        self.fetchrow_calls = []

    async def fetchrow(self, _query, *args):
        self.fetchrow_calls.append(args)
        return {
            "omi_uid": "uid-a",
            "status": "active",
        }


def _load_resolve_router():
    asyncpg_module = types.ModuleType("asyncpg")
    asyncpg_module.Pool = object
    asyncpg_module.create_pool = None
    sys.modules.setdefault("asyncpg", asyncpg_module)

    provisioning_module = types.ModuleType("database.ella_provisioning")
    provisioning_module.EllaProvisioningRepository = object
    sys.modules.setdefault("database.ella_provisioning", provisioning_module)

    runtime_module = types.ModuleType("ella.services.runtime_resolver")

    async def resolve_isolated_runtime(*_args, **_kwargs):
        return None

    runtime_module.resolve_isolated_runtime = resolve_isolated_runtime
    runtime_errors_module = types.ModuleType("ella.services.runtime_errors")

    class ProvisioningError(Exception):
        pass

    runtime_errors_module.ProvisioningError = ProvisioningError
    sys.modules.setdefault("ella", types.ModuleType("ella"))
    sys.modules.setdefault("ella.services", types.ModuleType("ella.services"))
    sys.modules.setdefault("ella.services.runtime_errors", runtime_errors_module)
    sys.modules.setdefault("ella.services.runtime_resolver", runtime_module)

    path = _BACKEND / "ella" / "routers" / "resolve.py"
    spec = importlib.util.spec_from_file_location("ella_reviewed_resolve_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


resolve = _load_resolve_router()


def _resolve_client(monkeypatch, pool, *, runtime):
    monkeypatch.setattr(resolve, "_pool", pool)
    runtime_calls = []

    async def resolve_runtime(uid, repository, target_mode):
        runtime_calls.append((uid, repository, target_mode))
        return runtime

    monkeypatch.setattr(resolve, "EllaProvisioningRepository", lambda active_pool: ("repository", active_pool))
    monkeypatch.setattr(resolve, "resolve_isolated_runtime", resolve_runtime)

    def verify_token(token):
        if token == "valid-a":
            return {"uid": "uid-a"}
        raise ValueError("expired or invalid")

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify_token)
    app = FastAPI()
    app.include_router(resolve.router)
    return TestClient(app), runtime_calls


def test_resolve_requires_exact_owner_before_lookup_and_returns_no_private_routing(monkeypatch):
    pool = _ResolvePool()
    runtime = SimpleNamespace(provider="hermes_cloud", status="active")
    client, runtime_calls = _resolve_client(monkeypatch, pool, runtime=runtime)

    assert client.get("/v1/ella/resolve?uid=uid-a").status_code == 401
    assert (
        client.get(
            "/v1/ella/resolve?uid=uid-b",
            headers={"Authorization": "Bearer valid-a"},
        ).status_code
        == 403
    )
    assert pool.fetchrow_calls == []

    response = client.get(
        "/v1/ella/resolve?uid=uid-a",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert pool.fetchrow_calls == [("uid-a",)]
    assert runtime_calls == [("uid-a", ("repository", pool), "hermes-cloud-chat")]
    assert response.json() == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": True, "clusterStatus": "active", "platform": "hermes_cloud"},
    }
    serialized = str(response.json()).lower()
    for forbidden in ("token", "session", "agentid", "workspace", "condition", "medication", "provision"):
        assert forbidden not in serialized


def test_resolve_retained_user_without_isolated_binding_fails_closed_without_global_fallback(monkeypatch):
    pool = _ResolvePool()
    monkeypatch.setattr(resolve, "CHAT_PLATFORM", "hermes")
    client, runtime_calls = _resolve_client(monkeypatch, pool, runtime=None)

    response = client.get(
        "/v1/ella/resolve?uid=uid-a",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": False, "clusterStatus": None, "platform": None},
    }
    assert runtime_calls == [("uid-a", ("repository", pool), "hermes-cloud-chat")]
    assert "HERMES_WORKSPACE" not in (_BACKEND / "ella" / "routers" / "resolve.py").read_text(encoding="utf-8")


def test_resolve_reports_ready_invitation_self_hosted_binding_without_legacy_workspace(monkeypatch):
    pool = _ResolvePool()
    runtime = SimpleNamespace(provider="hermes", status="active")
    client, runtime_calls = _resolve_client(monkeypatch, pool, runtime=runtime)

    response = client.get(
        "/v1/ella/resolve",
        headers={"Authorization": "Bearer valid-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": True, "clusterStatus": "active", "platform": "hermes"},
    }
    assert runtime_calls == [("uid-a", ("repository", pool), "hermes-cloud-chat")]


def _load_delete_account_route():
    source = (_BACKEND / "routers" / "users.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "delete_account")
    function.decorator_list = []
    authenticated_uid = lambda: "uid-a"
    firestore_delete = Mock()
    firebase_delete = Mock()
    auth = types.SimpleNamespace(get_current_user_uid=authenticated_uid, delete_account=firebase_delete)
    namespace = {
        "Depends": Depends,
        "HTTPException": HTTPException,
        "auth": auth,
        "delete_user_data": firestore_delete,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "backend/routers/users.py", "exec"), namespace)
    return namespace["delete_account"], firestore_delete, firebase_delete


def test_account_deletion_declines_before_firestore_or_firebase_removal():
    route, firestore_delete, firebase_delete = _load_delete_account_route()
    app = FastAPI()
    app.add_api_route("/v1/users/delete-account", route, methods=["DELETE"])

    with TestClient(app) as client:
        response = client.delete("/v1/users/delete-account")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "account_deletion_temporarily_unavailable",
            "message": "Account deletion is temporarily unavailable.",
        }
    }
    firestore_delete.assert_not_called()
    firebase_delete.assert_not_called()
