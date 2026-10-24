"""Private v4 graph fence for effect entry and atomic summary CAS.

Existing consent, source, model and destination validators remain authoritative.
This adds exact worker-attempt binding; it never creates authority or effects.
"""

from datetime import datetime, timezone

from google.cloud.firestore_v1 import transactional

from database._client import db
from database import normal_enrichment_activation as activation
from database import normal_account_lifecycle as lifecycle
from database import enrichment_job_isolation as isolation
from database import enrichment_attempt_fence as fence
from database.normal_future_source_disposition import _worker_job


def require_in_transaction(transaction, database, expected, uid, cid):
    ps = activation.reference(database).get(transaction=transaction, timeout=5, retry=None)
    policy = ps.to_dict() if ps.exists else {}
    if not isolation.enabled(policy):
        if (
            type(expected) is dict
            and isolation.FENCE_FIELD in expected
            or policy.get('mode') == lifecycle.MODE
            and not lifecycle.policy_shape(policy)
        ):
            raise fence.FenceRejected('graph_policy_version_changed')
        return  # Existing v3 and legacy validators remain unchanged.
    now = datetime.now(timezone.utc)
    if (
        not activation.valid_policy(policy, now)
        or type(expected) is not dict
        or expected.get('uid') != uid
        or expected.get('conversation_id') != cid
    ):
        raise fence.FenceRejected('graph_worker_required')
    snap = (
        database.collection(activation.JOBS)
        .document(expected['job_id'])
        .get(transaction=transaction, timeout=5, retry=None)
    )
    job = snap.to_dict() if snap.exists else {}
    if (
        job.get('status') != 'running'
        or job.get('dispatch_state') != 'started_unknown'
        or job.get('lease_token') != expected.get('lease_token')
        or job.get('attempt_count') != expected.get('attempt_count')
        or job.get('admission_epoch') != policy['epoch']
        or job.get('lease_expires_at') is None
        or job['lease_expires_at'] <= now
        or any(job.get(k) != expected.get(k) for k in activation.IDENTITY)
        or not any(
            item.get('uid') == uid
            and item.get('job_id') == job.get('job_id')
            and item.get('lease_token') == job.get('lease_token')
            for item in policy['active']
        )
    ):
        raise fence.FenceRejected('graph_attempt_not_active')
    ts = activation.tenant_ref(database, uid).get(transaction=transaction, timeout=5, retry=None)
    tenant = ts.to_dict() if ts.exists else {}
    authority = lifecycle.read_account(transaction, database, uid, tenant, now, policy=policy)
    if (
        authority is None
        or tenant.get('active_job_id') != job['job_id']
        or tenant.get('queue_resource_hold')
        or any(item['uid'] == uid for item in policy['queue_unresolved_resources'])
        or job.get(lifecycle.TAG, {}).get('account_epoch') != authority[0]['epoch']
        or job.get(lifecycle.TAG, {}).get('route_sha256') != authority[0]['route_sha256']
        or isolation.replay_forbidden(transaction, database, job, authority[0], tenant)
        or not fence.callback_allowed(job.get(isolation.FENCE_FIELD), isolation.attempt(job))
    ):
        raise fence.FenceRejected('graph_authority_changed')


@transactional
def _read_guard(transaction, database, expected, uid, cid):
    require_in_transaction(transaction, database, expected, uid, cid)


def require_current(uid, cid):
    # START before a send is a read of the same job record closed by finish.
    # A closure after this check makes the already-admitted remote operation
    # uncertain; its whole-graph resource reservation cannot be discarded.
    return _read_guard(db.transaction(), db, _worker_job.get(), uid, cid)


def make_cas_guard(uid, cid):
    expected = _worker_job.get()

    def guard(transaction):
        # Called INSIDE the actual source-summary transaction, before writes.
        # A concurrent close conflicts with this job read and retries/rejects.
        require_in_transaction(transaction, db, expected, uid, cid)

    return guard
