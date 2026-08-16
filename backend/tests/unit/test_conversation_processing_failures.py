from datetime import datetime, timedelta, timezone
import asyncio
import binascii
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


def test_capture_finalization_v2_rejects_missing_and_stale_protocol_authority_tuple(monkeypatch):
    app, client, state = _mount_capture_finalization_route(monkeypatch)
    state['conversations']['capture-a'].update(
        {
            'capture_protocol_version': 2,
            'capture_generation': 'generation-a',
            'capture_owner_token': 'owner-a',
            'capture_state': 'drained',
        }
    )
    claims = []

    def reject_stale(*args):
        claims.append(args)
        return {'outcome': 'mismatch'}

    monkeypatch.setattr(conversations_router.conversations_db, 'claim_capture_finalization', reject_stale)
    try:
        missing = client.post('/v1/conversations', json={'conversation_id': 'capture-a'})
        stale = client.post(
            '/v1/conversations',
            json={
                'conversation_id': 'capture-a',
                'protocol_version': 2,
                'generation': 'generation-stale',
                'owner_token': 'owner-a',
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 409
    assert stale.status_code == 409
    assert len(claims) == 1
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


def test_capture_writer_rechecks_firestore_status_inside_transaction_after_finalization_wins():
    transaction = _FakeTransaction()
    authority_ref = _FakeDocumentRef(
        {
            'protocol_version': 2,
            'conversation_id': 'capture-a',
            'generation': 'generation-a',
            'owner_token': 'owner-a',
            'state': 'active',
        }
    )
    conversation_ref = _FakeDocumentRef(
        {
            'id': 'capture-a',
            'status': ConversationStatus.processing.value,
            'data_protection_level': 'standard',
        }
    )

    updated = conversations_router.conversations_db._update_in_progress_conversation_capture_transaction(
        transaction,
        authority_ref,
        conversation_ref,
        'authenticated-user',
        'capture-a',
        'generation-a',
        'owner-a',
        {'finished_at': datetime(2026, 8, 15, tzinfo=timezone.utc)},
        [],
        [],
    )

    assert updated is None
    assert transaction.updates == []
    assert transaction.sets == []


class _LegacyWriterDocument:
    def __init__(self, data):
        self.data = data
        self.writes = []

    def get(self, *args, **kwargs):
        return _FakeSnapshot(self.data)

    def update(self, update):
        self.writes.append(update)
        self.data.update(update)


class _LegacyWriterDb:
    def __init__(self, document):
        self._document = document

    def collection(self, *args):
        return self

    def document(self, *args):
        return self._document if args and args[-1] == 'capture-a' else self


def _v2_authority(conversation_id='capture-a', generation='generation-a', owner='owner-a', state='active'):
    return {
        'protocol_version': 2,
        'conversation_id': conversation_id,
        'generation': generation,
        'owner_token': owner,
        'state': state,
        'lease_expires_at': datetime(2026, 8, 15, 1, 5, tzinfo=timezone.utc),
    }


def _v2_conversation(generation='generation-a', owner='owner-a', state='active', status='in_progress'):
    return {
        'id': 'capture-a',
        'status': status,
        'data_protection_level': 'standard',
        'transcript_segments': [],
        'capture_protocol_version': 2,
        'capture_generation': generation,
        'capture_owner_token': owner,
        'capture_state': state,
    }


def test_capture_v2_fails_closed_until_actual_legacy_writer_drain(monkeypatch):
    """The unchanged base helper proves a Redis/Firestore field cannot fence it."""
    legacy_document = _LegacyWriterDocument(_v2_conversation())
    monkeypatch.setattr(conversations_router.conversations_db, 'db', _LegacyWriterDb(legacy_document))
    monkeypatch.delenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', raising=False)

    conversations_router.conversations_db.update_conversation(
        'authenticated-user',
        'capture-a',
        {'finished_at': datetime(2026, 8, 15, 1, 2, tzinfo=timezone.utc)},
    )

    assert legacy_document.writes
    assert capture_authority.capture_protocol_v2_rollout_enabled() is False
    monkeypatch.setenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', 'legacy_workers_drained')
    assert capture_authority.capture_protocol_v2_rollout_enabled() is True


def test_capture_stale_pause_then_successor_adopt_rejects_write_and_stale_arrays():
    transaction = _FakeTransaction()
    authority_ref = _FakeDocumentRef(_v2_authority(generation='generation-b', owner='owner-b'))
    conversation_ref = _FakeDocumentRef(_v2_conversation(generation='generation-b', owner='owner-b'))

    result = conversations_router.conversations_db._update_in_progress_conversation_capture_transaction(
        transaction,
        authority_ref,
        conversation_ref,
        'authenticated-user',
        'capture-a',
        'generation-a',
        'owner-a',
        {'finished_at': datetime(2026, 8, 15, 1, 2, tzinfo=timezone.utc)},
        [TranscriptSegment(id='stale', text='stale', start=0, end=1, is_user=False)],
        [],
    )

    assert result is None
    assert transaction.updates == []
    assert transaction.sets == []


def test_capture_finalization_process_kill_boundary_reclaims_expired_durable_lease():
    started = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)
    first_transaction = _FakeTransaction()
    first = conversations_router.conversations_db._claim_capture_finalization_transaction(
        first_transaction,
        _FakeDocumentRef(_v2_authority(state='drained')),
        _FakeDocumentRef(_v2_conversation(state='drained')),
        'capture-a',
        2,
        'generation-a',
        'owner-a',
        'claim-a',
        started,
        5,
    )
    assert first['outcome'] == 'claimed'

    killed_process_document = {
        **_v2_conversation(state='finalizing', status='processing'),
        'capture_finalization_claim_token': 'claim-a',
        'capture_finalization_lease_expires_at': started + timedelta(seconds=5),
        'capture_finalization_attempt_count': 1,
    }
    second = conversations_router.conversations_db._claim_capture_finalization_transaction(
        _FakeTransaction(),
        _FakeDocumentRef(_v2_authority(state='finalizing')),
        _FakeDocumentRef(killed_process_document),
        'capture-a',
        2,
        'generation-a',
        'owner-a',
        'claim-b',
        started + timedelta(seconds=6),
        5,
    )
    assert second['outcome'] == 'claimed'
    assert second['claim_token'] == 'claim-b'


def test_capture_stale_finalizer_cannot_persist_after_expired_lease_is_reclaimed():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            **_v2_conversation(state='finalizing', status='processing'),
            'capture_finalization_claim_token': 'claim-b',
        }
    )

    persisted = conversations_router.conversations_db._upsert_conversation_if_capture_finalizer_transaction(
        transaction,
        conversation_ref,
        'authenticated-user',
        {
            **_v2_conversation(state='finalizing', status='completed'),
            'transcript_segments': [{'id': 'stale-result', 'text': 'stale'}],
        },
        'generation-a',
        'owner-a',
        'claim-a',
    )

    assert persisted is False
    assert transaction.sets == []


def test_capture_successor_installation_is_one_atomic_transaction_without_rollback_delete():
    transaction = _FakeTransaction()
    authority_ref = _FakeDocumentRef(_v2_authority())
    predecessor_ref = _FakeDocumentRef(_v2_conversation())
    successor_ref = _FakeDocumentRef()
    successor = {
        'id': 'capture-b',
        'status': 'in_progress',
        'created_at': datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc),
        'started_at': datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc),
        'finished_at': datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc),
        'structured': {},
        'transcript_segments': [],
    }

    outcome = conversations_router.conversations_db._install_capture_conversation_transaction(
        transaction,
        authority_ref,
        successor_ref,
        'authenticated-user',
        successor,
        'generation-a',
        'owner-a',
        datetime(2026, 8, 15, 1, 1, tzinfo=timezone.utc),
        'capture-a',
        predecessor_ref,
    )

    assert outcome == 'installed'
    assert [ref for ref, _ in transaction.sets] == [successor_ref, authority_ref]
    assert transaction.updates[0][0] is predecessor_ref
    assert transaction.updates[0][1]['capture_state'] == 'drained'


def test_capture_drain_waits_for_final_segment_persistence_scheduling_race():
    async def scenario():
        drained = asyncio.Event()
        order = []

        async def persist_tail():
            await asyncio.sleep(0)
            order.append('tail-durable')
            drained.set()

        task = asyncio.create_task(persist_tail())

        async def finish_stt():
            order.append('stt-finished')

        flushed = await capture_authority.flush_capture_before_drained(finish_stt, {task}, drained, timeout=1)
        order.append('drained-emitted')
        return flushed, order

    flushed, order = asyncio.run(scenario())
    assert flushed is True
    assert order == ['stt-finished', 'tail-durable', 'drained-emitted']


def test_capture_redis_keys_share_real_cluster_slot():
    keys = conversations_router.redis_db.capture_stream_keys('authenticated-user')

    def cluster_slot(key):
        tag = key[key.index('{') + 1 : key.index('}')]
        return binascii.crc_hqx(tag.encode(), 0) % 16384

    assert len(set(keys)) == 3
    assert len({cluster_slot(key) for key in keys}) == 1
    assert all('{capture:authenticated-user}' in key for key in keys)


def test_capture_drain_body_rejects_missing_wrong_and_stale_authority_fields():
    valid = {
        'type': 'capture_drain',
        'protocol_version': 2,
        'conversation_id': 'capture-a',
        'generation': 'generation-a',
        'owner_token': 'owner-a',
    }
    assert capture_authority.valid_capture_drain_body(valid, 2, 'capture-a', 'generation-a', 'owner-a')
    for field in ('protocol_version', 'conversation_id', 'generation', 'owner_token'):
        missing = {key: value for key, value in valid.items() if key != field}
        assert not capture_authority.valid_capture_drain_body(missing, 2, 'capture-a', 'generation-a', 'owner-a')
    assert not capture_authority.valid_capture_drain_body(
        {**valid, 'generation': 'generation-stale'},
        2,
        'capture-a',
        'generation-a',
        'owner-a',
    )
