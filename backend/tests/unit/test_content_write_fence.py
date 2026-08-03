import asyncio
import ast
from contextlib import asynccontextmanager
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

from fastapi import Depends, FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GCLOUD_PROJECT", "omi-ci")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "omi-ci")

from database import account_deletion as account_deletion_db
from database import content_write_fence
from ella.services import account_deletion as account_deletion_service
from routers import announcements
from utils.other import endpoints as auth_endpoints

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
    postgres_owner_lock = threading.Lock()
    mutation_reached = threading.Event()
    release_mutation = threading.Event()
    content = set()
    order = []

    @asynccontextmanager
    async def postgres_fence(uid):
        assert uid == UID
        acquired = await asyncio.to_thread(postgres_owner_lock.acquire, True, 2)
        assert acquired
        try:
            yield
        finally:
            postgres_owner_lock.release()

    monkeypatch.setattr(content_write_fence, "_postgres_owner_fence", postgres_fence)

    async def quarantine(uid):
        assert uid == UID
        acquired = await asyncio.to_thread(postgres_owner_lock.acquire, True, 2)
        assert acquired
        try:
            order.append("postgres_tombstone")
            return _state()
        finally:
            postgres_owner_lock.release()

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


def test_mounted_authenticated_writer_inventory_uses_scope_holding_dependencies():
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

    assert len(protected_mutations) >= 100
    assert raw_mutation_dependencies == []
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
