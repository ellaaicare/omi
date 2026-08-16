import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


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
