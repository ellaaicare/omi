import os
import sys
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("ENCRYPTION_SECRET", "test-encryption-secret-32-bytes-long")

if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")

    class _RedisStub:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    redis_stub.Redis = _RedisStub
    sys.modules["redis"] = redis_stub

if "stripe" not in sys.modules:
    stripe_stub = types.ModuleType("stripe")
    stripe_stub.api_key = None
    sys.modules["stripe"] = stripe_stub

storage_stub = types.ModuleType("utils.other.storage")
storage_stub.list_audio_chunks = lambda *args, **kwargs: []
sys.modules.setdefault("utils.other.storage", storage_stub)

from database import conversations as conversations_db
from models.conversation import ConversationStatus


class _FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeDocumentRef:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return _FakeSnapshot(self.data)


class _FakeTransaction:
    def __init__(self):
        self.updates = []
        self.sets = []

    def update(self, document_ref, update_data):
        self.updates.append((document_ref, update_data))

    def set(self, document_ref, data):
        self.sets.append((document_ref, data))


def _failed_conversation(request_id=None, error='conversation_summary_failed'):
    return {
        'id': 'conversation-1',
        'status': ConversationStatus.failed.value,
        'discarded': False,
        'processing_error': error,
        'processing_error_at': datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        'processing_retry_id': request_id,
        'transcript_segments': [{'text': 'important transcript'}],
    }


def _claim(data, request_id='request-123', retry_data=None):
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(data)
    retry_ref = _FakeDocumentRef(retry_data)
    requested_at = datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc)
    result = conversations_db._claim_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        request_id,
        requested_at,
    )
    return result, transaction


def test_failed_summary_is_atomically_claimed_without_returning_protected_transcript():
    result, transaction = _claim(_failed_conversation())

    assert result['outcome'] == 'claimed'
    assert 'conversation' not in result
    assert transaction.updates[0][1] == {
        'status': ConversationStatus.processing.value,
        'processing_retry_id': 'request-123',
        'processing_retry_mode': 'full',
        'processing_retry_started_at': datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc),
        'processing_retry_lease_expires_at': datetime(2026, 7, 20, 8, 20, tzinfo=timezone.utc),
        'processing_retry_completed_at': None,
        'processing_retry_attempt_count': 1,
    }
    assert transaction.sets[0][1]['outcome'] == ConversationStatus.processing.value


def test_repeated_failed_request_id_returns_terminal_receipt_without_requeue():
    result, transaction = _claim(
        _failed_conversation(request_id='newer-request'),
        retry_data={
            'request_id': 'request-123',
            'outcome': ConversationStatus.failed.value,
            'mode': 'full',
            'phase': 'enrichment_failed',
            'attempt_count': 1,
        },
    )

    assert result['outcome'] == 'failed'
    assert result['phase'] == 'enrichment_failed'
    assert result['attempt_count'] == 1
    assert transaction.updates == []
    assert transaction.sets == []


def test_new_request_id_can_retry_after_terminal_failure():
    conversation = _failed_conversation(request_id='request-123')
    conversation['processing_retry_completed_at'] = datetime(2026, 7, 20, 8, 4, tzinfo=timezone.utc)

    result, transaction = _claim(conversation, request_id='request-456')

    assert result['outcome'] == 'claimed'
    assert result['mode'] == 'full'
    assert result['attempt_count'] == 1
    assert transaction.updates[0][1]['processing_retry_id'] == 'request-456'
    assert transaction.sets[0][1]['request_id'] == 'request-456'


def test_concurrent_request_observes_processing_without_reclaiming():
    conversation = _failed_conversation(request_id='request-first')
    conversation['status'] = ConversationStatus.processing.value
    conversation['processing_retry_lease_expires_at'] = datetime(2026, 7, 20, 8, 10, tzinfo=timezone.utc)

    result, transaction = _claim(conversation, request_id='request-second')

    assert result['outcome'] == 'busy'
    assert 'conversation' not in result
    assert transaction.updates == []
    assert transaction.sets == []


def test_expired_same_request_lease_is_reclaimed_and_requeued():
    requested_at = datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc)
    conversation = _failed_conversation(request_id='request-123')
    conversation.update(
        {
            'status': ConversationStatus.processing.value,
            'processing_retry_lease_expires_at': requested_at - timedelta(seconds=1),
        }
    )
    retry_data = {
        'request_id': 'request-123',
        'outcome': ConversationStatus.processing.value,
        'mode': 'full',
        'phase': 'claimed',
        'attempt_count': 1,
        'lease_expires_at': requested_at - timedelta(seconds=1),
    }

    result, transaction = _claim(conversation, retry_data=retry_data)

    assert result['outcome'] == 'claimed'
    assert result['reason'] == 'lease_reclaimed'
    assert result['attempt_count'] == 2
    assert transaction.updates[1][1]['outcome'] == 'processing'
    assert transaction.updates[1][1]['lease_expires_at'] > requested_at


def test_active_same_request_lease_returns_processing_without_duplicate_work():
    requested_at = datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc)
    retry_data = {
        'request_id': 'request-123',
        'outcome': ConversationStatus.processing.value,
        'mode': 'full',
        'phase': 'claimed',
        'attempt_count': 1,
        'lease_expires_at': requested_at + timedelta(minutes=5),
    }

    result, transaction = _claim(_failed_conversation(request_id='request-123'), retry_data=retry_data)

    assert result['outcome'] == 'processing'
    assert result['attempt_count'] == 1
    assert transaction.updates == []
    assert transaction.sets == []


def test_old_failed_receipt_cannot_supersede_newer_active_lease():
    conversation = _failed_conversation(request_id='newer-request')
    conversation.update(
        {
            'status': ConversationStatus.processing.value,
            'processing_retry_lease_expires_at': datetime(2026, 7, 20, 8, 10, tzinfo=timezone.utc),
        }
    )
    retry_data = {
        'request_id': 'request-123',
        'outcome': ConversationStatus.failed.value,
        'mode': 'full',
        'attempt_count': 1,
    }

    result, transaction = _claim(conversation, retry_data=retry_data)

    assert result['outcome'] == 'busy'
    assert transaction.updates == []


def test_completed_enriched_conversation_is_idempotent_for_any_retry_id():
    conversation = _failed_conversation()
    conversation.update(
        {
            'status': ConversationStatus.completed.value,
            'structured': {'title': 'Enriched', 'overview': '[Ella] Complete.'},
            'active_summary_version_id': 'enriched-v2',
            'summary_versions': [
                {'id': 'enriched-v2', 'kind': 'observer_enriched', 'source': 'observer', 'is_active': True}
            ],
            'enrichment_state': {'status': 'writeback_applied', 'kind': 'observer_enriched'},
        }
    )

    result, transaction = _claim(conversation, request_id='request-second')

    assert result['outcome'] == 'completed'
    assert transaction.updates == []
    assert transaction.sets[0][1]['outcome'] == ConversationStatus.completed.value


def test_enriched_conversation_with_unconfirmed_vector_remains_stage_two_retryable():
    conversation = _failed_conversation()
    conversation.update(
        {
            'status': ConversationStatus.completed.value,
            'structured': {'title': 'Enriched', 'overview': '[Ella] Complete.'},
            'active_summary_version_id': 'enriched-v2',
            'summary_versions': [
                {'id': 'enriched-v2', 'kind': 'recovered_enriched', 'source': 'observer', 'is_active': True}
            ],
            'enrichment_state': {'status': 'writeback_applied', 'kind': 'recovered_enriched'},
            'processing_retry_enrichment_vector_status': 'failed',
        }
    )

    result, transaction = _claim(conversation)

    assert result['outcome'] == 'claimed'
    assert result['mode'] == 'enrichment_only'
    assert result['reason'] == 'enriched_summary_without_confirmed_vector'
    assert 'status' not in transaction.updates[0][1]


def test_completed_generic_conversation_claims_enrichment_only_without_changing_visibility():
    conversation = _failed_conversation()
    conversation.update(
        {
            'status': ConversationStatus.completed.value,
            'processing_error': None,
            'structured': {'title': 'Generic', 'overview': '[Ella] Generic summary.'},
            'active_summary_version_id': 'generic-v1',
            'summary_versions': [{'id': 'generic-v1', 'kind': 'generic_recovered', 'source': 'omi', 'is_active': True}],
        }
    )

    result, transaction = _claim(conversation)

    assert result['outcome'] == 'claimed'
    assert result['mode'] == 'enrichment_only'
    assert 'conversation' not in result
    assert transaction.updates[0][1] == {
        'processing_retry_id': 'request-123',
        'processing_retry_mode': 'enrichment_only',
        'processing_retry_started_at': datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc),
        'processing_retry_lease_expires_at': datetime(2026, 7, 20, 8, 20, tzinfo=timezone.utc),
        'processing_retry_completed_at': None,
        'processing_retry_attempt_count': 1,
    }
    assert transaction.sets[0][1]['generic_status'] == 'completed'
    assert transaction.sets[0][1]['generic_vector_status'] == 'unknown'
    assert transaction.sets[0][1]['enrichment_status'] == 'pending'


def test_generic_processing_failure_is_visible_but_not_automatically_retried():
    result, transaction = _claim(_failed_conversation(error='conversation_processing_failed'))

    assert result['outcome'] == 'not_retryable'
    assert transaction.updates == []
    assert transaction.sets == []


def test_missing_conversation_is_not_claimed():
    result, transaction = _claim(None)

    assert result['outcome'] == 'not_found'
    assert 'conversation' not in result
    assert transaction.updates == []
    assert transaction.sets == []


def test_decrypted_source_hash_is_receipted_without_transcript_content():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            'processing_retry_id': 'request-123',
            'transcript_segments': 'encrypted-payload-must-not-be-copied',
        }
    )
    retry_ref = _FakeDocumentRef({'request_id': 'request-123', 'outcome': 'processing'})
    updated_at = datetime(2026, 7, 20, 8, 6, tzinfo=timezone.utc)
    lease_expires_at = updated_at + timedelta(minutes=15)

    result = conversations_db._record_conversation_processing_retry_source_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        'request-123',
        'a' * 64,
        99,
        updated_at,
        lease_expires_at,
        generic_summary_sha256='b' * 64,
    )

    assert result is True
    assert transaction.updates[0][1] == {
        'processing_retry_lease_expires_at': lease_expires_at,
        'processing_retry_transcript_sha256': 'a' * 64,
        'processing_retry_source_request_id': 'request-123',
        'processing_retry_generic_summary_sha256': 'b' * 64,
    }
    assert transaction.updates[1][1] == {
        'transcript_sha256': 'a' * 64,
        'transcript_segment_count': 99,
        'lease_expires_at': lease_expires_at,
        'updated_at': updated_at,
        'generic_summary_sha256': 'b' * 64,
    }
    assert 'transcript_segments' not in transaction.updates[1][1]


def test_legacy_generic_summary_bootstraps_version_history_before_enrichment():
    legacy_structured = {
        'title': 'Legacy generic summary',
        'overview': '[Ella] The preserved generic summary.',
        'emoji': 'brain',
        'category': 'other',
    }
    enriched_structured = {
        'title': 'Enriched summary',
        'overview': '[Ella] The full-context enriched summary.',
        'emoji': 'brain',
        'category': 'personal',
    }

    update = conversations_db.build_summary_version_update(
        {
            'created_at': datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            'structured': legacy_structured,
            'summary_versions': [],
            'active_summary_version_id': None,
        },
        next_structured=enriched_structured,
        source='observer',
        kind='recovered_enriched',
        based_on_version_id=None,
    )

    assert len(update['summary_versions']) == 2
    legacy_version, enriched_version = update['summary_versions']
    assert legacy_version['kind'] == 'legacy_current'
    assert legacy_version['title'] == legacy_structured['title']
    assert legacy_version['overview'] == legacy_structured['overview']
    assert legacy_version['is_active'] is False
    assert enriched_version['kind'] == 'recovered_enriched'
    assert enriched_version['title'] == enriched_structured['title']
    assert enriched_version['based_on_version_id'] == legacy_version['id']
    assert enriched_version['is_active'] is True
    assert update['active_summary_version_id'] == enriched_version['id']


def test_generic_completion_keeps_overall_receipt_processing_for_hermes():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "processing",
            "processing_retry_id": "request-123",
        }
    )
    retry_ref = _FakeDocumentRef({"request_id": "request-123", "outcome": "processing"})
    applied_at = datetime(2026, 7, 20, 8, 6, tzinfo=timezone.utc)

    result = conversations_db._record_conversation_processing_retry_summary_applied_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        "request-123",
        "recovered-v1",
        applied_at,
    )

    assert result is True
    assert transaction.updates[0][1] == {
        "status": "completed",
        "discarded": False,
        "processing_error": None,
        "processing_error_at": None,
        "processing_retry_summary_version_id": "recovered-v1",
        "processing_retry_generic_vector_status": "pending",
    }
    assert transaction.updates[1][1] == {
        "outcome": "processing",
        "phase": "generic_writeback_applied",
        "generic_status": "writeback_applied",
        "generic_vector_status": "pending",
        "enrichment_status": "pending",
        "summary_version_id": "recovered-v1",
        "updated_at": applied_at,
    }


def test_completed_retry_atomically_clears_failure_and_discarded_state():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "processing",
            "discarded": True,
            "processing_error": "conversation_summary_failed",
            "processing_retry_id": "request-123",
        }
    )
    retry_ref = _FakeDocumentRef({"request_id": "request-123", "outcome": "processing"})
    finished_at = datetime(2026, 7, 20, 8, 7, tzinfo=timezone.utc)

    result = conversations_db._finish_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        "request-123",
        "completed",
        finished_at,
        None,
    )

    assert result is True
    assert transaction.updates[0][1] == {
        "outcome": "completed",
        "phase": "completed",
        "updated_at": finished_at,
    }
    assert transaction.updates[1][1] == {
        "status": "completed",
        "discarded": False,
        "processing_error": None,
        "processing_error_at": None,
        "processing_retry_completed_at": finished_at,
    }


def test_failed_vector_completion_stays_retryable_with_stable_error_code():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "processing",
            "processing_retry_id": "request-123",
            "processing_retry_summary_version_id": "recovered-v1",
        }
    )
    retry_ref = _FakeDocumentRef({"request_id": "request-123", "outcome": "processing"})
    finished_at = datetime(2026, 7, 20, 8, 7, tzinfo=timezone.utc)

    result = conversations_db._finish_conversation_processing_retry_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        "request-123",
        "failed",
        finished_at,
        "conversation_summary_recovery_failed",
    )

    assert result is True
    assert transaction.updates[0][1]["error_code"] == "conversation_summary_recovery_failed"
    assert transaction.updates[0][1]["phase"] == "failed"
    assert transaction.updates[1][1] == {
        "status": "failed",
        "discarded": False,
        "processing_error": "conversation_summary_recovery_failed",
        "processing_error_at": finished_at,
    }


def test_hermes_failure_keeps_completed_generic_summary_and_marks_enrichment_retryable():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "completed",
            "processing_retry_id": "request-123",
            "processing_retry_summary_version_id": "generic-v1",
            "active_summary_version_id": "enriched-v2",
        }
    )
    retry_ref = _FakeDocumentRef({"request_id": "request-123", "outcome": "completed"})
    failed_at = datetime(2026, 7, 20, 8, 8, tzinfo=timezone.utc)

    result = conversations_db._record_conversation_processing_retry_enrichment_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        "conversation-1",
        "request-123",
        "failed",
        failed_at,
        None,
    )

    assert result is True
    assert transaction.updates[0][1] == {
        "enrichment_status": "failed",
        "outcome": "failed",
        "phase": "enrichment_failed",
        "updated_at": failed_at,
    }
    assert transaction.updates[1][1] == {
        "status": "completed",
        "processing_retry_completed_at": failed_at,
        "enrichment_state": {
            "status": "failed",
            "pending": True,
            "source": "observer",
            "kind": "recovered_enriched",
            "trace_id": "summary-retry:conversation-1:request-123:hermes",
            "updated_at": failed_at,
            "error": "hermes_temporarily_unavailable",
        },
    }


def test_hermes_success_records_enriched_version_without_changing_completed_status():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "completed",
            "processing_retry_id": "request-123",
            "processing_retry_summary_version_id": "generic-v1",
            "active_summary_version_id": "enriched-v2",
        }
    )
    retry_ref = _FakeDocumentRef({"request_id": "request-123", "outcome": "completed"})
    completed_at = datetime(2026, 7, 20, 8, 9, tzinfo=timezone.utc)

    result = conversations_db._record_conversation_processing_retry_enrichment_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        "conversation-1",
        "request-123",
        "completed",
        completed_at,
        "enriched-v2",
        "e" * 64,
    )

    assert result is True
    assert transaction.updates[0][1] == {
        "enrichment_status": "completed",
        "vector_status": "completed",
        "outcome": "completed",
        "phase": "completed",
        "updated_at": completed_at,
        "enriched_summary_version_id": "enriched-v2",
        "vector_summary_version_id": "enriched-v2",
        "vector_content_sha256": "e" * 64,
    }
    assert transaction.updates[1][1] == {
        "status": "completed",
        "processing_retry_enriched_version_id": "enriched-v2",
        "processing_retry_enrichment_vector_status": "completed",
        "processing_retry_enrichment_vector_version_id": "enriched-v2",
        "processing_retry_enrichment_vector_sha256": "e" * 64,
        "processing_retry_completed_at": completed_at,
    }


def test_stale_attempt_cannot_mark_reclaimed_enrichment_complete():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            'status': 'completed',
            'processing_retry_id': 'request-123',
            'processing_retry_attempt_count': 2,
        }
    )
    retry_ref = _FakeDocumentRef({'request_id': 'request-123', 'outcome': 'processing', 'attempt_count': 2})

    result = conversations_db._record_conversation_processing_retry_enrichment_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        'conversation-1',
        'request-123',
        'completed',
        datetime(2026, 7, 20, 8, 9, tzinfo=timezone.utc),
        'enriched-v2',
        attempt_count=1,
    )

    assert result is False
    assert transaction.updates == []


def test_changed_active_summary_cannot_mark_enriched_vector_complete():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            'status': 'completed',
            'processing_retry_id': 'request-123',
            'processing_retry_attempt_count': 2,
            'active_summary_version_id': 'manual-v3',
        }
    )
    retry_ref = _FakeDocumentRef({'request_id': 'request-123', 'outcome': 'processing', 'attempt_count': 2})

    result = conversations_db._record_conversation_processing_retry_enrichment_transaction(
        transaction,
        conversation_ref,
        retry_ref,
        'conversation-1',
        'request-123',
        'completed',
        datetime(2026, 7, 20, 8, 9, tzinfo=timezone.utc),
        'enriched-v2',
        'e' * 64,
        attempt_count=2,
    )

    assert result is False
    assert transaction.updates == []
