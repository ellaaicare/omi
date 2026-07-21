from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

from models.conversation import Conversation, ConversationStatus, Structured
from models.transcript_segment import TranscriptSegment
from utils.conversations.historical_repair import (
    conversation_repair_metadata,
    is_long_discarded_summary_failure,
)


def _repair_script_module():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "ella" / "repair_long_discarded_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("repair_long_discarded_conversation", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _conversation(*, text_words: int, discarded: bool, title: str = "") -> Conversation:
    return Conversation(
        id="repair-candidate",
        created_at=datetime(2026, 7, 8, 19, 43, tzinfo=timezone.utc),
        started_at=datetime(2026, 7, 8, 19, 43, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 8, 20, 10, tzinfo=timezone.utc),
        structured=Structured(title=title),
        transcript_segments=[
            TranscriptSegment(
                text=" ".join(["important"] * text_words),
                speaker="SPEAKER_00",
                is_user=True,
                start=0,
                end=1620,
            )
        ],
        status=ConversationStatus.completed,
        discarded=discarded,
    )


def test_stale_processing_long_discarded_empty_summary_is_repair_candidate():
    conversation = _conversation(text_words=3200, discarded=True)
    conversation.status = ConversationStatus.processing

    now = conversation.finished_at + timedelta(hours=7)

    assert is_long_discarded_summary_failure(conversation, now=now) is True


def test_active_processing_long_discarded_empty_summary_is_not_repair_candidate():
    conversation = _conversation(text_words=3200, discarded=True)
    conversation.status = ConversationStatus.processing

    now = conversation.finished_at + timedelta(minutes=5)

    assert is_long_discarded_summary_failure(conversation, now=now) is False


def test_long_discarded_empty_summary_is_repair_candidate():
    conversation = _conversation(text_words=3200, discarded=True)

    assert is_long_discarded_summary_failure(conversation) is True


def test_repaired_conversation_is_not_repair_candidate():
    conversation = _conversation(text_words=3200, discarded=False, title="Recovered summary")

    assert is_long_discarded_summary_failure(conversation) is False


def test_short_discarded_summary_is_not_repair_candidate():
    conversation = _conversation(text_words=20, discarded=True)

    assert is_long_discarded_summary_failure(conversation) is False


def test_repair_metadata_omits_transcript_text():
    conversation = _conversation(text_words=3200, discarded=True)

    metadata = conversation_repair_metadata(conversation)

    assert metadata["conversation_id"] == "repair-candidate"
    assert metadata["segment_count"] == 1
    assert metadata["transcript_chars"] > 25_000
    assert "important important" not in str(metadata)


def test_repair_conversation_dry_run_does_not_write(monkeypatch):
    repair_script = _repair_script_module()
    conversation = _conversation(text_words=3200, discarded=True)
    updates = []
    vectors = []

    monkeypatch.setattr(repair_script, "_load_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(repair_script, "_load_people", lambda uid, conversation: [])
    monkeypatch.setattr(repair_script, "_fetch_existing_conversation_vector_ids", lambda uid, conversation_ids: [])
    monkeypatch.setattr(
        repair_script,
        "_generate_structured",
        lambda *args, **kwargs: (Structured(title="Recovered", overview="Recovered summary."), False),
    )
    monkeypatch.setattr(repair_script, "_save_structured_vector", lambda *args, **kwargs: vectors.append(args))
    monkeypatch.setattr(repair_script, "_update_conversation", lambda *args, **kwargs: updates.append(args))

    result = repair_script.repair_conversation(
        uid="uid-1",
        conversation_id=conversation.id,
        language="en",
        min_transcript_chars=25_000,
        apply=False,
    )

    assert result["status"] == "would_repair"
    assert updates == []
    assert vectors == []


def test_repair_conversation_apply_upserts_vector_before_firestore(monkeypatch):
    repair_script = _repair_script_module()
    conversation = _conversation(text_words=3200, discarded=True)
    load_results = [conversation, conversation.model_copy(deep=True)]
    calls = []

    monkeypatch.setattr(repair_script, "_load_conversation", lambda uid, conversation_id: load_results.pop(0))
    monkeypatch.setattr(repair_script, "_load_people", lambda uid, conversation: [])
    monkeypatch.setattr(repair_script, "_fetch_existing_conversation_vector_ids", lambda uid, conversation_ids: [])
    monkeypatch.setattr(
        repair_script,
        "_generate_structured",
        lambda *args, **kwargs: (Structured(title="Recovered", overview="Recovered summary."), False),
    )
    monkeypatch.setattr(repair_script, "_save_structured_vector", lambda *args, **kwargs: calls.append("vector"))
    monkeypatch.setattr(
        repair_script,
        "_build_summary_version_update",
        lambda *args, **kwargs: {"summary_versions": [], "active_summary_version_id": "repair-version"},
    )
    monkeypatch.setattr(repair_script, "_update_conversation", lambda *args, **kwargs: calls.append(("firestore", args[2])))

    result = repair_script.repair_conversation(
        uid="uid-1",
        conversation_id=conversation.id,
        language="en",
        min_transcript_chars=25_000,
        apply=True,
    )

    assert result["status"] == "repaired"
    assert calls[0] == "vector"
    assert calls[1][0] == "firestore"
    assert calls[1][1]["historical_repair"]["vector_upserted"] is True


def test_repair_conversation_apply_skips_when_record_changes_before_write(monkeypatch):
    repair_script = _repair_script_module()
    conversation = _conversation(text_words=3200, discarded=True)
    changed = conversation.model_copy(deep=True)
    changed.discarded = False
    load_results = [conversation, changed]
    updates = []
    vectors = []

    monkeypatch.setattr(repair_script, "_load_conversation", lambda uid, conversation_id: load_results.pop(0))
    monkeypatch.setattr(repair_script, "_load_people", lambda uid, conversation: [])
    monkeypatch.setattr(repair_script, "_fetch_existing_conversation_vector_ids", lambda uid, conversation_ids: [])
    monkeypatch.setattr(
        repair_script,
        "_generate_structured",
        lambda *args, **kwargs: (Structured(title="Recovered", overview="Recovered summary."), False),
    )
    monkeypatch.setattr(repair_script, "_save_structured_vector", lambda *args, **kwargs: vectors.append(args))
    monkeypatch.setattr(repair_script, "_update_conversation", lambda *args, **kwargs: updates.append(args))

    result = repair_script.repair_conversation(
        uid="uid-1",
        conversation_id=conversation.id,
        language="en",
        min_transcript_chars=25_000,
        apply=True,
    )

    assert result["status"] == "concurrent_change_detected"
    assert updates == []
    assert vectors == []


def test_repair_conversation_does_not_clear_discarded_for_empty_generated_summary(monkeypatch):
    repair_script = _repair_script_module()
    conversation = _conversation(text_words=3200, discarded=True)
    updates = []
    vectors = []

    monkeypatch.setattr(repair_script, "_load_conversation", lambda uid, conversation_id: conversation)
    monkeypatch.setattr(repair_script, "_load_people", lambda uid, conversation: [])
    monkeypatch.setattr(repair_script, "_fetch_existing_conversation_vector_ids", lambda uid, conversation_ids: [])
    monkeypatch.setattr(
        repair_script,
        "_generate_structured",
        lambda *args, **kwargs: (Structured(title="", overview=""), False),
    )
    monkeypatch.setattr(repair_script, "_save_structured_vector", lambda *args, **kwargs: vectors.append(args))
    monkeypatch.setattr(repair_script, "_update_conversation", lambda *args, **kwargs: updates.append(args))

    result = repair_script.repair_conversation(
        uid="uid-1",
        conversation_id=conversation.id,
        language="en",
        min_transcript_chars=25_000,
        apply=True,
    )

    assert result["status"] == "summary_generation_empty"
    assert updates == []
    assert vectors == []


def test_repair_conversation_vector_failure_does_not_update_firestore(monkeypatch):
    repair_script = _repair_script_module()
    conversation = _conversation(text_words=3200, discarded=True)
    load_results = [conversation, conversation.model_copy(deep=True)]
    updates = []

    monkeypatch.setattr(repair_script, "_load_conversation", lambda uid, conversation_id: load_results.pop(0))
    monkeypatch.setattr(repair_script, "_load_people", lambda uid, conversation: [])
    monkeypatch.setattr(repair_script, "_fetch_existing_conversation_vector_ids", lambda uid, conversation_ids: [])
    monkeypatch.setattr(
        repair_script,
        "_generate_structured",
        lambda *args, **kwargs: (Structured(title="Recovered", overview="Recovered summary."), False),
    )

    def fail_vector(*args, **kwargs):
        raise RuntimeError("vector down")

    monkeypatch.setattr(repair_script, "_save_structured_vector", fail_vector)
    monkeypatch.setattr(repair_script, "_update_conversation", lambda *args, **kwargs: updates.append(args))

    result = repair_script.repair_conversation(
        uid="uid-1",
        conversation_id=conversation.id,
        language="en",
        min_transcript_chars=25_000,
        apply=True,
    )

    assert result["status"] == "vector_upsert_failed"
    assert updates == []
