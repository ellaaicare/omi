"""Durable Firestore outbox for OMI conversation enrichment delivery."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud.firestore_v1 import FieldFilter, transactional

from database._client import db

COLLECTION = "ella_hermes_cloud_enrichment_outbox"
PENDING_STATUSES = ("pending", "retryable", "running")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@transactional
def _enqueue_transaction(
    transaction,
    reference,
    payload: dict[str, Any],
) -> dict[str, Any]:
    snapshot = reference.get(transaction=transaction)
    if snapshot.exists:
        existing = snapshot.to_dict() or {}
        immutable_fields = (
            "uid",
            "conversation_id",
            "client_interaction_id",
            "transcript_sha256",
        )
        if any(existing.get(field) != payload.get(field) for field in immutable_fields):
            raise ValueError("hermes_cloud_enrichment_outbox_identity_conflict")
        return existing
    transaction.set(reference, payload)
    return payload


@transactional
def _claim_transaction(
    transaction,
    reference,
    *,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    snapshot = reference.get(transaction=transaction)
    if not snapshot.exists:
        return None
    current = snapshot.to_dict() or {}
    status = current.get("status")
    next_attempt_at = _aware(current.get("next_attempt_at"))
    lease_expires_at = _aware(current.get("lease_expires_at"))
    ready = status in {"pending", "retryable"} and (next_attempt_at is None or next_attempt_at <= now)
    lease_expired = status == "running" and (lease_expires_at is None or lease_expires_at <= now)
    if not ready and not lease_expired:
        return None

    lease_token = secrets.token_urlsafe(24)
    claimed = {
        **current,
        "status": "running",
        "lease_token": lease_token,
        "lease_expires_at": now + timedelta(seconds=lease_seconds),
        "attempt_count": int(current.get("attempt_count") or 0) + 1,
        "updated_at": now,
    }
    transaction.update(
        reference,
        {
            "status": claimed["status"],
            "lease_token": claimed["lease_token"],
            "lease_expires_at": claimed["lease_expires_at"],
            "attempt_count": claimed["attempt_count"],
            "updated_at": claimed["updated_at"],
        },
    )
    return claimed


@transactional
def _complete_transaction(
    transaction,
    reference,
    *,
    lease_token: str,
    receipt: dict[str, Any],
    now: datetime,
) -> bool:
    snapshot = reference.get(transaction=transaction)
    current = snapshot.to_dict() if snapshot.exists else {}
    if not current or current.get("status") != "running" or current.get("lease_token") != lease_token:
        return False
    transaction.update(
        reference,
        {
            "status": "completed",
            "receipt": receipt,
            "last_error_code": None,
            "lease_token": None,
            "lease_expires_at": None,
            "completed_at": now,
            "updated_at": now,
        },
    )
    return True


@transactional
def _fail_transaction(
    transaction,
    reference,
    *,
    lease_token: str,
    error_code: str,
    retryable: bool,
    next_attempt_at: Optional[datetime],
    now: datetime,
) -> bool:
    snapshot = reference.get(transaction=transaction)
    current = snapshot.to_dict() if snapshot.exists else {}
    if not current or current.get("status") != "running" or current.get("lease_token") != lease_token:
        return False
    transaction.update(
        reference,
        {
            "status": "retryable" if retryable else "blocked",
            "last_error_code": error_code[:120],
            "next_attempt_at": next_attempt_at if retryable else None,
            "lease_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        },
    )
    return True


class FirestoreHermesCloudEnrichmentOutbox:
    def __init__(self, firestore_db: Any = db):
        self.db = firestore_db

    def enqueue(
        self,
        *,
        job_id: str,
        uid: str,
        conversation_id: str,
        client_interaction_id: str,
        transcript_sha256: str,
        policy_version: str,
    ) -> dict[str, Any]:
        now = _utcnow()
        payload = {
            "job_id": job_id,
            "uid": uid,
            "conversation_id": conversation_id,
            "client_interaction_id": client_interaction_id,
            "transcript_sha256": transcript_sha256,
            "policy_version": policy_version,
            "status": "pending",
            "attempt_count": 0,
            "next_attempt_at": now,
            "lease_token": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "receipt": None,
            "created_at": now,
            "updated_at": now,
        }
        reference = self.db.collection(COLLECTION).document(job_id)
        return _enqueue_transaction(self.db.transaction(), reference, payload)

    def claim_next(
        self,
        *,
        lease_seconds: int,
        scan_limit: int = 50,
    ) -> Optional[dict[str, Any]]:
        now = _utcnow()
        query = (
            self.db.collection(COLLECTION)
            .where(filter=FieldFilter("status", "in", list(PENDING_STATUSES)))
            .limit(scan_limit)
        )
        for snapshot in query.stream():
            claimed = _claim_transaction(
                self.db.transaction(),
                snapshot.reference,
                now=now,
                lease_seconds=lease_seconds,
            )
            if claimed:
                return claimed
        return None

    def complete(
        self,
        *,
        job_id: str,
        lease_token: str,
        receipt: dict[str, Any],
    ) -> bool:
        return _complete_transaction(
            self.db.transaction(),
            self.db.collection(COLLECTION).document(job_id),
            lease_token=lease_token,
            receipt=receipt,
            now=_utcnow(),
        )

    def fail(
        self,
        *,
        job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_after_seconds: int,
    ) -> bool:
        now = _utcnow()
        return _fail_transaction(
            self.db.transaction(),
            self.db.collection(COLLECTION).document(job_id),
            lease_token=lease_token,
            error_code=error_code,
            retryable=retryable,
            next_attempt_at=(now + timedelta(seconds=max(1, retry_after_seconds)) if retryable else None),
            now=now,
        )
