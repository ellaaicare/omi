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
            uid="user-123",
        )
    )

    assert result.status == "queued"
    assert result.queued is True
    assert result.proposal_id == "proposal-123"
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
    assert proposals[0]["conversation_id"] == "conv-123"
    assert proposals[0]["request"].correction_text == "This was background TV audio, not a real memory concern."
    assert any(event["stage"] == "proposal_created" for event in events)
    assert events[-1]["stage"] == "queued"
    assert conversation_updates[0]["correction_state"]["status"] == "submitted"
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
            uid="user-123",
        )
    )

    assert result.status == "queue_failed"
    assert result.queued is False
    assert result.proposal_id == "proposal-123"
    assert audits[-1]["status"] == "queue_failed"
    assert audits[-1]["queue_error"] == "n8n offline"
    assert events[-1]["stage"] == "queue_failed"
    assert events[-1]["status"] == "error"
    assert conversation_updates[0]["correction_state"]["status"] == "submitted"
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
        return {"status": "ok", "active_summary_version_id": "corrected-v1"}

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
    assert audits[-1]["status"] == "applied"
    assert audits[-1]["direct_apply_result"]["active_summary_version_id"] == "corrected-v1"
    assert events[-1]["stage"] == "direct_apply_succeeded"
    assert conversation_updates[0]["correction_state"]["status"] == "submitted"


def test_router_uses_custom_ella_namespace_only():
    paths = {route.path for route in corrections.router.routes}

    assert "/v1/ella/conversations/{conversation_id}/corrections" in paths
    assert "/v1/conversations/{conversation_id}/corrections" in paths


def test_submit_correction_accepts_text_alias():
    request = corrections.ConversationCorrectionRequest(text="Please fix the doctor name.")

    assert request.correction_text == "Please fix the doctor name."


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
