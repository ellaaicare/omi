import asyncio
import copy
import importlib.util
import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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

from ella.services.canonical_summary_source import (  # noqa: E402
    canonical_source_bytes,
    canonical_source_from_conversation,
    canonical_source_from_payload,
    canonical_source_sha256,
)


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


def _cas_conversation():
    return {
        "started_at": "2026-08-15T12:00:00Z",
        "finished_at": "2026-08-15T12:01:00Z",
        "structured": {
            "title": "Original",
            "overview": "Original overview",
            "emoji": "🪽",
            "category": "other",
        },
        "transcript_segments": [{"speaker": "Other", "text": "private transcript"}],
        "summary_versions": [{"id": "original-v1", "is_active": True}],
        "active_summary_version_id": "original-v1",
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

    def transact(_uid, _conversation_id, builder):
        if interleave is not None:
            # Emulate Firestore's optimistic retry: the first transaction read
            # passes CAS, a concurrent writer wins before commit, and the
            # transaction callback reruns against the winning document.
            builder(copy.deepcopy(conversation))
            interleave(conversation)
        before = copy.deepcopy(conversation)
        update_data, result = builder(copy.deepcopy(conversation))
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
    return writes


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
    token = _cas_token(conversation)
    response = _cas_client().patch(
        "/v1/ella/conversation/conversation-a/summary?uid=uid-a",
        json=_cas_update().model_dump(),
        headers={
            "X-Ella-CAS-Contract": callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
            "If-Match": f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{token}"',
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Ella-CAS-Applied"] == callbacks.ELLA_CANONICAL_SOURCE_CONTRACT
    assert len(writes) == 1
    assert conversation["structured"]["title"] == "Winning summary"
    assert conversation["active_summary_version_id"] == "winner-v2"
    assert "private transcript" not in response.text


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
        headers={
            "X-Ella-CAS-Contract": callbacks.ELLA_CANONICAL_SOURCE_CONTRACT,
            "If-Match": f'"{callbacks.ELLA_CANONICAL_SOURCE_CONTRACT}:{stale_token}"',
        },
    )

    assert response.status_code == 412
    assert stale_token not in response.text
    assert "private transcript" not in response.text
    assert writes == []
    assert conversation["active_summary_version_id"] == "original-v1"
    assert conversation["structured"]["title"] != "Winning summary"


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
            )
        )
    assert owner_error.value.status_code == 403
    writer.assert_not_called()

    async def no_assessment(_uid, _conversation_id):
        return None

    transaction_calls = []

    def missing_transaction(uid, conversation_id, _builder):
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
            )
        )

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "Conversation summary update failed"
    assert secret not in caplog.text
    assert token not in caplog.text
