"""Firestore persistence for the owner-scoped memory-artwork lifecycle."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import transactional

from ._client import db

ARTWORK_FIELD = "artwork"
PREFERENCES_FIELD = "memory_artwork_preferences"
STORAGE_CLEANUP_REQUIRED_FIELD = "memory_artwork_storage_cleanup_required"
DELETION_PENDING_FIELD = "memory_artwork_deletion_pending"
JOB_COLLECTION = "ella_memory_artwork_jobs"


def _user_ref(uid: str):
    return db.collection("users").document(uid)


def _conversation_ref(uid: str, memory_id: str):
    return _user_ref(uid).collection("conversations").document(memory_id)


def _job_id(uid: str, memory_id: str, generation_key: str) -> str:
    return hashlib.sha256(f"{uid}\0{memory_id}\0{generation_key}".encode("utf-8")).hexdigest()


def _job_ref(uid: str, memory_id: str, generation_key: str):
    return db.collection(JOB_COLLECTION).document(_job_id(uid, memory_id, generation_key))


def get_preferences(uid: str) -> dict[str, Any]:
    snapshot = _user_ref(uid).get()
    if not snapshot.exists:
        return {}
    payload = snapshot.to_dict() or {}
    preferences = payload.get(PREFERENCES_FIELD)
    result = dict(preferences) if isinstance(preferences, dict) else {}
    result[STORAGE_CLEANUP_REQUIRED_FIELD] = bool(payload.get(STORAGE_CLEANUP_REQUIRED_FIELD))
    result[DELETION_PENDING_FIELD] = bool(payload.get(DELETION_PENDING_FIELD))
    return result


def set_preferences(uid: str, preferences: dict[str, Any]) -> None:
    _user_ref(uid).set({PREFERENCES_FIELD: preferences}, merge=True)


def get_conversation(uid: str, memory_id: str) -> Optional[dict[str, Any]]:
    snapshot = _conversation_ref(uid, memory_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def list_recent_conversations(uid: str, *, limit: int) -> list[dict[str, Any]]:
    query = (
        db.collection("users")
        .document(uid)
        .collection("conversations")
        .where("discarded", "==", False)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [snapshot.to_dict() for snapshot in query.stream()]


def _terminal_enrichment_matches(conversation: dict[str, Any], enrichment_revision: str) -> bool:
    enrichment = conversation.get("enrichment_state") or {}
    return bool(
        not conversation.get("deletion_pending")
        and not conversation.get("discarded")
        and conversation.get("status") == "completed"
        and conversation.get("active_summary_version_id") == enrichment_revision
        and isinstance(enrichment, dict)
        and enrichment.get("status") == "writeback_applied"
        and enrichment.get("kind")
        in {"observer_enriched", "corrected_enriched", "hermes_enriched", "recovered_enriched"}
    )


def _reserve_generation_transaction(
    transaction,
    user_ref,
    conversation_ref,
    *,
    enrichment_revision: str,
    generation_key: str,
    artwork_state: dict[str, Any],
    job_ref=None,
    job_state: Optional[dict[str, Any]] = None,
    preserve_job_attempts: bool = False,
) -> dict[str, Any]:
    user_snapshot = user_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    if not user_snapshot.exists or bool(user.get(DELETION_PENDING_FIELD)):
        return {"outcome": "deletion_pending"}
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {"outcome": "not_found"}
    conversation = snapshot.to_dict() or {}
    if not _terminal_enrichment_matches(conversation, enrichment_revision):
        return {"outcome": "source_changed"}
    current_job: dict[str, Any] = {}
    effective_job_state = job_state
    if job_ref is not None and job_state is not None:
        job_snapshot = job_ref.get(transaction=transaction)
        current_job = job_snapshot.to_dict() if job_snapshot.exists else {}
        effective_job_state = {
            **job_state,
            "attempt_count": (
                int(current_job.get("attempt_count") or 0)
                if preserve_job_attempts
                else int(job_state.get("attempt_count") or 0)
            ),
            "created_at": current_job.get("created_at") or job_state.get("created_at"),
        }
    current = conversation.get(ARTWORK_FIELD) or {}
    if isinstance(current, dict) and current.get("generation_key") == generation_key:
        current_status = current.get("status")
        ready_object_key = str(current.get("object_key") or "").strip()
        if current_status == "generating" or (current_status == "ready" and ready_object_key):
            if current_status == "generating" and job_ref is not None and job_state is not None:
                if current_job.get("status") not in {"pending", "processing"}:
                    transaction.set(job_ref, effective_job_state)
            return {"outcome": "existing", "artwork": dict(current)}
    transaction.update(conversation_ref, {ARTWORK_FIELD: artwork_state})
    if job_ref is not None and job_state is not None:
        # A worker owns a processing record before it refreshes a retry's
        # generation state. Do not replace that durable claim with pending.
        if not (preserve_job_attempts and current_job.get("status") == "processing"):
            transaction.set(job_ref, effective_job_state)
    return {"outcome": "reserved", "artwork": dict(artwork_state)}


@transactional
def _reserve_generation(transaction, user_ref, conversation_ref, **kwargs):
    return _reserve_generation_transaction(transaction, user_ref, conversation_ref, **kwargs)


def reserve_generation(
    uid: str,
    memory_id: str,
    *,
    enrichment_revision: str,
    generation_key: str,
    artwork_state: dict[str, Any],
    job_state: dict[str, Any],
    preserve_job_attempts: bool = False,
) -> dict[str, Any]:
    return _reserve_generation(
        db.transaction(),
        _user_ref(uid),
        _conversation_ref(uid, memory_id),
        enrichment_revision=enrichment_revision,
        generation_key=generation_key,
        artwork_state=artwork_state,
        job_ref=_job_ref(uid, memory_id, generation_key),
        job_state=job_state,
        preserve_job_attempts=preserve_job_attempts,
    )


def list_pending_jobs(*, limit: int = 25, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    collection = db.collection(JOB_COLLECTION)
    query_limit = max(1, limit)
    snapshots = list(
        collection.where("status", "==", "pending")
        .where("available_at", "<=", current_time)
        .order_by("available_at", direction=firestore.Query.ASCENDING)
        .limit(query_limit)
        .stream()
    )
    snapshots.extend(
        collection.where("status", "==", "processing")
        .where("lease_expires_at", "<=", current_time)
        .order_by("lease_expires_at", direction=firestore.Query.ASCENDING)
        .limit(query_limit)
        .stream()
    )
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        if snapshot.id in seen:
            continue
        seen.add(snapshot.id)
        payload = snapshot.to_dict() or {}
        due_at = (
            payload.get("lease_expires_at") if payload.get("status") == "processing" else payload.get("available_at")
        )
        if not isinstance(due_at, datetime) or due_at > current_time:
            continue
        pending.append({**payload, "job_id": snapshot.id})
    pending.sort(
        key=lambda job: (
            (job.get("lease_expires_at") if job.get("status") == "processing" else job.get("available_at")),
            str(job.get("job_id") or ""),
        )
    )
    return pending[: max(1, limit)]


def _claim_job_transaction(
    transaction,
    user_ref,
    job_ref,
    *,
    lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    user_snapshot = user_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    if not user_snapshot.exists or bool(user.get(DELETION_PENDING_FIELD)):
        return None
    job_snapshot = job_ref.get(transaction=transaction)
    if not job_snapshot.exists:
        return None
    job = job_snapshot.to_dict() or {}
    status = job.get("status")
    if status == "pending":
        available_at = job.get("available_at")
        if isinstance(available_at, datetime) and available_at > now:
            return None
    elif status == "processing":
        lease_expires_at = job.get("lease_expires_at")
        if not isinstance(lease_expires_at, datetime) or lease_expires_at > now:
            return None
    else:
        return None
    claimed = {
        **job,
        "status": "processing",
        "lease_token": lease_token,
        "lease_expires_at": now + timedelta(seconds=max(1, lease_seconds)),
        "updated_at": now,
    }
    transaction.update(job_ref, claimed)
    return claimed


@transactional
def _claim_job(transaction, user_ref, job_ref, **kwargs):
    return _claim_job_transaction(transaction, user_ref, job_ref, **kwargs)


def claim_job(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    lease_token: str,
    now: datetime,
    lease_seconds: int = 120,
) -> Optional[dict[str, Any]]:
    return _claim_job(
        db.transaction(),
        _user_ref(uid),
        _job_ref(uid, memory_id, generation_key),
        lease_token=lease_token,
        now=now,
        lease_seconds=lease_seconds,
    )


def _processing_job_is_active(job: dict[str, Any], *, now: datetime) -> bool:
    lease_expires_at = job.get("lease_expires_at")
    return bool(job.get("status") == "processing" and isinstance(lease_expires_at, datetime) and lease_expires_at > now)


def _job_claim_is_current_transaction(
    transaction,
    user_ref,
    job_ref,
    *,
    lease_token: str,
    now: datetime,
) -> bool:
    user_snapshot = user_ref.get(transaction=transaction)
    job_snapshot = job_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    job = job_snapshot.to_dict() if job_snapshot.exists else {}
    return bool(
        user_snapshot.exists
        and not user.get(DELETION_PENDING_FIELD)
        and job_snapshot.exists
        and job.get("lease_token") == lease_token
        and _processing_job_is_active(job, now=now)
    )


@transactional
def _job_claim_is_current(transaction, user_ref, job_ref, **kwargs):
    return _job_claim_is_current_transaction(transaction, user_ref, job_ref, **kwargs)


def job_claim_is_current(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    lease_token: str,
    now: Optional[datetime] = None,
) -> bool:
    return _job_claim_is_current(
        db.transaction(),
        _user_ref(uid),
        _job_ref(uid, memory_id, generation_key),
        lease_token=lease_token,
        now=now or datetime.now(timezone.utc),
    )


def _finish_job_transaction(
    transaction,
    job_ref,
    *,
    lease_token: str,
    update: dict[str, Any],
) -> bool:
    snapshot = job_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    job = snapshot.to_dict() or {}
    if job.get("status") != "processing" or job.get("lease_token") != lease_token:
        return False
    transaction.update(
        job_ref,
        {
            **update,
            "lease_token": firestore.DELETE_FIELD,
            "lease_expires_at": firestore.DELETE_FIELD,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    return True


@transactional
def _finish_job(transaction, job_ref, **kwargs):
    return _finish_job_transaction(transaction, job_ref, **kwargs)


def complete_job(uid: str, memory_id: str, generation_key: str, *, lease_token: str) -> bool:
    return _finish_job(
        db.transaction(),
        _job_ref(uid, memory_id, generation_key),
        lease_token=lease_token,
        update={"status": "completed"},
    )


def retry_job(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    lease_token: str,
    attempt_count: int,
    delay_seconds: int,
    failure_code: str,
) -> bool:
    now = datetime.now(timezone.utc)
    return _finish_job(
        db.transaction(),
        _job_ref(uid, memory_id, generation_key),
        lease_token=lease_token,
        update={
            "status": "pending",
            "attempt_count": attempt_count,
            "available_at": now + timedelta(seconds=max(1, delay_seconds)),
            "failure_code": failure_code,
        },
    )


def fail_job(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    lease_token: str,
    failure_code: str,
) -> bool:
    return _finish_job(
        db.transaction(),
        _job_ref(uid, memory_id, generation_key),
        lease_token=lease_token,
        update={"status": "failed", "failure_code": failure_code},
    )


def _mark_storage_cleanup_required_transaction(
    transaction,
    user_ref,
    conversation_ref,
    job_ref,
    *,
    generation_key: str,
    generation_lease_token: str,
    job_lease_token: str,
) -> bool:
    user_snapshot = user_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    job_snapshot = job_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    conversation = conversation_snapshot.to_dict() if conversation_snapshot.exists else {}
    job = job_snapshot.to_dict() if job_snapshot.exists else {}
    artwork = conversation.get(ARTWORK_FIELD) or {}
    if (
        not user_snapshot.exists
        or bool(user.get(DELETION_PENDING_FIELD))
        or not conversation_snapshot.exists
        or not isinstance(artwork, dict)
        or artwork.get("generation_key") != generation_key
        or artwork.get("lease_token") != generation_lease_token
        or artwork.get("status") != "generating"
        or not _terminal_enrichment_matches(conversation, str(artwork.get("enrichment_revision") or ""))
        or not job_snapshot.exists
        or job.get("generation_key") != generation_key
        or job.get("lease_token") != job_lease_token
        or not _processing_job_is_active(job, now=datetime.now(timezone.utc))
    ):
        return False
    transaction.update(user_ref, {STORAGE_CLEANUP_REQUIRED_FIELD: True})
    return True


@transactional
def _mark_storage_cleanup_required(transaction, user_ref, conversation_ref, job_ref, **kwargs) -> bool:
    return _mark_storage_cleanup_required_transaction(transaction, user_ref, conversation_ref, job_ref, **kwargs)


def mark_storage_cleanup_required(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    generation_lease_token: str,
    job_lease_token: str,
) -> bool:
    return _mark_storage_cleanup_required(
        db.transaction(),
        _user_ref(uid),
        _conversation_ref(uid, memory_id),
        _job_ref(uid, memory_id, generation_key),
        generation_key=generation_key,
        generation_lease_token=generation_lease_token,
        job_lease_token=job_lease_token,
    )


def storage_cleanup_required(uid: str) -> bool:
    snapshot = _user_ref(uid).get()
    return bool(snapshot.exists and (snapshot.to_dict() or {}).get(STORAGE_CLEANUP_REQUIRED_FIELD))


def _begin_account_deletion_transaction(transaction, user_ref) -> bool:
    snapshot = user_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    transaction.update(user_ref, {DELETION_PENDING_FIELD: True})
    return True


@transactional
def _begin_account_deletion(transaction, user_ref) -> bool:
    return _begin_account_deletion_transaction(transaction, user_ref)


def begin_account_deletion(uid: str) -> bool:
    return _begin_account_deletion(db.transaction(), _user_ref(uid))


def has_processing_jobs(uid: str, *, now: Optional[datetime] = None) -> bool:
    current_time = now or datetime.now(timezone.utc)
    snapshots = db.collection(JOB_COLLECTION).where("uid", "==", uid).stream()
    return any(_processing_job_is_active(snapshot.to_dict() or {}, now=current_time) for snapshot in snapshots)


def has_processing_jobs_for_memory(uid: str, memory_id: str, *, now: Optional[datetime] = None) -> bool:
    current_time = now or datetime.now(timezone.utc)
    snapshots = db.collection(JOB_COLLECTION).where("uid", "==", uid).stream()
    return any(
        (snapshot.to_dict() or {}).get("memory_id") == memory_id
        and _processing_job_is_active(snapshot.to_dict() or {}, now=current_time)
        for snapshot in snapshots
    )


def delete_jobs_for_uid(uid: str, *, batch_size: int = 450) -> int:
    deleted = 0
    while True:
        snapshots = list(db.collection(JOB_COLLECTION).where("uid", "==", uid).limit(max(1, batch_size)).stream())
        if not snapshots:
            return deleted
        batch = db.batch()
        for snapshot in snapshots:
            batch.delete(snapshot.reference)
        batch.commit()
        deleted += len(snapshots)


def delete_jobs_for_memory(uid: str, memory_id: str, *, batch_size: int = 450) -> int:
    deleted = 0
    while True:
        snapshots = [
            snapshot
            for snapshot in db.collection(JOB_COLLECTION).where("uid", "==", uid).stream()
            if (snapshot.to_dict() or {}).get("memory_id") == memory_id
        ][: max(1, batch_size)]
        if not snapshots:
            return deleted
        batch = db.batch()
        for snapshot in snapshots:
            batch.delete(snapshot.reference)
        batch.commit()
        deleted += len(snapshots)


def _claim_generation_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    conversation = snapshot.to_dict() or {}
    artwork = conversation.get(ARTWORK_FIELD) or {}
    if not isinstance(artwork, dict) or artwork.get("generation_key") != generation_key:
        return None
    if not _terminal_enrichment_matches(conversation, str(artwork.get("enrichment_revision") or "")):
        return None
    if artwork.get("status") != "generating":
        return None
    current_expiry = artwork.get("lease_expires_at")
    if isinstance(current_expiry, datetime) and current_expiry > now:
        return None
    claimed = dict(artwork)
    claimed.update(
        {
            "lease_token": lease_token,
            "lease_expires_at": now + timedelta(seconds=max(1, lease_seconds)),
            "updated_at": now,
        }
    )
    transaction.update(conversation_ref, {ARTWORK_FIELD: claimed})
    return claimed


@transactional
def _claim_generation(transaction, conversation_ref, **kwargs):
    return _claim_generation_transaction(transaction, conversation_ref, **kwargs)


def claim_generation(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    lease_token: str,
    now: datetime,
    lease_seconds: int = 120,
) -> Optional[dict[str, Any]]:
    return _claim_generation(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        lease_token=lease_token,
        now=now,
        lease_seconds=lease_seconds,
    )


def _generation_is_current(
    conversation: dict[str, Any],
    generation_key: str,
    authority_digest: str,
    lease_token: str,
) -> bool:
    artwork = conversation.get(ARTWORK_FIELD) or {}
    return bool(
        isinstance(artwork, dict)
        and artwork.get("generation_key") == generation_key
        and artwork.get("authority_digest") == authority_digest
        and artwork.get("lease_token") == lease_token
        and artwork.get("status") == "generating"
        and _terminal_enrichment_matches(conversation, str(artwork.get("enrichment_revision") or ""))
    )


def _finalize_generation_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    authority_digest: str,
    lease_token: str,
    ready_state: dict[str, Any],
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    if not _generation_is_current(conversation, generation_key, authority_digest, lease_token):
        return False
    transaction.update(conversation_ref, {ARTWORK_FIELD: ready_state})
    return True


@transactional
def _finalize_generation(transaction, conversation_ref, **kwargs):
    return _finalize_generation_transaction(transaction, conversation_ref, **kwargs)


def finalize_generation(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    authority_digest: str,
    lease_token: str,
    ready_state: dict[str, Any],
) -> bool:
    return _finalize_generation(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        authority_digest=authority_digest,
        lease_token=lease_token,
        ready_state=ready_state,
    )


def _mark_generation_unavailable_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    failure_code: str,
    lease_token: Optional[str] = None,
    expected_artwork: Optional[dict[str, Any]] = None,
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    current = conversation.get(ARTWORK_FIELD) or {}
    if not isinstance(current, dict) or current.get("generation_key") != generation_key:
        return False
    if lease_token is not None and current.get("lease_token") != lease_token:
        return False
    if expected_artwork is not None:
        if (
            expected_artwork.get("status") != "ready"
            or not str(expected_artwork.get("object_key") or "").strip()
            or expected_artwork.get("generation_key") != generation_key
            or current != expected_artwork
        ):
            return False
    unavailable = dict(current)
    unavailable.update(
        {
            "status": "unavailable",
            "failure_code": failure_code,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    unavailable.pop("lease_token", None)
    unavailable.pop("lease_expires_at", None)
    transaction.update(conversation_ref, {ARTWORK_FIELD: unavailable})
    return True


@transactional
def _mark_generation_unavailable(transaction, conversation_ref, **kwargs):
    return _mark_generation_unavailable_transaction(transaction, conversation_ref, **kwargs)


def mark_generation_unavailable(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    failure_code: str,
    lease_token: Optional[str] = None,
    expected_artwork: Optional[dict[str, Any]] = None,
) -> bool:
    return _mark_generation_unavailable(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        failure_code=failure_code,
        lease_token=lease_token,
        expected_artwork=expected_artwork,
    )


def _claim_deletion_transaction(transaction, conversation_ref) -> Optional[dict[str, Any]]:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    conversation = snapshot.to_dict() or {}
    if not conversation.get("deletion_pending"):
        transaction.update(conversation_ref, {"deletion_pending": True})
        conversation["deletion_pending"] = True
    return conversation


@transactional
def _claim_deletion(transaction, conversation_ref):
    return _claim_deletion_transaction(transaction, conversation_ref)


def claim_deletion(uid: str, memory_id: str) -> Optional[dict[str, Any]]:
    return _claim_deletion(db.transaction(), _conversation_ref(uid, memory_id))
