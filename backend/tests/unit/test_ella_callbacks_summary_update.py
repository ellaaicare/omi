import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from utils.ella.canonical_omi import transcript_grounding_hash

_MISSING_MODULE = object()
_STUBBED_IMPORT_MODULES = {
    "database._client": MagicMock(),
    "database.conversations": MagicMock(),
    "database.memories": MagicMock(),
    "database.users": MagicMock(),
    "httpx": MagicMock(),
    "asyncpg": MagicMock(),
    "firebase_admin": MagicMock(),
    "utils.other.endpoints": MagicMock(),
    "utils.notifications": MagicMock(),
    "utils.other.storage": MagicMock(),
    "database.ella_contacts": MagicMock(),
}
_RELOADED_IMPORT_MODULES = (
    "ella.config",
    "ella.services.provisioning",
    "ella.services.runtime_resolver",
    "ella.services.summary_writeback",
)
_ISOLATED_IMPORT_MODULES = (*_STUBBED_IMPORT_MODULES, *_RELOADED_IMPORT_MODULES)
_ORIGINAL_MODULES = {name: sys.modules.get(name, _MISSING_MODULE) for name in _ISOLATED_IMPORT_MODULES}
for _module_name, _stub_module in _STUBBED_IMPORT_MODULES.items():
    sys.modules[_module_name] = _stub_module
    _parent_name, _, _attribute_name = _module_name.rpartition(".")
    _parent_module = sys.modules.get(_parent_name)
    if _parent_module is not None:
        setattr(_parent_module, _attribute_name, _stub_module)
for _module_name in _RELOADED_IMPORT_MODULES:
    _removed_module = sys.modules.pop(_module_name, None)
    _parent_name, _, _attribute_name = _module_name.rpartition(".")
    _parent_module = sys.modules.get(_parent_name)
    if (
        _parent_module is not None
        and hasattr(_parent_module, _attribute_name)
        and getattr(_parent_module, _attribute_name) is _removed_module
    ):
        delattr(_parent_module, _attribute_name)

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_callbacks_path = Path(__file__).resolve().parents[2] / "ella" / "routers" / "callbacks.py"
_callbacks_spec = importlib.util.spec_from_file_location("ella_callbacks_test_module", _callbacks_path)
callbacks = importlib.util.module_from_spec(_callbacks_spec)
assert _callbacks_spec is not None and _callbacks_spec.loader is not None
_callbacks_spec.loader.exec_module(callbacks)
from ella.services import summary_writeback

for _module_name, _original_module in _ORIGINAL_MODULES.items():
    _loaded_module = sys.modules.get(_module_name)
    if _original_module is _MISSING_MODULE:
        sys.modules.pop(_module_name, None)
    else:
        sys.modules[_module_name] = _original_module
    _parent_name, _, _attribute_name = _module_name.rpartition(".")
    _parent_module = sys.modules.get(_parent_name)
    if _parent_module is None:
        continue
    if _original_module is _MISSING_MODULE:
        if getattr(_parent_module, _attribute_name, None) is _loaded_module:
            delattr(_parent_module, _attribute_name)
    else:
        setattr(_parent_module, _attribute_name, _original_module)

from ella.services.canonical_summary_source import (  # noqa: E402
    canonical_source_bytes,
    canonical_source_from_conversation,
    canonical_source_from_payload,
    canonical_source_sha256,
)
from ella.routers.canonical_events import _should_accept_canonical_summary_replacement  # noqa: E402
from utils.ella.canonical_omi import build_omi_canonical_event  # noqa: E402


def _service_authority(uid: str):
    return callbacks.EllaRequestAuthority(service="test_callback", service_subject_uid=uid)


@pytest.fixture(autouse=True)
def disable_canonical_omi_network(monkeypatch):
    class PendingConversationSummaryReconciliationError(RuntimeError):
        pass

    def guarded_summary_update(uid, conversation_id, _expected_active_version_id, update_data):
        callbacks.conversations_db.update_conversation(uid, conversation_id, update_data)
        return True

    monkeypatch.setattr(
        callbacks.conversations_db,
        "PendingConversationSummaryReconciliationError",
        PendingConversationSummaryReconciliationError,
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_active_summary_version",
        guarded_summary_update,
    )
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: {"ok": True, "inserted": 1, "duplicates": 0},
    )
    monkeypatch.setattr(callbacks, "require_omi_canonical_write_ready", lambda _uid: None)


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
            service=_service_authority("user-123"),
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
    assert captured["update_data"]["summary_writeback_receipt"] is None
    assert result["sanitizer_warnings"] == []


def test_terminal_summary_schedules_artwork_without_blocking_text_writeback(monkeypatch):
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", lambda *args, **kwargs: None)

    async def enqueue(uid, conversation_id):
        raise RuntimeError("artwork processing must not affect text enrichment")

    monkeypatch.setattr(callbacks, "enqueue_after_terminal_enrichment", enqueue)
    background_tasks = BackgroundTasks()
    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-artwork",
            callbacks.ConversationSummaryUpdate(
                title="Updated title",
                overview="[Ella] Terminal enriched summary remains successful independently of artwork.",
                emoji="🧠",
                category="personal",
            ),
            background_tasks=background_tasks,
            uid="user-artwork",
            service=_service_authority("user-artwork"),
        )
    )

    assert result["status"] == "ok"
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func is enqueue
    assert background_tasks.tasks[0].args == ("user-artwork", "conv-artwork")


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
            service=_service_authority("user-123"),
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
            service=_service_authority("user-123"),
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
                service=_service_authority("user-123"),
            )
        )

    assert excinfo.value.status_code == 422
    assert any("internal debug or routing jargon" in violation for violation in excinfo.value.detail["violations"])
    callbacks.conversations_db.update_conversation.assert_not_called()


def test_update_conversation_summary_allows_user_facing_mcp_api_topic(monkeypatch):
    captured = {}

    def fake_update(uid, conversation_id, update_data):
        captured["update_data"] = update_data

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", fake_update)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                overview=(
                    "[Ella] You and Greg discussed using an MCP connector and direct QuickBooks API access "
                    "to make accounting work faster than the browser-based workflow."
                ),
                category="technology",
            ),
            uid="user-123",
            service=_service_authority("user-123"),
        )
    )

    assert result["status"] == "ok"
    assert captured["update_data"]["structured.overview"].startswith("[Ella] ")
    assert captured["update_data"]["structured.category"] == "technology"


def test_update_conversation_summary_rejects_invalid_category():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conv-123",
                callbacks.ConversationSummaryUpdate(category="definitely-not-a-real-category"),
                uid="user-123",
                service=_service_authority("user-123"),
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
                {"id": 7, "is_user": True, "text": "Can you reprocess this?"},
                {"speaker": "Other", "text": "Yes, with the full transcript."},
            ],
            "structured": {
                "title": "Original title",
                "overview": "Original overview",
                "emoji": "🧠",
                "category": callbacks.CategoryEnum.technology,
            },
            "active_summary_version_id": "omi-v1",
            "summary_versions": [{"id": "omi-v1", "source": "omi", "kind": "generated"}],
            "enrichment_state": {
                "status": "writeback_applied",
                "canonical_status": "completed",
                "trace_id": "parallel-grounding:v2:synthetic",
            },
            "started_at": "2026-04-10T10:00:00Z",
            "finished_at": "2026-04-10T10:05:00Z",
        },
    )

    result = asyncio.run(
        callbacks.get_conversation_data(
            "conv-123",
            uid="user-123",
            service=_service_authority("user-123"),
        )
    )

    assert result["conversation_id"] == "conv-123"
    assert result["uid"] == "user-123"
    assert result["segment_count"] == 2
    assert result["transcript"] == "User: Can you reprocess this?\n\nOther: Yes, with the full transcript."
    assert result["transcript_segments"] == [
        {
            "id": "7",
            "text": "Can you reprocess this?",
            "speaker": None,
            "speaker_id": None,
            "is_user": True,
            "person_id": None,
            "start": None,
            "end": None,
            "timestamp": None,
        },
        {
            "id": None,
            "text": "Yes, with the full transcript.",
            "speaker": "Other",
            "speaker_id": None,
            "is_user": False,
            "person_id": None,
            "start": None,
            "end": None,
            "timestamp": None,
        },
    ]
    assert result["transcript_hash"] == transcript_grounding_hash(result["transcript_segments"])
    assert result["structured"]["title"] == "Original title"
    assert result["structured"]["overview"] == "Original overview"
    assert result["structured"]["emoji"] == "🧠"
    assert result["structured"]["category"] == "technology"
    assert result["active_summary_version_id"] == "omi-v1"
    assert result["active_summary_source"] == "omi"
    assert result["active_summary_kind"] == "generated"
    assert result["enrichment_status"] == "writeback_applied"
    assert result["enrichment_canonical_status"] == "completed"
    assert result["enrichment_trace_id"] == "parallel-grounding:v2:synthetic"
    assert result["started_at"] == "2026-04-10T10:00:00Z"
    assert result["finished_at"] == "2026-04-10T10:05:00Z"


def test_get_conversation_data_requires_uid():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(callbacks.get_conversation_data("conv-123"))

    assert excinfo.value.status_code == 400
    assert "uid query parameter required" in excinfo.value.detail


def test_summary_route_forwards_required_active_version_match(monkeypatch):
    captured = {}

    async def fake_write_conversation_summary(**kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(callbacks, "write_conversation_summary", fake_write_conversation_summary)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conv-123",
            callbacks.ConversationSummaryUpdate(
                title="Grounded",
                based_on_version_id="omi-v1",
                require_based_on_match=True,
            ),
            uid="user-123",
            service=_service_authority("user-123"),
        )
    )

    assert result == {"status": "ok"}
    assert captured["based_on_version_id"] == "omi-v1"
    assert captured["require_based_on_match"] is True


def test_get_conversation_data_404s_when_missing(monkeypatch):
    monkeypatch.setattr(callbacks.conversations_db, "get_conversation", lambda uid, conversation_id: None)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.get_conversation_data(
                "missing-conv",
                uid="user-123",
                service=_service_authority("user-123"),
            )
        )

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
            service=_service_authority("user-123"),
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
            service=_service_authority("user-123"),
        )
    )

    assert captured["update_data"]["internal_assessment"]["media_likelihood"] == 0.92
    assert captured["update_data"]["internal_assessment"]["reason_codes"] == ["likely_media_or_background_audio"]


def test_isolated_internal_assessment_uses_active_hermes_runtime(monkeypatch):
    requests = []
    runtime = MagicMock(agent_id="omi-isolated")

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-isolated"
        assert target_mode == "hermes-cloud-transcript"
        return runtime

    async def fail_legacy(_uid):
        raise AssertionError("isolated summary metadata must not use OpenClaw routing")

    async def authority_enabled(uid=None):
        return uid == "uid-isolated"

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"internal_assessment": {"risk_level": "none"}}

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            requests.append((url, headers))
            return FakeResponse()

    monkeypatch.setattr(callbacks, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(callbacks, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(callbacks, "_resolve_agent_id_for_uid", fail_legacy)
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", "http://hermes-provision/")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", "hermes-token")

    result = asyncio.run(callbacks._fetch_internal_assessment("uid-isolated", "conv-123"))

    assert result == {"risk_level": "none"}
    assert requests == [
        (
            "http://hermes-provision/workspace/omi-isolated/metadata/conversations/conv-123",
            {
                "Authorization": "Bearer hermes-token",
                "X-Ella-Owner-Uid": "uid-isolated",
            },
        )
    ]

    async def fake_target(uid):
        assert uid == "uid-unconfigured"
        return "omi-unconfigured", "http://hermes-provision", ""

    requests.clear()
    FakeResponse.status_code = 401
    monkeypatch.setattr(callbacks, "_resolve_workspace_target_for_uid", fake_target)

    result = asyncio.run(callbacks._fetch_internal_assessment("uid-unconfigured", "conv-123"))

    assert result is None
    assert requests == [
        (
            "http://hermes-provision/workspace/omi-unconfigured/metadata/conversations/conv-123",
            {},
        )
    ]


def test_cloud_internal_assessment_never_calls_mini_or_openclaw(monkeypatch):
    runtime = MagicMock(provider="hermes_cloud", agent_id="cloud-agent")

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-cloud"
        assert target_mode == "hermes-cloud-transcript"
        return runtime

    async def fail_legacy(_uid):
        raise AssertionError("Cloud callback must not resolve a process-global Plato agent")

    async def authority_enabled(uid=None):
        return uid == "uid-cloud"

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("Cloud callback must not call a Mini workspace endpoint")

    monkeypatch.setattr(callbacks, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(callbacks, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(callbacks, "_resolve_agent_id_for_uid", fail_legacy)
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", ForbiddenClient)

    result = asyncio.run(callbacks._fetch_internal_assessment("uid-cloud", "conv-cloud"))

    assert result is None


def test_update_conversation_summary_writes_enriched_omi_to_canonical(monkeypatch):
    canonical_writes = []

    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "created_at": "2026-05-07T18:56:59Z",
            "started_at": "2026-05-07T18:56:59Z",
            "finished_at": "2026-05-07T18:58:12Z",
            "structured": {
                "title": "Original cafe title",
                "overview": "[Ella] Original cafe overview with enough detail.",
                "emoji": "☕",
                "category": callbacks.CategoryEnum.other,
            },
            "transcript_segments": [{"is_user": True, "text": "I ordered a waffle."}],
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
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda uid, conversation, **kwargs: canonical_writes.append(
            {"uid": uid, "conversation": conversation, "kwargs": kwargs}
        )
        or {"ok": True, "inserted": 1, "duplicates": 0},
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "cafe-123",
            callbacks.ConversationSummaryUpdate(
                title="Cafe Coffee and Waffle Stop",
                overview="[Ella] You ordered a noah drink and a waffle with oat.",
                summary_source="observer",
                summary_kind="observer_enriched",
                trace_id="trace-cafe",
                require_canonical=True,
            ),
            uid="user-123",
            service=_service_authority("user-123"),
        )
    )

    assert result["canonical_confirmed"] is True

    assert canonical_writes[0]["uid"] == "user-123"
    assert canonical_writes[0]["conversation"]["id"] == "cafe-123"
    assert canonical_writes[0]["conversation"]["structured"]["title"] == "Cafe Coffee and Waffle Stop"
    assert canonical_writes[0]["conversation"]["structured"]["overview"] == (
        "[Ella] You ordered a noah drink and a waffle with oat."
    )
    assert canonical_writes[0]["conversation"]["active_summary_version_id"] == "obs-v2"
    assert canonical_writes[0]["kwargs"] == {
        "summary_source": "observer",
        "summary_kind": "observer_enriched",
        "trace_id": "trace-cafe",
    }


def _parallel_grounding_evidence():
    transcript_segments = [{"is_user": True, "text": "I ordered a waffle with oat milk after our morning walk."}]
    return {
        "attester": "hermes_parallel_grounding_verifier",
        "semantic_outcome": "supported",
        "supporting_quotes": ["I ordered a waffle with oat milk after our morning walk."],
        "policy_version": "hermes-parallel-grounding-verifier-v1",
        "transcript_hash": transcript_grounding_hash(transcript_segments),
        "summary_request_id": "summary-request-1",
        "summary_response_id": "summary-response-1",
        "verifier_request_id": "verifier-request-1",
        "verifier_response_id": "verifier-response-1",
    }


def _configure_parallel_grounding_write(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "active_summary_version_id": "original-v1",
            "summary_versions": [
                {
                    "id": "original-v1",
                    "source": "observer",
                    "kind": "observer_enriched",
                    "is_active": True,
                }
            ],
            "structured": {
                "title": "Original cafe title",
                "overview": "[Ella] Original cafe overview with enough detail.",
                "emoji": "☕",
                "category": callbacks.CategoryEnum.other,
            },
            "transcript_segments": [
                {"is_user": True, "text": "I ordered a waffle with oat milk after our morning walk."}
            ],
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "build_summary_version_update",
        lambda conversation, **kwargs: {
            "summary_versions": [
                {
                    "id": "parallel-v2",
                    "title": kwargs["next_structured"]["title"],
                    "overview": kwargs["next_structured"]["overview"],
                    "source": kwargs["source"],
                    "kind": kwargs["kind"],
                }
            ],
            "active_summary_version_id": "parallel-v2",
            "new_summary_version_id": "parallel-v2",
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_transcript_hash",
        lambda uid, conversation_id, expected_hash, update_data, **kwargs: (
            captured.setdefault("cas_expected_hash", expected_hash),
            captured.setdefault("update_data", update_data),
            True,
        )[-1],
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, update_data: captured.setdefault("update_data", update_data),
    )

    def update_if_authoritative(uid, conversation_id, expected_version, expected_state, update_data):
        captured.setdefault(
            "terminal_cas",
            {
                "uid": uid,
                "conversation_id": conversation_id,
                "expected_version": expected_version,
                "expected_state": expected_state,
                "update_data": update_data,
            },
        )
        captured.setdefault("confirmation_expected_version", expected_version)
        captured.setdefault("confirmation_expected_state", expected_state)
        captured.setdefault("confirmed_state", update_data)
        return True

    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_summary_authority",
        update_if_authoritative,
    )

    def write_canonical(uid, conversation, **kwargs):
        captured["canonical_conversation"] = conversation
        return {"ok": True}

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", write_canonical)
    return captured


def test_parallel_enrichment_binds_independent_grounding_to_canonical_version(monkeypatch):
    captured = _configure_parallel_grounding_write(monkeypatch)

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "cafe-123",
            callbacks.ConversationSummaryUpdate(
                title="Cafe Coffee and Waffle Stop",
                overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                summary_source="hermes_parallel",
                summary_kind="hermes_enriched",
                trace_id="parallel-grounding:cafe-123",
                require_canonical=True,
                today_card_grounding_evidence=_parallel_grounding_evidence(),
            ),
            uid="user-123",
            service=_service_authority("user-123"),
        )
    )

    receipt = captured["update_data"]["enrichment_state"]["today_card_grounding"]
    assert result["canonical_confirmed"] is True
    assert receipt["source_version_id"] == "parallel-v2"
    assert receipt["attester"] == "hermes_parallel_grounding_verifier"
    assert receipt["summary_request_id"] == "summary-request-1"
    assert receipt["verifier_request_id"] == "verifier-request-1"
    assert receipt["supporting_quote_hashes"][0].startswith("sha256:")
    assert "supporting_quotes" not in receipt
    canonical_receipt = captured["canonical_conversation"]["enrichment_state"]["today_card_grounding"]
    assert canonical_receipt == receipt
    assert captured["terminal_cas"]["expected_version"] == "parallel-v2"
    assert captured["terminal_cas"]["update_data"]["enrichment_state"]["canonical_status"] == "completed"


def test_parallel_canonical_confirmation_repairs_latest_summary_after_race(monkeypatch):
    captured = _configure_parallel_grounding_write(monkeypatch)
    correction = {
        "id": "cafe-123",
        "active_summary_version_id": "correction-v3",
        "summary_versions": [
            {
                "id": "correction-v3",
                "source": "ios_correction",
                "kind": "corrected_enriched",
                "is_active": True,
            }
        ],
        "structured": {
            "title": "Corrected cafe title",
            "overview": "[Ella] Corrected cafe overview with enough detail.",
            "emoji": "☕",
            "category": callbacks.CategoryEnum.other,
        },
        "transcript_segments": [{"is_user": True, "text": "I ordered a waffle with oat milk after our morning walk."}],
        "enrichment_state": {
            "status": "writeback_applied",
            "source": "ios_correction",
            "kind": "corrected_enriched",
            "trace_id": "correction:cafe-123",
            "canonical_status": "completed",
        },
    }
    get_calls = 0

    def get_conversation(uid, conversation_id):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            return {
                "id": conversation_id,
                "structured": {
                    "title": "Original cafe title",
                    "overview": "[Ella] Original cafe overview with enough detail.",
                    "emoji": "☕",
                    "category": callbacks.CategoryEnum.other,
                },
                "transcript_segments": correction["transcript_segments"],
            }
        return correction

    canonical_versions = []
    monkeypatch.setattr(callbacks.conversations_db, "get_conversation", get_conversation)
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_summary_authority",
        lambda *args, **kwargs: False,
    )

    def write_canonical(uid, conversation, **kwargs):
        canonical_versions.append(conversation["active_summary_version_id"])
        return {"ok": True}

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", write_canonical)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "cafe-123",
                callbacks.ConversationSummaryUpdate(
                    title="Cafe Coffee and Waffle Stop",
                    overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                    summary_source="hermes_parallel",
                    summary_kind="hermes_enriched",
                    trace_id="parallel-grounding:cafe-123",
                    require_canonical=True,
                    today_card_grounding_evidence=_parallel_grounding_evidence(),
                ),
                uid="user-123",
                service=_service_authority("user-123"),
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "active_summary_version_changed"
    assert canonical_versions == ["parallel-v2", "correction-v3"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("quote", "today_card_grounding_quote_not_in_transcript"),
        ("identity", "today_card_grounding_identity_invalid"),
        ("transcript", "today_card_grounding_transcript_changed"),
        ("scope", "today_card_grounding_evidence_scope_invalid"),
    ],
)
def test_parallel_grounding_evidence_fails_closed(monkeypatch, mutation, expected):
    captured = _configure_parallel_grounding_write(monkeypatch)
    evidence = _parallel_grounding_evidence()
    summary_source = "hermes_parallel"
    if mutation == "quote":
        evidence["supporting_quotes"] = ["This statement never appeared in the transcript."]
    elif mutation == "identity":
        evidence["verifier_request_id"] = evidence["summary_request_id"]
        evidence["verifier_response_id"] = evidence["summary_response_id"]
    elif mutation == "transcript":
        evidence["transcript_hash"] = "sha256:" + ("f" * 64)
    elif mutation == "scope":
        summary_source = "observer"

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "cafe-123",
                callbacks.ConversationSummaryUpdate(
                    title="Cafe Coffee and Waffle Stop",
                    overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                    summary_source=summary_source,
                    summary_kind="hermes_enriched",
                    trace_id="parallel-grounding:cafe-123",
                    require_canonical=True,
                    today_card_grounding_evidence=evidence,
                ),
                uid="user-123",
                service=_service_authority("user-123"),
            )
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == expected
    assert "update_data" not in captured


def test_parallel_grounding_transcript_compare_and_set_loses_race_without_publication(monkeypatch):
    captured = _configure_parallel_grounding_write(monkeypatch)
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_transcript_hash",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "cafe-123",
                callbacks.ConversationSummaryUpdate(
                    title="Cafe Coffee and Waffle Stop",
                    overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                    summary_source="hermes_parallel",
                    summary_kind="hermes_enriched",
                    trace_id="parallel-grounding:cafe-123",
                    require_canonical=True,
                    today_card_grounding_evidence=_parallel_grounding_evidence(),
                ),
                uid="user-123",
                service=_service_authority("user-123"),
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "transcript_changed"
    assert "canonical_conversation" not in captured

    for canonical_succeeds in (True, False):
        _configure_parallel_grounding_write(monkeypatch)
        canonical_versions = []
        if canonical_succeeds:
            correction = {
                "id": "cafe-123",
                "active_summary_version_id": "correction-v3",
                "summary_versions": [
                    {
                        "id": "correction-v3",
                        "source": "ios_correction",
                        "kind": "corrected_enriched",
                        "is_active": True,
                    }
                ],
                "structured": {
                    "title": "Corrected cafe title",
                    "overview": "[Ella] Corrected cafe overview with enough detail.",
                    "emoji": "☕",
                    "category": callbacks.CategoryEnum.other,
                },
                "transcript_segments": [
                    {"is_user": True, "text": "I ordered a waffle with oat milk after our morning walk."}
                ],
                "enrichment_state": {
                    "status": "writeback_applied",
                    "source": "ios_correction",
                    "kind": "corrected_enriched",
                    "trace_id": "correction:cafe-123",
                    "canonical_status": "completed",
                },
            }
            get_calls = 0

            def get_conversation(uid, conversation_id):
                nonlocal get_calls
                get_calls += 1
                if get_calls == 1:
                    return {
                        "id": conversation_id,
                        "active_summary_version_id": "original-v1",
                        "summary_versions": [
                            {
                                "id": "original-v1",
                                "source": "observer",
                                "kind": "observer_enriched",
                                "is_active": True,
                            }
                        ],
                        "structured": {
                            "title": "Original cafe title",
                            "overview": "[Ella] Original cafe overview with enough detail.",
                            "emoji": "☕",
                            "category": callbacks.CategoryEnum.other,
                        },
                        "transcript_segments": correction["transcript_segments"],
                    }
                return correction

            def write_canonical(uid, conversation, **kwargs):
                canonical_versions.append(conversation["active_summary_version_id"])
                return {"ok": True}

            monkeypatch.setattr(callbacks.conversations_db, "get_conversation", get_conversation)
            monkeypatch.setattr(callbacks, "write_omi_canonical_event", write_canonical)
        else:

            def fail_canonical(*args, **kwargs):
                raise ConnectionError("synthetic canonical failure")

            monkeypatch.setattr(callbacks, "write_omi_canonical_event", fail_canonical)
        terminal_updates = []

        def reject_stale_terminal_state(uid, conversation_id, expected_version, expected_state, update_data):
            terminal_updates.append((uid, conversation_id, expected_version, expected_state, update_data))
            return False

        monkeypatch.setattr(
            callbacks.conversations_db,
            "update_conversation_if_summary_authority",
            reject_stale_terminal_state,
        )

        with pytest.raises(HTTPException) as post_await_error:
            asyncio.run(
                callbacks.update_conversation_summary(
                    "cafe-123",
                    callbacks.ConversationSummaryUpdate(
                        title="Cafe Coffee and Waffle Stop",
                        overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                        summary_source="hermes_parallel",
                        summary_kind="hermes_enriched",
                        trace_id="parallel-grounding:cafe-123:post-await-race",
                        require_canonical=True,
                        today_card_grounding_evidence=_parallel_grounding_evidence(),
                    ),
                    uid="user-123",
                    service=_service_authority("user-123"),
                )
            )

        assert post_await_error.value.status_code == 409
        assert post_await_error.value.detail == (
            "active_summary_version_changed" if canonical_succeeds else "summary_result_version_changed"
        )
        assert len(terminal_updates) == 1
        assert terminal_updates[0][:3] == ("user-123", "cafe-123", "parallel-v2")
        if canonical_succeeds:
            assert canonical_versions == ["parallel-v2", "correction-v3"]


def test_parallel_pending_canonical_replay_rejects_transcript_drift_without_publication(monkeypatch):
    captured = _configure_parallel_grounding_write(monkeypatch)
    original_hash = _parallel_grounding_evidence()["transcript_hash"]
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "structured": {
                "title": "Cafe Coffee and Waffle Stop",
                "overview": "[Ella] You ordered a waffle with oat milk after your morning walk.",
                "emoji": "☕",
                "category": callbacks.CategoryEnum.other,
            },
            "transcript_segments": [
                {"is_user": True, "text": "The transcript was replaced after summary publication."}
            ],
            "enrichment_state": {
                "status": "writeback_pending_canonical",
                "canonical_status": "pending",
                "trace_id": "parallel-grounding:cafe-123",
                "today_card_grounding": {"transcript_hash": original_hash},
            },
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "cafe-123",
                callbacks.ConversationSummaryUpdate(
                    title="Cafe Coffee and Waffle Stop",
                    overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                    summary_source="hermes_parallel",
                    summary_kind="hermes_enriched",
                    trace_id="parallel-grounding:cafe-123",
                    require_canonical=True,
                    today_card_grounding_evidence=_parallel_grounding_evidence(),
                ),
                uid="user-123",
                service=_service_authority("user-123"),
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "transcript_changed"
    assert "canonical_conversation" not in captured


def test_parallel_grounding_evidence_rejects_unknown_fields_at_schema_boundary():
    evidence = _parallel_grounding_evidence()
    evidence["untrusted"] = True

    with pytest.raises(ValidationError):
        callbacks.ConversationSummaryUpdate(
            title="Cafe",
            overview="[Ella] A grounded cafe memory with enough detail.",
            summary_source="hermes_parallel",
            summary_kind="hermes_enriched",
            require_canonical=True,
            today_card_grounding_evidence=evidence,
        )


def test_parallel_prebuilt_grounding_receipt_cannot_bypass_quote_binding(monkeypatch):
    captured = _configure_parallel_grounding_write(monkeypatch)

    with pytest.raises(ValueError, match="today_card_grounding_attestation_invalid"):
        asyncio.run(
            callbacks.write_conversation_summary(
                uid="user-123",
                conversation_id="cafe-123",
                title="Cafe Coffee and Waffle Stop",
                overview="[Ella] You ordered a waffle with oat milk after your morning walk.",
                summary_source="hermes_parallel",
                summary_kind="hermes_enriched",
                require_canonical=True,
                today_card_grounding={"attester": "hermes_parallel_grounding_verifier"},
            )
        )

    assert "update_data" not in captured


def test_update_conversation_summary_same_trace_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "active_summary_version_id": "recovered-v1",
            "structured": {
                "title": "Recovered",
                "overview": "[Ella] Recovered summary with enough detail.",
                "emoji": "brain",
                "category": "other",
            },
            "summary_versions": [
                {
                    "id": "recovered-v1",
                    "source": "observer",
                    "kind": "recovered_enriched",
                    "title": "Recovered",
                    "overview": "[Ella] Recovered summary with enough detail.",
                    "emoji": "brain",
                    "category": "other",
                    "is_active": True,
                }
            ],
            "enrichment_state": {
                "status": "writeback_applied",
                "source": "observer",
                "kind": "recovered_enriched",
                "trace_id": "summary-retry:conversation-1:request-1",
            },
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda *args, **kwargs: pytest.fail("idempotent replay must not write Firestore"),
    )
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: pytest.fail("idempotent replay must not duplicate the canonical event"),
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conversation-1",
            callbacks.ConversationSummaryUpdate(
                title="Recovered",
                overview="[Ella] Recovered summary with enough detail.",
                summary_kind="recovered_enriched",
                trace_id="summary-retry:conversation-1:request-1",
            ),
            uid="user-1",
            service=_service_authority("user-1"),
        )
    )

    assert result["status"] == "ok"
    assert result["active_summary_version_id"] == "recovered-v1"
    assert result["idempotent_replay"] is True


def test_parallel_summary_source_match_uses_transcript_and_version_cas(monkeypatch):
    segments = [{"is_user": True, "text": "A synthetic owner-scoped transcript."}]
    transcript_hash = transcript_grounding_hash(segments)
    captured = {}
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "active_summary_version_id": "summary-v1",
            "structured": {
                "title": "Before",
                "overview": "[Ella] Before with enough detail.",
                "emoji": "note",
                "category": callbacks.CategoryEnum.other,
            },
            "transcript_segments": segments,
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "build_summary_version_update",
        lambda conversation, **kwargs: {
            "summary_versions": [{"id": "summary-v2"}],
            "active_summary_version_id": "summary-v2",
            "new_summary_version_id": "summary-v2",
        },
    )

    def compare_and_set(
        uid,
        conversation_id,
        expected_hash,
        update_data,
        *,
        expected_active_summary_version_id=None,
        match_active_summary_version=False,
    ):
        captured.update(
            {
                "uid": uid,
                "conversation_id": conversation_id,
                "expected_hash": expected_hash,
                "expected_version": expected_active_summary_version_id,
                "match_version": match_active_summary_version,
                "update_data": update_data,
            }
        )
        return True

    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_transcript_hash",
        compare_and_set,
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conversation-1",
            callbacks.ConversationSummaryUpdate(
                title="After",
                overview="[Ella] After with enough owner-scoped detail.",
                summary_source="hermes_parallel",
                summary_kind="hermes_enriched",
                based_on_version_id="summary-v1",
                trace_id="hermes-parallel:conversation-1:source-receipt",
                require_canonical=True,
                expected_transcript_hash=transcript_hash,
                require_source_match=True,
            ),
            uid="user-1",
            service=_service_authority("user-1"),
        )
    )

    assert result["status"] == "ok"
    assert result["canonical_confirmed"] is True
    assert captured["uid"] == "user-1"
    assert captured["conversation_id"] == "conversation-1"
    assert captured["expected_hash"] == transcript_hash
    assert captured["expected_version"] == "summary-v1"
    assert captured["match_version"] is True
    state = captured["update_data"]["enrichment_state"]
    assert state["source_transcript_hash"] == transcript_hash
    assert state["source_active_summary_version_id"] == "summary-v1"


def test_parallel_summary_source_match_rejects_stale_version_before_write(monkeypatch):
    segments = [{"is_user": True, "text": "A synthetic transcript."}]
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "active_summary_version_id": "summary-newer",
            "structured": {},
            "transcript_segments": segments,
        },
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_transcript_hash",
        lambda *args, **kwargs: pytest.fail("stale source must fail before CAS"),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-1",
                callbacks.ConversationSummaryUpdate(
                    title="Stale",
                    based_on_version_id="summary-v1",
                    trace_id="hermes-parallel:conversation-1:stale",
                    expected_transcript_hash=transcript_grounding_hash(segments),
                    require_source_match=True,
                ),
                uid="user-1",
                service=_service_authority("user-1"),
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "active_summary_version_changed"


def test_parallel_summary_source_match_replay_is_idempotent(monkeypatch):
    segments = [{"is_user": True, "text": "A synthetic transcript."}]
    transcript_hash = transcript_grounding_hash(segments)
    structured = {
        "title": "After",
        "overview": None,
        "emoji": None,
        "category": None,
    }
    request_input = summary_writeback._summary_request_fingerprint_input(
        structured=structured,
        summary_source="observer",
        summary_kind="observer_enriched",
        correction_id=None,
        based_on_version_id="summary-v1",
        set_active=True,
        require_canonical=True,
        require_based_on_match=False,
        preserve_generated_results=False,
        ella_tags=[],
        ella_signal=None,
        today_card_grounding=None,
        today_card_grounding_evidence=None,
        expected_transcript_hash=transcript_hash,
        require_source_match=True,
    )
    conversation = {
        "active_summary_version_id": "summary-v2",
        "summary_versions": [
            {
                "id": "summary-v2",
                "source": "observer",
                "kind": "observer_enriched",
                "based_on_version_id": "summary-v1",
                **structured,
                "is_active": True,
            }
        ],
        "structured": structured,
        "transcript_segments": segments,
        "enrichment_state": {
            "status": "writeback_applied",
            "canonical_status": "completed",
            "trace_id": "hermes-parallel:conversation-1:source-receipt",
            "source_transcript_hash": transcript_hash,
            "source_active_summary_version_id": "summary-v1",
            "result_summary_version_id": "summary-v2",
            "request_fingerprint": summary_writeback._summary_request_fingerprint(request_input),
            "request_fingerprint_input": request_input,
        },
    }
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: conversation,
    )
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_transcript_hash",
        lambda *args, **kwargs: pytest.fail("idempotent replay must not write"),
    )

    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conversation-1",
            callbacks.ConversationSummaryUpdate(
                title="After",
                based_on_version_id="summary-v1",
                trace_id="hermes-parallel:conversation-1:source-receipt",
                require_canonical=True,
                expected_transcript_hash=transcript_hash,
                require_source_match=True,
            ),
            uid="user-1",
            service=_service_authority("user-1"),
        )
    )

    assert result["status"] == "ok"
    assert result["idempotent_replay"] is True
    assert result["active_summary_version_id"] == "summary-v2"


def test_pending_canonical_replay_rejects_result_version_drift(monkeypatch):
    segments = [{"is_user": True, "text": "A synthetic transcript."}]
    transcript_hash = transcript_grounding_hash(segments)
    canonical_writer = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "active_summary_version_id": "summary-v3",
            "summary_versions": [
                {"id": "summary-v2", "is_active": False},
                {"id": "summary-v3", "is_active": True},
            ],
            "structured": {
                "title": "Corrected later summary",
                "overview": "A later correction must not publish under an older trace.",
            },
            "transcript_segments": segments,
            "enrichment_state": {
                "status": "writeback_pending_canonical",
                "canonical_status": "pending",
                "trace_id": "hermes-parallel:conversation-1:source-receipt",
                "source_transcript_hash": transcript_hash,
                "source_active_summary_version_id": "summary-v1",
                "result_summary_version_id": "summary-v2",
            },
        },
    )
    monkeypatch.setattr(callbacks, "write_omi_canonical_event", canonical_writer)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-1",
                callbacks.ConversationSummaryUpdate(
                    title="Older generated summary",
                    based_on_version_id="summary-v1",
                    trace_id="hermes-parallel:conversation-1:source-receipt",
                    require_canonical=True,
                    expected_transcript_hash=transcript_hash,
                    require_source_match=True,
                ),
                uid="user-1",
                service=_service_authority("user-1"),
            )
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "summary_result_version_changed"
    canonical_writer.assert_not_called()


def _cas_conversation():
    return {
        "started_at": "2026-08-15T12:00:00Z",
        "finished_at": "2026-08-15T12:01:00Z",
        "created_at": "2026-08-15T11:59:00Z",
        "structured": {
            "title": "Original",
            "overview": "Original overview",
            "emoji": "🪽",
            "category": "other",
        },
        "transcript_segments": [{"speaker": "Other", "text": "private transcript"}],
        "summary_versions": [{"id": "original-v1", "is_active": True}],
        "active_summary_version_id": "original-v1",
        "enrichment_state": {"status": "original", "pending": False},
        "internal_assessment": {"risk_level": "original"},
        "ella_tags": ["original"],
        "ella_signal": {"signal": "original"},
        "source": "omi",
        "status": "completed",
    }


def _cas_token(conversation, *, uid="uid-a", conversation_id="conversation-a"):
    return canonical_source_sha256(
        canonical_source_from_conversation(uid=uid, conversation_id=conversation_id, conversation=conversation)
    )


def _cas_update():
    return callbacks.ConversationSummaryUpdate(
        title="Winning summary",
        overview="[Ella] Winning overview with enough detail to safely replace the original summary.",
        emoji="🧠",
        category="personal",
        summary_source="hermes_parallel",
        summary_kind="hermes_enriched",
    )


def _install_atomic_cas_fake(monkeypatch, conversation, *, interleave=None):
    writes = []
    transaction_lock = threading.Lock()

    async def no_assessment(_uid, _conversation_id):
        return None

    def version_update(_conversation, **kwargs):
        return {
            "summary_versions": [
                {"id": "original-v1", "is_active": False},
                {"id": "winner-v2", "is_active": True, **kwargs["next_structured"]},
            ],
            "active_summary_version_id": "winner-v2",
            "new_summary_version_id": "winner-v2",
        }

    def transact(_uid, _conversation_id, builder, correction_id=None):
        with transaction_lock:
            if interleave is not None:
                # Emulate Firestore's optimistic retry: the first transaction read
                # passes CAS, a concurrent writer wins before commit, and the
                # transaction callback reruns against the winning document.
                builder(copy.deepcopy(conversation))
                interleave(conversation)
            before = copy.deepcopy(conversation)
            update_data, result = builder(copy.deepcopy(conversation))
            if update_data:
                writes.append(copy.deepcopy(update_data))
            for key, value in update_data.items():
                if key.startswith("structured."):
                    conversation.setdefault("structured", {})[key.split(".", 1)[1]] = value
                else:
                    conversation[key] = value
            return {"conversation": before, "update_data": update_data, "result": result}

    monkeypatch.setattr(callbacks, "_fetch_internal_assessment", no_assessment)
    monkeypatch.setattr(callbacks.conversations_db, "build_summary_version_update", version_update)
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", transact)
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda _uid, _conversation_id: copy.deepcopy(conversation),
    )
    return writes


def _canonical_publication_tuple(conversation):
    return {
        key: copy.deepcopy(conversation.get(key))
        for key in (
            "started_at",
            "finished_at",
            "created_at",
            "structured",
            "transcript_segments",
            "summary_versions",
            "active_summary_version_id",
            "enrichment_state",
            "internal_assessment",
            "ella_tags",
            "ella_signal",
            "source",
            "status",
        )
    }


def _canonical_publication_sha256(conversation):
    encoded = json.dumps(
        _canonical_publication_tuple(conversation),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cas_client(uid="uid-a"):
    app = FastAPI()
    app.include_router(callbacks.router)
    app.dependency_overrides[callbacks.require_callback_service] = lambda: _service_authority(uid)
    return TestClient(app)


def test_canonical_source_fixture_vectors_match_hermes_contract_data():
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "ella_canonical_source_v1.json"
    vectors = json.loads(fixture_path.read_text(encoding="utf-8"))

    for vector in vectors:
        source = canonical_source_from_payload(
            vector["payload"],
            uid=vector["uid"],
            conversation_id=vector["conversation_id"],
        )
        assert canonical_source_bytes(source).decode("utf-8") == vector["canonical_json"]
        assert canonical_source_sha256(source) == vector["sha256"]


def test_canonical_summary_event_carries_the_durable_publication_fence():
    conversation = _cas_conversation()
    conversation["id"] = "conversation-a"
    conversation["canonical_summary_publication_sequence"] = 7
    conversation["canonical_summary_publication_sha256"] = "a" * 64

    event = build_omi_canonical_event("uid-a", conversation)

    assert event["source_ref"]["canonical_summary_publication_sequence"] == 7
    assert event["source_ref"]["canonical_summary_publication_sha256"] == "a" * 64
    assert event["metadata"]["canonical_summary_publication_sequence"] == 7
    assert event["metadata"]["canonical_summary_publication_sha256"] == "a" * 64


def test_canonical_summary_publication_fence_rejects_late_or_conflicting_repair_images():
    def publication(sequence=None, sha256=None):
        metadata = {"adapter": "omi-enriched-conversation"}
        if sequence is not None:
            metadata["canonical_summary_publication_sequence"] = sequence
            metadata["canonical_summary_publication_sha256"] = sha256
        return {"channel": "omi", "metadata": metadata}

    existing = publication(3, "c" * 64)
    assert _should_accept_canonical_summary_replacement(existing, publication(4, "d" * 64)) is True
    assert _should_accept_canonical_summary_replacement(existing, publication(3, "c" * 64)) is True
    assert _should_accept_canonical_summary_replacement(existing, publication(2, "b" * 64)) is False
    assert _should_accept_canonical_summary_replacement(existing, publication(3, "d" * 64)) is False
    assert _should_accept_canonical_summary_replacement(existing, publication()) is False
    assert _should_accept_canonical_summary_replacement(publication(), publication(1, "a" * 64)) is True


def test_firestore_conversation_payload_matches_canonical_datetime_segment_and_transcript_representation():
    conversation = {
        "started_at": datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
        "structured": {"title": "  A\n title  ", "overview": "Overview", "emoji": "🪽", "category": "other"},
        "transcript_segments": [
            {"is_user": True, "text": "First"},
            {"speaker": "Taylor", "text": "Second"},
        ],
    }
    payload = callbacks.conversation_data_payload(
        uid="uid-a", conversation_id="conversation-a", conversation=conversation
    )
    source = canonical_source_from_conversation(
        uid="uid-a", conversation_id="conversation-a", conversation=conversation
    )

    assert payload["started_at"] == "2026-08-15 12:00:00+00:00"
    assert payload["finished_at"] == "2026-08-15 12:01:00+00:00"
    assert payload["segment_count"] == 2
    assert payload["transcript"] == "User: First\n\nTaylor: Second"
    assert source == canonical_source_from_payload(payload, uid="uid-a", conversation_id="conversation-a")
    assert source["title"] == "A title"


def test_cas_success_is_atomic_and_returns_content_free_receipt_header(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    canonical_publications = []
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda _uid, current, **_kwargs: canonical_publications.append(copy.deepcopy(current)) or {"ok": True},
    )
    token = _cas_token(conversation)
    payload = json.dumps(
        _cas_update().model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        content=payload,
        headers=_cas_headers(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "completed",
        "operation_receipt": {
            "token": "c" * 64,
            "status": "completed",
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "source_sha256": token,
            "source_version": "2026-08-15T12:01:00Z",
        },
    }
    assert response.headers["X-Ella-CAS-Applied"] == callbacks.ELLA_CANONICAL_SOURCE_CONTRACT
    assert len(writes) == 2
    assert conversation["structured"]["title"] == "Winning summary"
    assert conversation["active_summary_version_id"] == "winner-v2"
    assert len(canonical_publications) == 1
    publication = canonical_publications[0]
    pending_receipt = writes[0]["summary_writeback_receipt"]
    assert pending_receipt["post_image_sha256"] != pending_receipt["completed_post_image_sha256"]
    assert writes[0]["canonical_summary_publication_sha256"] == pending_receipt["post_image_sha256"]
    assert publication["enrichment_state"]["canonical_status"] == "completed"
    assert publication["canonical_summary_publication_sha256"] == pending_receipt["completed_post_image_sha256"]
    assert publication["canonical_summary_publication_sha256"] == _canonical_publication_sha256(publication)
    assert conversation["canonical_summary_publication_sha256"] == publication["canonical_summary_publication_sha256"]
    assert "private transcript" not in response.text


def _required_cas_update():
    update = _cas_update()
    update.require_canonical = True
    return update


def _cas_headers(token, *, operation_token="c" * 64, source_version="2026-08-15T12:01:00Z"):
    return {
        "Content-Type": "application/json",
        "X-Ella-CAS-Contract": callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
        "If-Match": f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{token}"',
        "X-Ella-Operation-Token": operation_token,
        "X-Ella-Source-Version": source_version,
    }


@pytest.mark.parametrize("failure", ["exception", "unconfirmed"])
def test_required_canonical_failure_returns_durable_pending_and_exact_retry_reconciles(monkeypatch, failure):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    token = _cas_token(conversation)
    canonical_calls = []

    def unavailable_writer(*args, **kwargs):
        canonical_calls.append((args, kwargs))
        if failure == "exception":
            raise RuntimeError("synthetic canonical outage")
        return {"ok": False}

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", unavailable_writer)
    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )

    assert first.status_code == 202
    assert first.json()["status"] == "pending_reconciliation"
    assert set(first.json()) == {"status", "operation_receipt"}
    assert set(first.json()["operation_receipt"]) == {
        "token",
        "status",
        "payload_sha256",
        "source_sha256",
        "source_version",
    }
    assert first.json()["operation_receipt"]["status"] == "pending_reconciliation"
    assert "X-Ella-CAS-Applied" not in first.headers
    assert first.headers["X-Ella-CAS-Reconciliation"] == "pending"
    assert len(writes) == 1
    assert conversation["structured"]["title"] == "Winning summary"
    assert conversation["summary_writeback_receipt"]["status"] == "pending_reconciliation"
    assert conversation["enrichment_state"]["canonical_status"] == "pending"

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", lambda *args, **kwargs: {"ok": True})
    retry = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "completed"
    assert retry.json()["operation_receipt"]["token"] == "c" * 64
    assert len(writes) == 2
    assert len(conversation["summary_versions"]) == 2
    assert conversation["active_summary_version_id"] == "winner-v2"
    assert conversation["summary_writeback_receipt"]["status"] == "completed"
    assert conversation["enrichment_state"]["canonical_status"] == "completed"
    assert len(canonical_calls) == 1


def test_cas_contract_requires_canonical_publication_without_body_flag(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic canonical outage")),
    )

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=_cas_headers(_cas_token(conversation)),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending_reconciliation"
    assert "X-Ella-CAS-Applied" not in response.headers
    assert len(writes) == 1


def test_completed_response_loss_retry_returns_same_receipt_without_republish(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    canonical_calls = []
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: canonical_calls.append((args, kwargs)) or {"ok": True},
    )
    headers = _cas_headers(_cas_token(conversation))

    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )
    retry = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )

    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert len(writes) == 2
    assert len(canonical_calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "started_at",
        "finished_at",
        "created_at",
        "structured",
        "transcript_segments",
        "summary_versions",
        "active_summary_version_id",
        "enrichment_state",
        "internal_assessment",
        "ella_tags",
        "ella_signal",
        "source",
        "status",
    ],
)
def test_post_publication_drift_repairs_every_canonical_field_before_terminal_supersession(monkeypatch, change):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    publications = []
    interleaved = [False]

    def interleave_later_writer(*args, **kwargs):
        if getattr(args[2], "__name__", "") == "finalize" and not interleaved[0]:
            interleaved[0] = True
            replacements = {
                "started_at": "2026-08-15T11:58:00Z",
                "finished_at": "2026-08-15T12:02:00Z",
                "created_at": "2026-08-15T11:57:00Z",
                "structured": {
                    "title": "Later writer B",
                    "overview": "Later overview",
                    "emoji": "🛡️",
                    "category": "work",
                },
                "transcript_segments": [{"speaker": "Other", "text": "later transcript"}],
                "summary_versions": [{"id": "later-v3", "is_active": True, "title": "Later writer B"}],
                "active_summary_version_id": "later-v3",
                "enrichment_state": {"status": "later_writer", "pending": False, "source": "other"},
                "internal_assessment": {"risk_level": "later-writer-B"},
                "ella_tags": ["later_writer_b"],
                "ella_signal": {"signal": "later-writer-B"},
                "source": "later-source",
                "status": "later-status",
            }
            conversation[change] = copy.deepcopy(replacements[change])
        return transact(*args, **kwargs)

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", interleave_later_writer)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda _uid, current, **_kwargs: publications.append(copy.deepcopy(current)) or {"ok": True},
    )

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=_cas_headers(_cas_token(conversation)),
    )

    assert response.status_code == 409
    assert "X-Ella-CAS-Applied" not in response.headers
    assert len(publications) == 2
    assert publications[0].get(change) != publications[1].get(change)
    assert _canonical_publication_tuple(publications[0]) != _canonical_publication_tuple(conversation)
    assert _canonical_publication_tuple(publications[1]) == _canonical_publication_tuple(conversation)
    assert conversation["summary_writeback_receipt"]["status"] == "superseded"
    assert conversation["summary_writeback_receipt"]["canonical_status"] == "superseded"
    assert len(writes) == 3


def test_terminal_supersession_is_exact_retry_safe_and_unblocks_later_cas_and_legacy_writers(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    publications = []
    interleaved = [False]

    def interleave_once(*args, **kwargs):
        if getattr(args[2], "__name__", "") == "finalize" and not interleaved[0]:
            interleaved[0] = True
            conversation["internal_assessment"] = {"risk_level": "later-winner"}
        return transact(*args, **kwargs)

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", interleave_once)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda _uid, current, **_kwargs: publications.append(copy.deepcopy(current)) or {"ok": True},
    )
    client = _cas_client()
    original_source = _cas_token(conversation)
    original_headers = _cas_headers(original_source)
    first = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=original_headers,
    )
    publication_count_after_repair = len(publications)
    exact_retry = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=original_headers,
    )

    assert first.status_code == exact_retry.status_code == 409
    assert exact_retry.json() == {"detail": "Canonical summary operation conflict"}
    assert len(publications) == publication_count_after_repair == 2
    assert conversation["summary_writeback_receipt"]["status"] == "superseded"

    later = _cas_update()
    later.title = "Later CAS winner"
    current_source = _cas_token(conversation)
    later_cas = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=later.model_dump(),
        headers=_cas_headers(
            current_source,
            operation_token="d" * 64,
            source_version=str(conversation["finished_at"]),
        ),
    )
    assert later_cas.status_code == 200
    assert later_cas.json()["status"] == "completed"
    assert conversation["summary_writeback_receipt"]["token"] == "d" * 64

    def legacy_update(_uid, _conversation_id, expected_active_version, update_data):
        if str(conversation.get("active_summary_version_id") or "") != str(expected_active_version or ""):
            return False
        for key, value in update_data.items():
            if key.startswith("structured."):
                conversation.setdefault("structured", {})[key.split(".", 1)[1]] = value
            else:
                conversation[key] = value
        return True

    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_OPTIONAL)
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_if_active_summary_version", legacy_update)
    legacy = _cas_update()
    legacy.title = "Later legacy winner"
    legacy_response = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=legacy.model_dump(),
    )
    assert legacy_response.status_code == 200
    assert conversation["structured"]["title"] == "Later legacy winner"
    assert conversation["summary_writeback_receipt"] is None
    assert len(writes) == 5


@pytest.mark.parametrize("failure_point", ["before_commit", "lost_ack"])
def test_supersession_finalize_crash_or_lost_ack_is_retry_safe(monkeypatch, failure_point):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    interleaved = [False]
    terminal_failure = [False]
    publications = []

    def fail_terminalize_once(*args, **kwargs):
        builder_name = getattr(args[2], "__name__", "")
        if builder_name == "finalize" and not interleaved[0]:
            interleaved[0] = True
            conversation["ella_signal"] = {"signal": "post-publication-winner"}
        if builder_name == "terminalize" and not terminal_failure[0]:
            terminal_failure[0] = True
            if failure_point == "before_commit":
                raise RuntimeError("synthetic terminalize crash")
            result = transact(*args, **kwargs)
            raise RuntimeError("synthetic terminalize acknowledgement loss")
        return transact(*args, **kwargs)

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", fail_terminalize_once)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda _uid, current, **_kwargs: publications.append(copy.deepcopy(current)) or {"ok": True},
    )
    headers = _cas_headers(_cas_token(conversation))
    client = _cas_client()
    first = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )
    retry = client.patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )

    assert first.status_code == 202
    assert retry.status_code == 409
    assert conversation["summary_writeback_receipt"]["status"] == "superseded"
    assert _canonical_publication_tuple(publications[-1]) == _canonical_publication_tuple(conversation)
    assert all(
        publication["canonical_summary_publication_sha256"] == _canonical_publication_sha256(publication)
        for publication in publications
    )
    assert len(publications) == (3 if failure_point == "before_commit" else 2)


def test_concurrent_exact_retries_share_one_fenced_repair_and_terminalize_once(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    interleaved = [False]

    def interleave_once(*args, **kwargs):
        if getattr(args[2], "__name__", "") == "finalize" and not interleaved[0]:
            interleaved[0] = True
            conversation["ella_tags"] = ["concurrent-repair-winner"]
        return transact(*args, **kwargs)

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", interleave_once)
    canonical_calls = [0]

    def leave_repair_pending(_uid, _current, **_kwargs):
        canonical_calls[0] += 1
        return {"ok": canonical_calls[0] == 1}

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", leave_repair_pending)
    headers = _cas_headers(_cas_token(conversation))
    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )
    assert first.status_code == 202
    assert conversation["summary_writeback_receipt"]["canonical_status"] == "supersession_pending"

    repair_barrier = threading.Barrier(2)
    logical_publications = {}
    publication_lock = threading.Lock()

    def fenced_repair(_uid, current, **_kwargs):
        repair_barrier.wait(timeout=10)
        assert current["canonical_summary_publication_sha256"] == _canonical_publication_sha256(current)
        fence = (
            current["canonical_summary_publication_sequence"],
            current["canonical_summary_publication_sha256"],
        )
        with publication_lock:
            logical_publications[fence] = _canonical_publication_tuple(current)
        return {"ok": True}

    monkeypatch.setattr(callbacks, "write_omi_canonical_event", fenced_repair)

    def exact_retry():
        return _cas_client().patch(
            "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
            json=_cas_update().model_dump(),
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [future.result(timeout=20) for future in [executor.submit(exact_retry) for _ in range(2)]]

    assert [response.status_code for response in responses] == [409, 409]
    assert len(logical_publications) == 1
    assert next(iter(logical_publications.values())) == _canonical_publication_tuple(conversation)
    assert conversation["summary_writeback_receipt"]["status"] == "superseded"


def test_later_cas_writer_after_completion_gets_412_and_cannot_replay_old_receipt(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    original_source = _cas_token(conversation)
    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=_cas_headers(original_source),
    )
    later = _cas_update()
    later.title = "Later writer B"
    second = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=later.model_dump(),
        headers=_cas_headers(original_source, operation_token="d" * 64),
    )

    assert first.status_code == 200
    assert second.status_code == 412
    assert conversation["structured"]["title"] == "Winning summary"
    assert len(writes) == 2


def test_later_legacy_writer_is_blocked_while_cas_receipt_is_pending(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_OPTIONAL)
    conversation = _cas_conversation()
    conversation["summary_writeback_receipt"] = {"status": "pending_reconciliation", "token": "c" * 64}
    monkeypatch.setattr(callbacks.conversations_db, "get_conversation", lambda *_args: copy.deepcopy(conversation))
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_if_active_summary_version",
        lambda *_args: (_ for _ in ()).throw(
            callbacks.conversations_db.PendingConversationSummaryReconciliationError("pending")
        ),
    )
    canonical_writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_omi_canonical_event", canonical_writer)

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Canonical summary reconciliation pending"}
    canonical_writer.assert_not_called()


def test_required_canonical_preflight_failure_is_zero_write(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    writer = MagicMock()
    transaction = MagicMock()
    monkeypatch.setattr(callbacks, "write_omi_canonical_event", writer)
    monkeypatch.setattr(
        callbacks,
        "require_omi_canonical_write_ready",
        lambda _uid: (_ for _ in ()).throw(RuntimeError("not configured")),
    )
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", transaction)

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(_cas_token(_cas_conversation())),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Canonical summary dependency unavailable"}
    transaction.assert_not_called()
    writer.assert_not_called()


def test_firestore_response_loss_reconciles_committed_receipt_without_second_summary_write(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    calls = [0]

    def response_loss(*args, **kwargs):
        result = transact(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("synthetic response loss after commit")
        return result

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", response_loss)
    token = _cas_token(conversation)
    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(writes) == 2
    assert len(conversation["summary_versions"]) == 2
    assert conversation["summary_writeback_receipt"]["status"] == "completed"


def test_firestore_outcome_without_readable_receipt_is_explicitly_unknown(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_omi_canonical_event", writer)
    monkeypatch.setattr(
        callbacks.conversations_db,
        "update_conversation_with_builder",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic transaction outcome loss")),
    )
    monkeypatch.setattr(callbacks.conversations_db, "get_conversation", lambda *args, **kwargs: _cas_conversation())

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(_cas_token(_cas_conversation())),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Conversation summary outcome unknown; retry exact request"}
    writer.assert_not_called()


def test_canonical_finalize_failure_stays_pending_and_retry_is_idempotent(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    transact = callbacks.conversations_db.update_conversation_with_builder
    finalize_failures = [0]
    canonical_calls = []

    def fail_first_finalize(*args, **kwargs):
        if getattr(args[2], "__name__", "") == "finalize" and finalize_failures[0] == 0:
            finalize_failures[0] += 1
            raise RuntimeError("synthetic finalize outage")
        return transact(*args, **kwargs)

    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", fail_first_finalize)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: canonical_calls.append((args, kwargs)) or {"ok": True},
    )
    token = _cas_token(conversation)
    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )
    retry = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )

    assert first.status_code == 202
    assert retry.status_code == 200
    assert len(writes) == 2
    assert len(conversation["summary_versions"]) == 2
    assert conversation["summary_writeback_receipt"]["status"] == "completed"
    assert len(canonical_calls) == 2


def test_different_cas_is_blocked_while_required_reconciliation_is_pending(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)
    token = _cas_token(conversation)
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic outage")),
    )
    first = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_required_cas_update().model_dump(),
        headers=_cas_headers(token),
    )
    different = _required_cas_update()
    different.title = "Different summary"
    second = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=different.model_dump(),
        headers=_cas_headers(token, operation_token="d" * 64),
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json() == {"detail": "Canonical summary reconciliation pending"}
    assert len(writes) == 1
    assert len(conversation["summary_versions"]) == 2


@pytest.mark.parametrize(
    "change",
    ["transcript", "started_at", "finished_at", "segment_count", "structured_summary"],
)
def test_interleaved_canonical_change_returns_412_and_preserves_winner(monkeypatch, change):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    stale_token = _cas_token(conversation)

    def interleave(current):
        if change == "transcript":
            current["transcript_segments"][0]["text"] = "winning transcript"
        elif change == "started_at":
            current["started_at"] = "2026-08-15T11:59:59Z"
        elif change == "finished_at":
            current["finished_at"] = "2026-08-15T12:02:00Z"
        elif change == "segment_count":
            current["transcript_segments"].append({"speaker": "Other", "text": "winning second segment"})
        else:
            current["structured"]["title"] = "Winning concurrent summary"

    writes = _install_atomic_cas_fake(monkeypatch, conversation, interleave=interleave)

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=_cas_headers(stale_token),
    )

    assert response.status_code == 412
    assert stale_token not in response.text
    assert "private transcript" not in response.text
    assert writes == []
    assert conversation["active_summary_version_id"] == "original-v1"
    assert conversation["structured"]["title"] != "Winning summary"


def test_cas_source_version_mismatch_returns_412_without_writes(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=_cas_headers(_cas_token(conversation), source_version="2026-08-15T12:00:59Z"),
    )

    assert response.status_code == 412
    assert writes == []
    assert conversation["structured"]["title"] == "Original"


@pytest.mark.parametrize(
    ("contract", "if_match"),
    [
        (None, None),
        ("unknown-contract", '"ella-canonical-source-v1:' + "a" * 64 + '"'),
        ("ella-canonical-source-v1", None),
        ("ella-canonical-source-v1", "ella-canonical-source-v1:" + "a" * 64),
        ("ella-canonical-source-v1", 'W/"ella-canonical-source-v1:' + "a" * 64 + '"'),
        ("ella-canonical-source-v1", '"ella-canonical-source-v1:' + "A" * 64 + '"'),
        ("ella-canonical-source-v1", '"ella-canonical-source-v1:' + "a" * 63 + '"'),
    ],
)
def test_required_cas_rejects_missing_or_malformed_headers_before_write(monkeypatch, contract, if_match):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", writer)
    monkeypatch.setattr(callbacks, "write_conversation_summary", writer)

    headers = {}
    if contract is not None:
        headers["X-Ella-CAS-Contract"] = contract
    if if_match is not None:
        headers["If-Match"] = if_match
    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )

    assert response.status_code == 428
    if if_match:
        assert if_match not in response.text
    writer.assert_not_called()


@pytest.mark.parametrize(
    ("operation_token", "source_version"),
    [
        (None, "2026-08-15T12:01:00Z"),
        ("short", "2026-08-15T12:01:00Z"),
        ("c" * 257, "2026-08-15T12:01:00Z"),
        ("invalid token value", "2026-08-15T12:01:00Z"),
        ("c" * 64, None),
        ("c" * 64, ""),
        ("c" * 64, "line\nbreak"),
        ("c" * 64, "v" * 257),
    ],
)
def test_cas_rejects_missing_or_malformed_operation_headers_before_write(monkeypatch, operation_token, source_version):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", writer)
    headers = {
        "X-Ella-CAS-Contract": callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
        "If-Match": f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"',
    }
    if operation_token is not None:
        headers["X-Ella-Operation-Token"] = operation_token
    if source_version is not None:
        headers["X-Ella-Source-Version"] = source_version

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )

    assert response.status_code == 428
    writer.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("X-Ella-CAS-Contract", "malformed"),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
        ],
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
        ],
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
            ("If-Match", "malformed"),
        ],
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
        ],
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
            ("X-Ella-Operation-Token", "c" * 64),
            ("X-Ella-Operation-Token", "d" * 64),
            ("X-Ella-Source-Version", "2026-08-15T12:01:00Z"),
        ],
        [
            ("X-Ella-CAS-Contract", callbacks.ELLA_CANONICAL_SOURCE_CONTRACT),
            ("If-Match", f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{"a" * 64}"'),
            ("X-Ella-Operation-Token", "c" * 64),
            ("X-Ella-Source-Version", "2026-08-15T12:01:00Z"),
            ("X-Ella-Source-Version", "2026-08-15T12:01:00Z"),
        ],
    ],
)
def test_cas_rejects_duplicate_raw_precondition_headers_before_write(monkeypatch, headers):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", writer)

    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Duplicate summary operation headers"}
    writer.assert_not_called()


def test_optional_mode_allows_only_fully_headerless_legacy_write(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_OPTIONAL)
    legacy_calls = []

    async def legacy_writer(**kwargs):
        legacy_calls.append(kwargs)
        return {"status": "ok", "conversation_id": kwargs["conversation_id"]}

    monkeypatch.setattr(callbacks, "write_conversation_summary", legacy_writer)
    result = asyncio.run(
        callbacks.update_conversation_summary(
            "conversation-a",
            _cas_update(),
            uid="uid-a",
            service=_service_authority("uid-a"),
        )
    )
    assert result["conversation_id"] == "conversation-a"
    assert len(legacy_calls) == 1

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-a",
                _cas_update(),
                uid="uid-a",
                service=_service_authority("uid-a"),
                cas_contract=callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
            )
        )
    assert excinfo.value.status_code == 428
    assert len(legacy_calls) == 1


def test_capability_signal_reports_enforcement_without_credentials(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    app = FastAPI()
    app.include_router(callbacks.router)
    response = TestClient(app).get("/v1/ella/conversation/summary/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "contract": "ella-canonical-source-v1",
        "conditional_write": True,
        "enforcement": "required",
        "headerless_legacy_writes": False,
    }


def test_cas_wrong_owner_and_missing_conversation_never_write(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    actual_cas_writer = callbacks.write_conversation_summary_cas
    writer = MagicMock()
    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", writer)
    token = "a" * 64
    with pytest.raises(HTTPException) as owner_error:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-a",
                _cas_update(),
                uid="uid-b",
                service=_service_authority("uid-a"),
                cas_contract=callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
                if_match=f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{token}"',
                operation_token="c" * 64,
                source_version="2026-08-15T12:01:00Z",
            )
        )
    assert owner_error.value.status_code == 403
    writer.assert_not_called()

    async def no_assessment(_uid, _conversation_id):
        return None

    transaction_calls = []

    def missing_transaction(uid, conversation_id, _builder, correction_id=None):
        transaction_calls.append((uid, conversation_id))
        return None

    monkeypatch.setattr(callbacks, "_fetch_internal_assessment", no_assessment)
    monkeypatch.setattr(callbacks.conversations_db, "update_conversation_with_builder", missing_transaction)
    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", actual_cas_writer)
    with pytest.raises(HTTPException) as missing_error:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-missing",
                _cas_update(),
                uid="uid-a",
                service=_service_authority("uid-a"),
                cas_contract=callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
                if_match=f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{token}"',
                operation_token="c" * 64,
                source_version="2026-08-15T12:01:00Z",
            )
        )
    assert missing_error.value.status_code == 404
    assert transaction_calls == [("uid-a", "conversation-missing")]


def test_cas_sanitizer_failure_performs_zero_writes(monkeypatch):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    conversation = _cas_conversation()
    writes = _install_atomic_cas_fake(monkeypatch, conversation)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-a",
                callbacks.ConversationSummaryUpdate(overview="too short"),
                uid="uid-a",
                service=_service_authority("uid-a"),
                cas_contract=callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
                if_match=f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{_cas_token(conversation)}"',
                operation_token="c" * 64,
                source_version="2026-08-15T12:01:00Z",
            )
        )
    assert excinfo.value.status_code == 422
    assert writes == []


def test_cas_failure_does_not_log_or_return_headers_secrets_or_payload(monkeypatch, caplog):
    monkeypatch.setenv(callbacks.SUMMARY_CAS_MODE_ENV, callbacks.SUMMARY_CAS_REQUIRED)
    secret = "private transcript bearer-secret"
    token = "b" * 64

    async def failing_writer(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(callbacks, "write_conversation_summary_cas", failing_writer)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            callbacks.update_conversation_summary(
                "conversation-a",
                _cas_update(),
                uid="uid-a",
                service=_service_authority("uid-a"),
                cas_contract=callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
                if_match=f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{token}"',
                operation_token="c" * 64,
                source_version="2026-08-15T12:01:00Z",
            )
        )

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "Conversation summary update failed"
    assert secret not in caplog.text
    assert token not in caplog.text
