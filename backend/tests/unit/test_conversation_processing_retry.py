import os
import sys
import types
from datetime import datetime, timezone

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


def test_failed_summary_is_atomically_claimed_without_transcript_loss():
    result, transaction = _claim(_failed_conversation())

    assert result['outcome'] == 'claimed'
    assert result['conversation']['status'] == ConversationStatus.processing.value
    assert result['conversation']['processing_error'] == 'conversation_summary_failed'
    assert result['conversation']['processing_retry_id'] == 'request-123'
    assert result['conversation']['transcript_segments'] == [{'text': 'important transcript'}]
    assert transaction.updates[0][1] == {
        'status': ConversationStatus.processing.value,
        'processing_retry_id': 'request-123',
        'processing_retry_started_at': datetime(2026, 7, 20, 8, 5, tzinfo=timezone.utc),
        'processing_retry_completed_at': None,
    }
    assert transaction.sets[0][1]['outcome'] == ConversationStatus.processing.value


def test_repeated_failed_request_id_does_not_start_duplicate_work():
    result, transaction = _claim(
        _failed_conversation(request_id='newer-request'),
        retry_data={'request_id': 'request-123', 'outcome': ConversationStatus.failed.value},
    )

    assert result['outcome'] == 'failed'
    assert transaction.updates == []
    assert transaction.sets == []


def test_concurrent_request_observes_processing_without_reclaiming():
    conversation = _failed_conversation(request_id='request-first')
    conversation['status'] = ConversationStatus.processing.value

    result, transaction = _claim(conversation, request_id='request-second')

    assert result['outcome'] == 'busy'
    assert result['conversation']['processing_retry_id'] == 'request-first'
    assert transaction.updates == []
    assert transaction.sets == []


def test_completed_conversation_is_idempotent_for_any_retry_id():
    conversation = _failed_conversation(request_id='request-first')
    conversation['status'] = ConversationStatus.completed.value

    result, transaction = _claim(conversation, request_id='request-second')

    assert result['outcome'] == 'completed'
    assert transaction.updates == []
    assert transaction.sets[0][1]['outcome'] == ConversationStatus.completed.value


def test_generic_processing_failure_is_visible_but_not_automatically_retried():
    result, transaction = _claim(_failed_conversation(error='conversation_processing_failed'))

    assert result['outcome'] == 'not_retryable'
    assert transaction.updates == []
    assert transaction.sets == []


def test_missing_conversation_is_not_claimed():
    result, transaction = _claim(None)

    assert result == {'outcome': 'not_found', 'conversation': None}
    assert transaction.updates == []
    assert transaction.sets == []


def test_summary_writeback_phase_is_receipted_before_vector_completion():
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
    assert transaction.updates[0][1] == {"processing_retry_summary_version_id": "recovered-v1"}
    assert transaction.updates[1][1] == {
        "phase": "summary_applied",
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
    assert transaction.updates[0][1] == {"outcome": "completed", "updated_at": finished_at}
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
        "updated_at": failed_at,
    }
    assert transaction.updates[1][1] == {
        "enrichment_state": {
            "status": "failed",
            "pending": True,
            "source": "observer",
            "kind": "recovered_enriched",
            "trace_id": "summary-retry:conversation-1:request-123:hermes",
            "updated_at": failed_at,
            "error": "hermes_temporarily_unavailable",
        }
    }


def test_hermes_success_records_enriched_version_without_changing_completed_status():
    transaction = _FakeTransaction()
    conversation_ref = _FakeDocumentRef(
        {
            "status": "completed",
            "processing_retry_id": "request-123",
            "processing_retry_summary_version_id": "generic-v1",
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
    )

    assert result is True
    assert transaction.updates[0][1] == {
        "enrichment_status": "completed",
        "updated_at": completed_at,
        "enriched_summary_version_id": "enriched-v2",
    }
    assert transaction.updates[1][1] == {
        "processing_retry_enriched_version_id": "enriched-v2",
    }
