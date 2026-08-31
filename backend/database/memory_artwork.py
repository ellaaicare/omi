"""Firestore persistence for the owner-scoped memory-artwork lifecycle."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import transactional

from ._client import db

ARTWORK_FIELD = "artwork"
PUBLISHED_ARTWORK_FIELD = "published_artwork"
PREFERENCES_FIELD = "memory_artwork_preferences"
BACKFILL_CONTROL_FIELD = "memory_artwork_backfill_control"
STORAGE_CLEANUP_REQUIRED_FIELD = "memory_artwork_storage_cleanup_required"
DELETION_PENDING_FIELD = "memory_artwork_deletion_pending"
JOB_COLLECTION = "ella_memory_artwork_jobs"
RECONCILIATION_COLLECTION = "ella_memory_artwork_reconciliation_jobs"
DEFAULT_BACKFILL_BATCH_SIZE = 10
FIRESTORE_MIGRATION_BATCH_SIZE = 400
WORKER_SCAN_PAGE_MULTIPLIER = 4
WORKER_SCAN_PAGE_MAX = 100
TERMINAL_ENRICHMENT_ORIGIN = "terminal_enrichment"
HISTORICAL_BACKFILL_ORIGIN = "historical_backfill"

_due_scan_cursors: dict[tuple[str, str], Any] = {}


def _user_ref(uid: str):
    return db.collection("users").document(uid)


def _conversation_ref(uid: str, memory_id: str):
    return _user_ref(uid).collection("conversations").document(memory_id)


def _job_id(uid: str, memory_id: str, generation_key: str) -> str:
    return hashlib.sha256(f"{uid}\0{memory_id}\0{generation_key}".encode("utf-8")).hexdigest()


def _job_ref(uid: str, memory_id: str, generation_key: str):
    return db.collection(JOB_COLLECTION).document(_job_id(uid, memory_id, generation_key))


def reconciliation_job_id(uid: str, authority_digest: str, style_version: str) -> str:
    return hashlib.sha256(f"{uid}\0{authority_digest}\0{style_version}".encode("utf-8")).hexdigest()


def _backfill_control_state(
    uid: str,
    preferences: dict[str, Any],
    state: str,
    *,
    auto_continue: bool = False,
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    pause_reason: str = "",
) -> dict[str, Any]:
    authority_digest = str(preferences.get("authority_digest") or "")
    style_version = str(preferences.get("style_version") or "")
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "ella.memory_artwork.queue_control.v1",
        "generation_id": reconciliation_job_id(uid, authority_digest, style_version),
        "authority_digest": authority_digest,
        "style_version": style_version,
        "state": state,
        "auto_continue": auto_continue,
        "batch_size": batch_size,
        "batch_remaining": 0 if state != "running" or auto_continue else batch_size,
        "pause_reason": pause_reason,
        "updated_at": now,
    }


def _reconciliation_ref(job_id: str):
    return db.collection(RECONCILIATION_COLLECTION).document(job_id)


def _create_reconciliation_job_transaction(transaction, user_ref, job_ref, *, job_state: dict[str, Any]):
    user_snapshot = user_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    if not user_snapshot.exists or bool(user.get(DELETION_PENDING_FIELD)):
        return {"outcome": "deletion_pending"}
    existing_snapshot = job_ref.get(transaction=transaction)
    existing = existing_snapshot.to_dict() if existing_snapshot.exists else {}
    if (
        existing.get("uid") == job_state.get("uid")
        and existing.get("authority_digest") == job_state.get("authority_digest")
        and existing.get("style_version") == job_state.get("style_version")
        and existing.get("status") in {"pending", "processing"}
    ):
        return {"outcome": "existing", "job": dict(existing)}
    transaction.set(job_ref, job_state)
    return {"outcome": "reserved", "job": dict(job_state)}


@transactional
def _create_reconciliation_job(transaction, user_ref, job_ref, **kwargs):
    return _create_reconciliation_job_transaction(transaction, user_ref, job_ref, **kwargs)


def create_reconciliation_job(uid: str, *, authority_digest: str, style_version: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    job_id = reconciliation_job_id(uid, authority_digest, style_version)
    state = {
        "schema_version": "ella.memory_artwork.reconciliation.v1",
        "job_id": job_id,
        "uid": uid,
        "authority_digest": authority_digest,
        "style_version": style_version,
        "status": "pending",
        "cursor": None,
        "pages_processed": 0,
        "scanned": 0,
        "queued": 0,
        "existing": 0,
        "skipped": 0,
        "attempt_count": 0,
        "available_at": now,
        "created_at": now,
        "updated_at": now,
    }
    return _create_reconciliation_job(
        db.transaction(),
        _user_ref(uid),
        _reconciliation_ref(job_id),
        job_state=state,
    )


def get_reconciliation_job(uid: str, job_id: str) -> Optional[dict[str, Any]]:
    snapshot = _reconciliation_ref(job_id).get()
    if not snapshot.exists:
        return None
    payload = snapshot.to_dict() or {}
    if payload.get("uid") != uid:
        return None
    return {**payload, "job_id": snapshot.id}


def _bounded_due_snapshots(
    collection_name: str,
    *,
    status: str,
    due_field: str,
    current_time: datetime,
    worker_limit: int,
) -> list[Any]:
    page_size = min(
        WORKER_SCAN_PAGE_MAX,
        max(1, worker_limit) * WORKER_SCAN_PAGE_MULTIPLIER,
    )
    query = (
        db.collection(collection_name)
        .where("status", "==", status)
        .where(due_field, "<=", current_time)
        .order_by(due_field, direction=firestore.Query.ASCENDING)
    )
    cursor_key = (collection_name, status)
    cursor = _due_scan_cursors.get(cursor_key)
    if cursor is not None:
        query = query.start_after(cursor)
    snapshots = list(query.limit(page_size).stream())
    if len(snapshots) == page_size:
        _due_scan_cursors[cursor_key] = snapshots[-1]
    else:
        _due_scan_cursors.pop(cursor_key, None)
    return snapshots


def list_pending_reconciliation_jobs(*, limit: int = 5, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    snapshots = _bounded_due_snapshots(
        RECONCILIATION_COLLECTION,
        status="pending",
        due_field="available_at",
        current_time=current_time,
        worker_limit=limit,
    )
    snapshots.extend(
        _bounded_due_snapshots(
            RECONCILIATION_COLLECTION,
            status="processing",
            due_field="lease_expires_at",
            current_time=current_time,
            worker_limit=limit,
        )
    )
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        if snapshot.id in seen:
            continue
        seen.add(snapshot.id)
        payload = snapshot.to_dict() or {}
        status = payload.get("status")
        due_at = payload.get("lease_expires_at") if status == "processing" else payload.get("available_at")
        if status not in {"pending", "processing"} or not isinstance(due_at, datetime) or due_at > current_time:
            continue
        pending.append({**payload, "job_id": snapshot.id})
    pending.sort(
        key=lambda job: (
            job.get("lease_expires_at") if job.get("status") == "processing" else job.get("available_at"),
            str(job.get("job_id") or ""),
        )
    )
    eligible: list[dict[str, Any]] = []
    users: dict[str, dict[str, Any]] = {}
    for job in pending:
        uid = str(job.get("uid") or "")
        if not uid:
            continue
        if uid not in users:
            user_snapshot = _user_ref(uid).get()
            users[uid] = user_snapshot.to_dict() if user_snapshot.exists else {}
        if _backfill_control_allows_job(users[uid], job):
            eligible.append(job)
            if len(eligible) >= max(1, limit):
                break
    return eligible


def _claim_reconciliation_job_transaction(
    transaction,
    user_ref,
    job_ref,
    *,
    lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    snapshot = job_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    user_snapshot = user_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    if not user_snapshot.exists or bool(user.get(DELETION_PENDING_FIELD)):
        transaction.delete(job_ref)
        return None
    job = snapshot.to_dict() or {}
    if not _backfill_control_allows_job(user, job):
        return None
    status = job.get("status")
    if status == "processing":
        expiry = job.get("lease_expires_at")
        if not isinstance(expiry, datetime) or expiry > now:
            return None
    elif status != "pending":
        return None
    claimed = {
        **job,
        "status": "processing",
        "lease_token": lease_token,
        "lease_expires_at": now + timedelta(seconds=max(1, lease_seconds)),
        "updated_at": now,
    }
    transaction.set(job_ref, claimed)
    return claimed


@transactional
def _claim_reconciliation_job(transaction, user_ref, job_ref, **kwargs):
    return _claim_reconciliation_job_transaction(transaction, user_ref, job_ref, **kwargs)


def claim_reconciliation_job(
    uid: str,
    job_id: str,
    *,
    lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    return _claim_reconciliation_job(
        db.transaction(),
        _user_ref(uid),
        _reconciliation_ref(job_id),
        lease_token=lease_token,
        now=now,
        lease_seconds=lease_seconds,
    )


def _finish_reconciliation_job_transaction(
    transaction,
    job_ref,
    *,
    lease_token: str,
    update: dict[str, Any],
) -> bool:
    snapshot = job_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    current = snapshot.to_dict() or {}
    if current.get("status") != "processing" or current.get("lease_token") != lease_token:
        return False
    finished = {**update, "updated_at": datetime.now(timezone.utc)}
    finished["lease_token"] = firestore.DELETE_FIELD
    finished["lease_expires_at"] = firestore.DELETE_FIELD
    transaction.update(job_ref, finished)
    return True


@transactional
def _finish_reconciliation_job(transaction, job_ref, **kwargs):
    return _finish_reconciliation_job_transaction(transaction, job_ref, **kwargs)


def finish_reconciliation_job(job_id: str, *, lease_token: str, update: dict[str, Any]) -> bool:
    return _finish_reconciliation_job(
        db.transaction(),
        _reconciliation_ref(job_id),
        lease_token=lease_token,
        update=update,
    )


def get_preferences(uid: str) -> dict[str, Any]:
    snapshot = _user_ref(uid).get()
    if not snapshot.exists:
        return {}
    payload = snapshot.to_dict() or {}
    preferences = payload.get(PREFERENCES_FIELD)
    result = dict(preferences) if isinstance(preferences, dict) else {}
    if payload.get(STORAGE_CLEANUP_REQUIRED_FIELD):
        result[STORAGE_CLEANUP_REQUIRED_FIELD] = True
    if payload.get(DELETION_PENDING_FIELD):
        result[DELETION_PENDING_FIELD] = True
    return result


def set_preferences(
    uid: str,
    preferences: dict[str, Any],
    *,
    backfill_control_state: Optional[str] = None,
) -> None:
    update: dict[str, Any] = {PREFERENCES_FIELD: preferences}
    if backfill_control_state is not None:
        update[BACKFILL_CONTROL_FIELD] = _backfill_control_state(uid, preferences, backfill_control_state)
    _user_ref(uid).set(update, merge=True)


def get_backfill_control(uid: str) -> dict[str, Any]:
    snapshot = _user_ref(uid).get()
    if not snapshot.exists:
        return {}
    control = (snapshot.to_dict() or {}).get(BACKFILL_CONTROL_FIELD)
    return dict(control) if isinstance(control, dict) else {}


def _set_backfill_control_transaction(
    transaction,
    user_ref,
    *,
    expected_generation_id: str,
    state: str,
    auto_continue: bool,
) -> dict[str, Any]:
    snapshot = user_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {"outcome": "not_found"}
    user = snapshot.to_dict() or {}
    if bool(user.get(DELETION_PENDING_FIELD)):
        return {"outcome": "deletion_pending"}
    preferences = user.get(PREFERENCES_FIELD)
    if not isinstance(preferences, dict):
        return {"outcome": "preferences_missing"}
    generation_id = reconciliation_job_id(
        str(snapshot.id),
        str(preferences.get("authority_digest") or ""),
        str(preferences.get("style_version") or ""),
    )
    if generation_id != expected_generation_id:
        return {"outcome": "generation_stale"}
    pause_reason = ""
    if state == "paused":
        pause_reason = "user_paused"
    elif state == "cancelled":
        pause_reason = "user_cancelled"
    control = _backfill_control_state(
        str(snapshot.id),
        preferences,
        state,
        auto_continue=auto_continue if state == "running" else False,
        pause_reason=pause_reason,
    )
    transaction.set(user_ref, {BACKFILL_CONTROL_FIELD: control}, merge=True)
    return {"outcome": "updated", "control": control}


@transactional
def _set_backfill_control(transaction, user_ref, **kwargs):
    return _set_backfill_control_transaction(transaction, user_ref, **kwargs)


def set_backfill_control(
    uid: str,
    *,
    expected_generation_id: str,
    state: str,
    auto_continue: bool = False,
) -> dict[str, Any]:
    return _set_backfill_control(
        db.transaction(),
        _user_ref(uid),
        expected_generation_id=expected_generation_id,
        state=state,
        auto_continue=auto_continue,
    )


def _legacy_job_metadata(job: dict[str, Any], conversation: dict[str, Any]) -> dict[str, str]:
    artwork = conversation.get(ARTWORK_FIELD)
    if not isinstance(artwork, dict) or artwork.get("generation_key") != job.get("generation_key"):
        return {}
    authority_digest = str(artwork.get("authority_digest") or "")
    style_version = str(artwork.get("style_version") or "")
    if not authority_digest or not style_version:
        return {}
    return {
        "authority_digest": authority_digest,
        "style_version": style_version,
        "origin": HISTORICAL_BACKFILL_ORIGIN,
    }


def list_jobs_for_uid(uid: str, *, migrate_legacy_jobs: bool = True) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    migration_batch = None
    migration_count = 0
    for snapshot in db.collection(JOB_COLLECTION).where("uid", "==", uid).stream():
        payload = snapshot.to_dict() or {}
        if migrate_legacy_jobs and (
            not payload.get("authority_digest") or not payload.get("style_version") or not payload.get("origin")
        ):
            memory_id = str(payload.get("memory_id") or "")
            if memory_id:
                conversation_snapshot = _conversation_ref(uid, memory_id).get()
                conversation = conversation_snapshot.to_dict() if conversation_snapshot.exists else {}
                migration = _legacy_job_metadata(payload, conversation)
                if migration:
                    migration_batch = migration_batch or db.batch()
                    payload = {**payload, **migration}
                    migration_batch.set(snapshot.reference, migration, merge=True)
                    migration_count += 1
                    if migration_count >= FIRESTORE_MIGRATION_BATCH_SIZE:
                        migration_batch.commit()
                        migration_batch = None
                        migration_count = 0
        jobs.append({**payload, "job_id": snapshot.id})
    if migration_count and migration_batch is not None:
        migration_batch.commit()
    return jobs


def get_conversation(uid: str, memory_id: str) -> Optional[dict[str, Any]]:
    snapshot = _conversation_ref(uid, memory_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def list_conversations_page(
    uid: str,
    *,
    limit: int,
    cursor_memory_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    conversations = db.collection("users").document(uid).collection("conversations")
    query = conversations.where("discarded", "==", False).order_by("created_at", direction=firestore.Query.DESCENDING)
    if cursor_memory_id:
        cursor = conversations.document(cursor_memory_id).get()
        if not cursor.exists:
            raise ValueError("memory_artwork_backfill_cursor_invalid")
        query = query.start_after(cursor)
    query = query.limit(limit)
    return [{**(snapshot.to_dict() or {}), "id": snapshot.id} for snapshot in query.stream()]


def list_recent_conversations(uid: str, *, limit: int) -> list[dict[str, Any]]:
    return list_conversations_page(uid, limit=limit)


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
        if current.get("status") in {"generating", "ready"}:
            if current.get("status") == "generating" and job_ref is not None and job_state is not None:
                should_promote_terminal = bool(
                    current_job.get("status") == "pending"
                    and current_job.get("origin") != TERMINAL_ENRICHMENT_ORIGIN
                    and job_state.get("origin") == TERMINAL_ENRICHMENT_ORIGIN
                )
                if should_promote_terminal:
                    transaction.update(
                        job_ref,
                        {
                            "origin": TERMINAL_ENRICHMENT_ORIGIN,
                            "updated_at": job_state.get("updated_at") or datetime.now(timezone.utc),
                        },
                    )
                elif current_job.get("status") not in {"pending", "processing"}:
                    transaction.set(job_ref, effective_job_state)
            return {"outcome": "existing", "artwork": dict(current)}
    published = conversation.get(PUBLISHED_ARTWORK_FIELD) or {}
    update: dict[str, Any] = {ARTWORK_FIELD: artwork_state}
    if (
        isinstance(current, dict)
        and current.get("status") == "ready"
        and current.get("enrichment_revision") == enrichment_revision
    ):
        update[PUBLISHED_ARTWORK_FIELD] = dict(current)
    elif not (
        isinstance(published, dict)
        and published.get("status") == "ready"
        and published.get("enrichment_revision") == enrichment_revision
    ):
        update[PUBLISHED_ARTWORK_FIELD] = firestore.DELETE_FIELD
    transaction.update(conversation_ref, update)
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
    snapshots = _bounded_due_snapshots(
        JOB_COLLECTION,
        status="pending",
        due_field="available_at",
        current_time=current_time,
        worker_limit=limit,
    )
    snapshots.extend(
        _bounded_due_snapshots(
            JOB_COLLECTION,
            status="processing",
            due_field="lease_expires_at",
            current_time=current_time,
            worker_limit=limit,
        )
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
    eligible: list[dict[str, Any]] = []
    users: dict[str, dict[str, Any]] = {}
    for job in pending:
        uid = str(job.get("uid") or "")
        if not uid:
            continue
        if uid not in users:
            user_snapshot = _user_ref(uid).get()
            users[uid] = user_snapshot.to_dict() if user_snapshot.exists else {}
        if _backfill_control_allows_job(users[uid], job):
            eligible.append(job)
            if len(eligible) >= max(1, limit):
                break
    return eligible


def _backfill_control_allows_job(user: dict[str, Any], job: dict[str, Any]) -> bool:
    preferences = user.get(PREFERENCES_FIELD)
    if not isinstance(preferences, dict):
        return False
    authority_digest = str(job.get("authority_digest") or preferences.get("authority_digest") or "")
    style_version = str(job.get("style_version") or preferences.get("style_version") or "")
    if not authority_digest or not style_version:
        return False
    if authority_digest != preferences.get("authority_digest") or style_version != preferences.get("style_version"):
        return False
    if str(job.get("origin") or HISTORICAL_BACKFILL_ORIGIN) == TERMINAL_ENRICHMENT_ORIGIN:
        return True
    control = user.get(BACKFILL_CONTROL_FIELD)
    if control is None:
        # The first claim for a legacy queue initializes the bounded batch
        # control transactionally below.
        return True
    if not isinstance(control, dict):
        return False
    expected_generation_id = reconciliation_job_id(
        str(job.get("uid") or ""),
        authority_digest,
        style_version,
    )
    return bool(
        control.get("schema_version") == "ella.memory_artwork.queue_control.v1"
        and control.get("generation_id") == expected_generation_id
        and control.get("authority_digest") == authority_digest
        and control.get("style_version") == style_version
        and control.get("state") == "running"
    )


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
    preferences = user.get(PREFERENCES_FIELD)
    if not isinstance(preferences, dict):
        return None
    job = {
        **job,
        "authority_digest": str(job.get("authority_digest") or preferences.get("authority_digest") or ""),
        "style_version": str(job.get("style_version") or preferences.get("style_version") or ""),
        "origin": str(job.get("origin") or HISTORICAL_BACKFILL_ORIGIN),
    }
    if not _backfill_control_allows_job(user, job):
        return None
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
    origin = str(job.get("origin") or HISTORICAL_BACKFILL_ORIGIN)
    if origin == HISTORICAL_BACKFILL_ORIGIN:
        control = user.get(BACKFILL_CONTROL_FIELD)
        if control is None:
            control = _backfill_control_state(str(user_ref.id), preferences, "running")
        if not isinstance(control, dict):
            return None
        auto_continue = bool(control.get("auto_continue"))
        batch_size = int(control.get("batch_size") or DEFAULT_BACKFILL_BATCH_SIZE)
        batch_remaining = int(control.get("batch_remaining", batch_size) or 0)
        if batch_size < 1 or (batch_remaining < 1 and not auto_continue):
            return None
        if not auto_continue:
            batch_remaining -= 1
            control = {
                **control,
                "batch_size": batch_size,
                "batch_remaining": batch_remaining,
                "state": "paused" if batch_remaining == 0 else "running",
                "pause_reason": "batch_complete" if batch_remaining == 0 else "",
                "updated_at": now,
            }
        transaction.set(user_ref, {BACKFILL_CONTROL_FIELD: control}, merge=True)
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


def _renew_publication_claim_transaction(
    transaction,
    user_ref,
    conversation_ref,
    job_ref,
    *,
    generation_key: str,
    generation_lease_token: str,
    job_lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    user_snapshot = user_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    job_snapshot = job_ref.get(transaction=transaction)
    user = user_snapshot.to_dict() if user_snapshot.exists else {}
    conversation = conversation_snapshot.to_dict() if conversation_snapshot.exists else {}
    job = job_snapshot.to_dict() if job_snapshot.exists else {}
    current_artwork = conversation.get(ARTWORK_FIELD) or {}
    if (
        not user_snapshot.exists
        or bool(user.get(DELETION_PENDING_FIELD))
        or not conversation_snapshot.exists
        or bool(conversation.get("deletion_pending"))
        or not isinstance(current_artwork, dict)
        or current_artwork.get("generation_key") != generation_key
        or current_artwork.get("lease_token") != generation_lease_token
        or current_artwork.get("status") != "generating"
        or not _terminal_enrichment_matches(conversation, str(current_artwork.get("enrichment_revision") or ""))
        or not job_snapshot.exists
        or job.get("generation_key") != generation_key
        or job.get("status") != "processing"
        or job.get("lease_token") != job_lease_token
    ):
        return False
    publication_expiry = now + timedelta(seconds=max(1, lease_seconds))
    renewed_artwork = dict(current_artwork)
    renewed_artwork.update({"lease_expires_at": publication_expiry, "updated_at": now})
    transaction.update(user_ref, {STORAGE_CLEANUP_REQUIRED_FIELD: True})
    transaction.update(conversation_ref, {ARTWORK_FIELD: renewed_artwork})
    transaction.update(job_ref, {"lease_expires_at": publication_expiry, "updated_at": now})
    return True


@transactional
def _renew_publication_claim(transaction, user_ref, conversation_ref, job_ref, **kwargs) -> bool:
    return _renew_publication_claim_transaction(transaction, user_ref, conversation_ref, job_ref, **kwargs)


def renew_publication_claim(
    uid: str,
    memory_id: str,
    generation_key: str,
    *,
    generation_lease_token: str,
    job_lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    return _renew_publication_claim(
        db.transaction(),
        _user_ref(uid),
        _conversation_ref(uid, memory_id),
        _job_ref(uid, memory_id, generation_key),
        generation_key=generation_key,
        generation_lease_token=generation_lease_token,
        job_lease_token=job_lease_token,
        now=now,
        lease_seconds=lease_seconds,
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
    for collection_name in (JOB_COLLECTION, RECONCILIATION_COLLECTION):
        while True:
            snapshots = list(db.collection(collection_name).where("uid", "==", uid).limit(max(1, batch_size)).stream())
            if not snapshots:
                break
            batch = db.batch()
            for snapshot in snapshots:
                batch.delete(snapshot.reference)
            batch.commit()
            deleted += len(snapshots)
    return deleted


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
    transaction.update(
        conversation_ref,
        {
            ARTWORK_FIELD: ready_state,
        },
    )
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


def _clear_published_artwork_transaction(
    transaction,
    conversation_ref,
    *,
    object_key: str,
    object_generation: str,
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    published = conversation.get(PUBLISHED_ARTWORK_FIELD) or {}
    if (
        not isinstance(published, dict)
        or published.get("object_key") != object_key
        or str(published.get("object_generation") or "") != object_generation
    ):
        return False
    transaction.update(conversation_ref, {PUBLISHED_ARTWORK_FIELD: firestore.DELETE_FIELD})
    return True


@transactional
def _clear_published_artwork(transaction, conversation_ref, **kwargs) -> bool:
    return _clear_published_artwork_transaction(transaction, conversation_ref, **kwargs)


def clear_published_artwork(
    uid: str,
    memory_id: str,
    *,
    object_key: str,
    object_generation: str,
) -> bool:
    return _clear_published_artwork(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        object_key=object_key,
        object_generation=object_generation,
    )


def _mark_generation_unavailable_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    failure_code: str,
    lease_token: Optional[str] = None,
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
) -> bool:
    return _mark_generation_unavailable(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        failure_code=failure_code,
        lease_token=lease_token,
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
