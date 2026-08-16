import asyncio
import importlib.util
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("database.vector_db", MagicMock())
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("utils.other.endpoints", MagicMock())
sys.modules.setdefault(
    "ella.routers.canonical_events",
    SimpleNamespace(
        CanonicalEventIn=lambda **kwargs: SimpleNamespace(**kwargs),
        PostgresCanonicalEventStore=MagicMock,
    ),
)
sys.modules.setdefault(
    "utils.conversations.vector",
    MagicMock(refresh_structured_summary_vector=MagicMock()),
)
sys.modules.setdefault(
    "utils.conversations.generic_summary",
    MagicMock(generate_stock_conversation_summary=MagicMock()),
)
sys.modules.pop("ella.config", None)

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_corrections_path = _backend_path / "ella" / "routers" / "corrections.py"
_corrections_spec = importlib.util.spec_from_file_location("ella_corrections_test_module", _corrections_path)
corrections = importlib.util.module_from_spec(_corrections_spec)
assert _corrections_spec is not None and _corrections_spec.loader is not None
_corrections_spec.loader.exec_module(corrections)
corrections.ELLA_CONFIG = SimpleNamespace(n8n_base_url="https://n8n.test")
_original_require_current_ai_consent = corrections.require_current_ai_consent
from ella.services import summary_recovery, summary_writeback


@pytest.fixture(autouse=True)
def _disable_external_observer_side_effects(monkeypatch):
    async def noop_emit(**kwargs):
        return None

    async def noop_propagate(**kwargs):
        return None

    async def passthrough_provider_config(uid, config):
        del uid
        return config

    def claim_initial(**kwargs):
        corrections._update_conversation_correction_state(
            kwargs["uid"],
            kwargs["conversation_id"],
            {**kwargs["bootstrap_update"], "correction_state": kwargs["correction_state"]},
        )
        corrections._persist_correction_audit(
            kwargs["uid"],
            kwargs["conversation_id"],
            kwargs["correction_id"],
            kwargs["audit_payload"],
        )
        return {"outcome": "created", "audit": kwargs["audit_payload"]}

    def record_failed(**kwargs):
        corrections._persist_correction_audit(
            kwargs["uid"], kwargs["conversation_id"], kwargs["correction_id"], kwargs["audit_update"]
        )
        corrections._update_conversation_correction_state(
            kwargs["uid"],
            kwargs["conversation_id"],
            {"correction_state": kwargs["correction_state"]},
        )
        return "recorded"

    def finish_canonical(**kwargs):
        corrections._persist_correction_audit(
            kwargs["uid"], kwargs["conversation_id"], kwargs["correction_id"], kwargs["audit_update"]
        )
        return "finished"

    monkeypatch.setattr(corrections, "_emit_canonical_correction_event", noop_emit)
    monkeypatch.setattr(corrections, "_run_correction_propagation_for_submission", noop_propagate)
    monkeypatch.setattr(corrections, "summary_provider_config_for_uid", passthrough_provider_config)
    monkeypatch.setattr(corrections, "require_current_ai_consent", lambda uid: uid)
    monkeypatch.setattr(corrections, "_claim_initial_correction_submission", claim_initial)
    monkeypatch.setattr(corrections, "_record_failed_correction_attempt", record_failed)
    monkeypatch.setattr(corrections, "_finish_canonical_reconciliation", finish_canonical)
    monkeypatch.setattr(corrections, "_start_correction_retry_attempt", lambda **kwargs: "started")
    monkeypatch.setattr(corrections, "_claim_downstream_stage", lambda **kwargs: "claimed")
    monkeypatch.setattr(corrections, "_complete_downstream_stage", lambda **kwargs: "completed")
    monkeypatch.setattr(summary_recovery, "_conversation_vector_present", lambda uid, cid: False)


def _conversation():
    return {
        "transcript_segments": [
            {"is_user": True, "text": "The podcast said memory can be tricky."},
            {"speaker": "Plato", "text": "That was the TV, not me."},
        ],
        "structured": {
            "title": "Memory concern",
            "overview": "[Ella] Plato discussed memory issues.",
            "emoji": "🧠",
            "category": "health",
        },
    }


def _retry_conversation(status="processing", request_id="84eb13fa-31d9-40ba-a742-c4de4757dc10"):
    return {
        "id": "conversation-1",
        "created_at": "2026-07-20T08:00:00+00:00",
        "started_at": "2026-07-20T08:00:00+00:00",
        "finished_at": "2026-07-20T08:30:00+00:00",
        "structured": {
            "title": "",
            "overview": "",
            "emoji": "brain",
            "category": "other",
        },
        "transcript_segments": [
            {
                "is_user": True,
                "speaker": "SPEAKER_00",
                "text": "Important retained transcript.",
                "start": 0,
                "end": 10,
            }
        ],
        "status": status,
        "discarded": False,
        "processing_error": "conversation_summary_failed" if status == "failed" else None,
        "processing_retry_id": request_id,
    }


def _retry_api_client():
    app = FastAPI()
    app.include_router(corrections.router)
    app.dependency_overrides[corrections.get_exact_firebase_uid] = lambda: "authenticated-user"
    return app, TestClient(app)


async def _async_event(events, value):
    events.append(value)


async def _async_raise(error):
    raise error


async def _async_event_result(events, value, result):
    events.append(value)
    return result


def test_submit_correction_accepts_ios_payload_and_queues(monkeypatch):
    audits = []
    events = []
    submitted = {}
    conversation_updates = []
    proposals = []

    monkeypatch.setattr(corrections, "N8N_CORRECTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(
        corrections.conversations_db,
        "bootstrap_summary_versioning_update",
        lambda conversation: {"summary_versions": [{"id": "legacy-v1"}], "active_summary_version_id": "legacy-v1"},
    )
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(
            {
                "uid": uid,
                "conversation_id": conversation_id,
                "correction_id": correction_id,
                "payload": payload,
            }
        ),
    )
    monkeypatch.setattr(
        corrections,
        "_append_correction_event",
        lambda uid, conversation_id, correction_id, event: events.append(event),
    )
    monkeypatch.setattr(
        corrections,
        "_create_summary_correction_proposal",
        lambda **kwargs: proposals.append(kwargs) or "proposal-123",
    )

    async def fake_submit_to_n8n(**kwargs):
        submitted.update(kwargs)
        return {"n8n_webhook": "conversation-correction", "n8n_status_code": 200}

    monkeypatch.setattr(corrections, "_submit_correction_to_n8n", fake_submit_to_n8n)

    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(
                correction_text="This was background TV audio, not a real memory concern.",
                source="ios",
                summary_context={
                    "title": "Memory concern",
                    "overview": "[Ella] Plato discussed memory issues.",
                    "app_summary": "The app summary was too clinical.",
                },
            ),
            background_tasks=None,
            uid="user-123",
        )
    )

    assert result.status == "queued"
    assert result.queued is True
    assert result.proposal_id is None
    assert result.conversation_id == "conv-123"
    assert result.correction_id
    assert result.trace_id.startswith("correction:conv-123:")
    assert audits[0]["uid"] == "user-123"
    assert audits[0]["payload"]["status"] == "queued"
    assert audits[0]["payload"]["category"] == "media"
    assert audits[0]["payload"]["correction_text"] == "This was background TV audio, not a real memory concern."
    assert submitted["uid"] == "user-123"
    assert submitted["conversation_id"] == "conv-123"
    assert "The podcast said memory can be tricky." in submitted["transcript"]
    assert submitted["request"].summary_context.app_summary == "The app summary was too clinical."
    assert proposals == []
    assert not any(event["stage"] == "proposal_created" for event in events)
    assert events[-1]["stage"] == "queued"
    assert conversation_updates[0]["correction_state"]["status"] == "queued"
    assert conversation_updates[0]["active_summary_version_id"] == "legacy-v1"
    assert conversation_updates[-1]["correction_state"]["status"] == "queued"
    assert conversation_updates[-1]["correction_state"]["active_summary_version_id"] == "legacy-v1"


def test_submit_correction_uses_authenticated_uid_for_ownership(monkeypatch):
    calls = []
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: calls.append((uid, conversation_id)) or None,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            corrections.submit_conversation_correction(
                "conv-123",
                corrections.ConversationCorrectionRequest(correction_text="Wrong person."),
                background_tasks=None,
                uid="authenticated-user",
            )
        )

    assert excinfo.value.status_code == 404
    assert calls == [("authenticated-user", "conv-123")]


def test_correction_receipt_exposes_old_and_new_summary_and_real_propagation_count(monkeypatch):
    conversation = {
        "summary_versions": [
            {
                "id": "before-v1",
                "title": "Coffee with Margaret",
                "overview": "Margaret came by.",
                "emoji": "☕",
                "category": "other",
            },
            {
                "id": "after-v2",
                "based_on_version_id": "before-v1",
                "correction_id": "corr-1",
                "title": "Coffee with Rose",
                "overview": "Rose came by.",
                "emoji": "☕",
                "category": "other",
            },
        ],
        "active_summary_version_id": "after-v2",
        "correction_state": {"correction_id": "corr-1", "status": "applied"},
    }

    class AuditRef:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "uid": "uid-1",
                    "conversation_id": "conv-1",
                    "correction_id": "corr-1",
                    "status": "applied",
                    "applied_at": "2026-07-23T17:00:00+00:00",
                },
            )

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections, "_correction_propagation_counts", lambda uid, conversation_id, correction_id: (1, 0, "known")
    )

    receipt = corrections._correction_receipt(
        uid="uid-1",
        conversation_id="conv-1",
        correction_id="corr-1",
    )

    assert receipt.before.title == "Coffee with Margaret"
    assert receipt.after.title == "Coffee with Rose"
    assert receipt.before_version_id == "before-v1"
    assert receipt.after_version_id == "after-v2"
    assert receipt.active_version_id == "after-v2"
    assert receipt.undo_version_id is None
    assert receipt.propagation_status == "known"
    assert receipt.propagation_applied_count == 1
    assert receipt.propagation_reverted_count == 0


def test_undo_restores_source_summary_and_reverts_actual_propagations(monkeypatch):
    conversation = {
        "summary_versions": [
            {
                "id": "before-v1",
                "title": "Coffee with Margaret",
                "overview": "Margaret came by.",
                "emoji": "☕",
                "category": "other",
            },
            {
                "id": "after-v2",
                "based_on_version_id": "before-v1",
                "correction_id": "corr-1",
                "title": "Coffee with Rose",
                "overview": "Rose came by.",
                "emoji": "☕",
                "category": "other",
            },
        ],
        "active_summary_version_id": "after-v2",
        "correction_state": {"correction_id": "corr-1", "status": "applied", "source": "ios"},
    }
    applied = {}
    audits = []
    updates = []

    class AuditRef:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "uid": "uid-1",
                    "conversation_id": "conv-1",
                    "correction_id": "corr-1",
                    "status": "applied",
                    "canonical_publication_state": "completed",
                    "canonical_publication_confirmed": True,
                    "downstream_work": {
                        "proposal": {"required": True, "status": "completed"},
                        "canonical_event": {"required": True, "status": "completed"},
                        "propagation": {"required": False, "status": "completed"},
                    },
                },
            )

    async def fake_apply(**kwargs):
        applied.update(kwargs)
        return {"status": "ok", "active_summary_version_id": "undo-v3", "canonical_confirmed": True}

    expected = corrections.ConversationCorrectionReceiptResponse(
        correction_id="corr-1",
        conversation_id="conv-1",
        status="undone",
        before=corrections.CorrectionSummarySnapshot(title="Coffee with Margaret"),
        after=corrections.CorrectionSummarySnapshot(title="Coffee with Rose"),
        propagation_applied_count=1,
        propagation_reverted_count=1,
    )

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update: updates.append(update),
    )
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(corrections, "apply_summary_update", fake_apply)
    monkeypatch.setattr(corrections, "_prepare_applied_propagation_rollbacks", lambda uid, cid, correction_id: [])

    async def fake_revert(uid, cid, correction_id, *, rollback_plan=None):
        assert rollback_plan == []
        return 1

    monkeypatch.setattr(corrections, "_revert_applied_propagations", fake_revert)
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )
    monkeypatch.setattr(corrections, "_correction_receipt", lambda **kwargs: expected)

    def finalize(**kwargs):
        audits.append(
            {
                "status": "undone",
                "propagation_reverted_count": kwargs["reverted_count"],
                "undo_version_id": kwargs["undo_version_id"],
            }
        )
        updates.append(
            {
                "correction_state": {
                    "correction_id": kwargs["correction_id"],
                    "status": "undone",
                    "active_summary_version_id": kwargs["undo_version_id"],
                }
            }
        )
        return "finalized"

    monkeypatch.setattr(corrections, "_finalize_correction_undo", finalize)

    receipt = asyncio.run(
        corrections.undo_conversation_correction(
            "conv-1",
            "corr-1",
            uid="uid-1",
        )
    )

    assert applied["summary"]["title"] == "Coffee with Margaret"
    assert applied["active_summary_version_id"] == "after-v2"
    assert applied["require_based_on_match"] is True
    assert audits[-1]["status"] == "undone"
    assert audits[-1]["propagation_reverted_count"] == 1
    assert updates[-1]["correction_state"]["status"] == "undone"
    assert receipt.status == "undone"


def test_propagation_undo_restores_each_related_summary_from_rollback_snapshot(monkeypatch):
    applies = []
    run_updates = []
    related = {
        "active_summary_version_id": "related-v2",
        "summary_versions": [
            {"id": "related-v1", "kind": "observer_enriched", "is_active": False},
            {"id": "related-v2", "kind": "correction_propagation", "is_active": True},
        ],
    }

    class RunReference:
        def set(self, payload, merge=False):
            run_updates.append((payload, merge))

    class RunSnapshot:
        reference = RunReference()

        def to_dict(self):
            return {
                "auto_applied_count": 1,
                "decisions": [
                    {
                        "action": "auto_applied",
                        "conversation_id": "related-1",
                        "applied_summary_version_id": "related-v2",
                        "rollback_ref": {
                            "active_summary_version_id": "related-v1",
                            "structured": {
                                "title": "Coffee with Margaret",
                                "overview": "Margaret came by.",
                                "emoji": "☕",
                                "category": "other",
                            },
                        },
                    }
                ],
            }

    class RunCollection:
        def stream(self):
            return [RunSnapshot()]

    class AuditRef:
        def collection(self, name):
            assert name == "propagation_runs"
            return RunCollection()

    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: related,
    )

    async def fake_apply(**kwargs):
        applies.append(kwargs)
        return {"status": "ok", "active_summary_version_id": "related-undo-v3"}

    monkeypatch.setattr(corrections, "apply_summary_update", fake_apply)

    plan = corrections._prepare_applied_propagation_rollbacks("uid-1", "conv-1", "corr-1")
    reverted = asyncio.run(
        corrections._revert_applied_propagations(
            "uid-1",
            "conv-1",
            "corr-1",
            rollback_plan=plan,
        )
    )

    assert reverted == 1
    assert applies[0]["conversation_id"] == "related-1"
    assert applies[0]["active_summary_version_id"] == "related-v2"
    assert applies[0]["summary"]["title"] == "Coffee with Margaret"
    assert applies[0]["summary_kind"] == "correction_propagation_undo"
    assert applies[0]["require_based_on_match"] is True
    assert run_updates[0][0]["reverted_count"] == 1
    assert run_updates[0][0]["decisions"][0]["reverted_summary_version_id"] == "related-undo-v3"
    assert run_updates[0][1] is True


def test_correction_receipt_exposes_unknown_propagation_state_and_ignores_other_correction_state(monkeypatch):
    conversation = {
        "summary_versions": [
            {"id": "before-v1", "title": "Before"},
            {
                "id": "after-v2",
                "based_on_version_id": "before-v1",
                "correction_id": "corr-1",
                "title": "After",
            },
        ],
        "active_summary_version_id": "after-v2",
        "correction_state": {"correction_id": "different-correction", "status": "undone"},
    }

    class AuditRef:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "uid": "uid-1",
                    "conversation_id": "conv-1",
                    "correction_id": "corr-1",
                    "status": "processing",
                    "applied_at": "2026-07-23T17:00:00+00:00",
                },
            )

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections,
        "_correction_propagation_counts",
        lambda uid, conversation_id, correction_id: (None, None, "unknown"),
    )

    receipt = corrections._correction_receipt(
        uid="uid-1",
        conversation_id="conv-1",
        correction_id="corr-1",
    )

    assert receipt.status == "processing"
    assert receipt.propagation_status == "unknown"
    assert receipt.propagation_applied_count is None
    assert receipt.propagation_reverted_count is None


def test_receipt_and_undo_reject_locked_conversation_before_audit_access(monkeypatch):
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {"is_locked": True},
    )
    monkeypatch.setattr(
        corrections,
        "_audit_ref",
        lambda *args: pytest.fail("locked conversations must not expose correction audit data"),
    )

    with pytest.raises(HTTPException) as receipt_error:
        corrections._correction_receipt(
            uid="uid-1",
            conversation_id="conv-locked",
            correction_id="corr-1",
        )
    with pytest.raises(HTTPException) as undo_error:
        asyncio.run(
            corrections.undo_conversation_correction(
                "conv-locked",
                "corr-1",
                uid="uid-1",
            )
        )

    assert receipt_error.value.status_code == 402
    assert undo_error.value.status_code == 402


def test_propagation_undo_preflight_rejects_stale_related_version_before_any_write(monkeypatch):
    run = {
        "auto_applied_count": 1,
        "decisions": [
            {
                "action": "auto_applied",
                "conversation_id": "related-1",
                "applied_summary_version_id": "related-v2",
                "rollback_ref": {
                    "active_summary_version_id": "related-v1",
                    "structured": {"title": "Before", "overview": "Before", "category": "other"},
                },
            }
        ],
    }

    class RunSnapshot:
        reference = SimpleNamespace(set=lambda *args, **kwargs: pytest.fail("preflight must not write"))

        def to_dict(self):
            return run

    class AuditRef:
        def collection(self, name):
            return SimpleNamespace(stream=lambda: [RunSnapshot()])

    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {"active_summary_version_id": "newer-v3"},
    )

    with pytest.raises(HTTPException) as excinfo:
        corrections._prepare_applied_propagation_rollbacks("uid-1", "source-1", "corr-1")

    assert excinfo.value.status_code == 409
    assert "newer related memory" in excinfo.value.detail


def test_undo_preflights_stale_related_target_before_source_or_audit_mutation(monkeypatch):
    source = {
        "summary_versions": [
            {"id": "before-v1", "title": "Before"},
            {
                "id": "after-v2",
                "based_on_version_id": "before-v1",
                "correction_id": "corr-1",
                "title": "After",
            },
        ],
        "active_summary_version_id": "after-v2",
    }
    run = {
        "auto_applied_count": 1,
        "decisions": [
            {
                "action": "auto_applied",
                "conversation_id": "related-1",
                "applied_summary_version_id": "related-v2",
                "rollback_ref": {
                    "active_summary_version_id": "related-v1",
                    "structured": {"title": "Before", "overview": "Before", "category": "other"},
                },
            }
        ],
    }

    class RunSnapshot:
        reference = SimpleNamespace(set=lambda *args, **kwargs: pytest.fail("preflight must not write"))

        def to_dict(self):
            return run

    class AuditRef:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "uid": "uid-1",
                    "conversation_id": "source-1",
                    "correction_id": "corr-1",
                    "status": "applied",
                    "canonical_publication_state": "completed",
                    "canonical_publication_confirmed": True,
                    "downstream_work": {
                        "proposal": {"required": True, "status": "completed"},
                        "canonical_event": {"required": True, "status": "completed"},
                        "propagation": {"required": False, "status": "completed"},
                    },
                },
            )

        def collection(self, name):
            return SimpleNamespace(stream=lambda: [RunSnapshot()])

    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: (
            source if conversation_id == "source-1" else {"active_summary_version_id": "newer-v3"}
        ),
    )
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda *args, **kwargs: pytest.fail("failed preflight must not mutate the audit"),
    )

    async def fail_apply(**kwargs):
        pytest.fail("failed preflight must not mutate the source")

    monkeypatch.setattr(corrections, "apply_summary_update", fail_apply)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            corrections.undo_conversation_correction(
                "source-1",
                "corr-1",
                uid="uid-1",
            )
        )

    assert excinfo.value.status_code == 409


def test_propagation_undo_resumes_after_one_of_multiple_writes_fails(monkeypatch):
    run = {
        "auto_applied_count": 2,
        "decisions": [
            {
                "action": "auto_applied",
                "conversation_id": "related-1",
                "applied_summary_version_id": "related-1-v2",
                "rollback_ref": {
                    "active_summary_version_id": "related-1-v1",
                    "structured": {"title": "Related one before", "overview": "Before", "category": "other"},
                },
            },
            {
                "action": "auto_applied",
                "conversation_id": "related-2",
                "applied_summary_version_id": "related-2-v2",
                "rollback_ref": {
                    "active_summary_version_id": "related-2-v1",
                    "structured": {"title": "Related two before", "overview": "Before", "category": "other"},
                },
            },
        ],
    }
    related = {
        "related-1": {
            "active_summary_version_id": "related-1-v2",
            "summary_versions": [{"id": "related-1-v2", "kind": "correction_propagation"}],
        },
        "related-2": {
            "active_summary_version_id": "related-2-v2",
            "summary_versions": [{"id": "related-2-v2", "kind": "correction_propagation"}],
        },
    }
    run_writes = []

    class RunReference:
        def set(self, payload, merge=False):
            assert merge is True
            run.update(payload)
            run_writes.append(payload)

    class RunSnapshot:
        reference = RunReference()

        def to_dict(self):
            return run

    class AuditRef:
        def collection(self, name):
            return SimpleNamespace(stream=lambda: [RunSnapshot()])

    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: related[conversation_id],
    )
    calls = []
    fail_second = {"enabled": True}

    async def fake_apply(**kwargs):
        related_id = kwargs["conversation_id"]
        calls.append(related_id)
        if related_id == "related-2" and fail_second["enabled"]:
            raise RuntimeError("simulated second write failure")
        undo_id = f"{related_id}-undo"
        related[related_id]["active_summary_version_id"] = undo_id
        related[related_id]["summary_versions"].append(
            {
                "id": undo_id,
                "kind": "correction_propagation_undo",
                "based_on_version_id": kwargs["active_summary_version_id"],
            }
        )
        return {"status": "ok", "active_summary_version_id": undo_id}

    monkeypatch.setattr(corrections, "apply_summary_update", fake_apply)

    first_plan = corrections._prepare_applied_propagation_rollbacks("uid-1", "source-1", "corr-1")
    with pytest.raises(RuntimeError, match="second write failure"):
        asyncio.run(
            corrections._revert_applied_propagations(
                "uid-1",
                "source-1",
                "corr-1",
                rollback_plan=first_plan,
            )
        )

    assert run["decisions"][0]["reverted_summary_version_id"] == "related-1-undo"
    assert "reverted_summary_version_id" not in run["decisions"][1]
    assert run["reverted_count"] == 1

    fail_second["enabled"] = False
    retry_plan = corrections._prepare_applied_propagation_rollbacks("uid-1", "source-1", "corr-1")
    reverted = asyncio.run(
        corrections._revert_applied_propagations(
            "uid-1",
            "source-1",
            "corr-1",
            rollback_plan=retry_plan,
        )
    )

    assert reverted == 2
    assert calls.count("related-1") == 1
    assert calls.count("related-2") == 2
    assert run["reverted_count"] == 2
    assert run["decisions"][1]["reverted_summary_version_id"] == "related-2-undo"
    assert len(run_writes) == 3


def test_repeated_undo_repairs_mismatched_terminal_pair_without_appending_version(monkeypatch):
    conversation = {
        "summary_versions": [
            {"id": "before-v1", "title": "Before"},
            {
                "id": "after-v2",
                "based_on_version_id": "before-v1",
                "correction_id": "corr-1",
                "title": "After",
            },
            {
                "id": "undo-v3",
                "based_on_version_id": "after-v2",
                "kind": "correction_undo",
                "title": "Before",
            },
        ],
        "active_summary_version_id": "undo-v3",
        "correction_state": {"correction_id": "corr-1", "status": "applied"},
    }

    class AuditRef:
        def get(self):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "uid": "uid-1",
                    "conversation_id": "conv-1",
                    "correction_id": "corr-1",
                    "status": "undone",
                    "applied_at": "2026-07-23T17:00:00+00:00",
                    "undone_at": "2026-07-23T18:00:00+00:00",
                    "undo_version_id": "undo-v3",
                },
            )

        def collection(self, name):
            return SimpleNamespace(stream=lambda: [])

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, conversation_id, correction_id: AuditRef())
    monkeypatch.setattr(
        corrections,
        "apply_summary_update",
        lambda **kwargs: pytest.fail("repeated undo must not append another version"),
    )
    repairs = []
    monkeypatch.setattr(
        corrections,
        "_finalize_correction_undo",
        lambda **kwargs: repairs.append(kwargs) or "finalized",
    )

    receipt = asyncio.run(
        corrections.undo_conversation_correction(
            "conv-1",
            "corr-1",
            uid="uid-1",
        )
    )

    assert receipt.status == "undone"
    assert receipt.before_version_id == "before-v1"
    assert receipt.after_version_id == "after-v2"
    assert receipt.active_version_id == "undo-v3"
    assert receipt.undo_version_id == "undo-v3"
    assert repairs[0]["undo_version_id"] == "undo-v3"


def test_submit_correction_rejects_locked_conversation(monkeypatch):
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {"is_locked": True},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            corrections.submit_conversation_correction(
                "conv-locked",
                corrections.ConversationCorrectionRequest(correction_text="Please fix this."),
                background_tasks=None,
                uid="user-123",
            )
        )

    assert excinfo.value.status_code == 402


def test_submit_correction_persists_n8n_failure_trace_and_still_returns_202(monkeypatch):
    audits = []
    events = []
    conversation_updates = []

    monkeypatch.setattr(corrections, "N8N_CORRECTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(corrections.conversations_db, "bootstrap_summary_versioning_update", lambda conversation: {})
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )
    monkeypatch.setattr(
        corrections,
        "_append_correction_event",
        lambda uid, conversation_id, correction_id, event: events.append(event),
    )
    monkeypatch.setattr(corrections, "_create_summary_correction_proposal", lambda **kwargs: "proposal-123")

    async def failing_submit_to_n8n(**kwargs):
        raise RuntimeError("n8n offline")

    monkeypatch.setattr(corrections, "_submit_correction_to_n8n", failing_submit_to_n8n)

    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(correction_text="Please correct the summary."),
            background_tasks=None,
            uid="user-123",
        )
    )

    assert result.status == "queue_failed"
    assert result.queued is False
    assert result.proposal_id is None
    assert audits[-1]["status"] == "queue_failed"
    assert audits[-1]["failure_code"] == "runtimeerror"
    assert events[-1]["stage"] == "queue_failed"
    assert events[-1]["status"] == "error"
    assert conversation_updates[0]["correction_state"]["status"] == "queued"
    assert conversation_updates[-1]["correction_state"]["status"] == "queue_failed"
    assert conversation_updates[-1]["correction_state"]["pending"] is False


def test_submit_correction_skips_n8n_fallback_by_default_when_direct_apply_fails(monkeypatch):
    audits = []
    events = []
    conversation_updates = []

    monkeypatch.setattr(corrections, "N8N_CORRECTION_FALLBACK_ENABLED", False)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(corrections.conversations_db, "bootstrap_summary_versioning_update", lambda conversation: {})
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )
    monkeypatch.setattr(
        corrections,
        "_append_correction_event",
        lambda uid, conversation_id, correction_id, event: events.append(event),
    )
    monkeypatch.setattr(corrections, "_create_summary_correction_proposal", lambda **kwargs: "proposal-123")

    async def failing_generate(**kwargs):
        raise RuntimeError("model unavailable")

    async def fail_if_called(**kwargs):
        raise AssertionError("OpenClaw-era n8n correction fallback must stay disabled")

    monkeypatch.setattr(corrections, "_generate_corrected_summary", failing_generate)
    monkeypatch.setattr(corrections, "_submit_correction_to_n8n", fail_if_called)

    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(correction_text="Please correct the summary."),
            background_tasks=None,
            uid="user-123",
        )
    )

    assert result.status == "direct_apply_failed"
    assert result.queued is False
    assert audits[-1]["status"] == "direct_apply_failed"
    assert events[-1]["stage"] == "direct_apply_failed"
    assert events[-1]["n8n_fallback_enabled"] is False
    assert conversation_updates[-1]["correction_state"]["status"] == "direct_apply_failed"
    assert conversation_updates[-1]["correction_state"]["pending"] is False


def test_submit_correction_directly_applies_when_model_path_succeeds(monkeypatch):
    audits = []
    events = []
    conversation_updates = []
    submitted = {}

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(
        corrections.conversations_db,
        "bootstrap_summary_versioning_update",
        lambda conversation: {"summary_versions": [{"id": "legacy-v1"}], "active_summary_version_id": "legacy-v1"},
    )
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )
    monkeypatch.setattr(
        corrections,
        "_append_correction_event",
        lambda uid, conversation_id, correction_id, event: events.append(event),
    )
    monkeypatch.setattr(corrections, "_create_summary_correction_proposal", lambda **kwargs: "proposal-123")

    async def fake_generate(**kwargs):
        return {
            "title": "TV Audio, Not Memory Concern",
            "overview": "[Ella] This was background TV audio, not a real memory concern.",
            "emoji": "📺",
            "category": "other",
            "ella_tags": ["omi", "correction", "media"],
            "ella_signal": {"salience": "low", "noise_level": "medium", "contains_media": True},
        }

    async def fake_apply(**kwargs):
        submitted.update(kwargs)
        return {"status": "ok", "active_summary_version_id": "corrected-v1", "canonical_confirmed": True}

    async def fake_submit_to_n8n(**kwargs):
        raise AssertionError("n8n should not run when direct correction apply succeeds")

    monkeypatch.setattr(corrections, "_generate_corrected_summary", fake_generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", fake_apply)
    monkeypatch.setattr(corrections, "_submit_correction_to_n8n", fake_submit_to_n8n)

    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(
                correction_text="This was background TV audio, not a real memory concern.",
                source="ios",
            ),
            background_tasks=None,
            uid="user-123",
        )
    )

    assert result.status == "applied"
    assert result.queued is False
    assert result.proposal_id == "proposal-123"
    assert submitted["uid"] == "user-123"
    assert submitted["conversation_id"] == "conv-123"
    assert submitted["active_summary_version_id"] == "legacy-v1"
    assert submitted["corrected"]["overview"].startswith("[Ella] ")
    applied_audit = next(payload for payload in audits if payload.get("status") == "applied")
    assert applied_audit["applied_at"] == applied_audit["updated_at"]
    assert applied_audit["direct_apply_result"]["active_summary_version_id"] == "corrected-v1"
    assert [event["stage"] for event in events].index("proposal_created") < [event["stage"] for event in events].index(
        "direct_apply_succeeded"
    )
    assert conversation_updates[0]["correction_state"]["status"] == "queued"


def test_apply_corrected_summary_uses_shared_direct_writeback_service(monkeypatch):
    captured = {}

    async def fake_apply(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "active_summary_version_id": "corrected-v1", "canonical_confirmed": True}

    monkeypatch.setattr(corrections, "apply_summary_update", fake_apply)
    corrected = {
        "title": "Corrected",
        "overview": "[Ella] Corrected summary.",
        "emoji": "brain",
        "category": "other",
        "ella_tags": ["omi", "correction"],
        "ella_signal": {},
    }

    result = asyncio.run(
        corrections._apply_corrected_summary(
            uid="user-123",
            conversation_id="conv-123",
            correction_id="corr-123",
            trace_id="correction:conv-123:corr-123",
            active_summary_version_id="legacy-v1",
            corrected=corrected,
        )
    )

    assert result["active_summary_version_id"] == "corrected-v1"
    canonical_egress_guard = captured.pop("canonical_egress_guard")
    assert captured.pop("canonical_egress_completion") is None
    assert captured.pop("canonical_timeout_provider") is None
    assert callable(canonical_egress_guard)
    assert callable(captured.pop("source_mutation_guard"))
    assert captured == {
        "uid": "user-123",
        "conversation_id": "conv-123",
        "trace_id": "correction:conv-123:corr-123",
        "active_summary_version_id": "legacy-v1",
        "summary": corrected,
        "summary_kind": "corrected_enriched",
        "correction_id": "corr-123",
        "require_based_on_match": True,
        "require_canonical": True,
        "correction_attempt_token": None,
        "correction_source_compare_and_set": None,
    }


def test_submit_correction_queues_background_direct_apply_without_waiting(monkeypatch):
    audits = []
    events = []
    conversation_updates = []
    proposals = []
    generated = []

    class FakeBackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_APPLY_ENABLED", True)
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(corrections, "N8N_CORRECTION_FALLBACK_ENABLED", False)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(
        corrections.conversations_db,
        "bootstrap_summary_versioning_update",
        lambda conversation: {"summary_versions": [{"id": "legacy-v1"}], "active_summary_version_id": "legacy-v1"},
    )
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audits.append(payload),
    )
    monkeypatch.setattr(
        corrections,
        "_append_correction_event",
        lambda uid, conversation_id, correction_id, event: events.append(event),
    )
    monkeypatch.setattr(
        corrections,
        "_create_summary_correction_proposal",
        lambda **kwargs: proposals.append(kwargs) or "proposal-123",
    )

    async def fake_generate(**kwargs):
        generated.append(kwargs)
        return {
            "title": "Corrected",
            "overview": "[Ella] Corrected in background.",
            "emoji": "🪽",
            "category": "other",
            "ella_tags": ["omi", "correction"],
            "ella_signal": {},
        }

    async def fake_apply(**kwargs):
        return {"status": "ok", "active_summary_version_id": "corrected-v1", "canonical_confirmed": True}

    monkeypatch.setattr(corrections, "_generate_corrected_summary", fake_generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", fake_apply)
    monkeypatch.setattr(corrections, "_start_correction_retry_attempt", lambda **kwargs: "started")

    background_tasks = FakeBackgroundTasks()
    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(
                correction_text="This was Mei Xin speaking.",
                source="ios",
            ),
            background_tasks=background_tasks,
            uid="user-123",
        )
    )

    assert result.status == "queued"
    assert result.queued is True
    assert result.proposal_id is None
    assert len(background_tasks.tasks) == 1
    assert generated == []
    assert audits[-1]["status"] == "queued"
    assert audits[-1]["queue_result"] == {"mode": "background_direct_apply"}
    assert audits[-1]["retry_attempt_token"]
    assert audits[-1]["retry_lease_expires_at"]
    assert audits[-1]["events"][-1]["stage"] == "queued"
    assert events == []
    assert conversation_updates[-1]["correction_state"]["status"] == "queued"

    func, args, kwargs = background_tasks.tasks[0]
    assert func is corrections._run_direct_correction_apply
    assert kwargs["retry_attempt_token"] == audits[-1]["retry_attempt_token"]
    asyncio.run(func(*args, **kwargs))

    assert generated[0]["uid"] == "user-123"
    assert generated[0]["conversation_id"] == "conv-123"
    assert proposals[0]["conversation_id"] == "conv-123"
    assert any(payload.get("status") == "applied" for payload in audits)
    assert [event["stage"] for event in events].index("proposal_created") < [event["stage"] for event in events].index(
        "direct_apply_succeeded"
    )


def test_submit_correction_does_not_queue_observer_work_before_source_success(monkeypatch):
    emitted = {}
    events = []
    conversation_updates = []

    class FakeBackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    async def capture_emit(**kwargs):
        emitted.update(kwargs)

    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_APPLY_ENABLED", False)
    monkeypatch.setattr(corrections, "N8N_CORRECTION_FALLBACK_ENABLED", False)
    monkeypatch.setattr(corrections, "CORRECTION_PROPAGATION_ENABLED", True)
    monkeypatch.setattr(corrections, "CORRECTION_OBSERVER_WORK_ENABLED", True)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
    monkeypatch.setattr(corrections.conversations_db, "bootstrap_summary_versioning_update", lambda conversation: {})
    monkeypatch.setattr(
        corrections.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: conversation_updates.append(update_data),
    )
    monkeypatch.setattr(corrections, "_persist_correction_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(corrections, "_append_correction_event", lambda uid, cid, corr_id, event: events.append(event))
    monkeypatch.setattr(corrections, "_create_summary_correction_proposal", lambda **kwargs: "proposal-123")
    monkeypatch.setattr(corrections, "_emit_canonical_correction_event", capture_emit)

    background_tasks = FakeBackgroundTasks()
    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(correction_text="Actually this was background TV audio."),
            background_tasks=background_tasks,
            uid="user-123",
        )
    )

    assert result.status == "direct_apply_disabled"
    assert emitted == {}
    assert background_tasks.tasks == []
    assert not any(event["stage"] == "observer_work_queued" for event in events)


def test_correction_propagation_feature_flag_defaults_off():
    assert corrections.CORRECTION_PROPAGATION_ENABLED is False


def test_router_uses_custom_ella_namespace_only():
    paths = {route.path for route in corrections.router.routes}

    assert "/v1/ella/conversations/{conversation_id}/corrections" in paths
    assert "/v1/conversations/{conversation_id}/corrections" in paths


def test_correction_routes_use_deployed_exact_auth_and_current_consent_dependencies():
    route_dependencies = {
        (route.path, method): [dependency.call for dependency in route.dependant.dependencies]
        for route in corrections.router.routes
        for method in route.methods
    }
    submit_paths = {
        "/v1/ella/conversations/{conversation_id}/corrections",
        "/v1/conversations/{conversation_id}/corrections",
    }
    for path in submit_paths:
        assert route_dependencies[(path, "POST")] == [_original_require_current_ai_consent]

    exact_paths = {
        ("/v1/conversations/{conversation_id}/processing-retry-plan", "GET"),
        ("/v1/conversations/{conversation_id}/processing-retries", "POST"),
        ("/v1/ella/conversations/{conversation_id}/corrections/{correction_id}", "GET"),
        ("/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/retry", "POST"),
        ("/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo", "POST"),
    }
    for route_key in exact_paths:
        assert route_dependencies[route_key] == [corrections.get_exact_firebase_uid]

    source = _corrections_path.read_text(encoding="utf-8")
    assert "utils.other.endpoints" not in source
    assert "Depends(auth.get_current_user_uid)" not in source


def test_submit_correction_accepts_text_alias():
    request = corrections.ConversationCorrectionRequest(text="Please fix the doctor name.")

    assert request.correction_text == "Please fix the doctor name."


def test_build_correction_prompt_guides_identity_reprocessing():
    prompt = corrections._build_direct_correction_prompt(
        request=corrections.ConversationCorrectionRequest(
            correction_text="the team in this Trang script is Mei Xin a.k.a. Rain",
            source="ios",
        ),
        structured={
            "title": "Teen avoids meeting",
            "overview": "[Ella] Speaker 5 discussed a teen avoiding an in-person meeting.",
            "emoji": "🏫",
            "category": "education",
        },
        transcript="Speaker 5: She wants to email the teacher instead of meeting in person.",
        segment_count=1,
    )

    assert "Do not append or splice the correction text verbatim" in prompt
    assert '"Trang script" likely means "transcript"' in prompt
    assert '"team" may mean "teen"' in prompt
    assert "propagate that identity through all relevant references" in prompt
    assert "Avoid raw speaker labels such as \"Speaker 5\"" in prompt


def test_generate_corrected_summary_uses_hermes_api_with_scoped_session(monkeypatch):
    calls = []

    monkeypatch.setattr(corrections, "CORRECTION_PROVIDER", "hermes-api")
    monkeypatch.setattr(corrections, "HERMES_CORRECTION_API_URL", "https://hermes.test/v1/chat/completions")
    monkeypatch.setattr(corrections, "HERMES_CORRECTION_MODEL", "profile-model")
    monkeypatch.setenv("ELLA_CORRECTION_HERMES_API_KEY", "hermes-secret")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"title":"Mei Xin Emails Teacher",'
                                '"overview":"[Ella] Mei Xin, also called Rain, preferred emailing her teacher instead of an in-person meeting.",'
                                '"emoji":"🏫","category":"education"}'
                            )
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        corrections._generate_corrected_summary(
            uid="user/123",
            conversation_id="conv 123",
            correction_id="corr 123",
            trace_id="trace-123",
            request=corrections.ConversationCorrectionRequest(
                correction_text="the team in this Trang script is Mei Xin a.k.a. Rain",
                source="ios",
            ),
            structured={"title": "Teen meeting", "overview": "[Ella] Speaker 5 talked about the teen."},
            transcript="Speaker 5: She wants to email Miss Boyd.",
            segment_count=1,
        )
    )

    assert result["title"] == "Mei Xin Emails Teacher"
    assert "Mei Xin" in result["overview"]
    assert result["ella_tags"] == ["omi", "correction"]
    assert calls[0]["url"] == "https://hermes.test/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer hermes-secret"
    assert calls[0]["headers"]["X-Hermes-Session-Id"] == "correction:user-123:conv-123:corr-123"
    assert calls[0]["headers"]["X-Hermes-Session-Key"] == "ella:omi:user-123:canonical"
    assert calls[0]["headers"]["X-Trace-Id"] == "trace-123"
    assert calls[0]["json"]["model"] == "profile-model"
    assert "Speaker 5" in calls[0]["json"]["messages"][0]["content"]


def test_generate_corrected_summary_binds_two_owners_to_two_current_runtimes_with_zero_legacy_egress(monkeypatch):
    calls = []
    resolved = []
    revalidated = []
    initial_runtimes = {
        "owner-a": SimpleNamespace(provider="hermes", uid="owner-a"),
        "owner-b": SimpleNamespace(provider="hermes", uid="owner-b"),
    }
    current_runtimes = {
        "owner-a": SimpleNamespace(
            gateway_url="https://owner-a.runtime.test",
            gateway_token="owner-a-token",
            agent_id="owner-a-model",
        ),
        "owner-b": SimpleNamespace(
            gateway_url="https://owner-b.runtime.test",
            gateway_token="owner-b-token",
            agent_id="owner-b-model",
        ),
    }

    async def resolve_runtime(uid, *, target_mode):
        resolved.append((uid, target_mode))
        return initial_runtimes[uid]

    def bind_authority(runtime):
        return SimpleNamespace(uid=runtime.uid, target_mode="hermes-chat")

    async def revalidate(authority):
        revalidated.append(authority.uid)
        return current_runtimes[authority.uid]

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"title":"Corrected","overview":"[Ella] Corrected summary.",'
                                '"emoji":"🪽","category":"other"}'
                            )
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(summary_recovery, "resolve_isolated_runtime", resolve_runtime)
    monkeypatch.setattr(summary_recovery, "runtime_authority_identity", bind_authority)
    monkeypatch.setattr(summary_recovery, "revalidate_runtime_authority", revalidate)
    monkeypatch.setattr(
        corrections, "summary_provider_config_for_uid", summary_recovery.summary_provider_config_for_uid
    )
    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_API_URL", "https://legacy.invalid/v1/chat/completions")
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_MODEL", "legacy-model")
    monkeypatch.setenv("ELLA_CORRECTION_API_KEY", "legacy-secret")

    for uid in ("owner-a", "owner-b"):
        result = asyncio.run(
            corrections._generate_corrected_summary(
                uid=uid,
                conversation_id="conversation-1",
                correction_id="correction-1",
                trace_id=f"trace-{uid}",
                request=corrections.ConversationCorrectionRequest(correction_text="Correct the owner-bound summary."),
                structured={"title": "Old", "overview": "[Ella] Old."},
                transcript="Speaker: retained transcript",
                segment_count=1,
            )
        )
        assert result["title"] == "Corrected"

    assert resolved == [
        ("owner-a", "hermes-cloud-transcript"),
        ("owner-b", "hermes-cloud-transcript"),
    ]
    assert revalidated == ["owner-a", "owner-b"]
    assert [call["url"] for call in calls] == [
        "https://owner-a.runtime.test/v1/chat/completions",
        "https://owner-b.runtime.test/v1/chat/completions",
    ]
    assert [call["json"]["model"] for call in calls] == ["owner-a-model", "owner-b-model"]
    assert [call["headers"]["Authorization"] for call in calls] == [
        "Bearer owner-a-token",
        "Bearer owner-b-token",
    ]
    assert "legacy.invalid" not in str(calls)
    assert "legacy-model" not in str(calls)
    assert "legacy-secret" not in str(calls)


def test_provider_egress_revalidates_consent_after_slow_runtime_authority_resolution(monkeypatch):
    consent_checks = []
    provider_calls = []
    revalidated = []

    async def resolve_runtime(uid, *, target_mode):
        return SimpleNamespace(uid=uid, target_mode=target_mode)

    async def revalidate(authority):
        revalidated.append(authority.uid)
        await asyncio.sleep(0)
        return SimpleNamespace(
            gateway_url="https://owner.runtime.test",
            gateway_token="owner-token",
            agent_id="owner-model",
        )

    def consent(uid):
        consent_checks.append(uid)
        if len(consent_checks) == 3:
            raise HTTPException(status_code=403, detail={"code": "ai_consent_required", "decision": "revoked"})
        return uid

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            provider_calls.append((args, kwargs))
            raise AssertionError("revocation after runtime resolution must prevent provider egress")

    monkeypatch.setattr(summary_recovery, "resolve_isolated_runtime", resolve_runtime)
    monkeypatch.setattr(
        summary_recovery,
        "runtime_authority_identity",
        lambda runtime: SimpleNamespace(uid=runtime.uid, target_mode=runtime.target_mode),
    )
    monkeypatch.setattr(summary_recovery, "revalidate_runtime_authority", revalidate)
    monkeypatch.setattr(
        corrections, "summary_provider_config_for_uid", summary_recovery.summary_provider_config_for_uid
    )
    monkeypatch.setattr(corrections, "require_current_ai_consent", consent)
    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            corrections._generate_corrected_summary(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id="corr-1",
                trace_id="trace-1",
                request=corrections.ConversationCorrectionRequest(correction_text="Correct attribution."),
                structured={"title": "Old", "overview": "[Ella] Old."},
                transcript="retained transcript",
                segment_count=1,
            )
        )

    assert error.value.status_code == 403
    assert revalidated == ["owner-1"]
    assert consent_checks == ["owner-1", "owner-1", "owner-1"]
    assert provider_calls == []


def test_correction_session_key_is_stable_per_user():
    assert corrections._correction_session_key("User/123") == "ella:omi:user-123:canonical"
    assert corrections._correction_session_key("User/123") == corrections._correction_session_key("user/123")
    assert corrections._correction_session_key("Other User") == "ella:omi:other-user:canonical"
    assert corrections._correction_session_key("User/123") != corrections._correction_session_key("Other User")


def test_correction_session_key_matches_chat_memory_scope(monkeypatch):
    from ella.routers import chat

    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")
    assert corrections._correction_session_key("abc123") == chat._hermes_chat_session_key("abc123")
    assert corrections._correction_session_key("abc123") == chat._hermes_chat_memory_key("abc123")

    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "daily")
    assert chat._hermes_chat_session_key("abc123").startswith("ella:omi:abc123:ios-chat:")
    assert corrections._correction_session_key("abc123") == chat._hermes_chat_memory_key("abc123")


def test_generate_corrected_summary_can_use_legacy_provider(monkeypatch):
    calls = []

    monkeypatch.setattr(corrections, "CORRECTION_PROVIDER", "legacy")
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_API_URL", "https://legacy.test/v1/chat/completions")
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_MODEL", "grok-4.3")
    monkeypatch.setenv("ELLA_CORRECTION_API_KEY", "legacy-secret")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"title":"Corrected","overview":"[Ella] Corrected summary.",'
                                '"emoji":"🪽","category":"other"}'
                            )
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        corrections._generate_corrected_summary(
            uid="user-123",
            conversation_id="conv-123",
            correction_id="corr-123",
            trace_id="trace-123",
            request=corrections.ConversationCorrectionRequest(correction_text="Fix this."),
            structured={"title": "Old", "overview": "[Ella] Old."},
            transcript="Speaker: transcript",
            segment_count=1,
        )
    )

    assert result["title"] == "Corrected"
    assert calls[0]["url"] == "https://legacy.test/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer legacy-secret"


def test_create_summary_correction_proposal_uses_proposal_only_policy(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"proposal": {"proposal_id": "proposal-123"}}

    monkeypatch.setattr(corrections.proposal_ingest, "create_proposal", fake_create)

    proposal_id = corrections._create_summary_correction_proposal(
        uid="user-123",
        conversation_id="conv-123",
        correction_id="corr-123",
        trace_id="trace-123",
        request=corrections.ConversationCorrectionRequest(
            correction_text="Actually, that was Dr. Pu, not Dr. Cuu.",
            source="ios",
            summary_context={"title": "Doctor visit"},
        ),
        structured={"title": "Doctor visit", "overview": "Mentioned Dr. Cuu."},
        transcript="Speaker: Dr. Pu helped.",
        segment_count=1,
        active_summary_version_id="version-1",
    )

    assert proposal_id == "proposal-123"
    assert captured["tool_name"] == "conversation_correction_submit"
    assert captured["proposal_type"] == "summary_correction"
    assert captured["idempotency_key"] == "summary-correction:user-123:conv-123:corr-123"
    assert captured["session_claims"]["profile_uid"] == "user-123"
    assert captured["session_claims"]["scopes"] == ["proposals:write"]
    assert captured["payload"]["write_policy"] == "proposal_only"
    assert captured["payload"]["target"]["conversation_id"] == "conv-123"
    assert captured["payload"]["target"]["active_summary_version_id"] == "version-1"
    assert captured["payload"]["requested_change"]["correction_text"] == "Actually, that was Dr. Pu, not Dr. Cuu."
    assert captured["payload"]["evidence"][2]["kind"] == "transcript_excerpt"


def test_submit_to_n8n_posts_single_structured_workflow_payload(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        content = b'{"status":"processing"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "processing"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            calls.append({"url": url, "json": json, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        corrections._submit_correction_to_n8n(
            uid="user-123",
            conversation_id="conv-123",
            correction_id="corr-123",
            trace_id="correction:conv-123:corr-123",
            request=corrections.ConversationCorrectionRequest(
                correction_text="This was background TV audio.",
                source="ios",
                summary_context={"title": "Memory concern"},
            ),
            structured={"title": "Memory concern", "overview": "[Ella] old", "emoji": "", "category": "health"},
            transcript="Speaker: transcript",
            segment_count=1,
        )
    )

    assert result["n8n_webhook"] == "conversation-correction"
    assert calls == [
        {
            "url": "https://n8n.test/webhook/conversation-correction",
            "json": {
                "uid": "user-123",
                "conversation_id": "conv-123",
                "correction_id": "corr-123",
                "trace_id": "correction:conv-123:corr-123",
                "correction_text": "This was background TV audio.",
                "text": "This was background TV audio.",
                "correction_type": "media",
                "source": "ios",
                "summary_context": {"title": "Memory concern", "overview": None, "app_summary": None},
                "current_summary": {
                    "title": "Memory concern",
                    "overview": "[Ella] old",
                    "emoji": "",
                    "category": "health",
                },
                "transcript": "Speaker: transcript",
                "segments_count": 1,
            },
            "timeout": 20.0,
        }
    ]


def test_submit_to_n8n_accepts_async_success_body_even_with_bad_http_status(monkeypatch):
    class FakeResponse:
        status_code = 500
        content = b'{"status":"processing","queued":true}'

        def raise_for_status(self):
            raise AssertionError("raise_for_status should not run for accepted n8n async responses")

        def json(self):
            return {"status": "processing", "queued": True, "trace_id": "trace-123"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr(corrections.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        corrections._submit_correction_to_n8n(
            uid="user-123",
            conversation_id="conv-123",
            correction_id="corr-123",
            trace_id="trace-123",
            request=corrections.ConversationCorrectionRequest(
                correction_text="This was background TV audio.",
                source="ios",
                summary_context={"title": "Memory concern"},
            ),
            structured={"title": "Memory concern"},
            transcript="Speaker: transcript",
            segment_count=1,
        )
    )

    assert result == {
        "n8n_webhook": "conversation-correction",
        "n8n_status_code": 500,
        "n8n_response": {"status": "processing", "queued": True, "trace_id": "trace-123"},
    }


def test_retry_api_cannot_claim_outside_authenticated_uid(monkeypatch):
    calls = []

    def fake_claim(uid, conversation_id, request_id):
        calls.append((uid, conversation_id, request_id))
        return {"outcome": "not_found", "conversation": None}

    monkeypatch.setattr(corrections.conversations_db, "claim_conversation_processing_retry", fake_claim)
    app, client = _retry_api_client()
    try:
        response = client.post(
            "/v1/conversations/another-users-conversation/processing-retries",
            json={"request_id": "5bd3c697-7aa6-4fc0-aab2-a7eb6cf333e8"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert calls == [
        (
            "authenticated-user",
            "another-users-conversation",
            "5bd3c697-7aa6-4fc0-aab2-a7eb6cf333e8",
        )
    ]


def test_retry_plan_is_uid_scoped_metadata_only_and_zero_write(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "structured": {
            "title": "Generic cafe conversation",
            "overview": "[Ella] A generic summary is available.",
            "emoji": "brain",
            "category": "other",
        },
        "active_summary_version_id": "generic-v1",
        "summary_versions": [{"id": "generic-v1", "source": "omi", "kind": "generic_recovered", "is_active": True}],
        "transcript_segments": [
            {"is_user": True, "text": "a" * 100},
            {"speaker": "Other", "text": "b" * 50},
        ],
    }
    reads = []

    def fake_get(uid, conversation_id):
        reads.append((uid, conversation_id))
        return conversation

    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", fake_get)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "conversation_processing_recovery_mode",
        lambda record: ("enrichment_only", "generic_summary_without_enrichment"),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "has_usable_conversation_summary",
        lambda record: True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "has_enriched_conversation_summary",
        lambda record: False,
    )
    monkeypatch.setattr(summary_recovery, "_conversation_vector_present", lambda uid, cid: True)
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_metadata",
        lambda uid, cid: {
            "active_summary_version_id": "generic-v1",
            "summary_content_sha256": "generic-hash",
        },
    )
    app, client = _retry_api_client()
    try:
        response = client.get("/v1/conversations/conversation-1/processing-retry-plan")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert reads == [("authenticated-user", "conversation-1")]
    assert payload["recovery_mode"] == "enrichment_only"
    assert payload["retryable"] is True
    assert payload["vector_present"] is True
    assert payload["vector_active_summary_version_id"] == "generic-v1"
    assert payload["vector_matches_active_summary"] is False
    assert payload["transcript_segment_count"] == 2
    assert payload["transcript_character_count"] == 150
    assert payload["transcript_sha256"] == summary_recovery.build_hermes_recovery_source(conversation)[1]
    assert payload["structured_summary_sha256"] == summary_recovery._summary_content_sha256(conversation)
    assert payload["zero_writes"] is True
    assert "transcript_segments" not in payload
    assert "transcript" not in payload
    assert len(payload["profile_scope_sha256"]) == 64
    assert len(payload["canonical_session_scope_sha256"]) == 64


def test_retry_plan_404s_without_leaking_other_user_record(monkeypatch):
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: None)
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_present",
        lambda *args: pytest.fail("vector lookup must not run for a missing UID-owned record"),
    )
    app, client = _retry_api_client()
    try:
        response = client.get("/v1/conversations/another-users-conversation/processing-retry-plan")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_retry_api_claims_once_and_queues_two_stage_recovery(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    worker_calls = []
    monkeypatch.setattr(
        corrections.conversations_db,
        "claim_conversation_processing_retry",
        lambda uid, conversation_id, claimed_request_id: {
            "outcome": "claimed",
            "mode": "full",
            "phase": "claimed",
            "generic_status": "pending",
            "generic_vector_status": "pending",
            "enrichment_status": "pending",
            "vector_status": "pending",
            "lease_expires_at": "2026-07-20T08:15:00+00:00",
            "attempt_count": 1,
        },
    )
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: _retry_conversation(request_id=request_id),
    )

    async def fake_recover(**kwargs):
        worker_calls.append(kwargs)
        return "completed"

    monkeypatch.setattr(corrections, "recover_failed_conversation_summary", fake_recover)
    app, client = _retry_api_client()
    try:
        response = client.post(
            "/v1/conversations/conversation-1/processing-retries",
            json={"request_id": request_id, "correction_text": "This was the morning cafe conversation."},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["outcome"] == "processing"
    assert response.json()["recovery_mode"] == "full"
    assert response.json()["phase"] == "claimed"
    assert response.json()["generic_status"] == "pending"
    assert response.json()["enrichment_status"] == "pending"
    assert response.json()["vector_status"] == "pending"
    assert response.json()["attempt_count"] == 1
    assert worker_calls == [
        {
            "uid": "authenticated-user",
            "conversation_id": "conversation-1",
            "request_id": request_id,
            "client_context": "This was the morning cafe conversation.",
            "attempt_count": 1,
        }
    ]


@pytest.mark.parametrize("stored_outcome", ["processing", "completed", "failed"])
def test_retry_api_repeated_request_returns_receipt_without_new_work(monkeypatch, stored_outcome):
    request_id = "45cc793c-b030-4e94-8f93-44c248488d50"
    worker_calls = []
    monkeypatch.setattr(
        corrections.conversations_db,
        "claim_conversation_processing_retry",
        lambda uid, conversation_id, claimed_request_id: {
            "outcome": stored_outcome,
            "conversation": _retry_conversation(status=stored_outcome, request_id=claimed_request_id),
        },
    )
    monkeypatch.setattr(
        corrections.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: _retry_conversation(status=stored_outcome, request_id=request_id),
    )
    monkeypatch.setattr(
        corrections, "recover_failed_conversation_summary", lambda **kwargs: worker_calls.append(kwargs)
    )
    app, client = _retry_api_client()
    try:
        response = client.post(
            "/v1/conversations/conversation-1/processing-retries",
            json={"request_id": request_id},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["outcome"] == stored_outcome
    assert worker_calls == []


def test_retry_api_requires_uuid_request_id(monkeypatch):
    monkeypatch.setattr(
        corrections.conversations_db,
        "claim_conversation_processing_retry",
        lambda *args: pytest.fail("invalid request must not reach the claim"),
    )
    app, client = _retry_api_client()
    try:
        response = client.post(
            "/v1/conversations/conversation-1/processing-retries",
            json={"request_id": "not-a-uuid"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_summary_recovery_preserves_full_25k_transcript_in_hermes_prompt():
    transcript = "start " + ("important " * 3000) + "tail-marker"
    conversation = _retry_conversation(status="failed")
    conversation["transcript_segments"] = [{"is_user": True, "text": transcript}]

    prompt = summary_recovery._build_recovery_prompt(conversation, None)

    assert len(transcript) > 25_000
    assert "tail-marker" in prompt
    assert "[truncated]" not in prompt


def test_reclaimed_attempt_fences_stale_worker_before_generic_writeback(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    stale = {
        **_retry_conversation(request_id=request_id),
        "processing_retry_attempt_count": 1,
    }
    reclaimed = {**stale, "processing_retry_attempt_count": 2}
    reads = [stale, reclaimed]
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_source",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: {"title": "Stale", "overview": "Must not apply", "category": "other"},
    )
    monkeypatch.setattr(
        summary_recovery,
        "apply_summary_update",
        lambda **kwargs: pytest.fail("stale lease attempt must not write a summary version"),
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
            attempt_count=1,
        )
    )

    assert outcome == "superseded"


def test_vector_confirmation_skips_existing_vector_without_duplicate_write(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id="request-1"),
        "active_summary_version_id": "generic-v1",
    }
    writes = []
    content_sha256 = summary_recovery._summary_content_sha256(conversation)
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_metadata",
        lambda uid, cid: {
            "active_summary_version_id": "generic-v1",
            "summary_content_sha256": content_sha256,
        },
    )
    monkeypatch.setattr(summary_recovery, "refresh_structured_summary_vector", lambda *args: writes.append(args))

    asyncio.run(summary_recovery._ensure_conversation_vector("user-1", conversation))

    assert writes == []


def test_vector_confirmation_requires_post_write_visibility(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "generic-v1",
    }
    content_sha256 = summary_recovery._summary_content_sha256(conversation)
    checks = iter(
        [
            None,
            {
                "active_summary_version_id": "generic-v1",
                "summary_content_sha256": content_sha256,
            },
        ]
    )
    writes = []
    monkeypatch.setattr(summary_recovery, "_conversation_vector_metadata", lambda uid, cid: next(checks))
    monkeypatch.setattr(
        summary_recovery,
        "refresh_structured_summary_vector",
        lambda uid, conv, **kwargs: writes.append((conv.id, kwargs)) or {"upserted_count": 1},
    )

    asyncio.run(summary_recovery._ensure_conversation_vector("user-1", conversation))

    assert writes == [
        (
            "conversation-1",
            {
                "summary_version_id": "generic-v1",
                "summary_content_sha256": content_sha256,
            },
        )
    ]


def test_enriched_vector_is_force_upserted_and_verified_by_version_and_hash(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "enriched-v2",
        "structured": {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    writes = []

    def fake_save(uid, model, **kwargs):
        writes.append((uid, model.id, kwargs))
        return {"upserted_count": 1}

    expected_hash = summary_recovery._summary_content_sha256(conversation)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "get_conversation",
        lambda uid, cid: conversation,
    )
    monkeypatch.setattr(summary_recovery, "refresh_structured_summary_vector", fake_save)
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_metadata",
        lambda uid, cid: {
            "active_summary_version_id": "enriched-v2",
            "summary_content_sha256": expected_hash,
        },
    )

    result = asyncio.run(summary_recovery._write_and_confirm_enriched_vector("user-1", conversation, "enriched-v2"))

    assert result == expected_hash
    assert writes == [
        (
            "user-1",
            "conversation-1",
            {
                "summary_version_id": "enriched-v2",
                "summary_content_sha256": expected_hash,
            },
        )
    ]


def test_enriched_vector_fails_closed_when_active_summary_changes_after_upsert(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "enriched-v2",
        "structured": {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    changed = {**conversation, "active_summary_version_id": "manual-v3"}
    reads = iter([conversation, changed])
    expected_hash = summary_recovery._summary_content_sha256(conversation)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "get_conversation",
        lambda uid, cid: next(reads),
    )
    monkeypatch.setattr(
        summary_recovery,
        "refresh_structured_summary_vector",
        lambda uid, model, **kwargs: {"upserted_count": 1},
    )
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_metadata",
        lambda uid, cid: {
            "active_summary_version_id": "enriched-v2",
            "summary_content_sha256": expected_hash,
        },
    )

    with pytest.raises(
        summary_recovery.ConcurrentConversationRecoveryChangeError,
        match="active_summary_changed_after_vector_write",
    ):
        asyncio.run(
            summary_recovery._write_and_confirm_enriched_vector(
                "user-1",
                conversation,
                "enriched-v2",
            )
        )


def test_hermes_recovery_uses_lossless_source_and_canonical_uid_session(monkeypatch):
    transcript = "start " + ("important " * 3000) + "tail-marker"
    conversation = _retry_conversation(status="completed", request_id="request-1")
    conversation["transcript_segments"] = [{"is_user": True, "text": transcript}]
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"title":"Cafe","overview":"[Ella] Cafe context.",'
                            '"emoji":"coffee","category":"media"}'
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured.update({"url": url, "headers": headers, "body": json})
            return FakeResponse()

    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: conversation)

    async def fake_apply(**kwargs):
        captured["apply"] = kwargs
        return {"active_summary_version_id": "enriched-v2", "canonical_confirmed": True}

    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    config = summary_recovery.SummaryProviderConfig(
        provider="hermes-api",
        hermes_url="http://hermes.test/v1/chat/completions",
        hermes_model="companion-runtime",
        hermes_api_key="test-key",
        legacy_url="",
        legacy_model="",
        legacy_api_key="",
        timeout_seconds=45,
    )
    result = asyncio.run(
        summary_recovery.invoke_hermes_recovery(
            uid="user-1",
            conversation=conversation,
            request_id="request-1",
            client_context="Cafe context",
            config=config,
            async_client_factory=FakeClient,
        )
    )

    assert captured["url"] == "http://hermes.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["X-Hermes-Session-Key"] == summary_recovery.canonical_omi_session_key("user-1")
    prompt = captured["body"]["messages"][0]["content"]
    assert "tail-marker" in prompt
    assert "Cafe context" in prompt
    assert summary_recovery.build_hermes_recovery_source(conversation)[1] in prompt
    assert captured["apply"]["summary"]["category"] == "entertainment"
    assert captured["apply"]["require_canonical"] is True
    assert captured["apply"]["preserve_generated_results"] is True
    assert result == {
        "active_summary_version_id": "enriched-v2",
        "canonical_confirmed": True,
        "source_sha256": summary_recovery.build_hermes_recovery_source(conversation)[1],
        "session_scope_sha256": summary_recovery.hashlib.sha256(
            summary_recovery.canonical_omi_session_key("user-1").encode("utf-8")
        ).hexdigest(),
    }


def test_summary_recovery_persists_generic_before_hermes_enrichment(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    conversation = _retry_conversation(request_id=request_id)
    generic = {
        **conversation,
        "active_summary_version_id": "generic-v1",
        "processing_retry_summary_version_id": "generic-v1",
        "structured": {
            "title": "Generic",
            "overview": "[Ella] Generic summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    completed_generic = {**generic, "status": "completed", "processing_error": None}
    enriched = {
        **completed_generic,
        "active_summary_version_id": "enriched-v2",
        "enrichment_state": {"status": "writeback_applied", "kind": "recovered_enriched"},
        "structured": {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    reads = [conversation, conversation, generic, completed_generic, enriched]
    events = []

    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda uid, conv: events.append("generic_generate")
        or {
            "title": "Generic",
            "overview": "Generic summary.",
            "emoji": "brain",
            "category": "other",
        },
    )

    async def fake_invoke(**kwargs):
        events.append("hermes_provision")
        return {"active_summary_version_id": "enriched-v2", "canonical_confirmed": True}

    async def fake_apply(**kwargs):
        events.append(f"apply:{kwargs['summary_kind']}")
        version_id = "generic-v1" if kwargs["summary_kind"] == "generic_recovered" else "enriched-v2"
        return {
            "status": "ok",
            "active_summary_version_id": version_id,
            "canonical_confirmed": kwargs["summary_kind"] == "recovered_enriched",
        }

    monkeypatch.setattr(summary_recovery, "invoke_hermes_recovery", fake_invoke)
    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_summary_applied",
        lambda *args, **kwargs: events.append("generic_receipt") or True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: _async_event(events, f"vector:{conv['structured']['title']}"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda uid, conv, version_id: _async_event_result(events, "vector:Enriched", "e" * 64),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append(f"generic_vector:{args[3]}") or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "finish_conversation_processing_retry",
        lambda *args, **kwargs: events.append(f"finish:{args[3]}") or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append(f"enrichment:{args[3]}") or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "completed"
    assert events == [
        "generic_generate",
        "apply:generic_recovered",
        "generic_receipt",
        "vector:Generic",
        "generic_vector:completed",
        "hermes_provision",
        "enrichment:canonical_completed",
        "vector:Enriched",
        "enrichment:completed",
    ]


def test_summary_recovery_hermes_failure_retains_completed_generic_summary(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    conversation = _retry_conversation(request_id=request_id)
    generic = {
        **conversation,
        "active_summary_version_id": "generic-v1",
        "processing_retry_summary_version_id": "generic-v1",
        "structured": {
            "title": "Generic",
            "overview": "[Ella] Generic summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    completed_generic = {**generic, "status": "completed", "processing_error": None}
    reads = [conversation, conversation, generic, completed_generic, completed_generic]
    events = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda uid, conv: {
            "title": "Generic",
            "overview": "Generic summary.",
            "emoji": "brain",
            "category": "other",
        },
    )

    async def fake_invoke(**kwargs):
        raise RuntimeError("private Hermes provider detail")

    async def fake_apply(**kwargs):
        assert kwargs["summary_kind"] == "generic_recovered"
        return {"status": "ok", "active_summary_version_id": "generic-v1"}

    monkeypatch.setattr(summary_recovery, "invoke_hermes_recovery", fake_invoke)
    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_summary_applied",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: _async_event(events, "vector"),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append(("generic_vector", args[3])) or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "finish_conversation_processing_retry",
        lambda *args, **kwargs: events.append(("finish", args[3], kwargs)) or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append(("enrichment", args[3], kwargs)) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "failed"
    assert events == [
        "vector",
        ("generic_vector", "completed"),
        ("enrichment", "failed", {"attempt_count": None}),
    ]
    assert "private Hermes provider detail" not in str(events)


def test_summary_recovery_resumes_hermes_after_generic_phase_completed(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    generic = {
        **_retry_conversation(status="completed", request_id=request_id),
        "active_summary_version_id": "generic-v1",
        "processing_retry_summary_version_id": "generic-v1",
        "processing_retry_mode": "enrichment_only",
    }
    enriched = {
        **generic,
        "active_summary_version_id": "enriched-v2",
        "processing_retry_enriched_version_id": "enriched-v2",
    }
    reads = [generic, generic, generic, enriched]
    events = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("generic provider must not rerun after its durable receipt"),
    )

    async def fake_invoke(**kwargs):
        events.append("hermes_provision")
        return {"status": "ok", "active_summary_version_id": "enriched-v2", "canonical_confirmed": True}

    monkeypatch.setattr(summary_recovery, "invoke_hermes_recovery", fake_invoke)
    monkeypatch.setattr(
        summary_recovery,
        "_conversation_vector_present",
        lambda uid, cid: True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: pytest.fail("stage-2-only recovery must preserve the existing generic vector"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda uid, conv, version_id: _async_event_result(events, "vector:enriched-v2", "e" * 64),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append(f"generic_vector:{args[3]}") or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "finish_conversation_processing_retry",
        lambda *args, **kwargs: events.append("finish") or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append(f"enrichment:{args[3]}") or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "completed"
    assert events == [
        "generic_vector:completed",
        "hermes_provision",
        "enrichment:canonical_completed",
        "vector:enriched-v2",
        "enrichment:completed",
    ]


def test_summary_recovery_enriches_legacy_generic_without_version_or_generic_rewrite(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    legacy = {
        **_retry_conversation(status="completed", request_id=request_id),
        "processing_retry_mode": "enrichment_only",
        "active_summary_version_id": None,
        "summary_versions": [],
        "structured": {
            "title": "Legacy generic",
            "overview": "[Ella] A preserved generic summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    enriched = {
        **legacy,
        "active_summary_version_id": "enriched-v2",
        "summary_versions": [
            {"id": "legacy-v1", "kind": "legacy_current", "is_active": False},
            {"id": "enriched-v2", "kind": "recovered_enriched", "is_active": True},
        ],
        "enrichment_state": {
            "status": "writeback_applied",
            "kind": "recovered_enriched",
            "canonical_status": "completed",
        },
    }
    reads = [legacy, legacy, legacy, enriched]
    events = []
    source_hashes = []
    expected_summary_hash = summary_recovery._summary_content_sha256(legacy)
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_source",
        lambda *args, **kwargs: source_hashes.append(kwargs.get("generic_summary_sha256")) or True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("legacy generic content must not be regenerated"),
    )
    monkeypatch.setattr(summary_recovery, "_conversation_vector_present", lambda uid, cid: True)
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: pytest.fail("the existing legacy generic vector must not be overwritten"),
    )

    async def fake_invoke(**kwargs):
        assert kwargs["conversation"]["active_summary_version_id"] is None
        assert summary_recovery._summary_content_sha256(kwargs["conversation"]) == expected_summary_hash
        events.append("hermes")
        return {"active_summary_version_id": "enriched-v2", "canonical_confirmed": True}

    monkeypatch.setattr(summary_recovery, "invoke_hermes_recovery", fake_invoke)
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda uid, conv, version_id: _async_event_result(events, "enriched_vector", "e" * 64),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append("generic_vector_receipt") or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append(args[3]) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "completed"
    assert source_hashes == [expected_summary_hash, expected_summary_hash]
    assert events == [
        "generic_vector_receipt",
        "hermes",
        "canonical_completed",
        "enriched_vector",
        "completed",
    ]


def test_summary_recovery_reconciles_transport_uncertainty_after_confirmed_writeback(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    generic = {
        **_retry_conversation(status="completed", request_id=request_id),
        "active_summary_version_id": "generic-v1",
        "processing_retry_summary_version_id": "generic-v1",
        "processing_retry_mode": "enrichment_only",
    }
    enriched = {
        **generic,
        "active_summary_version_id": "enriched-v2",
        "summary_versions": [
            {
                "id": "enriched-v2",
                "created_at": "2026-07-20T08:31:00+00:00",
                "kind": "recovered_enriched",
                "source": "observer",
                "is_active": True,
            }
        ],
        "enrichment_state": {
            "status": "writeback_applied",
            "kind": "recovered_enriched",
            "canonical_status": "completed",
        },
    }
    reads = [generic, generic, generic, enriched, enriched]
    events = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("generic provider must not run for enrichment-only recovery"),
    )

    async def uncertain_invoke(**kwargs):
        raise RuntimeError("transport closed after remote writeback")

    monkeypatch.setattr(summary_recovery, "invoke_hermes_recovery", uncertain_invoke)
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: _async_event(events, f"vector:{conv['active_summary_version_id']}"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda uid, conv, version_id: _async_event_result(events, "vector:enriched-v2", "e" * 64),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append(("generic_vector", args[3])) or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append((args[3], kwargs["summary_version_id"])) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "completed"
    assert events == [
        "vector:generic-v1",
        ("generic_vector", "completed"),
        ("canonical_completed", "enriched-v2"),
        "vector:enriched-v2",
        ("completed", "enriched-v2"),
    ]


def test_summary_recovery_retries_pending_canonical_write_without_second_model_call(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    pending = {
        **_retry_conversation(status="completed", request_id=request_id),
        "processing_retry_mode": "enrichment_only",
        "active_summary_version_id": "enriched-v2",
        "processing_retry_summary_version_id": "generic-v1",
        "enrichment_state": {
            "status": "writeback_pending_canonical",
            "pending": True,
            "kind": "recovered_enriched",
            "trace_id": "prior-hermes-trace",
            "canonical_status": "failed",
        },
    }
    confirmed = {
        **pending,
        "enrichment_state": {
            **pending["enrichment_state"],
            "status": "writeback_applied",
            "pending": False,
            "canonical_status": "completed",
        },
    }
    reads = [pending, pending, pending, confirmed, confirmed]
    events = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("generic provider must not run for enrichment-only recovery"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "invoke_hermes_recovery",
        lambda **kwargs: pytest.fail("Hermes enrichment must not rerun while canonical write is pending"),
    )

    async def fake_apply(**kwargs):
        events.append(("canonical_retry", kwargs["trace_id"], kwargs["summary"]))
        return {"active_summary_version_id": "enriched-v2", "canonical_confirmed": True}

    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: _async_event(events, f"vector:{conv['active_summary_version_id']}"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda uid, conv, version_id: _async_event_result(events, "vector:enriched-v2", "e" * 64),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: events.append(("generic_vector", args[3])) or True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: events.append(("enrichment", args[3], kwargs["summary_version_id"])) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "completed"
    assert events == [
        "vector:enriched-v2",
        ("generic_vector", "completed"),
        ("canonical_retry", "prior-hermes-trace", {}),
        ("enrichment", "canonical_completed", "enriched-v2"),
        "vector:enriched-v2",
        ("enrichment", "completed", "enriched-v2"),
    ]


def test_summary_recovery_vector_failure_remains_retryable_without_raw_error(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    conversation = {
        **_retry_conversation(request_id=request_id),
        "active_summary_version_id": "recovered-v1",
        "processing_retry_summary_version_id": "recovered-v1",
    }
    vector_receipts = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("receipted generic summary must not be regenerated"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_ensure_conversation_vector",
        lambda uid, conv: _async_raise(RuntimeError("secret provider detail")),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: vector_receipts.append(args[3]) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "failed"
    assert vector_receipts == ["failed"]
    assert "secret provider detail" not in str(vector_receipts)


def test_enriched_vector_failure_receipts_version_and_hash_without_rerunning_model(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    enriched = {
        **_retry_conversation(status="completed", request_id=request_id),
        "processing_retry_mode": "enrichment_only",
        "processing_retry_summary_version_id": "generic-v1",
        "processing_retry_enriched_version_id": "enriched-v2",
        "active_summary_version_id": "enriched-v2",
        "summary_versions": [
            {"id": "enriched-v2", "kind": "recovered_enriched", "source": "observer", "is_active": True}
        ],
        "enrichment_state": {
            "status": "writeback_applied",
            "kind": "recovered_enriched",
            "canonical_status": "completed",
        },
        "structured": {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
        },
    }
    reads = [enriched, enriched, enriched, enriched]
    receipts = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: reads.pop(0))
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_source",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(summary_recovery, "_ensure_conversation_vector", lambda uid, conv: _async_event([], None))
    monkeypatch.setattr(
        summary_recovery,
        "invoke_hermes_recovery",
        lambda **kwargs: pytest.fail("confirmed enrichment must not rerun Hermes"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "_write_and_confirm_enriched_vector",
        lambda *args: _async_raise(RuntimeError("private vector provider detail")),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_generic_vector",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_enrichment",
        lambda *args, **kwargs: receipts.append((args[3], kwargs)) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    expected_hash = summary_recovery._summary_content_sha256(enriched)
    assert outcome == "failed"
    assert receipts == [
        ("canonical_completed", {"summary_version_id": "enriched-v2", "attempt_count": None}),
        (
            "vector_failed",
            {
                "summary_version_id": "enriched-v2",
                "vector_content_sha256": expected_hash,
                "attempt_count": None,
            },
        ),
    ]
    assert "private vector provider detail" not in str(receipts)


def test_strict_summary_writeback_confirms_canonical_before_success(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "generic-v1",
        "summary_versions": [],
        "apps_results": [{"app_id": "preserve-me"}],
        "plugins_results": [{"plugin_id": "preserve-me"}],
    }
    updates = []
    canonical_calls = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [{"id": "enriched-v2", "kind": "recovered_enriched", "is_active": True}],
            "active_summary_version_id": "enriched-v2",
        },
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda uid, cid, update: updates.append(update),
    )

    def canonical_writer(uid, record, **kwargs):
        canonical_calls.append((uid, record, kwargs))
        return {"ok": True, "inserted": 1, "duplicates": 0}

    result = asyncio.run(
        summary_writeback.write_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            title="Enriched",
            overview="[Ella] Enriched and canonicalized summary.",
            category="other",
            summary_kind="recovered_enriched",
            trace_id="trace-1",
            canonical_writer=canonical_writer,
            require_canonical=True,
            preserve_generated_results=True,
        )
    )

    assert result["canonical_confirmed"] is True
    assert updates[0]["enrichment_state"]["status"] == "writeback_pending_canonical"
    assert "apps_results" not in updates[0]
    assert "plugins_results" not in updates[0]
    assert updates[1]["enrichment_state"]["status"] == "writeback_applied"
    assert updates[1]["enrichment_state"]["canonical_status"] == "completed"
    assert canonical_calls[0][1]["enrichment_state"]["canonical_status"] == "completed"


def test_correction_writeback_never_publishes_applied_before_canonical_durability(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "kind": "generic", "is_active": True}],
        "correction_state": {
            "correction_id": "correction-1",
            "status": "processing",
            "pending": True,
            "source": "ios",
        },
    }
    writes = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [
                {"id": "base-v1", "kind": "generic", "is_active": False},
                {"id": "corrected-v2", "kind": "corrected_enriched", "is_active": True},
            ],
            "active_summary_version_id": "corrected-v2",
        },
    )

    def cas(uid, cid, expected, update):
        writes.append(("source_cas", update))
        assert update["correction_state"]["status"] == "canonical_pending"
        assert update["correction_state"]["pending"] is True
        return True

    monkeypatch.setattr(summary_writeback.conversations_db, "update_conversation_if_active_summary_version", cas)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda uid, cid, update: writes.append(("canonical_confirmed", update)),
    )

    def canonical_writer(uid, record, **kwargs):
        writes.append(("canonical", record))
        assert record["correction_state"]["status"] == "applied"
        return {"ok": True}

    result = asyncio.run(
        summary_writeback.write_conversation_summary(
            uid="owner-1",
            conversation_id="conv-1",
            title="Corrected",
            overview=(
                "[Ella] Corrected the retained summary while preserving the source facts and version provenance."
            ),
            category="other",
            summary_kind="corrected_enriched",
            correction_id="correction-1",
            based_on_version_id="base-v1",
            trace_id="correction:conv-1:correction-1",
            require_based_on_match=True,
            require_canonical=True,
            canonical_writer=canonical_writer,
        )
    )

    assert result["canonical_confirmed"] is True
    assert [stage for stage, _ in writes] == ["source_cas", "canonical", "canonical_confirmed"]
    assert writes[-1][1]["correction_state"]["status"] == "applied"
    assert writes[-1][1]["correction_state"]["pending"] is False


def test_recovery_writeback_fails_closed_when_active_summary_changed(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "newer-v2",
    }
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda *args: pytest.fail("concurrent change must prevent writeback"),
    )

    with pytest.raises(
        summary_writeback.ConcurrentConversationSummaryChangeError,
        match="active_summary_version_changed",
    ):
        asyncio.run(
            summary_writeback.write_conversation_summary(
                uid="user-1",
                conversation_id="conversation-1",
                title="Stale recovery",
                overview="[Ella] Stale recovery.",
                category="other",
                based_on_version_id="generic-v1",
                require_based_on_match=True,
            )
        )


def test_strict_summary_writeback_uses_atomic_active_version_compare_and_set(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "generic-v1",
        "summary_versions": [{"id": "generic-v1", "kind": "generic", "is_active": True}],
    }
    cas_calls = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [
                {"id": "generic-v1", "kind": "generic", "is_active": False},
                {"id": "undo-v2", "kind": "correction_undo", "is_active": True},
            ],
            "active_summary_version_id": "undo-v2",
        },
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation_if_active_summary_version",
        lambda uid, cid, expected, update: cas_calls.append((uid, cid, expected, update)) or True,
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda *args: pytest.fail("strict write must use the transactional compare-and-set helper"),
    )

    result = asyncio.run(
        summary_writeback.write_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            title="Restored",
            overview="[Ella] Restored the prior conversation summary without changing newer memory.",
            category="other",
            summary_kind="correction_undo",
            based_on_version_id="generic-v1",
            require_based_on_match=True,
            canonical_writer=lambda *args, **kwargs: {"ok": True},
        )
    )

    assert result["active_summary_version_id"] == "undo-v2"
    assert cas_calls[0][0:3] == ("user-1", "conversation-1", "generic-v1")
    assert cas_calls[0][3]["active_summary_version_id"] == "undo-v2"


def test_strict_summary_writeback_fails_when_atomic_compare_and_set_loses_race(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "generic-v1",
        "summary_versions": [{"id": "generic-v1", "kind": "generic", "is_active": True}],
    }
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [
                {"id": "generic-v1", "kind": "generic", "is_active": False},
                {"id": "undo-v2", "kind": "correction_undo", "is_active": True},
            ],
            "active_summary_version_id": "undo-v2",
        },
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation_if_active_summary_version",
        lambda uid, cid, expected, update: False,
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda *args: pytest.fail("lost CAS must not fall back to an unconditional write"),
    )

    with pytest.raises(
        summary_writeback.ConcurrentConversationSummaryChangeError,
        match="active_summary_version_changed",
    ):
        asyncio.run(
            summary_writeback.write_conversation_summary(
                uid="user-1",
                conversation_id="conversation-1",
                title="Restored",
                overview="[Ella] Restored the prior conversation summary without changing newer memory.",
                category="other",
                summary_kind="correction_undo",
                based_on_version_id="generic-v1",
                require_based_on_match=True,
                canonical_writer=lambda *args, **kwargs: {"ok": True},
            )
        )


def test_strict_summary_writeback_keeps_retryable_state_when_canonical_fails(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "generic-v1",
        "summary_versions": [],
    }
    updates = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [{"id": "enriched-v2", "kind": "recovered_enriched", "is_active": True}],
            "active_summary_version_id": "enriched-v2",
        },
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda uid, cid, update: updates.append(update),
    )

    with pytest.raises(RuntimeError, match="canonical_write_unconfirmed"):
        asyncio.run(
            summary_writeback.write_conversation_summary(
                uid="user-1",
                conversation_id="conversation-1",
                title="Enriched",
                overview="[Ella] Enriched summary awaiting canonical confirmation.",
                category="other",
                summary_kind="recovered_enriched",
                trace_id="trace-2",
                canonical_writer=lambda *args, **kwargs: {"ok": False, "skipped": True},
                require_canonical=True,
            )
        )

    assert updates[-1]["enrichment_state"]["status"] == "writeback_pending_canonical"
    assert updates[-1]["enrichment_state"]["canonical_status"] == "failed"
    assert updates[-1]["enrichment_state"]["pending"] is True


def test_strict_summary_writeback_retries_only_canonical_after_transport_uncertainty(monkeypatch):
    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "enriched-v2",
        "summary_versions": [{"id": "enriched-v2", "kind": "recovered_enriched", "is_active": True}],
        "enrichment_state": {
            "status": "writeback_pending_canonical",
            "pending": True,
            "source": "observer",
            "kind": "recovered_enriched",
            "trace_id": "trace-3",
            "canonical_status": "failed",
        },
    }
    updates = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: pytest.fail("canonical replay must not append another summary version"),
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda uid, cid, update: updates.append(update),
    )

    result = asyncio.run(
        summary_writeback.write_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            summary_kind="recovered_enriched",
            trace_id="trace-3",
            canonical_writer=lambda *args, **kwargs: {"ok": True, "inserted": 0, "duplicates": 1},
            require_canonical=True,
        )
    )

    assert result["idempotent_replay"] is True
    assert result["canonical_confirmed"] is True
    assert updates == [
        {
            "enrichment_state": {
                **conversation["enrichment_state"],
                "status": "writeback_applied",
                "pending": False,
                "canonical_status": "completed",
                "error": None,
                "updated_at": updates[0]["enrichment_state"]["updated_at"],
            }
        }
    ]


def test_canonical_success_survives_confirmation_marker_failure_and_replays_without_second_source_cas(monkeypatch):
    from utils.ella.canonical_omi import build_omi_canonical_event

    conversation = {
        **_retry_conversation(status="completed", request_id=None),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "kind": "generic", "is_active": True}],
    }
    source_cas_calls = []
    canonical_calls = []
    canonical_payloads = []
    canonical_guard_calls = []
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "build_summary_version_update",
        lambda *args, **kwargs: {
            "summary_versions": [
                {"id": "base-v1", "kind": "generic", "is_active": False},
                {
                    "id": "corrected-v2",
                    "kind": "corrected_enriched",
                    "correction_id": "corr-1",
                    "based_on_version_id": "base-v1",
                    "is_active": True,
                },
            ],
            "active_summary_version_id": "corrected-v2",
        },
    )

    def source_cas(uid, cid, expected, update):
        source_cas_calls.append(expected)
        conversation["structured"] = {
            **conversation.get("structured", {}),
            "title": update["structured.title"],
            "overview": update["structured.overview"],
            "category": update["structured.category"],
        }
        conversation.update(
            {
                "summary_versions": update["summary_versions"],
                "active_summary_version_id": update["active_summary_version_id"],
                "enrichment_state": update["enrichment_state"],
                "correction_state": update["correction_state"],
            }
        )
        return True

    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation_if_active_summary_version",
        source_cas,
    )
    monkeypatch.setattr(
        summary_writeback.conversations_db,
        "update_conversation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("confirmation transport failed")),
    )

    def canonical_writer(*args, **kwargs):
        assert len(canonical_guard_calls) == len(canonical_calls) + 1
        canonical_calls.append(kwargs["trace_id"])
        canonical_payloads.append(
            build_omi_canonical_event(
                args[0], args[1], summary_kind=kwargs["summary_kind"], trace_id=kwargs["trace_id"]
            )
        )
        return {"ok": True, "inserted": 1}

    async def apply_once():
        return await summary_writeback.write_conversation_summary(
            uid="owner-1",
            conversation_id="conv-1",
            title="Corrected",
            overview="[Ella] Corrected the retained attribution while preserving the rest of the conversation.",
            category="other",
            summary_kind="corrected_enriched",
            correction_id="corr-1",
            based_on_version_id="base-v1",
            trace_id="correction:conv-1:corr-1",
            canonical_writer=canonical_writer,
            require_canonical=True,
            require_based_on_match=True,
            canonical_egress_guard=lambda: canonical_guard_calls.append("checked"),
        )

    first = asyncio.run(apply_once())
    replay = asyncio.run(apply_once())

    assert first["canonical_confirmed"] is True
    assert replay["canonical_confirmed"] is True
    assert source_cas_calls == ["base-v1"]
    assert canonical_calls == ["correction:conv-1:corr-1", "correction:conv-1:corr-1"]
    assert canonical_guard_calls == ["checked", "checked"]
    assert canonical_payloads[0] == canonical_payloads[1]


class _MutableCorrectionAuditRef:
    def __init__(self, value):
        self.value = value

    def get(self, transaction=None):
        return SimpleNamespace(exists=True, to_dict=lambda: dict(self.value))

    def set(self, payload, merge=False):
        if merge:
            self.value.update(payload)
        else:
            self.value = dict(payload)

    def collection(self, name):
        return SimpleNamespace(stream=lambda: [])


class _CorrectionBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, function, *args, **kwargs):
        self.tasks.append((function, args, kwargs))


def test_retry_failed_correction_reuses_existing_id_and_fences_completed_side_effects(monkeypatch):
    correction_id = "retained-correction"
    conversation = {
        **_conversation(),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "is_active": True}],
    }
    audit = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "trace_id": f"correction:conv-1:{correction_id}",
        "status": "direct_apply_failed",
        "source": "ios",
        "correction_text": "Correct the attribution.",
        "summary_context": {},
        "current_summary": _conversation()["structured"],
        "active_summary_version_id": "base-v1",
        "created_at": "2026-08-15T22:13:10+00:00",
        "proposal_id": "existing-proposal",
        "canonical_event_completed": True,
        "propagation_completed": True,
        "events": [],
    }
    audit_ref = _MutableCorrectionAuditRef(audit)
    source_applies = []
    downstream_calls = []

    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)

    def claim(**kwargs):
        assert kwargs["uid"] == "owner-1"
        assert kwargs["conversation_id"] == "conv-1"
        assert kwargs["correction_id"] == correction_id
        assert kwargs["recorded_base_version_id"] == "base-v1"
        if audit_ref.value.get("status") in {"retry_queued", "processing"}:
            return "already_queued"
        audit_ref.value.update(
            {
                "status": "retry_queued",
                "retry_count": 1,
                "retry_attempt_token": kwargs["retry_attempt_token"],
                "retry_lease_expires_at": kwargs["retry_lease_expires_at"],
            }
        )
        return "claimed"

    monkeypatch.setattr(corrections, "_claim_failed_correction_retry", claim)
    monkeypatch.setattr(corrections, "_new_correction_attempt_token", lambda: "attempt-1")
    attempt_starts = []

    def start_attempt(**kwargs):
        attempt_starts.append(kwargs["retry_attempt_token"])
        return "started"

    monkeypatch.setattr(corrections, "_start_correction_retry_attempt", start_attempt)
    monkeypatch.setattr(corrections.uuid, "uuid4", lambda: pytest.fail("retry must retain the existing correction id"))

    async def generate(**kwargs):
        return {"title": "Corrected", "overview": "[Ella] Corrected.", "category": "other"}

    async def apply(**kwargs):
        source_applies.append(kwargs)
        conversation["summary_versions"].append(
            {
                "id": "corrected-v2",
                "based_on_version_id": "base-v1",
                "correction_id": correction_id,
                "is_active": True,
            }
        )
        conversation["active_summary_version_id"] = "corrected-v2"
        return {"status": "ok", "active_summary_version_id": "corrected-v2", "canonical_confirmed": True}

    monkeypatch.setattr(corrections, "_generate_corrected_summary", generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", apply)
    monkeypatch.setattr(
        corrections,
        "_create_summary_correction_proposal",
        lambda **kwargs: downstream_calls.append("proposal") or "duplicate",
    )

    async def emit(**kwargs):
        downstream_calls.append("canonical")

    async def propagate(**kwargs):
        downstream_calls.append("propagation")

    monkeypatch.setattr(corrections, "_emit_canonical_correction_event", emit)
    monkeypatch.setattr(corrections, "_run_correction_propagation_for_submission", propagate)

    background = _CorrectionBackgroundTasks()
    queued = asyncio.run(
        corrections._retry_failed_conversation_correction(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=background,
        )
    )
    assert queued.correction_id == correction_id
    assert queued.status == "queued"
    assert len(background.tasks) == 1

    duplicate_background = _CorrectionBackgroundTasks()
    duplicate = asyncio.run(
        corrections._retry_failed_conversation_correction(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=duplicate_background,
        )
    )
    assert duplicate.status == "queued"
    assert duplicate_background.tasks == []

    function, args, kwargs = background.tasks[0]
    applied = asyncio.run(function(*args, **kwargs))
    assert applied.correction_id == correction_id
    assert applied.status == "applied"
    assert len(source_applies) == 1
    assert attempt_starts == ["attempt-1", "attempt-1"]
    assert source_applies[0]["active_summary_version_id"] == "base-v1"
    assert downstream_calls == []

    replay = asyncio.run(
        corrections._retry_failed_conversation_correction(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=_CorrectionBackgroundTasks(),
        )
    )
    assert replay.status == "applied"
    assert len(source_applies) == 1
    assert downstream_calls == []


def test_retry_failed_correction_rejects_changed_active_version_before_any_write(monkeypatch):
    correction_id = "retained-correction"
    conversation = {
        **_conversation(),
        "active_summary_version_id": "newer-v2",
        "summary_versions": [{"id": "newer-v2", "is_active": True}],
    }
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "trace_id": f"correction:conv-1:{correction_id}",
            "status": "direct_apply_failed",
            "source": "ios",
            "correction_text": "Correct the attribution.",
            "summary_context": {},
            "current_summary": _conversation()["structured"],
            "active_summary_version_id": "base-v1",
        }
    )
    writes = []
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        corrections,
        "_claim_failed_correction_retry",
        lambda **kwargs: writes.append("retry_claim") or "claimed",
    )
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda *args, **kwargs: writes.append("audit"),
    )
    monkeypatch.setattr(
        corrections,
        "_generate_corrected_summary",
        lambda **kwargs: pytest.fail("version drift must fail before provider work"),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            corrections._retry_failed_conversation_correction(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id=correction_id,
                background_tasks=_CorrectionBackgroundTasks(),
            )
        )

    assert error.value.status_code == 409
    assert writes == []


def test_retry_failed_correction_reclaims_stale_lost_task_without_reclaiming_live_lease():
    correction_id = "retained-correction"
    conversation_ref = _MutableCorrectionAuditRef({"active_summary_version_id": "base-v1"})
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "direct_apply_failed",
            "source": "ios",
            "retry_count": 1,
        }
    )

    class _Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, payload, merge=False):
            self.writes.append((ref, dict(payload), merge))
            ref.set(payload, merge=merge)

    transaction = _Transaction()
    kwargs = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "recorded_base_version_id": "base-v1",
        "retry_queued_at": "2026-08-15T23:19:09+00:00",
        "retry_lease_expires_at": "2026-08-15T23:21:09+00:00",
        "retry_attempt_token": "attempt-1",
        "source": "ios",
    }
    first = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **kwargs,
    )
    duplicate = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **kwargs,
    )

    assert first == "claimed"
    assert duplicate == "already_queued"
    assert len(transaction.writes) == 1
    assert audit_ref.value["retry_count"] == 2
    assert audit_ref.value["active_summary_version_id"] == "base-v1"
    assert audit_ref.value["retry_attempt_token"] == "attempt-1"

    audit_ref.value["retry_lease_expires_at"] = "2026-08-15T23:18:00+00:00"
    reclaimed_kwargs = {
        **kwargs,
        "retry_queued_at": "2026-08-15T23:22:00+00:00",
        "retry_lease_expires_at": "2026-08-15T23:24:00+00:00",
        "retry_attempt_token": "attempt-2",
    }
    reclaimed = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **reclaimed_kwargs,
    )
    live_duplicate = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **reclaimed_kwargs,
    )
    assert reclaimed == "claimed"
    assert live_duplicate == "already_queued"
    assert len(transaction.writes) == 2
    assert audit_ref.value["retry_attempt_token"] == "attempt-2"
    assert audit_ref.value["retry_count"] == 3

    stale_start = corrections._start_correction_retry_attempt_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-1",
        started_at="2026-08-15T23:22:01+00:00",
    )
    current_start = corrections._start_correction_retry_attempt_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-2",
        started_at="2026-08-15T23:22:01+00:00",
    )
    assert stale_start == "stale_attempt"
    assert current_start == "started"
    assert len(transaction.writes) == 3
    assert audit_ref.value["retry_lease_expires_at"] == "2026-08-15T23:24:00+00:00"

    second_ownership_check = corrections._start_correction_retry_attempt_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-2",
        started_at="2026-08-15T23:23:59+00:00",
    )
    expired_ownership_check = corrections._start_correction_retry_attempt_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-2",
        started_at="2026-08-15T23:24:01+00:00",
    )
    assert second_ownership_check == "started"
    assert expired_ownership_check == "lease_expired"
    assert len(transaction.writes) == 4
    assert audit_ref.value["retry_lease_expires_at"] == "2026-08-15T23:24:00+00:00"

    audit_ref.value["status"] = "direct_apply_failed"
    conversation_ref.value["active_summary_version_id"] = "newer-v2"
    drift = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **kwargs,
    )
    assert drift == "version_drift"
    assert len(transaction.writes) == 4


def test_canonical_pending_reconciliation_is_serialized_and_stale_completion_is_fenced():
    correction_id = "canonical-pending-correction"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1", "is_active": False},
                {
                    "id": "corrected-v2",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                    "is_active": True,
                },
            ],
        }
    )
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "canonical_pending",
            "active_summary_version_id": "base-v1",
            "retry_lease_expires_at": None,
        }
    )

    class _Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, payload, merge=False):
            self.writes.append((ref, dict(payload), merge))
            ref.set(payload, merge=merge)

    transaction = _Transaction()
    claim = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "recorded_base_version_id": "base-v1",
        "retry_queued_at": "2026-08-16T00:02:00+00:00",
        "retry_lease_expires_at": "2026-08-16T00:04:30+00:00",
        "retry_attempt_token": "repair-1",
    }
    first = corrections._claim_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **claim,
    )
    duplicate = corrections._claim_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **{**claim, "retry_attempt_token": "repair-2"},
    )

    assert first == "reservation_pending"
    assert duplicate == "already_queued"
    assert audit_ref.value["retry_attempt_token"] == "repair-1"
    assert len(transaction.writes) == 1

    stale_finish = corrections._finish_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="repair-2",
        audit_update={"status": "applied"},
    )
    assert stale_finish == "stale_attempt"
    assert audit_ref.value["retry_lease_expires_at"] == "2026-08-16T00:04:30+00:00"

    stale_failure = corrections._record_failed_correction_attempt_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="repair-2",
        audit_update={"status": "direct_apply_failed", "retry_lease_expires_at": None},
        correction_state={"correction_id": correction_id, "status": "direct_apply_failed"},
    )
    stale_revocation = corrections._finalize_revoked_correction_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        expected_retry_attempt_token="repair-2",
        finalized_at="2026-08-16T00:03:00+00:00",
    )
    assert stale_failure == "stale_attempt"
    assert stale_revocation["outcome"] == "stale_attempt"
    assert audit_ref.value["status"] == "canonical_pending"
    assert audit_ref.value["retry_lease_expires_at"] == "2026-08-16T00:04:30+00:00"

    audit_ref.value.update(
        {
            "canonical_publication_state": "completed",
            "canonical_publication_confirmed": True,
            "downstream_work": {
                "proposal": {"required": True, "status": "completed"},
                "canonical_event": {"required": True, "status": "completed"},
                "propagation": {"required": False, "status": "completed"},
            },
        }
    )
    current_finish = corrections._finish_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="repair-1",
        audit_update={"status": "applied", "retry_lease_expires_at": None},
    )
    assert current_finish == "finished"
    assert audit_ref.value["status"] == "applied"
    assert len(transaction.writes) == 2


def test_initial_queued_correction_reclaims_expired_lost_background_task_but_not_live_lease():
    correction_id = "initial-correction"
    conversation_ref = _MutableCorrectionAuditRef({"active_summary_version_id": "base-v1"})
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "queued",
            "source": "ios",
            "retry_attempt_token": "initial-attempt",
            "retry_lease_expires_at": "2026-08-16T00:03:00+00:00",
        }
    )

    class _Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, payload, merge=False):
            self.writes.append((ref, dict(payload), merge))
            ref.set(payload, merge=merge)

    transaction = _Transaction()
    claim = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "recorded_base_version_id": "base-v1",
        "retry_queued_at": "2026-08-16T00:02:00+00:00",
        "retry_lease_expires_at": "2026-08-16T00:05:00+00:00",
        "retry_attempt_token": "reclaimed-attempt",
        "source": "ios",
    }

    live = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **claim,
    )
    assert live == "already_queued"
    assert transaction.writes == []

    audit_ref.value["retry_lease_expires_at"] = "2026-08-16T00:01:59+00:00"
    reclaimed = corrections._claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **claim,
    )
    assert reclaimed == "claimed"
    assert len(transaction.writes) == 1
    assert audit_ref.value["status"] == "retry_queued"
    assert audit_ref.value["retry_attempt_token"] == "reclaimed-attempt"


def test_revoked_consent_after_source_commit_is_terminal_failed_until_required_receipts_exist(monkeypatch):
    correction_id = "revoked-after-source"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1", "is_active": False},
                {
                    "id": "corrected-v2",
                    "correction_id": correction_id,
                    "based_on_version_id": "base-v1",
                    "is_active": True,
                },
            ],
            "correction_state": {"correction_id": correction_id, "status": "canonical_pending"},
            "enrichment_state": {
                "trace_id": f"correction:conv-1:{correction_id}",
                "status": "writeback_pending_canonical",
                "canonical_status": "pending",
            },
        }
    )
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "trace_id": f"correction:conv-1:{correction_id}",
            "status": "canonical_pending",
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-1",
        }
    )

    class Transaction:
        def set(self, ref, payload, merge=False):
            ref.set(payload, merge=merge)

    result = corrections._finalize_revoked_correction_in_transaction(
        Transaction(),
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        expected_retry_attempt_token="attempt-1",
        finalized_at="2026-08-16T00:10:00+00:00",
    )

    assert result["audit"]["status"] == "consent_revoked"
    assert result["audit"]["failure_code"] == "consent_revoked_after_source_apply"
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))
    receipt = corrections._correction_receipt(
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        conversation=conversation_ref.value,
    )
    assert receipt.status == "consent_revoked"
    assert receipt.failure_code == "consent_revoked_after_source_apply"


def test_exact_receipt_poll_reclaims_stale_initial_queue_but_not_live_lease(monkeypatch):
    correction_id = "initial-receipt-recovery"
    conversation = _conversation()
    audit = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "status": "queued",
        "retry_attempt_token": "initial-attempt",
        "retry_lease_expires_at": "2020-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(corrections, "_read_correction_audit", lambda uid, cid, corr_id: dict(audit))
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))
    recovery_calls = []

    async def recover(**kwargs):
        recovery_calls.append(kwargs)

    monkeypatch.setattr(corrections, "_retry_failed_conversation_correction", recover)
    background = _CorrectionBackgroundTasks()

    stale_receipt = asyncio.run(
        corrections.get_conversation_correction_receipt(
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=background,
            uid="owner-1",
        )
    )
    assert stale_receipt.status == "queued"
    assert len(recovery_calls) == 1
    assert recovery_calls[0]["uid"] == "owner-1"
    assert recovery_calls[0]["background_tasks"] is background

    audit["retry_lease_expires_at"] = "2999-01-01T00:00:00+00:00"
    asyncio.run(
        corrections.get_conversation_correction_receipt(
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=background,
            uid="owner-1",
        )
    )
    assert len(recovery_calls) == 1


def test_correction_terminal_sla_caps_provider_and_matches_client_budget_with_margin():
    assert corrections._bounded_correction_provider_timeout("45") == 45.0
    assert corrections._bounded_correction_provider_timeout("999") == 120.0
    assert corrections._bounded_correction_provider_timeout("nan") == 45.0
    assert corrections.CORRECTION_TERMINAL_BOUND_SECONDS == 150.0
    assert corrections.CORRECTION_END_TO_END_DEADLINE_SECONDS == 145.0
    assert corrections.CORRECTION_CLIENT_POLL_BUDGET_SECONDS == 330.0
    assert corrections.CORRECTION_RETRY_LEASE_SECONDS == 150.0

    client_source = (
        _backend_path.parent / "app" / "lib" / "backend" / "http" / "api" / "conversations.dart"
    ).read_text(encoding="utf-8")
    assert "conversationCorrectionBackendTerminalBound = Duration(seconds: 150)" in client_source
    assert "conversationCorrectionSubmitBudget = Duration(seconds: 30)" in client_source
    assert "conversationCorrectionClientPollMargin = Duration(seconds: 30)" in client_source
    assert "conversationCorrectionClientPollBudget = Duration(seconds: 330)" in client_source
    assert "final stopwatch = Stopwatch()..start()" in client_source
    assert "requestTimeout: requestTimeout" in client_source


def test_canonical_failure_after_source_cas_stays_pending_then_retry_reconciles_without_second_provider_or_source_write(
    monkeypatch,
):
    correction_id = "canonical-pending-correction"
    trace_id = f"correction:conv-1:{correction_id}"
    conversation = {
        **_conversation(),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "is_active": True}],
    }
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "trace_id": trace_id,
            "status": "processing",
            "source": "ios",
            "correction_text": "Correct the attribution.",
            "summary_context": {},
            "current_summary": _conversation()["structured"],
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-1",
            "created_at": "2026-08-16T00:00:00+00:00",
            "events": [],
        }
    )
    provider_calls = []
    source_cas_writes = []
    canonical_calls = []
    downstream_calls = []

    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        corrections,
        "_persist_correction_audit",
        lambda uid, cid, corr_id, payload: audit_ref.value.update(payload),
    )

    def claim_reconciliation(**kwargs):
        audit_ref.value.update(
            {
                "status": "canonical_pending",
                "retry_attempt_token": kwargs["retry_attempt_token"],
                "retry_lease_expires_at": kwargs["retry_lease_expires_at"],
            }
        )
        return "claimed"

    def finish_reconciliation(**kwargs):
        if audit_ref.value.get("retry_attempt_token") != kwargs["retry_attempt_token"]:
            return "stale_attempt"
        audit_ref.value.update(kwargs["audit_update"])
        return "finished"

    monkeypatch.setattr(corrections, "_claim_canonical_reconciliation", claim_reconciliation)
    monkeypatch.setattr(corrections, "_finish_canonical_reconciliation", finish_reconciliation)

    def append_event(uid, cid, corr_id, event):
        audit_ref.value.setdefault("events", []).append(event)

    monkeypatch.setattr(corrections, "_append_correction_event", append_event)
    monkeypatch.setattr(
        corrections,
        "_update_conversation_correction_state",
        lambda uid, cid, payload: conversation.update(payload),
    )

    async def generate(**kwargs):
        provider_calls.append(kwargs["correction_id"])
        return {"title": "Corrected", "overview": "[Ella] Corrected.", "category": "other"}

    async def apply(**kwargs):
        canonical_calls.append(kwargs["correction_id"])
        if not source_cas_writes:
            source_cas_writes.append("corrected-v2")
            conversation["summary_versions"].append(
                {
                    "id": "corrected-v2",
                    "title": "Corrected",
                    "overview": "[Ella] Corrected.",
                    "category": "other",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                    "is_active": True,
                }
            )
            conversation["active_summary_version_id"] = "corrected-v2"
            conversation["enrichment_state"] = {
                "trace_id": trace_id,
                "status": "writeback_pending_canonical",
                "canonical_status": "failed",
            }
            raise corrections.CanonicalSummaryWriteUnconfirmedError("canonical_write_unconfirmed")
        conversation["enrichment_state"] = {
            "trace_id": trace_id,
            "status": "writeback_applied",
            "canonical_status": "completed",
        }
        return {
            "status": "ok",
            "active_summary_version_id": "corrected-v2",
            "canonical_confirmed": True,
            "idempotent_replay": True,
        }

    async def post_side_effects(**kwargs):
        downstream_calls.append(kwargs["correction_id"])
        return "proposal-1"

    monkeypatch.setattr(corrections, "_generate_corrected_summary", generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", apply)
    monkeypatch.setattr(corrections, "_run_post_source_correction_side_effects", post_side_effects)

    pending = asyncio.run(
        corrections._run_direct_correction_apply(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            trace_id=trace_id,
            request=corrections.ConversationCorrectionRequest(correction_text="Correct the attribution."),
            structured=_conversation()["structured"],
            transcript="Speaker: retained transcript",
            segment_count=1,
            submitted_at="2026-08-16T00:00:00+00:00",
            active_summary_version_id="base-v1",
            retry_attempt_token="attempt-1",
        )
    )
    assert pending.status == "canonical_pending"
    assert pending.queued is True
    assert audit_ref.value["status"] == "canonical_pending"
    assert source_cas_writes == ["corrected-v2"]
    assert downstream_calls == []

    applied = asyncio.run(
        corrections._retry_failed_conversation_correction(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=_CorrectionBackgroundTasks(),
        )
    )
    assert applied.status == "applied"
    assert applied.proposal_id == "proposal-1"
    assert provider_calls == [correction_id]
    assert source_cas_writes == ["corrected-v2"]
    assert canonical_calls == [correction_id, correction_id]
    assert downstream_calls == [correction_id]
    assert audit_ref.value["status"] == "applied"


def test_receipt_reconciles_stale_failure_to_applied_only_after_all_durable_receipts(monkeypatch):
    correction_id = "canonical-receipt"
    trace_id = f"correction:conv-1:{correction_id}"
    conversation = {
        **_conversation(),
        "active_summary_version_id": "corrected-v2",
        "summary_versions": [
            {"id": "base-v1", "title": "Before", "is_active": False},
            {
                "id": "corrected-v2",
                "title": "After",
                "based_on_version_id": "base-v1",
                "correction_id": correction_id,
                "is_active": True,
            },
        ],
        "enrichment_state": {
            "trace_id": trace_id,
            "status": "writeback_applied",
            "canonical_status": "completed",
            "updated_at": "2026-08-16T00:04:00+00:00",
        },
    }
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "trace_id": trace_id,
            "status": "direct_apply_failed",
            "active_summary_version_id": "base-v1",
            "downstream_work": {
                "proposal": {"required": True, "status": "completed"},
                "canonical_event": {"required": True, "status": "completed"},
                "propagation": {"required": False, "status": "completed"},
            },
        }
    )
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))

    receipt = corrections._correction_receipt(
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        conversation=conversation,
    )
    assert receipt.status == "applied"
    assert receipt.applied_at.isoformat() == "2026-08-16T00:04:00+00:00"
    assert receipt.after_version_id == "corrected-v2"


def test_source_correction_precedes_downstream_side_effects_and_duplicate_replay_is_fenced(monkeypatch):
    conversation = {
        **_conversation(),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "is_active": True}],
    }
    audit_ref = _MutableCorrectionAuditRef({})
    order = []
    monkeypatch.setattr(corrections, "DIRECT_CORRECTION_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(corrections.conversations_db, "bootstrap_summary_versioning_update", lambda value: {})
    monkeypatch.setattr(
        corrections.conversations_db, "update_conversation", lambda uid, cid, update: conversation.update(update)
    )

    async def generate(**kwargs):
        order.append("source_generate")
        return {"title": "Corrected", "overview": "[Ella] Corrected.", "category": "other"}

    async def apply(**kwargs):
        order.append("source_apply")
        conversation["active_summary_version_id"] = "corrected-v2"
        return {"status": "ok", "active_summary_version_id": "corrected-v2", "canonical_confirmed": True}

    monkeypatch.setattr(corrections, "_generate_corrected_summary", generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", apply)
    monkeypatch.setattr(
        corrections,
        "_create_summary_correction_proposal",
        lambda **kwargs: order.append("proposal") or "proposal-1",
    )

    async def emit(**kwargs):
        order.append("canonical")
        audit_ref.value["canonical_event_completed"] = True

    async def propagate(**kwargs):
        order.append("propagation")
        audit_ref.value["propagation_completed"] = True

    monkeypatch.setattr(corrections, "_emit_canonical_correction_event", emit)
    monkeypatch.setattr(corrections, "_run_correction_propagation_for_submission", propagate)

    result = asyncio.run(
        corrections._submit_conversation_correction(
            "conv-1",
            corrections.ConversationCorrectionRequest(correction_text="Correct the attribution."),
            background_tasks=None,
            uid="owner-1",
        )
    )
    assert result.status == "applied"
    assert order == ["source_generate", "source_apply", "proposal", "canonical", "propagation"]

    asyncio.run(
        corrections._run_post_source_correction_side_effects(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=result.correction_id,
            trace_id=result.trace_id,
            request=corrections.ConversationCorrectionRequest(correction_text="Correct the attribution."),
            source_conversation=conversation,
            structured=_conversation()["structured"],
            transcript="Speaker: retained transcript",
            segment_count=1,
            submitted_at="2026-08-15T22:13:10+00:00",
            active_summary_version_id="base-v1",
        )
    )
    assert order == ["source_generate", "source_apply", "proposal", "canonical", "propagation"]


def test_revoked_consent_terminalizes_background_retry_and_receipt_recovery_without_provider_egress(monkeypatch):
    correction_id = "revoked-correction"
    audit = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "trace_id": f"correction:conv-1:{correction_id}",
        "status": "queued",
        "source": "ios",
        "correction_text": "Retained private transcript correction.",
        "summary_context": {},
        "current_summary": _conversation()["structured"],
        "active_summary_version_id": "base-v1",
        "retry_attempt_token": "attempt-1",
        "retry_lease_expires_at": "2020-01-01T00:00:00+00:00",
    }
    conversation = {
        **_conversation(),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "is_active": True}],
        "correction_state": {"correction_id": correction_id, "status": "queued"},
    }
    provider_calls = []

    def revoked(uid):
        raise HTTPException(status_code=403, detail={"code": "ai_consent_required", "decision": "revoked"})

    def finalize(**kwargs):
        assert kwargs["uid"] == "owner-1"
        assert kwargs["correction_id"] == correction_id
        audit.update(
            {
                "status": "consent_revoked",
                "pending": False,
                "failure_code": "ai_consent_required",
                "retry_lease_expires_at": None,
            }
        )
        return {"outcome": "finalized", "audit": dict(audit)}

    monkeypatch.setattr(corrections, "require_current_ai_consent", revoked)
    monkeypatch.setattr(corrections, "_finalize_revoked_correction", finalize)
    monkeypatch.setattr(corrections, "_read_correction_audit", lambda uid, cid, corr_id: dict(audit))
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("revoked consent must prevent retained transcript egress")

    monkeypatch.setattr(corrections, "_generate_corrected_summary", provider)

    background_result = asyncio.run(
        corrections._run_direct_correction_apply(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            trace_id=audit["trace_id"],
            request=corrections.ConversationCorrectionRequest(correction_text=audit["correction_text"]),
            structured=_conversation()["structured"],
            transcript="retained private transcript",
            segment_count=1,
            submitted_at="2026-08-16T00:00:00+00:00",
            active_summary_version_id="base-v1",
            retry_attempt_token="attempt-1",
        )
    )
    assert background_result.status == "consent_revoked"

    audit.update({"status": "direct_apply_failed", "retry_lease_expires_at": None})
    explicit_result = asyncio.run(
        corrections._retry_failed_conversation_correction(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=_CorrectionBackgroundTasks(),
        )
    )
    assert explicit_result.status == "consent_revoked"

    audit.update({"status": "queued", "retry_lease_expires_at": "2020-01-01T00:00:00+00:00"})
    receipt = asyncio.run(
        corrections.get_conversation_correction_receipt(
            conversation_id="conv-1",
            correction_id=correction_id,
            background_tasks=_CorrectionBackgroundTasks(),
            uid="owner-1",
        )
    )
    assert receipt.status == "consent_revoked"
    assert receipt.failure_code == "ai_consent_required"
    assert provider_calls == []


def test_initial_submission_transaction_is_stable_across_crash_and_response_loss():
    class OptionalRef:
        def __init__(self, value=None):
            self.value = value

        def get(self, transaction=None):
            return SimpleNamespace(exists=self.value is not None, to_dict=lambda: dict(self.value or {}))

        def set(self, payload, merge=False):
            if merge and self.value is not None:
                self.value.update(payload)
            else:
                self.value = dict(payload)

    class Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, payload, merge=False):
            self.writes.append((ref, dict(payload), merge))
            ref.set(payload, merge=merge)

    correction_id = "1ea6c404-12e1-4c64-b752-d46ad3e06fb6"
    conversation_ref = OptionalRef({"active_summary_version_id": "base-v1"})
    audit_ref = OptionalRef()
    transaction = Transaction()
    request = corrections.ConversationCorrectionRequest(
        correction_id=correction_id,
        correction_text="Correct retained attribution.",
    )
    audit_payload = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "trace_id": f"correction:conv-1:{correction_id}",
        "status": "queued",
        "correction_text": request.correction_text,
        "source": request.source,
        "summary_context": request.summary_context.model_dump(),
        "active_summary_version_id": "base-v1",
        "request_fingerprint": corrections._correction_request_fingerprint(request),
        "retry_attempt_token": "initial-attempt",
        "retry_lease_expires_at": "2026-08-16T00:02:30+00:00",
    }
    kwargs = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "expected_active_summary_version_id": "base-v1",
        "bootstrap_update": {},
        "correction_state": {"correction_id": correction_id, "status": "queued", "pending": True},
        "audit_payload": audit_payload,
    }

    created = corrections._claim_initial_correction_submission_in_transaction(
        transaction, conversation_ref, audit_ref, **kwargs
    )
    replay_after_lost_response = corrections._claim_initial_correction_submission_in_transaction(
        transaction, conversation_ref, audit_ref, **kwargs
    )

    assert created["outcome"] == "created"
    assert replay_after_lost_response["outcome"] == "replay"
    assert replay_after_lost_response["audit"]["correction_id"] == correction_id
    assert replay_after_lost_response["audit"]["retry_attempt_token"] == "initial-attempt"
    assert replay_after_lost_response["audit"]["retry_lease_expires_at"]
    assert len(transaction.writes) == 2


def test_end_to_end_deadline_covers_slow_runtime_resolution_and_slow_canonical_writer(monkeypatch):
    correction_id = "deadline-correction"
    trace_id = f"correction:conv-1:{correction_id}"
    conversation = {
        **_conversation(),
        "active_summary_version_id": "base-v1",
        "summary_versions": [{"id": "base-v1", "is_active": True}],
    }
    audit = {
        "uid": "owner-1",
        "conversation_id": "conv-1",
        "correction_id": correction_id,
        "trace_id": trace_id,
        "status": "processing",
        "active_summary_version_id": "base-v1",
        "retry_attempt_token": "deadline-attempt",
    }
    monkeypatch.setattr(corrections, "CORRECTION_END_TO_END_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(corrections, "_read_correction_audit", lambda uid, cid, corr_id: dict(audit))

    async def slow_resolver(uid, config):
        await asyncio.sleep(0.05)
        return config

    monkeypatch.setattr(corrections, "summary_provider_config_for_uid", slow_resolver)
    runtime_timeout = asyncio.run(
        corrections._run_direct_correction_apply(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            trace_id=trace_id,
            request=corrections.ConversationCorrectionRequest(correction_text="Correct attribution."),
            structured=_conversation()["structured"],
            transcript="retained transcript",
            segment_count=1,
            submitted_at="2026-08-16T00:00:00+00:00",
            active_summary_version_id="base-v1",
            retry_attempt_token="deadline-attempt",
        )
    )
    assert runtime_timeout.status == "direct_apply_failed"

    async def generated(**kwargs):
        return {"title": "Corrected", "overview": "[Ella] Corrected.", "category": "other"}

    async def slow_canonical(**kwargs):
        conversation["summary_versions"].append(
            {
                "id": "corrected-v2",
                "correction_id": correction_id,
                "based_on_version_id": "base-v1",
                "is_active": True,
            }
        )
        conversation["active_summary_version_id"] = "corrected-v2"
        conversation["enrichment_state"] = {
            "trace_id": trace_id,
            "status": "writeback_pending_canonical",
            "canonical_status": "pending",
        }
        await asyncio.sleep(0.05)
        return {"canonical_confirmed": True, "active_summary_version_id": "corrected-v2"}

    monkeypatch.setattr(corrections, "_generate_corrected_summary", generated)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", slow_canonical)
    canonical_timeout = asyncio.run(
        corrections._run_direct_correction_apply(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            trace_id=trace_id,
            request=corrections.ConversationCorrectionRequest(correction_text="Correct attribution."),
            structured=_conversation()["structured"],
            transcript="retained transcript",
            segment_count=1,
            submitted_at="2026-08-16T00:00:00+00:00",
            active_summary_version_id="base-v1",
            retry_attempt_token="deadline-attempt",
        )
    )
    assert canonical_timeout.status == "canonical_pending"
    assert canonical_timeout.status != "direct_apply_failed"
    assert [version["id"] for version in conversation["summary_versions"]].count("corrected-v2") == 1

    async def assert_blocking_deadline_returns_without_waiting_for_worker():
        blocking_started = time.monotonic()
        with pytest.raises(TimeoutError):
            await corrections._CorrectionDeadline(budget_seconds=0.01).run_blocking(
                lambda: time.sleep(0.05),
            )
        assert time.monotonic() - blocking_started < 0.04

    asyncio.run(assert_blocking_deadline_returns_without_waiting_for_worker())


def test_expired_publication_lease_recovers_ack_loss_with_exact_idempotent_replay(monkeypatch):
    correction_id = "late-canonical"
    trace_id = f"correction:conv-1:{correction_id}"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "id": "conv-1",
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1", "is_active": False},
                {
                    "id": "corrected-v2",
                    "correction_id": correction_id,
                    "based_on_version_id": "base-v1",
                    "is_active": True,
                },
            ],
            "enrichment_state": {
                "status": "writeback_pending_canonical",
                "pending": True,
                "trace_id": trace_id,
                "canonical_status": "pending",
            },
            "correction_state": {"correction_id": correction_id, "status": "canonical_pending"},
        }
    )
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "trace_id": trace_id,
            "status": "canonical_pending",
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-a",
            "retry_lease_expires_at": "2999-01-01T00:00:00+00:00",
            "operation_deadline_at": "2026-08-16T01:05:00+00:00",
            "attempt_count": 1,
            "attempt_budget": 8,
        }
    )

    class Transaction:
        def set(self, ref, payload, merge=False):
            ref.set(payload, merge=merge)

    def claim_publication(**kwargs):
        token = kwargs["retry_attempt_token"]
        return corrections._claim_canonical_publication_in_transaction(
            Transaction(),
            audit_ref,
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            retry_attempt_token=token,
            claimed_at=("2026-08-16T01:00:00+00:00" if token == "attempt-a" else "2026-08-16T01:03:00+00:00"),
        )

    def complete_publication(**kwargs):
        return corrections._complete_canonical_publication_in_transaction(
            Transaction(),
            audit_ref,
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            retry_attempt_token=kwargs["retry_attempt_token"],
            confirmed=kwargs["confirmed"],
            completed_at="2026-08-16T01:03:00+00:00",
        )

    writer_started = threading.Event()
    release_writer = threading.Event()
    publications = []
    canonical_commits = []
    authority = {"generation": 0, "attempt_token": ""}
    authority_lock = threading.Lock()

    def reserve_at_sink(fence):
        with authority_lock:
            generation = int(fence["generation"])
            token = str(fence["attempt_token"])
            if generation > authority["generation"] or (
                generation == authority["generation"] and token == authority["attempt_token"]
            ):
                authority.update(generation=generation, attempt_token=token)
            return {
                "ok": authority == {"generation": generation, "attempt_token": token},
                **authority,
                "scope": fence["scope"],
            }

    def canonical_writer(*args, **kwargs):
        writer_started.set()
        assert release_writer.wait(timeout=1)
        fence = kwargs["publication_fence"]
        generation = int(fence["generation"])
        publications.append(generation)
        with authority_lock:
            authorized = authority == {
                "generation": generation,
                "attempt_token": str(fence["attempt_token"]),
            }
        if authorized:
            canonical_commits.append(generation)
            return {"ok": True, "inserted": 1, "duplicates": 0}
        return {"ok": False, "inserted": 0, "duplicates": 0, "stale": 1}

    monkeypatch.setattr(corrections, "require_current_ai_consent", lambda uid: None)
    monkeypatch.setattr(corrections, "_claim_canonical_publication", claim_publication)
    monkeypatch.setattr(corrections, "_complete_canonical_publication", complete_publication)
    monkeypatch.setattr(corrections, "_read_correction_audit", lambda *args: dict(audit_ref.value))
    monkeypatch.setattr(corrections, "reserve_omi_canonical_publication", reserve_at_sink)
    monkeypatch.setattr(
        corrections,
        "_claim_canonical_reconciliation_transaction",
        lambda transaction, conversation_arg, audit_arg, **kwargs: corrections._claim_canonical_reconciliation_in_transaction(
            Transaction(), conversation_ref, audit_ref, **kwargs
        ),
    )
    monkeypatch.setattr(
        corrections,
        "_confirm_canonical_publication_reservation_transaction",
        lambda transaction, audit_arg, **kwargs: corrections._confirm_canonical_publication_reservation_in_transaction(
            Transaction(), audit_ref, **kwargs
        ),
    )
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", lambda uid, cid: conversation_ref.value)

    async def publish(token):
        return await summary_writeback.write_conversation_summary(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            summary_kind="corrected_enriched",
            trace_id=trace_id,
            require_canonical=True,
            canonical_writer=canonical_writer,
            canonical_egress_guard=lambda: corrections._guard_canonical_publication(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id=correction_id,
                retry_attempt_token=token,
            ),
            canonical_egress_completion=lambda confirmed: corrections._complete_canonical_publication(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id=correction_id,
                retry_attempt_token=token,
                confirmed=confirmed,
            ),
            correction_source_compare_and_set=lambda update: "updated",
        )

    async def scenario():
        stale_task = asyncio.create_task(publish("attempt-a"))
        for _ in range(100):
            if writer_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert writer_started.is_set()
        stale_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stale_task

        audit_ref.value["retry_lease_expires_at"] = "2026-08-16T01:00:01+00:00"
        blocked_reclaim = corrections._claim_canonical_reconciliation(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            recorded_base_version_id="base-v1",
            retry_queued_at="2026-08-16T01:00:20+00:00",
            retry_lease_expires_at="2026-08-16T01:04:30+00:00",
            retry_attempt_token="attempt-b",
        )
        assert blocked_reclaim == "already_queued"
        assert (
            corrections._correction_work_lease_is_reclaimable(
                audit_ref.value, now=datetime(2026, 8, 16, 1, 0, 20, tzinfo=timezone.utc)
            )
            is False
        )

        assert corrections._correction_work_lease_is_reclaimable(
            audit_ref.value, now=datetime(2026, 8, 16, 1, 2, tzinfo=timezone.utc)
        )
        expired_reclaim = corrections._claim_canonical_reconciliation(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            recorded_base_version_id="base-v1",
            retry_queued_at="2026-08-16T01:02:00+00:00",
            retry_lease_expires_at="2026-08-16T01:04:30+00:00",
            retry_attempt_token="attempt-b",
        )
        assert expired_reclaim == "claimed"
        assert authority == {"generation": 2, "attempt_token": "attempt-b"}

        # The expired generation reaches the production writer first after B's
        # reclaim has returned, but the sink reservation rejects it.
        release_writer.set()
        for _ in range(100):
            if publications:
                break
            await asyncio.sleep(0.001)
        assert publications == [1]
        assert canonical_commits == []
        replay = await publish("attempt-b")
        assert replay["canonical_confirmed"] is True

    asyncio.run(scenario())
    assert publications == [1, 2]
    assert canonical_commits == [2]


def test_crash_after_publication_claim_before_remote_write_reclaims_after_bounded_lease():
    correction_id = "crash-before-write"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1"},
                {
                    "id": "corrected-v2",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                },
            ],
        }
    )
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "canonical_pending",
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-a",
            "retry_lease_expires_at": "2026-08-16T01:04:00+00:00",
            "operation_deadline_at": "2026-08-16T01:05:00+00:00",
            "attempt_count": 1,
            "attempt_budget": 8,
        }
    )

    class Transaction:
        def set(self, ref, payload, merge=False):
            ref.set(payload, merge=merge)

    transaction = Transaction()
    first_claim = corrections._claim_canonical_publication_in_transaction(
        transaction,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-a",
        claimed_at="2026-08-16T01:00:00+00:00",
    )
    live_reclaim = corrections._claim_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_queued_at="2026-08-16T01:00:20+00:00",
        retry_lease_expires_at="2026-08-16T01:02:50+00:00",
        retry_attempt_token="attempt-b",
    )
    expired_reclaim = corrections._claim_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_queued_at="2026-08-16T01:00:31+00:00",
        retry_lease_expires_at="2026-08-16T01:03:01+00:00",
        retry_attempt_token="attempt-b",
    )
    second_claim = corrections._claim_canonical_publication_in_transaction(
        transaction,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-b",
        claimed_at="2026-08-16T01:00:32+00:00",
    )
    stale_ack = corrections._complete_canonical_publication_in_transaction(
        transaction,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-a",
        confirmed=True,
        completed_at="2026-08-16T01:00:33+00:00",
    )

    assert first_claim == "claimed"
    assert live_reclaim == "already_queued"
    assert expired_reclaim == "reservation_pending"
    assert second_claim == "claimed"
    assert stale_ack == "stale_attempt"
    assert audit_ref.value["canonical_publication_attempt_token"] == "attempt-b"


def test_revocation_at_final_canonical_and_downstream_boundaries_prevents_all_egress(monkeypatch):
    consent_checks = 0
    completion = []

    def revoke_after_claim(uid):
        nonlocal consent_checks
        consent_checks += 1
        if consent_checks == 2:
            raise HTTPException(status_code=403, detail={"code": "ai_consent_required"})

    monkeypatch.setattr(corrections, "require_current_ai_consent", revoke_after_claim)
    monkeypatch.setattr(corrections, "_claim_canonical_publication", lambda **kwargs: "claimed")
    monkeypatch.setattr(
        corrections,
        "_complete_canonical_publication",
        lambda **kwargs: completion.append(kwargs["confirmed"]) or "completed",
    )
    with pytest.raises(HTTPException):
        corrections._guard_canonical_publication(
            uid="owner-1",
            conversation_id="conv-1",
            correction_id="corr-1",
            retry_attempt_token="attempt-1",
        )
    assert completion == [False]

    downstream_writes = []
    consent_checks = 0
    monkeypatch.setattr(corrections, "_read_correction_audit", lambda *args: {})
    monkeypatch.setattr(
        corrections,
        "_create_summary_correction_proposal",
        lambda **kwargs: downstream_writes.append("proposal") or "proposal-1",
    )
    with pytest.raises(HTTPException):
        asyncio.run(
            corrections._run_post_source_correction_side_effects(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id="corr-1",
                trace_id="correction:conv-1:corr-1",
                request=corrections.ConversationCorrectionRequest(correction_text="Private correction."),
                source_conversation=_conversation(),
                structured=_conversation()["structured"],
                transcript="private related transcript",
                segment_count=1,
                submitted_at="2026-08-16T01:00:00+00:00",
                active_summary_version_id="base-v1",
            )
        )
    assert downstream_writes == []


def test_undo_cannot_overtake_canonical_pending_or_publication_inflight(monkeypatch):
    correction_id = "undo-fence"
    conversation = {
        "active_summary_version_id": "corrected-v2",
        "summary_versions": [
            {"id": "base-v1"},
            {
                "id": "corrected-v2",
                "based_on_version_id": "base-v1",
                "correction_id": correction_id,
            },
        ],
    }
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "canonical_pending",
            "canonical_publication_state": "inflight",
            "canonical_publication_confirmed": False,
            "downstream_work": {"proposal": {"required": True, "status": "pending"}},
        }
    )
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(corrections, "_audit_ref", lambda uid, cid, corr_id: audit_ref)
    monkeypatch.setattr(
        corrections,
        "_prepare_applied_propagation_rollbacks",
        lambda *args: pytest.fail("undo must stop before rollback preflight or mutation"),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(corrections.undo_conversation_correction("conv-1", correction_id, uid="owner-1"))

    assert excinfo.value.status_code == 409
    assert audit_ref.value["status"] == "canonical_pending"
    assert conversation["active_summary_version_id"] == "corrected-v2"


@pytest.mark.parametrize("failed_stage", ["proposal", "canonical_event", "propagation"])
def test_terminal_applied_is_fenced_after_source_cas_before_each_downstream_receipt(failed_stage):
    correction_id = "downstream-gate"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1"},
                {
                    "id": "corrected-v2",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                },
            ],
            "correction_state": {"correction_id": correction_id, "status": "canonical_pending"},
        }
    )
    downstream = {
        stage: {"required": True, "status": "pending" if stage == failed_stage else "completed"}
        for stage in ("proposal", "canonical_event", "propagation")
    }
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "finalizing",
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-1",
            "canonical_publication_state": "completed",
            "canonical_publication_confirmed": True,
            "downstream_work": downstream,
        }
    )

    class Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, payload, merge=False):
            self.writes.append((ref, payload, merge))
            ref.set(payload, merge=merge)

    transaction = Transaction()
    outcome = corrections._finish_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-1",
        audit_update={"status": "applied", "pending": False},
    )

    assert outcome == "downstream_incomplete"
    assert transaction.writes == []
    assert audit_ref.value["status"] == "finalizing"

    # A partial/corrupt durable plan must fail closed instead of letting
    # all([]) or a missing stage terminalize the receipt.
    audit_ref.value["downstream_work"].pop(failed_stage)
    outcome = corrections._finish_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_attempt_token="attempt-1",
        audit_update={"status": "applied", "pending": False},
    )
    assert outcome == "downstream_incomplete"
    assert transaction.writes == []


def test_multiple_lost_reclaimers_exhaust_one_persisted_attempt_budget():
    correction_id = "bounded-reclaimers"
    conversation_ref = _MutableCorrectionAuditRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1"},
                {
                    "id": "corrected-v2",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                },
            ],
            "correction_state": {"correction_id": correction_id, "status": "finalizing"},
        }
    )
    audit_ref = _MutableCorrectionAuditRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "finalizing",
            "active_summary_version_id": "base-v1",
            "retry_attempt_token": "attempt-1",
            "retry_lease_expires_at": "2026-08-16T01:01:00+00:00",
            "operation_deadline_at": "2026-08-16T01:10:00+00:00",
            "attempt_count": 1,
            "attempt_budget": 4,
            "canonical_publication_state": "completed",
            "canonical_publication_confirmed": True,
            "downstream_work": {
                "proposal": {
                    "required": True,
                    "status": "inflight",
                    "lease_expires_at": "2026-08-16T01:00:30+00:00",
                }
            },
        }
    )

    class Transaction:
        def set(self, ref, payload, merge=False):
            ref.set(payload, merge=merge)

    transaction = Transaction()
    for attempt_number in range(2, 5):
        claimed_at = f"2026-08-16T01:0{attempt_number}:00+00:00"
        outcome = corrections._claim_canonical_reconciliation_in_transaction(
            transaction,
            conversation_ref,
            audit_ref,
            uid="owner-1",
            conversation_id="conv-1",
            correction_id=correction_id,
            recorded_base_version_id="base-v1",
            retry_queued_at=claimed_at,
            retry_lease_expires_at=f"2026-08-16T01:0{attempt_number + 1}:00+00:00",
            retry_attempt_token=f"attempt-{attempt_number}",
        )
        assert outcome == "reservation_pending"
        audit_ref.value["downstream_work"]["proposal"].update(
            {
                "status": "inflight",
                "attempt_token": f"attempt-{attempt_number}",
                "lease_expires_at": claimed_at,
            }
        )

    exhausted = corrections._claim_canonical_reconciliation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        recorded_base_version_id="base-v1",
        retry_queued_at="2026-08-16T01:06:00+00:00",
        retry_lease_expires_at="2026-08-16T01:07:00+00:00",
        retry_attempt_token="attempt-5",
    )
    terminal = corrections._expire_correction_operation_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        expired_at="2026-08-16T01:06:00+00:00",
    )

    assert exhausted == "operation_exhausted"
    assert audit_ref.value["attempt_count"] == 4
    assert terminal["outcome"] == "expired"
    assert audit_ref.value["status"] == "reconciliation_failed"
    assert audit_ref.value["pending"] is False


def test_propagation_revalidates_before_related_transcript_read_and_proposal_write():
    source = {**_conversation(), "id": "source", "uid": "owner-1"}
    related = {**_conversation(), "id": "related", "uid": "owner-1"}
    guard_calls = 0
    proposals = []

    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 3:
            raise HTTPException(status_code=403, detail={"code": "ai_consent_required"})

    with pytest.raises(HTTPException):
        corrections.run_correction_propagation(
            uid="owner-1",
            source_conversation=source,
            correction_id="corr-1",
            trace_id="correction:source:corr-1",
            correction_text="Correct the retained attribution.",
            correction_type="identity",
            candidate_loader=lambda *args, **kwargs: [related],
            create_proposal=lambda **kwargs: proposals.append(kwargs) or {"created": True, "proposal": {}},
            min_confidence=0.0,
            egress_guard=guard,
        )
    assert proposals == []


def test_receipt_audit_identity_or_status_mismatch_fails_closed_without_echo(monkeypatch):
    mismatches = [
        ("uid", "other-owner"),
        ("conversation_id", "other-conversation"),
        ("correction_id", "other-correction"),
        ("trace_id", "correction:other:identity"),
        ("status", "invented_status"),
    ]
    for field, value in mismatches:
        audit = {
            "uid": "owner-secret",
            "conversation_id": "conversation-secret",
            "correction_id": "correction-secret",
            "trace_id": "correction:conversation-secret:correction-secret",
            "status": "queued",
        }
        audit[field] = value
        monkeypatch.setattr(corrections, "_read_correction_audit", lambda *args, current=audit: current)
        with pytest.raises(HTTPException) as error:
            corrections._correction_receipt(
                uid="owner-secret",
                conversation_id="conversation-secret",
                correction_id="correction-secret",
                conversation=_conversation(),
            )
        assert error.value.status_code == 404
        assert error.value.detail == "Correction not found"
        assert "owner-secret" not in str(error.value.detail)
        assert "conversation-secret" not in str(error.value.detail)
        assert "correction-secret" not in str(error.value.detail)


def test_n8n_submission_is_bounded_by_the_existing_monotonic_deadline(monkeypatch):
    observed_timeouts = []

    class SlowClient:
        def __init__(self, *, timeout):
            observed_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            await asyncio.sleep(0.2)
            raise AssertionError("stalled n8n request must be cancelled by the shared deadline")

    monkeypatch.setattr(corrections.httpx, "AsyncClient", SlowClient)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(
            corrections._submit_correction_to_n8n(
                uid="owner-1",
                conversation_id="conv-1",
                correction_id="corr-1",
                trace_id="correction:conv-1:corr-1",
                request=corrections.ConversationCorrectionRequest(correction_text="Private correction."),
                structured=_conversation()["structured"],
                transcript="private transcript",
                segment_count=1,
                deadline=corrections._CorrectionDeadline(budget_seconds=0.01),
            )
        )
    assert time.monotonic() - started < 0.1
    assert observed_timeouts and observed_timeouts[0] <= 0.01


class _TransactionalRef:
    def __init__(self, value=None):
        self.value = value

    def get(self, transaction=None):
        del transaction
        value = self.value
        return SimpleNamespace(exists=value is not None, to_dict=lambda: dict(value or {}))

    def set(self, payload, merge=False):
        if merge and self.value is not None:
            self.value.update(payload)
        else:
            self.value = dict(payload)


class _RecordingTransaction:
    def __init__(self):
        self.writes = []

    def set(self, ref, payload, merge=False):
        self.writes.append((ref, payload, merge))
        ref.set(payload, merge=merge)


def test_propagation_sink_transaction_rejects_expired_reclaimed_and_post_undo_publishers():
    correction_id = "propagation-fence"
    conversation_ref = _TransactionalRef(
        {
            "active_summary_version_id": "corrected-v2",
            "summary_versions": [
                {"id": "base-v1"},
                {
                    "id": "corrected-v2",
                    "based_on_version_id": "base-v1",
                    "correction_id": correction_id,
                },
            ],
        }
    )
    audit_ref = _TransactionalRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": correction_id,
            "status": "finalizing",
            "retry_attempt_token": "attempt-b",
            "operation_deadline_at": "2999-01-01T00:00:00+00:00",
            "attempt_count": 2,
            "attempt_budget": 8,
            "downstream_work": {
                "propagation": {
                    "required": True,
                    "status": "inflight",
                    "attempt_token": "attempt-b",
                    "lease_expires_at": "2999-01-01T00:00:00+00:00",
                }
            },
        }
    )
    proposal_ref = _TransactionalRef()
    proposal = corrections.Proposal.from_claims(
        session_claims={"profile_uid": "owner-1", "trace_id": "trace-1"},
        tool_name="omi_correction_propagation_propose",
        proposal_type="summary_correction",
        payload={"target": {"conversation_id": "related-1"}},
        idempotency_key="stable-related-effect",
        proposal_id="proposal-1",
    )

    stale = corrections._create_fenced_correction_proposal_in_transaction(
        _RecordingTransaction(),
        conversation_ref,
        audit_ref,
        proposal_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-a",
        stage="propagation",
        proposal=proposal,
        committed_at="2026-08-16T03:00:00+00:00",
    )
    assert stale == {"outcome": "stale_attempt"}
    assert proposal_ref.value is None

    expired = corrections._create_fenced_correction_proposal_in_transaction(
        _RecordingTransaction(),
        conversation_ref,
        audit_ref,
        proposal_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-b",
        stage="propagation",
        proposal=proposal,
        committed_at="3000-01-01T00:00:00+00:00",
    )
    assert expired == {"outcome": "stale_attempt"}
    assert proposal_ref.value is None

    created = corrections._create_fenced_correction_proposal_in_transaction(
        _RecordingTransaction(),
        conversation_ref,
        audit_ref,
        proposal_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-b",
        stage="propagation",
        proposal=proposal,
        committed_at="2026-08-16T03:00:00+00:00",
    )
    assert created["outcome"] == "created"
    assert proposal_ref.value["proposal_id"] == "proposal-1"

    conversation_ref.value["summary_versions"].append(
        {"id": "undo-v3", "based_on_version_id": "corrected-v2", "kind": "correction_undo"}
    )
    conversation_ref.value["active_summary_version_id"] = "undo-v3"
    proposal_after_undo = corrections.Proposal.from_claims(
        session_claims={"profile_uid": "owner-1", "trace_id": "trace-1"},
        tool_name="omi_correction_propagation_propose",
        proposal_type="summary_correction",
        payload={"target": {"conversation_id": "related-2"}},
        idempotency_key="late-related-effect",
        proposal_id="proposal-2",
    )
    rejected_after_undo = corrections._create_fenced_correction_proposal_in_transaction(
        _RecordingTransaction(),
        conversation_ref,
        audit_ref,
        _TransactionalRef(),
        uid="owner-1",
        conversation_id="conv-1",
        correction_id=correction_id,
        retry_attempt_token="attempt-b",
        stage="propagation",
        proposal=proposal_after_undo,
        committed_at="2026-08-16T03:00:00+00:00",
    )
    assert rejected_after_undo == {"outcome": "version_drift"}


def test_undo_terminal_transaction_is_atomic_and_exact_retry_repairs_legacy_pair():
    conversation_ref = _TransactionalRef(
        {
            "active_summary_version_id": "undo-v3",
            "summary_versions": [
                {"id": "base-v1"},
                {"id": "corrected-v2", "based_on_version_id": "base-v1", "correction_id": "corr-1"},
                {"id": "undo-v3", "based_on_version_id": "corrected-v2", "kind": "correction_undo"},
            ],
            "correction_state": {
                "correction_id": "corr-1",
                "status": "applied",
                "pending": False,
                "active_summary_version_id": "corrected-v2",
            },
        }
    )
    audit_ref = _TransactionalRef(
        {
            "uid": "owner-1",
            "conversation_id": "conv-1",
            "correction_id": "corr-1",
            "status": "undone",
            "undone_at": "2026-08-16T03:00:00+00:00",
        }
    )
    transaction = _RecordingTransaction()

    result = corrections._finalize_correction_undo_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        uid="owner-1",
        conversation_id="conv-1",
        correction_id="corr-1",
        corrected_version_id="corrected-v2",
        undo_version_id="undo-v3",
        reverted_count=2,
        related_target_count=2,
        undone_at="2026-08-16T03:00:00+00:00",
    )

    assert result == "finalized"
    assert len(transaction.writes) == 2
    assert audit_ref.value["status"] == "undone"
    assert audit_ref.value["undo_operation"]["status"] == "completed"
    assert conversation_ref.value["correction_state"]["status"] == "undone"
    assert conversation_ref.value["correction_state"]["active_summary_version_id"] == "undo-v3"


def test_hosted_source_ci_checks_out_and_receipts_exact_pull_request_head_with_scans_preserved():
    workflow = (_backend_path.parent / ".github" / "workflows" / "ella-ios-source-ci.yml").read_text(encoding="utf-8")
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert "ELLA_EXPECTED_HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert '[[ "$actual_head" == "$ELLA_EXPECTED_HEAD_SHA" ]]' in workflow
    assert "Immutable source head:" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert "refs/remotes/pull/" not in workflow
    assert "Diff and signing artifact scan" in workflow
    assert "secret_patterns=" in workflow
    assert "forbidden_artifact_paths=" in workflow
