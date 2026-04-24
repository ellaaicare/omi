import asyncio
import importlib.util
import sys
from unittest.mock import MagicMock
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.modules.setdefault("database._client", MagicMock())
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("database.memories", MagicMock())
sys.modules.setdefault("database.users", MagicMock())
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("firebase_admin", MagicMock())
sys.modules.setdefault("utils.other.endpoints", MagicMock())
sys.modules.setdefault("utils.notifications", MagicMock())
sys.modules.setdefault("utils.other.storage", MagicMock())
sys.modules.pop("ella.config", None)
sys.modules.setdefault("database.ella_contacts", MagicMock())

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_callbacks_path = Path(__file__).resolve().parents[2] / "ella" / "routers" / "callbacks.py"
_callbacks_spec = importlib.util.spec_from_file_location("ella_callbacks_test_module", _callbacks_path)
callbacks = importlib.util.module_from_spec(_callbacks_spec)
assert _callbacks_spec is not None and _callbacks_spec.loader is not None
_callbacks_spec.loader.exec_module(callbacks)


def test_update_conversation_summary_clears_stale_app_results(monkeypatch):
    captured = {}

    def fake_update_conversation(uid, conversation_id, update_data):
        captured["uid"] = uid
        captured["conversation_id"] = conversation_id
        captured["update_data"] = update_data

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", fake_update_conversation)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                title="Updated title",
                overview="[Ella] Updated overview with enough context to safely replace the prior summary.",
                emoji="🧠",
                category="personal",
            ),
            uid="user-123",
        )
    )

    assert result["status"] == "ok"
    assert captured["uid"] == "user-123"
    assert captured["conversation_id"] == "conv-123"
    assert captured["update_data"]["structured.title"] == "Updated title"
    assert (
        captured["update_data"]["structured.overview"]
        == "[Ella] Updated overview with enough context to safely replace the prior summary."
    )
    assert captured["update_data"]["structured.emoji"] == "🧠"
    assert captured["update_data"]["structured.category"] == "personal"
    assert captured["update_data"]["apps_results"] == []
    assert captured["update_data"]["plugins_results"] == []
    assert result["sanitizer_warnings"] == []


def test_update_conversation_summary_adds_missing_ella_prefix(monkeypatch):
    captured = {}

    def fake_update_conversation(uid, conversation_id, update_data):
        captured["update_data"] = update_data

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", fake_update_conversation)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                overview="Updated overview with enough useful context to safely replace the prior summary.",
            ),
            uid="user-123",
        )
    )

    assert captured["update_data"]["structured.overview"].startswith("[Ella] ")
    assert result["sanitizer_warnings"] == ["overview_missing_ella_prefix"]


def test_update_conversation_summary_removes_raw_scanner_audit_sentence(monkeypatch):
    captured = {}

    def fake_update_conversation(uid, conversation_id, update_data):
        captured["update_data"] = update_data

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", fake_update_conversation)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                overview=(
                    "[Ella] The conversation centered on testing Omi device reliability, privacy, and caregiver "
                    "alert behavior while nearby media played in the background. "
                    "Scanner picked up 416 escalations across cognitive, emotional, health, and media categories."
                ),
            ),
            uid="user-123",
        )
    )

    assert "416 escalations" not in captured["update_data"]["structured.overview"]
    assert result["sanitizer_warnings"] == ["removed_raw_scanner_audit"]


def test_update_conversation_summary_rejects_internal_debug_jargon(monkeypatch):
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", MagicMock())

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conv-123",
                callbacks.ConversationSummaryUpdate(
                    overview=(
                        "[Ella] The transcript mostly covered device testing and family conversation. "
                        "The n8n write-back routing selected a model_runner retry."
                    ),
                ),
                uid="user-123",
            )
        )

    assert excinfo.value.status_code == 422
    assert any("internal debug or routing jargon" in violation for violation in excinfo.value.detail["violations"])
    callbacks.conversations_db.update_conversation.assert_not_called()


def test_update_conversation_summary_rejects_invalid_category():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conv-123",
                callbacks.ConversationSummaryUpdate(category="definitely-not-a-real-category"),
                uid="user-123",
            )
        )

    assert excinfo.value.status_code == 400
    assert "Invalid category" in excinfo.value.detail


def test_get_conversation_data_returns_transcript_payload(monkeypatch):
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "transcript_segments": [
                {"is_user": True, "text": "Can you reprocess this?"},
                {"speaker": "Other", "text": "Yes, with the full transcript."},
            ],
            "structured": {
                "title": "Original title",
                "overview": "Original overview",
                "emoji": "🧠",
                "category": callbacks.CategoryEnum.technology,
            },
            "started_at": "2026-04-10T10:00:00Z",
            "finished_at": "2026-04-10T10:05:00Z",
        },
    )

    result = asyncio.run(callbacks.get_conversation_data("conv-123", uid="user-123"))

    assert result["conversation_id"] == "conv-123"
    assert result["uid"] == "user-123"
    assert result["segment_count"] == 2
    assert result["transcript"] == "User: Can you reprocess this?\n\nOther: Yes, with the full transcript."
    assert result["structured"]["title"] == "Original title"
    assert result["structured"]["overview"] == "Original overview"
    assert result["structured"]["emoji"] == "🧠"
    assert result["structured"]["category"] == "technology"
    assert result["started_at"] == "2026-04-10T10:00:00Z"
    assert result["finished_at"] == "2026-04-10T10:05:00Z"


def test_get_conversation_data_requires_uid():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(callbacks.get_conversation_data("conv-123"))

    assert excinfo.value.status_code == 400
    assert "uid query parameter required" in excinfo.value.detail


def test_get_conversation_data_404s_when_missing(monkeypatch):
    monkeypatch.setattr(callbacks.conversations_db, "get_conversation", lambda uid, conversation_id: None)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(callbacks.get_conversation_data("missing-conv", uid="user-123"))

    assert excinfo.value.status_code == 404
    assert "Conversation not found" in excinfo.value.detail


def test_update_conversation_summary_records_version_and_marks_correction_applied(monkeypatch):
    captured = {}
    audit_updates = []

    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "structured": {
                "title": "Original title",
                "overview": "[Ella] Original overview with enough detail to preserve.",
                "emoji": "🧠",
                "category": callbacks.CategoryEnum.health,
            },
            "correction_state": {
                "correction_id": "corr-123",
                "source": "ios",
                "submitted_at": "2026-04-22T17:00:00+00:00",
            },
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "build_summary_version_update",
        lambda conversation, **kwargs: {
            "summary_versions": [
                {"id": "legacy-v1", "title": "Original title"},
                {"id": "corr-v2", "title": kwargs["next_structured"]["title"]},
            ],
            "active_summary_version_id": "corr-v2",
            "new_summary_version_id": "corr-v2",
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: captured.setdefault("update_data", update_data),
    )
    monkeypatch.setattr(
        callbacks,
        "_update_correction_audit",
        lambda uid, conversation_id, correction_id, payload: audit_updates.append(payload),
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                title="Corrected title",
                overview="[Ella] Corrected overview with enough detail to safely replace the summary.",
                correction_id="corr-123",
                summary_kind="corrected_enriched",
                summary_source="observer",
            ),
            uid="user-123",
        )
    )

    assert captured["update_data"]["summary_versions"][1]["id"] == "corr-v2"
    assert captured["update_data"]["active_summary_version_id"] == "corr-v2"
    assert captured["update_data"]["correction_state"]["status"] == "applied"
    assert captured["update_data"]["correction_state"]["pending"] is False
    assert captured["update_data"]["correction_state"]["correction_id"] == "corr-123"
    assert captured["update_data"]["correction_state"]["source"] == "ios"
    assert captured["update_data"]["correction_state"]["submitted_at"] == "2026-04-22T17:00:00+00:00"
    assert audit_updates[0]["status"] == "applied"
    assert audit_updates[0]["applied_summary_version_id"] == "corr-v2"
    assert result["active_summary_version_id"] == "corr-v2"


def test_update_conversation_summary_records_enrichment_state(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "structured": {
                "title": "Original title",
                "overview": "[Ella] Original overview with enough detail to preserve.",
                "emoji": "🧠",
                "category": callbacks.CategoryEnum.health,
            },
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "build_summary_version_update",
        lambda conversation, **kwargs: {
            "summary_versions": [{"id": "obs-v2", "title": kwargs["next_structured"]["title"]}],
            "active_summary_version_id": "obs-v2",
            "new_summary_version_id": "obs-v2",
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: captured.setdefault("update_data", update_data),
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                title="Observer title",
                overview="[Ella] Observer overview with enough detail to safely replace the summary.",
                summary_source="observer",
                summary_kind="observer_enriched",
                trace_id="trace-123",
            ),
            uid="user-123",
        )
    )

    enrichment_state = captured["update_data"]["enrichment_state"]
    assert enrichment_state["status"] == "writeback_applied"
    assert enrichment_state["pending"] is False
    assert enrichment_state["source"] == "observer"
    assert enrichment_state["kind"] == "observer_enriched"
    assert enrichment_state["trace_id"] == "trace-123"
    assert enrichment_state["error"] is None
    assert result["active_summary_version_id"] == "obs-v2"


def test_update_conversation_summary_persists_internal_assessment_when_available(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "structured": {
                "title": "Original title",
                "overview": "[Ella] Original overview with enough detail to preserve.",
                "emoji": "🧠",
                "category": callbacks.CategoryEnum.health,
            },
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "build_summary_version_update",
        lambda conversation, **kwargs: {
            "summary_versions": [{"id": "obs-v2", "title": kwargs["next_structured"]["title"]}],
            "active_summary_version_id": "obs-v2",
            "new_summary_version_id": "obs-v2",
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: captured.setdefault("update_data", update_data),
    )
    async def fake_fetch_internal_assessment(uid, conversation_id):
        return {
            "media_likelihood": 0.92,
            "speaker_confidence": "low",
            "risk_level": "none",
            "caregiver_relevance": "low",
            "escalation_recommendation": "none",
            "reason_codes": ["likely_media_or_background_audio"],
            "notes": "Likely ambient media.",
        }

    monkeypatch.setattr(callbacks, "_fetch_internal_assessment", fake_fetch_internal_assessment)

    asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                title="Observer title",
                overview="[Ella] Observer overview with enough detail to safely replace the summary.",
                summary_source="observer",
                summary_kind="observer_enriched",
            ),
            uid="user-123",
        )
    )

    assert captured["update_data"]["internal_assessment"]["media_likelihood"] == 0.92
    assert captured["update_data"]["internal_assessment"]["reason_codes"] == ["likely_media_or_background_audio"]


def test_list_enrichment_reconcile_candidates_filters_to_unenriched_recent_conversations(monkeypatch):
    fixture = [
        {
            "id": "conv-enriched",
            "created_at": "2026-04-23T18:00:00Z",
            "structured": {"title": "Enriched"},
            "active_summary_version_id": "v1",
            "summary_versions": [
                {"id": "v1", "source": "observer", "kind": "observer_enriched", "is_active": True}
            ],
            "enrichment_state": {"status": "writeback_applied"},
        },
        {
            "id": "conv-missing",
            "created_at": "2026-04-23T18:05:00Z",
            "structured": {"title": "Missing enrich"},
            "active_summary_version_id": "v2",
            "summary_versions": [{"id": "v2", "source": "legacy", "kind": "legacy_current", "is_active": True}],
        },
        {
            "id": "conv-failed",
            "created_at": "2026-04-23T18:10:00Z",
            "structured": {"title": "Failed enrich"},
            "active_summary_version_id": "v3",
            "summary_versions": [
                {"id": "v3", "source": "observer", "kind": "observer_enriched", "is_active": True}
            ],
            "enrichment_state": {"status": "failed", "error": "timeout"},
        },
    ]

    monkeypatch.setattr(callbacks.conversations_db, "get_conversations_without_photos", lambda uid, **kwargs: fixture)
    monkeypatch.setattr(callbacks.conversations_db, "get_conversations", lambda uid, **kwargs: fixture)

    result = asyncio.run(
        callbacks.list_enrichment_reconcile_candidates(
            uid="user-123",
            lookback_minutes=180,
            limit=25,
        )
    )

    assert result["uid"] == "user-123"
    assert result["total_scanned"] == 3
    assert result["candidate_count"] == 2
    assert [candidate["conversation_id"] for candidate in result["candidates"]] == ["conv-missing", "conv-failed"]
    assert result["candidates"][0]["reason"] == "active_summary_not_enriched"
    assert result["candidates"][1]["reason"] == "enrichment_failed"
