import asyncio
import os
import sys
import types
from datetime import timedelta
from types import SimpleNamespace

import pytest
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

from database.memory_reinterpretations import (
    InMemoryMemoryReinterpretationRepository,
    PostgresMemoryReinterpretationRepository,
    canonical_refs,
    canonical_transcript_hash,
)
from ella.routers import corrections, memory_reinterpretation as reinterpretation_router
from ella.routers.canonical_events import (
    CanonicalEventIn,
    InMemoryCanonicalEventStore,
    PostgresCanonicalEventStore,
    SessionCompleteIn,
    create_canonical_events_router,
)
from ella.routers import canonical_events
from ella.services import memory_reinterpretation as reinterpretation_service
from ella.services.memory_reinterpretation import (
    ApplyResult,
    MemoryReinterpretationWorker,
    ReinterpretationPlan,
    ReinterpretationProposal,
    ReinterpretationWorkerError,
    run_worker_loop,
    worker_runtime_metrics,
    _validate_rows,
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
        assert job["transcript_revision"] == 2
        assert [ref["event_id"] for ref in job["canonical_refs"]] == ["turn-1", "turn-2"]

    asyncio.run(run())


def test_changed_completion_during_running_invalidates_lease_and_stale_finish():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        store, job = await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        claimed = await repository.claim_due("worker-a", lease_seconds=30)
        assert claimed is not None

        await store.write_batch(
            [
                _event(
                    "turn-2",
                    "The backpack is beside the door.",
                    connection_id="connection-b",
                    started_at="2026-07-24T18:01:20Z",
                )
            ]
        )
        session_id, completion = _completion()
        await store.complete_session(session_id, completion)
        current = repository.jobs[job["id"]]

        assert current["status"] == "pending"
        assert current["transcript_revision"] == 2
        assert current["lease_token"] is None
        assert current["proposal_plan"] is None
        assert repository.attempts[0]["status"] == "superseded"
        assert await repository.record_plan(claimed, {"outcome": "no_change", "proposals": []}) is False
        assert (
            await repository.finish(
                claimed,
                status="no_change",
                outcome="no_change",
                proposal_ids=[],
                correction_ids=[],
                receipt_refs=[],
            )
            is False
        )

    asyncio.run(run())


def test_changed_completion_after_plan_before_retry_forces_fresh_analysis():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        store, job = await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        claimed = await repository.claim_due("worker-a", lease_seconds=30)
        assert claimed is not None
        assert await repository.record_plan(
            claimed,
            {"outcome": "no_change", "proposals": []},
        )
        assert (
            await repository.fail_or_retry(
                claimed,
                error_code="temporary",
                retryable=True,
            )
            == "retry"
        )

        await store.write_batch(
            [
                _event(
                    "turn-2",
                    "Actually it is the green backpack.",
                    connection_id="connection-b",
                    started_at="2026-07-24T18:01:20Z",
                )
            ]
        )
        session_id, completion = _completion()
        await store.complete_session(session_id, completion)
        current = repository.jobs[job["id"]]

        assert current["status"] == "pending"
        assert current["transcript_revision"] == 2
        assert current["proposal_plan"] is None
        assert current["progress"] == {}
        assert current["proposal_ids"] == []
        assert current["correction_ids"] == []
        assert current["attempt_count"] == 0

    asyncio.run(run())


def test_completion_after_terminal_outcome_does_not_rewrite_processed_transcript():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        store, job = await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        claimed = await repository.claim_due("worker-a", lease_seconds=30)
        assert claimed is not None
        assert await repository.finish(
            claimed,
            status="no_change",
            outcome="no_change",
            proposal_ids=[],
            correction_ids=[],
            receipt_refs=[],
        )
        original = dict(repository.jobs[job["id"]])

        await store.write_batch(
            [
                _event(
                    "turn-2",
                    "A later reconnect turn.",
                    connection_id="connection-b",
                    started_at="2026-07-24T18:01:20Z",
                )
            ]
        )
        session_id, completion = _completion()
        await store.complete_session(session_id, completion)
        current = repository.jobs[job["id"]]

        assert current["status"] == "no_change"
        assert current["transcript_revision"] == original["transcript_revision"]
        assert current["transcript_hash"] == original["transcript_hash"]
        assert current["canonical_refs"] == original["canonical_refs"]
        assert current["updated_at"] == original["updated_at"]

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


def test_postgres_expired_lease_at_attempt_ceiling_closes_attempt_and_dead_letters():
    statements = []
    candidate = {
        "id": "job-1",
        "uid": UID,
        "logical_session_id": SESSION_ID,
        "conversation_id": CONVERSATION_ID,
        "starting_summary_version_id": VERSION_ID,
        "transcript_hash": "hash",
        "transcript_revision": 3,
        "status": "running",
        "lease_token": "expired-lease",
        "attempt_count": 2,
        "max_attempts": 2,
    }

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *args):
            statements.append(("fetchrow", query, args))
            assert "FOR UPDATE SKIP LOCKED" in query
            return candidate

        async def execute(self, query, *args):
            statements.append(("execute", query, args))
            return "UPDATE 1"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    async def get_pool():
        return Pool()

    repository = PostgresMemoryReinterpretationRepository(get_pool, max_attempts=2)
    result = asyncio.run(repository.claim_due("worker-b"))

    assert result is None
    executed = [query for kind, query, args in statements if kind == "execute"]
    assert any("UPDATE memory_reinterpretation_attempts" in query for query in executed)
    assert any("lease_expired_attempt_limit" in query for query in executed)
    assert not any("INSERT INTO memory_reinterpretation_attempts" in query for query in executed)


def test_postgres_stale_fail_does_not_close_attempt_or_report_retry():
    statements = []

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            statements.append(query)
            if "UPDATE memory_reinterpretation_attempts" in query:
                raise AssertionError("stale worker must not close the attempt")
            return "UPDATE 0"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    async def get_pool():
        return Pool()

    repository = PostgresMemoryReinterpretationRepository(get_pool)
    transition = asyncio.run(
        repository.fail_or_retry(
            {
                "id": "job-1",
                "lease_token": "stale-lease",
                "transcript_revision": 2,
                "attempt_count": 1,
                "max_attempts": 5,
            },
            error_code="stale_worker",
            retryable=True,
        )
    )

    assert transition == "lease_lost"
    assert len(statements) == 1


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
        headers={
            "Authorization": "Bearer ledger-secret",
            "X-Ella-Subject-Uid": UID,
        },
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


@pytest.mark.parametrize(
    "error_code",
    (
        "managed_cloud_consent_stale",
        "hermes_cloud_quarantined",
        "hermes_cloud_runtime_authority_changed",
    ),
)
def test_reinterpretation_final_authority_change_sends_zero_transcripts(monkeypatch, error_code):
    provider_posts = 0
    authority = SimpleNamespace(
        uid=UID,
        target_mode="hermes-cloud-transcript",
        digest="a" * 64,
    )
    config = SimpleNamespace(
        provider="hermes-api",
        hermes_api_key="",
        cloud_authority=authority,
        timeout_seconds=45,
    )

    class TrackingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            nonlocal provider_posts
            provider_posts += 1
            raise AssertionError("provider post must not be reached")

    async def config_for_uid(uid):
        assert uid == UID
        return config

    async def deny_current_authority(selected):
        assert selected is config
        raise RuntimeError(error_code)

    monkeypatch.setattr(
        reinterpretation_service,
        "summary_provider_config_for_uid",
        config_for_uid,
    )
    monkeypatch.setattr(
        reinterpretation_service,
        "resolve_summary_provider_send",
        deny_current_authority,
    )
    monkeypatch.setattr(reinterpretation_service.httpx, "AsyncClient", TrackingClient)

    with pytest.raises(ReinterpretationWorkerError) as error:
        asyncio.run(
            reinterpretation_service.HermesReinterpretationClient().propose(
                job={"uid": UID, "id": "reinterpretation-job"},
                transcript="Complete protected transcript.",
                current_summary={"overview": "Current summary."},
                event_ids=["event-a"],
            )
        )

    assert error.value.code == "hermes_invalid_response"
    assert provider_posts == 0


async def _loader(uid, conversation_id):
    if uid == UID and conversation_id == CONVERSATION_ID:
        return _conversation()
    return None


def test_daily_card_worker_scope_validation_and_no_change_completion():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        job = next(iter(repository.jobs.values()))
        rows = repository.rows[(UID, SESSION_ID)]
        for row in rows:
            row["scope_kind"] = "daily_card"

        _validate_rows(job, rows)

        mixed_rows = [
            *rows,
            {
                **rows[0],
                "event_id": "mixed-memory-turn",
                "source_identity": "mixed-memory-source",
                "scope_kind": "memory",
            },
        ]
        mixed_job = {
            **job,
            "transcript_hash": canonical_transcript_hash(mixed_rows),
            "canonical_refs": canonical_refs(mixed_rows),
        }
        with pytest.raises(ReinterpretationWorkerError, match="canonical_scope_mismatch") as error:
            _validate_rows(mixed_job, mixed_rows)
        assert error.value.retryable is False

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


def test_pending_proposal_ingest_dedupes_within_revision_and_separates_new_revision(monkeypatch):
    from ella.services import proposal_ingest

    proposals_by_key = {}

    def get_by_key(profile_uid, idempotency_key):
        return proposals_by_key.get((profile_uid, idempotency_key))

    def save(proposal):
        proposals_by_key[(proposal.profile_uid, proposal.idempotency_key)] = proposal
        return proposal

    monkeypatch.setattr(proposal_ingest.proposals_db, "get_proposal_by_idempotency_key", get_by_key)
    monkeypatch.setattr(proposal_ingest.proposals_db, "save_proposal", save)

    base_job = {
        "id": "job-revision-boundary",
        "uid": UID,
        "conversation_id": CONVERSATION_ID,
        "starting_summary_version_id": VERSION_ID,
        "transcript_hash": "revision-1-hash",
        "transcript_revision": 1,
    }
    revision_1 = ReinterpretationProposal(
        kind="ambiguous_reinterpretation",
        certainty="ambiguous",
        correction_text="The glasses may be in the blue backpack.",
        evidence_event_ids=["turn-1"],
        evidence_quote="blue backpack",
    )
    revision_2 = ReinterpretationProposal(
        kind="ambiguous_reinterpretation",
        certainty="ambiguous",
        correction_text="The glasses may instead be beside the front door.",
        evidence_event_ids=["turn-2"],
        evidence_quote="beside the front door",
    )

    first_id = asyncio.run(
        reinterpretation_service._create_pending_proposal(
            job=base_job,
            proposal=revision_1,
            proposal_id="fallback-revision-1",
            proposal_index=0,
        )
    )
    retry_id = asyncio.run(
        reinterpretation_service._create_pending_proposal(
            job=base_job,
            proposal=revision_1,
            proposal_id="different-fallback-must-not-be-used",
            proposal_index=0,
        )
    )
    newer_job = {
        **base_job,
        "transcript_revision": 2,
        "transcript_hash": "revision-2-hash",
    }
    newer_id = asyncio.run(
        reinterpretation_service._create_pending_proposal(
            job=newer_job,
            proposal=revision_2,
            proposal_id="fallback-revision-2",
            proposal_index=0,
        )
    )

    assert retry_id == first_id
    assert newer_id != first_id
    assert len(proposals_by_key) == 2
    revision_1_saved = proposals_by_key[(UID, "memory-reinterpretation:job-revision-boundary:revision:1:proposal:0")]
    revision_2_saved = proposals_by_key[(UID, "memory-reinterpretation:job-revision-boundary:revision:2:proposal:0")]
    assert revision_1_saved.payload["description"] == revision_1.correction_text
    assert revision_2_saved.payload["description"] == revision_2.correction_text
    assert revision_1_saved.trace_id.endswith("revision:1:proposal:0")
    assert revision_2_saved.trace_id.endswith("revision:2:proposal:0")


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


def test_expired_process_lease_dead_letters_at_attempt_ceiling():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(
            debounce_seconds=0,
            max_attempts=2,
        )
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)

        first = await repository.claim_due("worker-a", lease_seconds=30)
        assert first is not None
        repository.now += timedelta(seconds=31)
        second = await repository.claim_due("worker-b", lease_seconds=30)
        assert second is not None
        assert second["attempt_count"] == 2
        assert repository.attempts[0]["status"] == "lease_expired"

        repository.now += timedelta(seconds=31)
        assert await repository.claim_due("worker-c", lease_seconds=30) is None
        job = next(iter(repository.jobs.values()))
        assert job["status"] == "dead_letter"
        assert job["attempt_count"] == 2
        assert job["last_error_code"] == "lease_expired_attempt_limit"
        assert [attempt["status"] for attempt in repository.attempts] == [
            "lease_expired",
            "lease_expired",
        ]

    asyncio.run(run())


def test_stale_worker_cannot_record_retry_or_close_new_attempt():
    async def run():
        repository = InMemoryMemoryReinterpretationRepository(
            debounce_seconds=0,
            max_attempts=3,
        )
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        first = await repository.claim_due("worker-a", lease_seconds=30)
        assert first is not None
        repository.now += timedelta(seconds=31)
        second = await repository.claim_due("worker-b", lease_seconds=30)
        assert second is not None

        transition = await repository.fail_or_retry(
            first,
            error_code="stale_worker_failure",
            retryable=True,
        )
        job = next(iter(repository.jobs.values()))

        assert transition == "lease_lost"
        assert job["status"] == "running"
        assert job["lease_token"] == second["lease_token"]
        assert repository.attempts[-1]["status"] == "running"

    asyncio.run(run())


def test_worker_verifies_revision_lease_immediately_before_side_effect():
    class SupersedeAfterPlanRepository(InMemoryMemoryReinterpretationRepository):
        async def record_plan(self, job, plan):
            recorded = await super().record_plan(job, plan)
            current = self.jobs[job["id"]]
            current["transcript_revision"] += 1
            current["status"] = "pending"
            current["lease_owner"] = None
            current["lease_token"] = None
            current["lease_expires_at"] = None
            return recorded

    async def run():
        repository = SupersedeAfterPlanRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)
        writes = []
        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=_Hermes(
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
                                "overview": "[Ella] The glasses are in the blue backpack.",
                            },
                        }
                    ],
                }
            ),
            conversation_loader=_loader,
            correction_writer=lambda **kwargs: writes.append(kwargs),
        )

        result = await worker.run_once("worker-a")

        assert result["status"] == "lease_lost"
        assert writes == []

    asyncio.run(run())


def test_worker_heartbeat_renews_long_running_lease(monkeypatch):
    class HeartbeatRepository:
        def __init__(self):
            self.calls = []

        async def renew_lease(self, job, *, lease_seconds):
            self.calls.append((job["id"], job["transcript_revision"], lease_seconds))
            return False

    async def run():
        repository = HeartbeatRepository()
        worker = MemoryReinterpretationWorker(repository, lease_seconds=30)

        async def no_wait(seconds):
            return None

        monkeypatch.setattr(reinterpretation_service.asyncio, "sleep", no_wait)
        await worker._heartbeat_lease(
            {
                "id": "job-1",
                "transcript_revision": 4,
            }
        )

        assert repository.calls == [("job-1", 4, 30)]

    asyncio.run(run())


def test_worker_loop_recovers_after_transient_claim_failure():
    class FlakyWorker:
        def __init__(self):
            self.calls = 0

        async def run_once(self, worker_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary postgres outage")
            return {"status": "ok"}

    async def run():
        worker = FlakyWorker()
        sleeps = []
        before = worker_runtime_metrics()["loop_failures_total"]

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        await run_worker_loop(
            worker,
            max_iterations=2,
            sleep_func=fake_sleep,
        )

        assert worker.calls == 2
        assert sleeps == [1.0]
        assert worker_runtime_metrics()["loop_failures_total"] == before + 1

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
    app.dependency_overrides[reinterpretation_router.get_exact_firebase_uid] = lambda: UID
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
    app.dependency_overrides[reinterpretation_router.get_exact_firebase_uid] = lambda: UID.lower()
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


def test_worker_reinterpretation_receipt_and_authenticated_undo_restore_prior_summary(monkeypatch):
    conversation = {
        **_conversation(),
        "summary_versions": [
            {
                "id": VERSION_ID,
                "title": "Glasses location",
                "overview": "[Ella] The glasses were near the desk.",
                "emoji": "👓",
                "category": "other",
                "kind": "enriched",
                "is_active": True,
            }
        ],
    }
    audit: dict = {}

    class AuditRef:
        def set(self, payload, merge=False):
            if merge:
                audit.update(payload)
            else:
                audit.clear()
                audit.update(payload)

        def get(self):
            return SimpleNamespace(
                exists=bool(audit),
                to_dict=lambda: dict(audit),
            )

        def collection(self, name):
            return SimpleNamespace(stream=lambda: [])

    def load_conversation(uid, conversation_id):
        if uid == UID and conversation_id == CONVERSATION_ID:
            return conversation
        return None

    def update_conversation(uid, conversation_id, update):
        assert uid == UID
        assert conversation_id == CONVERSATION_ID
        conversation.update(update)

    async def apply_summary_update(**kwargs):
        active_id = kwargs["active_summary_version_id"]
        assert conversation["active_summary_version_id"] == active_id
        for version in conversation["summary_versions"]:
            version["is_active"] = False
        if kwargs["summary_kind"] == "voice_reinterpreted":
            version_id = "summary-v2"
        else:
            assert kwargs["summary_kind"] == "correction_undo"
            version_id = "summary-undo-v3"
        version = {
            "id": version_id,
            **kwargs["summary"],
            "kind": kwargs["summary_kind"],
            "based_on_version_id": active_id,
            "is_active": True,
        }
        if kwargs.get("correction_id"):
            version["correction_id"] = kwargs["correction_id"]
        conversation["summary_versions"].append(version)
        conversation["active_summary_version_id"] = version_id
        conversation["structured"] = {
            "title": version.get("title") or "",
            "overview": version.get("overview") or "",
            "emoji": version.get("emoji") or "",
            "category": version.get("category") or "other",
        }
        return {
            "status": "ok",
            "active_summary_version_id": version_id,
            "idempotent_replay": False,
        }

    async def revert_propagations(*args, **kwargs):
        return 0

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", load_conversation)
    monkeypatch.setattr(corrections.conversations_db, "update_conversation", update_conversation)
    monkeypatch.setattr(corrections, "_audit_ref", lambda *args: AuditRef())
    monkeypatch.setattr(corrections, "apply_summary_update", apply_summary_update)
    monkeypatch.setattr(corrections, "_prepare_applied_propagation_rollbacks", lambda *args: [])
    monkeypatch.setattr(corrections, "_revert_applied_propagations", revert_propagations)
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))

    async def run_worker():
        repository = InMemoryMemoryReinterpretationRepository(debounce_seconds=0)
        await _seed_job(repository)
        repository.now += timedelta(seconds=1)

        async def conversation_loader(uid, conversation_id):
            return load_conversation(uid, conversation_id)

        worker = MemoryReinterpretationWorker(
            repository,
            hermes_client=_Hermes(
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
                                "title": "Glasses in backpack",
                                "overview": "[Ella] The glasses are in the blue backpack.",
                                "emoji": "👓",
                                "category": "other",
                            },
                        }
                    ],
                }
            ),
            conversation_loader=conversation_loader,
        )
        result = await worker.run_once("worker-a")
        return repository, result

    repository, result = asyncio.run(run_worker())
    job = next(iter(repository.jobs.values()))
    correction_id = job["correction_ids"][0]
    assert result["status"] == "applied"
    assert conversation["active_summary_version_id"] == "summary-v2"
    assert audit["source"] == "voice-memory-reinterpretation"

    app = FastAPI()
    app.include_router(corrections.router)
    app.dependency_overrides[corrections.get_exact_firebase_uid] = lambda: UID
    client = TestClient(app)

    receipt = client.get(f"/v1/ella/conversations/{CONVERSATION_ID}/corrections/{correction_id}")
    assert receipt.status_code == 200
    assert receipt.json()["status"] == "applied"
    assert receipt.json()["before_version_id"] == VERSION_ID
    assert receipt.json()["after_version_id"] == "summary-v2"

    undone = client.post(f"/v1/ella/conversations/{CONVERSATION_ID}/corrections/{correction_id}/undo")
    assert undone.status_code == 200
    assert undone.json()["status"] == "undone"
    assert undone.json()["active_version_id"] == "summary-undo-v3"
    assert undone.json()["undo_version_id"] == "summary-undo-v3"
    assert conversation["active_summary_version_id"] == "summary-undo-v3"
    active = next(version for version in conversation["summary_versions"] if version["id"] == "summary-undo-v3")
    assert active["title"] == "Glasses location"
    assert active["overview"] == "[Ella] The glasses were near the desk."
