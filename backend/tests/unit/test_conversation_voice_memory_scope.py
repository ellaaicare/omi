import copy
import asyncio
import os
import sys
import types

import pytest
from fastapi import HTTPException

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:9999")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("ENCRYPTION_SECRET", "test-encryption-secret-32-bytes-long")

if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = lambda *args, **kwargs: None
    sys.modules["redis"] = redis_stub

if "stripe" not in sys.modules:
    stripe_stub = types.ModuleType("stripe")
    stripe_stub.api_key = None
    sys.modules["stripe"] = stripe_stub

storage_stub = types.ModuleType("utils.other.storage")
storage_stub.list_audio_chunks = lambda *args, **kwargs: []
sys.modules.setdefault("utils.other.storage", storage_stub)
sys.modules.setdefault("websockets", types.ModuleType("websockets"))

from database import conversations as conversations_db
from ella.routers import voice


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return copy.deepcopy(self._data)


class _DocumentRef:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Transaction:
    def __init__(self):
        self.updates = []

    def update(self, document_ref, update_data):
        update_data = copy.deepcopy(update_data)
        self.updates.append(update_data)
        document_ref.data.update(update_data)


def _legacy_conversation():
    return {
        "id": "memory-a",
        "structured": {
            "title": "Cafe visit",
            "overview": "The user ordered coffee and waffles.",
            "category": "other",
        },
    }


def test_legacy_memory_atomically_bootstraps_a_durable_active_version():
    reference = _DocumentRef(_legacy_conversation())
    transaction = _Transaction()

    result = conversations_db._ensure_voice_memory_summary_version_transaction(
        transaction,
        reference,
    )

    assert result["status"] == "ready"
    assert result["active_summary_version_id"]
    assert reference.data["active_summary_version_id"] == result["active_summary_version_id"]
    assert reference.data["summary_versions"][0]["id"] == result["active_summary_version_id"]
    assert transaction.updates == [
        {
            "summary_versions": reference.data["summary_versions"],
            "active_summary_version_id": result["active_summary_version_id"],
        }
    ]


def test_transaction_retry_after_competing_bootstrap_observes_committed_version_without_second_write():
    losing_reference = _DocumentRef(_legacy_conversation())
    losing_transaction = _Transaction()
    first_attempt = conversations_db._ensure_voice_memory_summary_version_transaction(
        losing_transaction,
        losing_reference,
        "client-prebootstrap-version",
    )

    assert first_attempt["status"] == "stale"
    assert len(losing_transaction.updates) == 1

    # Simulate another transaction committing the same deterministic bootstrap
    # while Firestore retries this callback from a fresh server snapshot.
    winning_snapshot = _legacy_conversation()
    winning_snapshot.update(copy.deepcopy(losing_transaction.updates[0]))
    established_version = winning_snapshot["active_summary_version_id"]
    retry_reference = _DocumentRef(winning_snapshot)
    retry_transaction = _Transaction()

    retry = conversations_db._ensure_voice_memory_summary_version_transaction(
        retry_transaction,
        retry_reference,
        established_version,
    )

    assert retry["status"] == "ready"
    assert retry["active_summary_version_id"] == established_version
    assert retry_transaction.updates == []


def test_unversionable_memory_returns_defined_nonwriteable_state():
    reference = _DocumentRef({"id": "memory-empty", "structured": {}})
    transaction = _Transaction()

    result = conversations_db._ensure_voice_memory_summary_version_transaction(
        transaction,
        reference,
    )

    assert result == {"status": "version_unavailable"}
    assert transaction.updates == []


class _PathReference:
    def __init__(self, path):
        self.path = path

    def collection(self, name):
        return _PathReference((*self.path, name))

    def document(self, name):
        return _PathReference((*self.path, name))


class _PathDatabase:
    def collection(self, name):
        return _PathReference((name,))

    def transaction(self):
        return object()


def test_missing_and_nonowned_loader_queries_only_authenticated_user_subcollection(monkeypatch):
    queried_paths = []

    def missing(_transaction, conversation_ref, expected_version_id=None):
        queried_paths.append((conversation_ref.path, expected_version_id))
        return {"status": "not_found"}

    monkeypatch.setattr(conversations_db, "db", _PathDatabase())
    monkeypatch.setattr(conversations_db, "_ensure_voice_memory_summary_version", missing)

    responses = []
    for conversation_id in ("missing", "owned-by-user-b"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                voice._resolve_voice_memory_scope(
                    "user-a",
                    voice.VoiceSessionScope(kind="memory", conversation_id=conversation_id),
                )
            )
        responses.append((error.value.status_code, error.value.detail))

    assert responses == [
        (404, {"code": "voice_session_scope_not_found"}),
        (404, {"code": "voice_session_scope_not_found"}),
    ]
    assert queried_paths == [
        (("users", "user-a", "conversations", "missing"), None),
        (("users", "user-a", "conversations", "owned-by-user-b"), None),
    ]
