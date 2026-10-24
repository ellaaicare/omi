"""Durable Firestore outbox for OMI conversation enrichment delivery."""

from __future__ import annotations

from ella.services.normal_account_routing import activated_subject

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud.firestore_v1 import FieldFilter, transactional

from database._client import db
from database import normal_enrichment_activation as activation
from database import enrichment_job_isolation as isolation

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
    account_routing_contract: int | None = None,
    activation_db=None,
) -> Optional[dict[str, Any]]:
    snapshot = reference.get(transaction=transaction)
    if not snapshot.exists:
        return None
    current = snapshot.to_dict() or {}
    if account_routing_contract is not None and (
        type(current.get('account_routing_contract')) is not int
        or current['account_routing_contract'] != account_routing_contract
    ):
        return None
    if account_routing_contract is not None:
        # Strict first activation only. Legacy/default retry policy is untouched.
        if (
            current.get('status') != 'pending'
            or type(current.get('attempt_count')) is not int
            or current['attempt_count'] != 0
            or current.get('lease_token') is not None
            or current.get('lease_expires_at') is not None
            or any(
                current.get(k)
                for k in (
                    'dispatch_epoch',
                    'canary_epoch',
                    'dispatch_budget',
                    'claimed_at',
                    'completed_at',
                    'receipt',
                    'semantic_start',
                    'source_consumed',
                )
            )
        ):
            return None
    status = current.get("status")
    next_attempt_at = _aware(current.get("next_attempt_at"))
    lease_expires_at = _aware(current.get("lease_expires_at"))
    ready = status in {"pending", "retryable"} and (next_attempt_at is None or next_attempt_at <= now)
    lease_expired = status == "running" and (lease_expires_at is None or lease_expires_at <= now)
    if not ready and not lease_expired:
        return None

    lease_token = secrets.token_urlsafe(24)
    activation_claim = None
    if account_routing_contract is not None:
        if activation_db is None or any(
            not isinstance(current.get(k), str) or not current[k]
            for k in ('job_id', 'uid', 'conversation_id', 'client_interaction_id', 'transcript_sha256')
        ):
            return None
        activation_claim = activation.claim_in_transaction(
            transaction, activation_db, current, now=now, lease_token=lease_token
        )
        if activation_claim is None:
            return None
    claimed = {
        **current,
        "status": "running",
        "lease_token": lease_token,
        "lease_expires_at": now + timedelta(seconds=lease_seconds),
        "attempt_count": int(current.get("attempt_count") or 0) + 1,
        "updated_at": now,
    }
    if activation_claim is not None:
        activation_ref, activation_update = activation_claim
        transaction.update(activation_ref, activation_update)
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
            **({'account_routing_contract': 1} if activated_subject(uid) else {}),
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


class NormalAccountEnrichmentOutbox(FirestoreHermesCloudEnrichmentOutbox):
    automatic_retry_allowed = False  # Only verified preflight retries below.
    account_routing_contract = 1

    def _query(self, scan_limit, epoch):
        return (
            self.db.collection(COLLECTION)
            .where(filter=FieldFilter('account_routing_contract', '==', 1))
            .where(filter=FieldFilter('admission_epoch', '==', epoch))
            .where(filter=FieldFilter('status', 'in', ['pending', 'retryable']))
            .order_by('next_attempt_at')
            .limit(min(50, max(1, scan_limit)))
        )

    def dispatch_ready(self):
        return activation.valid_policy(activation.read_policy(self.db), _utcnow())

    def maintenance_closed(self):
        policy = activation.read_policy(self.db)
        return (
            isolation.enabled(policy)
            and activation.valid_policy(policy, _utcnow())
            and not isolation.claims_open(policy)
        )

    def validate_readonly(self):
        policy = activation.read_policy(self.db)
        if not activation.valid_policy(policy, _utcnow()):
            return False
        list(self._query(1, policy['epoch']).stream(timeout=5, retry=None))
        return True

    def quarantine_expired(self):
        policy = activation.read_policy(self.db)
        now = _utcnow()
        for operation in policy.get('active', [])[:4]:
            expires = _aware(operation.get('lease_expires_at'))
            if expires is not None and expires <= now:
                activation.finish(
                    self.db.transaction(),
                    self.db,
                    self.db.collection(COLLECTION).document(operation['job_id']),
                    lease_token=operation['lease_token'],
                    now=now,
                    outcome='unknown',
                    error_code='normal_enrichment_restart_expired_unknown',
                )

    def claim_next(self, *, lease_seconds, scan_limit=50):
        self.quarantine_expired()
        now = _utcnow()
        policy = activation.read_policy(self.db)
        if not activation.valid_policy(policy, now):
            return None
        if isolation.enabled(policy):
            if not isolation.claims_open(policy):
                return None
            if getattr(self, '_scan_epoch', None) != policy['epoch']:
                self._scan_epoch = policy['epoch']
                self._scan_cursor = None
            # A bounded rolling metadata scan cannot stay stuck forever behind
            # >50 old ineligible jobs. Cursor is not authority or a job claim.
            for _ in range(4):
                query = self._query(scan_limit, policy['epoch'])
                cursor = getattr(self, '_scan_cursor', None)
                if cursor is not None:
                    query = query.start_after(cursor)
                page = list(query.stream(timeout=5, retry=None))
                if not page:
                    self._scan_cursor = None
                    return None
                for snapshot in page:
                    claimed = activation.claim(
                        self.db.transaction(), self.db, snapshot.reference, now=now, lease_seconds=lease_seconds
                    )
                    self._scan_cursor = snapshot
                    if claimed:
                        return claimed
            return None  # Partial scan; never an all-queue absence claim.
        for snapshot in self._query(scan_limit, policy['epoch']).stream(timeout=5, retry=None):
            claimed = activation.claim(
                self.db.transaction(), self.db, snapshot.reference, now=now, lease_seconds=lease_seconds
            )
            if claimed:
                return claimed
        return None

    def begin_dispatch(self, *, job_id, lease_token):
        return activation.begin(
            self.db.transaction(),
            self.db,
            self.db.collection(COLLECTION).document(job_id),
            lease_token=lease_token,
            now=_utcnow(),
        )

    def finish_dispatch(self, *, job_id, lease_token, outcome, error_code='', receipt=None):
        return activation.finish(
            self.db.transaction(),
            self.db,
            self.db.collection(COLLECTION).document(job_id),
            lease_token=lease_token,
            now=_utcnow(),
            outcome=outcome,
            error_code=error_code,
            receipt=receipt,
        )

    def pause(self):
        return activation.pause(self.db.transaction(), activation.reference(self.db), now=_utcnow())

    def job_state(self, *, uid, job_id):
        snapshot = self.db.collection(COLLECTION).document(job_id).get(timeout=5, retry=None)
        job = snapshot.to_dict() if snapshot.exists else {}
        if job.get('uid') != uid:
            return None
        return {
            k: job.get(k)
            for k in (
                'job_id',
                'status',
                'attempt_count',
                'last_error_code',
                'next_attempt_at',
                'operator_action',
                'dispatch_state',
            )
        }

    def tenant_processing_state(self, uid):
        policy = activation.read_policy(self.db)
        if isolation.enabled(policy):
            return activation.lifecycle.processing_status(self.db, uid, now=_utcnow())
        now = _utcnow()
        if not activation.valid_policy(policy, now):
            return {'state': 'paused', 'operator_action': 'owner_review_activation_policy'}
        snapshot = activation.tenant_ref(self.db, uid).get(timeout=5, retry=None)
        tenant = snapshot.to_dict() if snapshot.exists else {}
        if tenant and tenant.get('uid') != uid:
            return {'state': 'held', 'operator_action': 'owner_review_tenant_identity'}
        if tenant.get('quarantined_job_id'):
            return {
                'state': 'quarantined',
                'operator_action': 'reconcile_exact_operation_readback_and_owned_provider_cleanup',
            }
        if tenant.get('active_job_id'):
            return {'state': 'processing', 'operator_action': None}
        hour = int(now.timestamp()) // 3600
        q = policy['quotas']
        if len(policy['quarantined_tenants']) + len(policy['active']) >= q['max_quarantined_tenants']:
            return {'state': 'capacity_hold', 'operator_action': 'reconcile_quarantined_provider_capacity'}
        if (
            policy['hour'] == hour
            and policy['hour_claims'] >= q['max_claims_per_hour']
            or tenant.get('hour') == hour
            and tenant.get('hour_claims', 0) >= q['max_tenant_claims_per_hour']
        ):
            return {
                'state': 'rate_limited',
                'operator_action': None,
                'retry_after_ms': max(1, int(((hour + 1) * 3600 - now.timestamp()) * 1000)),
            }
        return {'state': 'available', 'operator_action': None}
