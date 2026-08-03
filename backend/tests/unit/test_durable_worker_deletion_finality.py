import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import types
import threading
import uuid

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
import httpx
import pytest
import requests

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
vector_stub.save_structured_vector = lambda *args, **kwargs: None
sys.modules.setdefault("utils.conversations.vector", vector_stub)
summary_stub = types.ModuleType("utils.conversations.generic_summary")
summary_stub.generate_stock_conversation_summary = lambda *args, **kwargs: None
sys.modules.setdefault("utils.conversations.generic_summary", summary_stub)

from database import account_deletion as account_deletion_db
from database import (
    content_write_fence,
    content_write_recovery,
    content_write_recovery_authority,
    content_writer_owner,
)
from database.memory_reinterpretations import InMemoryMemoryReinterpretationRepository
from ella.routers import canonical_events
from ella.routers.canonical_events import (
    CanonicalEventsBatch,
    InMemoryCanonicalEventStore,
    create_canonical_events_router,
)
from ella.services import account_deletion as account_deletion_service
from ella.services import hermes_cloud_enrichment_outbox as enrichment_service
from ella.services import memory_reinterpretation as reinterpretation_service
from ella.services import proposal_ingest
from ella.services.hermes_cloud_enrichment_outbox import DeliveryResult, HermesCloudEnrichmentOutboxWorker
from ella.services.memory_reinterpretation import ApplyResult, MemoryReinterpretationWorker, ReinterpretationPlan

UID = "startup-worker-delete-user"
OTHER_UID = "startup-worker-retained-user"
PRODUCTION_CURRENT_PROCESS_BOUNDARY = content_writer_owner.current_process_boundary
PRODUCTION_PROCESS_SNAPSHOT = content_writer_owner.process_snapshot


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
    def __init__(self, *, documents=None, lock=None):
        self.documents = {} if documents is None else documents
        self.lock = threading.RLock() if lock is None else lock

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        return _Transaction(self)


def _transactional(function):
    def run(transaction, *args, **kwargs):
        with transaction.database.lock:
            return function(transaction, *args, **kwargs)

    return run


def _hold_process_writer(firestore, uid, token, ready, release):
    content_write_fence.firestore.transactional = _transactional
    content_write_fence.configure_firestore_db(firestore)
    content_write_fence._acquire_firestore_writer(firestore, uid, token, 3)

    def hold_work():
        ready.set()
        release.wait(30)

    worker = threading.Thread(target=hold_work, name="real-orphan-writer-thread")
    worker.start()
    worker.join()


def _shared_firestore(context):
    manager = context.Manager()
    return manager, _Firestore(documents=manager.dict(), lock=manager.RLock())


def _start_real_writer(context, firestore, *, uid, token):
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_process_writer,
        args=(firestore, uid, token, ready, release),
    )
    process.start()
    assert ready.wait(5)
    return process, release


def _writer_record(firestore, uid, token):
    state = content_write_fence._snapshot_data(content_write_fence._fence_reference(firestore, uid).get())
    return state["writers"][token]


def _invoke_recovery(firestore, *, uid, token, transactional=_transactional):
    refusal = {
        "action": "content_writer_recovery",
        "content_free": True,
        "result": "refused",
    }
    try:
        receipt = content_write_recovery.recover_orphaned_writer(
            firestore,
            subject_hash=content_write_recovery.hash_selector(uid),
            token_hash=content_write_recovery.hash_selector(token),
            transactional=transactional,
        )
    except content_write_recovery.ContentWriterRecoveryError as exc:
        print(json.dumps({**refusal, "reason": exc.code}, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


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
    purge_canonical=None,
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
    if purge_canonical is None:

        async def purge_canonical(_uid):
            return 0

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_canonical_event_ledger",
        purge_canonical,
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


class _HeldCanonicalStore(InMemoryCanonicalEventStore):
    def __init__(self):
        super().__init__()
        self.hold_uid = None
        self.write_entered = threading.Event()
        self.release_write = threading.Event()

    async def write_batch(self, events):
        if self.hold_uid and any(event.uid == self.hold_uid for event in events):
            self.write_entered.set()
            assert await asyncio.to_thread(self.release_write.wait, 5)
        return await super().write_batch(events)

    def purge(self, uid):
        event_keys = [key for key, event in self._events.items() if event.get("uid") == uid]
        session_keys = [key for key, session in self._sessions.items() if session.get("uid") == uid]
        for key in event_keys:
            del self._events[key]
        for key in session_keys:
            del self._sessions[key]
        return len(event_keys) + len(session_keys)


def _canonical_event(uid, event_id):
    return {
        "uid": uid,
        "canonical_identity": uid,
        "event_id": event_id,
        "channel": "ios_voice",
        "provider": "mounted-adversarial-test",
        "role": "user",
        "text": "Synthetic private transcript",
        "started_at": "2026-08-03T00:00:00Z",
        "source_ref": {"source_identity": f"source:{event_id}"},
        "metadata": {"synthetic": True},
    }


def test_positive_control_legacy_mounted_canonical_route_writes_after_tombstone(deletion_fence):
    del deletion_fence
    assert asyncio.run(content_write_fence.tombstone_content_writes(UID)) is True
    store = InMemoryCanonicalEventStore()
    router = APIRouter()

    @router.post("/v1/ella/events")
    async def legacy_write(batch: CanonicalEventsBatch):
        return await store.write_batch(batch.events)

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(content_write_fence.ContentWriteFenceMiddleware)
    response = TestClient(app).post(
        "/v1/ella/events",
        json={"events": [_canonical_event(UID, "legacy-post-tombstone")]},
    )

    assert response.status_code == 200
    assert any(event["uid"] == UID for event in store._events.values())


def test_mounted_canonical_auth_drain_exact_purge_firebase_last_and_retry(monkeypatch, deletion_fence):
    del deletion_fence
    store = _HeldCanonicalStore()
    asyncio.run(
        store.write_batch(
            [canonical_events.CanonicalEventIn(**_canonical_event(OTHER_UID, "retained-canonical-event"))]
        )
    )
    store.hold_uid = UID
    order = []

    def authenticate(authorization):
        if authorization != "Bearer exact-user-token":
            raise HTTPException(status_code=401, detail="Invalid authorization token")
        return UID

    monkeypatch.setattr(canonical_events.auth, "get_authenticated_user_uid", authenticate)

    async def purge_memory(_uid):
        return 0

    async def purge_canonical(uid):
        order.append("canonical-purge")
        return store.purge(uid)

    app = _deletion_app(
        monkeypatch,
        authority_enabled=True,
        delete_firestore=lambda _uid: order.append("firestore-purge"),
        delete_firebase=lambda _uid: order.append("firebase-delete"),
        purge_memory=purge_memory,
        purge_canonical=purge_canonical,
    )
    app.include_router(create_canonical_events_router(store))

    with TestClient(app) as client:
        unauthenticated = client.post(
            "/v1/ella/events",
            json={"events": [_canonical_event(UID, "unauthenticated")]},
        )
        mismatch = client.post(
            "/v1/ella/events",
            headers={"Authorization": "Bearer exact-user-token"},
            json={"events": [_canonical_event(OTHER_UID, "cross-subject")]},
        )
        assert unauthenticated.status_code == 401
        assert mismatch.status_code == 403

        write_result = {}

        def write():
            write_result["response"] = client.post(
                "/v1/ella/events",
                headers={"Authorization": "Bearer exact-user-token"},
                json={"events": [_canonical_event(UID, "held-canonical-event")]},
            )

        writer = threading.Thread(target=write)
        writer.start()
        assert store.write_entered.wait(3)
        pending = client.delete("/v1/users/delete-account")
        assert pending.status_code == 202
        assert "firebase-delete" not in order

        store.release_write.set()
        writer.join(3)
        assert not writer.is_alive()
        assert write_result["response"].status_code == 200

        completed = client.delete("/v1/users/delete-account")
        assert completed.status_code == 200
        denied = client.post(
            "/v1/ella/events",
            headers={"Authorization": "Bearer exact-user-token"},
            json={"events": [_canonical_event(UID, "fresh-post-tombstone")]},
        )
        assert denied.status_code == 403

    assert not any(event["uid"] == UID for event in store._events.values())
    assert any(event["uid"] == OTHER_UID for event in store._events.values())
    assert order == ["canonical-purge", "firestore-purge", "firebase-delete"]


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


def test_positive_control_legacy_two_stops_release_enrichment_writer_before_thread_terminal(
    monkeypatch,
    deletion_fence,
):
    """Preserve Iris's exact old ordering as a mounted positive control."""
    del deletion_fence
    repository = _EnrichmentOutbox()
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    thread_terminal = threading.Event()
    order = []

    def deliver(_job):
        order.append("thread-entered")
        delivery_entered.set()
        assert release_delivery.wait(5)
        order.append("thread-terminal")
        thread_terminal.set()
        return DeliveryResult(True, receipt={"content_free": True})

    async def legacy_finish_admitted_content_mutation(uid, awaitable):
        content_write_fence.assert_content_writer_admitted(uid)
        task = asyncio.create_task(awaitable)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def legacy_stop_worker():
        worker_task = enrichment_service._worker_task
        if worker_task is None:
            return
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        enrichment_service._worker_task = None

    monkeypatch.setattr(
        content_write_fence,
        "finish_admitted_content_mutation",
        legacy_finish_admitted_content_mutation,
    )
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS", UID)
    monkeypatch.setattr(enrichment_service, "_worker_task", None)
    monkeypatch.setattr(enrichment_service, "_worker_shutdown_task", None)
    worker = HermesCloudEnrichmentOutboxWorker(repository, deliver=deliver, poll_seconds=0.01)

    async def purge_memory(_uid):
        return 0

    app = _deletion_app(
        monkeypatch,
        authority_enabled=True,
        delete_firestore=lambda uid: (order.append("purge"), repository.purge(uid)),
        delete_firebase=lambda _uid: order.append("firebase"),
        purge_memory=purge_memory,
    )

    async def scenario():
        await enrichment_service.start_worker(worker)
        assert await asyncio.to_thread(delivery_entered.wait, 3)
        first_stop = asyncio.create_task(legacy_stop_worker())
        await asyncio.sleep(0)
        second_stop = asyncio.create_task(legacy_stop_worker())
        await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 3)
        assert not thread_terminal.is_set()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            deleted = await client.delete("/v1/users/delete-account")
            assert deleted.status_code == 200

        release_delivery.set()
        assert await asyncio.to_thread(thread_terminal.wait, 3)

    asyncio.run(scenario())
    assert order == ["thread-entered", "purge", "firebase", "thread-terminal"]


async def _cancel_two_coalesced_stops(service):
    first_stop = asyncio.create_task(service.stop_worker())
    while service._worker_shutdown_task is None:
        await asyncio.sleep(0)
    shared_shutdown = service._worker_shutdown_task
    second_stop = asyncio.create_task(service.stop_worker())
    await asyncio.sleep(0)
    assert service._worker_shutdown_task is shared_shutdown

    first_stop.cancel()
    first_stop.cancel()
    first_stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_stop
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(second_stop, 0.05)

    assert service._worker_shutdown_task is shared_shutdown
    assert not shared_shutdown.done()
    return shared_shutdown


def test_process_restart_uses_root_cli_and_real_kernel_terminal_proof_before_firebase_last(
    monkeypatch,
    deletion_fence,
    capsys,
):
    del deletion_fence
    context = multiprocessing.get_context("fork")
    manager, firestore = _shared_firestore(context)
    token = "real-child-process-writer-token"
    same_uid_token = "retained-same-uid-writer-token"
    other_token = "retained-other-uid-writer-token"
    process, release = _start_real_writer(context, firestore, uid=UID, token=token)
    del release
    content_write_fence.configure_firestore_db(firestore)
    content_write_fence._acquire_firestore_writer(firestore, UID, same_uid_token, 3)
    content_write_fence._acquire_firestore_writer(firestore, OTHER_UID, other_token, 3)
    child_record = _writer_record(firestore, UID, token)
    owner = content_writer_owner.ProcessOwner.from_storage(child_record["owner"])
    assert owner.pid == process.pid
    assert owner.generation != content_writer_owner.current_process_owner().generation
    order = []

    async def purge_memory(_uid):
        return 0

    app = _deletion_app(
        monkeypatch,
        authority_enabled=True,
        delete_firestore=lambda _uid: order.append("purge"),
        delete_firebase=lambda _uid: order.append("firebase"),
        purge_memory=purge_memory,
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first_process = await client.delete("/v1/users/delete-account")
            assert first_process.status_code == 202
            state = content_write_fence._snapshot_data(content_write_fence._fence_reference(firestore, UID).get())
            assert state["state"] == content_write_fence.DRAINING
            assert token in state["writers"]

            restarted_process = await client.delete("/v1/users/delete-account")
            assert restarted_process.status_code == 202
            assert order == []

            assert _invoke_recovery(firestore, uid=UID, token=token) == 2
            refused = json.loads(capsys.readouterr().out)
            assert refused == {
                "action": "content_writer_recovery",
                "content_free": True,
                "reason": "account_writer_recovery_owner_live",
                "result": "refused",
            }
            still_pending = await client.delete("/v1/users/delete-account")
            assert still_pending.status_code == 202
            assert order == []

            process.terminate()
            process.join(5)
            assert not process.is_alive()
            assert _invoke_recovery(firestore, uid=UID, token=token) == 0
            recovered = json.loads(capsys.readouterr().out)
            assert recovered["result"] == "recovered"
            assert recovered["proof_kind"] == "kernel_process_absent"
            assert recovered["content_free"] is True
            assert UID not in json.dumps(recovered)
            assert token not in json.dumps(recovered)
            recovered_state = content_write_fence._snapshot_data(
                content_write_fence._fence_reference(firestore, UID).get()
            )
            assert token not in recovered_state["writers"]
            assert same_uid_token in recovered_state["writers"]

            content_write_fence._release_firestore_writer(firestore, UID, same_uid_token)
            converged = await client.delete("/v1/users/delete-account")
            assert converged.status_code == 200
            assert _invoke_recovery(firestore, uid=UID, token=token) == 2
            assert json.loads(capsys.readouterr().out)["reason"] == "account_writer_recovery_stale_token"

    try:
        asyncio.run(scenario())
        assert order == ["purge", "firebase"]
        retained = content_write_fence._snapshot_data(content_write_fence._fence_reference(firestore, OTHER_UID).get())
        assert other_token in retained["writers"]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        manager.shutdown()


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("host", "account_writer_recovery_host_mismatch"),
        ("boot", "account_writer_recovery_boot_mismatch"),
        ("namespace", "account_writer_recovery_pid_namespace_mismatch"),
        ("start", "account_writer_recovery_pid_reused"),
    ],
)
def test_recovery_surface_rejects_other_boundary_and_pid_reuse(
    monkeypatch,
    deletion_fence,
    capsys,
    mutation,
    expected_reason,
):
    token = f"boundary-{mutation}-token"
    content_write_fence._acquire_firestore_writer(deletion_fence, UID, token, 3)
    reference = content_write_fence._fence_reference(deletion_fence, UID)
    state = content_write_fence._snapshot_data(reference.get())
    owner = content_writer_owner.ProcessOwner.from_storage(state["writers"][token]["owner"])
    replacement = {
        "host": replace(owner, host_id="f" * 64),
        "boot": replace(owner, boot_id=f"other-{owner.boot_id}"),
        "namespace": replace(owner, pid_namespace=f"other-{owner.pid_namespace}"),
        "start": replace(owner, start_id=f"other-{owner.start_id}"),
    }[mutation]
    state["writers"][token]["owner"] = replacement.to_storage()
    deletion_fence.documents[reference.key] = state

    assert _invoke_recovery(deletion_fence, uid=UID, token=token) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == expected_reason
    assert token in content_write_fence._snapshot_data(reference.get())["writers"]


def test_recovery_surface_rejects_stale_cross_uid_ownerless_and_actual_unprivileged_invocation(
    deletion_fence,
    capsys,
    tmp_path,
):
    token = "selector-control-token"
    content_write_fence._acquire_firestore_writer(deletion_fence, UID, token, 3)
    assert _invoke_recovery(deletion_fence, uid=UID, token="stale-token") == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "account_writer_recovery_stale_token"
    assert _invoke_recovery(deletion_fence, uid=OTHER_UID, token=token) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "account_writer_recovery_stale_token"

    reference = content_write_fence._fence_reference(deletion_fence, UID)
    state = content_write_fence._snapshot_data(reference.get())
    state["writers"][token] = state["writers"][token]["expires_at"]
    deletion_fence.documents[reference.key] = state
    assert _invoke_recovery(deletion_fence, uid=UID, token=token) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "account_writer_recovery_owner_unknown"

    hostile_path = tmp_path / "hostile"
    hostile_database = hostile_path / "database"
    hostile_database.mkdir(parents=True)
    marker = tmp_path / "hostile-imported"
    (hostile_database / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )
    hostile_google = hostile_path / "google"
    hostile_google.mkdir()
    (hostile_google / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )
    credential_target = tmp_path / "credential-target"
    (tmp_path / "google-credentials.json").symlink_to(credential_target)
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(hostile_path),
            "SERVICE_ACCOUNT_JSON": '{"private_key":"fake-reviewer-control"}',
            "FIRESTORE_EMULATOR_HOST": "127.0.0.1:65530",
            "GCLOUD_PROJECT": "wrong-project",
            "GOOGLE_CLOUD_PROJECT": "wrong-project",
        }
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "content_writer_recovery.py"
    for interpreter_flags in ([], ["-I"]):
        completed = subprocess.run(
            [
                sys.executable,
                *interpreter_flags,
                str(script),
                "--subject-hash",
                content_write_recovery.hash_selector(UID),
                "--token-hash",
                content_write_recovery.hash_selector(token),
            ],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == (77 if sys.platform == "linux" else 78)
        assert completed.stdout == ""
    assert not marker.exists()
    assert not credential_target.exists()


def test_recovery_surface_rejects_cross_generation_replacement_after_real_terminal_proof(
    monkeypatch,
    deletion_fence,
    capsys,
):
    del deletion_fence
    context = multiprocessing.get_context("fork")
    manager, firestore = _shared_firestore(context)
    token = "cross-generation-cas-token"
    process, release = _start_real_writer(context, firestore, uid=UID, token=token)
    del release
    process.terminate()
    process.join(5)
    assert not process.is_alive()
    reference = content_write_fence._fence_reference(firestore, UID)
    replaced_generation = uuid.uuid4().hex

    def replace_before_compare_and_set(function):
        def run(transaction, *args, **kwargs):
            with transaction.database.lock:
                state = content_write_fence._snapshot_data(reference.get())
                state["writers"][token]["owner"]["generation"] = replaced_generation
                transaction.database.documents[reference.key] = state
                return function(transaction, *args, **kwargs)

        return run

    try:
        assert (
            _invoke_recovery(
                firestore,
                uid=UID,
                token=token,
                transactional=replace_before_compare_and_set,
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["reason"] == "account_writer_recovery_record_replaced"
        assert _writer_record(firestore, UID, token)["owner"]["generation"] == replaced_generation
    finally:
        manager.shutdown()


def test_concurrent_recovery_surface_is_exact_and_idempotent(deletion_fence):
    del deletion_fence
    context = multiprocessing.get_context("fork")
    manager, firestore = _shared_firestore(context)
    token = "concurrent-recovery-token"
    process, release = _start_real_writer(context, firestore, uid=UID, token=token)
    del release
    process.terminate()
    process.join(5)
    assert not process.is_alive()

    def recover():
        return content_write_recovery.recover_orphaned_writer(
            firestore,
            subject_hash=content_write_recovery.hash_selector(UID),
            token_hash=content_write_recovery.hash_selector(token),
            transactional=_transactional,
        ).to_dict()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda _index: recover(), range(2)))
        assert {receipt["result"] for receipt in receipts} == {"recovered", "already_recovered"}
        state = content_write_fence._snapshot_data(content_write_fence._fence_reference(firestore, UID).get())
        assert token not in state["writers"]
        assert state["writer_recovery"]["token_hash"] == content_write_recovery.hash_selector(token)
    finally:
        manager.shutdown()


def test_concurrent_different_token_compaction_keeps_only_latest_retry_authority(monkeypatch, deletion_fence):
    tokens = ("concurrent-compaction-a", "concurrent-compaction-b")
    other_token = "concurrent-compaction-other-uid"
    for token in tokens:
        content_write_fence._acquire_firestore_writer(deletion_fence, UID, token, 3)
    content_write_fence._acquire_firestore_writer(deletion_fence, OTHER_UID, other_token, 3)
    monkeypatch.setattr(
        content_write_recovery,
        "prove_recorded_owner_terminal",
        lambda owner: content_write_recovery.TerminalProcessProof(owner=owner, proof_kind="test-only-terminal"),
    )

    def recover(token):
        return content_write_recovery.recover_orphaned_writer(
            deletion_fence,
            subject_hash=content_write_recovery.hash_selector(UID),
            token_hash=content_write_recovery.hash_selector(token),
            transactional=_transactional,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(recover, tokens))
    assert [receipt.result for receipt in receipts] == ["recovered", "recovered"]
    state = content_write_fence._snapshot_data(content_write_fence._fence_reference(deletion_fence, UID).get())
    assert state["writers"] == {}
    latest_hash = state["writer_recovery"]["token_hash"]
    latest_token = next(token for token in tokens if content_write_recovery.hash_selector(token) == latest_hash)
    evicted_token = next(token for token in tokens if token != latest_token)
    assert recover(latest_token).result == "already_recovered"
    with pytest.raises(content_write_recovery.ContentWriterRecoveryError) as stale:
        recover(evicted_token)
    assert stale.value.code == "account_writer_recovery_stale_token"
    other_state = content_write_fence._snapshot_data(
        content_write_fence._fence_reference(deletion_fence, OTHER_UID).get()
    )
    assert set(other_state["writers"]) == {other_token}


def test_recovery_receipt_is_strictly_bounded_across_five_thousand_cycles_and_deletion_converges(
    monkeypatch,
    deletion_fence,
):
    retained_token = "same-uid-retained-through-compaction"
    other_token = "other-uid-retained-through-compaction"
    content_write_fence._acquire_firestore_writer(deletion_fence, UID, retained_token, 3)
    content_write_fence._acquire_firestore_writer(deletion_fence, OTHER_UID, other_token, 3)
    owner = content_writer_owner.current_process_owner()
    legacy_uid = "legacy-recovery-compaction-user"
    legacy_token = "legacy-recovery-compaction-live-token"
    legacy_reference = content_write_fence._fence_reference(deletion_fence, legacy_uid)
    recovered_at = datetime.now(timezone.utc)
    deletion_fence.documents[legacy_reference.key] = {
        "state": content_write_fence.ACTIVE,
        "writers": {},
        "writer_recoveries": {
            content_write_recovery.hash_selector(f"legacy-recovery-{index}"): {
                "owner": owner.to_storage(),
                "recovered_at": recovered_at,
            }
            for index in range(3001)
        },
    }
    content_write_fence._acquire_firestore_writer(deletion_fence, legacy_uid, legacy_token, 3)
    compacted_legacy = content_write_fence._snapshot_data(legacy_reference.get())
    assert "writer_recoveries" not in compacted_legacy
    assert set(compacted_legacy["writers"]) == {legacy_token}
    assert len(json.dumps(compacted_legacy, default=str, separators=(",", ":")).encode("utf-8")) < 2048
    content_write_fence._release_firestore_writer(deletion_fence, legacy_uid, legacy_token)
    assert not legacy_reference.get().exists
    monkeypatch.setattr(
        content_write_recovery,
        "prove_recorded_owner_terminal",
        lambda recorded_owner: content_write_recovery.TerminalProcessProof(
            owner=recorded_owner,
            proof_kind="test-only-terminal",
        ),
    )
    first_token = "bounded-recovery-0000"
    last_token = None
    for index in range(5001):
        token = f"bounded-recovery-{index:04d}"
        last_token = token
        content_write_fence._acquire_firestore_writer(deletion_fence, UID, token, 3)
        receipt = content_write_recovery.recover_orphaned_writer(
            deletion_fence,
            subject_hash=content_write_recovery.hash_selector(UID),
            token_hash=content_write_recovery.hash_selector(token),
            transactional=_transactional,
        )
        assert receipt.owner_fingerprint == owner.fingerprint()

    reference = content_write_fence._fence_reference(deletion_fence, UID)
    state = content_write_fence._snapshot_data(reference.get())
    assert set(state["writers"]) == {retained_token}
    assert state["writer_recovery"]["token_hash"] == content_write_recovery.hash_selector(last_token)
    serialized = json.dumps(state, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(serialized) < 2048
    assert _invoke_recovery(deletion_fence, uid=UID, token=first_token) == 2
    assert retained_token in content_write_fence._snapshot_data(reference.get())["writers"]

    assert content_write_fence._advance_firestore_tombstone(deletion_fence, UID) is False
    content_write_fence._release_firestore_writer(deletion_fence, UID, retained_token)
    assert content_write_fence._advance_firestore_tombstone(deletion_fence, UID) is True
    tombstone = content_write_fence._snapshot_data(reference.get())
    assert tombstone["state"] == content_write_fence.TOMBSTONED
    assert "writer_recovery" not in tombstone
    other_state = content_write_fence._snapshot_data(
        content_write_fence._fence_reference(deletion_fence, OTHER_UID).get()
    )
    assert set(other_state["writers"]) == {other_token}


def test_stale_receipt_or_hash_collision_never_releases_a_different_writer(monkeypatch, deletion_fence):
    old_token = "old-recovered-token"
    current_token = "different-current-token"
    subject_hash = content_write_recovery.hash_selector(UID)
    content_write_fence._acquire_firestore_writer(deletion_fence, UID, old_token, 3)
    monkeypatch.setattr(
        content_write_recovery,
        "prove_recorded_owner_terminal",
        lambda owner: content_write_recovery.TerminalProcessProof(owner=owner, proof_kind="test-only-terminal"),
    )
    content_write_recovery.recover_orphaned_writer(
        deletion_fence,
        subject_hash=subject_hash,
        token_hash=content_write_recovery.hash_selector(old_token),
        transactional=_transactional,
    )
    reference = content_write_fence._fence_reference(deletion_fence, UID)
    state = content_write_fence._snapshot_data(reference.get())
    owner = content_writer_owner.current_process_owner()
    state["writers"][current_token] = content_write_fence.WriterRegistration(
        expires_at=state["writer_recovery"]["recovered_at"] + timedelta(seconds=3),
        owner=owner,
    ).to_storage()
    deletion_fence.documents[reference.key] = state
    monkeypatch.setattr(content_write_recovery, "hash_selector", lambda _value: "a" * 64)
    state = content_write_fence._snapshot_data(reference.get())
    state["writer_recovery"]["token_hash"] = "a" * 64
    deletion_fence.documents[reference.key] = state

    with pytest.raises(content_write_recovery.ContentWriterRecoveryError) as raised:
        content_write_recovery.recover_orphaned_writer(
            deletion_fence,
            subject_hash=subject_hash,
            token_hash="a" * 64,
            transactional=_transactional,
        )
    assert raised.value.code == "account_writer_recovery_selector_ambiguous"
    assert current_token in content_write_fence._snapshot_data(reference.get())["writers"]


def test_pinned_recovery_authority_ignores_hostile_ambient_project_and_credentials(monkeypatch):
    pinned_project = "omi-production"
    pinned_database = "(default)"
    credential_path = Path("/protected/recovery-credential.json")
    receipt_path = Path("/protected/recovery-receipt.json")
    credential_payload = json.dumps(
        {"type": "service_account", "project_id": pinned_project, "private_key": "test-only"}
    ).encode("utf-8")
    calls = {"credentials": 0, "clients": 0}

    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setenv("GCLOUD_PROJECT", "hostile-project")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "hostile-project")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/hostile.json")
    monkeypatch.setenv("FIRESTORE_DATABASE", "hostile-database")
    monkeypatch.setenv("SERVICE_ACCOUNT_JSON", '{"project_id":"hostile-project"}')
    monkeypatch.setattr(
        content_write_recovery_authority,
        "_validated_config",
        lambda _path: (pinned_project, pinned_database, credential_path, receipt_path),
    )
    monkeypatch.setattr(
        content_write_recovery_authority,
        "_read_protected_file",
        lambda path, **_kwargs: credential_payload if path == credential_path else b"",
    )
    monkeypatch.setattr(content_write_recovery_authority, "_validated_receipt", lambda *_args, **_kwargs: None)

    def credentials_factory(info):
        calls["credentials"] += 1
        assert info["project_id"] == pinned_project
        return object()

    class Client:
        def __init__(self, *, project, database, credentials):
            calls["clients"] += 1
            assert credentials is not None
            self.project = project
            self.database = database

    client, authority = content_write_recovery_authority._load_recovery_firestore_client(
        Path("/fixed/config.json"),
        credentials_factory=credentials_factory,
        client_factory=Client,
    )
    assert (client.project, client.database) == (pinned_project, pinned_database)
    assert (authority.project_id, authority.database_id) == (pinned_project, pinned_database)
    assert calls == {"credentials": 1, "clients": 1}


def test_recovery_authority_rejects_emulator_mismatch_and_symlink_before_credentials(
    monkeypatch,
    tmp_path,
):
    calls = {"config": 0, "credentials": 0, "clients": 0}
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:65530")

    def config(_path):
        calls["config"] += 1
        raise AssertionError("emulator refusal must precede config")

    monkeypatch.setattr(content_write_recovery_authority, "_validated_config", config)
    with pytest.raises(content_write_recovery_authority.RecoveryAuthorityError) as emulator:
        content_write_recovery_authority._load_recovery_firestore_client(
            Path("/fixed/config.json"),
            credentials_factory=lambda _info: calls.__setitem__("credentials", calls["credentials"] + 1),
            client_factory=lambda **_kwargs: calls.__setitem__("clients", calls["clients"] + 1),
        )
    assert emulator.value.code == "account_writer_recovery_emulator_forbidden"
    assert calls == {"config": 0, "credentials": 0, "clients": 0}

    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST")
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "config.json"
    symlink.symlink_to(target)
    monkeypatch.setattr(content_write_recovery_authority, "_protected_parent_chain", lambda _path: None)
    with pytest.raises(content_write_recovery_authority.RecoveryAuthorityError):
        content_write_recovery_authority._read_protected_file(
            symlink,
            maximum=1024,
            code="account_writer_recovery_config_unavailable",
        )
    assert calls == {"config": 0, "credentials": 0, "clients": 0}


def test_recovery_authority_wrong_receipt_or_credential_project_loads_no_credentials(monkeypatch):
    project = "omi-production"
    credential_path = Path("/protected/recovery-credential.json")
    receipt_path = Path("/protected/recovery-receipt.json")
    calls = {"credentials": 0, "clients": 0}
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setattr(
        content_write_recovery_authority,
        "_validated_config",
        lambda _path: (project, "(default)", credential_path, receipt_path),
    )
    monkeypatch.setattr(
        content_write_recovery_authority,
        "_read_protected_file",
        lambda _path, **_kwargs: json.dumps(
            {"type": "service_account", "project_id": "wrong-project", "private_key": "test-only"}
        ).encode("utf-8"),
    )

    def wrong_receipt(*_args, **_kwargs):
        raise content_write_recovery_authority.RecoveryAuthorityError(
            "account_writer_recovery_deployment_receipt_mismatch"
        )

    def credentials_factory(_info):
        calls["credentials"] += 1

    def client_factory(**_kwargs):
        calls["clients"] += 1

    monkeypatch.setattr(content_write_recovery_authority, "_validated_receipt", wrong_receipt)
    with pytest.raises(content_write_recovery_authority.RecoveryAuthorityError) as receipt_mismatch:
        content_write_recovery_authority._load_recovery_firestore_client(
            Path("/fixed/config.json"),
            credentials_factory=credentials_factory,
            client_factory=client_factory,
        )
    assert receipt_mismatch.value.code == "account_writer_recovery_deployment_receipt_mismatch"
    assert calls == {"credentials": 0, "clients": 0}

    monkeypatch.setattr(content_write_recovery_authority, "_validated_receipt", lambda *_args, **_kwargs: None)
    with pytest.raises(content_write_recovery_authority.RecoveryAuthorityError) as mismatch:
        content_write_recovery_authority._load_recovery_firestore_client(
            Path("/fixed/config.json"),
            credentials_factory=credentials_factory,
            client_factory=client_factory,
        )
    assert mismatch.value.code == "account_writer_recovery_credentials_mismatch"
    assert calls == {"credentials": 0, "clients": 0}


def test_production_recovery_modules_refuse_darwin_before_firestore_calls(monkeypatch):
    calls = {"firestore": 0}

    class Firestore:
        def collection(self, _name):
            calls["firestore"] += 1
            raise AssertionError("non-Linux refusal must precede Firestore")

    assert content_writer_owner.SUPPORTED_RECOVERY_SYSTEMS == {"Linux"}
    monkeypatch.setattr(content_write_recovery.platform, "system", lambda: "Darwin")
    with pytest.raises(content_write_recovery.ContentWriterRecoveryError) as recovery:
        content_write_recovery.recover_orphaned_writer(
            Firestore(),
            subject_hash="a" * 64,
            token_hash="b" * 64,
            transactional=_transactional,
        )
    assert recovery.value.code == "account_writer_recovery_system_unsupported"
    assert calls == {"firestore": 0}

    monkeypatch.setattr(content_writer_owner.platform, "system", lambda: "Darwin")
    with pytest.raises(content_writer_owner.ProcessOwnerError) as boundary:
        PRODUCTION_CURRENT_PROCESS_BOUNDARY()
    assert boundary.value.code == "account_writer_os_boundary_unsupported"
    with pytest.raises(content_writer_owner.ProcessOwnerError) as snapshot:
        PRODUCTION_PROCESS_SNAPSHOT("Darwin", 1)
    assert snapshot.value.code == "account_writer_os_boundary_unsupported"


@pytest.mark.parametrize("terminal", ["success", "error", "timeout"])
def test_stop_worker_joins_actual_enrichment_thread_before_deletion(monkeypatch, deletion_fence, terminal):
    del deletion_fence
    repository = _EnrichmentOutbox()
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    order = []

    def deliver(_job):
        order.append("delivery-entered")
        delivery_entered.set()
        assert release_delivery.wait(5)
        order.append(f"delivery-{terminal}")
        if terminal == "error":
            raise RuntimeError("synthetic-delivery-error")
        if terminal == "timeout":
            raise requests.Timeout("synthetic-delivery-timeout")
        return DeliveryResult(True, receipt={"content_free": True})

    worker = HermesCloudEnrichmentOutboxWorker(repository, deliver=deliver, poll_seconds=0.01)

    async def purge_memory(_uid):
        return 0

    app = _deletion_app(
        monkeypatch,
        authority_enabled=True,
        delete_firestore=lambda uid: (order.append("enrichment-purge"), repository.purge(uid)),
        delete_firebase=lambda _uid: order.append("firebase-delete"),
        purge_memory=purge_memory,
    )
    monkeypatch.setenv("ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS", UID)
    monkeypatch.setattr(enrichment_service, "_worker_task", None)
    monkeypatch.setattr(enrichment_service, "_worker_shutdown_task", None)

    async def start():
        await enrichment_service.start_worker(worker)

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", enrichment_service.stop_worker)

    async def scenario():
        await app.router.startup()
        first_task = enrichment_service._worker_task
        assert first_task is not None
        await enrichment_service.start_worker(worker)
        assert enrichment_service._worker_task is first_task
        assert await asyncio.to_thread(delivery_entered.wait, 3)

        shared_shutdown = await _cancel_two_coalesced_stops(enrichment_service)
        assert enrichment_service._worker_task is first_task
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = await client.delete("/v1/users/delete-account")
            assert pending.status_code == 202
            assert "firebase-delete" not in order
            assert repository.jobs["target"]["status"] == "running"
            assert enrichment_service._worker_shutdown_task is shared_shutdown

            release_delivery.set()
            await asyncio.wait_for(enrichment_service.stop_worker(), 3)
            assert shared_shutdown.done()
            assert enrichment_service._worker_task is None
            assert enrichment_service._worker_shutdown_task is None
            await enrichment_service.stop_worker()
            completed = await client.delete("/v1/users/delete-account")
            assert completed.status_code == 200

    asyncio.run(scenario())
    assert "target" not in repository.jobs
    assert repository.jobs["retained"]["uid"] == OTHER_UID
    assert order.index(f"delivery-{terminal}") < order.index("enrichment-purge")
    assert order.index("enrichment-purge") < order.index("firebase-delete")


@pytest.mark.parametrize("mutation_kind", ["proposal", "correction"])
def test_stop_worker_joins_actual_memory_thread_before_deletion(monkeypatch, deletion_fence, mutation_kind):
    del deletion_fence
    repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
    target = asyncio.run(_seed_reinterpretation(repository, UID))
    retained = asyncio.run(_seed_reinterpretation(repository, OTHER_UID, completed=True))
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    order = []

    class HeldPlanHermes:
        async def propose(self, **_kwargs):
            if mutation_kind == "correction":
                return ReinterpretationPlan(
                    outcome="proposals",
                    proposals=[
                        {
                            "kind": "factual_correction",
                            "certainty": "confirmed",
                            "correction_text": "The remembered detail is corrected.",
                            "evidence_event_ids": [f"event-{UID}"],
                            "evidence_quote": "The remembered detail is corrected.",
                            "corrected_summary": {
                                "overview": "[Ella] The remembered detail is corrected.",
                            },
                        }
                    ],
                )
            return ReinterpretationPlan(
                outcome="proposals",
                proposals=[
                    {
                        "kind": "ambiguous_reinterpretation",
                        "certainty": "ambiguous",
                        "correction_text": "The remembered location may have changed.",
                        "evidence_event_ids": [f"event-{UID}"],
                    }
                ],
            )

    async def conversation_loader(uid, conversation_id):
        return {
            "id": conversation_id,
            "active_summary_version_id": f"version-{uid}",
            "structured": {"overview": "[Ella] A remembered detail."},
        }

    def create_proposal(**_kwargs):
        order.append("proposal-thread-entered")
        mutation_entered.set()
        assert release_mutation.wait(5)
        order.append("proposal-thread-terminal")
        return {"proposal": {"proposal_id": "proposal-after-cancel"}}

    def apply_correction():
        order.append("correction-thread-entered")
        mutation_entered.set()
        assert release_mutation.wait(5)
        order.append("correction-thread-terminal")
        return ApplyResult(
            correction_id="correction-after-cancel",
            active_summary_version_id=f"corrected-version-{UID}",
        )

    async def correction_writer(**_kwargs):
        return await content_write_fence.run_admitted_threaded_mutation(UID, apply_correction)

    monkeypatch.setattr(proposal_ingest, "create_proposal", create_proposal)
    worker_kwargs = {"correction_writer": correction_writer} if mutation_kind == "correction" else {}
    worker = MemoryReinterpretationWorker(
        repository,
        hermes_client=HeldPlanHermes(),
        conversation_loader=conversation_loader,
        lease_seconds=30,
        **worker_kwargs,
    )

    async def purge_memory(uid):
        order.append("memory-purge")
        removed = [job_id for job_id, job in repository.jobs.items() if job["uid"] == uid]
        for job_id in removed:
            del repository.jobs[job_id]
        repository.attempts = [attempt for attempt in repository.attempts if attempt["job_id"] not in removed]
        return len(removed)

    app = _deletion_app(
        monkeypatch,
        authority_enabled=True,
        delete_firestore=lambda _uid: order.append("firestore-purge"),
        delete_firebase=lambda _uid: order.append("firebase-delete"),
        purge_memory=purge_memory,
    )
    monkeypatch.setenv("ELLA_MEMORY_REINTERPRETATION_WORKER_ENABLED", "true")
    monkeypatch.setenv("ELLA_MEMORY_REINTERPRETATION_IDLE_SECONDS", "0.01")
    monkeypatch.setattr(reinterpretation_service, "_worker_task", None)
    monkeypatch.setattr(reinterpretation_service, "_worker_shutdown_task", None)

    async def start():
        await reinterpretation_service.start_worker(worker)

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", reinterpretation_service.stop_worker)

    async def scenario():
        await app.router.startup()
        first_task = reinterpretation_service._worker_task
        assert first_task is not None
        await reinterpretation_service.start_worker(worker)
        assert reinterpretation_service._worker_task is first_task
        assert await asyncio.to_thread(mutation_entered.wait, 3)

        shared_shutdown = await _cancel_two_coalesced_stops(reinterpretation_service)
        assert reinterpretation_service._worker_task is first_task
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = await client.delete("/v1/users/delete-account")
            assert pending.status_code == 202
            assert "firebase-delete" not in order
            assert repository.jobs[target["id"]]["status"] == "running"
            assert reinterpretation_service._worker_shutdown_task is shared_shutdown

            release_mutation.set()
            await asyncio.wait_for(reinterpretation_service.stop_worker(), 3)
            assert shared_shutdown.done()
            assert reinterpretation_service._worker_task is None
            assert reinterpretation_service._worker_shutdown_task is None
            await reinterpretation_service.stop_worker()
            completed = await client.delete("/v1/users/delete-account")
            assert completed.status_code == 200

    asyncio.run(scenario())
    assert target["id"] not in repository.jobs
    assert repository.jobs[retained["id"]]["uid"] == OTHER_UID
    assert order.index(f"{mutation_kind}-thread-terminal") < order.index("memory-purge")
    assert order.index("memory-purge") < order.index("firebase-delete")
