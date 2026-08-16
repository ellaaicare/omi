import asyncio
import copy
import importlib.util
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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
