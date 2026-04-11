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
sys.modules.setdefault("utils.notifications", MagicMock())
sys.modules.setdefault("utils.other.storage", MagicMock())
sys.modules.setdefault("ella.config", MagicMock())
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
