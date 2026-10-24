"""Private reviewed-operator policy CAS; NOT a runnable launcher or endpoint.

Uses the existing caller-supplied, already-bootstrapped database authority.
No capture read, credential, account mutation, job mutation or network dispatch.
Resource-closure proofs must be independently verified metadata receipt hashes;
this helper does not manufacture them or infer cleanup from elapsed time.
"""

from dataclasses import dataclass

from google.cloud.firestore_v1 import transactional

from database import normal_enrichment_activation as activation
from database import normal_account_lifecycle as lifecycle
from database import enrichment_job_isolation as isolation
from database import enrichment_attempt_fence as fence

ARCHIVES = 'ella_queue_policy_migrations_v1'
CONTRACT = 'ella-queue-v4-migration-v1'


@dataclass(frozen=True)
class ReviewedOwnerSnapshot:
    route: lifecycle.VerifiedRoute
    account_sha256: str
    tenant_sha256: str
    held_preimages: dict
    terminal_resource_receipts: dict


@dataclass(frozen=True)
class ReviewedResourceSettlement:
    route: lifecycle.VerifiedRoute
    job_id: str
    tombstone_sha256: str
    tenant_sha256: str
    terminal_receipt_sha256: str
    account_sha256: str
    job_sha256: str
    migration_archive_id: str | None = None


@transactional
def upgrade(transaction, db, *, expected_policy_sha256, owners, authorization_ref, now):
    """Atomically enter v4 with claims CLOSED and existing epoch unchanged.

    A concurrent v3 claim conflicts on the policy read. If any worker attempt
    is still active, return a precise hold; an active capture is irrelevant.
    This is a global code-policy migration, not personal-backlog admission.
    """
    snapshot = activation.reference(db).get(transaction=transaction, timeout=5, retry=None)
    old = snapshot.to_dict() if snapshot.exists else {}
    if (
        not lifecycle.valid_policy(old, now)
        or old.get('version') != 3
        or isolation.digest(old) != expected_policy_sha256
        or any(key in old for key in ('queue_isolation', 'queue_unresolved_resources'))
        or type(authorization_ref) is not str
        or not authorization_ref
        or old.get('lifecycle_authorization_ref') != authorization_ref
    ):
        raise fence.FenceRejected('migration_policy_authority_changed')
    if old['active']:
        raise fence.FenceRejected('migration_worker_attempts_not_drained')
    if type(owners) is not tuple or len(owners) > 16 or any(type(p) is not ReviewedOwnerSnapshot for p in owners):
        raise fence.FenceRejected('migration_reviewed_owner_snapshots_required')
    supplied = {p.route.uid: p for p in owners if type(p.route) is lifecycle.VerifiedRoute}
    if len(supplied) != len(owners) or set(supplied) != set(old['quarantined_tenants']):
        raise fence.FenceRejected('migration_legacy_owner_inventory_changed')
    archive = db.collection(ARCHIVES).document(expected_policy_sha256)
    if archive.get(transaction=transaction, timeout=5, retry=None).exists:
        raise fence.FenceRejected('migration_already_applied_no_replay')
    unresolved = []
    receipts = {}
    legacy_job_preimages = {}
    for uid in sorted(supplied):
        proof = supplied[uid]
        tenant_snapshot = activation.tenant_ref(db, uid).get(transaction=transaction, timeout=5, retry=None)
        tenant = tenant_snapshot.to_dict() if tenant_snapshot.exists else {}
        account_snapshot = lifecycle.account_ref(db, uid).get(transaction=transaction, timeout=5, retry=None)
        account = account_snapshot.to_dict() if account_snapshot.exists else {}
        if (
            isolation.digest(tenant) != proof.tenant_sha256
            or tenant.get('uid') != uid
            or tenant.get('active_job_id')
            or tenant.get('queue_resource_hold')
            or isolation.digest(account) != proof.account_sha256
            or account.get('uid') != uid
            or account.get('state') not in {'active', 'paused'}
            or account.get('contract') != lifecycle.CONTRACT
            or not lifecycle.native.uuid(account.get('epoch'))
            or any(
                not fence._sha(value)
                for value in (proof.route.image_sha256, proof.route.account_owner_sha256, proof.route.profile_sha256)
            )
            or account.get('route_sha256') != proof.route.image_sha256
            or account.get('account_owner_sha256') != proof.route.account_owner_sha256
            or account.get('profile_sha256') != proof.route.profile_sha256
            or type(proof.held_preimages) is not dict
            or type(proof.terminal_resource_receipts) is not dict
            or not set(proof.terminal_resource_receipts) <= set(proof.held_preimages)
            or any(not fence._sha(v) for v in proof.terminal_resource_receipts.values())
        ):
            raise fence.FenceRejected('migration_owner_route_or_resource_proof_changed')
        holds = lifecycle.held_preimages(transaction, db, uid, tenant)
        if holds != proof.held_preimages:
            raise fence.FenceRejected('migration_legacy_quarantine_changed')
        for jid in sorted(holds):
            legacy_job_preimages[jid] = holds[jid]
            if jid in proof.terminal_resource_receipts:
                receipts[jid] = proof.terminal_resource_receipts[jid]
                continue
            row = (
                db.collection(activation.JOBS)
                .document(jid)
                .get(transaction=transaction, timeout=5, retry=None)
                .to_dict()
            )
            operation = row.get('operation') if type(row) is dict else None
            token = operation.get('lease_token') if type(operation) is dict else None
            if type(token) is not str or not token:
                raise fence.FenceRejected('migration_uncertain_resource_identity_missing')
            unresolved.append(
                {
                    'uid': uid,
                    'job_id': jid,
                    'route_sha256': proof.route.image_sha256,
                    'lease_sha256': fence.sha256(token.encode()).hexdigest(),
                }
            )
    new = {
        **old,
        'version': 4,
        'queue_isolation': {'contract': isolation.CONTRACT, 'maintenance_revision': 0, 'claims_open': False},
        'queue_unresolved_resources': unresolved,
        'quotas': {**old['quotas'], 'max_attempts': 3},
    }
    if not lifecycle.policy_shape(new):
        raise fence.FenceRejected('migration_resource_capacity_or_schema_invalid')
    archive_record = {
        'contract': CONTRACT,
        'policy_before': old,
        'policy_after_sha256': isolation.digest(new),
        'created_at': now,
        'terminal_resource_receipts': receipts,
        'legacy_job_preimages': legacy_job_preimages,
        'resources_carried': unresolved,
        'owner_snapshot_sha256': {
            uid: isolation.digest(
                {
                    'account': supplied[uid].account_sha256,
                    'tenant': supplied[uid].tenant_sha256,
                    'holds': supplied[uid].held_preimages,
                }
            )
            for uid in supplied
        },
    }
    transaction.create(archive, archive_record)
    transaction.set(activation.reference(db), new)
    return {
        'state': 'v4_claims_closed',
        'policy_sha256': isolation.digest(new),
        'unresolved_resources': len(unresolved),
        'source_job_account_tenant_writes': 0,
    }


@transactional
def rollback_before_open(transaction, db, *, archive_id, expected_policy_sha256, now):
    """Rollback only untouched, never-opened v4. After open, retain v4 readers."""
    current_snapshot = activation.reference(db).get(transaction=transaction, timeout=5, retry=None)
    current = current_snapshot.to_dict() if current_snapshot.exists else {}
    archive = db.collection(ARCHIVES).document(archive_id).get(transaction=transaction, timeout=5, retry=None)
    record = archive.to_dict() if archive.exists else {}
    if (
        record.get('contract') != CONTRACT
        or not isolation.shape(current)
        or isolation.digest(current) != expected_policy_sha256
        or record.get('policy_after_sha256') != expected_policy_sha256
        or current['queue_isolation']['claims_open'] is not False
        or current['queue_isolation']['maintenance_revision'] != 0
        or current['active']
        or not lifecycle.valid_policy(record.get('policy_before'), now)
    ):
        raise fence.FenceRejected('migration_rollback_not_pristine')
    transaction.set(activation.reference(db), record['policy_before'])
    return {'state': 'prior_policy_restored', 'source_job_account_tenant_writes': 0}


@transactional
def settle_resource(transaction, db, *, proof, expected_policy_sha256, now):
    """Private exact-resource cleanup; no job retry, remap or output application.

    Independently reviewed whole-graph terminal receipt is a required external
    capability. A correctly shaped hash does not establish its authenticity.
    Legacy resources bind their migration archive; v4 binds its immutable fence.
    """
    if (
        type(proof) is not ReviewedResourceSettlement
        or type(proof.route) is not lifecycle.VerifiedRoute
        or not lifecycle.aware(now)
        or type(proof.job_id) is not str
        or not proof.job_id
        or any(
            not fence._sha(value)
            for value in (
                proof.terminal_receipt_sha256,
                proof.account_sha256,
                proof.job_sha256,
                proof.tenant_sha256,
                proof.route.image_sha256,
                proof.route.account_owner_sha256,
                proof.route.profile_sha256,
            )
        )
    ):
        raise fence.FenceRejected('settlement_reviewed_receipt_required')
    legacy = proof.migration_archive_id is not None
    if (
        legacy
        and (not fence._sha(proof.migration_archive_id) or proof.tombstone_sha256 is not None)
        or not legacy
        and not fence._sha(proof.tombstone_sha256)
    ):
        raise fence.FenceRejected('settlement_anchor_invalid')
    uid = proof.route.uid
    ps = activation.reference(db).get(transaction=transaction, timeout=5, retry=None)
    policy = ps.to_dict() if ps.exists else {}
    if (
        not lifecycle.policy_shape(policy)
        or not isolation.shape(policy)
        or isolation.digest(policy) != expected_policy_sha256
    ):
        raise fence.FenceRejected('settlement_policy_changed')
    account_snapshot = lifecycle.account_ref(db, uid).get(transaction=transaction, timeout=5, retry=None)
    account = account_snapshot.to_dict() if account_snapshot.exists else {}
    tr = activation.tenant_ref(db, uid)
    ts = tr.get(transaction=transaction, timeout=5, retry=None)
    tenant = ts.to_dict() if ts.exists else {}
    js = db.collection(activation.JOBS).document(proof.job_id).get(transaction=transaction, timeout=5, retry=None)
    job = js.to_dict() if js.exists else {}
    if (
        isolation.digest(account) != proof.account_sha256
        or isolation.digest(job) != proof.job_sha256
        or job.get('uid') != uid
        or job.get('job_id') != proof.job_id
        or job.get('status') != 'quarantined'
        or job.get('dispatch_state') != 'unknown_quarantined'
        or job.get('lease_token') is not None
        or job.get('lease_expires_at') is not None
        or tenant.get('uid') != uid
        or tenant.get('active_job_id')
        or isolation.digest(tenant) != proof.tenant_sha256
        or account.get('uid') != uid
        or account.get('contract') != lifecycle.CONTRACT
        or not lifecycle.native.uuid(account.get('epoch'))
        or account.get('route_sha256') != proof.route.image_sha256
        or account.get('account_owner_sha256') != proof.route.account_owner_sha256
        or account.get('profile_sha256') != proof.route.profile_sha256
    ):
        raise fence.FenceRejected('settlement_owner_or_job_changed')
    operation = job.get('operation')
    token = operation.get('lease_token') if type(operation) is dict else None
    if type(token) is not str or not token:
        raise fence.FenceRejected('settlement_attempt_identity_missing')
    resource = {
        'uid': uid,
        'job_id': proof.job_id,
        'lease_sha256': fence.sha256(token.encode()).hexdigest(),
        'route_sha256': proof.route.image_sha256,
    }
    if policy['queue_unresolved_resources'].count(resource) != 1:
        raise fence.FenceRejected('settlement_resource_inventory_changed')
    ref = isolation.tombstone_ref(db, job)
    if legacy:
        snap = (
            db.collection(ARCHIVES)
            .document(proof.migration_archive_id)
            .get(transaction=transaction, timeout=5, retry=None)
        )
        archive = snap.to_dict() if snap.exists else {}
        held_ids = ([tenant['quarantined_job_id']] if tenant.get('quarantined_job_id') else []) + tenant.get(
            'additional_quarantined_job_ids', []
        )
        if (
            archive.get('contract') != CONTRACT
            or resource not in archive.get('resources_carried', [])
            or archive.get('legacy_job_preimages', {}).get(proof.job_id) != proof.job_sha256
            or proof.job_id not in held_ids
            or isolation.FENCE_FIELD in job
            or tenant.get('queue_resource_hold')
        ):
            raise fence.FenceRejected('settlement_legacy_anchor_changed')
        audit_identity = {
            **resource,
            'job_preimage_sha256': proof.job_sha256,
            'migration_archive_id': proof.migration_archive_id,
        }
    else:
        qs = ref.get(transaction=transaction, timeout=5, retry=None)
        quarantine = qs.to_dict() if qs.exists else {}
        identity = quarantine.get('identity')
        hold = tenant.get('queue_resource_hold')
        prior = isolation.attempt({**job, 'lease_token': token})
        fence._require(job.get(isolation.FENCE_FIELD), prior)
        if (
            isolation.digest(quarantine) != proof.tombstone_sha256
            or quarantine.get('contract') != isolation.CONTRACT
            or identity != job[isolation.FENCE_FIELD]['key']
            or job[isolation.FENCE_FIELD]['phase'] != 'closed'
            or job[isolation.FENCE_FIELD]['outcome'] != 'unknown_quarantined'
            or type(hold) is not dict
            or hold.get('identity') != identity
            or hold.get('tombstone_id') != ref.id
        ):
            raise fence.FenceRejected('settlement_resource_identity_changed')
        audit_identity = identity
    audit = db.collection(ARCHIVES).document('settled-' + ref.id)
    if audit.get(transaction=transaction, timeout=5, retry=None).exists:
        raise fence.FenceRejected('settlement_already_recorded_no_replay')
    transaction.create(
        audit,
        {
            'contract': CONTRACT,
            'action': 'settle_resource_only',
            'created_at': now,
            'identity': audit_identity,
            'terminal_receipt_sha256': proof.terminal_receipt_sha256,
            'tombstone_sha256': proof.tombstone_sha256,
            'job_writes': 0,
        },
    )
    transaction.update(
        activation.reference(db),
        {'queue_unresolved_resources': [item for item in policy['queue_unresolved_resources'] if item != resource]},
    )
    if not legacy:
        transaction.update(tr, {'queue_resource_hold': None})
    return {'state': 'resource_settled_job_still_quarantined', 'job_writes': 0, 'provider_calls': 0}
