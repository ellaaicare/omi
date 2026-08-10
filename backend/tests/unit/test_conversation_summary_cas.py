import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from utils.ella.canonical_omi import transcript_grounding_hash


def _load_conversations_module(monkeypatch):
    import utils
    import utils.other

    monkeypatch.setitem(sys.modules, "database._client", MagicMock(db=MagicMock()))
    monkeypatch.setitem(sys.modules, "database.users", MagicMock())
    monkeypatch.setitem(sys.modules, "database.redis_db", MagicMock())
    monkeypatch.setitem(sys.modules, "utils.encryption", MagicMock())
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
