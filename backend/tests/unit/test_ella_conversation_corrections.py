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
    submitted = {}

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
    assert submitted["uid"] == "user-123"
    assert submitted["conversation_id"] == "conv-123"
    assert "The podcast said memory can be tricky." in submitted["transcript"]
    assert submitted["request"].summary_context.app_summary == "The app summary was too clinical."
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


def test_submit_correction_persists_n8n_failure_trace_and_still_returns_202(monkeypatch):
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

    async def failing_submit_to_n8n(**kwargs):
        raise RuntimeError("n8n offline")

    monkeypatch.setattr(corrections, "_submit_correction_to_n8n", failing_submit_to_n8n)

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
    assert audits[-1]["queue_error"] == "n8n offline"
    assert events[-1]["stage"] == "queue_failed"
    assert events[-1]["status"] == "error"


def test_router_uses_custom_ella_namespace_only():
    paths = {route.path for route in corrections.router.routes}

    assert "/v1/ella/conversations/{conversation_id}/corrections" in paths
    assert "/v1/conversations/{conversation_id}/corrections" not in paths


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
