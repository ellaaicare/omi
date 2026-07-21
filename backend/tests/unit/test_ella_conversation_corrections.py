import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.modules.setdefault("database._client", MagicMock(db=MagicMock()))
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("httpx", MagicMock())
sys.modules.setdefault("utils.other.endpoints", MagicMock())
sys.modules.setdefault("utils.conversations.vector", MagicMock(save_structured_vector=MagicMock()))
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
from ella.services import summary_recovery


@pytest.fixture(autouse=True)
def _disable_external_observer_side_effects(monkeypatch):
    async def noop_emit(**kwargs):
        return None

    async def noop_propagate(**kwargs):
        return None

    monkeypatch.setattr(corrections, "_emit_canonical_correction_event", noop_emit)
    monkeypatch.setattr(corrections, "_run_correction_propagation_for_submission", noop_propagate)


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
    app.dependency_overrides[corrections.auth.get_current_user_uid] = lambda: "authenticated-user"
    return app, TestClient(app)


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
                background_tasks=None,
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
    assert audits[-1]["status"] == "applied"
    assert audits[-1]["direct_apply_result"]["active_summary_version_id"] == "corrected-v1"
    assert events[-1]["stage"] == "direct_apply_succeeded"
    assert conversation_updates[0]["correction_state"]["status"] == "submitted"


def test_apply_corrected_summary_uses_shared_direct_writeback_service(monkeypatch):
    captured = {}

    async def fake_apply(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "active_summary_version_id": "corrected-v1"}

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
    assert captured == {
        "uid": "user-123",
        "conversation_id": "conv-123",
        "trace_id": "correction:conv-123:corr-123",
        "active_summary_version_id": "legacy-v1",
        "summary": corrected,
        "summary_kind": "corrected_enriched",
        "correction_id": "corr-123",
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
        return {"status": "ok", "active_summary_version_id": "corrected-v1"}

    monkeypatch.setattr(corrections, "_generate_corrected_summary", fake_generate)
    monkeypatch.setattr(corrections, "_apply_corrected_summary", fake_apply)

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
    assert result.proposal_id == "proposal-123"
    assert len(background_tasks.tasks) == 2
    assert generated == []
    assert audits[-1]["status"] == "queued"
    assert audits[-1]["queue_result"] == {"mode": "background_direct_apply"}
    assert events[-1]["stage"] == "direct_apply_queued"
    assert conversation_updates[-1]["correction_state"]["status"] == "queued"

    observer_func, _, _ = background_tasks.tasks[0]
    assert observer_func is corrections._run_correction_observer_work

    func, args, kwargs = background_tasks.tasks[1]
    assert func is corrections._run_direct_correction_apply
    asyncio.run(func(*args, **kwargs))

    assert generated[0]["uid"] == "user-123"
    assert generated[0]["conversation_id"] == "conv-123"
    assert audits[-1]["status"] == "applied"
    assert events[-1]["stage"] == "direct_apply_succeeded"


def test_submit_correction_queues_observer_work_without_blocking_on_canonical_event(monkeypatch):
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
    assert len(background_tasks.tasks) == 1
    func, args, kwargs = background_tasks.tasks[0]
    assert func is corrections._run_correction_observer_work
    assert kwargs["uid"] == "user-123"
    assert kwargs["conversation_id"] == "conv-123"
    assert kwargs["correction_id"] == result.correction_id
    assert any(event["stage"] == "observer_work_queued" for event in events)

    asyncio.run(func(*args, **kwargs))

    assert emitted["uid"] == "user-123"
    assert emitted["conversation_id"] == "conv-123"
    assert emitted["correction_id"] == result.correction_id
    assert emitted["proposal_id"] == "proposal-123"
    assert emitted["request"].correction_text == "Actually this was background TV audio."


def test_correction_propagation_feature_flag_defaults_off():
    assert corrections.CORRECTION_PROPAGATION_ENABLED is False


def test_router_uses_custom_ella_namespace_only():
    paths = {route.path for route in corrections.router.routes}

    assert "/v1/ella/conversations/{conversation_id}/corrections" in paths
    assert "/v1/conversations/{conversation_id}/corrections" in paths


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


def test_retry_api_claims_once_and_queues_two_stage_recovery(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    worker_calls = []
    monkeypatch.setattr(
        corrections.conversations_db,
        "claim_conversation_processing_retry",
        lambda uid, conversation_id, claimed_request_id: {
            "outcome": "claimed",
            "conversation": _retry_conversation(request_id=claimed_request_id),
        },
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
    assert worker_calls == [
        {
            "uid": "authenticated-user",
            "conversation_id": "conversation-1",
            "request_id": request_id,
            "client_context": "This was the morning cafe conversation.",
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
    reads = [conversation, generic, completed_generic, enriched]
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

    async def fake_generate(**kwargs):
        events.append("hermes_generate")
        return {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
            "ella_tags": ["omi", "recovery"],
            "ella_signal": {},
        }

    async def fake_apply(**kwargs):
        events.append(f"apply:{kwargs['summary_kind']}")
        version_id = "generic-v1" if kwargs["summary_kind"] == "generic_recovered" else "enriched-v2"
        return {"status": "ok", "active_summary_version_id": version_id}

    monkeypatch.setattr(summary_recovery, "generate_summary_from_prompt", fake_generate)
    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_summary_applied",
        lambda *args: events.append("generic_receipt") or True,
    )
    monkeypatch.setattr(
        summary_recovery,
        "save_structured_vector",
        lambda uid, conv: events.append(f"vector:{conv.structured.title}"),
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
        "finish:completed",
        "hermes_generate",
        "apply:recovered_enriched",
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
    reads = [conversation, generic, {**generic, "status": "completed", "processing_error": None}]
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

    async def fake_generate(**kwargs):
        raise RuntimeError("private Hermes provider detail")

    async def fake_apply(**kwargs):
        assert kwargs["summary_kind"] == "generic_recovered"
        return {"status": "ok", "active_summary_version_id": "generic-v1"}

    monkeypatch.setattr(summary_recovery, "generate_summary_from_prompt", fake_generate)
    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "record_conversation_processing_retry_summary_applied",
        lambda *args: True,
    )
    monkeypatch.setattr(summary_recovery, "save_structured_vector", lambda uid, conv: events.append("vector"))
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

    assert outcome == "completed"
    assert events == [
        "vector",
        ("finish", "completed", {}),
        ("enrichment", "failed", {}),
    ]
    assert "private Hermes provider detail" not in str(events)


def test_summary_recovery_resumes_hermes_after_generic_phase_completed(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    generic = {
        **_retry_conversation(status="completed", request_id=request_id),
        "active_summary_version_id": "generic-v1",
        "processing_retry_summary_version_id": "generic-v1",
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

    async def fake_generate(**kwargs):
        events.append("hermes_generate")
        return {
            "title": "Enriched",
            "overview": "[Ella] Enriched summary.",
            "emoji": "brain",
            "category": "other",
            "ella_tags": ["omi", "recovery"],
            "ella_signal": {},
        }

    async def fake_apply(**kwargs):
        events.append("hermes_apply")
        return {"status": "ok", "active_summary_version_id": "enriched-v2"}

    monkeypatch.setattr(summary_recovery, "generate_summary_from_prompt", fake_generate)
    monkeypatch.setattr(summary_recovery, "apply_summary_update", fake_apply)
    monkeypatch.setattr(summary_recovery, "save_structured_vector", lambda uid, conv: events.append("vector"))
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
    assert events == ["vector", "finish", "hermes_generate", "hermes_apply", "vector", "enrichment:completed"]


def test_summary_recovery_vector_failure_remains_retryable_without_raw_error(monkeypatch):
    request_id = "84eb13fa-31d9-40ba-a742-c4de4757dc10"
    conversation = {
        **_retry_conversation(request_id=request_id),
        "active_summary_version_id": "recovered-v1",
        "processing_retry_summary_version_id": "recovered-v1",
    }
    finish_calls = []
    monkeypatch.setattr(summary_recovery.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(
        summary_recovery,
        "generate_stock_conversation_summary",
        lambda *args: pytest.fail("receipted generic summary must not be regenerated"),
    )
    monkeypatch.setattr(
        summary_recovery,
        "save_structured_vector",
        lambda uid, conv: (_ for _ in ()).throw(RuntimeError("secret provider detail")),
    )
    monkeypatch.setattr(
        summary_recovery.conversations_db,
        "finish_conversation_processing_retry",
        lambda *args, **kwargs: finish_calls.append((args, kwargs)) or True,
    )

    outcome = asyncio.run(
        summary_recovery.recover_failed_conversation_summary(
            uid="user-1",
            conversation_id="conversation-1",
            request_id=request_id,
        )
    )

    assert outcome == "failed"
    assert finish_calls[0][0][3] == "failed"
    assert finish_calls[0][1]["error_code"] == "conversation_summary_recovery_failed"
    assert "secret provider detail" not in str(finish_calls[0])
