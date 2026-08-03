import ast
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import threading

from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GCLOUD_PROJECT", "omi-ci")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "omi-ci")
os.environ.setdefault("ENCRYPTION_SECRET", "omi_ci_durable_worker_finality_key")

if "stripe" not in sys.modules:
    stripe_stub = types.ModuleType("stripe")
    stripe_stub.api_key = None
    sys.modules["stripe"] = stripe_stub
if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = lambda *args, **kwargs: None
    sys.modules["redis"] = redis_stub
storage_stub = types.ModuleType("utils.other.storage")
storage_stub.list_audio_chunks = lambda *args, **kwargs: []
sys.modules.setdefault("utils.other.storage", storage_stub)
sys.modules.setdefault("websockets", types.ModuleType("websockets"))
vector_stub = types.ModuleType("utils.conversations.vector")
vector_stub.refresh_structured_summary_vector = lambda *args, **kwargs: None
sys.modules.setdefault("utils.conversations.vector", vector_stub)
summary_stub = types.ModuleType("utils.conversations.generic_summary")
summary_stub.generate_stock_conversation_summary = lambda *args, **kwargs: None
sys.modules.setdefault("utils.conversations.generic_summary", summary_stub)

from database import account_deletion as account_deletion_db
from database import content_write_fence
from database.memory_reinterpretations import InMemoryMemoryReinterpretationRepository
from ella.services import account_deletion as account_deletion_service
from ella.services import hermes_cloud_enrichment_outbox as enrichment_service
from ella.services import memory_reinterpretation as reinterpretation_service
from ella.services.hermes_cloud_enrichment_outbox import DeliveryResult, HermesCloudEnrichmentOutboxWorker
from ella.services.memory_reinterpretation import MemoryReinterpretationWorker, ReinterpretationPlan

UID = "startup-worker-delete-user"
OTHER_UID = "startup-worker-retained-user"


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


@pytest.fixture
def deletion_fence(monkeypatch):
    previous_database = content_write_fence._firestore_db
    firestore = _Firestore()
    content_write_fence.configure_firestore_db(firestore)
    monkeypatch.setattr(content_write_fence.firestore, "transactional", _transactional)
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_DRAIN_SECONDS", "0.15")
    monkeypatch.setenv("ELLA_CONTENT_WRITE_FENCE_LEASE_SECONDS", "3")

    async def assert_postgres_active(_uid):
        return None

    async def purge_routing_traces(_uid):
        return 0

    monkeypatch.setattr(content_write_fence, "_assert_postgres_owner_active", assert_postgres_active)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "purge_routing_traces", purge_routing_traces)
    yield firestore
    content_write_fence.configure_firestore_db(previous_database)


def _deletion_app(
    monkeypatch,
    *,
    authority_enabled,
    delete_firestore,
    delete_firebase,
    purge_memory,
):
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true" if authority_enabled else "false")

    async def quarantine(_uid):
        assert authority_enabled
        return _state()

    async def finalize(_uid):
        assert authority_enabled
        return True

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", finalize)
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db, "purge_memory_reinterpretation_work", purge_memory
    )
    route, namespace = _load_production_delete_route()
    namespace["delete_user_data"] = delete_firestore
    namespace["auth"].delete_account = delete_firebase
    app = FastAPI()
    app.add_api_route("/v1/users/delete-account", route, methods=["DELETE"])
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)
    return app


class _EnrichmentOutbox:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {
            "target": {"job_id": "target", "uid": UID, "status": "pending", "attempt_count": 0},
            "retained": {"job_id": "retained", "uid": OTHER_UID, "status": "completed", "attempt_count": 1},
        }
        self.commits = []

    def peek_next_uid(self, *, scan_limit=50):
        del scan_limit
        with self.lock:
            return next(
                (job["uid"] for job in self.jobs.values() if job["status"] in {"pending", "retryable"}),
                None,
            )

    def claim_next(self, *, uid, writer_token, lease_seconds, scan_limit=50):
        del writer_token, lease_seconds, scan_limit
        with self.lock:
            job = next(
                (
                    item
                    for item in self.jobs.values()
                    if item["uid"] == uid and item["status"] in {"pending", "retryable"}
                ),
                None,
            )
            if job is None:
                return None
            job.update(status="running", lease_token="lease", attempt_count=job["attempt_count"] + 1)
            return deepcopy(job)

    def complete(self, *, job_id, lease_token, receipt):
        del receipt
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("lease_token") != lease_token:
                return False
            job["status"] = "completed"
            self.commits.append(("complete", job["uid"]))
            return True

    def fail(self, **_kwargs):
        raise AssertionError("the successful adversarial delivery must not fail")

    def purge(self, uid):
        with self.lock:
            removed = [job_id for job_id, job in self.jobs.items() if job["uid"] == uid]
            for job_id in removed:
                del self.jobs[job_id]
            return len(removed)


@pytest.mark.parametrize("authority_enabled", [True, False])
def test_startup_enrichment_worker_is_drained_purged_and_tombstone_denied(
    monkeypatch,
    deletion_fence,
    authority_enabled,
):
    del deletion_fence
    repository = _EnrichmentOutbox()
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    worker_committed = threading.Event()
    order = []

    def deliver(job):
        assert job["uid"] == UID
        order.append("delivery_boundary")
        delivery_entered.set()
        assert release_delivery.wait(5)
        order.append("delivery_returned")
        return DeliveryResult(True, receipt={"content_free": True})

    class Worker(HermesCloudEnrichmentOutboxWorker):
        async def run_once(self):
            worked = await super().run_once()
            if repository.commits:
                worker_committed.set()
            return worked

    worker = Worker(repository, deliver=deliver, poll_seconds=0.01)

    def delete_firestore(uid):
        order.append("enrichment_purge")
        repository.purge(uid)

    def delete_firebase(uid):
        order.append(("firebase", uid))

    async def purge_memory(_uid):
        return 0

    app = _deletion_app(
        monkeypatch,
        authority_enabled=authority_enabled,
        delete_firestore=delete_firestore,
        delete_firebase=delete_firebase,
        purge_memory=purge_memory,
    )
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS", UID)
    monkeypatch.setattr(enrichment_service, "_worker_task", None)

    async def start():
        await enrichment_service.start_worker(worker)

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", enrichment_service.stop_worker)
    with TestClient(app) as client:
        assert delivery_entered.wait(3)
        pending = client.delete("/v1/users/delete-account")
        assert pending.status_code == 202
        assert ("firebase", UID) not in order
        assert repository.jobs["target"]["status"] == "running"

        release_delivery.set()
        assert worker_committed.wait(3)
        completed = client.delete("/v1/users/delete-account")
        assert completed.status_code == 200

    assert "target" not in repository.jobs
    assert repository.jobs["retained"]["uid"] == OTHER_UID
    assert order.index("delivery_returned") < order.index("enrichment_purge")
    assert order.index("enrichment_purge") < order.index(("firebase", UID))

    repository.jobs["post-tombstone"] = {
        "job_id": "post-tombstone",
        "uid": UID,
        "status": "pending",
        "attempt_count": 0,
    }
    deliveries_before = order.count("delivery_boundary")
    assert asyncio.run(worker.run_once()) is False
    assert repository.jobs["post-tombstone"]["status"] == "pending"
    assert order.count("delivery_boundary") == deliveries_before


async def _seed_reinterpretation(repository, uid, *, completed=False):
    session_id = f"session-{uid}"
    conversation_id = f"conversation-{uid}"
    version_id = f"version-{uid}"
    row = {
        "uid": uid,
        "session_id": session_id,
        "event_id": f"event-{uid}",
        "source_identity": f"source-{uid}",
        "connection_id": "connection",
        "turn_index": 0,
        "role": "user",
        "text": "The remembered detail is corrected.",
        "started_at": "2026-08-03T00:00:00Z",
        "scope_kind": "memory",
        "conversation_id": conversation_id,
        "active_summary_version_id": version_id,
    }
    completion = {
        "uid": uid,
        "session_id": session_id,
        "source_identity": f"completion-{uid}",
        "source_ref": {
            "scope_kind": "memory",
            "can_reinterpret": True,
            "conversation_id": conversation_id,
            "active_summary_version_id": version_id,
        },
    }
    job = await repository.enqueue(completion, [row])
    repository.now += timedelta(seconds=1)
    if completed:
        repository.jobs[job["id"]]["status"] = "no_change"
    return job


@pytest.mark.parametrize("authority_enabled", [True, False])
def test_startup_memory_worker_is_drained_purged_and_tombstone_denied(
    monkeypatch,
    deletion_fence,
    authority_enabled,
):
    del deletion_fence
    repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
    target = asyncio.run(_seed_reinterpretation(repository, UID))
    retained = asyncio.run(_seed_reinterpretation(repository, OTHER_UID, completed=True))
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    worker_committed = threading.Event()
    order = []

    class HeldHermes:
        calls = 0

        async def propose(self, **_kwargs):
            self.calls += 1
            order.append("memory_delivery_boundary")
            delivery_entered.set()
            assert await asyncio.to_thread(release_delivery.wait, 5)
            order.append("memory_delivery_returned")
            return ReinterpretationPlan(outcome="no_change", proposals=[])

    async def conversation_loader(uid, conversation_id):
        return {
            "id": conversation_id,
            "active_summary_version_id": f"version-{uid}",
            "structured": {"overview": "[Ella] A remembered detail."},
        }

    class Worker(MemoryReinterpretationWorker):
        async def run_once(self, worker_id):
            result = await super().run_once(worker_id)
            if result and result.get("status") == "no_change":
                worker_committed.set()
            return result

    hermes = HeldHermes()
    worker = Worker(repository, hermes_client=hermes, conversation_loader=conversation_loader, lease_seconds=30)

    async def purge_memory(uid):
        order.append("memory_purge")
        removed = [job_id for job_id, job in repository.jobs.items() if job["uid"] == uid]
        for job_id in removed:
            del repository.jobs[job_id]
        repository.attempts = [attempt for attempt in repository.attempts if attempt["job_id"] not in removed]
        return len(removed)

    def delete_firestore(uid):
        order.append(("firestore", uid))

    def delete_firebase(uid):
        order.append(("firebase", uid))

    app = _deletion_app(
        monkeypatch,
        authority_enabled=authority_enabled,
        delete_firestore=delete_firestore,
        delete_firebase=delete_firebase,
        purge_memory=purge_memory,
    )
    monkeypatch.setenv("ELLA_MEMORY_REINTERPRETATION_WORKER_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_REINTERPRETATION_IDLE_SECONDS", "0.01")
    monkeypatch.setattr(reinterpretation_service, "_worker_task", None)

    async def start():
        await reinterpretation_service.start_worker(worker)

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", reinterpretation_service.stop_worker)
    with TestClient(app) as client:
        assert delivery_entered.wait(3)
        pending = client.delete("/v1/users/delete-account")
        assert pending.status_code == 202
        assert ("firebase", UID) not in order
        assert repository.jobs[target["id"]]["status"] == "running"

        release_delivery.set()
        assert worker_committed.wait(3)
        completed = client.delete("/v1/users/delete-account")
        assert completed.status_code == 200

    assert target["id"] not in repository.jobs
    assert repository.jobs[retained["id"]]["uid"] == OTHER_UID
    assert order.index("memory_delivery_returned") < order.index("memory_purge")
    assert order.index("memory_purge") < order.index(("firebase", UID))

    post_tombstone = asyncio.run(_seed_reinterpretation(repository, UID))
    calls_before = hermes.calls
    assert asyncio.run(worker.run_once("post-tombstone-worker")) is None
    assert repository.jobs[post_tombstone["id"]]["status"] == "pending"
    assert hermes.calls == calls_before
