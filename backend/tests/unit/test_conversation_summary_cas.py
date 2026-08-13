import ast
import importlib.util
import json
import logging
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from utils.ella.canonical_omi import transcript_grounding_hash


def _load_conversations_module(monkeypatch):
    import utils
    import utils.other

    encryption_mock = MagicMock()
    monkeypatch.setitem(sys.modules, "database._client", MagicMock(db=MagicMock()))
    monkeypatch.setitem(sys.modules, "database.users", MagicMock())
    monkeypatch.setitem(sys.modules, "database.redis_db", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.encryption", encryption_mock)
    monkeypatch.setattr(utils, "encryption", encryption_mock, raising=False)
    monkeypatch.setitem(sys.modules, "utils.other.hume", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.other.storage", MagicMock(list_audio_chunks=MagicMock()))

    path = Path(__file__).resolve().parents[2] / "database" / "conversations.py"
    spec = importlib.util.spec_from_file_location("database.conversations_cas_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_active_summary_version_compare_and_set_is_atomic_and_fail_closed(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)

    class ConversationRef:
        def __init__(self, active_version):
            self.active_version = active_version

        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": self.active_version,
                    "data_protection_level": "standard",
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    matching_ref = ConversationRef("expected-v1")
    matching_transaction = Transaction()
    assert (
        conversations._update_conversation_if_active_summary_version_transaction(
            matching_transaction,
            matching_ref,
            "uid-1",
            "expected-v1",
            {"active_summary_version_id": "undo-v2"},
        )
        is True
    )
    assert matching_transaction.updates == [(matching_ref, {"active_summary_version_id": "undo-v2"})]

    stale_ref = ConversationRef("newer-v2")
    stale_transaction = Transaction()
    assert (
        conversations._update_conversation_if_active_summary_version_transaction(
            stale_transaction,
            stale_ref,
            "uid-1",
            "expected-v1",
            {"active_summary_version_id": "undo-v2"},
        )
        is False
    )
    assert stale_transaction.updates == []


@pytest.mark.parametrize(
    ("field", "racing_value"),
    [
        ("trace_id", "trace-racing"),
        ("request_fingerprint", "sha256:" + ("f" * 64)),
        ("status", "writeback_applied"),
        ("canonical_status", "completed"),
        ("source", "ios_correction"),
        ("kind", "corrected_enriched"),
    ],
)
def test_summary_authority_compare_and_set_rejects_same_version_enrichment_race(
    monkeypatch,
    field,
    racing_value,
):
    conversations = _load_conversations_module(monkeypatch)
    expected_state = {
        "trace_id": "trace-expected",
        "request_fingerprint": "sha256:" + ("a" * 64),
        "status": "writeback_pending_canonical",
        "canonical_status": "pending",
        "source": "observer",
        "kind": "recovered_enriched",
    }
    racing_state = {**expected_state, field: racing_value}

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "summary-v2",
                    "data_protection_level": "standard",
                    "enrichment_state": racing_state,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    transaction = Transaction()
    assert (
        conversations._update_conversation_if_summary_authority_transaction(
            transaction,
            ConversationRef(),
            "uid-1",
            "summary-v2",
            expected_state,
            {"enrichment_state": {**expected_state, "canonical_status": "completed"}},
        )
        is False
    )
    assert transaction.updates == []


def test_summary_authority_compare_and_set_updates_only_exact_authority(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    expected_state = {
        "trace_id": "trace-expected",
        "request_fingerprint": "sha256:" + ("a" * 64),
        "status": "writeback_pending_canonical",
        "canonical_status": "pending",
        "source": "observer",
        "kind": "recovered_enriched",
    }

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "summary-v2",
                    "data_protection_level": "standard",
                    "enrichment_state": expected_state,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    conversation_ref = ConversationRef()
    transaction = Transaction()
    update = {"enrichment_state": {**expected_state, "canonical_status": "completed"}}
    assert (
        conversations._update_conversation_if_summary_authority_transaction(
            transaction,
            conversation_ref,
            "uid-1",
            "summary-v2",
            expected_state,
            update,
        )
        is True
    )
    assert transaction.updates == [(conversation_ref, update)]


@pytest.mark.parametrize(
    "current_segments",
    [
        [
            {"id": "one", "text": "The original supported statement.", "start": 0.0, "end": 2.0},
            {"id": "two", "text": "A later contradictory segment.", "start": 2.0, "end": 4.0},
        ],
        [{"id": "one", "text": "The original statement was edited.", "start": 0.0, "end": 2.0}],
        [
            {"id": "two", "text": "A second original segment.", "start": 2.0, "end": 4.0},
            {"id": "one", "text": "The original supported statement.", "start": 0.0, "end": 2.0},
        ],
        [{"id": "replacement", "text": "An unrelated replacement transcript.", "start": 8.0, "end": 10.0}],
    ],
    ids=["append", "edit", "reorder", "replacement"],
)
def test_transcript_hash_compare_and_set_rejects_every_source_race(monkeypatch, current_segments):
    conversations = _load_conversations_module(monkeypatch)
    observed_segments = [
        {"id": "one", "text": "The original supported statement.", "start": 0.0, "end": 2.0},
        {"id": "two", "text": "A second original segment.", "start": 2.0, "end": 4.0},
    ]

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "source-v1",
                    "data_protection_level": "standard",
                    "transcript_segments": current_segments,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    transaction = Transaction()
    assert (
        conversations._update_conversation_if_transcript_hash_transaction(
            transaction,
            ConversationRef(),
            "uid-1",
            transcript_grounding_hash(observed_segments),
            {"active_summary_version_id": "summary-v2"},
        )
        is False
    )
    assert transaction.updates == []


def test_transcript_hash_compare_and_set_publishes_once_for_exact_source(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    observed_segments = [
        {"id": "one", "text": "The original supported statement.", "start": 0.0, "end": 2.0},
        {"id": "two", "text": "A second original segment.", "start": 2.0, "end": 4.0},
    ]

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "source-v1",
                    "data_protection_level": "standard",
                    "transcript_segments": observed_segments,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    conversation_ref = ConversationRef()
    transaction = Transaction()
    assert (
        conversations._update_conversation_if_transcript_hash_transaction(
            transaction,
            conversation_ref,
            "uid-1",
            transcript_grounding_hash(observed_segments),
            {"active_summary_version_id": "summary-v2"},
            expected_active_summary_version_id="source-v1",
        )
        is True
    )
    assert transaction.updates == [(conversation_ref, {"active_summary_version_id": "summary-v2"})]


def test_transcript_hash_compare_and_set_can_require_no_active_source_version(
    monkeypatch,
):
    conversations = _load_conversations_module(monkeypatch)
    observed_segments = [{"id": "one", "text": "Synthetic source."}]

    class ConversationRef:
        def __init__(self, active_version):
            self.active_version = active_version

        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": self.active_version,
                    "data_protection_level": "standard",
                    "transcript_segments": observed_segments,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    expected_hash = transcript_grounding_hash(observed_segments)
    empty_transaction = Transaction()
    assert conversations._update_conversation_if_transcript_hash_transaction(
        empty_transaction,
        ConversationRef(None),
        "uid-1",
        expected_hash,
        {"active_summary_version_id": "summary-v1"},
        expected_active_summary_version_id=None,
        match_active_summary_version=True,
    )
    assert len(empty_transaction.updates) == 1

    raced_transaction = Transaction()
    assert not conversations._update_conversation_if_transcript_hash_transaction(
        raced_transaction,
        ConversationRef("raced-v1"),
        "uid-1",
        expected_hash,
        {"active_summary_version_id": "summary-v1"},
        expected_active_summary_version_id=None,
        match_active_summary_version=True,
    )
    assert raced_transaction.updates == []


def test_transcript_hash_compare_and_set_rejects_same_version_enrichment_race(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    observed_segments = [
        {"id": "one", "text": "The exact supported statement.", "start": 0.0, "end": 2.0},
    ]
    expected_state = {
        "trace_id": "trace-expected",
        "request_fingerprint": "sha256:" + ("a" * 64),
        "status": "writeback_applied",
        "canonical_status": "completed",
        "source": "observer",
        "kind": "generic_recovered",
    }

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "source-v1",
                    "data_protection_level": "standard",
                    "transcript_segments": observed_segments,
                    "enrichment_state": {**expected_state, "trace_id": "trace-racing"},
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    transaction = Transaction()
    assert (
        conversations._update_conversation_if_transcript_hash_transaction(
            transaction,
            ConversationRef(),
            "uid-1",
            transcript_grounding_hash(observed_segments),
            {"active_summary_version_id": "summary-v2"},
            expected_active_summary_version_id="source-v1",
            expected_enrichment_state=expected_state,
        )
        is False
    )
    assert transaction.updates == []


@pytest.mark.parametrize("protection_level", ["standard", "enhanced"])
def test_transcript_hash_compare_and_set_uses_decrypted_transaction_snapshot(monkeypatch, protection_level):
    conversations = _load_conversations_module(monkeypatch)
    observed_segments = [
        {"id": "one", "text": "The exact protected source statement.", "start": 0.0, "end": 2.0},
        {"id": "two", "text": "A second protected source segment.", "start": 2.0, "end": 4.0},
    ]
    compressed = zlib.compress(json.dumps(observed_segments).encode("utf-8"))
    stored_segments = compressed
    if protection_level == "enhanced":
        stored_segments = "encrypted-transcript"
        conversations.encryption.decrypt_strict.return_value = compressed.hex()

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "source-v1",
                    "data_protection_level": protection_level,
                    "transcript_segments": stored_segments,
                    "transcript_segments_compressed": True,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    conversation_ref = ConversationRef()
    transaction = Transaction()
    assert (
        conversations._update_conversation_if_transcript_hash_transaction(
            transaction,
            conversation_ref,
            "uid-1",
            transcript_grounding_hash(observed_segments),
            {"active_summary_version_id": "summary-v2"},
            expected_active_summary_version_id="source-v1",
        )
        is True
    )
    assert transaction.updates == [(conversation_ref, {"active_summary_version_id": "summary-v2"})]


def test_transcript_hash_compare_and_set_fails_closed_silently_when_protected_source_cannot_decrypt(
    monkeypatch, caplog, capsys
):
    conversations = _load_conversations_module(monkeypatch)
    subject = "private-subject-uid"
    ciphertext = "private-ciphertext"
    exception_detail = "private-decryption-exception"
    conversations.encryption.decrypt_strict.side_effect = ValueError(exception_detail)

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": "source-v1",
                    "data_protection_level": "enhanced",
                    "transcript_segments": ciphertext,
                    "transcript_segments_compressed": True,
                },
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    transaction = Transaction()
    with caplog.at_level(logging.DEBUG):
        assert (
            conversations._update_conversation_if_transcript_hash_transaction(
                transaction,
                ConversationRef(),
                subject,
                transcript_grounding_hash([{"text": "Expected protected source."}]),
                {"active_summary_version_id": "summary-v2"},
            )
            is False
        )
    assert transaction.updates == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert caplog.records == []


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _ConversationRef:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Transaction:
    def __init__(self):
        self.updates = []
        self.sets = []

    def update(self, ref, payload):
        self.updates.append((ref, payload))
        ref.data.update(payload)

    def set(self, ref, payload):
        self.sets.append((ref, payload))
        ref.data = dict(payload)


def _stock_processing_payload(active_summary_version_id=None):
    return {
        "id": "conversation-1",
        "created_at": "2026-07-22T00:00:00+00:00",
        "started_at": "2026-07-22T00:00:00+00:00",
        "finished_at": "2026-07-22T00:01:00+00:00",
        "structured": {
            "title": "Processed title",
            "overview": "Processed overview",
            "emoji": "brain",
            "category": "other",
        },
        "summary_versions": [],
        "active_summary_version_id": active_summary_version_id,
        "transcript_segments": [{"text": "stale processor segment"}],
        "transcript_segments_compressed": False,
        "status": "completed",
        "discarded": False,
        "processing_error": None,
        "processing_error_at": None,
    }


def test_stock_summary_commit_preserves_concurrent_capture_fields_and_merges_process_metadata(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    durable = {
        "id": "conversation-1",
        "created_at": "2026-07-22T00:00:00+00:00",
        "started_at": "2026-07-22T00:00:00+00:00",
        "finished_at": "2026-07-22T00:02:00+00:00",
        "structured": {},
        "summary_versions": [],
        "active_summary_version_id": None,
        "transcript_segments": [{"text": "fresh capture segment"}],
        "transcript_segments_compressed": True,
        "capture_persistence_applied_batch_ids": ["batch-a"],
        "audio_files": [{"id": "fresh-audio"}],
        "folder_id": "fresh-folder",
        "correction_state": {"status": "fresh-correction"},
        "enrichment_state": {"status": "fresh-enrichment"},
        "external_data": {
            "durable_only": True,
            "shared": "durable",
        },
        "geolocation": {"latitude": 1.0, "longitude": 2.0},
        "unknown_runtime_field": {"keep": True},
        "status": "processing",
        "discarded": False,
    }
    ref = _ConversationRef(dict(durable))
    transaction = _Transaction()
    processing_payload = _stock_processing_payload()
    processing_payload.update(
        {
            "audio_files": [{"id": "stale-audio"}],
            "folder_id": "stale-folder",
            "correction_state": {"status": "stale-correction"},
            "enrichment_state": {"status": "stale-enrichment"},
            "external_data": {
                "process_only": True,
                "shared": "process",
            },
            "geolocation": {"latitude": 9.0, "longitude": 9.0},
        }
    )

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        processing_payload,
        expected_active_summary_version_id=None,
    )

    assert result["status"] == "committed"
    assert result["dispatched"] is True
    assert len(transaction.updates) == 1
    updated_fields = transaction.updates[0][1]
    assert "transcript_segments" not in updated_fields
    assert "transcript_segments_compressed" not in updated_fields
    assert "finished_at" not in updated_fields
    assert "capture_persistence_applied_batch_ids" not in updated_fields
    assert ref.data["transcript_segments"] == durable["transcript_segments"]
    assert ref.data["transcript_segments_compressed"] is True
    assert ref.data["finished_at"] == durable["finished_at"]
    assert ref.data["capture_persistence_applied_batch_ids"] == ["batch-a"]
    assert ref.data["audio_files"] == [{"id": "fresh-audio"}]
    assert ref.data["folder_id"] == "fresh-folder"
    assert ref.data["correction_state"] == {"status": "fresh-correction"}
    assert ref.data["enrichment_state"] == {"status": "fresh-enrichment"}
    assert updated_fields["external_data"] == {
        "durable_only": True,
        "process_only": True,
        "shared": "process",
    }
    assert updated_fields["geolocation"] == {"latitude": 9.0, "longitude": 9.0}
    assert ref.data["external_data"] == updated_fields["external_data"]
    assert ref.data["geolocation"] == updated_fields["geolocation"]
    assert ref.data["unknown_runtime_field"] == {"keep": True}
    assert result["conversation"]["transcript_segments"] == durable["transcript_segments"]
    assert result["conversation"]["active_summary_version_id"] == result["active_summary_version_id"]
    assert len(ref.data["summary_versions"]) == 1
    assert ref.data["summary_versions"][0]["source"] == "omi"
    assert ref.data["summary_versions"][0]["kind"] == "generated"
    assert ref.data["summary_versions"][0]["is_active"] is True


def test_stock_summary_commit_does_not_clear_durable_metadata_with_process_nulls(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    durable = {
        "id": "conversation-1",
        "created_at": "2026-07-22T00:00:00+00:00",
        "structured": {},
        "summary_versions": [],
        "active_summary_version_id": None,
        "external_data": {"durable_only": True},
        "geolocation": {"latitude": 1.0, "longitude": 2.0},
        "status": "processing",
        "discarded": False,
    }
    ref = _ConversationRef(dict(durable))
    transaction = _Transaction()
    processing_payload = _stock_processing_payload()
    processing_payload.update({"external_data": None, "geolocation": None})

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        processing_payload,
        expected_active_summary_version_id=None,
    )

    assert result["status"] == "committed"
    updated_fields = transaction.updates[0][1]
    assert "external_data" not in updated_fields
    assert "geolocation" not in updated_fields
    assert ref.data["external_data"] == durable["external_data"]
    assert ref.data["geolocation"] == durable["geolocation"]


def test_stock_summary_missing_persisted_conversation_does_not_resurrect_without_create_authority(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(None)
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
        allow_create=False,
    )

    assert result == {
        "status": conversations.conversation_stock_summary_deleted,
        "dispatched": False,
    }
    assert transaction.sets == []
    assert transaction.updates == []


def test_stock_summary_two_processors_from_empty_source_produce_one_winner(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {},
            "summary_versions": [],
            "active_summary_version_id": None,
            "status": "processing",
            "discarded": False,
        }
    )

    first = conversations._commit_stock_summary_processing_result_transaction(
        _Transaction(),
        ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
    )
    second_transaction = _Transaction()
    second = conversations._commit_stock_summary_processing_result_transaction(
        second_transaction,
        ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
    )

    assert first["status"] == "committed"
    assert first["dispatched"] is True
    assert second["status"] == conversations.conversation_stock_summary_cas_lost
    assert second["dispatched"] is False
    assert second_transaction.updates == []
    assert len(ref.data["summary_versions"]) == 1
    assert ref.data["active_summary_version_id"] == first["active_summary_version_id"]


def test_stock_summary_initial_version_id_is_deterministic_for_same_source(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    payload = _stock_processing_payload()

    def commit_once():
        ref = _ConversationRef(
            {
                "id": "conversation-1",
                "created_at": "2026-07-22T00:00:00+00:00",
                "structured": {},
                "summary_versions": [],
                "active_summary_version_id": None,
                "status": "processing",
                "discarded": False,
            }
        )
        return conversations._commit_stock_summary_processing_result_transaction(
            _Transaction(),
            ref,
            "uid-1",
            payload,
            expected_active_summary_version_id=None,
        )

    assert commit_once()["active_summary_version_id"] == commit_once()["active_summary_version_id"]


def test_stock_summary_missing_doc_parent_write_sanitizes_photos_and_audio_url(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    payload = _stock_processing_payload()
    payload["photos"] = [{"id": "photo-1", "base64": "raw-photo"}]
    payload["audio_base64_url"] = "data:audio/wav;base64,raw-audio"
    ref = _ConversationRef(None)
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        payload,
        expected_active_summary_version_id=None,
        allow_create=True,
    )

    assert result["status"] == "committed"
    assert len(transaction.sets) == 1
    written_parent = transaction.sets[0][1]
    assert "photos" not in written_parent
    assert "audio_base64_url" not in written_parent
    assert result["conversation"]["photos"] == [{"id": "photo-1", "base64": "raw-photo"}]
    assert result["conversation"]["audio_base64_url"] == "data:audio/wav;base64,raw-audio"


def test_stock_summary_missing_doc_honors_enhanced_data_protection(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    conversations.encryption.encrypt.return_value = "encrypted-compressed-transcript"
    payload = _stock_processing_payload()
    payload["data_protection_level"] = "enhanced"
    ref = _ConversationRef(None)
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        payload,
        expected_active_summary_version_id=None,
        allow_create=True,
    )

    assert result["status"] == "committed"
    written_parent = transaction.sets[0][1]
    assert written_parent["data_protection_level"] == "enhanced"
    assert written_parent["transcript_segments"] == "encrypted-compressed-transcript"
    assert written_parent["transcript_segments_compressed"] is True


def test_stock_summary_public_wrapper_backfills_enhanced_data_protection(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    import database.helpers as helpers

    captured = {}

    def commit_inner(
        transaction,
        conversation_ref,
        uid,
        processing_conversation,
        expected_active_summary_version_id=None,
        allow_create=False,
        enqueue_hermes_cloud_enrichment=False,
        enrichment_enqueued_at=None,
    ):
        captured["processing_conversation"] = dict(processing_conversation)
        return {"status": "committed", "conversation": processing_conversation, "dispatched": True}

    monkeypatch.setattr(helpers.redis_db, "get_user_data_protection_level", lambda uid: "enhanced")
    monkeypatch.setattr(conversations, "_commit_stock_summary_processing_result", commit_inner)

    conversations.commit_stock_summary_processing_result(
        "uid-1",
        "conversation-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
    )

    assert captured["processing_conversation"]["data_protection_level"] == "enhanced"


def test_stock_summary_sequential_reprocess_uses_expected_active_version_cas(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {"title": "Old", "overview": "Old"},
            "summary_versions": [
                {
                    "id": "stock-v1",
                    "created_at": "2026-07-22T00:00:00+00:00",
                    "source": "omi",
                    "kind": "generated",
                    "title": "Old",
                    "overview": "Old",
                    "emoji": "brain",
                    "category": "other",
                    "is_active": True,
                }
            ],
            "active_summary_version_id": "stock-v1",
            "status": "completed",
            "discarded": False,
        }
    )

    first = conversations._commit_stock_summary_processing_result_transaction(
        _Transaction(),
        ref,
        "uid-1",
        _stock_processing_payload(active_summary_version_id="stock-v1"),
        expected_active_summary_version_id="stock-v1",
    )
    second_transaction = _Transaction()
    second = conversations._commit_stock_summary_processing_result_transaction(
        second_transaction,
        ref,
        "uid-1",
        _stock_processing_payload(active_summary_version_id="stock-v1"),
        expected_active_summary_version_id="stock-v1",
    )

    assert first["status"] == "committed"
    assert len(ref.data["summary_versions"]) == 2
    assert ref.data["summary_versions"][0]["is_active"] is False
    assert ref.data["summary_versions"][1]["based_on_version_id"] == "stock-v1"
    assert isinstance(ref.data["summary_versions"][1]["created_at"], datetime)
    assert ref.data["summary_versions"][1]["created_at"] != datetime.fromisoformat("2026-07-22T00:00:00+00:00")
    assert second["status"] == conversations.conversation_stock_summary_cas_lost
    assert second_transaction.updates == []


def test_stock_summary_discarded_processing_preserves_durable_authority(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {"title": "Durable", "overview": "Durable"},
            "summary_versions": [
                {
                    "id": "durable-v1",
                    "created_at": "2026-07-22T00:00:00+00:00",
                    "source": "omi",
                    "kind": "generated",
                    "title": "Durable",
                    "overview": "Durable",
                    "emoji": "brain",
                    "category": "other",
                    "is_active": True,
                }
            ],
            "active_summary_version_id": "durable-v1",
            "status": "processing",
            "discarded": False,
        }
    )
    stale_processing = _stock_processing_payload(active_summary_version_id="stale-v2")
    stale_processing["discarded"] = True
    stale_processing["summary_versions"] = [{"id": "stale-v2", "is_active": True}]
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        stale_processing,
        expected_active_summary_version_id="durable-v1",
    )

    assert result["status"] == "committed"
    updated_fields = transaction.updates[0][1]
    assert "summary_versions" not in updated_fields
    assert "active_summary_version_id" not in updated_fields
    assert ref.data["active_summary_version_id"] == "durable-v1"
    assert ref.data["summary_versions"][0]["id"] == "durable-v1"
    assert result["conversation"]["active_summary_version_id"] == "durable-v1"


@pytest.mark.parametrize(
    "summary_versions,active_id,reason",
    [
        (
            [{"id": "v1", "is_active": True}, {"id": "v2", "is_active": True}],
            "v1",
            "multiple_active_versions",
        ),
        ([{"id": "v1", "is_active": True}], "missing", "active_id_missing_or_duplicate"),
        ([{"id": "v1", "is_active": False}], "v1", "active_id_points_inactive"),
    ],
)
def test_stock_summary_malformed_authority_fails_closed(monkeypatch, summary_versions, active_id, reason):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {},
            "summary_versions": summary_versions,
            "active_summary_version_id": active_id,
            "status": "processing",
            "discarded": False,
        }
    )
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        _stock_processing_payload(active_summary_version_id=active_id),
        expected_active_summary_version_id=active_id,
    )

    assert result["status"] == conversations.conversation_stock_summary_malformed
    assert result["reason"] == reason
    assert result["dispatched"] is False
    assert transaction.updates == []


@pytest.mark.parametrize("malformed_versions", [{}, "", 0, False])
def test_stock_summary_falsey_non_list_authority_fails_closed(monkeypatch, malformed_versions):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {},
            "summary_versions": malformed_versions,
            "active_summary_version_id": None,
            "status": "processing",
            "discarded": False,
        }
    )
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
    )

    assert result["status"] == conversations.conversation_stock_summary_malformed
    assert result["reason"] == "versions_not_list"
    assert result["dispatched"] is False
    assert transaction.updates == []


@pytest.mark.parametrize("malformed_active_id", [{}, [], 0, False])
def test_stock_summary_falsey_non_string_active_id_fails_closed(monkeypatch, malformed_active_id):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {},
            "summary_versions": [],
            "active_summary_version_id": malformed_active_id,
            "status": "processing",
            "discarded": False,
        }
    )
    transaction = _Transaction()

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
    )

    assert result["status"] == conversations.conversation_stock_summary_malformed
    assert result["reason"] == "active_id_not_string"
    assert result["dispatched"] is False
    assert transaction.updates == []


def test_hermes_enrichment_outbox_is_written_in_same_transaction_as_summary(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    source = Path(conversations.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "ella.services.hermes_cloud_enrichment" not in source
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        for node in ast.walk(function)
    )
    monkeypatch.setattr(
        conversations,
        "hermes_cloud_enrichment_outbox_collection",
        "ella_hermes_cloud_enrichment_outbox",
    )
    monkeypatch.setattr(conversations, "HERMES_CLOUD_ENRICHMENT_POLICY_VERSION", "policy-v1")
    monkeypatch.setattr(
        conversations,
        "build_enrichment_identity",
        lambda **_kwargs: SimpleNamespace(
            job_id="hce-job-1",
            client_interaction_id="interaction-1",
            transcript_sha256="transcript-sha-1",
        ),
    )

    outbox_ref = _ConversationRef(None)
    fake_db = SimpleNamespace(
        collection=lambda collection_name: SimpleNamespace(
            document=lambda job_id: (
                outbox_ref
                if collection_name == "ella_hermes_cloud_enrichment_outbox" and job_id == "hce-job-1"
                else None
            )
        )
    )
    monkeypatch.setattr(conversations, "db", fake_db)
    conversation_ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "started_at": "2026-07-22T00:00:00+00:00",
            "finished_at": "2026-07-22T00:01:00+00:00",
            "structured": {},
            "summary_versions": [],
            "active_summary_version_id": None,
            "transcript_segments": [{"text": "durable capture"}],
            "status": "processing",
            "discarded": False,
            "data_protection_level": "standard",
        }
    )
    transaction = _Transaction()
    enqueued_at = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        conversation_ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
        enqueue_hermes_cloud_enrichment=True,
        enrichment_enqueued_at=enqueued_at,
    )

    assert result["status"] == "committed"
    assert result["hermes_enrichment_job_id"] == "hce-job-1"
    assert outbox_ref.data["status"] == "pending"
    assert outbox_ref.data["conversation_id"] == "conversation-1"
    assert outbox_ref.data["created_at"] == enqueued_at
    assert transaction.sets == [(outbox_ref, outbox_ref.data)]
    assert transaction.updates[0][0] is conversation_ref


def test_conditional_processing_failure_cannot_roll_back_completed_authority(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    durable = {
        "id": "conversation-1",
        "status": "completed",
        "discarded": False,
        "transcript_segments": [{"text": "fresh capture"}],
        "active_summary_version_id": "stock-v1",
        "summary_versions": [{"id": "stock-v1", "is_active": True}],
    }
    ref = _ConversationRef(dict(durable))
    transaction = _Transaction()

    result = conversations._mark_conversation_processing_failed_if_unfinished_transaction(
        transaction,
        ref,
        "uid-1",
        "conversation_processing_failed",
        datetime.now().astimezone(),
    )

    assert result == {"updated": False, "reason": "already_completed"}
    assert transaction.updates == []
    assert ref.data == durable


def test_conditional_processing_failure_updates_only_failure_fields(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    durable = {
        "id": "conversation-1",
        "status": "processing",
        "discarded": False,
        "transcript_segments": [{"text": "fresh capture"}],
        "unknown_runtime_field": {"keep": True},
    }
    ref = _ConversationRef(dict(durable))
    transaction = _Transaction()
    failed_at = datetime.now().astimezone()

    result = conversations._mark_conversation_processing_failed_if_unfinished_transaction(
        transaction,
        ref,
        "uid-1",
        "conversation_processing_failed",
        failed_at,
    )

    assert result["updated"] is True
    assert len(transaction.updates) == 1
    assert set(transaction.updates[0][1]) == {
        "status",
        "discarded",
        "processing_error",
        "processing_error_at",
    }
    assert ref.data["transcript_segments"] == durable["transcript_segments"]
    assert ref.data["unknown_runtime_field"] == {"keep": True}


def test_folder_assignment_cas_does_not_overwrite_concurrent_owner_choice(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef({"id": "conversation-1", "folder_id": "manual-folder"})
    transaction = _Transaction()

    updated = conversations._assign_conversation_folder_if_unset_transaction(
        transaction,
        ref,
        "automatic-folder",
    )

    assert updated is False
    assert transaction.updates == []
    assert ref.data["folder_id"] == "manual-folder"


def test_initial_processing_claim_rejects_completed_duplicate_without_status_rollback(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef({"id": "conversation-1", "status": "completed"})
    transaction = _Transaction()

    result = conversations._claim_initial_conversation_processing_transaction(transaction, ref)

    assert result == {"status": "already_completed"}
    assert transaction.updates == []
    assert ref.data["status"] == "completed"


def test_initial_processing_claim_promotes_in_progress_once_with_partial_update(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "status": "in_progress",
            "transcript_segments": [{"text": "durable capture"}],
        }
    )
    transaction = _Transaction()

    result = conversations._claim_initial_conversation_processing_transaction(transaction, ref)

    assert result == {"status": "processing_claimed"}
    assert transaction.updates == [(ref, {"status": "processing"})]
    assert ref.data["transcript_segments"] == [{"text": "durable capture"}]


def test_strict_decrypt_round_trip_and_failure_emit_no_protected_context(monkeypatch, caplog, capsys):
    monkeypatch.setenv("ENCRYPTION_SECRET", "test-encryption-secret-exactly-32-bytes")
    path = Path(__file__).resolve().parents[2] / "utils" / "encryption.py"
    spec = importlib.util.spec_from_file_location("utils.encryption_strict_cas_test", path)
    encryption = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(encryption)

    subject = "private-subject-uid"
    ciphertext = "private-ciphertext"
    plaintext = "private-transcript-payload"
    assert encryption.decrypt_strict(encryption.encrypt(plaintext, subject), subject) == plaintext
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception):
        encryption.decrypt_strict(ciphertext, subject)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert caplog.records == []
