import asyncio
import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.services.runtime_errors import ProvisioningError


def _install_proposal_stubs():
    proposals = types.ModuleType("database.proposals")
    proposals.get_proposal = MagicMock(return_value=None)
    proposals.get_proposal_by_idempotency_key = MagicMock(return_value=None)
    proposals.list_proposals = MagicMock(return_value=[])
    proposals.save_proposal = MagicMock(side_effect=lambda proposal: proposal)
    proposals.update_proposal_status = MagicMock(return_value=None)
    database = importlib.import_module("database")
    setattr(database, "proposals", proposals)
    sys.modules["database.proposals"] = proposals


def _load_observer_service():
    _install_proposal_stubs()
    sys.modules.pop("ella.services.observer", None)
    return importlib.import_module("ella.services.observer")


def _event(**overrides):
    metadata = overrides.pop("metadata", None)
    if metadata is None:
        metadata = {
            "observer": {
                "proposals": [
                    {
                        "proposal_type": "memory_note",
                        "title": "Spare glasses location",
                        "description": "Plato said the spare glasses are in the blue backpack pocket.",
                        "requested_change": {
                            "memory": "Spare glasses are in the blue backpack pocket.",
                            "entity": "spare glasses",
                        },
                        "reason": "User explicitly provided a durable location fact.",
                        "confidence": 0.94,
                    }
                ]
            }
        }
    return {
        "uid": "user-1",
        "canonical_identity": "plato",
        "event_id": "evt-1",
        "source_identity": "ios_voice:call-1",
        "channel": "ios_voice",
        "provider": "gemini-live",
        "role": "user",
        "text": "I put the spare glasses in the blue backpack pocket.",
        "started_at": "2026-05-07T20:00:00+00:00",
        "metadata": metadata,
        **overrides,
    }


def test_observer_dry_run_logs_candidate_without_mutating():
    service = _load_observer_service()
    created = []

    log = service.run_observer(
        profile_uid="user-1",
        canonical_identity="plato",
        cursor_before="2026-05-07T19:55:00+00:00",
        dry_run=True,
        events=[_event()],
        create_proposal=lambda **kwargs: created.append(kwargs) or {},
        run_id="run-1",
    )

    assert created == []
    assert log.run_id == "run-1"
    assert log.dry_run is True
    assert log.status == "success"
    assert log.source_counts == {"ios_voice": 1}
    assert log.cursor_after == "2026-05-07T20:00:00+00:00"
    assert log.candidate_count == 1
    assert log.proposal_count == 1
    assert log.decisions[0].action == "would_create_proposal"
    assert log.decisions[0].reason == "User explicitly provided a durable location fact."
    assert log.decisions[0].idempotency_key.startswith("observer:user-1:evt-1:memory_note:")


def test_observer_live_creates_proposal_with_observer_claims():
    service = _load_observer_service()
    captured = []

    def fake_create(**kwargs):
        captured.append(kwargs)
        return {"created": True, "proposal": {"proposal_id": "proposal-1", "status": "submitted"}}

    log = service.run_observer(
        profile_uid="user-1",
        dry_run=False,
        events=[_event()],
        create_proposal=fake_create,
        run_id="run-2",
    )

    assert log.proposal_ids == ["proposal-1"]
    assert log.decisions[0].action == "created_proposal"
    assert captured[0]["session_claims"]["sub"] == "ella-observer-cron"
    assert captured[0]["session_claims"]["allowed_tools"] == ["ella_observer_fact_promotion"]
    assert captured[0]["tool_name"] == "ella_observer_fact_promotion"
    assert captured[0]["proposal_type"] == "memory_note"
    assert captured[0]["payload"]["write_policy"] == "proposal_only"
    assert captured[0]["payload"]["observer_run_id"] == "run-2"


def test_observer_skips_malformed_or_unsupported_candidates():
    service = _load_observer_service()
    event = _event(
        event_id="evt-bad",
        metadata={
            "observer_proposals": [
                {"proposal_type": "direct_mutation", "title": "Bad", "description": "Should not pass"},
                {"proposal_type": "memory_note", "description": "Missing title"},
            ]
        },
    )

    log = service.run_observer(profile_uid="user-1", dry_run=False, events=[event], run_id="run-3")

    assert log.proposal_count == 0
    assert log.skipped_count == 2
    assert [decision.reason for decision in log.decisions] == [
        "unsupported_proposal_type",
        "missing_title_or_description",
    ]


def test_observer_logs_events_with_no_candidates():
    service = _load_observer_service()

    log = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[_event(event_id="evt-empty", metadata={})],
        run_id="run-4",
    )

    assert log.candidate_count == 0
    assert log.proposal_count == 0
    assert log.skipped_count == 1
    assert log.decisions[0].reason == "no_structured_observer_candidates"


def test_heuristic_extractor_promotes_explicit_memory_and_scanner_requests():
    service = _load_observer_service()
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    event = _event(
        event_id="evt-heuristic",
        metadata={},
        text="Hey Ella, please remember my spare glasses are in the blue backpack. Also alert me if I ask where they are.",
    )
    result = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[event],
        extractor=extractor_module.heuristic_candidate_extractor,
        run_id="run-heuristic",
    )

    assert result.proposal_count == 2
    assert {decision.proposal_type for decision in result.decisions} == {"memory_note", "scanner_rule_change"}


def test_heuristic_extractor_ignores_omi_summary_language():
    service = _load_observer_service()
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    event = _event(
        event_id="evt-omi-summary",
        channel="omi",
        role="system",
        metadata={},
        text="[Ella] This summary says Ella should remember that the voice memory fix worked.",
    )
    result = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[event],
        extractor=extractor_module.heuristic_candidate_extractor,
        run_id="run-omi-summary",
    )

    assert result.proposal_count == 0
    assert result.decisions[0].reason == "no_structured_observer_candidates"


def test_heuristic_extractor_does_not_treat_memory_questions_as_instructions():
    service = _load_observer_service()
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    question = _event(
        event_id="evt-memory-question",
        metadata={},
        channel="ios_chat",
        role="user",
        text="Hey Ella, do you remember what I ordered for breakfast this morning?",
    )
    instruction = _event(
        event_id="evt-memory-instruction",
        metadata={},
        channel="ios_chat",
        role="user",
        text="Hey Ella, please remember where I put the valet key.",
    )

    question_log = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[question],
        extractor=extractor_module.heuristic_candidate_extractor,
        run_id="run-memory-question",
    )
    instruction_log = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[instruction],
        extractor=extractor_module.heuristic_candidate_extractor,
        run_id="run-memory-instruction",
    )

    assert question_log.proposal_count == 0
    assert instruction_log.proposal_count == 1
    assert instruction_log.decisions[0].proposal_type == "memory_note"


def test_observer_extractors_ignore_synthetic_e2e_events():
    service = _load_observer_service()
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    synthetic = _event(
        event_id="e2e:ios_chat:123",
        channel="ios_chat",
        provider="ella-memory-e2e",
        role="user",
        text="Please remember the E2E phrase synthetic anchor.",
        metadata={"synthetic": True, "test": "ella_memory_e2e_smoke"},
    )
    structured_synthetic = _event(
        event_id="e2e:structured",
        metadata={
            "synthetic": True,
            "observer": {
                "proposals": [
                    {
                        "proposal_type": "memory_note",
                        "title": "Synthetic",
                        "description": "Synthetic",
                        "requested_change": {"memory": "synthetic"},
                        "confidence": 0.95,
                    }
                ]
            },
        },
    )

    heuristic_log = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[synthetic],
        extractor=extractor_module.heuristic_candidate_extractor,
        run_id="run-synthetic-heuristic",
    )
    structured_log = service.run_observer(
        profile_uid="user-1",
        dry_run=True,
        events=[structured_synthetic],
        extractor=extractor_module.combined_extractor(extractor_module.ExtractionResult()),
        run_id="run-synthetic-structured",
    )

    assert heuristic_log.proposal_count == 0
    assert structured_log.proposal_count == 0


def test_observer_router_runs_against_in_memory_test_ledger(monkeypatch):
    _install_proposal_stubs()
    monkeypatch.setenv("ELLA_OBSERVER_ADMIN_TOKEN", "observer-token")
    sys.modules.pop("ella.routers.observer", None)
    sys.modules.pop("ella.services.observer_apply", None)
    router_module = importlib.import_module("ella.routers.observer")
    canonical_module = importlib.import_module("ella.routers.canonical_events")
    logs_module = importlib.import_module("ella.services.observer_logs")

    event_store = canonical_module.InMemoryCanonicalEventStore()
    log_store = logs_module.InMemoryObserverRunLogStore()

    asyncio.run(
        event_store.write_batch(
            [
                canonical_module.CanonicalEventIn(
                    uid="user-1",
                    canonical_identity="plato",
                    event_id="evt-router",
                    channel="ios_chat",
                    provider="ella-ios",
                    role="user",
                    text="Please remember the doctor is Dr. Pu.",
                    started_at="2026-05-07T20:05:00+00:00",
                    source_ref={"message_id": "m-1"},
                    metadata={
                        "observer": {
                            "proposals": [
                                {
                                    "proposal_type": "profile_update",
                                    "title": "Doctor name correction",
                                    "description": "The doctor name should be Dr. Pu.",
                                    "requested_change": {"doctor_name": "Dr. Pu"},
                                    "reason": "User corrected a relationship/entity fact.",
                                    "confidence": 0.98,
                                }
                            ]
                        }
                    },
                )
            ]
        )
    )

    app = FastAPI()
    app.include_router(router_module.create_observer_router(event_store=event_store, log_store=log_store))
    client = TestClient(app)

    response = client.post(
        "/v1/ella/observer/run",
        headers={"X-Ella-Observer-Token": "observer-token"},
        json={"uid": "user-1", "dry_run": True, "channels": ["ios_chat"], "limit": 10},
    )

    assert response.status_code == 200
    body = response.json()["observer_run"]
    assert body["source_event_count"] == 1
    assert body["proposal_count"] == 1
    assert body["decisions"][0]["action"] == "would_create_proposal"

    run_id = body["run_id"]
    readback = client.get(
        f"/v1/ella/observer/runs/{run_id}",
        headers={"Authorization": "Bearer observer-token"},
    )
    assert readback.status_code == 200
    assert readback.json()["observer_run"]["run_id"] == run_id


def test_observer_router_can_use_extraction_result(monkeypatch):
    _install_proposal_stubs()
    monkeypatch.setenv("ELLA_OBSERVER_ADMIN_TOKEN", "observer-token")
    sys.modules.pop("ella.routers.observer", None)
    sys.modules.pop("ella.services.observer_apply", None)
    router_module = importlib.import_module("ella.routers.observer")
    canonical_module = importlib.import_module("ella.routers.canonical_events")
    logs_module = importlib.import_module("ella.services.observer_logs")
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    event_store = canonical_module.InMemoryCanonicalEventStore()
    log_store = logs_module.InMemoryObserverRunLogStore()

    asyncio.run(
        event_store.write_batch(
            [
                canonical_module.CanonicalEventIn(
                    uid="user-1",
                    canonical_identity="plato",
                    event_id="evt-router-extracted",
                    channel="ios_chat",
                    provider="ella-ios",
                    role="user",
                    text="Please remember that the valet key is in the blue ceramic bowl.",
                    started_at="2026-05-07T20:05:00+00:00",
                    source_ref={"message_id": "m-2"},
                    metadata={},
                )
            ]
        )
    )

    async def fake_build_extraction_result(events, *, mode, uid="", timeout_seconds=45.0, limit=60):
        assert mode == "heuristic"
        assert uid == "user-1"
        return await extractor_module.build_extraction_result(
            events,
            mode=mode,
            uid=uid,
            timeout_seconds=timeout_seconds,
            limit=limit,
        )

    monkeypatch.setattr(router_module, "build_extraction_result", fake_build_extraction_result)

    app = FastAPI()
    app.include_router(router_module.create_observer_router(event_store=event_store, log_store=log_store))
    client = TestClient(app)

    response = client.post(
        "/v1/ella/observer/run",
        headers={"X-Ella-Observer-Token": "observer-token"},
        json={"uid": "user-1", "dry_run": True, "extractor_mode": "heuristic", "limit": 10},
    )

    assert response.status_code == 200
    body = response.json()["observer_run"]
    assert body["source_event_count"] == 1
    assert body["proposal_count"] == 1
    assert body["model_metadata"]["extractor_mode"] == "heuristic"
    assert body["model_metadata"]["extractor"] == "heuristic_candidate_extractor"


def test_observer_extractor_uses_isolated_runtime_for_hermes(monkeypatch):
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")
    captured = {}

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-isolated"
        assert target_mode == "hermes-cloud-guardian"
        return types.SimpleNamespace(
            provider="hermes_cloud",
            gateway_url="http://isolated-hermes:8642",
            gateway_token="isolated-token",
            agent_id="omi-isolated",
        )

    async def fake_hermes(events, **kwargs):
        captured.update(kwargs)
        return extractor_module.ExtractionResult(metadata={"extractor": "hermes"})

    monkeypatch.setattr(
        extractor_module.runtime_resolver,
        "runtime_authority_enabled",
        lambda uid=None: uid == "uid-isolated",
    )
    monkeypatch.setattr(extractor_module.runtime_resolver, "resolve_isolated_runtime", fake_runtime)
    authority = extractor_module.runtime_resolver.CloudRuntimeAuthorityIdentity(
        uid="uid-isolated",
        target_mode="hermes-cloud-guardian",
        digest="a" * 64,
    )
    monkeypatch.setattr(
        extractor_module.runtime_resolver,
        "cloud_runtime_authority_identity",
        lambda runtime: authority,
    )
    monkeypatch.setattr(extractor_module, "hermes_candidate_extraction", fake_hermes)

    asyncio.run(
        extractor_module.build_extraction_result(
            [_event()],
            mode="hermes",
            uid="uid-isolated",
        )
    )

    assert captured == {
        "timeout_seconds": 45.0,
        "limit": 60,
        "cloud_authority": authority,
    }


@pytest.mark.parametrize(
    "error_code",
    (
        "managed_cloud_consent_stale",
        "hermes_cloud_quarantined",
        "hermes_cloud_runtime_authority_changed",
    ),
)
def test_observer_final_authority_change_sends_zero_protected_events(monkeypatch, error_code):
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")
    provider_posts = 0

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

    async def deny_current_authority(identity):
        raise ProvisioningError(error_code, retryable=False)

    monkeypatch.setattr(extractor_module.httpx, "AsyncClient", TrackingClient)
    monkeypatch.setattr(
        extractor_module.runtime_resolver,
        "revalidate_cloud_runtime_authority",
        deny_current_authority,
    )
    authority = extractor_module.runtime_resolver.CloudRuntimeAuthorityIdentity(
        uid="uid-isolated",
        target_mode="hermes-cloud-guardian",
        digest="a" * 64,
    )

    result = asyncio.run(
        extractor_module.hermes_candidate_extraction(
            [_event()],
            cloud_authority=authority,
        )
    )

    assert provider_posts == 0
    assert result.metadata["error"] == "ProvisioningError"


def test_model_extractor_rejects_assistant_only_evidence():
    sys.modules.pop("ella.services.observer_extractor", None)
    extractor_module = importlib.import_module("ella.services.observer_extractor")

    event_lookup = {
        "assistant-event": _event(
            event_id="assistant-event",
            channel="ios_chat",
            role="assistant",
            metadata={},
            text="I will remind you every evening.",
        )
    }
    candidate = extractor_module._candidate_from_model(
        {
            "proposal_type": "reminder_request",
            "title": "Evening reminder",
            "description": "Assistant said it would remind the user.",
            "requested_change": {"reminder_text": "evening reminder"},
            "confidence": 0.9,
            "evidence_event_ids": ["assistant-event"],
        },
        event_lookup,
    )

    assert candidate is None


def test_observer_apply_pending_writes_safe_memory_event(monkeypatch):
    _install_proposal_stubs()
    monkeypatch.setenv("ELLA_OBSERVER_ADMIN_TOKEN", "observer-token")
    sys.modules.pop("ella.routers.observer", None)
    sys.modules.pop("ella.services.observer_apply", None)
    router_module = importlib.import_module("ella.routers.observer")
    canonical_module = importlib.import_module("ella.routers.canonical_events")
    logs_module = importlib.import_module("ella.services.observer_logs")
    proposals_db = importlib.import_module("database.proposals")
    proposal_models = importlib.import_module("models.proposals")

    proposal = proposal_models.Proposal.from_claims(
        session_claims={
            "profile_uid": "user-1",
            "role": "system_observer",
            "external_provider": "ella_backend",
            "trace_id": "observer:test",
        },
        tool_name="ella_observer_fact_promotion",
        proposal_type="memory_note",
        payload={
            "title": "Spare glasses location",
            "description": "Spare glasses are in the blue backpack.",
            "source": "ella_observer_cron",
            "confidence": 0.94,
            "requested_change": {"memory": "Spare glasses are in the blue backpack."},
            "target": {"canonical_identity": "plato"},
        },
        proposal_id="proposal-safe-memory",
    )
    proposals_db.list_proposals.return_value = [proposal]

    event_store = canonical_module.InMemoryCanonicalEventStore()
    log_store = logs_module.InMemoryObserverRunLogStore()
    app = FastAPI()
    app.include_router(router_module.create_observer_router(event_store=event_store, log_store=log_store))
    client = TestClient(app)

    response = client.post(
        "/v1/ella/observer/apply-pending",
        headers={"X-Ella-Observer-Token": "observer-token"},
        json={"uid": "user-1", "dry_run": False, "min_confidence": 0.9},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied_count"] == 1
    assert body["decisions"][0]["action"] == "applied"
    proposals_db.update_proposal_status.assert_called_once()

    events = asyncio.run(event_store.timeline(uid="user-1", since=None, limit=10, channels=["observer_memory"]))
    assert len(events) == 1
    assert events[0]["channel"] == "observer_memory"
    assert "Spare glasses are in the blue backpack" in events[0]["text"]


def test_observer_apply_pending_accepts_trusted_mcp_memory_proposal(monkeypatch):
    _install_proposal_stubs()
    monkeypatch.setenv("ELLA_OBSERVER_ADMIN_TOKEN", "observer-token")
    sys.modules.pop("ella.routers.observer", None)
    sys.modules.pop("ella.services.observer_apply", None)
    router_module = importlib.import_module("ella.routers.observer")
    canonical_module = importlib.import_module("ella.routers.canonical_events")
    logs_module = importlib.import_module("ella.services.observer_logs")
    proposals_db = importlib.import_module("database.proposals")
    proposal_models = importlib.import_module("models.proposals")

    proposal = proposal_models.Proposal.from_claims(
        session_claims={
            "profile_uid": "user-1",
            "role": "self",
            "external_provider": "static_bearer",
            "trace_id": "mcp:test",
        },
        tool_name="companion_propose_change",
        proposal_type="memory_note",
        payload={
            "title": "MCP memory test",
            "description": "Remember the MCP test phrase copper sailboat.",
            "source": "plato_mcp",
            "requested_change": {"memory": "The MCP test phrase is copper sailboat."},
            "target": {"canonical_identity": "plato"},
        },
        proposal_id="proposal-mcp-memory",
    )
    proposals_db.list_proposals.return_value = [proposal]

    event_store = canonical_module.InMemoryCanonicalEventStore()
    log_store = logs_module.InMemoryObserverRunLogStore()
    app = FastAPI()
    app.include_router(router_module.create_observer_router(event_store=event_store, log_store=log_store))
    client = TestClient(app)

    response = client.post(
        "/v1/ella/observer/apply-pending",
        headers={"X-Ella-Observer-Token": "observer-token"},
        json={"uid": "user-1", "dry_run": False, "min_confidence": 0.9},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied_count"] == 1
    assert body["decisions"][0]["action"] == "applied"
    assert body["decisions"][0]["confidence"] == 0.92

    events = asyncio.run(event_store.timeline(uid="user-1", since=None, limit=10, channels=["observer_memory"]))
    assert len(events) == 1
    assert events[0]["metadata"]["proposal_source"] == "plato_mcp"
    assert "copper sailboat" in events[0]["text"]


def test_observer_log_store_keeps_datetimes_for_postgres(monkeypatch):
    service = _load_observer_service()
    sys.modules.pop("ella.services.observer_logs", None)
    logs_module = importlib.import_module("ella.services.observer_logs")

    log = service.run_observer(profile_uid="user-1", dry_run=True, events=[_event()], run_id="run-python-dates")
    data = logs_module._log_to_python(log)

    assert hasattr(data["started_at"], "isoformat")
    assert hasattr(data["completed_at"], "isoformat")
    assert isinstance(service.observer_log_to_dict(log)["started_at"], str)
