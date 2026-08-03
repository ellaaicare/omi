from datetime import datetime, timedelta, timezone
import os
import sys
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
    storage_client=types.SimpleNamespace(),
)
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
    CONVERSATION_PROCESSING_FAILED,
    CONVERSATION_SUMMARY_FAILED,
    apply_conversation_processing_failed,
    clear_conversation_processing_error,
)
from routers import conversations as conversations_router


@pytest.fixture(autouse=True)
def _isolate_conversation_meeting_cache(monkeypatch):
    monkeypatch.setattr(conversation_processor.redis_db, "get_conversation_meeting_id", lambda _conversation_id: None)


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

    failed_at = conversation.processing_error_at
    persisted = conversation_processor.mark_unexpected_conversation_processing_failed("uid-1", conversation)

    assert persisted is False
    assert len(writes) == 1
    assert conversation.processing_error_at == failed_at


def test_unexpected_non_summary_failure_uses_distinct_processing_error(monkeypatch):
    conversation = _long_conversation()
    writes = []
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        "upsert_conversation",
        lambda uid, payload: writes.append((uid, payload)),
    )

    persisted = conversation_processor.mark_unexpected_conversation_processing_failed("uid-1", conversation)

    assert persisted is True
    assert len(writes) == 1
    assert writes[0][1]["status"] == ConversationStatus.failed
    assert writes[0][1]["processing_error"] == CONVERSATION_PROCESSING_FAILED


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
    app.dependency_overrides[conversations_router.auth.get_writable_user_uid] = lambda: "authenticated-user"
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
