import asyncio
import ast
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import time
import uuid
from zipfile import ZipFile

from fastapi import Depends, FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
import pytest

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("STORAGE_EMULATOR_HOST", "http://localhost:4443")
os.environ.setdefault("GCLOUD_PROJECT", "omi-ci")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "omi-ci")
os.environ.setdefault("ENCRYPTION_SECRET", "omi_ci_content_fence_test_key_32b")

from database import account_deletion as account_deletion_db
from database import content_write_fence
from ella.routers import ai_consent
from ella.services import account_deletion as account_deletion_service
from utils.other import endpoints as auth_endpoints

_previous_notifications = sys.modules.get("utils.notifications")
sys.modules["utils.notifications"] = SimpleNamespace(send_notification=lambda **_kwargs: None)
try:
    from routers import announcements, imports as imports_router
    from utils.imports import limitless
finally:
    if _previous_notifications is None:
        sys.modules.pop("utils.notifications", None)
    else:
        sys.modules["utils.notifications"] = _previous_notifications

UID = "distributed-fence-user"


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


@pytest.fixture
def fence_environment(monkeypatch):
    previous_database = content_write_fence._firestore_db
    database = _Firestore()
    content_write_fence.configure_firestore_db(database)
    monkeypatch.setattr(content_write_fence.firestore, "transactional", _transactional)
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_LEASE_SECONDS", "3")
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_DRAIN_SECONDS", "3")

    async def purge_routing_traces(_uid):
        return 0

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_routing_traces",
        purge_routing_traces,
    )
    yield database
    content_write_fence.configure_firestore_db(previous_database)


def _fence_state(database):
    reference = content_write_fence._fence_reference(database, UID)
    return deepcopy(database.documents.get(reference.key))


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


def _load_voice_entitlement_route():
    path = Path(__file__).resolve().parents[2] / "ella" / "routers" / "voice.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    route = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_voice_entitlement"
    )
    route.decorator_list = []
    namespace = {
        "Depends": Depends,
        "auth": auth_endpoints,
        "voice_canary_db": SimpleNamespace(get_entitlement_contract=None),
        "uuid": uuid,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), str(path), "exec"), namespace)
    return namespace["get_voice_entitlement"], namespace


def _production_app(delete_route):
    app = FastAPI()
    app.include_router(announcements.router)
    app.add_api_route("/v1/users/delete-account", delete_route, methods=["DELETE"])
    app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: UID
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)
    return app


@pytest.mark.parametrize("authority_enabled", [True, False])
def test_adversarial_production_route_writer_is_drained_before_deletion_reports_complete(
    monkeypatch,
    fence_environment,
    authority_enabled,
):
    """Reproduce Iris's barrier on mounted writer and deletion entrypoints."""
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true" if authority_enabled else "false")
    mutation_reached = threading.Event()
    release_mutation = threading.Event()
    content = set()
    order = []

    async def assert_postgres_active(uid):
        assert uid == UID

    monkeypatch.setattr(content_write_fence, "_assert_postgres_owner_active", assert_postgres_active)

    async def quarantine(uid):
        assert uid == UID
        order.append("postgres_tombstone")
        return _state()

    async def finalize(uid):
        assert uid == UID
        assert not content
        order.append("final_absence_proof")
        return True

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", finalize)
    monkeypatch.setattr(announcements, "get_announcement_by_id", lambda _announcement_id: object())

    def dismiss(uid, announcement_id, cta_clicked):
        assert (uid, announcement_id, cta_clicked) == (UID, "notice", False)
        mutation_reached.set()
        assert release_mutation.wait(5)
        content.add("dismissal")
        order.append("writer_commit")
        return True

    def delete_firestore(uid):
        assert uid == UID
        order.append("firestore_sweep")
        content.clear()

    def delete_firebase(uid):
        assert uid == UID
        assert not content
        expected_prior = "final_absence_proof" if authority_enabled else "firestore_sweep"
        assert order[-1] == expected_prior
        order.append("firebase_delete")

    monkeypatch.setattr(announcements, "dismiss_announcement", dismiss)
    delete_route, delete_namespace = _load_production_delete_route()
    delete_namespace["delete_user_data"] = delete_firestore
    delete_namespace["auth"].delete_account = delete_firebase

    app = _production_app(delete_route)
    with TestClient(app) as writer_client, TestClient(app) as deletion_client:
        responses = {}

        def writer_request():
            responses["writer"] = writer_client.post(
                "/v1/announcements/notice/dismiss",
                json={"cta_clicked": False},
            )

        def deletion_request():
            responses["deletion"] = deletion_client.delete("/v1/users/delete-account")

        writer = threading.Thread(target=writer_request)
        deletion = threading.Thread(target=deletion_request)
        writer.start()
        assert mutation_reached.wait(2)
        writer_state = _fence_state(fence_environment)
        assert writer_state["state"] == content_write_fence.ACTIVE
        assert len(writer_state["writers"]) == 1

        deletion.start()
        if not authority_enabled:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = _fence_state(fence_environment)
                if state and state["state"] == content_write_fence.DRAINING:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("legacy deletion did not durably drain the admitted writer")
        assert "firestore_sweep" not in order

        release_mutation.set()
        writer.join(3)
        deletion.join(3)

        assert responses["writer"].status_code == 200
        assert responses["deletion"].status_code == 200, (
            responses["deletion"].json(),
            order,
            _fence_state(fence_environment),
        )
        assert order.index("writer_commit") < order.index("firestore_sweep")
        if authority_enabled:
            assert order.index("postgres_tombstone") < order.index("firestore_sweep")
            assert order.index("firestore_sweep") < order.index("final_absence_proof")
            assert order.index("final_absence_proof") < order.index("firebase_delete")
        else:
            assert order.index("firestore_sweep") < order.index("firebase_delete")
        assert not content
        assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED

        fresh = writer_client.post(
            "/v1/announcements/notice/dismiss",
            json={"cta_clicked": False},
        )
        assert fresh.status_code == 403
        assert fresh.json()["detail"]["code"] == "account_write_forbidden"
        assert not content


def test_active_writer_positive_control_commits_without_deletion(monkeypatch, fence_environment):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(announcements, "get_announcement_by_id", lambda _announcement_id: object())
    mutations = []
    monkeypatch.setattr(
        announcements,
        "dismiss_announcement",
        lambda uid, *_args: mutations.append(uid) or True,
    )

    delete_route, _delete_namespace = _load_production_delete_route()
    with TestClient(_production_app(delete_route)) as client:
        response = client.post("/v1/announcements/notice/dismiss", json={"cta_clicked": False})

    assert response.status_code == 200
    assert mutations == [UID]
    assert _fence_state(fence_environment) is None


def test_request_fence_covers_streamed_commit_after_route_returns(monkeypatch, fence_environment):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    stream_entered = threading.Event()
    release_stream = threading.Event()
    content = set()

    app = FastAPI()

    @app.post("/stream")
    async def stream(uid: str = Depends(auth_endpoints.get_writable_user_uid)):
        async def body():
            stream_entered.set()
            await asyncio.to_thread(release_stream.wait, 2)
            content.add(uid)
            yield b"done"

        return StreamingResponse(body())

    app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: UID
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)

    with TestClient(app) as writer_client:
        response = {}
        writer = threading.Thread(target=lambda: response.setdefault("value", writer_client.post("/stream")))
        writer.start()
        assert stream_entered.wait(2)

        deletion = {}
        deletion_thread = threading.Thread(
            target=lambda: deletion.setdefault(
                "value",
                asyncio.run(content_write_fence.tombstone_content_writes(UID)),
            )
        )
        deletion_thread.start()
        time.sleep(0.1)
        assert deletion_thread.is_alive()
        assert not content

        release_stream.set()
        writer.join(3)
        deletion_thread.join(3)

    assert response["value"].status_code == 200
    assert deletion == {"value": True}
    assert content == {UID}
    assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED


def test_two_independent_workers_are_durably_drained(monkeypatch, fence_environment):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def worker(index):
        async def run():
            async with content_write_fence.content_write_fence(UID):
                entered[index].set()
                await asyncio.to_thread(release.wait, 2)

        asyncio.run(run())

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for worker_thread in workers:
        worker_thread.start()
    assert all(event.wait(2) for event in entered)
    assert len(_fence_state(fence_environment)["writers"]) == 2

    result = {}

    def delete():
        result["tombstoned"] = asyncio.run(content_write_fence.tombstone_content_writes(UID))

    deletion = threading.Thread(target=delete)
    deletion.start()
    time.sleep(0.1)
    assert deletion.is_alive()
    assert _fence_state(fence_environment)["state"] == content_write_fence.DRAINING
    release.set()
    for worker_thread in workers:
        worker_thread.join(3)
    deletion.join(3)

    assert result == {"tombstoned": True}
    assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED


def test_detached_writer_transfers_child_registration_before_parent_release(monkeypatch, fence_environment):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    child_entered = threading.Event()
    release_child = threading.Event()
    order = []

    def child_commit():
        child_entered.set()
        assert release_child.wait(3)
        content_write_fence.assert_detached_content_writer_current(UID)
        order.append("child_commit")

    async def parent():
        async with content_write_fence.detached_content_write_fence(UID):
            content_write_fence.start_content_writer_thread(UID, child_commit, name="nested-content-writer")
            assert child_entered.wait(2)

    asyncio.run(parent())
    assert len(_fence_state(fence_environment)["writers"]) == 1

    def delete():
        assert asyncio.run(content_write_fence.tombstone_content_writes(UID)) is True
        order.append("tombstoned")

    deletion = threading.Thread(target=delete)
    deletion.start()
    time.sleep(0.1)
    assert deletion.is_alive()

    release_child.set()
    deletion.join(3)
    assert order == ["child_commit", "tombstoned"]
    assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED


def test_expired_registration_never_proves_a_live_writer_absent(monkeypatch, fence_environment):
    """Exercise the exact blocked-event-loop ordering from Iris's review."""
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_LEASE_SECONDS", "1")
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_DRAIN_SECONDS", "3")
    entered = threading.Event()
    order = []

    def writer():
        async def run():
            async with content_write_fence.content_write_fence(UID):
                entered.set()
                time.sleep(1.5)
                order.append("writer_commit")

        asyncio.run(run())

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(2)
    time.sleep(1.1)

    def delete():
        assert asyncio.run(content_write_fence.tombstone_content_writes(UID)) is True
        order.append("tombstoned")

    deletion_thread = threading.Thread(target=delete)
    deletion_thread.start()
    writer_thread.join(3)
    deletion_thread.join(3)

    assert order == ["writer_commit", "tombstoned"]
    assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED


@pytest.mark.parametrize("exit_kind", ["error", "cancellation"])
def test_error_and_cancellation_release_writer_lease(monkeypatch, fence_environment, exit_kind):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")

    async def run():
        entered = asyncio.Event()

        async def write():
            async with content_write_fence.content_write_fence(UID):
                entered.set()
                if exit_kind == "error":
                    raise RuntimeError("synthetic_writer_error")
                await asyncio.Event().wait()

        task = asyncio.create_task(write())
        await entered.wait()
        if exit_kind == "cancellation":
            task.cancel()
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await task

    asyncio.run(run())
    assert _fence_state(fence_environment) is None


def test_authority_disabled_never_opens_postgres(monkeypatch, fence_environment):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")

    async def forbidden_pool():
        raise AssertionError("legacy content fencing must not open Ella PostgreSQL")

    monkeypatch.setattr(content_write_fence.voice_canary, "get_pool", forbidden_pool)

    async def run():
        async with content_write_fence.content_write_fence(UID):
            return "committed"

    assert asyncio.run(run()) == "committed"


class _PoolTransaction:
    async def start(self):
        return None

    async def rollback(self):
        return None


class _PoolConnection:
    def transaction(self):
        return _PoolTransaction()

    async def fetchval(self, _query, *_args):
        return content_write_fence.ACTIVE


class _BoundedPool:
    def __init__(self, capacity):
        self.semaphore = asyncio.Semaphore(capacity)
        self.active = 0
        self.maximum_active = 0

    def acquire(self):
        pool = self

        @asynccontextmanager
        async def manager():
            await pool.semaphore.acquire()
            pool.active += 1
            pool.maximum_active = max(pool.maximum_active, pool.active)
            try:
                yield _PoolConnection()
            finally:
                pool.active -= 1
                pool.semaphore.release()

        return manager()


@pytest.mark.parametrize("route_kind", ["consent", "entitlement"])
def test_mounted_nested_database_routes_release_admission_connection_before_handler(
    monkeypatch,
    fence_environment,
    route_kind,
):
    """Pool-size-N requests must all reach their real nested DB boundary."""
    capacity = 2

    async def scenario():
        pool = _BoundedPool(capacity)
        nested_count = 0
        all_nested = asyncio.Event()
        nested_lock = asyncio.Lock()

        async def get_pool():
            return pool

        async def resolve_owner(_connection, *, uid):
            assert uid == UID
            return SimpleNamespace(account_user_id="account", profile_user_id="profile")

        async def acquire_lock(_connection, *, owner):
            assert owner.account_user_id == "account"
            return object()

        async def verify_owner(_connection, *, uid, owner, proof):
            assert (uid, owner.profile_user_id, proof is not None) == (UID, "profile", True)
            return "database-user"

        async def nested_database_call(result):
            nonlocal nested_count
            async with pool.acquire():
                async with nested_lock:
                    nested_count += 1
                    if nested_count == capacity:
                        all_nested.set()
                await asyncio.wait_for(all_nested.wait(), timeout=1)
                return result

        monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr(content_write_fence.voice_canary, "get_pool", get_pool)
        monkeypatch.setattr(
            content_write_fence.authority_advisory_lock,
            "resolve_self_owner_unlocked",
            resolve_owner,
        )
        monkeypatch.setattr(
            content_write_fence.authority_advisory_lock,
            "acquire_authority_lock",
            acquire_lock,
        )
        monkeypatch.setattr(
            content_write_fence.authority_advisory_lock,
            "verify_self_owner_after_lock",
            verify_owner,
        )

        app = FastAPI()
        if route_kind == "consent":

            async def submit_authority(**_kwargs):
                return await nested_database_call({"status": "ok"})

            monkeypatch.setattr(ai_consent, "submit_with_managed_cloud_authority", submit_authority)
            monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: object())
            app.include_router(ai_consent.router)
            method = "POST"
            path = "/v1/users/ai-consent"
            body = {
                "decision": "granted",
                "policy_version": "ai-data-processors-v8",
                "processor_set_hash": "sha256:" + ("1" * 64),
                "request_id": "synthetic-request",
                "app_version": "1.0",
                "build_number": "1",
                "locale": "en-US",
                "scope_version": "scope-v1",
                "scope_hash": "sha256:" + ("2" * 64),
            }
        else:

            async def entitlement_contract(uid):
                assert uid == UID
                return await nested_database_call({"entitled": True})

            entitlement_route, entitlement_namespace = _load_voice_entitlement_route()
            entitlement_namespace["voice_canary_db"].get_entitlement_contract = entitlement_contract
            app.add_api_route("/v1/entitlement", entitlement_route, methods=["GET"])
            method = "GET"
            path = "/v1/entitlement"
            body = None

        app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: UID
        app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            responses = await asyncio.wait_for(
                asyncio.gather(*(client.request(method, path, json=body) for _index in range(capacity))),
                timeout=2,
            )

        assert [response.status_code for response in responses] == [200, 200]
        assert nested_count == capacity
        assert pool.maximum_active == capacity
        assert pool.active == 0

    asyncio.run(scenario())


def test_mounted_limitless_worker_transfers_fence_until_actual_upsert_finishes(
    monkeypatch,
    fence_environment,
    tmp_path,
):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(imports_router, "TEMP_DIR", str(tmp_path))
    upsert_entered = threading.Event()
    release_upsert = threading.Event()
    worker_finished = threading.Event()
    conversations = set()
    job_updates = []

    job = SimpleNamespace(id="limitless-job", status=SimpleNamespace(value="pending"))
    monkeypatch.setattr(imports_router, "create_import_job", lambda uid, _source: job)
    monkeypatch.setattr(
        imports_router.import_jobs_db,
        "update_import_job",
        lambda job_id, update: job_updates.append((job_id, deepcopy(update))),
    )

    def upsert(uid, conversation):
        assert uid == UID
        upsert_entered.set()
        assert release_upsert.wait(5)
        conversations.add(conversation["id"])

    monkeypatch.setattr(limitless.conversations_db, "upsert_conversation", upsert, raising=False)
    monkeypatch.setattr(limitless, "send_notification", lambda **_kwargs: worker_finished.set())

    archive = io.BytesIO()
    with ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "lifelogs/2026-08-02_12h00m00s_Synthetic.md",
            "# Synthetic\n\n## Summary\n\n> [1](#startMs=1000&endMs=2000): Test content",
        )

    app = FastAPI()
    app.include_router(imports_router.router)
    app.dependency_overrides[auth_endpoints.get_current_user_uid] = lambda: UID
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)

    with TestClient(app) as client:
        response = client.post(
            "/v1/import/limitless",
            files={"file": ("synthetic.zip", archive.getvalue(), "application/zip")},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert upsert_entered.wait(2)
        assert len(_fence_state(fence_environment)["writers"]) == 1

        deletion_result = {}

        def delete():
            async def run():
                assert await content_write_fence.tombstone_content_writes(UID) is True
                conversations.clear()
                deletion_result["complete"] = True

            asyncio.run(run())

        deletion = threading.Thread(target=delete)
        deletion.start()
        time.sleep(0.1)
        assert deletion.is_alive()
        assert not deletion_result

        release_upsert.set()
        assert worker_finished.wait(3)
        deletion.join(3)

    assert deletion_result == {"complete": True}
    assert not conversations
    assert any(update.get("status") == "completed" for _job_id, update in job_updates)
    assert _fence_state(fence_environment)["state"] == content_write_fence.TOMBSTONED


def test_mounted_authenticated_writer_inventory_and_detached_lifetimes_are_exact():
    backend = Path(__file__).resolve().parents[2]
    endpoints_source = (backend / "utils" / "other" / "endpoints.py").read_text(encoding="utf-8")
    endpoints_tree = ast.parse(endpoints_source)
    writable_dependency = next(
        node
        for node in endpoints_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_writable_user_uid"
    )
    assert any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(writable_dependency))
    assert "content_write_fence.request_content_write_fence(uid)" in (
        ast.get_source_segment(endpoints_source, writable_dependency) or ""
    )

    dependency_source = (backend / "dependencies.py").read_text(encoding="utf-8")
    dependency_tree = ast.parse(dependency_source)
    api_writer_dependencies = {
        "get_current_user_id",
        "get_uid_from_mcp_api_key",
        "get_uid_with_conversations_write",
        "get_uid_with_memories_write",
        "get_uid_with_action_items_write",
    }
    functions = {
        node.name: node for node in dependency_tree.body if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert api_writer_dependencies <= set(functions)
    for name in api_writer_dependencies:
        assert any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(functions[name])), name

    raw_mutation_dependencies = []
    protected_mutations = []
    mutation_routes = []
    for directory in (backend / "routers", backend / "ella" / "routers"):
        for path in sorted(directory.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                methods = {
                    decorator.func.attr
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"post", "put", "patch", "delete", "websocket"}
                }
                if not methods:
                    continue
                function_source = ast.get_source_segment(source, node) or ""
                key = f"{path.relative_to(backend)}:{node.name}"
                mutation_routes.append(key)
                if (
                    "Depends(auth.get_writable_user_uid)" in function_source
                    or "Depends(auth_endpoints.get_writable_user_uid)" in function_source
                    or "Depends(require_current_ai_consent)" in function_source
                ):
                    protected_mutations.append(key)
                if (
                    "Depends(auth.get_current_user_uid)" in function_source
                    or "Depends(auth_endpoints.get_current_user_uid)" in function_source
                ):
                    raw_mutation_dependencies.append(key)

    exact_protected = sorted(set(protected_mutations))
    exact_inventory_hash = hashlib.sha256("\n".join(exact_protected).encode("utf-8")).hexdigest()
    assert len(exact_protected) == 133
    assert exact_inventory_hash == "34e4792e5e7e04cf26e50e3af31d6abf4936e8f49637adcb151b457ca0bd3a36"
    assert raw_mutation_dependencies == []
    assert "ella/routers/trace.py:ingest_client_trace" in protected_mutations

    exact_mutations = sorted(set(mutation_routes))
    exact_mutation_hash = hashlib.sha256("\n".join(exact_mutations).encode("utf-8")).hexdigest()
    assert len(exact_mutations) == 229
    assert exact_mutation_hash == "aec1004c1c066d2c3fa31cb18ec978811f9c33a88df1cf16a2d3f406f489c2d5"
    mutations_without_writable_authority = sorted(set(exact_mutations) - set(exact_protected))
    unauthenticated_inventory_hash = hashlib.sha256(
        "\n".join(mutations_without_writable_authority).encode("utf-8")
    ).hexdigest()
    assert len(mutations_without_writable_authority) == 96
    assert unauthenticated_inventory_hash == "a002a673c6ea5c5f04a7a558ccfce9f466feafde568899bc8b3ebe8be6cabd8d"
    assert "ella/routers/trace.py:ingest_client_trace" not in mutations_without_writable_authority
    assert "routers/announcements.py:dismiss_announcement_endpoint" in protected_mutations
    assert "routers/transcribe.py:listen_handler" in protected_mutations
    transcribe_source = (backend / "routers" / "transcribe.py").read_text(encoding="utf-8")
    assert "async with content_write_fence.request_content_write_fence(uid):" in transcribe_source
    main_source = (backend / "main.py").read_text(encoding="utf-8")
    assert "app.add_middleware(ContentWriteFenceMiddleware)" in main_source
    apps_source = (backend / "routers" / "apps.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(migrate_memories" not in apps_source
    assert "background_tasks.add_task(migrate_memories" in apps_source
    for relative_path in ("routers/integration.py", "routers/workflow.py", "routers/mcp_sse.py"):
        source = (backend / relative_path).read_text(encoding="utf-8")
        assert "await admit_authenticated_content_writer(" in source, relative_path
    assert "await auth.assert_authenticated_user_writable(uid)" not in transcribe_source

    expected_transferred_threads = {
        "routers/action_items.py:create_action_item",
        "routers/chat.py:send_message",
        "routers/developer.py:create_memories_batch",
        "routers/developer.py:create_memory",
        "routers/imports.py:import_limitless_data",
        "routers/mcp.py:create_memory",
        "routers/memories.py:create_memory",
        "routers/memories.py:update_memory_visibility",
        "routers/sync.py:get_audio_signed_urls_endpoint",
        "routers/sync.py:precache_conversation_audio_endpoint",
        "routers/wrapped.py:generate_wrapped",
    }
    transferred_threads = set()
    direct_thread_routes = set()
    detached_task_routes = set()
    raw_task_functions = set()
    for directory in (backend / "routers", backend / "ella" / "routers"):
        for path in sorted(directory.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                key = f"{path.relative_to(backend)}:{node.name}"
                calls = {
                    child.func.attr if isinstance(child.func, ast.Attribute) else child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, (ast.Attribute, ast.Name))
                }
                if "safe_create_task" in calls:
                    detached_task_routes.add(key)
                if "create_task" in calls:
                    raw_task_functions.add(key)
                methods = {
                    decorator.func.attr
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete", "websocket"}
                }
                if not methods:
                    continue
                if "start_content_writer_thread" in calls:
                    transferred_threads.add(key)
                if "Thread" in calls:
                    direct_thread_routes.add(key)

    assert transferred_threads == expected_transferred_threads
    assert direct_thread_routes == {"routers/sync.py:sync_local_files"}
    assert detached_task_routes == {
        "routers/pusher.py:_websocket_util_trigger",
        "routers/pusher.py:receive_tasks",
    }
    assert raw_task_functions == {
        "ella/routers/chat.py:_stream_level_4_openclaw",
        "ella/routers/voice.py:get_voice_context",
        "ella/routers/voice.py:heartbeat_voice_canary_session",
        "routers/pusher.py:_websocket_util_trigger",
        "routers/transcribe.py:_create_speech_profile_loader_task",
        "routers/transcribe.py:_send_message_event",
        "routers/transcribe.py:_stream_handler",
        "routers/transcribe.py:flush_stt_buffer",
        "routers/transcribe.py:receive_data",
        "routers/transcribe.py:speaker_identification_task",
    }
    assert "ella/routers/trace.py:record_trace" not in raw_task_functions
    sync_source = (backend / "routers" / "sync.py").read_text(encoding="utf-8")
    assert "[t.join() for t in threads" in sync_source
    assert "await content_write_fence.start_content_writer_task(" in transcribe_source
    assert "safe_create_task(process_photo" not in transcribe_source

    pusher_source = (backend / "routers" / "pusher.py").read_text(encoding="utf-8")
    assert "async with content_write_fence.detached_content_write_fence(uid):" in pusher_source
    assert "_process_conversation_task_with_fence(" in pusher_source

    processing_source = (backend / "utils" / "conversations" / "process_conversation.py").read_text(encoding="utf-8")
    assert processing_source.count("content_write_fence.start_content_writer_thread(") == 6
    for escaped_target in (
        "threading.Thread(target=_extract_memories",
        "threading.Thread(target=_extract_trends",
        "threading.Thread(target=_save_action_items",
        "threading.Thread(target=_update_goal_progress",
        "threading.Thread(target=update_personas_async",
    ):
        assert escaped_target not in processing_source

    storage_source = (backend / "utils" / "other" / "storage.py").read_text(encoding="utf-8")
    assert storage_source.count("content_write_fence.start_content_writer_thread(") == 2
    assert "threading.Thread(" not in storage_source
