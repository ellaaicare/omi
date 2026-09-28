"""Unit tests that execute the REAL state-machine functions in `database/sync_segments.py` --
claim/reserve, complete, release, and the append-to-existing-conversation transaction -- against a
small in-memory fake Firestore transaction and document objects, not `test_ella_sync_v2_contract
.py`'s own route-level fakes (`FakeSyncSegmentStore`).

That contract test stubs `database.sync_segments` out of `sys.modules` entirely (a `MagicMock`), so
it proves the *route*'s claim -> process -> complete/release call sequence is correct, but never
actually runs a single line of this module's own transaction logic. This file closes that gap: it
imports the real module and calls its private `_..._transaction` functions directly (the same
functions the `@transactional`-wrapped public API delegates to) with fake `transaction`/document-
reference objects standing in for `google.cloud.firestore_v1`'s real ones. Firestore's own
retry-under-contention machinery is out of scope here (same boundary the contract test draws for
Redis, and the boundary an emulator-backed suite elsewhere in this repo would cover) -- what these
tests prove is this module's own *decision logic* per state transition:

  * reservation: claiming always stores the same deterministic `reserved_new_conversation_id`.
  * retry-after-commit (SYNC-V2-002): if an earlier attempt's `process_conversation` already
    durably created the reserved conversation but this segment's own completion write never
    landed, a (re)claim resumes that result instead of handing back 'claimed' -- whether the prior
    claimant's lease simply expired, or it was explicitly released after a failure.
  * stale-releaser-cannot-stomp-a-successor: a release from a claimant that no longer holds the
    claim (already reclaimed by someone else) is a no-op, never erasing the new claimant's lease.
  * complete/append: the receipt only advances to 'done' for the claimant that actually holds it,
    and the append-to-existing-conversation transaction commits the merge and the receipt together.

Import-safety: `database.sync_segments` itself only needs `google.cloud.firestore_v1` (a real,
lightweight import -- no credentials touched merely by importing the `transactional` decorator) and
`database._client` / `database.conversations`, both stubbed below *before* the real module is
imported so this file never touches Firestore, GCS, or (transitively, via the real
`database.conversations`) the `redis` package this focused CI job does not install.
"""

import copy
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# Force our own stubs for `database._client`/`database.conversations` (never `setdefault`: this
# file needs the REAL `database.sync_segments`, so its transitive imports must be *these* stubs,
# not whatever another test module -- e.g. `test_ella_sync_v2_contract.py`, which stubs
# `database.sync_segments` itself as a bare `MagicMock` -- happened to leave in `sys.modules` if it
# was collected first in the same pytest session). Also drop any cached `database.sync_segments`
# so this import always re-executes the real module against these stubs, regardless of import
# order across test files.
for _module_name in ("database._client", "database.conversations"):
    sys.modules[_module_name] = MagicMock(db=MagicMock(), conversations_collection="conversations")
sys.modules.pop("database.sync_segments", None)

import database.sync_segments as sync_segments

UID = "uid-state-machine"
SEGMENT_ID = "segment-abc"
T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    """Minimal stand-in for a `google.cloud.firestore_v1.DocumentReference`: identity is its key
    in the shared [store] dict, `.get(transaction=...)` reads the current value, and reads/writes
    always go through the same backing dict a `_FakeTransaction` writes to -- close enough to
    Firestore's real read-your-writes-within-a-transaction semantics for these single-threaded
    tests (this module's own docstring already draws the line at not modeling cross-transaction
    contention/retry, which needs a real emulator)."""

    def __init__(self, store: dict, key: str):
        self._store = store
        self.id = key
        self._key = key

    def get(self, transaction=None):
        return _FakeSnapshot(self._store.get(self._key))


class _FakeTransaction:
    def __init__(self):
        pass

    def set(self, ref: _FakeDocRef, data: dict, merge: bool = False):
        if merge and ref._key in ref._store:
            ref._store[ref._key] = {**ref._store[ref._key], **copy.deepcopy(data)}
        else:
            ref._store[ref._key] = copy.deepcopy(data)


def _refs():
    """Fresh, independent backing stores for the segment doc and the conversation doc -- exactly
    the two different Firestore collections the real functions read/write."""
    segments_store: dict = {}
    conversations_store: dict = {}
    segment_ref = _FakeDocRef(segments_store, SEGMENT_ID)
    conversation_ref = _FakeDocRef(conversations_store, "reserved-conv-id")
    return segments_store, conversations_store, segment_ref, conversation_ref


# **********************************************
# ******************* RESERVATION ***************
# **********************************************


def test_reserved_new_conversation_id_is_deterministic_and_namespaced():
    first = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    second = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    assert first == second
    # A valid uuid, not just an opaque string.
    assert str(uuid.UUID(first)) == first
    # Different (uid, segment_id) never collide onto the same reservation.
    assert sync_segments.reserved_new_conversation_id("other-uid", SEGMENT_ID) != first
    assert sync_segments.reserved_new_conversation_id(UID, "other-segment") != first


def test_claim_transaction_reserves_the_deterministic_id_on_a_fresh_claim():
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)

    result = sync_segments._claim_sync_segment_transaction(
        _FakeTransaction(), segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )

    assert result == {"outcome": "claimed", "claimant": "claimant-a", "reserved_conversation_id": reserved_id}
    assert segments_store[SEGMENT_ID]["reserved_conversation_id"] == reserved_id
    assert segments_store[SEGMENT_ID]["state"] == "processing"


# **********************************************
# ************ RETRY-AFTER-COMMIT (SYNC-V2-002) *
# **********************************************


def test_claim_resumes_reserved_conversation_after_release_following_a_completion_failure():
    """The exact round-3 bug: `process_conversation` durably committed the reserved conversation,
    but the completion write after it failed and the segment was released as failed. A retry must
    resume the durable conversation, not reclaim and re-run the pipeline."""
    segments_store, conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()

    first_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )
    assert first_claim["outcome"] == "claimed"

    # `process_conversation` commits the conversation directly (outside this module, in the real
    # design) -- simulate that external durable write.
    conversations_store[conversation_ref.id] = {"id": reserved_id, "structured": {"title": "..."}}

    # The completion write fails; the caller releases the claim (as `routers/sync.py` does on any
    # exception from `process_segment`).
    release_result = sync_segments._release_sync_segment_transaction(transaction, segment_ref, "claimant-a", T0)
    assert release_result == {"outcome": "released"}
    assert segments_store[SEGMENT_ID]["state"] == "failed"

    # A retry (fresh claimant) must resume the durable conversation, never re-claim.
    retry = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-b", T0 + timedelta(seconds=1), 60
    )

    assert retry == {"outcome": "done", "kind": "new_memories", "conversation_id": reserved_id}
    assert segments_store[SEGMENT_ID]["state"] == "done"
    assert segments_store[SEGMENT_ID]["conversation_id"] == reserved_id


def test_claim_resumes_reserved_conversation_after_lease_expiry_with_a_new_claimant():
    """Variant of the same bug where the first claimant never got to release at all (crashed
    outright) -- the segment is stuck 'processing' with a now-expired lease. A successor claiming
    after expiry must still resume the durable conversation instead of reclaiming and re-running
    the pipeline a second time."""
    segments_store, conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()
    lease_seconds = 60

    first_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, lease_seconds
    )
    assert first_claim["outcome"] == "claimed"

    # claimant-a's pipeline got far enough to durably commit the conversation, then died --
    # never released, never completed.
    conversations_store[conversation_ref.id] = {"id": reserved_id, "structured": {"title": "..."}}

    # A successor claims well after the lease window has elapsed.
    after_expiry = T0 + timedelta(seconds=lease_seconds + 1)
    successor_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-b", after_expiry, lease_seconds
    )

    assert successor_claim == {"outcome": "done", "kind": "new_memories", "conversation_id": reserved_id}
    assert segments_store[SEGMENT_ID]["state"] == "done"


def test_claim_still_reclaims_normally_when_no_conversation_was_ever_committed():
    """The counterpart proving the new check doesn't over-fire: if the prior attempt died before
    `process_conversation` ever committed anything, a retry must reclaim and run the pipeline
    normally -- there is nothing durable yet to resume."""
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()

    first_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )
    assert first_claim["outcome"] == "claimed"
    sync_segments._release_sync_segment_transaction(transaction, segment_ref, "claimant-a", T0)

    retry = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-b", T0 + timedelta(seconds=1), 60
    )
    assert retry == {"outcome": "claimed", "claimant": "claimant-b", "reserved_conversation_id": reserved_id}
    assert segments_store[SEGMENT_ID]["state"] == "processing"


# **********************************************
# ******* STALE-RELEASER-CANNOT-STOMP-A-SUCCESSOR *
# **********************************************


def test_release_cannot_stomp_a_successors_claim():
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()

    sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )
    sync_segments._release_sync_segment_transaction(transaction, segment_ref, "claimant-a", T0)

    second_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-b", T0 + timedelta(seconds=1), 60
    )
    assert second_claim["outcome"] == "claimed"

    # A late/stale release from claimant-a (e.g. a delayed retry of its own cleanup after already
    # losing the claim) must not touch claimant-b's now-live claim.
    stale_release = sync_segments._release_sync_segment_transaction(
        transaction, segment_ref, "claimant-a", T0 + timedelta(seconds=2)
    )
    assert stale_release == {"outcome": "lost"}
    assert segments_store[SEGMENT_ID]["state"] == "processing"
    assert segments_store[SEGMENT_ID]["claimant"] == "claimant-b"

    # claimant-b's own claim is still intact -- a fresh claim attempt for anyone else sees 'busy',
    # not a re-claimable slot.
    third_claim = sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-c", T0 + timedelta(seconds=3), 60
    )
    assert third_claim == {"outcome": "busy"}


# **********************************************
# ******************* COMPLETE ******************
# **********************************************


def test_complete_new_conversation_transaction_records_receipt_for_the_current_claimant():
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()
    sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )

    result = sync_segments._complete_new_conversation_transaction(
        transaction, segment_ref, "claimant-a", reserved_id, T0 + timedelta(seconds=1)
    )

    assert result == {"outcome": "done", "kind": "new_memories", "conversation_id": reserved_id}
    assert segments_store[SEGMENT_ID]["state"] == "done"

    # Idempotent replay: completing again (e.g. a duplicated call) returns the same receipt.
    replay = sync_segments._complete_new_conversation_transaction(
        transaction, segment_ref, "claimant-a", reserved_id, T0 + timedelta(seconds=2)
    )
    assert replay == {"outcome": "done", "kind": "new_memories", "conversation_id": reserved_id}


def test_complete_new_conversation_transaction_rejects_a_claimant_that_lost_the_claim():
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    reserved_id = sync_segments.reserved_new_conversation_id(UID, SEGMENT_ID)
    transaction = _FakeTransaction()
    sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-a", T0, 60
    )
    sync_segments._release_sync_segment_transaction(transaction, segment_ref, "claimant-a", T0)
    sync_segments._claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_id, "claimant-b", T0 + timedelta(seconds=1), 60
    )

    # claimant-a's stale completion (e.g. a slow request finally landing after it was already
    # reclaimed) must not overwrite claimant-b's now-live processing state.
    stale_complete = sync_segments._complete_new_conversation_transaction(
        transaction, segment_ref, "claimant-a", reserved_id, T0 + timedelta(seconds=2)
    )
    assert stale_complete == {"outcome": "lost"}
    assert segments_store[SEGMENT_ID]["state"] == "processing"
    assert segments_store[SEGMENT_ID]["claimant"] == "claimant-b"


# **********************************************
# ******* APPEND TO EXISTING CONVERSATION ********
# **********************************************


def test_append_and_complete_transaction_commits_merge_and_receipt_together(monkeypatch):
    segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    merge_calls = []

    def fake_merge(transaction, conv_ref, uid, new_segments, segment_timestamp):
        merge_calls.append((conv_ref.id, uid, new_segments, segment_timestamp))
        return {"status": "committed", "discarded": False}

    monkeypatch.setattr(sync_segments, "merge_transcript_segments_into_conversation_transaction", fake_merge)

    result = sync_segments._append_and_complete_transaction(
        _FakeTransaction(),
        segment_ref,
        conversation_ref,
        UID,
        "claimant-a",
        [{"text": "hi", "start": 0.0, "end": 1.0}],
        1735689600.0,
        T0,
    )

    assert result == {
        "outcome": "done",
        "kind": "updated_memories",
        "conversation_id": conversation_ref.id,
        "discarded": False,
    }
    assert len(merge_calls) == 1
    assert segments_store[SEGMENT_ID]["state"] == "done"
    assert segments_store[SEGMENT_ID]["conversation_id"] == conversation_ref.id

    # Idempotent replay: the merge is never re-run once the receipt is durably 'done'.
    replay = sync_segments._append_and_complete_transaction(
        _FakeTransaction(),
        segment_ref,
        conversation_ref,
        UID,
        "claimant-a",
        [{"text": "hi again", "start": 0.0, "end": 1.0}],
        1735689600.0,
        T0 + timedelta(seconds=1),
    )
    assert replay["outcome"] == "done"
    assert len(merge_calls) == 1  # unchanged


def test_append_and_complete_transaction_reports_conversation_missing(monkeypatch):
    _segments_store, _conversations_store, segment_ref, conversation_ref = _refs()
    monkeypatch.setattr(
        sync_segments,
        "merge_transcript_segments_into_conversation_transaction",
        lambda *_args, **_kwargs: {"status": "missing"},
    )

    result = sync_segments._append_and_complete_transaction(
        _FakeTransaction(), segment_ref, conversation_ref, UID, "claimant-a", [], 1735689600.0, T0
    )

    assert result == {"outcome": "conversation_missing"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
