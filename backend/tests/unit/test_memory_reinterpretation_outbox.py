import asyncio
import os
import sys
import types
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("ENCRYPTION_SECRET", "test-encryption-secret-32-bytes-long")

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

from database.memory_reinterpretations import InMemoryMemoryReinterpretationRepository
from ella.routers import corrections, memory_reinterpretation as reinterpretation_router
from ella.routers.canonical_events import (
    CanonicalEventIn,
    InMemoryCanonicalEventStore,
    PostgresCanonicalEventStore,
    SessionCompleteIn,
    create_canonical_events_router,
)
from ella.routers import canonical_events
from ella.services.memory_reinterpretation import (
    ApplyResult,
    MemoryReinterpretationWorker,
    ReinterpretationPlan,
    ReinterpretationWorkerError,
)

UID = "CaseSensitiveUserA"
SESSION_ID = "signed-jti-1"
CONVERSATION_ID = "memory-1"
VERSION_ID = "summary-v1"


def _event(
    event_id: str,
    text: str,
    *,
    uid: str = UID,
    session_id: str = SESSION_ID,
    conversation_id: str = CONVERSATION_ID,
    version_id: str = VERSION_ID,
    connection_id: str = "connection-a",
    turn_index: int = 0,
    role: str = "user",
    started_at: str = "2026-07-24T18:00:00Z",
) -> CanonicalEventIn:
    scope = {
        "scope_kind": "memory",
        "conversation_id": conversation_id,
        "active_summary_version_id": version_id,
        "can_reinterpret": True,
    }
    return CanonicalEventIn(
        uid=uid,
        canonical_identity=uid,
        event_id=event_id,
        session_id=session_id,
        channel="ios_voice",
        provider="grok-realtime",
        role=role,
        text=text,
        started_at=started_at,
        ended_at=started_at,
        privacy_scope="user_private",
        scan_policy="none",
        source_ref={
            "source_identity": f"test:{uid}:{session_id}:{connection_id}:{turn_index}:{role}",
            "connection_id": connection_id,
            "turn_index": turn_index,
            **scope,
        },
        metadata={"connection_id": connection_id, "turn_index": turn_index, **scope},
    )


def _completion(
    *,
    uid: str = UID,
    session_id: str = SESSION_ID,
    conversation_id: str = CONVERSATION_ID,
    version_id: str = VERSION_ID,
    can_reinterpret: bool = True,
    scope_kind: str = "memory",
) -> tuple[str, SessionCompleteIn]:
    scope = {
        "scope_kind": scope_kind,
        "conversation_id": conversation_id,
        "active_summary_version_id": version_id,
        "can_reinterpret": can_reinterpret,
    }
    return (
        session_id,
        SessionCompleteIn(
            uid=uid,
            canonical_identity=uid,
            channel="ios_voice",
            provider="grok-realtime",
            started_at="2026-07-24T18:00:00Z",
            ended_at="2026-07-24T18:01:00Z",
            source_ref={
                "source_identity": f"grok-realtime:ios_voice:session:{session_id}",
                **scope,
            },
            metadata=scope,
        ),
    )


def _conversation(version_id: str = VERSION_ID):
    return {
        "id": CONVERSATION_ID,
        "active_summary_version_id": version_id,
        "structured": {
            "title": "Glasses location",
            "overview": "[Ella] The glasses were near the desk.",
            "emoji": "👓",
            "category": "other",
        },
    }


async def _seed_job(
    repository: InMemoryMemoryReinterpretationRepository,
    *,
    events: list[CanonicalEventIn] | None = None,
) -> tuple[InMemoryCanonicalEventStore, dict]:
    store = InMemoryCanonicalEventStore(repository)
    await store.write_batch(events or [_event("turn-1", "The glasses are actually in the blue backpack.")])
    session_id, completion = _completion()
    result = await store.complete_session(session_id, completion)
    job_id = result["reinterpretation"]["job_id"]
    return store, repository.jobs[job_id]


def test_duplicate_completion_is_one_job_and_reconnect_extends_debounce_and_hash():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=45)
        store, original = await _seed_job(repository)
        original_job_id = original["id"]
        original_due = original["not_before"]
        original_hash = original["transcript_hash"]

        repository.now += timedelta(seconds=10)
        await store.write_batch(
            [
                _event(
                    "turn-2",
                    "Yes, the blue backpack by the door.",
                    connection_id="connection-b",
                    turn_index=0,
                    started_at="2026-07-24T18:01:20Z",
                )
            ]
        )
        session_id, completion = _completion()
        replay = await store.complete_session(session_id, completion)
        job = repository.jobs[original_job_id]

        assert replay["duplicate"] is True
        assert len(repository.jobs) == 1
        assert replay["reinterpretation"]["job_id"] == original_job_id
        assert job["not_before"] == repository.now + timedelta(seconds=45)
        assert job["not_before"] > original_due
        assert job["transcript_hash"] != original_hash
        assert [ref["event_id"] for ref in job["canonical_refs"]] == ["turn-1", "turn-2"]

    asyncio.run(run())


def test_postgres_completion_and_enqueue_share_one_transaction(monkeypatch):
    class Transaction:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            self.connection.in_transaction = True

        async def __aexit__(self, exc_type, exc, traceback):
            self.connection.in_transaction = False

    class Connection:
        def __init__(self):
            self.in_transaction = False

        def transaction(self):
            return Transaction(self)

        async def fetchrow(self, query, *args):
            assert self.in_transaction is True
            assert "ON CONFLICT (session_id, source_identity)" in query
            assert "DO UPDATE SET" in query
            return {"id": 1, "inserted": True}

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    class Repository:
        def __init__(self):
            self.called = False

        async def enqueue_from_completion(self, connection, completion):
            assert connection.in_transaction is True
            self.called = True
            return {
                "id": "job-1",
                "status": "pending",
                "not_before": completion["completed_at"] + timedelta(seconds=45),
            }

    connection = Connection()
    repository = Repository()

    async def get_pool():
        return Pool(connection)

    monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
    session_id, completion = _completion()
    result = asyncio.run(PostgresCanonicalEventStore(repository).complete_session(session_id, completion))

    assert repository.called is True
    assert connection.in_transaction is False
    assert result["reinterpretation"]["job_id"] == "job-1"


def test_reinterpretation_completion_requires_configured_ledger_bearer(monkeypatch):
    repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=45)
    store = InMemoryCanonicalEventStore(repository)
    asyncio.run(store.write_batch([_event("turn-auth", "The glasses are in the blue backpack.")]))
    app = FastAPI()
    app.include_router(create_canonical_events_router(store))
    client = TestClient(app)
    session_id, completion = _completion()
    body = completion.model_dump(mode="json")

    monkeypatch.setenv("ELLA_MEMORY_REINTERPRETATION_ENABLED", "true")
    monkeypatch.setenv("ELLA_EVENT_LEDGER_TOKEN", "ledger-secret")

    missing = client.post(f"/v1/ella/sessions/{session_id}/complete", json=body)
    wrong = client.post(
        f"/v1/ella/sessions/{session_id}/complete",
        headers={"Authorization": "Bearer wrong"},
        json=body,
    )
    accepted = client.post(
        f"/v1/ella/sessions/{session_id}/complete",
        headers={"Authorization": "Bearer ledger-secret"},
        json=body,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["reinterpretation"]["job_id"]


def test_read_only_or_noise_completion_never_enqueues():
    async def run():
        for completion_values in (
            {"can_reinterpret": False},
            {"scope_kind": "general"},
        ):
            repository = InMemoryMemoryReinterpretationRepository()
            store = InMemoryCanonicalEventStore(repository)
            await store.write_batch([_event("turn-1", "Just reminiscing.")])
            session_id, completion = _completion(**completion_values)
            result = await store.complete_session(session_id, completion)
            assert result["reinterpretation"] is None
            assert repository.jobs == {}

    asyncio.run(run())


class _Hermes:
    def __init__(self, plan):
        self.plan = ReinterpretationPlan(**plan)
        self.calls = 0

    async def propose(self, **kwargs):
        self.calls += 1
        return self.plan


async def _loader(uid, conversation_id):
    if uid == UID and conversation_id == CONVERSATION_ID:
        return _conversation()
    return None


def test_worker_no_change_finishes_without_proposals_or_writes():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        hermes = _Hermes({"outcome": "no_change", "proposals": []})

        async def should_not_write(**kwargs):
            raise AssertionError("no_change must not write")

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=hermes,
            conversation_loader=_loader,
            pending_proposal_writer=should_not_write,
            correction_writer=should_not_write,
        )
        result = await worker.run_once("worker-a")
        job = next(iter(repository.jobs.values()))

        assert result["status"] == "no_change"
        assert job["status"] == "no_change"
        assert job["proposal_ids"] == []
        assert job["correction_ids"] == []
        assert hermes.calls == 1

    asyncio.run(run())


def test_worker_preserves_zero_to_many_proposal_order_and_only_auto_applies_explicit_fact():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        plan = {
            "outcome": "proposals",
            "proposals": [
                {
                    "kind": "ambiguous_reinterpretation",
                    "certainty": "ambiguous",
                    "correction_text": "The user may have meant the red bag.",
                    "evidence_event_ids": ["turn-1"],
                    "evidence_quote": "blue backpack",
                },
                {
                    "kind": "factual_correction",
                    "certainty": "confirmed",
                    "correction_text": "The glasses are in the blue backpack.",
                    "evidence_event_ids": ["turn-1"],
                    "evidence_quote": "glasses are actually in the blue backpack",
                    "corrected_summary": {
                        "title": "Glasses in blue backpack",
                        "overview": "[Ella] The glasses are in the blue backpack.",
                        "emoji": "👓",
                        "category": "other",
                    },
                },
                {
                    "kind": "ambiguous_reinterpretation",
                    "certainty": "ambiguous",
                    "correction_text": "The bag may be beside the door.",
                    "evidence_event_ids": ["turn-1"],
                    "evidence_quote": "blue backpack",
                },
            ],
        }
        pending_order = []
        apply_order = []

        async def pending_writer(**kwargs):
            pending_order.append(kwargs["proposal_index"])
            return f"pending-{kwargs['proposal_index']}"

        async def correction_writer(**kwargs):
            apply_order.append(kwargs["proposal_index"])
            return ApplyResult(
                correction_id=kwargs["correction_id"],
                active_summary_version_id="summary-v2",
            )

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=_Hermes(plan),
            conversation_loader=_loader,
            pending_proposal_writer=pending_writer,
            correction_writer=correction_writer,
        )
        result = await worker.run_once("worker-a")
        job = next(iter(repository.jobs.values()))

        assert result["status"] == "pending_review"
        assert job["outcome"] == "applied_with_pending"
        assert pending_order == [0, 2]
        assert apply_order == [1]
        assert job["proposal_ids"] == ["pending-0", "pending-2"]
        assert len(job["correction_ids"]) == 1
        assert job["receipt_refs"][0]["correction_id"] == job["correction_ids"][0]

    asyncio.run(run())


def test_stale_starting_version_is_typed_conflict_before_hermes_call():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        hermes = _Hermes({"outcome": "no_change", "proposals": []})

        async def stale_loader(uid, conversation_id):
            return _conversation("summary-v2")

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=hermes,
            conversation_loader=stale_loader,
        )
        result = await worker.run_once("worker-a")
        job = next(iter(repository.jobs.values()))

        assert result["status"] == "conflict"
        assert job["status"] == "conflict"
        assert job["outcome"] == "stale_version"
        assert hermes.calls == 0

    asyncio.run(run())


def test_retry_backoff_reaches_dead_letter_at_attempt_limit():
    class UnavailableHermes:
        async def propose(self, **kwargs):
            raise ReinterpretationWorkerError("hermes_unavailable", retryable=True)

    async def run():
        repository = InMemoryMemoryReinterpretationRepository(
            debounce_seconds=0,
            max_attempts=2,
        )
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=UnavailableHermes(),
            conversation_loader=_loader,
        )

        first = await worker.run_once("worker-a")
        job = next(iter(repository.jobs.values()))
        first_retry_at = job["not_before"]
        assert first["status"] == "retry"
        assert first_retry_at > repository.now

        repository.now = first_retry_at
        second = await worker.run_once("worker-b")
        assert second["status"] == "dead_letter"
        assert job["attempt_count"] == 2
        assert job["last_error_code"] == "hermes_unavailable"
        assert job["outcome"] == "failed"

    asyncio.run(run())


def test_exact_uid_case_collision_fails_closed():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.set_rows(
            UID.lower(),
            SESSION_ID,
            [
                {
                    "uid": UID.lower(),
                    "session_id": SESSION_ID,
                    "event_id": "foreign-turn",
                    "source_identity": "foreign",
                }
            ],
        )
        repository.now += timedelta(seconds=1)
        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=_Hermes({"outcome": "no_change", "proposals": []}),
            conversation_loader=_loader,
        )

        result = await worker.run_once("worker-a")
        job = next(iter(repository.jobs.values()))

        assert result["status"] == "dead_letter"
        assert result["error_code"] == "canonical_owner_collision"
        assert job["last_error_code"] == "canonical_owner_collision"

    asyncio.run(run())


def test_worker_crash_after_apply_reuses_plan_and_deterministic_receipt_without_duplicate_version():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        hermes = _Hermes(
            {
                "outcome": "proposals",
                "proposals": [
                    {
                        "kind": "factual_correction",
                        "certainty": "confirmed",
                        "correction_text": "The glasses are in the blue backpack.",
                        "evidence_event_ids": ["turn-1"],
                        "evidence_quote": "glasses are actually in the blue backpack",
                        "corrected_summary": {
                            "title": "Glasses in blue backpack",
                            "overview": "[Ella] The glasses are in the blue backpack.",
                            "category": "other",
                        },
                    }
                ],
            }
        )
        applied_ids = set()
        calls = []

        async def crash_once_writer(**kwargs):
            correction_id = kwargs["correction_id"]
            calls.append(correction_id)
            if correction_id not in applied_ids:
                applied_ids.add(correction_id)
                raise RuntimeError("crash after durable apply")
            return ApplyResult(
                correction_id=correction_id,
                active_summary_version_id="summary-v2",
                idempotent_replay=True,
            )

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=hermes,
            conversation_loader=_loader,
            correction_writer=crash_once_writer,
        )
        first = await worker.run_once("worker-a")
        assert first["status"] == "retry"

        repository.now = next(iter(repository.jobs.values()))["not_before"]
        second = await worker.run_once("worker-b")
        job = next(iter(repository.jobs.values()))

        assert second["status"] == "applied"
        assert hermes.calls == 1
        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert applied_ids == {calls[0]}
        assert job["correction_ids"] == [calls[0]]
        assert job["receipt_refs"] == [
            {
                "conversation_id": CONVERSATION_ID,
                "correction_id": calls[0],
                "status": "applied",
            }
        ]

    asyncio.run(run())


def test_worker_crash_after_pending_progress_keeps_pending_review_outcome():
    class FinishCrashRepository(InMemoryMemoryReinterpretationRepository):
        def __init__(self):
            super().__init__(debounce_seconds=0)
            self.crashed = False

        async def finish(self, job, **values):
            if values["status"] == "pending_review" and not self.crashed:
                self.crashed = True
                raise RuntimeError("crash after progress")
            return await super().finish(job, **values)

    async def run():
        repository = FinishCrashRepository()
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        hermes = _Hermes(
            {
                "outcome": "proposals",
                "proposals": [
                    {
                        "kind": "ambiguous_reinterpretation",
                        "certainty": "ambiguous",
                        "correction_text": "The user might mean another backpack.",
                        "evidence_event_ids": ["turn-1"],
                        "evidence_quote": "blue backpack",
                    }
                ],
            }
        )
        pending_calls = []

        async def pending_writer(**kwargs):
            pending_calls.append(kwargs["proposal_index"])
            return "pending-0"

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=hermes,
            conversation_loader=_loader,
            pending_proposal_writer=pending_writer,
        )
        first = await worker.run_once("worker-a")
        assert first["status"] == "retry"

        repository.now = next(iter(repository.jobs.values()))["not_before"]
        second = await worker.run_once("worker-b")
        job = next(iter(repository.jobs.values()))

        assert second["status"] == "pending_review"
        assert job["status"] == "pending_review"
        assert job["proposal_ids"] == ["pending-0"]
        assert pending_calls == [0]
        assert hermes.calls == 1

    asyncio.run(run())


def test_authenticated_status_is_identifier_only_and_missing_nonowned_have_parity(monkeypatch):
    async def seed():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        _, job = await _seed_job(repository)
        return repository, job

    repository, job = asyncio.run(seed())
    app = FastAPI()
    app.include_router(reinterpretation_router.create_memory_reinterpretation_router(repository))
    app.dependency_overrides[reinterpretation_router.auth.get_current_user_uid] = lambda: UID
    monkeypatch.setattr(
        reinterpretation_router.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: _conversation() if uid == UID and conversation_id == CONVERSATION_ID else None,
    )
    client = TestClient(app)

    response = client.get(f"/v1/ella/conversations/{CONVERSATION_ID}/reinterpretations/{job['id']}")
    assert response.status_code == 200
    body = response.json()["reinterpretation"]
    assert body["job_id"] == job["id"]
    assert "transcript_hash" not in body
    assert "canonical_refs" not in body
    assert "proposal_plan" not in body

    missing = client.get("/v1/ella/conversations/missing/reinterpretations/latest")
    app.dependency_overrides[reinterpretation_router.auth.get_current_user_uid] = lambda: UID.lower()
    nonowned = client.get(f"/v1/ella/conversations/{CONVERSATION_ID}/reinterpretations/latest")
    assert (missing.status_code, missing.json()) == (
        nonowned.status_code,
        nonowned.json(),
    )
    assert missing.status_code == 404


def test_omi_apply_boundary_uses_cas_canonical_receipt_and_idempotent_replay(monkeypatch):
    conversation = {
        **_conversation(),
        "summary_versions": [
            {
                "id": VERSION_ID,
                "title": "Glasses location",
                "overview": "[Ella] The glasses were near the desk.",
                "kind": "enriched",
                "is_active": True,
            }
        ],
    }
    corrected = {
        "id": "summary-v2",
        "title": "Glasses in backpack",
        "overview": "[Ella] The glasses are in the blue backpack.",
        "kind": "voice_reinterpreted",
        "correction_id": "correction-1",
        "based_on_version_id": VERSION_ID,
        "is_active": True,
    }
    audits = []
    apply_calls = []

    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: conversation,
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )

    async def apply_summary(**kwargs):
        apply_calls.append(kwargs)
        conversation["summary_versions"] = [conversation["summary_versions"][0], corrected]
        conversation["active_summary_version_id"] = corrected["id"]
        return {
            "status": "ok",
            "active_summary_version_id": corrected["id"],
            "idempotent_replay": False,
        }

    monkeypatch.setattr(corrections, "apply_summary_update", apply_summary)
    monkeypatch.setattr(
        corrections,
        "_correction_receipt",
        lambda **kwargs: corrections.ConversationCorrectionReceiptResponse(
            correction_id="correction-1",
            conversation_id=CONVERSATION_ID,
            status="applied",
            before_version_id=VERSION_ID,
            after_version_id="summary-v2",
            active_version_id="summary-v2",
            before=corrections.CorrectionSummarySnapshot(
                title="Glasses location",
                overview="[Ella] The glasses were near the desk.",
            ),
            after=corrections.CorrectionSummarySnapshot(
                title="Glasses in backpack",
                overview="[Ella] The glasses are in the blue backpack.",
            ),
        ),
    )

    first = asyncio.run(
        corrections.apply_memory_reinterpretation_correction(
            uid=UID,
            conversation_id=CONVERSATION_ID,
            correction_id="correction-1",
            trace_id="memory-reinterpretation:job-1:proposal:0",
            active_summary_version_id=VERSION_ID,
            correction_text="The glasses are in the blue backpack.",
            corrected_summary={
                "title": "Glasses in backpack",
                "overview": "[Ella] The glasses are in the blue backpack.",
                "category": "other",
            },
            evidence_event_ids=["turn-1"],
            source_session_id=SESSION_ID,
        )
    )
    second = asyncio.run(
        corrections.apply_memory_reinterpretation_correction(
            uid=UID,
            conversation_id=CONVERSATION_ID,
            correction_id="correction-1",
            trace_id="memory-reinterpretation:job-1:proposal:0",
            active_summary_version_id=VERSION_ID,
            correction_text="The glasses are in the blue backpack.",
            corrected_summary={
                "title": "Glasses in backpack",
                "overview": "[Ella] The glasses are in the blue backpack.",
                "category": "other",
            },
            evidence_event_ids=["turn-1"],
            source_session_id=SESSION_ID,
        )
    )

    assert first["active_summary_version_id"] == "summary-v2"
    assert second["idempotent_replay"] is True
    assert second["receipt"]["after_version_id"] == "summary-v2"
    assert len(apply_calls) == 1
    assert apply_calls[0]["require_based_on_match"] is True
    assert apply_calls[0]["require_canonical"] is True
    assert apply_calls[0]["correction_id"] == "correction-1"
    assert audits[0]["status"] == "submitted"
    assert audits[-1]["status"] == "applied"
