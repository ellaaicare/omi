import importlib.util
import json
import logging
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from utils.ella.canonical_omi import transcript_grounding_hash


def _load_conversations_module(monkeypatch):
    import utils
    import utils.other

    encryption = MagicMock()
    monkeypatch.setitem(sys.modules, "database._client", MagicMock(db=MagicMock()))
    monkeypatch.setitem(sys.modules, "database.users", MagicMock())
    monkeypatch.setitem(sys.modules, "database.redis_db", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.encryption", encryption)
    monkeypatch.setattr(utils, "encryption", encryption, raising=False)
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
