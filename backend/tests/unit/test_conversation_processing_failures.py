import ast
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.conversation import Conversation, ConversationStatus, Structured
from models.transcript_segment import TranscriptSegment

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("ENCRYPTION_SECRET", "test-encryption-secret-32-bytes-long")

if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")

    class _RedisStub:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None

            return _noop

    redis_stub.Redis = _RedisStub
    sys.modules["redis"] = redis_stub

if "stripe" not in sys.modules:
    stripe_stub = types.ModuleType("stripe")
    stripe_stub.api_key = None
    sys.modules["stripe"] = stripe_stub

if "pinecone" not in sys.modules:
    pinecone_stub = types.ModuleType("pinecone")

    class _PineconeStub:
        def __init__(self, *args, **kwargs):
            pass

        def Index(self, *args, **kwargs):
            return types.SimpleNamespace()

    pinecone_stub.Pinecone = _PineconeStub
    sys.modules["pinecone"] = pinecone_stub


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    for attr_name, value in attrs.items():
        setattr(module, attr_name, value)
    sys.modules[name] = module
    return module


def _noop(*args, **kwargs):
    return None


class _AudioSegmentStub:
    @staticmethod
    def from_wav(*_args, **_kwargs):
        return None


_stub_module("pydub", AudioSegment=_AudioSegmentStub)
_stub_module(
    "database.vector_db",
    find_similar_memories=lambda *args, **kwargs: [],
    upsert_memory_vector=_noop,
    delete_memory_vector=_noop,
    upsert_vector2=_noop,
    update_vector_metadata=_noop,
    delete_vector=_noop,
    query_vectors_by_metadata=lambda *args, **kwargs: [],
)
_stub_module(
    "database.apps",
    record_app_usage=_noop,
    get_omi_personas_by_uid_db=lambda *args, **kwargs: [],
    get_app_by_id_db=lambda *args, **kwargs: None,
)
_stub_module(
    "utils.llm.memories",
    resolve_memory_conflict=_noop,
    extract_memories_from_text=lambda *args, **kwargs: [],
    new_memories_extractor=lambda *args, **kwargs: [],
    identify_category_for_memory=lambda *args, **kwargs: "other",
)
_stub_module(
    "utils.llm.conversation_processing",
    get_transcript_structure=_noop,
    get_app_result=_noop,
    should_discard_conversation=lambda *args, **kwargs: False,
    select_best_app_for_conversation=lambda *args, **kwargs: None,
    get_suggested_apps_for_conversation=lambda *args, **kwargs: [],
    get_reprocess_transcript_structure=_noop,
    assign_conversation_to_folder=lambda *args, **kwargs: (None, 0.0, ""),
    generate_summary_with_prompt=lambda *args, **kwargs: "",
)
_stub_module(
    "utils.llm.external_integrations",
    summarize_experience_text=_noop,
    get_message_structure=_noop,
)
_stub_module("utils.llm.trends", trends_extractor=lambda *args, **kwargs: [])
_stub_module("utils.llm.goals", extract_and_update_goal_progress=_noop)
_stub_module(
    "utils.llm.chat",
    retrieve_metadata_from_text=lambda *args, **kwargs: {},
    retrieve_metadata_from_message=lambda *args, **kwargs: {},
    retrieve_metadata_fields_from_transcript=lambda *args, **kwargs: {},
    obtain_emotional_message=_noop,
)
_stub_module("utils.llm.clients", generate_embedding=lambda *args, **kwargs: [0.1, 0.2])
_stub_module("utils.retrieval.rag", retrieve_rag_conversation_context=lambda *args, **kwargs: "")
_stub_module("utils.conversations.search", search_conversations=lambda *args, **kwargs: [])
_stub_module("utils.speaker_identification", extract_speaker_samples=_noop)
_stub_module("utils.app_integrations", trigger_external_integrations=lambda *args, **kwargs: [])
_stub_module("utils.conversations.location", get_google_maps_location=lambda *args, **kwargs: None)
_stub_module(
    "utils.other.storage",
    list_audio_chunks=lambda *args, **kwargs: [],
    precache_conversation_audio=_noop,
    get_conversation_recording_if_exists=lambda *args, **kwargs: None,
    upload_postprocessing_audio=lambda *args, **kwargs: "https://audio.test/recording.wav",
    delete_postprocessing_audio=_noop,
    upload_conversation_recording=_noop,
    delete_conversation_audio_files=_noop,
    storage_client=types.SimpleNamespace(),
    private_cloud_sync_bucket="test-bucket",
)
_stub_module(
    "utils.notifications",
    send_important_conversation_message=_noop,
    send_notification=_noop,
    send_action_item_data_message=_noop,
    send_apple_reminders_sync_push=_noop,
    send_merge_completed_message=_noop,
)
_stub_module(
    "utils.apps",
    get_available_apps=lambda *args, **kwargs: [],
    update_personas_async=_noop,
    sync_update_persona_prompt=_noop,
)
_stub_module("utils.webhooks", conversation_created_webhook=_noop)
_stub_module("utils.task_sync", auto_sync_action_items_batch=_noop)
_stub_module(
    "utils.stt.pre_recorded",
    deepgram_prerecorded=lambda *args, **kwargs: [],
    postprocess_words=lambda *args, **kwargs: [],
)
_stub_module(
    "utils.stt.speech_profile",
    get_speech_profile_matching_predictions=lambda *args, **kwargs: [],
)
_stub_module("utils.stt.vad", vad_is_empty=lambda *args, **kwargs: False)

storage_stub = types.ModuleType("google.cloud.storage")


class _StorageClientStub:
    def __init__(self, *args, **kwargs):
        pass

    def bucket(self, *args, **kwargs):
        return types.SimpleNamespace(blob=lambda *blob_args, **blob_kwargs: types.SimpleNamespace())


storage_stub.Client = _StorageClientStub
storage_stub.transfer_manager = types.SimpleNamespace()
sys.modules["google.cloud.storage"] = storage_stub
try:
    import google.cloud

    google.cloud.storage = storage_stub
except Exception:
    pass

from utils.conversations import process_conversation as conversation_processor
from utils.conversations.failure_state import (
    CONVERSATION_PROCESSING_FAILED,
    CONVERSATION_SUMMARY_FAILED,
    apply_conversation_processing_failed,
    clear_conversation_processing_error,
)
from routers import conversations as conversations_router
from routers import developer as developer_router
from utils.conversations import merge_conversations as merge_processor
from utils.conversations import postprocess_conversation as postprocess_processor


@pytest.fixture(autouse=True)
def _claim_persisted_processing(monkeypatch):
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "claim_initial_conversation_processing",
        lambda uid, conversation_id: {"status": "processing_claimed"},
    )
    for background_target in (
        "save_structured_vector",
        "_extract_memories",
        "_extract_trends",
        "_save_action_items",
        "_update_goal_progress",
        "conversation_created_webhook",
        "update_personas_async",
    ):
        monkeypatch.setattr(conversation_processor, background_target, _noop)


def _long_conversation() -> Conversation:
    text = " ".join(["important"] * 3200)
    return Conversation(
        id="long-transcript-failure",
        created_at=datetime(2026, 7, 8, 19, 43, tzinfo=timezone.utc),
        started_at=datetime(2026, 7, 8, 19, 43, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 8, 20, 10, tzinfo=timezone.utc),
        structured=Structured(),
        transcript_segments=[
            TranscriptSegment(
                text=text,
                speaker="SPEAKER_00",
                is_user=True,
                start=0,
                end=1620,
            )
        ],
        status=ConversationStatus.processing,
        discarded=False,
    )


def _committed_processing_result(payload, *, dispatched=True):
    durable = dict(payload)
    if not durable.get("discarded"):
        active_id = durable.get("active_summary_version_id") or "stock-version-1"
        durable["active_summary_version_id"] = active_id
        durable["summary_versions"] = [
            {
                "id": active_id,
                "created_at": durable["created_at"],
                "source": "omi",
                "kind": "generated",
                "title": durable["structured"].get("title", ""),
                "overview": durable["structured"].get("overview", ""),
                "emoji": durable["structured"].get("emoji", "brain"),
                "category": durable["structured"].get("category", "other"),
                "correction_id": None,
                "based_on_version_id": None,
                "is_active": True,
            }
        ]
    return {
        "status": "committed",
        "active_summary_version_id": durable.get("active_summary_version_id"),
        "conversation": durable,
        "dispatched": dispatched,
    }


def test_processing_failure_helper_preserves_long_transcript_and_retryable_state():
    conversation = _long_conversation()

    result = apply_conversation_processing_failed(conversation)

    assert result.discarded is False
    assert result.status == ConversationStatus.failed
    assert result.processing_error == CONVERSATION_SUMMARY_FAILED
    assert result.processing_error_at is not None
    assert len(result.transcript_segments[0].text) > 25_000


def test_successful_processing_clears_previous_retryable_error():
    conversation = _long_conversation()
    apply_conversation_processing_failed(conversation)

    result = clear_conversation_processing_error(conversation)

    assert result.processing_error is None
    assert result.processing_error_at is None


def test_process_conversation_provider_failure_persists_retryable_payload(monkeypatch):
    conversation = _long_conversation()
    failure_updates = []

    def fail_summary(*args, **kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(conversation_processor, "_get_structured", fail_summary)

    def mark_failed(uid, conversation_id, error_code, failed_at=None):
        failure_updates.append((uid, conversation_id, error_code, failed_at))
        return {"updated": True, "reason": "marked_failed"}

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "mark_conversation_processing_failed_if_unfinished",
        mark_failed,
    )

    with pytest.raises(RuntimeError):
        conversation_processor.process_conversation("uid-1", "en", conversation)

    assert len(failure_updates) == 1
    uid, conversation_id, error_code, failed_at = failure_updates[0]
    assert uid == "uid-1"
    assert conversation_id == "long-transcript-failure"
    assert error_code == CONVERSATION_SUMMARY_FAILED
    assert failed_at is not None
    assert conversation.discarded is False
    assert conversation.status == ConversationStatus.failed
    assert conversation.processing_error == CONVERSATION_SUMMARY_FAILED
    assert conversation.processing_error_at == failed_at

    failed_at = conversation.processing_error_at
    persisted = conversation_processor.mark_unexpected_conversation_processing_failed("uid-1", conversation)

    assert persisted is False
    assert len(failure_updates) == 1
    assert conversation.processing_error_at == failed_at


def test_unexpected_non_summary_failure_uses_distinct_processing_error(monkeypatch):
    conversation = _long_conversation()
    failure_updates = []

    def mark_failed(uid, conversation_id, error_code, failed_at=None):
        failure_updates.append((uid, conversation_id, error_code, failed_at))
        return {"updated": True, "reason": "marked_failed"}

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "mark_conversation_processing_failed_if_unfinished",
        mark_failed,
    )

    persisted = conversation_processor.mark_unexpected_conversation_processing_failed("uid-1", conversation)

    assert persisted is True
    assert len(failure_updates) == 1
    assert failure_updates[0][2] == CONVERSATION_PROCESSING_FAILED
    assert conversation.status == ConversationStatus.failed
    assert conversation.processing_error == CONVERSATION_PROCESSING_FAILED


def test_process_conversation_intentional_discard_stays_completed_discarded(monkeypatch):
    conversation = _long_conversation()
    commits = []

    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (Structured(), True),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda uid, conversation_id, payload, **kwargs: commits.append((uid, conversation_id, payload, kwargs))
        or _committed_processing_result(payload),
    )
    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", None)

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation, is_reprocess=True)
    result = outcome.conversation

    assert result.discarded is True
    assert result.status == ConversationStatus.completed
    assert commits[-1][2]["discarded"] is True
    assert commits[-1][2]["status"] == ConversationStatus.completed
    assert commits[-1][2].get("processing_error") is None
    assert outcome.dispatched is True


def test_process_commits_summary_authority_before_apps_and_postprocess_webhook(monkeypatch):
    conversation = _long_conversation()
    order = []
    downstream_versions = []
    webhook_versions = []
    app_state_updates = []

    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (
            Structured(title="Durable summary", overview="Committed before dispatch."),
            False,
        ),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda uid, conversation_id, payload, **kwargs: order.append("commit")
        or _committed_processing_result({**payload, "active_summary_version_id": "durable-stock-v1"}),
    )
    monkeypatch.setattr(
        conversation_processor,
        "_trigger_apps",
        lambda uid, payload, **kwargs: order.append("apps")
        or setattr(payload, "suggested_summarization_apps", ["summary-assistant"])
        or downstream_versions.append(payload.active_summary_version_id),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "update_conversation",
        lambda uid, conversation_id, payload: order.append("app-state")
        or app_state_updates.append((uid, conversation_id, payload)),
    )
    monkeypatch.setattr(conversation_processor, "record_usage", lambda *args, **kwargs: None)

    def postprocess_webhook(uid, payload):
        order.append("webhook")
        webhook_versions.append(payload.active_summary_version_id)

    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", postprocess_webhook)

    class _PostprocessOnlyThread:
        def __init__(self, target=None, args=(), **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            if self.target is postprocess_webhook:
                self.target(*self.args)

    monkeypatch.setattr(conversation_processor.threading, "Thread", _PostprocessOnlyThread)

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation, is_reprocess=True)
    result = outcome.conversation

    assert order[:4] == ["commit", "apps", "app-state", "webhook"]
    assert result.active_summary_version_id == "durable-stock-v1"
    assert downstream_versions == ["durable-stock-v1"]
    assert webhook_versions == ["durable-stock-v1"]
    assert app_state_updates == [
        (
            "uid-1",
            "long-transcript-failure",
            {
                "apps_results": [],
                "suggested_summarization_apps": ["summary-assistant"],
            },
        )
    ]
    assert len(result.summary_versions) == 1
    assert result.summary_versions[0].source == "omi"
    assert result.summary_versions[0].kind == "generated"
    assert outcome.dispatched is True
    assert outcome.status == "committed"


def test_process_cas_loser_returns_durable_authority_without_dispatch(monkeypatch):
    conversation = _long_conversation()
    side_effects = []
    folder_initializations = []
    durable = conversation.dict()
    durable["structured"] = {
        "title": "Winner",
        "overview": "Already committed by the winning processor.",
        "emoji": "brain",
        "category": "other",
        "action_items": [],
        "events": [],
    }
    durable["status"] = ConversationStatus.completed
    durable["active_summary_version_id"] = "winner-stock-v1"
    durable["summary_versions"] = [
        {
            "id": "winner-stock-v1",
            "created_at": durable["created_at"],
            "source": "omi",
            "kind": "generated",
            "title": "Winner",
            "overview": "Already committed by the winning processor.",
            "emoji": "brain",
            "category": "other",
            "correction_id": None,
            "based_on_version_id": None,
            "is_active": True,
        }
    ]

    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (
            Structured(title="Losing processor", overview="This must not dispatch."),
            False,
        ),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda *args, **kwargs: {
            "status": conversation_processor.conversations_db.conversation_stock_summary_cas_lost,
            "conversation": durable,
            "active_summary_version_id": "winner-stock-v1",
            "dispatched": False,
        },
    )
    monkeypatch.setattr(
        conversation_processor,
        "_trigger_apps",
        lambda *args, **kwargs: side_effects.append("apps"),
    )
    monkeypatch.setattr(
        conversation_processor,
        "fire_postprocess_webhook",
        lambda *args, **kwargs: side_effects.append("webhook"),
    )
    monkeypatch.setattr(conversation_processor.folders_db, "get_folders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        conversation_processor.folders_db,
        "initialize_system_folders",
        lambda *_args, **_kwargs: folder_initializations.append("initialized") or [],
    )

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation)
    result = outcome.conversation

    assert result.active_summary_version_id == "winner-stock-v1"
    assert result.structured.title == "Winner"
    assert side_effects == []
    assert folder_initializations == []
    assert outcome.dispatched is False
    assert outcome.status == conversation_processor.conversations_db.conversation_stock_summary_cas_lost


def test_process_retries_latest_transcript_after_capture_cas_loss(monkeypatch):
    conversation = _long_conversation()
    latest = conversation.dict()
    latest["transcript_segments"] = [
        *latest["transcript_segments"],
        TranscriptSegment(
            text="late durable capture",
            speaker="SPEAKER_00",
            is_user=True,
            start=1621,
            end=1622,
        ).dict(),
    ]
    commits = []
    summaries = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda _uid, _language, current, *_args, **_kwargs: summaries.append(len(current.transcript_segments))
        or (Structured(title="Latest", overview="Latest transcript."), False),
    )

    def commit(_uid, _conversation_id, payload, **_kwargs):
        commits.append(payload)
        if len(commits) == 1:
            return {
                "status": conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
                "conversation": latest,
                "dispatched": False,
            }
        return _committed_processing_result(payload)

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        commit,
    )
    monkeypatch.setattr(
        conversation_processor.folders_db,
        "get_folders",
        lambda *_args, **_kwargs: [{"id": "existing-folder", "name": "Existing"}],
    )
    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", None)

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation)

    assert summaries == [1, 2]
    assert len(commits) == 2
    assert outcome.status == "committed"
    assert outcome.dispatched is True
    assert len(outcome.conversation.transcript_segments) == 2


def test_completed_duplicate_initial_processing_returns_explicit_no_dispatch(monkeypatch):
    conversation = _long_conversation()
    conversation.status = ConversationStatus.completed
    calls = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "claim_initial_conversation_processing",
        lambda uid, conversation_id: {"status": "already_completed"},
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: conversation.dict(),
    )
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: calls.append("summary") or (Structured(), False),
    )

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation)

    assert outcome.conversation.id == conversation.id
    assert outcome.conversation.status == ConversationStatus.completed
    assert outcome.status == "already_completed"
    assert outcome.dispatched is False
    assert calls == []


def test_inflight_duplicate_initial_processing_returns_explicit_no_dispatch(monkeypatch):
    conversation = _long_conversation()
    conversation.status = ConversationStatus.processing
    calls = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "claim_initial_conversation_processing",
        lambda uid, conversation_id: {"status": "processing_in_progress"},
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: conversation.dict(),
    )
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: calls.append("summary") or (Structured(), False),
    )

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation)

    assert outcome.conversation.id == conversation.id
    assert outcome.conversation.status == ConversationStatus.processing
    assert outcome.status == "processing_in_progress"
    assert outcome.dispatched is False
    assert calls == []


def test_explicit_reprocess_of_completed_conversation_remains_authorized(monkeypatch):
    conversation = _long_conversation()
    conversation.status = ConversationStatus.completed
    commits = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (Structured(title="Reprocessed", overview="Explicit request."), False),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda uid, conversation_id, payload, **kwargs: commits.append(kwargs) or _committed_processing_result(payload),
    )
    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", None)

    outcome = conversation_processor.process_conversation_with_outcome(
        "uid-1", "en", conversation, force_process=True, is_reprocess=True
    )

    assert outcome.dispatched is True
    assert outcome.status == "committed"
    assert len(commits) == 1
    assert commits[0]["allow_create"] is False

    durable_completed = conversation.dict()
    durable_completed["status"] = ConversationStatus.completed.value
    transcript_race_commits = []
    recovery_updates = []

    def reject_changed_transcript(_uid, _conversation_id, _payload, **_kwargs):
        transcript_race_commits.append(True)
        return {
            "status": conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
            "conversation": durable_completed,
            "dispatched": False,
        }

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        reject_changed_transcript,
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "update_conversation",
        lambda *_args, **_kwargs: recovery_updates.append((_args, _kwargs)),
    )

    raced_outcome = conversation_processor.process_conversation_with_outcome(
        "uid-1", "en", conversation, force_process=True, is_reprocess=True
    )

    assert len(transcript_race_commits) == 2
    assert raced_outcome.status == conversation_processor.conversations_db.conversation_stock_summary_transcript_changed
    assert raced_outcome.conversation.status == ConversationStatus.completed
    assert recovery_updates == []


def test_transcript_cas_exhaustion_redelivers_a_fresh_processing_invocation(monkeypatch):
    conversation = _long_conversation()
    calls = []
    outcomes = [
        conversation_processor.ConversationProcessingOutcome(
            conversation=conversation,
            dispatched=False,
            status=conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
        ),
        conversation_processor.ConversationProcessingOutcome(
            conversation=conversation,
            dispatched=True,
            status="committed",
        ),
    ]
    monkeypatch.setattr(
        conversation_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)) or outcomes.pop(0),
    )

    outcome = conversation_processor.process_conversation_with_transcript_redelivery(
        "uid-1",
        "en",
        conversation,
    )

    assert outcome.status == "committed"
    assert outcome.dispatched is True
    assert len(calls) == 2


def test_transcript_redelivery_exhaustion_is_failed_instead_of_left_unowned(monkeypatch):
    conversation = _long_conversation()
    calls = []
    failed = []
    monkeypatch.setattr(
        conversation_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs))
        or conversation_processor.ConversationProcessingOutcome(
            conversation=conversation,
            dispatched=False,
            status=conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
            released_claim_token="release-a",
        ),
    )
    monkeypatch.setattr(
        conversation_processor,
        "mark_released_conversation_processing_failed",
        lambda uid, durable, token: failed.append((uid, durable.id, token)) or True,
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "get_conversation",
        lambda *_args: {**conversation.model_dump(), "status": ConversationStatus.failed.value},
    )

    outcome = conversation_processor.process_conversation_with_transcript_redelivery(
        "uid-1",
        "en",
        conversation,
    )

    assert len(calls) == 2
    assert failed == [("uid-1", conversation.id, "release-a")]
    assert outcome.status == conversation_processor.CONVERSATION_TRANSCRIPT_REDELIVERY_EXHAUSTED
    assert outcome.conversation.status == ConversationStatus.failed
    assert outcome.dispatched is False


def test_transcript_redelivery_exhaustion_accepts_a_concurrent_completed_winner(monkeypatch):
    conversation = _long_conversation()
    completed = {**conversation.model_dump(), "status": ConversationStatus.completed.value}
    monkeypatch.setattr(
        conversation_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: conversation_processor.ConversationProcessingOutcome(
            conversation=conversation,
            dispatched=False,
            status=conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
            released_claim_token="release-a",
        ),
    )
    monkeypatch.setattr(
        conversation_processor,
        "mark_released_conversation_processing_failed",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "get_conversation",
        lambda *_args: completed,
    )

    outcome = conversation_processor.process_conversation_with_transcript_redelivery(
        "uid-1",
        "en",
        conversation,
        max_redeliveries=0,
    )

    assert outcome.status == "already_completed"
    assert outcome.conversation.status == ConversationStatus.completed
    assert outcome.dispatched is False


def test_transcript_redelivery_exhaustion_preserves_a_new_processing_claimant(monkeypatch):
    conversation = _long_conversation()
    processing = {**conversation.model_dump(), "status": ConversationStatus.processing.value}
    monkeypatch.setattr(
        conversation_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: conversation_processor.ConversationProcessingOutcome(
            conversation=conversation,
            dispatched=False,
            status=conversation_processor.conversations_db.conversation_stock_summary_transcript_changed,
            released_claim_token="release-a",
        ),
    )
    monkeypatch.setattr(
        conversation_processor,
        "mark_released_conversation_processing_failed",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "get_conversation",
        lambda *_args: processing,
    )

    outcome = conversation_processor.process_conversation_with_transcript_redelivery(
        "uid-1",
        "en",
        conversation,
        max_redeliveries=0,
    )

    assert outcome.status == "processing_in_progress"
    assert outcome.conversation.status == ConversationStatus.processing
    assert outcome.dispatched is False


def test_post_commit_app_failure_does_not_rollback_or_suppress_hermes_dispatch(monkeypatch):
    conversation = _long_conversation()
    order = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (Structured(title="Durable", overview="Committed summary."), False),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda uid, conversation_id, payload, **kwargs: order.append("commit") or _committed_processing_result(payload),
    )

    def fail_apps(*args, **kwargs):
        order.append("apps")
        raise RuntimeError("app failed after commit")

    def postprocess_webhook(uid, payload):
        order.append("webhook")

    monkeypatch.setattr(conversation_processor, "_trigger_apps", fail_apps)
    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", postprocess_webhook)

    class _PostprocessOnlyThread:
        def __init__(self, target=None, args=(), **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            if self.target is postprocess_webhook:
                self.target(*self.args)

    monkeypatch.setattr(conversation_processor.threading, "Thread", _PostprocessOnlyThread)

    outcome = conversation_processor.process_conversation_with_outcome("uid-1", "en", conversation, is_reprocess=True)

    assert outcome.status == "committed"
    assert outcome.dispatched is True
    assert order == ["commit", "apps", "webhook"]


def test_cloud_selected_processing_atomically_queues_hermes_before_post_commit_effects(monkeypatch):
    conversation = _long_conversation()
    commit_kwargs = []
    legacy_webhook_calls = []
    monkeypatch.setattr(conversation_processor, "assert_current_ai_consent", lambda _uid: None)
    monkeypatch.setattr(conversation_processor, "HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS", frozenset({"uid-1"}))
    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (Structured(title="Durable", overview="Queued atomically."), False),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "commit_stock_summary_processing_result",
        lambda uid, conversation_id, payload, **kwargs: commit_kwargs.append(kwargs)
        or {
            **_committed_processing_result(payload),
            "hermes_enrichment_job_id": "hce-job-1",
        },
    )
    monkeypatch.setattr(conversation_processor, "_trigger_apps", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "update_conversation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        conversation_processor,
        "fire_postprocess_webhook",
        lambda *args, **kwargs: legacy_webhook_calls.append((args, kwargs)),
    )

    outcome = conversation_processor.process_conversation_with_outcome(
        "uid-1",
        "en",
        conversation,
        is_reprocess=True,
    )

    assert outcome.dispatched is True
    assert commit_kwargs == [
        {
            "expected_active_summary_version_id": None,
            "allow_create": False,
            "enqueue_hermes_cloud_enrichment": True,
            "expected_transcript_hash": conversation_processor.transcript_grounding_hash(
                conversation.transcript_segments
            ),
        }
    ]
    assert legacy_webhook_calls == []


def test_unexpected_failure_after_durable_completion_is_a_noop(monkeypatch):
    conversation = _long_conversation()
    calls = []
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "mark_conversation_processing_failed_if_unfinished",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"updated": False, "reason": "already_completed"},
    )

    persisted = conversation_processor.mark_unexpected_conversation_processing_failed("uid-1", conversation)

    assert persisted is False
    assert len(calls) == 1
    assert conversation.status == ConversationStatus.processing
    assert conversation.processing_error is None


@pytest.mark.parametrize("relative_path", ["routers/pusher.py", "routers/transcribe.py"])
def test_live_recording_callers_gate_external_integrations_on_dispatch_outcome(relative_path):
    source = (Path(__file__).resolve().parents[2] / relative_path).read_text()

    assert "process_conversation_with_transcript_redelivery" in source
    assert "if outcome.dispatched:" in source
    guarded_block = source.split("if outcome.dispatched:", 1)[1].split("except Exception", 1)[0]
    assert "trigger_external_integrations" in guarded_block


def test_intentional_reprocess_and_destructive_merge_callers_require_explicit_outcomes():
    backend = Path(__file__).resolve().parents[2]
    postprocess_source = (backend / "utils/conversations/postprocess_conversation.py").read_text()
    merge_source = (backend / "utils/conversations/merge_conversations.py").read_text()

    assert "process_conversation_with_outcome(" in postprocess_source
    assert "is_reprocess=True" in postprocess_source
    assert "if not outcome.dispatched:" in postprocess_source
    assert "process_conversation_with_outcome(" in merge_source
    assert "if not processing_outcome.dispatched:" in merge_source
    merge_tree = ast.parse(merge_source)
    perform_merge = next(
        node for node in merge_tree.body if isinstance(node, ast.FunctionDef) and node.name == "perform_merge_async"
    )
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(perform_merge))
    failure_block = merge_source.split("except Exception as e:", 2)[1].split("else:", 1)[0]
    assert "_handle_merge_failure(uid, conversation_ids)" in failure_block
    assert "return" in failure_block


def test_merge_preserves_sources_when_new_conversation_processing_does_not_dispatch(monkeypatch):
    first = _long_conversation().model_dump()
    second = _long_conversation().model_dump()
    first.update({"id": "source-1", "status": ConversationStatus.completed})
    second.update(
        {
            "id": "source-2",
            "created_at": first["created_at"] + timedelta(minutes=30),
            "started_at": first["started_at"] + timedelta(minutes=30),
            "finished_at": first["finished_at"] + timedelta(minutes=30),
            "status": ConversationStatus.completed,
        }
    )
    sources = {first["id"]: first, second["id"]: second}
    deleted = []
    failures = []
    completions = []

    monkeypatch.setattr(
        merge_processor.conversations_db,
        "get_conversation",
        lambda _uid, conversation_id: sources.get(conversation_id),
    )
    monkeypatch.setattr(merge_processor.conversations_db, "upsert_conversation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(merge_processor.conversations_db, "store_conversation_photos", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(merge_processor, "_merge_transcript_segments", lambda _sources: [])
    monkeypatch.setattr(merge_processor, "_collect_all_photos", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(merge_processor, "_copy_audio_chunks_for_merge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        merge_processor,
        "_delete_conversation_and_related_data",
        lambda _uid, conversation_id: deleted.append(conversation_id),
    )
    monkeypatch.setattr(
        merge_processor,
        "_handle_merge_failure",
        lambda uid, conversation_ids: failures.append((uid, list(conversation_ids))),
    )
    monkeypatch.setattr(
        merge_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            conversation=_long_conversation(),
            dispatched=False,
            status="stock_summary_cas_lost",
        ),
    )
    monkeypatch.setattr(
        merge_processor,
        "send_merge_completed_message",
        lambda *_args, **_kwargs: completions.append("completed"),
    )

    merge_processor.perform_merge_async("uid-1", ["source-1", "source-2"])

    assert failures == [("uid-1", ["source-1", "source-2"])]
    assert deleted == []
    assert completions == []


def test_postprocessing_marks_failed_when_summary_reprocess_does_not_dispatch(monkeypatch):
    conversation = _long_conversation()
    statuses = []

    class _Audio:
        duration_seconds = 20
        frame_rate = 8000

        def __getitem__(self, _item):
            return self

        def export(self, *_args, **_kwargs):
            return None

    class _NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(
        postprocess_processor,
        "_get_conversation_by_id",
        lambda _uid, _conversation_id: conversation.model_dump(),
    )
    monkeypatch.setattr(postprocess_processor.AudioSegment, "from_wav", lambda *_args, **_kwargs: _Audio())
    monkeypatch.setattr(postprocess_processor, "vad_is_empty", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(postprocess_processor, "upload_postprocessing_audio", lambda *_args: "https://audio.test/file")
    monkeypatch.setattr(postprocess_processor.threading, "Thread", _NoopThread)
    monkeypatch.setattr(postprocess_processor, "deepgram_prerecorded", lambda *_args, **_kwargs: ["word"])
    monkeypatch.setattr(
        postprocess_processor,
        "postprocess_words",
        lambda *_args, **_kwargs: list(conversation.transcript_segments),
    )
    monkeypatch.setattr(postprocess_processor, "_handle_segment_embedding_matching", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        postprocess_processor.conversations_db,
        "set_postprocessing_status",
        lambda _uid, _conversation_id, status, **kwargs: statuses.append((status, kwargs.get("fail_reason"))),
    )
    monkeypatch.setattr(
        postprocess_processor.conversations_db,
        "store_model_segments_result",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(postprocess_processor.conversations_db, "upsert_conversation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        postprocess_processor,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status="stock_summary_cas_lost",
        ),
    )

    status_code, detail = postprocess_processor.postprocess_conversation(
        conversation.id,
        "/tmp/test.wav",
        "uid-1",
        False,
        "deepgram",
    )

    assert status_code == 500
    assert "stock_summary_cas_lost" in detail
    assert statuses[0][0] == postprocess_processor.PostProcessingStatus.in_progress
    assert statuses[-1][0] == postprocess_processor.PostProcessingStatus.failed
    assert all(status != postprocess_processor.PostProcessingStatus.completed for status, _reason in statuses)


def test_manual_conversation_processing_suppresses_integrations_for_non_dispatch_outcome(monkeypatch):
    conversation = _long_conversation()
    integration_calls = []
    monkeypatch.setattr(
        conversations_router,
        "retrieve_in_progress_conversation",
        lambda _uid: conversation.model_dump(),
    )
    monkeypatch.setattr(conversations_router.redis_db, "get_in_progress_conversation_id", lambda _uid: "")
    monkeypatch.setattr(conversations_router.redis_db, "get_cached_user_geolocation", lambda _uid: None)
    monkeypatch.setattr(
        conversations_router,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status="already_completed",
        ),
    )
    monkeypatch.setattr(
        conversations_router,
        "trigger_external_integrations",
        lambda *args, **kwargs: integration_calls.append((args, kwargs)),
    )

    response = conversations_router.process_in_progress_conversation(uid="uid-1")

    assert response.conversation.id == conversation.id
    assert response.messages == []
    assert integration_calls == []


def test_manual_conversation_processing_rejects_live_capture_owner(monkeypatch):
    conversation = _long_conversation()
    monkeypatch.setattr(
        conversations_router,
        "retrieve_in_progress_conversation",
        lambda _uid: conversation.model_dump(),
    )
    monkeypatch.setattr(
        conversations_router.redis_db,
        "get_in_progress_conversation_id",
        lambda _uid: conversation.id,
    )
    monkeypatch.setattr(
        conversations_router.redis_db,
        "get_in_progress_conversation_owner",
        lambda _uid: "socket-active",
    )

    with pytest.raises(conversations_router.HTTPException) as raised:
        conversations_router.process_in_progress_conversation(uid="uid-1")

    assert raised.value.status_code == 409
    assert "still active" in raised.value.detail


def test_manual_conversation_processing_targets_exact_closed_conversation(monkeypatch):
    conversation = _long_conversation()
    processed = []
    monkeypatch.setattr(
        conversations_router.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: (
            conversation.model_dump() if (uid, conversation_id) == ("uid-1", conversation.id) else None
        ),
    )
    monkeypatch.setattr(
        conversations_router.redis_db,
        "get_in_progress_conversation_id",
        lambda _uid: "replacement-conversation",
    )
    monkeypatch.setattr(conversations_router.redis_db, "get_cached_user_geolocation", lambda _uid: None)
    monkeypatch.setattr(
        conversations_router,
        "process_conversation_with_outcome",
        lambda uid, language, target, force_process: (
            processed.append((uid, target.id, force_process))
            or types.SimpleNamespace(conversation=target, dispatched=False, status="already_completed")
        ),
    )

    response = conversations_router.process_in_progress_conversation(
        request=conversations_router.ProcessConversationRequest(conversation_id=conversation.id),
        uid="uid-1",
    )

    assert response.conversation.id == conversation.id
    assert processed == [("uid-1", conversation.id, True)]


class _FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDocumentRef:
    def __init__(self, data=None):
        self.snapshot = _FakeSnapshot(data)

    def get(self, transaction=None):
        return self.snapshot


class _FakeTransaction:
    def __init__(self):
        self.updates = []
        self.sets = []

    def update(self, ref, data):
        self.updates.append((ref, data))

    def set(self, ref, data):
        self.sets.append((ref, data))


def _claim_retry(conversation_data, request_id, retry_data=None):
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(conversation_data)
    retry_ref = _FakeDocumentRef(retry_data)
    requested_at = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    result = conversation_processor.conversations_db._claim_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        requested_at,
    )
    return result, transaction


def test_retry_claim_is_atomic_and_preserves_failed_error_provenance():
    conversation = _long_conversation()
    apply_conversation_processing_failed(conversation)

    result, transaction = _claim_retry(conversation.dict(), "request-a")

    assert result["outcome"] == "claimed"
    assert "conversation" not in result
    assert len(transaction.updates) == 1
    assert transaction.updates[0][1]["status"] == ConversationStatus.processing.value
    assert transaction.updates[0][1]["processing_retry_id"] == "request-a"
    assert conversation.processing_error == CONVERSATION_SUMMARY_FAILED
    assert len(transaction.sets) == 1
    assert transaction.sets[0][1]["outcome"] == ConversationStatus.processing.value


def test_retry_claim_reuses_completed_durable_request_receipt_without_enqueuing():
    conversation = _long_conversation()
    conversation.status = ConversationStatus.processing
    conversation.processing_retry_id = "newer-request"

    result, transaction = _claim_retry(
        conversation.dict(),
        "older-request",
        retry_data={"request_id": "older-request", "outcome": "completed"},
    )

    assert result["outcome"] == "completed"
    assert transaction.updates == []
    assert transaction.sets == []


def test_retry_claim_rejects_a_different_request_while_processing():
    conversation = _long_conversation()
    conversation.status = ConversationStatus.processing
    conversation.processing_retry_id = "active-request"
    conversation.processing_retry_lease_expires_at = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc) + timedelta(
        minutes=5
    )

    result, transaction = _claim_retry(conversation.dict(), "other-request")

    assert result["outcome"] == "busy"
    assert transaction.updates == []
    assert transaction.sets == []


def _conversation_api_client():
    app = FastAPI()
    app.include_router(conversations_router.router)
    app.dependency_overrides[conversations_router.auth.get_current_user_uid] = lambda: "authenticated-user"
    return app, TestClient(app)


def test_failed_conversations_api_is_uid_scoped_and_preserves_long_transcript(monkeypatch):
    conversation = _long_conversation()
    apply_conversation_processing_failed(conversation)
    calls = []

    def fake_get_conversations(uid, limit, offset, **kwargs):
        calls.append((uid, limit, offset, kwargs))
        return [conversation.dict()]

    monkeypatch.setattr(conversations_router.conversations_db, "get_conversations", fake_get_conversations)
    app, client = _conversation_api_client()
    try:
        response = client.get("/v1/conversations?statuses=failed&include_discarded=true")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert calls[0][0] == "authenticated-user"
    assert calls[0][3]["statuses"] == ["failed"]
    payload = response.json()[0]
    assert payload["status"] == "failed"
    assert payload["processing_error"] == CONVERSATION_SUMMARY_FAILED
    assert payload["processing_error_at"] is not None
    assert len(payload["transcript_segments"][0]["text"]) > 25_000


def test_conversation_delete_offloads_blocking_stores_from_event_loop(monkeypatch):
    loop_thread = None
    blocking_threads = []

    def blocking_call(*_args):
        blocking_threads.append(threading.get_ident())
        return {}

    async def invalidate(*_args):
        assert threading.get_ident() == loop_thread
        return 1

    monkeypatch.setattr(conversations_router, "_get_valid_conversation_by_id", blocking_call)
    monkeypatch.setattr(conversations_router.conversations_db, "delete_conversation", blocking_call)
    monkeypatch.setattr(conversations_router, "delete_vector", blocking_call)
    monkeypatch.setattr(conversations_router, "invalidate_deleted_conversation_source", invalidate)

    async def run():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        return await conversations_router.delete_conversation("conversation-a", "uid-a")

    result = asyncio.run(run())

    assert result == {"status": "Ok"}
    assert len(blocking_threads) == 3
    assert all(thread_id != loop_thread for thread_id in blocking_threads)


def test_developer_conversation_delete_offloads_blocking_stores_from_event_loop(monkeypatch):
    loop_thread = None
    blocking_threads = []

    def blocking_get(*_args):
        blocking_threads.append(threading.get_ident())
        return {"id": "conversation-a"}

    def blocking_delete(*_args):
        blocking_threads.append(threading.get_ident())

    async def invalidate(*_args):
        assert threading.get_ident() == loop_thread
        return 1

    monkeypatch.setattr(developer_router.conversations_db, "get_conversation", blocking_get)
    monkeypatch.setattr(developer_router.conversations_db, "delete_conversation", blocking_delete)
    monkeypatch.setattr(developer_router, "invalidate_deleted_conversation_source", invalidate)

    async def run():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        return await developer_router.delete_conversation_endpoint("conversation-a", "uid-a")

    result = asyncio.run(run())

    assert result == {"success": True}
    assert len(blocking_threads) == 2
    assert all(thread_id != loop_thread for thread_id in blocking_threads)
