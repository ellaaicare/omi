import asyncio
import ast
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.testclient import TestClient
import pytest

from database import account_deletion as account_deletion_db
from database import content_write_fence
from ella.routers import trace as trace_router
from ella.services import account_deletion as account_deletion_service
from utils.other import endpoints as auth_endpoints

UID = "trace-deletion-user"
RETAINED_UID = "retained-trace-user"


class _Snapshot:
    def __init__(self, data):
        self._data = deepcopy(data)
        self.exists = data is not None

    def to_dict(self):
        return deepcopy(self._data)


class _Document:
    def __init__(self, database, key):
        self.database = database
        self.key = key

    def get(self, transaction=None):
        del transaction
        return _Snapshot(self.database.documents.get(self.key))


class _Collection:
    def __init__(self, database, name):
        self.database = database
        self.name = name

    def document(self, document_id):
        return _Document(self.database, (self.name, document_id))


class _Transaction:
    def __init__(self, database):
        self.database = database

    def set(self, reference, data):
        self.database.documents[reference.key] = deepcopy(data)

    def delete(self, reference):
        self.database.documents.pop(reference.key, None)


class _Firestore:
    def __init__(self):
        self.documents = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        return _Transaction(self)


def _transactional(function):
    def run(transaction, *args, **kwargs):
        with transaction.database.lock:
            return function(transaction, *args, **kwargs)

    return run


def _state():
    return account_deletion_db.AccountDeletionState(
        user_found=True,
        capacity_released=True,
        authority_quarantined=True,
        external_cleanup_required=(),
        external_cleanup_references=(),
        counts={},
    )


def _load_production_delete_route():
    path = Path(__file__).resolve().parents[2] / "routers" / "users.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    route = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_account")
    route.decorator_list = []
    namespace = {
        "Depends": Depends,
        "Response": Response,
        "auth": SimpleNamespace(
            get_authenticated_user_uid=lambda: UID,
            delete_account=lambda _uid: None,
        ),
        "delete_user_data": lambda _uid: None,
        "execute_account_deletion": account_deletion_service.execute_account_deletion,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), str(path), "exec"), namespace)
    return namespace["delete_account"], namespace


class _RoutingPool:
    def __init__(self, *, hold=True, max_size=20):
        self.rows = []
        self.committed = []
        self.lock = threading.RLock()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.entered = threading.Event()
        self.entered_count = 0
        self.slots = asyncio.Semaphore(max_size)

    def seed(self, uid, note):
        with self.lock:
            self.rows.append(
                self._row(
                    (
                        f"seed-{uid}",
                        datetime.now(timezone.utc),
                        None,
                        None,
                        None,
                        None,
                        None,
                        uid,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        None,
                        0,
                        None,
                        f'["{note}"]',
                        None,
                        "{}",
                    )
                )
            )

    @staticmethod
    def _row(args):
        names = (
            "trace_id",
            "created_at",
            "endpoint",
            "method",
            "client_ip",
            "client_type",
            "client_version",
            "uid",
            "debug_level",
            "resolved_agent",
            "resolved_gateway",
            "resolved_session_key",
            "resolve_source",
            "openclaw_status",
            "openclaw_latency_ms",
            "response_status",
            "total_latency_ms",
            "error",
            "notes",
            "client_route",
            "client_headers",
        )
        return dict(zip(names, args))

    async def execute(self, query, *args):
        assert "INSERT INTO routing_traces" in query
        async with self.slots:
            with self.lock:
                self.entered_count += 1
                self.entered.set()
            deadline = asyncio.get_running_loop().time() + 30
            while not self.release.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert self.release.is_set()
            with self.lock:
                row = self._row(args)
                self.rows.append(row)
                self.committed.append(dict(row))

    async def fetch(self, _query, *params):
        uid = params[1]
        with self.lock:
            return [dict(row) for row in self.rows if row["uid"] == uid]

    def purge(self, uid):
        with self.lock:
            before = len(self.rows)
            self.rows = [row for row in self.rows if row["uid"] != uid]
            return before - len(self.rows)


@pytest.fixture
def trace_environment(monkeypatch):
    previous_database = content_write_fence._firestore_db
    firestore = _Firestore()
    content_write_fence.configure_firestore_db(firestore)
    monkeypatch.setattr(content_write_fence.firestore, "transactional", _transactional)
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_DRAIN_SECONDS", "3")
    yield firestore
    content_write_fence.configure_firestore_db(previous_database)


def _fixed_app(monkeypatch, routing_pool, identity, order):
    async def get_pool():
        return routing_pool

    async def quarantine(uid):
        assert uid == UID
        order.append("quarantine")
        return _state()

    async def purge(uid):
        assert uid == UID
        order.append("purge")
        return routing_pool.purge(uid)

    async def finalize(uid):
        assert uid == UID
        order.append("finalize")
        return True

    async def assert_postgres_active(uid):
        assert uid == identity["uid"]

    monkeypatch.setattr(trace_router, "_get_pool", get_pool)
    monkeypatch.setattr(content_write_fence, "_assert_postgres_owner_active", assert_postgres_active)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "purge_routing_traces", purge)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", finalize)

    delete_route, namespace = _load_production_delete_route()
    namespace["delete_user_data"] = lambda uid: order.append(("firestore", uid))
    namespace["auth"].delete_account = lambda uid: order.append(("firebase", uid))

    app = FastAPI()
    app.include_router(trace_router.router)
    app.add_api_route("/v1/users/delete-account", delete_route, methods=["DELETE"])
    app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: identity["uid"]
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)
    return app


async def _start_asgi_request(app, method, path, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    received = False
    response = {"status": None, "body": bytearray()}
    response_sent = asyncio.Event()

    async def receive():
        nonlocal received
        if received:
            await asyncio.sleep(3600)
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response["body"].extend(message.get("body", b""))
            if not message.get("more_body", False):
                response_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    application_task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(response_sent.wait(), timeout=3)
    return response, application_task


def _response_json(response):
    return json.loads(bytes(response["body"]))


def test_trace_probe_positive_control_reproduces_old_mounted_escape():
    memory = []
    persisted = []
    persistence_entered = threading.Event()
    release_persistence = threading.Event()
    router = APIRouter(prefix="/v1/ella/debug")

    async def persist(payload):
        persistence_entered.set()
        assert await asyncio.to_thread(release_persistence.wait, 3)
        persisted.append(dict(payload))

    @router.post("/client-trace")
    async def ingest(payload: dict):
        memory.append(dict(payload))
        asyncio.get_running_loop().create_task(persist(payload))
        return {"ok": True}

    @router.get("/trace/{uid}")
    async def read(uid: str):
        return [row for row in memory if row.get("uid") == uid]

    @router.delete("/account/{uid}")
    async def delete(uid: str):
        assert uid == UID
        return {"status": "ok"}

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        accepted = client.post("/v1/ella/debug/client-trace", json={"uid": UID, "notes": ["recoverable-note"]})
        assert accepted.status_code == 200
        assert persistence_entered.wait(2)
        assert client.delete(f"/v1/ella/debug/account/{UID}").status_code == 200
        assert client.get(f"/v1/ella/debug/trace/{UID}").json()[0]["notes"] == ["recoverable-note"]
        release_persistence.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(persisted) < 1:
            time.sleep(0.01)
        assert persisted[0]["notes"] == ["recoverable-note"]
        fresh = client.post("/v1/ella/debug/client-trace", json={"uid": UID, "notes": ["post-tombstone"]})
        assert fresh.status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(persisted) < 2:
            time.sleep(0.01)
        assert persisted[1]["notes"] == ["post-tombstone"]


def test_mounted_trace_persistence_is_drained_purged_and_tombstoned(
    monkeypatch,
    trace_environment,
):
    routing_pool = _RoutingPool()
    routing_pool.seed(RETAINED_UID, "retained-note")
    identity = {"uid": UID}
    order = []
    app = _fixed_app(monkeypatch, routing_pool, identity, order)

    async def scenario():
        accepted, accepted_task = await _start_asgi_request(
            app,
            "POST",
            "/v1/ella/debug/client-trace",
            {
                "uid": UID,
                "clientType": "ios",
                "latencyMs": 17,
                "status": 200,
                "notes": ["recoverable-sensitive-note"],
                "headers": {"X-Ella-Secret": "recoverable-header"},
                "sessionKey": "recoverable-session",
                "error": "recoverable-error",
            },
        )
        assert accepted["status"] == 200
        await asyncio.wait_for(accepted_task, timeout=2)
        assert routing_pool.entered.wait(2)
        mismatch, mismatch_task = await _start_asgi_request(
            app,
            "POST",
            "/v1/ella/debug/client-trace",
            {"uid": RETAINED_UID},
        )
        assert mismatch["status"] == 403
        await mismatch_task

        deletion = asyncio.create_task(_start_asgi_request(app, "DELETE", "/v1/users/delete-account"))
        await asyncio.sleep(0.15)
        assert not deletion.done()
        assert "purge" not in order

        routing_pool.release.set()
        deleted, deleted_task = await asyncio.wait_for(deletion, timeout=4)
        assert deleted["status"] == 200
        await deleted_task
        assert order.index("quarantine") < order.index("purge")
        assert order.index("purge") < order.index(("firestore", UID))
        assert order.index(("firestore", UID)) < order.index("finalize")
        assert order[-1] == ("firebase", UID)
        committed = next(row for row in routing_pool.committed if row["uid"] == UID)
        assert committed["notes"] == '["client-telemetry"]'
        assert committed["client_headers"] == "{}"
        assert committed["resolved_session_key"] is None
        assert committed["error"] is None

        deleted_read, deleted_read_task = await _start_asgi_request(
            app,
            "GET",
            f"/v1/ella/debug/trace/{UID}",
        )
        await deleted_read_task
        assert deleted_read["status"] == 200
        assert _response_json(deleted_read)["source"] == "database"
        assert _response_json(deleted_read)["traces"] == []
        assert not hasattr(trace_router, "_traces")
        assert all(row["uid"] != UID for row in routing_pool.rows)

        fresh, fresh_task = await _start_asgi_request(
            app,
            "POST",
            "/v1/ella/debug/client-trace",
            {"uid": UID, "latencyMs": 1},
        )
        await fresh_task
        assert fresh["status"] == 403
        assert _response_json(fresh)["detail"]["code"] == "account_write_forbidden"

        identity["uid"] = RETAINED_UID
        retained_read, retained_read_task = await _start_asgi_request(
            app,
            "GET",
            f"/v1/ella/debug/trace/{RETAINED_UID}",
        )
        await retained_read_task
        assert retained_read["status"] == 200
        assert _response_json(retained_read)["traces"][0]["notes"] == ["retained-note"]

    asyncio.run(scenario())


def test_trace_reads_require_exact_authenticated_subject(monkeypatch, trace_environment):
    routing_pool = _RoutingPool(hold=False)

    async def get_pool():
        return routing_pool

    monkeypatch.setattr(trace_router, "_get_pool", get_pool)
    app = FastAPI()
    app.include_router(trace_router.router)
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)

    with TestClient(app) as client:
        assert client.get(f"/v1/ella/debug/trace/{UID}").status_code == 401

    app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: UID
    with TestClient(app) as client:
        denied = client.get(f"/v1/ella/debug/trace/{RETAINED_UID}")
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "ownership_mismatch"


def test_concurrent_trace_requests_release_request_lifetimes_before_bounded_pool_progress(
    monkeypatch,
    trace_environment,
):
    routing_pool = _RoutingPool(max_size=2)
    identity = {"uid": UID}
    app = _fixed_app(monkeypatch, routing_pool, identity, [])

    async def scenario():
        requests = await asyncio.gather(
            *(
                _start_asgi_request(
                    app,
                    "POST",
                    "/v1/ella/debug/client-trace",
                    {"uid": UID, "clientType": "ios", "latencyMs": index},
                )
                for index in range(6)
            )
        )
        assert [response["status"] for response, _task in requests] == [200] * 6
        await asyncio.gather(*(task for _response, task in requests))
        assert routing_pool.entered.wait(2)
        assert routing_pool.entered_count == 2
        fence_key = content_write_fence._fence_reference(trace_environment, UID).key
        state = trace_environment.documents[fence_key]
        assert len(state["writers"]) == 6

        routing_pool.release.set()
        deadline = asyncio.get_running_loop().time() + 4
        while asyncio.get_running_loop().time() < deadline:
            with routing_pool.lock:
                if len(routing_pool.rows) == 6:
                    break
            await asyncio.sleep(0.01)
        assert len(routing_pool.rows) == 6
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            state = trace_environment.documents.get(fence_key)
            if state is None or not state["writers"]:
                break
            await asyncio.sleep(0.01)
        assert state is None or state["writers"] == {}

    asyncio.run(scenario())
