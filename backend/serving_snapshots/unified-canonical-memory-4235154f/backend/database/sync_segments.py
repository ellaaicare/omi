"""Durable, Firestore-backed idempotency for `/v2/sync-local-files` VAD segments.

Replaces the prior design (Redis `SET NX EX` claim/lease for exclusivity, plus a separate Redis
cache entry as the idempotency record -- see PR #594's review history, SYNC-V2-001/002/003).
That design had two structural problems:

  * Redis was the *source of truth* for "was this segment already processed" -- a Redis outage or
    restart could silently lose that record, and there was no way to distinguish "never
    processed" from "processed, but we lost the receipt".
  * The claim, the actual conversation write, and the cache write were three separate operations.
    A failure between the write and the cache write (a real scenario the round-2 review
    reproduced: persistence succeeds, then the cache write fails) left the conversation durably
    updated but with no idempotency record, so a retry could not tell it had already happened and
    reprocessed -- duplicating the segment.

Here, the idempotency record is a per-user Firestore document (`sync_v2_segments/{segment_id}`)
with an explicit state machine (processing/done/failed) and a lease expiry for claimant death,
following the same pattern this codebase already uses for other retry-safe side effects (see
`database/task_sync.py`). For the "append to an existing conversation" outcome specifically,
`append_segment_to_conversation_and_complete` writes the idempotency record in the *same*
Firestore transaction as the conversation's segment merge, so the two can never succeed or fail
independently -- structurally closing the exact bug above, rather than just handling it after the
fact. Creating a *new* conversation goes through `process_conversation`, a complex, non-Firestore
-transactional pipeline (LLM structuring, memory extraction, etc.) that can't be folded into a
Firestore transaction; `complete_new_conversation_sync_segment` durably records that outcome
immediately afterward instead, the same trade-off `database/task_sync.py` makes for other
external side effects it can't make transactional either. The claim below still guarantees only
one execution reaches that point per lease window.

Redis may remain in this codebase as an optional read-through cache elsewhere, but it is never the
source of truth for whether a sync segment was already processed.

**New-conversation retries (SYNC-V2-002, round 3):** creating a brand-new conversation still can't
be folded into the segment's own Firestore transaction -- `process_conversation` is a whole
LLM/STT pipeline, not a Firestore write. What closes the gap instead is a *deterministic* target:
`reserved_new_conversation_id(uid, segment_id)` always returns the same id for the same segment, so
it is reserved into the claim record *before* the pipeline ever runs (`claim_or_get_sync_segment`).
If a later attempt (a retry after the completion write failed, or a successor claiming after the
original claimant's lease expired) finds a conversation already durably sitting at that exact id,
it means an earlier attempt's pipeline already committed it -- so `claim_or_get_sync_segment`
short-circuits straight to `'done'` with that id, and the caller never re-runs STT/LLM/persistence.
`process_conversation` itself (see `utils/conversations/process_conversation.py`) is also made
idempotent for an explicit id as a second, independent line of defense.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from google.cloud.firestore_v1 import transactional

from ._client import db
from .conversations import conversations_collection, merge_transcript_segments_into_conversation_transaction

SYNC_SEGMENTS_COLLECTION = 'sync_v2_segments'

# Bounds how long a crashed/hung claimant can block a legitimate retry of the same segment.
# Generous relative to one segment's STT + LLM + persistence latency, short relative to a human
# noticing a stuck upload.
SEGMENT_LEASE_SECONDS = 15 * 60

# Fixed, never-changing namespace for the UUIDv5 conversation ids `reserved_new_conversation_id`
# derives below. Changing this constant would change every future reservation's id -- it must
# stay fixed for the lifetime of this feature.
_RESERVED_CONVERSATION_ID_NAMESPACE = uuid.UUID('c9f6b1d2-5b8e-4c3a-9d7b-2f6a3b8e1c0d')


def reserved_new_conversation_id(uid: str, segment_id: str) -> str:
    """Deterministic id for the conversation a sync-v2 segment would create *if* it turns out to
    start a brand new conversation (never used for the append-to-existing-conversation outcome).
    Same (uid, segment_id) always yields the same id, independent of the audio content and of
    which attempt/claimant ultimately runs the pipeline -- so a retry can recognize its own prior
    attempt's conversation purely from (uid, segment_id), before ever touching STT/LLM."""
    return str(uuid.uuid5(_RESERVED_CONVERSATION_ID_NAMESPACE, f'{uid}:{segment_id}'))


def _conversation_ref(uid: str, conversation_id: str):
    return db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)


def _segment_ref(uid: str, segment_id: str):
    return db.collection('users').document(uid).collection(SYNC_SEGMENTS_COLLECTION).document(segment_id)


def _is_future(value: Any, now: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return value > now


def _done_result(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return {'outcome': 'done', 'kind': receipt.get('kind'), 'conversation_id': receipt.get('conversation_id')}


# **********************************************
# ****************** CLAIM **********************
# **********************************************


def _claim_sync_segment_transaction(
    transaction,
    segment_ref,
    conversation_ref,
    reserved_conversation_id: str,
    claimant: str,
    now: datetime,
    lease_seconds: int,
) -> Dict[str, Any]:
    snapshot = segment_ref.get(transaction=transaction)
    if snapshot.exists:
        receipt = snapshot.to_dict() or {}
        if receipt.get('state') == 'done':
            return _done_result(receipt)
        if receipt.get('state') == 'processing' and _is_future(receipt.get('lease_expires_at'), now):
            return {'outcome': 'busy'}
        # 'failed', or 'processing' with an expired lease (claimant died mid-work): before
        # (re)claiming, check whether an earlier attempt already durably created the
        # deterministically-reserved conversation for this exact segment (SYNC-V2-002) -- e.g. its
        # `process_conversation` commit succeeded but the completion write after it failed, or its
        # lease simply expired before it got that far. If so, resume that result instead of
        # re-running STT/LLM/persistence: mark the segment done and hand back the same id.
        reserved_id = receipt.get('reserved_conversation_id') or reserved_conversation_id
        existing_conversation = conversation_ref.get(transaction=transaction)
        if existing_conversation.exists:
            done_update = {
                'state': 'done',
                'kind': 'new_memories',
                'conversation_id': reserved_id,
                'completed_at': now,
                'reserved_conversation_id': reserved_id,
            }
            transaction.set(segment_ref, done_update, merge=True)
            return _done_result(done_update)
    transaction.set(
        segment_ref,
        {
            'state': 'processing',
            'claimant': claimant,
            'claimed_at': now,
            'lease_expires_at': now + timedelta(seconds=lease_seconds),
            'reserved_conversation_id': reserved_conversation_id,
        },
    )
    return {'outcome': 'claimed', 'claimant': claimant, 'reserved_conversation_id': reserved_conversation_id}


@transactional
def _claim_sync_segment(
    transaction, segment_ref, conversation_ref, reserved_conversation_id, claimant, now, lease_seconds
) -> Dict[str, Any]:
    return _claim_sync_segment_transaction(
        transaction, segment_ref, conversation_ref, reserved_conversation_id, claimant, now, lease_seconds
    )


def claim_or_get_sync_segment(
    uid: str,
    segment_id: str,
    claimant: str,
    *,
    lease_seconds: int = SEGMENT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Atomically claim exclusive processing rights for one VAD segment, or return its
    already-durable result if it was processed before.

    Outcomes:
      * 'done' (+ kind/conversation_id) -- this segment was already durably processed (including
        by an attempt whose HTTP response never reached the client, or whose new-conversation
        commit succeeded but whose completion write afterward failed -- see
        `reserved_new_conversation_id`). Report this result; never re-run STT/LLM/persistence.
      * 'busy' -- another execution currently holds the claim; back off as retryable.
      * 'claimed' (+ reserved_conversation_id) -- this claimant may proceed with STT/LLM/persistence
        for this segment id. If the pipeline decides to start a brand new conversation, it must
        create it at exactly [reserved_conversation_id] (see `routers/sync.py::process_segment`).
    """
    now = now or datetime.now(timezone.utc)
    reserved_conversation_id = reserved_new_conversation_id(uid, segment_id)
    return _claim_sync_segment(
        db.transaction(),
        _segment_ref(uid, segment_id),
        _conversation_ref(uid, reserved_conversation_id),
        reserved_conversation_id,
        claimant,
        now,
        lease_seconds,
    )


# **********************************************
# ************** NEW CONVERSATION ***************
# **********************************************


def _complete_new_conversation_transaction(
    transaction, segment_ref, claimant: str, conversation_id: str, now: datetime
) -> Dict[str, Any]:
    snapshot = segment_ref.get(transaction=transaction)
    receipt = snapshot.to_dict() if snapshot.exists else None
    if receipt and receipt.get('state') == 'done':
        return _done_result(receipt)
    if not receipt or receipt.get('state') != 'processing' or receipt.get('claimant') != claimant:
        return {'outcome': 'lost'}
    transaction.set(
        segment_ref,
        {'state': 'done', 'kind': 'new_memories', 'conversation_id': conversation_id, 'completed_at': now},
        merge=True,
    )
    return {'outcome': 'done', 'kind': 'new_memories', 'conversation_id': conversation_id}


@transactional
def _complete_new_conversation_sync_segment(transaction, segment_ref, claimant, conversation_id, now):
    return _complete_new_conversation_transaction(transaction, segment_ref, claimant, conversation_id, now)


def complete_new_conversation_sync_segment(
    uid: str, segment_id: str, claimant: str, conversation_id: str, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Durably record that [segment_id] resulted in newly-created conversation [conversation_id].
    See module docstring for why this is a best-effort step immediately after `process_conversation`
    rather than folded into one transaction with it."""
    now = now or datetime.now(timezone.utc)
    return _complete_new_conversation_sync_segment(
        db.transaction(), _segment_ref(uid, segment_id), claimant, conversation_id, now
    )


# **********************************************
# ********** APPEND TO EXISTING CONVERSATION ****
# **********************************************


def _append_and_complete_transaction(
    transaction,
    segment_ref,
    conversation_ref,
    uid: str,
    claimant: str,
    new_transcript_segments: List[dict],
    segment_timestamp: float,
    now: datetime,
) -> Dict[str, Any]:
    snapshot = segment_ref.get(transaction=transaction)
    receipt = snapshot.to_dict() if snapshot.exists else None
    if receipt and receipt.get('state') == 'done':
        return _done_result(receipt)

    merge_result = merge_transcript_segments_into_conversation_transaction(
        transaction, conversation_ref, uid, new_transcript_segments, segment_timestamp
    )
    if merge_result['status'] == 'missing':
        return {'outcome': 'conversation_missing'}

    transaction.set(
        segment_ref,
        {
            'state': 'done',
            'claimant': claimant,
            'kind': 'updated_memories',
            'conversation_id': conversation_ref.id,
            'completed_at': now,
        },
        merge=True,
    )
    return {
        'outcome': 'done',
        'kind': 'updated_memories',
        'conversation_id': conversation_ref.id,
        'discarded': merge_result['discarded'],
    }


@transactional
def _append_segment_to_conversation_and_complete(
    transaction, segment_ref, conversation_ref, uid, claimant, new_transcript_segments, segment_timestamp, now
):
    return _append_and_complete_transaction(
        transaction, segment_ref, conversation_ref, uid, claimant, new_transcript_segments, segment_timestamp, now
    )


def append_segment_to_conversation_and_complete(
    uid: str,
    segment_id: str,
    claimant: str,
    conversation_id: str,
    new_transcript_segments: List[dict],
    segment_timestamp: float,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Merge [new_transcript_segments] into conversation_id's transcript AND durably record this
    segment's outcome, in one Firestore transaction.

    This is what makes the round-2-reviewed bug (persistence succeeding, then a *separate*
    idempotency-cache write failing, so a retry couldn't tell and reprocessed -- duplicating the
    segment) structurally impossible: the merge and the idempotency record can no longer succeed
    or fail independently. It's also what makes parallel segments explicitly targeting the same
    conversation safe (SYNC-V2-003) -- Firestore transactions serialize/retry conflicting
    concurrent writes to the same document instead of a plain get+merge+set silently dropping one.
    """
    now = now or datetime.now(timezone.utc)
    segment_ref = _segment_ref(uid, segment_id)
    conversation_ref = _conversation_ref(uid, conversation_id)
    return _append_segment_to_conversation_and_complete(
        db.transaction(), segment_ref, conversation_ref, uid, claimant, new_transcript_segments, segment_timestamp, now
    )


# **********************************************
# ****************** RELEASE *********************
# **********************************************


def _release_sync_segment_transaction(transaction, segment_ref, claimant: str, now: datetime) -> Dict[str, Any]:
    snapshot = segment_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {'outcome': 'lost'}
    receipt = snapshot.to_dict() or {}
    if receipt.get('state') == 'done':
        return _done_result(receipt)
    if receipt.get('state') != 'processing' or receipt.get('claimant') != claimant:
        # Either already reclaimed by someone else, or never ours -- never stomp a successor's
        # lease (the exact non-atomic GET-compare-DEL bug the round-2 review found in the Redis
        # releasers this replaces; a Firestore transaction makes the compare-and-clear atomic).
        return {'outcome': 'lost'}
    transaction.set(segment_ref, {'state': 'failed', 'claimant': claimant, 'failed_at': now}, merge=True)
    return {'outcome': 'released'}


@transactional
def _release_sync_segment(transaction, segment_ref, claimant, now):
    return _release_sync_segment_transaction(transaction, segment_ref, claimant, now)


def release_sync_segment(uid: str, segment_id: str, claimant: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Mark a claim as failed after an unsuccessful attempt (STT error, exception, etc.) so a
    retry doesn't have to wait out the full lease. No-ops (outcome 'lost') if [claimant] no longer
    holds the claim -- it already expired and/or was reclaimed by someone else."""
    now = now or datetime.now(timezone.utc)
    return _release_sync_segment(db.transaction(), _segment_ref(uid, segment_id), claimant, now)
