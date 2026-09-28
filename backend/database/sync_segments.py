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
"""

from __future__ import annotations

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
    transaction, segment_ref, claimant: str, now: datetime, lease_seconds: int
) -> Dict[str, Any]:
    snapshot = segment_ref.get(transaction=transaction)
    if snapshot.exists:
        receipt = snapshot.to_dict() or {}
        if receipt.get('state') == 'done':
            return _done_result(receipt)
        if receipt.get('state') == 'processing' and _is_future(receipt.get('lease_expires_at'), now):
            return {'outcome': 'busy'}
        # 'failed', or 'processing' with an expired lease (claimant died mid-work): fall through
        # and (re)claim.
    transaction.set(
        segment_ref,
        {
            'state': 'processing',
            'claimant': claimant,
            'claimed_at': now,
            'lease_expires_at': now + timedelta(seconds=lease_seconds),
        },
    )
    return {'outcome': 'claimed', 'claimant': claimant}


@transactional
def _claim_sync_segment(transaction, segment_ref, claimant: str, now: datetime, lease_seconds: int) -> Dict[str, Any]:
    return _claim_sync_segment_transaction(transaction, segment_ref, claimant, now, lease_seconds)


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
        by an attempt whose HTTP response never reached the client). Report this result; never
        re-run STT/LLM/persistence.
      * 'busy' -- another execution currently holds the claim; back off as retryable.
      * 'claimed' -- this claimant may proceed with STT/LLM/persistence for this segment id.
    """
    now = now or datetime.now(timezone.utc)
    return _claim_sync_segment(db.transaction(), _segment_ref(uid, segment_id), claimant, now, lease_seconds)


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
    conversation_ref = (
        db.collection('users').document(uid).collection(conversations_collection).document(conversation_id)
    )
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
