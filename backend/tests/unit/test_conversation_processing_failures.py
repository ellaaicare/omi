from datetime import datetime, timedelta, timezone
import asyncio
import binascii
import os
from pathlib import Path
import re
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

import database.user_usage as user_usage_db

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


def _apply_updates(document: dict, transaction: _FakeTransaction, ref: _FakeDocumentRef) -> dict:
    updated = dict(document)
    for updated_ref, values in transaction.updates:
        if updated_ref is ref:
            updated.update(values)
    return updated


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


def _mount_capture_finalization_route(
    monkeypatch,
    *,
    conversation_id='capture-a',
    pointer='capture-a',
    protocol_v2=False,
):
    initial_conversation = _capture_finalization_conversation(conversation_id, ConversationStatus.in_progress)
    if protocol_v2:
        initial_conversation.update(
            {
                'capture_protocol_version': 2,
                'capture_generation': 'generation-a',
                'capture_owner_token': 'owner-a',
                'capture_state': 'drained',
            }
        )
    state = {
        'conversations': {conversation_id: initial_conversation},
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

    def process(uid, language, conversation, force_process=False, capture_finalization=None):
        assert uid == 'authenticated-user'
        assert language == 'en'
        assert force_process is True
        state['processes'] += 1
        if state['successor_during_processing']:
            state['pointer'] = state['successor_during_processing']
        conversation.status = ConversationStatus.completed
        durable = conversation.dict()
        durable.update(
            {key: value for key, value in state['conversations'][conversation.id].items() if key.startswith('capture_')}
        )
        state['conversations'][conversation.id] = durable
        return conversation

    def integrations(uid, conversation, idempotency_key=None):
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
    if protocol_v2:

        def claim_finalization(uid, requested_id, version, generation, owner_token, claim_token):
            conversation = state['conversations'][requested_id]
            if (version, generation, owner_token) != (2, 'generation-a', 'owner-a'):
                return {'outcome': 'mismatch'}
            if conversation.get('capture_state') == 'terminal':
                return {'outcome': 'settled', 'conversation': dict(conversation)}
            conversation['capture_state'] = 'finalizing'
            conversation['capture_finalization_claim_token'] = claim_token
            if conversation['status'] not in {
                ConversationStatus.completed.value,
                ConversationStatus.failed.value,
            }:
                conversation['status'] = ConversationStatus.processing.value
            return {'outcome': 'claimed', 'conversation': dict(conversation)}

        def renew_finalization(uid, requested_id, claim_token, generation, owner_token, **kwargs):
            conversation = state['conversations'][requested_id]
            return (
                conversation.get('capture_state') == 'finalizing'
                and conversation.get('capture_generation') == generation
                and conversation.get('capture_owner_token') == owner_token
                and conversation.get('capture_finalization_claim_token') == claim_token
            )

        def claim_effect(uid, requested_id, generation, owner_token, claim_token, effect_id):
            if not renew_finalization(uid, requested_id, claim_token, generation, owner_token):
                return {'outcome': 'lost'}
            effects = state['conversations'][requested_id].setdefault('capture_finalization_effects', {})
            receipt = effects.get(effect_id)
            if receipt and receipt['state'] == 'completed':
                return {'outcome': 'completed', **receipt}
            operation_token = (receipt or {}).get('operation_token', f'operation:{effect_id}')
            effects[effect_id] = {
                'state': 'claimed',
                'claim_token': claim_token,
                'operation_token': operation_token,
            }
            return {'outcome': 'claimed', 'operation_token': operation_token}

        def complete_effect(
            uid,
            requested_id,
            generation,
            owner_token,
            claim_token,
            effect_id,
            operation_token,
            result=None,
        ):
            if not renew_finalization(uid, requested_id, claim_token, generation, owner_token):
                return False
            receipt = state['conversations'][requested_id]['capture_finalization_effects'][effect_id]
            if receipt['operation_token'] != operation_token or receipt['claim_token'] != claim_token:
                return False
            receipt.update({'state': 'completed', 'result': result})
            return True

        def complete_finalization(uid, requested_id, generation, owner_token, claim_token):
            if not renew_finalization(uid, requested_id, claim_token, generation, owner_token):
                return False
            integration = (
                state['conversations'][requested_id]
                .get('capture_finalization_effects', {})
                .get('integrations:external', {})
            )
            if integration.get('state') != 'completed':
                return False
            state['conversations'][requested_id]['capture_state'] = 'terminal'
            return True

        monkeypatch.setattr(conversations_router.conversations_db, 'claim_capture_finalization', claim_finalization)
        monkeypatch.setattr(conversations_router.conversations_db, 'renew_capture_finalization', renew_finalization)
        monkeypatch.setattr(conversations_router.conversations_db, 'claim_capture_finalization_effect', claim_effect)
        monkeypatch.setattr(
            conversations_router.conversations_db, 'complete_capture_finalization_effect', complete_effect
        )
        monkeypatch.setattr(
            conversations_router.conversations_db, 'complete_capture_finalization', complete_finalization
        )
        monkeypatch.setattr(conversations_router.redis_db, 'project_capture_stream_authority', lambda *args: True)
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
    app, client, state = _mount_capture_finalization_route(monkeypatch, protocol_v2=True)
    request = {
        'conversation_id': 'capture-a',
        'protocol_version': 2,
        'generation': 'generation-a',
        'owner_token': 'owner-a',
    }
    try:
        lost_acknowledgement = client.post('/v1/conversations', json=request)
        state['pointer'] = 'successor-capture'
        replay = client.post('/v1/conversations', json=request)
        repeated = client.post('/v1/conversations', json=request)
    finally:
        app.dependency_overrides.clear()

    assert lost_acknowledgement.status_code == 200
    assert replay.status_code == 200
    assert repeated.status_code == 200
    assert replay.json()['conversation']['id'] == 'capture-a'
    assert replay.json()['conversation']['status'] == ConversationStatus.completed.value
    assert state['processes'] == 1
    assert state['integrations'] == 1
    assert state['conversations']['capture-a']['capture_protocol_version'] == 2
    assert state['conversations']['capture-a']['capture_state'] == 'terminal'
    assert (
        state['conversations']['capture-a']['capture_finalization_effects']['integrations:external']['state']
        == 'completed'
    )
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


def test_capture_successor_install_publishes_each_exact_ready_tuple(monkeypatch):
    authority = capture_authority.CaptureStreamAuthority('authenticated-user', 'generation-a', 'owner-a')
    installs = []
    published = []

    monkeypatch.setattr(
        authority,
        'acquire',
        lambda conversation, checkpoint: installs.append(('acquire', conversation['id'], checkpoint)) or True,
    )
    monkeypatch.setattr(
        authority,
        'rotate',
        lambda current_id, conversation, checkpoint: installs.append(
            ('rotate', current_id, conversation['id'], checkpoint)
        )
        or True,
    )

    async def publish(protocol_version, conversation_id, generation, owner_token):
        published.append((protocol_version, conversation_id, generation, owner_token))
        return True

    async def scenario():
        initial = await authority.install_and_publish_ready({'id': 'capture-a'}, 'initial', publish)
        successor = await authority.install_and_publish_ready(
            {'id': 'capture-b'},
            'rotation',
            publish,
            expected_conversation_id='capture-a',
        )
        return initial, successor

    assert asyncio.run(scenario()) == (True, True)
    assert installs == [
        ('acquire', 'capture-a', 'initial'),
        ('rotate', 'capture-a', 'capture-b', 'rotation'),
    ]
    assert published == [
        (2, 'capture-a', 'generation-a', 'owner-a'),
        (2, 'capture-b', 'generation-a', 'owner-a'),
    ]


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


def test_capture_pre_v2_clients_are_rejected_before_creation_and_leave_zero_active_state():
    class _Socket:
        def __init__(self):
            self.closes = []

        async def close(self, *, code, reason):
            self.closes.append((code, reason))

    async def scenario(protocol_version):
        socket = _Socket()
        active = {'created': 0, 'drained': 0, 'finalized': 0}
        accepted = await capture_authority.require_capture_protocol_before_creation(socket, protocol_version)
        if accepted:
            active['created'] += 1
        return accepted, socket.closes, active

    for installed_protocol in (0, 1):
        accepted, closes, active = asyncio.run(scenario(installed_protocol))
        assert accepted is False
        assert closes == [
            (
                capture_authority.CAPTURE_PROTOCOL_UPGRADE_CLOSE_CODE,
                capture_authority.CAPTURE_PROTOCOL_UPGRADE_REASON,
            )
        ]
        assert active == {'created': 0, 'drained': 0, 'finalized': 0}

    accepted, closes, active = asyncio.run(scenario(2))
    assert accepted is True
    assert closes == []
    assert active['created'] == 1


def test_capture_drain_rechecks_idle_after_provider_returns_with_late_tail():
    async def scenario():
        drained = asyncio.Event()
        drained.set()  # The old bug observed this idle indication too early.
        tasks = set()
        durable = []

        async def persist_tail():
            await asyncio.sleep(0)
            durable.append('late-soniox-tail')
            drained.set()

        async def provider_receiver():
            await asyncio.sleep(0)
            drained.clear()
            persistence = asyncio.create_task(persist_tail())
            tasks.add(persistence)
            persistence.add_done_callback(tasks.discard)

        async def finish_provider():
            receiver = asyncio.create_task(provider_receiver())
            tasks.add(receiver)
            receiver.add_done_callback(tasks.discard)

        flushed = await capture_authority.flush_capture_before_drained(finish_provider, tasks, drained, timeout=1)
        return flushed, durable, tasks

    flushed, durable, tasks = asyncio.run(scenario())
    assert flushed is True
    assert durable == ['late-soniox-tail']
    assert tasks == set()


def test_capture_drain_barrier_waits_for_copied_final_provider_tail_before_finalization_claim():
    async def scenario():
        buffers = []
        durable = []
        tasks = set()
        buffers_drained = asyncio.Event()
        buffers_drained.set()
        copied_and_cleared = asyncio.Event()
        allow_durable_write = asyncio.Event()
        claim_attempts = []

        async def persist_like_stream_handler():
            segments_to_process = list(buffers)
            buffers.clear()
            copied_and_cleared.set()
            await allow_durable_write.wait()
            durable.extend(segments_to_process)
            buffers_drained.set()

        async def finish_provider_inputs():
            buffers_drained.clear()
            buffers.append('tail-only-final-segment')
            task = asyncio.create_task(persist_like_stream_handler())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        drain = asyncio.create_task(
            capture_authority.flush_capture_before_drained(
                finish_provider_inputs,
                tasks,
                buffers_drained,
                timeout=1,
            )
        )
        await copied_and_cleared.wait()
        assert buffers == []
        assert not drain.done()
        assert claim_attempts == []

        allow_durable_write.set()
        assert await drain is True
        claim_attempts.append('finalization-claim-after-drained-ack')
        return durable, claim_attempts

    durable, claim_attempts = asyncio.run(scenario())
    assert durable == ['tail-only-final-segment']
    assert claim_attempts == ['finalization-claim-after-drained-ack']


def test_capture_finalization_expired_lease_cannot_be_renewed_or_persisted():
    now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
    expired = {
        **_v2_conversation(state='finalizing', status='processing'),
        'capture_finalization_claim_token': 'claim-a',
        'capture_finalization_lease_expires_at': now - timedelta(seconds=1),
    }
    conversation_ref = _FakeDocumentRef(expired)
    transaction = _FakeTransaction()

    renewed = conversations_router.conversations_db._renew_capture_finalization_transaction(
        transaction,
        conversation_ref,
        'claim-a',
        now,
        15,
        'generation-a',
        'owner-a',
    )
    persisted = conversations_router.conversations_db._upsert_conversation_if_capture_finalizer_transaction(
        transaction,
        conversation_ref,
        'authenticated-user',
        {**expired, 'status': 'completed'},
        'generation-a',
        'owner-a',
        'claim-a',
        now,
    )

    assert renewed is False
    assert persisted is False
    assert transaction.updates == []
    assert transaction.sets == []


def test_capture_effect_claim_after_successful_guard_rejects_expired_predecessor():
    started = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
    predecessor = {
        **_v2_conversation(state='finalizing', status='processing'),
        'capture_finalization_claim_token': 'claim-a',
        'capture_finalization_lease_expires_at': started + timedelta(seconds=5),
    }
    renew_transaction = _FakeTransaction()
    assert conversations_router.conversations_db._renew_capture_finalization_transaction(
        renew_transaction,
        _FakeDocumentRef(predecessor),
        'claim-a',
        started,
        5,
        'generation-a',
        'owner-a',
    )

    expired = {**predecessor, 'capture_finalization_lease_expires_at': started + timedelta(seconds=5)}
    reclaim_transaction = _FakeTransaction()
    reclaim_ref = _FakeDocumentRef(expired)
    reclaimed = conversations_router.conversations_db._claim_capture_finalization_transaction(
        reclaim_transaction,
        _FakeDocumentRef(_v2_authority(state='finalizing')),
        reclaim_ref,
        'capture-a',
        2,
        'generation-a',
        'owner-a',
        'claim-b',
        started + timedelta(seconds=6),
        5,
    )
    assert reclaimed['outcome'] == 'claimed'

    successor_ref = _FakeDocumentRef(reclaimed['conversation'])
    successor_effect = conversations_router.conversations_db._claim_capture_finalization_effect_transaction(
        _FakeTransaction(),
        successor_ref,
        'generation-a',
        'owner-a',
        'claim-b',
        'integrations:external',
        started + timedelta(seconds=6),
    )
    stale_effect = conversations_router.conversations_db._claim_capture_finalization_effect_transaction(
        _FakeTransaction(),
        successor_ref,
        'generation-a',
        'owner-a',
        'claim-a',
        'integrations:external',
        started + timedelta(seconds=6),
    )

    assert successor_effect['outcome'] == 'claimed'
    assert stale_effect == {'outcome': 'lost'}


def test_capture_finalization_resumes_after_result_persistence_at_every_terminal_boundary():
    started = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)
    for status in (ConversationStatus.completed.value, ConversationStatus.failed.value):
        for boundary in ('before_integration', 'before_terminal'):
            effects = {}
            if boundary == 'before_terminal':
                effects['integrations:external'] = {
                    'state': 'completed',
                    'operation_token': 'stable-operation',
                    'claim_token': 'claim-a',
                    'result': [],
                }
            killed = {
                **_v2_conversation(state='finalizing', status=status),
                'capture_finalization_claim_token': 'claim-a',
                'capture_finalization_lease_expires_at': started,
                'capture_finalization_effects': effects,
            }
            claim_transaction = _FakeTransaction()
            conversation_ref = _FakeDocumentRef(killed)
            reclaimed = conversations_router.conversations_db._claim_capture_finalization_transaction(
                claim_transaction,
                _FakeDocumentRef(_v2_authority(state='finalizing')),
                conversation_ref,
                'capture-a',
                2,
                'generation-a',
                'owner-a',
                'claim-b',
                started + timedelta(seconds=1),
                5,
            )
            assert reclaimed['outcome'] == 'claimed'
            assert reclaimed['conversation']['status'] == status

            reclaimed_document = reclaimed['conversation']
            reclaimed_ref = _FakeDocumentRef(reclaimed_document)
            effect_transaction = _FakeTransaction()
            effect = conversations_router.conversations_db._claim_capture_finalization_effect_transaction(
                effect_transaction,
                reclaimed_ref,
                'generation-a',
                'owner-a',
                'claim-b',
                'integrations:external',
                started + timedelta(seconds=1),
            )
            if boundary == 'before_integration':
                assert effect['outcome'] == 'claimed'
                effect_document = _apply_updates(reclaimed_document, effect_transaction, reclaimed_ref)
                effect_ref = _FakeDocumentRef(effect_document)
                complete_effect_transaction = _FakeTransaction()
                assert conversations_router.conversations_db._complete_capture_finalization_effect_transaction(
                    complete_effect_transaction,
                    effect_ref,
                    'generation-a',
                    'owner-a',
                    'claim-b',
                    'integrations:external',
                    effect['operation_token'],
                    [],
                    started + timedelta(seconds=1),
                )
                terminal_document = _apply_updates(effect_document, complete_effect_transaction, effect_ref)
            else:
                assert effect['outcome'] == 'completed'
                terminal_document = reclaimed_document

            terminal_transaction = _FakeTransaction()
            assert conversations_router.conversations_db._complete_capture_finalization_transaction(
                terminal_transaction,
                _FakeDocumentRef(_v2_authority(state='finalizing')),
                _FakeDocumentRef(terminal_document),
                'capture-a',
                'generation-a',
                'owner-a',
                'claim-b',
                started + timedelta(seconds=1),
            )


def test_capture_finalization_effect_paths_reject_wrong_owner_generation_and_claim_token():
    now = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
    current = {
        **_v2_conversation(state='finalizing', status='completed'),
        'capture_finalization_claim_token': 'claim-b',
        'capture_finalization_lease_expires_at': now + timedelta(seconds=10),
    }
    for generation, owner, claim in (
        ('generation-stale', 'owner-a', 'claim-b'),
        ('generation-a', 'owner-stale', 'claim-b'),
        ('generation-a', 'owner-a', 'claim-stale'),
    ):
        transaction = _FakeTransaction()
        assert not conversations_router.conversations_db._renew_capture_finalization_transaction(
            transaction,
            _FakeDocumentRef(current),
            claim,
            now,
            15,
            generation,
            owner,
        )
        assert conversations_router.conversations_db._claim_capture_finalization_effect_transaction(
            transaction,
            _FakeDocumentRef(current),
            generation,
            owner,
            claim,
            'integrations:external',
            now,
        ) == {'outcome': 'lost'}
        assert not conversations_router.conversations_db._upsert_conversation_if_capture_finalizer_transaction(
            transaction,
            _FakeDocumentRef(current),
            'authenticated-user',
            current,
            generation,
            owner,
            claim,
            now,
        )
        completed_receipt = {
            **current,
            'capture_finalization_effects': {'integrations:external': {'state': 'completed'}},
        }
        assert not conversations_router.conversations_db._complete_capture_finalization_transaction(
            transaction,
            _FakeDocumentRef(_v2_authority(state='finalizing')),
            _FakeDocumentRef(completed_receipt),
            'capture-a',
            generation,
            owner,
            claim,
            now,
        )
        assert transaction.updates == []


def test_capture_effect_reclaim_reuses_stable_idempotency_token():
    now = datetime(2026, 8, 15, 4, 30, tzinfo=timezone.utc)
    current = {
        **_v2_conversation(state='finalizing', status='completed'),
        'capture_finalization_claim_token': 'claim-b',
        'capture_finalization_lease_expires_at': now + timedelta(seconds=10),
        'capture_finalization_effects': {
            'integrations:external': {
                'state': 'claimed',
                'claim_token': 'claim-a',
                'operation_token': 'stable-idempotency-token',
                'attempt_count': 1,
            }
        },
    }
    transaction = _FakeTransaction()
    reclaimed = conversations_router.conversations_db._claim_capture_finalization_effect_transaction(
        transaction,
        _FakeDocumentRef(current),
        'generation-a',
        'owner-a',
        'claim-b',
        'integrations:external',
        now,
    )

    assert reclaimed == {'outcome': 'claimed', 'operation_token': 'stable-idempotency-token'}
    receipt = transaction.updates[0][1]['capture_finalization_effects']['integrations:external']
    assert receipt['claim_token'] == 'claim-b'
    assert receipt['attempt_count'] == 2


def test_capture_effect_successful_claim_then_expiry_reclaim_deduplicates_resumed_predecessor(monkeypatch):
    state = {
        'live_claim': 'claim-a',
        'completed': False,
        'operation_token': 'stable-effect-operation',
    }
    state_lock = threading.Lock()
    predecessor_entered_operation = threading.Event()
    successor_completed = threading.Event()
    deliveries = set()
    delivery_attempts = []
    predecessor_failures = []

    def claim_effect(uid, conversation_id, generation, owner_token, claim_token, effect_id):
        assert (uid, conversation_id, generation, owner_token, effect_id) == (
            'authenticated-user',
            'capture-a',
            'generation-a',
            'owner-a',
            'integrations:external',
        )
        with state_lock:
            if state['completed']:
                return {
                    'outcome': 'completed',
                    'operation_token': state['operation_token'],
                    'result': [],
                }
            if claim_token != state['live_claim']:
                return {'outcome': 'lost'}
            return {'outcome': 'claimed', 'operation_token': state['operation_token']}

    def complete_effect(
        uid,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        effect_id,
        operation_token,
        result,
    ):
        with state_lock:
            if claim_token != state['live_claim'] or operation_token != state['operation_token']:
                return False
            state['completed'] = True
            return True

    def idempotent_delivery(operation_token):
        with state_lock:
            delivery_attempts.append(operation_token)
            deliveries.add(operation_token)
        return []

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        'claim_capture_finalization_effect',
        claim_effect,
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        'complete_capture_finalization_effect',
        complete_effect,
    )

    predecessor = conversation_processor.CaptureFinalizationEffectRunner(
        'authenticated-user',
        'capture-a',
        ('generation-a', 'owner-a', 'claim-a'),
    )
    successor = conversation_processor.CaptureFinalizationEffectRunner(
        'authenticated-user',
        'capture-a',
        ('generation-a', 'owner-a', 'claim-b'),
    )

    def paused_predecessor_delivery(operation_token):
        predecessor_entered_operation.set()
        assert successor_completed.wait(timeout=2)
        return idempotent_delivery(operation_token)

    def run_predecessor():
        try:
            predecessor.run('integrations:external', paused_predecessor_delivery)
        except Exception as error:
            predecessor_failures.append(error)

    predecessor_thread = threading.Thread(target=run_predecessor)
    predecessor_thread.start()
    assert predecessor_entered_operation.wait(timeout=2)

    with state_lock:
        state['live_claim'] = 'claim-b'
    assert successor.run('integrations:external', idempotent_delivery) == []
    successor_completed.set()
    predecessor_thread.join(timeout=2)

    assert not predecessor_thread.is_alive()
    assert len(predecessor_failures) == 1
    assert isinstance(predecessor_failures[0], conversation_processor.CaptureFinalizationLeaseLost)
    assert delivery_attempts == ['stable-effect-operation', 'stable-effect-operation']
    assert deliveries == {'stable-effect-operation'}


@pytest.mark.parametrize('sink_name', ['action_items', 'usage', 'audio'])
def test_capture_internal_sink_a_pause_b_reclaim_a_resume_is_idempotent(monkeypatch, sink_name):
    state = {
        'live_claim': 'claim-a',
        'completed': False,
        'operation_token': f'stable-{sink_name}-operation',
    }
    state_lock = threading.Lock()
    predecessor_paused = threading.Event()
    successor_completed = threading.Event()
    attempts = []
    logical_writes = set()
    usage_total = 0

    def claim_effect(uid, conversation_id, generation, owner_token, claim_token, effect_id):
        with state_lock:
            if state['completed']:
                return {'outcome': 'completed', 'operation_token': state['operation_token'], 'result': None}
            if claim_token != state['live_claim']:
                return {'outcome': 'lost'}
            return {'outcome': 'claimed', 'operation_token': state['operation_token']}

    def complete_effect(*args):
        claim_token = args[4]
        operation_token = args[6]
        with state_lock:
            if claim_token != state['live_claim'] or operation_token != state['operation_token']:
                return False
            state['completed'] = True
            return True

    def internal_sink(operation_token):
        nonlocal usage_total
        with state_lock:
            attempts.append(operation_token)
            if sink_name == 'action_items':
                logical_writes.update(
                    conversation_processor.action_items_db.capture_action_item_ids(operation_token, 2)
                )
            elif sink_name == 'usage':
                receipt_id = user_usage_db.usage_idempotency_receipt_id(operation_token)
                if receipt_id not in logical_writes:
                    logical_writes.add(receipt_id)
                    usage_total += 1
            else:
                logical_writes.update(
                    conversation_processor.conversations_db.capture_audio_file_id(operation_token, index)
                    for index in range(2)
                )

    monkeypatch.setattr(conversation_processor.conversations_db, 'claim_capture_finalization_effect', claim_effect)
    monkeypatch.setattr(
        conversation_processor.conversations_db, 'complete_capture_finalization_effect', complete_effect
    )

    predecessor = conversation_processor.CaptureFinalizationEffectRunner(
        'authenticated-user', 'capture-a', ('generation-a', 'owner-a', 'claim-a')
    )
    successor = conversation_processor.CaptureFinalizationEffectRunner(
        'authenticated-user', 'capture-a', ('generation-a', 'owner-a', 'claim-b')
    )

    def paused_sink(operation_token):
        predecessor_paused.set()
        assert successor_completed.wait(timeout=2)
        return internal_sink(operation_token)

    failures = []
    predecessor_thread = threading.Thread(
        target=lambda: _capture_failure(failures, predecessor.run, f'internal:{sink_name}', paused_sink)
    )
    predecessor_thread.start()
    assert predecessor_paused.wait(timeout=2)

    with state_lock:
        state['live_claim'] = 'claim-b'
    successor.run(f'internal:{sink_name}', internal_sink)
    successor_completed.set()
    predecessor_thread.join(timeout=2)

    assert not predecessor_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], conversation_processor.CaptureFinalizationLeaseLost)
    assert attempts == [state['operation_token'], state['operation_token']]
    assert len(logical_writes) == (1 if sink_name == 'usage' else 2)
    assert usage_total == (1 if sink_name == 'usage' else 0)


def _capture_failure(failures, operation, *args):
    try:
        operation(*args)
    except Exception as error:
        failures.append(error)


def test_capture_usage_sink_receipt_and_increment_commit_atomically_once(monkeypatch):
    from utils import analytics

    class UsageRef:
        def __init__(self, path):
            self.path = path

        def collection(self, name):
            return UsageRef(f'{self.path}/{name}')

        def document(self, name):
            return UsageRef(f'{self.path}/{name}')

    class UsageDB:
        def collection(self, name):
            return UsageRef(name)

        def transaction(self):
            return object()

    timestamps = [
        datetime(2026, 8, 15, 11, 59, 59),
        datetime(2026, 8, 15, 12, 0, 1),
    ]
    timestamp_lock = threading.Lock()

    class AttemptDateTime:
        @classmethod
        def utcnow(cls):
            with timestamp_lock:
                return timestamps.pop(0)

    predecessor_entered = threading.Event()
    successor_completed = threading.Event()
    apply_lock = threading.Lock()
    receipts = set()
    increments = []
    observed = []
    apply_calls = 0

    def apply_once(transaction, hourly_ref, receipt_ref, update_doc, receipt_doc):
        nonlocal apply_calls
        with apply_lock:
            apply_calls += 1
            is_predecessor = apply_calls == 1
            observed.append((hourly_ref.path, receipt_ref.path, receipt_doc['hourly_usage_id']))
        if is_predecessor:
            predecessor_entered.set()
            assert successor_completed.wait(timeout=2)
        with apply_lock:
            if receipt_ref.path in receipts:
                return False
            receipts.add(receipt_ref.path)
            increments.append(hourly_ref.path)
            return True

    monkeypatch.setattr(analytics, 'datetime', AttemptDateTime)
    monkeypatch.setattr(user_usage_db, 'db', UsageDB())
    monkeypatch.setattr(user_usage_db, '_apply_hourly_usage_once', apply_once)

    predecessor_results = []
    predecessor = threading.Thread(
        target=lambda: predecessor_results.append(
            analytics.record_usage(
                'authenticated-user',
                insights_gained=1,
                idempotency_key='stable-usage-operation',
            )
        )
    )
    predecessor.start()
    assert predecessor_entered.wait(timeout=2)

    successor_result = analytics.record_usage(
        'authenticated-user',
        insights_gained=1,
        idempotency_key='stable-usage-operation',
    )
    successor_completed.set()
    predecessor.join(timeout=2)

    assert not predecessor.is_alive()
    assert predecessor_results == [False]
    assert successor_result is True
    assert observed[0][0] != observed[1][0]
    assert observed[0][1] == observed[1][1]
    assert observed[0][2] != observed[1][2]
    assert increments == ['users/authenticated-user/hourly_usage/2026-08-15-12']


def test_capture_action_items_auto_sync_threads_stable_effect_operation_to_real_batch_caller(monkeypatch):
    conversation = _long_conversation()
    conversation.id = 'capture-a'
    conversation.structured = Structured(action_items=[{'description': 'Create a task'}])
    received = []

    class EffectRunner:
        def run(self, effect_id, operation, **kwargs):
            operation_tokens = {
                'action_items:create': 'stable-action-item-create',
                'action_items:delete_existing': 'stable-action-item-delete',
                'action_items:auto_sync': 'stable-action-items-auto-sync',
            }
            return operation(operation_tokens[effect_id])

    async def capture_batch(uid, action_items, idempotency_key=None):
        received.append((uid, action_items, idempotency_key))
        return []

    monkeypatch.setattr(
        conversation_processor.action_items_db,
        'create_action_items_batch',
        lambda *args, **kwargs: ['action-item-a'],
    )
    monkeypatch.setattr(
        conversation_processor.action_items_db,
        'delete_action_items_for_conversation',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(conversation_processor, 'auto_sync_action_items_batch', capture_batch)

    conversation_processor._save_action_items(
        'authenticated-user',
        conversation,
        side_effect_guard=lambda boundary: None,
        effect_runner=EffectRunner(),
    )

    assert len(received) == 1
    assert received[0][0] == 'authenticated-user'
    assert received[0][1][0]['id'] == 'action-item-a'
    assert received[0][2] == 'stable-action-items-auto-sync'


def test_capture_completed_result_is_fetchable_before_webhooks_and_lost_ack_recovery_is_idempotent(monkeypatch):
    conversation = _long_conversation()
    conversation.id = 'capture-a'
    durable = {
        **conversation.dict(),
        'capture_protocol_version': 2,
        'capture_generation': 'generation-a',
        'capture_owner_token': 'owner-a',
        'capture_state': 'finalizing',
        'capture_finalization_claim_token': 'claim-a',
    }
    state = {'durable': durable, 'live_claim': 'claim-a', 'effects': {}}
    attempts = {'created': [], 'postprocess': []}
    deliveries = {'created': set(), 'postprocess': set()}

    monkeypatch.setattr(
        conversation_processor,
        '_get_structured',
        lambda *args, **kwargs: (Structured(title='Durable result'), True),
    )
    monkeypatch.setattr(conversation_processor, 'update_personas_async', lambda uid: None)

    def claim_effect(uid, conversation_id, generation, owner_token, claim_token, effect_id):
        assert claim_token == state['live_claim']
        receipt = state['effects'].get(effect_id, {})
        if receipt.get('state') == 'completed':
            return {'outcome': 'completed', **receipt}
        operation_token = receipt.get(
            'operation_token'
        ) or conversation_processor.conversations_db.capture_finalization_effect_operation_token(
            conversation_id, generation, owner_token, effect_id
        )
        state['effects'][effect_id] = {
            **receipt,
            'state': 'claimed',
            'claim_token': claim_token,
            'operation_token': operation_token,
        }
        return {'outcome': 'claimed', 'operation_token': operation_token}

    def complete_effect(
        uid,
        conversation_id,
        generation,
        owner_token,
        claim_token,
        effect_id,
        operation_token,
        result,
    ):
        receipt = state['effects'][effect_id]
        if claim_token != state['live_claim'] or receipt['claim_token'] != claim_token:
            return False
        if effect_id == 'webhook:postprocess' and claim_token == 'claim-a':
            return False
        receipt.update({'state': 'completed', 'result': result})
        return True

    def persist_result(uid, payload, generation, owner_token, claim_token):
        assert claim_token == state['live_claim']
        capture_fields = {key: value for key, value in state['durable'].items() if key.startswith('capture_')}
        state['durable'] = {**payload, **capture_fields, 'capture_finalization_claim_token': claim_token}
        return True

    def receive(kind, operation_token, webhook_conversation):
        fetched = dict(state['durable'])
        assert fetched['status'] == ConversationStatus.completed.value
        assert fetched['capture_state'] == 'finalizing'
        assert fetched['structured']['title'] == 'Durable result'
        assert webhook_conversation.status == ConversationStatus.completed
        attempts[kind].append(operation_token)
        deliveries[kind].add(operation_token)

    monkeypatch.setattr(conversation_processor.conversations_db, 'claim_capture_finalization_effect', claim_effect)
    monkeypatch.setattr(
        conversation_processor.conversations_db, 'complete_capture_finalization_effect', complete_effect
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db, 'upsert_conversation_if_capture_finalizer', persist_result
    )
    monkeypatch.setattr(
        conversation_processor,
        'conversation_created_webhook',
        lambda uid, current, idempotency_key=None: receive('created', idempotency_key, current),
    )
    monkeypatch.setattr(
        conversation_processor,
        'fire_postprocess_webhook',
        lambda uid, current, idempotency_key=None, synchronous=False: receive('postprocess', idempotency_key, current),
    )

    with pytest.raises(conversation_processor.CaptureFinalizationLeaseLost):
        conversation_processor.process_conversation(
            'authenticated-user',
            'en',
            conversation,
            force_process=True,
            capture_finalization=('generation-a', 'owner-a', 'claim-a'),
        )
    assert not conversation_processor.mark_unexpected_conversation_processing_failed(
        'authenticated-user',
        Conversation(**state['durable']),
        capture_finalization=('generation-a', 'owner-a', 'claim-a'),
    )

    state['live_claim'] = 'claim-b'
    state['durable']['capture_finalization_claim_token'] = 'claim-b'
    recovered = conversation_processor.process_conversation(
        'authenticated-user',
        'en',
        Conversation(**state['durable']),
        force_process=True,
        capture_finalization=('generation-a', 'owner-a', 'claim-b'),
    )

    assert recovered.status == ConversationStatus.completed
    assert len(deliveries['created']) == 1
    assert len(deliveries['postprocess']) == 1
    assert len(attempts['created']) == 1
    assert attempts['postprocess'] == [attempts['postprocess'][0], attempts['postprocess'][0]]


def test_capture_raw_head_ci_inventory_matches_intentional_sources_and_required_nodes():
    repo_root = Path(__file__).resolve().parents[3]
    workflow = (repo_root / '.github/workflows/ella-ios-source-ci.yml').read_text()
    inventory_match = re.search(r'capture_tests=\(\n(?P<body>.*?)\n\s*\)', workflow, re.DOTALL)
    assert inventory_match is not None
    inventory = re.findall(r"'([^']+test_conversation_processing_failures\.py::[^']+)'", inventory_match['body'])

    assert len(inventory) == 41
    assert len(set(inventory)) == 41
    required_nodes = {
        'test_capture_effect_claim_after_successful_guard_rejects_expired_predecessor',
        'test_capture_finalization_resumes_after_result_persistence_at_every_terminal_boundary',
        'test_capture_finalization_effect_paths_reject_wrong_owner_generation_and_claim_token',
        'test_capture_effect_reclaim_reuses_stable_idempotency_token',
        'test_capture_effect_successful_claim_then_expiry_reclaim_deduplicates_resumed_predecessor',
        'test_capture_internal_sink_a_pause_b_reclaim_a_resume_is_idempotent[action_items]',
        'test_capture_internal_sink_a_pause_b_reclaim_a_resume_is_idempotent[usage]',
        'test_capture_internal_sink_a_pause_b_reclaim_a_resume_is_idempotent[audio]',
        'test_capture_usage_sink_receipt_and_increment_commit_atomically_once',
        'test_capture_completed_result_is_fetchable_before_webhooks_and_lost_ack_recovery_is_idempotent',
        'test_capture_finalization_route_reclaims_durable_result_and_finishes_missing_effects[completed]',
        'test_capture_finalization_route_reclaims_durable_result_and_finishes_missing_effects[failed]',
        'test_capture_drain_barrier_waits_for_copied_final_provider_tail_before_finalization_claim',
        'test_capture_raw_head_ci_inventory_matches_intentional_sources_and_required_nodes',
    }
    assert required_nodes <= {node.rsplit('::', 1)[1] for node in inventory}
    assert (
        "ref: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}"
        in workflow
    )

    intentional_paths = {
        'backend/database/chat.py',
        'backend/database/conversations.py',
        'backend/database/action_items.py',
        'backend/database/folders.py',
        'backend/database/task_sync.py',
        'backend/database/user_usage.py',
        'backend/routers/conversations.py',
        'backend/routers/pusher.py',
        'backend/routers/task_integrations.py',
        'backend/routers/transcribe.py',
        'backend/utils/app_integrations.py',
        'backend/utils/analytics.py',
        'backend/utils/conversations/process_conversation.py',
        'backend/utils/ella/postprocess.py',
        'backend/utils/llm/knowledge_graph.py',
        'backend/utils/notifications.py',
        'backend/utils/stt/streaming.py',
        'backend/utils/task_sync.py',
        'backend/utils/webhooks.py',
        'backend/tests/unit/test_capture_task_sync_idempotency.py',
        'backend/tests/unit/test_conversation_processing_failures.py',
    }
    trigger_block = workflow.split('workflow_dispatch:', 1)[0]
    diff_allowlist = workflow.split('case "$changed_path" in', 1)[1].split('*)', 1)[0]
    for path in intentional_paths:
        assert f'- {path}' in trigger_block
        assert path in diff_allowlist


@pytest.mark.parametrize('status', [ConversationStatus.completed, ConversationStatus.failed])
def test_capture_finalization_route_reclaims_durable_result_and_finishes_missing_effects(monkeypatch, status):
    app, client, state = _mount_capture_finalization_route(monkeypatch, protocol_v2=True)
    state['conversations']['capture-a']['status'] = status.value
    state['conversations']['capture-a']['capture_state'] = 'finalizing'
    request = {
        'conversation_id': 'capture-a',
        'protocol_version': 2,
        'generation': 'generation-a',
        'owner_token': 'owner-a',
    }
    try:
        response = client.post('/v1/conversations', json=request)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['conversation']['status'] == status.value
    assert state['processes'] == (1 if status == ConversationStatus.completed else 0)
    assert state['integrations'] == (1 if status == ConversationStatus.completed else 0)
    assert state['conversations']['capture-a']['capture_state'] == 'terminal'


def test_capture_reclaimed_finalizer_fences_all_late_side_effect_classes(monkeypatch):
    conversation = _long_conversation()
    conversation.id = 'capture-a'
    conversation.folder_id = 'existing-folder'
    entered_guard = threading.Event()
    successor_completed = threading.Event()
    failures = []
    side_effects = {
        name: 0 for name in ('usage', 'apps', 'vectors', 'memories', 'trends', 'actions', 'goals', 'audio', 'jobs')
    }

    monkeypatch.setattr(
        conversation_processor,
        '_get_structured',
        lambda *args, **kwargs: (
            Structured(title='Captured title', overview='one two three four five six seven.'),
            False,
        ),
    )

    def stale_a_effect_claim(*args, **kwargs):
        entered_guard.set()
        assert successor_completed.wait(timeout=2)
        return {'outcome': 'lost'}

    monkeypatch.setattr(
        conversation_processor.conversations_db,
        'claim_capture_finalization_effect',
        stale_a_effect_claim,
    )
    monkeypatch.setattr(
        conversation_processor,
        'record_usage',
        lambda *args, **kwargs: side_effects.__setitem__('usage', side_effects['usage'] + 1),
    )
    monkeypatch.setattr(
        conversation_processor.conversations_db,
        'upsert_conversation_if_capture_finalizer',
        lambda *args, **kwargs: side_effects.__setitem__('jobs', side_effects['jobs'] + 1),
    )

    def run_stale_a():
        try:
            conversation_processor.process_conversation(
                'authenticated-user',
                'en',
                conversation,
                force_process=True,
                capture_finalization=('generation-a', 'owner-a', 'claim-a'),
            )
        except Exception as error:
            failures.append(error)

    stale_a = threading.Thread(target=run_stale_a)
    stale_a.start()
    assert entered_guard.wait(timeout=2)

    # B reclaims and completes every effect class while A is paused at its
    # first immediate lease boundary.
    for name in side_effects:
        side_effects[name] += 1
    successor_completed.set()
    stale_a.join(timeout=2)

    assert not stale_a.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], conversation_processor.CaptureFinalizationLeaseLost)
    assert side_effects == {name: 1 for name in side_effects}
