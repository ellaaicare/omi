"""One durable next-completion canary; no retry, reset, or stale-job replay.

The normal outbox, consent, runtime, source and semantic ledgers remain authority.
This collection budgets dispatch claims, not dollars or provider token usage.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from google.cloud.firestore_v1 import FieldFilter, transactional
from database.hermes_cloud_enrichment_outbox import (
    FirestoreHermesCloudEnrichmentOutbox, _aware, _utcnow,
)

BUDGET_COLLECTION = 'ella_hermes_enrichment_dispatch_budgets'


@dataclass(frozen=True)
class CanaryPolicy:
    epoch: str
    owners: frozenset[str]
    start: datetime
    end: datetime

    def __post_init__(self):
        if str(uuid.UUID(self.epoch)) != self.epoch:
            raise ValueError('dispatch_epoch_invalid')
        if type(self.owners) is not frozenset or not self.owners or any(not isinstance(u, str) or not u.strip() for u in self.owners):
            raise ValueError('dispatch_owners_invalid')
        if (self.start.tzinfo is None or self.end.tzinfo is None
                or not 0 < (self.end-self.start).total_seconds() <= 3600):
            raise ValueError('dispatch_window_invalid')

    @property
    def binding(self):
        raw = json.dumps({'owners': sorted(self.owners), 'start': self.start.isoformat(),
                          'end': self.end.isoformat(), 'claim_limit': 1}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()


def claim_canary(transaction, reference, budget_ref, *, policy, now, lease_seconds):
    """Read both documents before writes; claim and permanent budget are atomic."""
    if not policy.start <= now < policy.end or lease_seconds < 900:
        return None
    snapshot = reference.get(transaction=transaction)
    saved = budget_ref.get(transaction=transaction)
    job = snapshot.to_dict() if snapshot.exists else None
    budget = saved.to_dict() if saved.exists else None
    if budget is not None:
        if budget.get('binding') != policy.binding or budget.get('claim_limit') != 1:
            raise ValueError('dispatch_budget_identity_conflict')
        if type(budget.get('claims')) is not int or budget['claims'] not in {0, 1}:
            raise ValueError('dispatch_budget_invalid')
        if budget['claims'] == 1:
            return None
    # No retries or expired-running reclaims in acceptance. A lost response
    # keeps its original job/semantic START and spent epoch budget forever.
    if (not isinstance(job, dict) or job.get('uid') not in policy.owners
            or job.get('status') != 'pending' or type(job.get('attempt_count')) is not int
            or job['attempt_count'] != 0):
        return None
    created = _aware(job.get('created_at'))
    due = _aware(job.get('next_attempt_at'))
    if created is None or not policy.start <= created <= now or (due and due > now):
        return None
    if any(not isinstance(job.get(k), str) or not job[k] for k in
           ('job_id', 'conversation_id', 'client_interaction_id', 'transcript_sha256')):
        raise ValueError('dispatch_job_identity_invalid')
    if reference.id != job['job_id']:
        raise ValueError('dispatch_job_document_identity_changed')
    from datetime import timedelta
    import secrets
    changes = {'status': 'running', 'lease_token': secrets.token_urlsafe(24),
               'lease_expires_at': now+timedelta(seconds=lease_seconds),
               'attempt_count': 1, 'updated_at': now}
    transaction.set(budget_ref, {'binding': policy.binding, 'claim_limit': 1,
                                'claims': 1, 'claimed_at': now,
                                'job_id_sha256': hashlib.sha256(job['job_id'].encode()).hexdigest()})
    transaction.update(reference, changes)
    return {**job, **changes}


_claim_canary_transaction = transactional(claim_canary)


class BoundedFirestoreEnrichmentOutbox(FirestoreHermesCloudEnrichmentOutbox):
    automatic_retry_allowed = False

    def __init__(self, *, policy, firestore_db=None):
        if firestore_db is None:
            super().__init__()
        else:
            super().__init__(firestore_db)
        self.policy = policy

    def claim_next(self, *, lease_seconds, scan_limit=50):
        now = _utcnow()
        if not self.policy.start <= now < self.policy.end:
            return None
        from database.hermes_cloud_enrichment_outbox import COLLECTION
        # Scope the query, not just its returned result: no foreign owners scan.
        budget = self.db.collection(BUDGET_COLLECTION).document(self.policy.epoch)
        for uid in sorted(self.policy.owners):
            query = (self.db.collection(COLLECTION)
                     .where(filter=FieldFilter('uid', '==', uid))
                     .where(filter=FieldFilter('status', '==', 'pending'))
                     .limit(min(scan_limit, 50)))
            snapshots = list(query.stream(timeout=10, retry=None))
            if len(snapshots) >= min(scan_limit, 50):
                raise ValueError('dispatch_scan_capacity_hold')
            for snapshot in snapshots:
                claimed = _claim_canary_transaction(self.db.transaction(), snapshot.reference,
                    budget, policy=self.policy, now=now, lease_seconds=lease_seconds)
                if claimed:
                    return claimed
        return None
