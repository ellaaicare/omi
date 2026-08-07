"""Mounted contract tests for the reviewed Ella route integration."""

import asyncio
import ast
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from ella.services.ai_consent import build_account_deletion_receipt
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
    runtime_module.retained_owner_uid_configured = lambda _uid: False
    runtime_errors_module = types.ModuleType("ella.services.runtime_errors")

    class ProvisioningError(Exception):
        def __init__(self, code, *, retryable=False, detail=None):
            super().__init__(code)
            self.code = code
            self.retryable = retryable
            self.detail = detail or {}

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
        if isinstance(runtime, BaseException):
            raise runtime
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


class _HostileRuntimeError(RuntimeError):
    def __init__(self):
        super().__init__(
            "endpoint=https://secret token=secret session=secret-session workspace=/secret/workspace provider_payload=SECRET"
        )
        self.endpoint = "https://secret"
        self.token = "secret"
        self.session = "secret-session"
        self.workspace = "/secret/workspace"
        self.provider_payload = {"private": "SECRET"}


def _assert_runtime_material_absent(value):
    serialized = str(value)
    for forbidden in (
        "https://secret",
        "token=secret",
        "secret-session",
        "/secret/workspace",
        "provider_payload",
        "SECRET",
    ):
        assert forbidden not in serialized


def test_resolve_unexpected_runtime_failure_logs_fixed_content_free_classification(monkeypatch, caplog):
    pool = _ResolvePool()
    client, runtime_calls = _resolve_client(monkeypatch, pool, runtime=_HostileRuntimeError())

    with caplog.at_level("ERROR", logger=resolve.__name__):
        response = client.get(
            "/v1/ella/resolve",
            headers={"Authorization": "Bearer valid-a"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "user": {"omiUid": "uid-a", "status": "active"},
        "routing": {"available": False, "clusterStatus": None, "platform": None},
    }
    assert runtime_calls == [("uid-a", ("repository", pool), "hermes-cloud-chat")]
    assert [record.getMessage() for record in caplog.records] == [
        "code=ella_resolve_runtime_authority_error classification=unexpected"
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    _assert_runtime_material_absent(caplog.text)
    _assert_runtime_material_absent(response.text)


def test_resolve_expected_provisioning_failure_logs_fixed_content_free_classification(monkeypatch, caplog):
    pool = _ResolvePool()
    hostile_code = "runtime_missing endpoint=https://secret token=secret"
    error = resolve.ProvisioningError(
        hostile_code,
        retryable=True,
        detail={"session": "secret-session", "workspace": "/secret/workspace"},
    )
    client, _runtime_calls = _resolve_client(monkeypatch, pool, runtime=error)

    with caplog.at_level("INFO", logger=resolve.__name__):
        response = client.get(
            "/v1/ella/resolve",
            headers={"Authorization": "Bearer valid-a"},
        )

    assert response.status_code == 200
    assert response.json()["routing"] == {"available": False, "clusterStatus": None, "platform": None}
    assert [record.getMessage() for record in caplog.records] == [
        "code=ella_resolve_runtime_unavailable classification=provisioning"
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    _assert_runtime_material_absent(caplog.text)
    _assert_runtime_material_absent(response.text)


def test_legacy_history_proxy_unexpected_failure_logs_no_runtime_material(monkeypatch, caplog):
    async def no_runtime(*_args, **_kwargs):
        return None

    async def owned_routing(uid):
        assert uid == "uid-a"
        return {"routing": {"agentId": "agent-a"}}

    class HostileAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            raise _HostileRuntimeError()

    monkeypatch.setattr(resolve, "_pool", _ResolvePool())
    monkeypatch.setattr(resolve, "EllaProvisioningRepository", lambda pool: ("repository", pool))
    monkeypatch.setattr(resolve, "resolve_isolated_runtime", no_runtime)
    monkeypatch.setattr(resolve, "resolve_user_routing", owned_routing)
    monkeypatch.setattr(resolve.httpx, "AsyncClient", HostileAsyncClient)

    with caplog.at_level("ERROR", logger=resolve.__name__):
        response = asyncio.run(resolve.proxy_chat_history("agent-a", authenticated_uid="uid-a"))

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "provision_unreachable"}
    assert [record.getMessage() for record in caplog.records] == [
        "code=ella_legacy_history_proxy_unavailable classification=unexpected"
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    _assert_runtime_material_absent(caplog.text)
    _assert_runtime_material_absent(response.body)


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
        "build_account_deletion_receipt": build_account_deletion_receipt,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "backend/routers/users.py", "exec"), namespace)
    return namespace["delete_account"], firestore_delete, firebase_delete


def test_account_deletion_completes_unlink_receipt_without_destructive_removal():
    # The confirmation handler MUST keep the deletion-receipt contract the
    # client enforces (HTTP 200, status ok, completed/account_and_user_data
    # receipt, aidel_ request id) while NOT running the destructive wipe
    # (Firestore subcollections / Firebase auth identity) synchronously — the
    # deep data wipe is deferred to the GC/retention pipeline. Guard against
    # both destructive removals being gate-crashed on a stale session token.
    route, firestore_delete, firebase_delete = _load_delete_account_route()
    app = FastAPI()
    app.add_api_route("/v1/users/delete-account", route, methods=["DELETE"])

    with TestClient(app) as client:
        response = client.delete("/v1/users/delete-account")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["deletion_receipt"]["status"] == "completed"
    assert body["deletion_receipt"]["scope"] == "account_and_user_data"
    assert re.match(r"^aidel_[A-Za-z0-9_-]{16,128}$", body["deletion_receipt"]["request_id"])
    assert "server_completed_at" in body["deletion_receipt"]
    firestore_delete.assert_not_called()
    firebase_delete.assert_not_called()
