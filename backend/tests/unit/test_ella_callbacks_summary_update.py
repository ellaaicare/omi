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
sys.modules.setdefault("utils.notifications", MagicMock())
sys.modules.setdefault("utils.other.storage", MagicMock())
sys.modules.setdefault("ella.config", MagicMock())
sys.modules.setdefault("database.ella_contacts", MagicMock())

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
                overview="[Ella] Updated overview",
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
    assert captured["update_data"]["structured.overview"] == "[Ella] Updated overview"
    assert captured["update_data"]["structured.emoji"] == "🧠"
    assert captured["update_data"]["structured.category"] == "personal"
    assert captured["update_data"]["apps_results"] == []
    assert captured["update_data"]["plugins_results"] == []


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
