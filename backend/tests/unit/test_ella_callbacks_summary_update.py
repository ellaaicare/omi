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


@pytest.fixture(autouse=True)
def disable_canonical_omi_network(monkeypatch):
    monkeypatch.setattr(
        callbacks,
        "write_omi_canonical_event",
        lambda *args, **kwargs: {"ok": True, "inserted": 1, "duplicates": 0},
    )


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


def test_isolated_internal_assessment_uses_active_hermes_runtime(monkeypatch):
    requests = []
    runtime = MagicMock(agent_id="omi-isolated")

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-isolated"
        assert target_mode == "hermes-cloud-transcript"
        return runtime

    async def fail_legacy(_uid):
        raise AssertionError("isolated summary metadata must not use OpenClaw routing")

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

    monkeypatch.setattr(callbacks, "runtime_authority_enabled", lambda uid=None: uid == "uid-isolated")
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
            {"Authorization": "Bearer hermes-token"},
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

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("Cloud callback must not call a Mini workspace endpoint")

    monkeypatch.setattr(callbacks, "runtime_authority_enabled", lambda uid=None: uid == "uid-cloud")
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


def test_update_conversation_summary_same_trace_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: {
            "id": conversation_id,
            "active_summary_version_id": "recovered-v1",
            "enrichment_state": {
                "status": "writeback_applied",
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
        )
    )

    assert result["status"] == "ok"
    assert result["active_summary_version_id"] == "recovered-v1"
    assert result["idempotent_replay"] is True
