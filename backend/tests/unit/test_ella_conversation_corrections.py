import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("utils.other.endpoints", MagicMock())
sys.modules.setdefault("ella.config", MagicMock(ELLA_CONFIG=SimpleNamespace(n8n_base_url="https://n8n.test")))
sys.modules.setdefault("ella.routers.resolve", MagicMock())

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_corrections_path = _backend_path / "ella" / "routers" / "corrections.py"
_corrections_spec = importlib.util.spec_from_file_location("ella_corrections_test_module", _corrections_path)
corrections = importlib.util.module_from_spec(_corrections_spec)
assert _corrections_spec is not None and _corrections_spec.loader is not None
_corrections_spec.loader.exec_module(corrections)


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


def test_submit_correction_accepts_ios_payload_and_queues(monkeypatch):
    audits = []
    events = []
    enqueued = {}

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
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

    async def fake_enqueue(**kwargs):
        enqueued.update(kwargs)
        return {"agent_id": "ella-user-test", "correction_file": "corrections/corr.md"}

    monkeypatch.setattr(corrections, "_enqueue_correction", fake_enqueue)

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
            uid="user-123",
        )
    )

    assert result.status == "queued"
    assert result.queued is True
    assert result.conversation_id == "conv-123"
    assert result.correction_id
    assert result.trace_id.startswith("correction:conv-123:")
    assert audits[0]["uid"] == "user-123"
    assert audits[0]["payload"]["status"] == "submitted"
    assert audits[0]["payload"]["category"] == "media"
    assert audits[0]["payload"]["correction_text"] == "This was background TV audio, not a real memory concern."
    assert enqueued["uid"] == "user-123"
    assert enqueued["conversation_id"] == "conv-123"
    assert "The podcast said memory can be tricky." in enqueued["transcript"]
    assert enqueued["request"].summary_context.app_summary == "The app summary was too clinical."
    assert events[-1]["stage"] == "queued"


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
                uid="authenticated-user",
            )
        )

    assert excinfo.value.status_code == 404
    assert calls == [("authenticated-user", "conv-123")]


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
                uid="user-123",
            )
        )

    assert excinfo.value.status_code == 402


def test_submit_correction_persists_failure_trace_and_still_returns_202(monkeypatch):
    audits = []
    events = []

    monkeypatch.setattr(corrections.conversations_db, "get_conversation", lambda uid, conversation_id: _conversation())
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

    async def failing_enqueue(**kwargs):
        raise RuntimeError("provision offline")

    monkeypatch.setattr(corrections, "_enqueue_correction", failing_enqueue)

    result = asyncio.run(
        corrections.submit_conversation_correction(
            "conv-123",
            corrections.ConversationCorrectionRequest(correction_text="Please correct the summary."),
            uid="user-123",
        )
    )

    assert result.status == "queue_failed"
    assert result.queued is False
    assert audits[-1]["status"] == "queue_failed"
    assert audits[-1]["queue_error"] == "provision offline"
    assert events[-1]["stage"] == "queue_failed"
    assert events[-1]["status"] == "error"
