import ast
import asyncio
import copy
import importlib.util
import json
import logging
import os
import sys
import threading
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
        def __init__(self, active_version, receipt=None):
            self.active_version = active_version
            self.receipt = receipt

        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "active_summary_version_id": self.active_version,
                    "data_protection_level": "standard",
                    "summary_writeback_receipt": self.receipt,
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

    pending_ref = ConversationRef("expected-v1", {"status": "pending_reconciliation"})
    pending_transaction = Transaction()
    with pytest.raises(
        conversations.PendingConversationSummaryReconciliationError,
        match="canonical_summary_reconciliation_pending",
    ):
        conversations._update_conversation_if_active_summary_version_transaction(
            pending_transaction,
            pending_ref,
            "uid-1",
            "expected-v1",
            {"structured.title": "legacy-b", "summary_writeback_receipt": None},
        )
    assert pending_transaction.updates == []

    completed_ref = ConversationRef("expected-v1", {"status": "completed"})
    completed_transaction = Transaction()
    assert conversations._update_conversation_if_active_summary_version_transaction(
        completed_transaction,
        completed_ref,
        "uid-1",
        "expected-v1",
        {"structured.title": "legacy-b", "summary_writeback_receipt": None},
    )
    assert completed_transaction.updates == [
        (completed_ref, {"structured.title": "legacy-b", "summary_writeback_receipt": None})
    ]


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


def test_capture_finalizer_summary_commit_requires_exact_unexpired_claim_in_same_transaction(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    now = datetime.now(timezone.utc)
    durable = {
        "id": "conversation-1",
        "created_at": "2026-07-22T00:00:00+00:00",
        "structured": {},
        "summary_versions": [],
        "active_summary_version_id": None,
        "status": "processing",
        "discarded": False,
        "data_protection_level": "standard",
        "capture_protocol_version": 2,
        "capture_generation": "generation-a",
        "capture_owner_token": "owner-a",
        "capture_state": "finalizing",
        "capture_finalization_claim_token": "claim-a",
        "capture_finalization_lease_expires_at": now + timedelta(seconds=30),
    }

    exact_ref = _ConversationRef(copy.deepcopy(durable))
    exact_transaction = _Transaction()
    exact = conversations._commit_stock_summary_processing_result_transaction(
        exact_transaction,
        exact_ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
        capture_finalization=("generation-a", "owner-a", "claim-a"),
    )
    assert exact["status"] == "committed"
    assert len(exact_transaction.updates) == 1

    for stale_tuple in (
        ("generation-b", "owner-a", "claim-a"),
        ("generation-a", "owner-b", "claim-a"),
        ("generation-a", "owner-a", "claim-b"),
    ):
        stale_ref = _ConversationRef(copy.deepcopy(durable))
        stale_transaction = _Transaction()
        stale = conversations._commit_stock_summary_processing_result_transaction(
            stale_transaction,
            stale_ref,
            "uid-1",
            _stock_processing_payload(),
            expected_active_summary_version_id=None,
            capture_finalization=stale_tuple,
        )
        assert stale["status"] == conversations.capture_finalization_lost
        assert stale_transaction.updates == []

    expired_ref = _ConversationRef(
        {
            **durable,
            "capture_finalization_lease_expires_at": now - timedelta(seconds=1),
        }
    )
    expired_transaction = _Transaction()
    expired = conversations._commit_stock_summary_processing_result_transaction(
        expired_transaction,
        expired_ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
        capture_finalization=("generation-a", "owner-a", "claim-a"),
    )
    assert expired["status"] == conversations.capture_finalization_lost
    assert expired_transaction.updates == []


def test_capture_summary_commit_replay_after_lost_receipt_keeps_one_deterministic_version(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    now = datetime.now(timezone.utc)
    conversation_ref = _ConversationRef(
        {
            "id": "conversation-1",
            "created_at": "2026-07-22T00:00:00+00:00",
            "structured": {},
            "summary_versions": [],
            "active_summary_version_id": None,
            "status": "processing",
            "discarded": False,
            "data_protection_level": "standard",
            "capture_protocol_version": 2,
            "capture_generation": "generation-a",
            "capture_owner_token": "owner-a",
            "capture_state": "finalizing",
            "capture_finalization_claim_token": "claim-a",
            "capture_finalization_lease_expires_at": now + timedelta(seconds=30),
        }
    )
    exact_tuple = ("generation-a", "owner-a", "claim-a")

    first = conversations._commit_stock_summary_processing_result_transaction(
        _Transaction(),
        conversation_ref,
        "uid-1",
        _stock_processing_payload(),
        expected_active_summary_version_id=None,
        capture_finalization=exact_tuple,
    )
    first_version_id = first["active_summary_version_id"]
    assert len(conversation_ref.data["summary_versions"]) == 1

    replay = conversations._commit_stock_summary_processing_result_transaction(
        _Transaction(),
        conversation_ref,
        "uid-1",
        _stock_processing_payload(active_summary_version_id=first_version_id),
        expected_active_summary_version_id=first_version_id,
        capture_finalization=exact_tuple,
    )

    assert replay["status"] == "committed"
    assert replay["active_summary_version_id"] == first_version_id
    assert conversation_ref.data["active_summary_version_id"] == first_version_id
    assert [version["id"] for version in conversation_ref.data["summary_versions"]] == [first_version_id]


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

    data_protection_redis = conversations.commit_stock_summary_processing_result.__globals__["redis_db"]
    monkeypatch.setattr(data_protection_redis, "get_user_data_protection_level", lambda uid: "enhanced")
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

    assert result["status"] == "processing_claimed"
    assert result["claim_token"]
    assert len(transaction.updates) == 1
    updated_ref, update = transaction.updates[0]
    assert updated_ref is ref
    assert update["status"] == "processing"
    assert isinstance(update["initial_processing_claimed_at"], datetime)
    assert update["initial_processing_claimed_at"].tzinfo is not None
    assert update["initial_processing_claim_token"] == result["claim_token"]
    assert update["initial_processing_release_token"] is None
    assert ref.data["transcript_segments"] == [{"text": "durable capture"}]


def test_released_processing_claim_fences_terminal_failure_from_new_claimant(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "status": "processing",
            "initial_processing_claimed_at": datetime.now(timezone.utc),
            "initial_processing_claim_token": "claim-a",
        }
    )
    release_transaction = _Transaction()

    released = conversations._release_initial_conversation_processing_claim_transaction(
        release_transaction,
        ref,
        "claim-a",
        "release-a",
    )

    assert released == {"released": True, "reason": "released", "release_token": "release-a"}
    assert ref.data["status"] == "in_progress"
    assert ref.data["initial_processing_release_token"] == "release-a"

    new_claim_transaction = _Transaction()
    claimed = conversations._claim_initial_conversation_processing_transaction(
        new_claim_transaction,
        ref,
        "claim-b",
    )
    assert claimed["status"] == "processing_claimed"

    failure_transaction = _Transaction()
    failed = conversations._mark_conversation_processing_failed_if_released_transaction(
        failure_transaction,
        ref,
        "conversation_processing_failed",
        datetime.now(timezone.utc),
        "release-a",
    )

    assert failed == {"updated": False, "reason": "processing_authority_changed"}
    assert failure_transaction.updates == []
    assert ref.data["status"] == "processing"
    assert ref.data["initial_processing_claim_token"] == "claim-b"


def test_terminal_failure_updates_only_the_exact_released_generation(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    ref = _ConversationRef(
        {
            "id": "conversation-1",
            "status": "in_progress",
            "initial_processing_claimed_at": None,
            "initial_processing_claim_token": None,
            "initial_processing_release_token": "release-a",
            "transcript_segments": [{"text": "retained"}],
        }
    )
    transaction = _Transaction()

    result = conversations._mark_conversation_processing_failed_if_released_transaction(
        transaction,
        ref,
        "conversation_processing_failed",
        datetime.now(timezone.utc),
        "release-a",
    )

    assert result["updated"] is True
    assert ref.data["status"] == "failed"
    assert ref.data["initial_processing_release_token"] is None
    assert ref.data["transcript_segments"] == [{"text": "retained"}]


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


def test_conversation_builder_reads_and_writes_with_the_same_transaction(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)

    class ConversationRef:
        def __init__(self, exists=True):
            self.exists = exists
            self.read_transactions = []

        def get(self, transaction=None):
            self.read_transactions.append(transaction)
            return SimpleNamespace(
                exists=self.exists,
                to_dict=lambda: {"structured": {"title": "current"}, "data_protection_level": "standard"},
            )

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    ref = ConversationRef()
    transaction = Transaction()

    result = conversations._update_conversation_with_builder_transaction(
        transaction,
        ref,
        "uid-1",
        lambda current: (
            {"structured.title": "winner"},
            {"previous_title": current["structured"]["title"]},
        ),
    )

    assert ref.read_transactions == [transaction]
    assert transaction.updates == [(ref, {"structured.title": "winner"})]
    assert result["result"] == {"previous_title": "current"}

    missing_ref = ConversationRef(exists=False)
    missing_transaction = Transaction()
    missing = conversations._update_conversation_with_builder_transaction(
        missing_transaction,
        missing_ref,
        "uid-1",
        lambda _current: (_ for _ in ()).throw(AssertionError("builder must not run")),
    )
    assert missing is None
    assert missing_ref.read_transactions == [missing_transaction]
    assert missing_transaction.updates == []


def test_conversation_builder_commits_correction_audit_in_the_same_transaction(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)

    correction_ref = object()

    class CorrectionCollection:
        def document(self, correction_id):
            assert correction_id == "correction-a"
            return correction_ref

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: {"data_protection_level": "standard"})

        def collection(self, name):
            assert name == "corrections"
            return CorrectionCollection()

    class Transaction:
        def __init__(self):
            self.updates = []
            self.sets = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def set(self, ref, payload, merge=False):
            self.sets.append((ref, payload, merge))

    ref = ConversationRef()
    transaction = Transaction()
    result = conversations._update_conversation_with_builder_transaction(
        transaction,
        ref,
        "uid-1",
        lambda _current: (
            {"structured.title": "winner"},
            {"correction_audit": {"status": "applied", "applied_summary_version_id": "winner-v2"}},
        ),
        correction_id="correction-a",
    )

    assert result["result"]["correction_audit"]["status"] == "applied"
    assert transaction.updates == [(ref, {"structured.title": "winner"})]
    assert transaction.sets == [
        (correction_ref, {"status": "applied", "applied_summary_version_id": "winner-v2"}, True)
    ]


def test_correction_audit_enqueue_failure_aborts_the_conversation_transaction(monkeypatch):
    conversations = _load_conversations_module(monkeypatch)
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)

    class CorrectionCollection:
        def document(self, _correction_id):
            return object()

    class ConversationRef:
        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: {"data_protection_level": "standard"})

        def collection(self, _name):
            return CorrectionCollection()

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def set(self, _ref, _payload, merge=False):
            raise RuntimeError("synthetic correction audit enqueue failure")

    transaction = Transaction()
    with pytest.raises(RuntimeError, match="correction audit enqueue failure"):
        conversations._update_conversation_with_builder_transaction(
            transaction,
            ConversationRef(),
            "uid-1",
            lambda _current: (
                {"structured.title": "must-not-commit"},
                {"correction_audit": {"status": "applied"}},
            ),
            correction_id="correction-a",
        )

    # The real Firestore transaction never commits queued writes when its body raises.
    assert len(transaction.updates) == 1


def _load_emulator_modules(monkeypatch):
    from google.cloud import firestore

    import utils
    import utils.other

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "omi-ci")
    client = firestore.Client(project=project)
    database_client = SimpleNamespace(db=client)
    monkeypatch.setitem(sys.modules, "database._client", database_client)
    monkeypatch.setitem(sys.modules, "database.users", MagicMock())
    monkeypatch.setitem(sys.modules, "database.redis_db", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.encryption", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.other.hume", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.other.storage", MagicMock(list_audio_chunks=MagicMock(return_value=[])))

    backend = Path(__file__).resolve().parents[2]
    conversations_path = backend / "database" / "conversations.py"
    conversations_spec = importlib.util.spec_from_file_location(
        "database.conversations_summary_cas_emulator_test", conversations_path
    )
    conversations = importlib.util.module_from_spec(conversations_spec)
    assert conversations_spec is not None and conversations_spec.loader is not None
    conversations_spec.loader.exec_module(conversations)
    monkeypatch.setitem(sys.modules, "database.conversations", conversations)

    writeback_path = backend / "ella" / "services" / "summary_writeback.py"
    writeback_spec = importlib.util.spec_from_file_location(
        "ella.services.summary_writeback_emulator_test", writeback_path
    )
    writeback = importlib.util.module_from_spec(writeback_spec)
    assert writeback_spec is not None and writeback_spec.loader is not None
    writeback_spec.loader.exec_module(writeback)
    return client, conversations, writeback


@pytest.mark.skipif(
    os.environ.get("ELLA_FIRESTORE_EMULATOR_TESTS") != "true",
    reason="requires the hosted Firestore emulator gate",
)
def test_real_firestore_transaction_contention_preserves_one_complete_canonical_source(monkeypatch):
    client, _conversations, writeback = _load_emulator_modules(monkeypatch)
    uid = f"summary-cas-{uuid.uuid4()}"
    conversation_id = "conversation-a"
    conversation_ref = client.collection("users").document(uid).collection("conversations").document(conversation_id)
    original = {
        "started_at": datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
        "created_at": datetime(2026, 8, 15, 11, 59, tzinfo=timezone.utc),
        "structured": {
            "title": "Original",
            "overview": "Original overview",
            "emoji": "🪽",
            "category": "other",
        },
        "transcript_segments": [{"speaker": "Other", "text": "private transcript"}],
        "summary_versions": [{"id": "original-v1", "is_active": True}],
        "active_summary_version_id": "original-v1",
        "source": "omi",
        "status": "completed",
        "data_protection_level": "standard",
    }
    conversation_ref.set(copy.deepcopy(original))
    expected = writeback.canonical_source_sha256(
        writeback.canonical_source_from_conversation(
            uid=uid,
            conversation_id=conversation_id,
            conversation=original,
        )
    )
    barrier = threading.Barrier(2)
    boundary_lock = threading.Lock()
    first_read_id = [None]
    first_commit_id = [None]
    transactional_read_ids = []
    first_commit_waiting = threading.Event()
    overlapping_read_started = threading.Event()
    first_commit_rpc_started = threading.Event()
    first_commit_completed = threading.Event()
    transaction_type = type(client.transaction())
    original_commit = transaction_type._commit
    original_batch_get_documents = client._firestore_api.batch_get_documents

    def observed_batch_get_documents(*args, **kwargs):
        request = kwargs.get("request") or args[0]
        transaction_id = request.get("transaction") if isinstance(request, dict) else request.transaction
        transaction_id = bytes(transaction_id or b"")
        wait_for_first_commit = False
        if transaction_id:
            with boundary_lock:
                transactional_read_ids.append(transaction_id)
                if first_read_id[0] is None:
                    first_read_id[0] = transaction_id
                elif transaction_id != first_read_id[0] and not overlapping_read_started.is_set():
                    assert not first_commit_rpc_started.is_set()
                    overlapping_read_started.set()
                    wait_for_first_commit = True
        if wait_for_first_commit and not first_commit_completed.wait(timeout=10):
            raise AssertionError("first transaction did not complete its Firestore commit")
        return original_batch_get_documents(*args, **kwargs)

    def coordinated_commit(transaction):
        transaction_id = bytes(transaction._id or b"")
        coordinate = False
        with boundary_lock:
            if first_commit_id[0] is None:
                assert transaction_id == first_read_id[0]
                first_commit_id[0] = transaction_id
                coordinate = True
                first_commit_waiting.set()
                if any(read_id != transaction_id for read_id in transactional_read_ids):
                    overlapping_read_started.set()
        if coordinate:
            if not overlapping_read_started.wait(timeout=10):
                raise AssertionError("second transaction did not reach the Firestore read boundary")
            first_commit_rpc_started.set()
            try:
                return original_commit(transaction)
            finally:
                first_commit_completed.set()
        return original_commit(transaction)

    monkeypatch.setattr(client._firestore_api, "batch_get_documents", observed_batch_get_documents)
    monkeypatch.setattr(transaction_type, "_commit", coordinated_commit)
    publication_lock = threading.Lock()
    canonical_publications = []
    contenders = {
        "A": {
            "marker": "contender-alpha-only",
            "emoji": "🅰️",
            "category": "personal",
            "tags": ["winner_alpha"],
            "signal": {"winner": "alpha"},
            "assessment": {"risk_level": "alpha"},
        },
        "B": {
            "marker": "contender-bravo-only",
            "emoji": "🅱️",
            "category": "work",
            "tags": ["winner_bravo"],
            "signal": {"winner": "bravo"},
            "assessment": {"risk_level": "bravo"},
        },
    }

    def publish(_uid, current, **kwargs):
        with publication_lock:
            canonical_publications.append(
                {
                    "structured": copy.deepcopy(current["structured"]),
                    "transcript_segments": copy.deepcopy(current["transcript_segments"]),
                    "started_at": current["started_at"],
                    "finished_at": current["finished_at"],
                    "created_at": current["created_at"],
                    "summary_versions": copy.deepcopy(current["summary_versions"]),
                    "active_summary_version_id": current["active_summary_version_id"],
                    "enrichment_state": copy.deepcopy(current["enrichment_state"]),
                    "internal_assessment": copy.deepcopy(current["internal_assessment"]),
                    "ella_tags": copy.deepcopy(current["ella_tags"]),
                    "ella_signal": copy.deepcopy(current["ella_signal"]),
                    "source": current["source"],
                    "status": current["status"],
                    "summary_source": kwargs["summary_source"],
                    "summary_kind": kwargs["summary_kind"],
                    "trace_id": kwargs["trace_id"],
                }
            )
        return {"ok": True}

    def contender(label):
        contender_data = contenders[label]

        async def fetch_assessment(_uid, _conversation_id):
            return contender_data["assessment"]

        # Start both requests together. The client-boundary instrumentation above
        # pauses the first commit only until the other transactional read RPC has
        # started, avoiding a barrier after either lock-holding read completes.
        barrier.wait(timeout=10)
        return asyncio.run(
            writeback.write_conversation_summary_cas(
                uid=uid,
                conversation_id=conversation_id,
                expected_canonical_source_sha256=expected,
                operation_token=label * 64,
                source_version="2026-08-15 12:01:00+00:00",
                payload_sha256=("a" if label == "A" else "b") * 64,
                title=f"Winner {label}",
                overview=f"[Ella] Complete winning overview {contender_data['marker']} with stable source data.",
                emoji=contender_data["emoji"],
                category=contender_data["category"],
                summary_source=f"hermes_parallel_{label.lower()}",
                summary_kind=f"hermes_enriched_{label.lower()}",
                correction_id=f"correction-{label}",
                trace_id=f"trace-{label}",
                ella_tags=contender_data["tags"],
                ella_signal=contender_data["signal"],
                internal_assessment_fetcher=fetch_assessment,
                canonical_writer=publish,
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(contender, label) for label in ("A", "B")]
            outcomes = []
            errors = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=20))
                except Exception as error:
                    errors.append(error)

        assert len(outcomes) == 1, [repr(error) for error in errors]
        assert outcomes[0]["status"] == "completed"
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            (
                writeback.CanonicalConversationSourceMismatchError,
                writeback.CanonicalSummaryReconciliationPendingError,
            ),
        )
        assert len(canonical_publications) == 1
        assert first_commit_waiting.is_set()
        assert overlapping_read_started.is_set()
        assert first_commit_rpc_started.is_set()
        assert len(set(transactional_read_ids)) >= 2

        stored = conversation_ref.get().to_dict()
        winning_title = stored["structured"]["title"]
        winning_overview = stored["structured"]["overview"]
        assert winning_title in {"Winner A", "Winner B"}
        assert stored["transcript_segments"] == original["transcript_segments"]
        assert stored["started_at"] == original["started_at"]
        assert stored["finished_at"] == original["finished_at"]
        assert len(stored["transcript_segments"]) == 1
        assert len(stored["summary_versions"]) == 2
        active = next(version for version in stored["summary_versions"] if version["is_active"])
        assert active["id"] == stored["active_summary_version_id"]
        assert active["title"] == winning_title
        assert active["overview"] == winning_overview
        assert stored["summary_writeback_receipt"]["active_summary_version_id"] == active["id"]
        winning_label = winning_title.rsplit(" ", 1)[1]
        corrections = list(conversation_ref.collection("corrections").stream())
        assert len(corrections) == 1
        assert corrections[0].id == f"correction-{winning_label}"
        assert corrections[0].to_dict()["applied_summary_version_id"] == active["id"]
        winner = contenders[winning_label]
        loser_label = "B" if winning_label == "A" else "A"
        publication = canonical_publications[0]
        assert publication["structured"] == {
            "title": f"Winner {winning_label}",
            "overview": f"[Ella] Complete winning overview {winner['marker']} with stable source data.",
            "emoji": winner["emoji"],
            "category": winner["category"],
        }
        assert publication["transcript_segments"] == original["transcript_segments"]
        assert publication["started_at"] == original["started_at"]
        assert publication["finished_at"] == original["finished_at"]
        assert publication["created_at"] == original["created_at"]
        assert publication["summary_versions"] == stored["summary_versions"]
        assert publication["active_summary_version_id"] == stored["active_summary_version_id"]
        assert publication["enrichment_state"] == stored["enrichment_state"]
        assert publication["internal_assessment"] == winner["assessment"]
        assert publication["ella_tags"] == winner["tags"]
        assert publication["ella_signal"] == winner["signal"]
        assert publication["source"] == original["source"]
        assert publication["status"] == original["status"]
        assert publication["summary_source"] == f"hermes_parallel_{winning_label.lower()}"
        assert publication["summary_kind"] == f"hermes_enriched_{winning_label.lower()}"
        assert publication["trace_id"] == f"trace-{winning_label}"
        assert sum(item["trace_id"] == f"trace-{winning_label}" for item in canonical_publications) == 1
        assert sum(item["trace_id"] == f"trace-{loser_label}" for item in canonical_publications) == 0
        assert contenders[loser_label]["marker"] not in json.dumps(publication, default=str)
    finally:
        conversation_ref.delete()
