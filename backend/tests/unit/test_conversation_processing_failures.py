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
from utils.conversations import capture_authority
from utils.conversations.failure_state import (
    CONVERSATION_PROCESSING_FAILED,
    CONVERSATION_SUMMARY_FAILED,
    apply_conversation_processing_failed,
    clear_conversation_processing_error,
)
from routers import conversations as conversations_router


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


def _capture_finalization_conversation(conversation_id: str, status: ConversationStatus) -> dict:
    conversation = _long_conversation()
    conversation.id = conversation_id
    conversation.status = status
    conversation.language = 'en'
    return conversation.dict()


def _mount_capture_finalization_route(monkeypatch, *, conversation_id='capture-a', pointer='capture-a'):
    state = {
        'conversations': {
            conversation_id: _capture_finalization_conversation(conversation_id, ConversationStatus.in_progress)
        },
        'pointer': pointer,
        'claims': 0,
        'processes': 0,
        'integrations': 0,
        'compare_deletes': 0,
        'restores': 0,
        'successor_during_processing': None,
    }

    def get_conversation(uid, requested_id):
        assert uid == 'authenticated-user'
        conversation = state['conversations'].get(requested_id)
        return dict(conversation) if conversation else None

    def claim_pointer(uid, requested_id):
        assert uid == 'authenticated-user'
        state['claims'] += 1
        expected_fence = f'processing:{requested_id}'
        if state['pointer'] == requested_id:
            state['pointer'] = expected_fence
            return 'claimed'
        if state['pointer'] == expected_fence:
            return 'already_claimed'
        return 'mismatch'

    def claim_conversation(uid, requested_id):
        conversation = state['conversations'].get(requested_id)
        if not conversation or conversation['status'] != ConversationStatus.in_progress:
            return False
        conversation['status'] = ConversationStatus.processing
        return True

    def process(uid, language, conversation, force_process=False):
        assert uid == 'authenticated-user'
        assert language == 'en'
        assert force_process is True
        state['processes'] += 1
        if state['successor_during_processing']:
            state['pointer'] = state['successor_during_processing']
        conversation.status = ConversationStatus.completed
        state['conversations'][conversation.id] = conversation.dict()
        return conversation

    def integrations(uid, conversation):
        assert uid == 'authenticated-user'
        assert conversation.id == conversation_id
        state['integrations'] += 1
        return []

    def terminalize_pointer(uid, requested_id):
        assert uid == 'authenticated-user'
        state['compare_deletes'] += 1
        if state['pointer'] not in {None, requested_id, f'processing:{requested_id}'}:
            return 'mismatch'
        state['pointer'] = None
        return 'terminalized'

    def restore_pointer(uid, requested_id):
        assert uid == 'authenticated-user'
        state['restores'] += 1
        if state['pointer'] != f'processing:{requested_id}':
            return False
        state['pointer'] = requested_id
        return True

    monkeypatch.setattr(conversations_router.conversations_db, 'get_conversation', get_conversation)
    monkeypatch.setattr(conversations_router.conversations_db, 'claim_in_progress_conversation', claim_conversation)
    monkeypatch.setattr(conversations_router.redis_db, 'claim_in_progress_conversation_id', claim_pointer)
    monkeypatch.setattr(
        conversations_router.redis_db,
        'terminalize_in_progress_conversation_id',
        terminalize_pointer,
    )
    monkeypatch.setattr(
        conversations_router.redis_db,
        'restore_in_progress_conversation_id_if_matches',
        restore_pointer,
    )
    monkeypatch.setattr(conversations_router.redis_db, 'get_cached_user_geolocation', lambda uid: None)
    monkeypatch.setattr(conversations_router, 'process_conversation', process)
    monkeypatch.setattr(conversations_router, 'trigger_external_integrations', integrations)
    app, client = _conversation_api_client()
    return app, client, state


def test_capture_finalization_route_requires_exact_id_and_rejects_non_owner_before_mutation(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    try:
        missing = client.post('/v1/conversations', json={})
        wrong_owner = client.post('/v1/conversations', json={'conversation_id': 'not-owned'})
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 422
    assert wrong_owner.status_code == 404
    assert state['claims'] == 0
    assert state['processes'] == 0
    assert state['integrations'] == 0
    assert state['pointer'] == 'capture-a'


def test_capture_finalization_route_successor_mismatch_fails_before_any_conversation_mutation(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch, pointer='successor-capture')
    try:
        response = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert state['conversations']['capture-a']['status'] == ConversationStatus.in_progress
    assert state['processes'] == 0
    assert state['integrations'] == 0
    assert state['compare_deletes'] == 0
    assert state['pointer'] == 'successor-capture'


def test_capture_finalization_route_cross_id_document_fails_before_pointer_claim(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    state['conversations']['capture-a']['id'] = 'successor-capture'
    try:
        response = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert state['claims'] == 0
    assert state['processes'] == 0
    assert state['integrations'] == 0
    assert state['pointer'] == 'capture-a'


def test_capture_finalization_lost_ack_and_repeats_converge_without_duplicate_side_effects(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    try:
        lost_acknowledgement = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
        state['pointer'] = 'successor-capture'
        replay = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
        repeated = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert lost_acknowledgement.status_code == 200
    assert replay.status_code == 200
    assert repeated.status_code == 200
    assert replay.json()['conversation']['id'] == 'capture-a'
    assert replay.json()['conversation']['status'] == ConversationStatus.completed.value
    assert state['processes'] == 1
    assert state['integrations'] == 1
    assert state['compare_deletes'] == 3
    assert state['pointer'] == 'successor-capture'


def test_capture_finalization_compare_delete_cannot_clear_successor_race(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    state['successor_during_processing'] = 'successor-capture'
    try:
        response = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert state['processes'] == 1
    assert state['integrations'] == 1
    assert state['compare_deletes'] == 1
    assert state['pointer'] == 'successor-capture'


def test_capture_finalization_processing_replay_does_not_touch_successor_or_side_effects(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch, pointer='successor-capture')
    state['conversations']['capture-a']['status'] = ConversationStatus.processing
    try:
        response = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['conversation']['status'] == ConversationStatus.processing.value
    assert state['claims'] == 0
    assert state['processes'] == 0
    assert state['integrations'] == 0
    assert state['compare_deletes'] == 1
    assert state['pointer'] == 'successor-capture'


def test_capture_finalization_pointer_acquisition_failure_preserves_exact_pointer(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)

    def fail_pointer_claim(uid, requested_id):
        raise RuntimeError('redis acquisition failed')

    monkeypatch.setattr(conversations_router.redis_db, 'claim_in_progress_conversation_id', fail_pointer_claim)
    try:
        with pytest.raises(RuntimeError, match='redis acquisition failed'):
            client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert state['pointer'] == 'capture-a'
    assert state['conversations']['capture-a']['status'] == ConversationStatus.in_progress
    assert state['processes'] == 0
    assert state['restores'] == 0
    assert state['compare_deletes'] == 0


@pytest.mark.parametrize('failure_boundary', ['enrichment', 'firestore_claim'])
def test_capture_finalization_preclaim_failure_restores_exact_pointer_for_safe_replay(monkeypatch, failure_boundary):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    original_claim = conversations_router.conversations_db.claim_in_progress_conversation
    failed = False

    def fail_enrichment_once(uid):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError(f'{failure_boundary} failed')
        return None

    def fail_claim_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError(f'{failure_boundary} failed')
        return original_claim(*args, **kwargs)

    if failure_boundary == 'enrichment':
        monkeypatch.setattr(conversations_router.redis_db, 'get_cached_user_geolocation', fail_enrichment_once)
    else:
        monkeypatch.setattr(conversations_router.conversations_db, 'claim_in_progress_conversation', fail_claim_once)

    try:
        with pytest.raises(RuntimeError, match=f'{failure_boundary} failed'):
            client.post('/v1/conversations', json={'conversation_id': 'capture-a'})

        assert state['pointer'] == 'capture-a'
        assert state['conversations']['capture-a']['status'] == ConversationStatus.in_progress
        assert state['restores'] == 1
        assert state['compare_deletes'] == 0

        replay = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert replay.status_code == 200
    assert replay.json()['conversation']['id'] == 'capture-a'
    assert replay.json()['conversation']['status'] == ConversationStatus.completed.value
    assert state['processes'] == 1
    assert state['integrations'] == 1
    assert state['pointer'] is None


def test_capture_finalization_preclaim_failure_retains_an_existing_processing_fence(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch, pointer='processing:capture-a')
    failed = False

    def fail_enrichment_once(uid):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError('enrichment failed')
        return None

    monkeypatch.setattr(conversations_router.redis_db, 'get_cached_user_geolocation', fail_enrichment_once)
    try:
        with pytest.raises(RuntimeError, match='enrichment failed'):
            client.post('/v1/conversations', json={'conversation_id': 'capture-a'})

        assert state['pointer'] == 'processing:capture-a'
        assert state['restores'] == 0
        replay = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert replay.status_code == 200
    assert replay.json()['conversation']['id'] == 'capture-a'
    assert state['processes'] == 1
    assert state['pointer'] is None


def test_capture_finalization_preclaim_restore_cannot_overwrite_successor_or_finalize_wrong_capture(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)

    def fail_enrichment(uid):
        raise RuntimeError('enrichment failed')

    def successor_wins_restore(uid, requested_id):
        assert state['pointer'] == f'processing:{requested_id}'
        state['restores'] += 1
        state['pointer'] = 'successor-capture'
        return False

    monkeypatch.setattr(conversations_router.redis_db, 'get_cached_user_geolocation', fail_enrichment)
    monkeypatch.setattr(
        conversations_router.redis_db,
        'restore_in_progress_conversation_id_if_matches',
        successor_wins_restore,
    )
    try:
        with pytest.raises(RuntimeError, match='enrichment failed'):
            client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
        retry = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
    finally:
        app.dependency_overrides.clear()

    assert retry.status_code == 409
    assert state['pointer'] == 'successor-capture'
    assert state['conversations']['capture-a']['status'] == ConversationStatus.in_progress
    assert state['processes'] == 0
    assert state['integrations'] == 0
    assert state['compare_deletes'] == 0


def test_capture_finalization_firestore_claim_is_atomic_and_exact():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef({'id': 'capture-a', 'status': ConversationStatus.in_progress.value})

    claimed = conversations_router.conversations_db._claim_in_progress_conversation_transaction(
        transaction,
        conversation_ref,
    )

    assert claimed is True
    assert transaction.updates == [(conversation_ref, {'status': ConversationStatus.processing.value})]


class _ExpiringCaptureAuthorityRedis:
    def __init__(self, pointer=None, authority=None):
        self.now = 0
        self.pointer = pointer
        self.authority = authority
        self.rollback = None
        self.pointer_expires_at = 300 if pointer is not None else None

    def _expire_if_needed(self):
        if self.pointer_expires_at is not None and self.now >= self.pointer_expires_at:
            self.pointer = None
            self.pointer_expires_at = None

    def advance(self, seconds):
        self.now += seconds
        self._expire_if_needed()

    def legacy_refresh(self, conversation_id, ttl=300):
        """Model the pre-generation pointer-only restore used by old workers."""
        self._expire_if_needed()
        if self.pointer == conversation_id:
            self.pointer_expires_at = self.now + ttl
            return 'refreshed'
        if self.pointer is None:
            self.pointer = conversation_id
            self.pointer_expires_at = self.now + ttl
            return 'restored'
        return 'mismatch'

    def eval(self, script, key_count, *keys_and_args):
        keys = keys_and_args[:key_count]
        args = keys_and_args[key_count:]
        expected_keys = (
            'users:authenticated-user:in_progress_memory_id',
            'users:authenticated-user:capture_stream_authority',
        )
        if key_count == 3:
            expected_keys += ('users:authenticated-user:capture_stream_authority_rollback',)
        assert keys == expected_keys
        self._expire_if_needed()

        if 'capture_authority:acquire' in script:
            conversation_id, ttl, authority = args
            if self.pointer is not None:
                predecessor_id = self.pointer.removeprefix('terminal:')
                if (
                    not self.pointer.startswith('terminal:')
                    or self.authority is None
                    or not self.authority.startswith('terminal:')
                    or not self.authority.endswith(f':{predecessor_id}')
                ):
                    return 0
                self.rollback = f'{self.pointer}|{self.authority}'
            elif self.authority is not None and self.authority.startswith('terminal:'):
                predecessor_id = self.authority.rsplit(':', 1)[-1]
                self.rollback = f'terminal:{predecessor_id}|{self.authority}'
            else:
                self.rollback = None
            self.pointer = conversation_id
            self.pointer_expires_at = self.now + int(ttl)
            self.authority = authority
            return 1

        if 'capture_authority:confirm_acquire' in script:
            conversation_id, authority = args
            if self.pointer != conversation_id or self.authority != authority:
                return 0
            self.rollback = None
            return 1

        if 'capture_authority:adopt' in script:
            conversation_id, ttl, authority = args
            if self.pointer not in {None, conversation_id}:
                return 0
            if self.authority is not None:
                active_match = self.authority.startswith('active:') and self.authority.endswith(f':{conversation_id}')
                legacy_successor = (
                    self.pointer == conversation_id
                    and self.authority.startswith('terminal:')
                    and not self.authority.endswith(f':{conversation_id}')
                )
                if not active_match and not legacy_successor:
                    return 0
            self.pointer = conversation_id
            self.pointer_expires_at = self.now + int(ttl)
            self.authority = authority
            return 1

        if 'capture_authority:refresh' in script:
            conversation_id, ttl, authority = args
            if self.authority != authority:
                return 0
            if self.pointer == conversation_id:
                self.pointer_expires_at = self.now + int(ttl)
                return 1
            if self.pointer is None:
                self.pointer = conversation_id
                self.pointer_expires_at = self.now + int(ttl)
                return 2
            return 0

        if 'capture_authority:rotate' in script:
            current_id, new_id, ttl, expected_authority, new_authority = args
            if self.pointer != current_id or self.authority != expected_authority:
                return 0
            self.pointer = new_id
            self.pointer_expires_at = self.now + int(ttl)
            self.authority = new_authority
            return 1

        if 'capture_authority:claim' in script:
            conversation_id, processing_fence, ttl = args
            active_authority = (
                self.authority is not None
                and self.authority.startswith('active:')
                and self.authority.endswith(f':{conversation_id}')
            )
            if self.pointer == conversation_id or (self.pointer is None and active_authority):
                if self.authority is not None and (
                    not self.authority.startswith('active:') or not self.authority.endswith(f':{conversation_id}')
                ):
                    return 0
                if self.authority is None:
                    self.authority = f'active:legacy:{conversation_id}'
                self.pointer = processing_fence
                self.pointer_expires_at = self.now + int(ttl)
                self.authority = self.authority.replace('active:', 'finalizing:', 1)
                return 1
            if self.pointer == processing_fence:
                if self.authority is None:
                    self.authority = f'finalizing:legacy:{conversation_id}'
                    return 2
                if self.authority.startswith('finalizing:') and self.authority.endswith(f':{conversation_id}'):
                    return 2
            return 0

        if 'capture_authority:restore' in script:
            processing_fence, conversation_id, ttl = args
            if self.pointer not in {None, processing_fence} or self.authority is None:
                return 0
            if not self.authority.startswith('finalizing:') or not self.authority.endswith(f':{conversation_id}'):
                return 0
            self.pointer = conversation_id
            self.pointer_expires_at = self.now + int(ttl)
            self.authority = self.authority.replace('finalizing:', 'active:', 1)
            return 1

        if 'capture_authority:terminalize' in script:
            conversation_id, processing_fence = args
            terminal_fence = f'terminal:{conversation_id}'
            if self.authority is not None and self.authority.startswith('terminal:'):
                if self.authority.endswith(f':{conversation_id}'):
                    if self.pointer is None:
                        self.pointer = terminal_fence
                        self.pointer_expires_at = None
                        return 2
                    if self.pointer == terminal_fence:
                        return 2
                return 0
            if self.pointer not in {None, conversation_id, processing_fence}:
                return 0
            if self.authority is not None:
                if not self.authority.endswith(f':{conversation_id}') or not self.authority.startswith(
                    ('active:', 'finalizing:')
                ):
                    return 0
                generation_and_conversation = self.authority.split(':', 1)[1]
            else:
                generation_and_conversation = f'legacy:{conversation_id}'
            self.authority = f'terminal:{generation_and_conversation}'
            self.pointer = terminal_fence
            self.pointer_expires_at = None
            return 1

        if 'capture_authority:release_failed_acquire' in script:
            conversation_id, authority = args
            if self.pointer != conversation_id or self.authority != authority:
                return 0
            if self.rollback is not None:
                self.pointer, self.authority = self.rollback.split('|', 1)
            else:
                self.pointer = None
                self.authority = None
            self.pointer_expires_at = None
            self.rollback = None
            return 1

        raise AssertionError('unexpected Redis script')


def test_capture_finalization_redis_claim_and_terminal_release_preserve_successor(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis(
        pointer='capture-a',
        authority='active:generation-a:capture-a',
    )
    monkeypatch.setattr(conversations_router.redis_db, 'r', fake_redis)

    assert (
        conversations_router.redis_db.claim_in_progress_conversation_id('authenticated-user', 'capture-a') == 'claimed'
    )
    assert fake_redis.pointer == 'processing:capture-a'
    assert fake_redis.authority == 'finalizing:generation-a:capture-a'

    fake_redis.pointer = 'successor-capture'
    fake_redis.authority = 'active:generation-b:successor-capture'
    assert (
        conversations_router.redis_db.terminalize_in_progress_conversation_id('authenticated-user', 'capture-a')
        == 'mismatch'
    )
    assert fake_redis.pointer == 'successor-capture'
    assert fake_redis.authority == 'active:generation-b:successor-capture'


def test_capture_finalization_active_generation_survives_five_minutes_and_exactly_restores_expired_pointer(
    monkeypatch,
):
    fake_redis = _ExpiringCaptureAuthorityRedis()
    monkeypatch.setattr(conversations_router.redis_db, 'r', fake_redis)

    assert (
        conversations_router.redis_db.acquire_capture_stream_authority(
            'authenticated-user',
            'generation-a',
            'capture-a',
        )
        == 'acquired'
    )
    fake_redis.advance(295)
    assert (
        conversations_router.redis_db.refresh_in_progress_conversation_id(
            'authenticated-user',
            'generation-a',
            'capture-a',
        )
        == 'refreshed'
    )
    fake_redis.advance(10)
    assert fake_redis.now > 300
    assert fake_redis.pointer == 'capture-a'

    fake_redis.advance(301)
    assert fake_redis.pointer is None
    assert (
        conversations_router.redis_db.refresh_in_progress_conversation_id(
            'authenticated-user',
            'generation-a',
            'capture-a',
        )
        == 'restored'
    )
    assert fake_redis.pointer == 'capture-a'
    assert fake_redis.authority == 'active:generation-a:capture-a'

    fake_redis.advance(301)
    assert fake_redis.pointer is None
    assert (
        conversations_router.redis_db.claim_in_progress_conversation_id('authenticated-user', 'capture-a') == 'claimed'
    )
    assert fake_redis.pointer == 'processing:capture-a'
    assert fake_redis.authority == 'finalizing:generation-a:capture-a'

    fake_redis.advance(301)
    assert fake_redis.pointer is None
    assert (
        conversations_router.redis_db.restore_in_progress_conversation_id_if_matches(
            'authenticated-user',
            'capture-a',
        )
        is True
    )
    assert fake_redis.pointer == 'capture-a'
    assert fake_redis.authority == 'active:generation-a:capture-a'


def test_capture_finalization_active_generation_refresh_never_overwrites_successor(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis(
        pointer='successor-capture',
        authority='active:generation-b:successor-capture',
    )
    monkeypatch.setattr(conversations_router.redis_db, 'r', fake_redis)

    assert (
        conversations_router.redis_db.refresh_in_progress_conversation_id(
            'authenticated-user',
            'generation-a',
            'capture-a',
        )
        == 'mismatch'
    )
    assert fake_redis.pointer == 'successor-capture'
    assert fake_redis.authority == 'active:generation-b:successor-capture'


def test_capture_finalization_stream_reconnect_adopts_exact_conversation_and_retires_old_generation(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis(
        pointer='capture-a',
        authority='active:generation-a:capture-a',
    )
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    old_stream = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')
    reconnected_stream = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-b')

    assert reconnected_stream.adopt('capture-a', 'resume') is True
    assert fake_redis.pointer == 'capture-a'
    assert fake_redis.authority == 'active:generation-b:capture-a'
    assert old_stream.refresh('capture-a', 'stale_reconnect_checkpoint') is False
    assert reconnected_stream.refresh('capture-a', 'lifecycle') is True


def test_capture_finalization_redis_rotation_is_exact_and_preserves_successor(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis(
        pointer='capture-a',
        authority='active:generation-a:capture-a',
    )
    monkeypatch.setattr(conversations_router.redis_db, 'r', fake_redis)

    assert (
        conversations_router.redis_db.rotate_in_progress_conversation_id(
            'authenticated-user',
            'generation-a',
            'capture-a',
            'capture-a-next',
        )
        == 'rotated'
    )
    assert fake_redis.pointer == 'capture-a-next'
    assert fake_redis.authority == 'active:generation-a:capture-a-next'
    assert fake_redis.pointer_expires_at == 300

    fake_redis.pointer = 'capture-b'
    fake_redis.authority = 'active:generation-b:capture-b'
    assert (
        conversations_router.redis_db.rotate_in_progress_conversation_id(
            'authenticated-user',
            'generation-a',
            'capture-a-next',
            'capture-a-late',
        )
        == 'mismatch'
    )
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'


def _mount_capture_stream_authority(monkeypatch, pointer='capture-a'):
    authority = {
        'capture-a': 'active:generation-a:capture-a',
        'capture-b': 'active:generation-b:capture-b',
        'processing:capture-a': 'finalizing:generation-a:capture-a',
    }.get(pointer)
    fake_redis = _ExpiringCaptureAuthorityRedis(pointer=pointer, authority=authority)
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    return fake_redis


@pytest.mark.parametrize('checkpoint', ['resume', 'segment_photo_update', 'lifecycle'])
def test_capture_finalization_live_stream_mismatch_paths_are_terminal(monkeypatch, checkpoint):
    fake_redis = _mount_capture_stream_authority(monkeypatch, pointer='capture-b')
    losses = []
    durable_updates = []
    authority = capture_authority.CaptureStreamAuthority(
        'authenticated-user',
        'generation-a',
        lambda lost_checkpoint, conversation_id: losses.append((lost_checkpoint, conversation_id)),
    )

    if checkpoint == 'segment_photo_update':
        result = authority.run_if_owned('capture-a', checkpoint, lambda: durable_updates.append('segment-photo'))
        assert result is None
    else:
        assert authority.refresh('capture-a', checkpoint) is False

    assert authority.lost is True
    assert losses == [(checkpoint, 'capture-a')]
    assert durable_updates == []
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'
    assert authority.refresh('capture-a', 'later_work') is False


def test_capture_finalization_overlapping_stream_successor_blocks_stale_updates_processing_and_split(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis()
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    durable = {
        'capture_b_stubs': 0,
        'capture_a_segments': 0,
        'capture_a_photos': 0,
        'capture_a_finished_at': 0,
        'capture_a_processing': 0,
        'capture_a_integrations': 0,
        'capture_a_new_stubs': 0,
    }
    stream_a = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')
    stream_b = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-b')

    assert stream_a.acquire('capture-a', lambda: None, 'initial_acquisition') is True
    fake_redis.advance(301)
    assert fake_redis.pointer is None

    def install_capture_b():
        durable['capture_b_stubs'] += 1

    assert stream_b.acquire('capture-b', install_capture_b, 'initial_acquisition') is True
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'

    def persist_stale_data():
        durable['capture_a_segments'] += 1
        durable['capture_a_photos'] += 1
        durable['capture_a_finished_at'] += 1

    assert stream_a.run_if_owned('capture-a', 'segment_photo_update', persist_stale_data) is None
    if stream_a.refresh('capture-a', 'lifecycle'):
        rotated = stream_a.rotate(
            'capture-a',
            'capture-a-next',
            lambda: durable.__setitem__('capture_a_new_stubs', durable['capture_a_new_stubs'] + 1),
            'conversation_rotation',
        )
        if rotated:
            durable['capture_a_processing'] += 1
            durable['capture_a_integrations'] += 1

    assert (
        stream_a.rotate(
            'capture-a',
            'capture-a-next',
            lambda: durable.__setitem__('capture_a_new_stubs', durable['capture_a_new_stubs'] + 1),
            'conversation_rotation',
        )
        is False
    )
    assert stream_b.refresh('capture-b', 'lifecycle') is True
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'
    assert durable == {
        'capture_b_stubs': 1,
        'capture_a_segments': 0,
        'capture_a_photos': 0,
        'capture_a_finished_at': 0,
        'capture_a_processing': 0,
        'capture_a_integrations': 0,
        'capture_a_new_stubs': 0,
    }


def test_capture_finalization_normal_same_stream_rotation_is_exact_and_bounded(monkeypatch):
    fake_redis = _mount_capture_stream_authority(monkeypatch)
    installed_with_pointer = []
    authority = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')

    assert (
        authority.rotate(
            'capture-a',
            'capture-a-next',
            lambda: installed_with_pointer.append(fake_redis.pointer),
            'conversation_rotation',
        )
        is True
    )

    assert fake_redis.pointer == 'capture-a-next'
    assert fake_redis.authority == 'active:generation-a:capture-a-next'
    assert fake_redis.pointer_expires_at == 300
    assert installed_with_pointer == ['capture-a-next']


def test_capture_finalization_rotation_never_overwrites_processing_fence_or_successor(monkeypatch):
    for protected_pointer in ('processing:capture-a', 'capture-b'):
        fake_redis = _mount_capture_stream_authority(monkeypatch, pointer=protected_pointer)
        installed = []
        authority = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')

        assert (
            authority.rotate(
                'capture-a',
                'capture-a-next',
                lambda: installed.append('stub'),
                'conversation_rotation',
            )
            is False
        )
        assert authority.lost is True
        assert fake_redis.pointer == protected_pointer
        assert installed == []


def test_capture_finalization_terminal_release_blocks_stale_generation_restoration_and_rotation(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis()
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    stream = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')

    assert stream.acquire('capture-a', lambda: None, 'initial_acquisition') is True
    assert (
        conversations_router.redis_db.claim_in_progress_conversation_id('authenticated-user', 'capture-a') == 'claimed'
    )
    assert fake_redis.pointer == 'processing:capture-a'
    assert fake_redis.authority == 'finalizing:generation-a:capture-a'
    assert (
        conversations_router.redis_db.terminalize_in_progress_conversation_id('authenticated-user', 'capture-a')
        == 'terminalized'
    )
    assert fake_redis.pointer == 'terminal:capture-a'
    assert fake_redis.authority == 'terminal:generation-a:capture-a'

    assert stream.refresh('capture-a', 'stale_post_stop_refresh') is False
    assert stream.lost is True
    assert fake_redis.pointer == 'terminal:capture-a'
    assert (
        stream.rotate('capture-a', 'capture-a-next', lambda: pytest.fail('stale stream installed a stub'), 'lifecycle')
        is False
    )


def test_capture_finalization_interleaves_replacement_and_legacy_restore_behind_terminal_fence(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis()
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    stream_a = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a')

    assert stream_a.acquire('capture-a', lambda: None, 'initial_acquisition') is True
    assert (
        conversations_router.redis_db.claim_in_progress_conversation_id('authenticated-user', 'capture-a') == 'claimed'
    )

    replacement_during_finalization = capture_authority.CaptureStreamAuthority(
        'authenticated-user',
        'generation-too-early',
    )
    assert replacement_during_finalization.adopt('capture-a', 'resume') is False
    assert fake_redis.pointer == 'processing:capture-a'
    assert fake_redis.authority == 'finalizing:generation-a:capture-a'

    assert (
        conversations_router.redis_db.terminalize_in_progress_conversation_id('authenticated-user', 'capture-a')
        == 'terminalized'
    )
    assert fake_redis.pointer == 'terminal:capture-a'
    assert fake_redis.legacy_refresh('capture-a') == 'mismatch'
    assert fake_redis.pointer == 'terminal:capture-a'
    assert fake_redis.authority == 'terminal:generation-a:capture-a'

    successor = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-b')
    installed = []
    assert successor.acquire('capture-b', lambda: installed.append('capture-b'), 'initial_acquisition') is True
    assert installed == ['capture-b']
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'
    assert fake_redis.rollback is None

    assert fake_redis.legacy_refresh('capture-a') == 'mismatch'
    assert stream_a.refresh('capture-a', 'stale_worker_after_successor') is False
    assert fake_redis.pointer == 'capture-b'
    assert fake_redis.authority == 'active:generation-b:capture-b'

    # A pointer-only rollout worker may create a fresh successor with an
    # unconditional legacy SET. A current worker may adopt that different id,
    # but never the terminal predecessor itself.
    fake_redis.pointer = 'capture-c'
    fake_redis.pointer_expires_at = fake_redis.now + 300
    fake_redis.authority = 'terminal:generation-a:capture-a'
    current_after_legacy_successor = capture_authority.CaptureStreamAuthority(
        'authenticated-user',
        'generation-c',
    )
    assert current_after_legacy_successor.adopt('capture-c', 'resume') is True
    assert fake_redis.pointer == 'capture-c'
    assert fake_redis.authority == 'active:generation-c:capture-c'


def test_capture_finalization_failed_successor_install_restores_terminal_fence(monkeypatch):
    fake_redis = _ExpiringCaptureAuthorityRedis(
        pointer='terminal:capture-a',
        authority='terminal:generation-a:capture-a',
    )
    fake_redis.pointer_expires_at = None
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    successor = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-b')

    with pytest.raises(RuntimeError, match='stub install failed'):
        successor.acquire(
            'capture-b',
            lambda: (_ for _ in ()).throw(RuntimeError('stub install failed')),
            'initial_acquisition',
        )

    assert fake_redis.pointer == 'terminal:capture-a'
    assert fake_redis.authority == 'terminal:generation-a:capture-a'
    assert fake_redis.rollback is None
    assert fake_redis.legacy_refresh('capture-a') == 'mismatch'


@pytest.mark.parametrize('ordering', [('stop', 'disconnect'), ('disconnect', 'stop')])
def test_capture_finalization_stop_disconnect_ordering_converges_terminal(monkeypatch, ordering):
    fake_redis = _ExpiringCaptureAuthorityRedis()
    monkeypatch.setattr(capture_authority.redis_db, 'r', fake_redis)
    losses = []
    stream = capture_authority.CaptureStreamAuthority(
        'authenticated-user',
        'generation-a',
        lambda checkpoint, conversation_id: losses.append((checkpoint, conversation_id)),
    )
    assert stream.acquire('capture-a', lambda: None, 'initial_acquisition') is True

    stop_has_run = False
    for operation in ordering:
        if operation == 'disconnect':
            # Transport teardown does not release stream authority. Before POST it stays
            # active; after POST it must not reopen the terminal generation.
            if stop_has_run:
                assert fake_redis.pointer == 'terminal:capture-a'
                assert fake_redis.authority == 'terminal:generation-a:capture-a'
            else:
                assert fake_redis.pointer == 'capture-a'
                assert fake_redis.authority == 'active:generation-a:capture-a'
        else:
            assert (
                conversations_router.redis_db.claim_in_progress_conversation_id('authenticated-user', 'capture-a')
                == 'claimed'
            )
            assert (
                conversations_router.redis_db.terminalize_in_progress_conversation_id(
                    'authenticated-user',
                    'capture-a',
                )
                == 'terminalized'
            )
            stop_has_run = True

    assert stream.refresh('capture-a', 'post_stop_checkpoint') is False
    assert losses == [('post_stop_checkpoint', 'capture-a')]
    assert fake_redis.pointer == 'terminal:capture-a'
    assert fake_redis.authority == 'terminal:generation-a:capture-a'
