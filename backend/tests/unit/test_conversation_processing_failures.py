from datetime import datetime, timezone
import os
import sys
import types

import pytest

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


_stub_module(
    "database.vector_db",
    find_similar_memories=lambda *args, **kwargs: [],
    upsert_memory_vector=_noop,
    delete_memory_vector=_noop,
    upsert_vector2=_noop,
    update_vector_metadata=_noop,
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
_stub_module("utils.other.storage", list_audio_chunks=lambda *args, **kwargs: [], precache_conversation_audio=_noop)
_stub_module(
    "utils.notifications",
    send_important_conversation_message=_noop,
    send_notification=_noop,
    send_action_item_data_message=_noop,
    send_apple_reminders_sync_push=_noop,
)
_stub_module(
    "utils.apps",
    get_available_apps=lambda *args, **kwargs: [],
    update_personas_async=_noop,
    sync_update_persona_prompt=_noop,
)
_stub_module("utils.webhooks", conversation_created_webhook=_noop)
_stub_module("utils.task_sync", auto_sync_action_items_batch=_noop)

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
    CONVERSATION_SUMMARY_FAILED,
    apply_conversation_processing_failed,
    clear_conversation_processing_error,
)


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
    writes = []

    def fail_summary(*args, **kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(conversation_processor, "_get_structured", fail_summary)
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "upsert_conversation",
        lambda uid, payload: writes.append((uid, payload)),
    )

    with pytest.raises(RuntimeError):
        conversation_processor.process_conversation("uid-1", "en", conversation)

    assert len(writes) == 1
    uid, payload = writes[0]
    assert uid == "uid-1"
    assert payload["id"] == "long-transcript-failure"
    assert payload["discarded"] is False
    assert payload["status"] == ConversationStatus.failed
    assert payload["processing_error"] == CONVERSATION_SUMMARY_FAILED
    assert payload["processing_error_at"] is not None
    assert len(payload["transcript_segments"][0]["text"]) > 25_000


def test_process_conversation_intentional_discard_stays_completed_discarded(monkeypatch):
    conversation = _long_conversation()
    writes = []

    monkeypatch.setattr(
        conversation_processor,
        "_get_structured",
        lambda *args, **kwargs: (Structured(), True),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "upsert_conversation",
        lambda uid, payload: writes.append((uid, payload)),
    )
    monkeypatch.setattr(conversation_processor, "fire_postprocess_webhook", None)

    result = conversation_processor.process_conversation("uid-1", "en", conversation, is_reprocess=True)

    assert result.discarded is True
    assert result.status == ConversationStatus.completed
    assert writes[-1][1]["discarded"] is True
    assert writes[-1][1]["status"] == ConversationStatus.completed
    assert writes[-1][1].get("processing_error") is None
