"""Private, one-request saved-backlog repair. No public endpoint or import I/O.

The authenticated user explicitly requested processing retained generic summaries.
Only pinned never-dispatched blocked jobs are eligible. This does not manufacture
a new first-completion event: it uses each source's existing completion timestamp.
The owner repair is proven by the exact UID document path AND immutable job owner,
with the current original resolver/enrollment proof. No transcript/version rewrite.
Atlas is the sole approved live executor; possession of this module is not access.
"""

from datetime import datetime, timezone
from hashlib import sha256
import asyncio
import json
from pathlib import Path

SPEC_SHA256 = '59091879a32cab719305f14883e6158f94c0e0a8e87b0aed2bf122ac348a1eaf'
AUDITS = 'ella_explicit_saved_backlog_repairs'


class RepairRejected(ValueError):
    pass


class RepairOutcomeUnknown(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise RepairRejected(code)


def specification():
    raw = Path(__file__).with_name('explicit_saved_backlog_repair.expected.json').read_bytes()
    need(sha256(raw).hexdigest() == SPEC_SHA256, 'approved_scope_changed')
    return json.loads(raw)


def plan(
    *,
    spec,
    cid,
    uid,
    proof,
    lifecycle,
    product,
    build_identity,
    account,
    account_created,
    policy,
    tenant,
    raw,
    decoded,
    snapshot_path,
    snapshot_created,
    job,
    active_capture,
    now
):
    """Pure preparation. Caller transaction also verifies absence of both receipts."""
    expected = spec['records'].get(cid)
    need(type(expected) is dict and expected.get('eligible_metadata') is True, 'source_not_in_approved_batch')
    need(type(uid) is str and sha256(uid.encode()).hexdigest() == spec['owner_sha256'], 'wrong_owner')
    need(type(proof) is lifecycle.VerifiedRoute and proof.uid == uid, 'current_verified_route_required')
    need(
        proof.image_sha256 == spec['route']['route_sha256'] == account.get('route_sha256')
        and proof.account_owner_sha256 == spec['route']['account_owner_sha256'] == account.get('account_owner_sha256')
        and proof.profile_sha256 == spec['route']['profile_sha256'] == account.get('profile_sha256'),
        'original_route_changed',
    )
    need(
        account.get('uid') == uid
        and account.get('state') == 'active'
        and account.get('epoch') == spec['account_epoch'],
        'account_authority_changed',
    )
    need(lifecycle.valid_policy(policy, now), 'processing_policy_not_active')
    need(
        not tenant.get('active_job_id') and not any(x.get('uid') == uid for x in policy.get('active', [])),
        'account_worker_busy',
    )
    need(snapshot_path == 'users/' + uid + '/conversations/' + cid, 'source_parent_owner_mismatch')
    need(lifecycle.digest(raw) == expected['source_snapshot_sha256'], 'source_snapshot_changed')
    need(lifecycle.digest(job) == expected['job_snapshot_sha256'], 'job_snapshot_changed')
    need(raw.get('uid') is None and decoded.get('uid') is None, 'owner_repair_no_longer_exact')
    need(active_capture.get('conversation_id') != cid, 'source_still_active')
    need(
        decoded.get('id') == cid
        and decoded.get('status') == 'completed'
        and decoded.get('capture_state') == 'terminal'
        and decoded.get('discarded') is not True
        and not decoded.get('capture_finalization_claim_token'),
        'source_not_saved_terminal',
    )
    need(
        all(
            decoded.get(k) is None or lifecycle.aware(decoded[k]) and decoded[k] <= now
            for k in ['capture_lease_expires_at', 'capture_finalization_lease_expires_at']
        ),
        'source_lease_not_settled',
    )
    need(decoded.get('active_summary_version_id') == expected['stock_version'], 'stock_version_changed')
    versions = [
        v for v in decoded.get('summary_versions', []) if type(v) is dict and v.get('id') == expected['stock_version']
    ]
    need(
        len(versions) == 1
        and versions[0].get('source') == 'omi'
        and versions[0].get('kind') == 'generated'
        and versions[0].get('is_active') is True,
        'source_no_longer_generic',
    )
    need(
        job.get('uid') == uid and job.get('conversation_id') == cid and job.get('job_id') == expected['job_id'],
        'job_owner_or_source_changed',
    )
    need(
        job.get('status') == 'blocked'
        and job.get('dispatch_state') == 'account_processing_held'
        and type(job.get('attempt_count')) is int
        and job['attempt_count'] == 0
        and not any(
            job.get(k)
            for k in [
                'lease_token',
                'lease_expires_at',
                'receipt',
                'semantic_start',
                'source_consumed',
                'completed_at',
                'dispatch_epoch',
                'canary_epoch',
                'dispatch_budget',
            ]
        ),
        'prior_dispatch_or_uncertainty_excluded',
    )
    completed = decoded.get('normal_source_completed_at')
    need(
        lifecycle.aware(now)
        and all(lifecycle.aware(x) for x in [account_created, snapshot_created, completed])
        and max(account_created, policy['source_completion_boundary']) < snapshot_created <= completed <= now
        and job.get('source_completed_at') == completed
        and str(completed) == expected['first_completed_at']
        and str(snapshot_created) == expected['source_created_at'],
        'retained_first_completion_not_proven',
    )
    need(lifecycle.native.sha(cid) not in account.get('excluded_source_ids_sha256', []), 'old_excluded_source')
    # This in-memory projection is derived from the authenticated parent and job,
    # never caller-supplied identity. Only uid is later persisted; no other source field.
    repaired = dict(decoded, uid=uid)
    need(product.source_native(repaired, uid, cid), 'strict_native_provenance_failed')
    binding = product.binding(repaired, uid, cid)
    identity = build_identity(uid=uid, conversation_id=cid, conversation=repaired)
    need(
        binding is not None
        and lifecycle.digest(binding) == expected['binding_sha256']
        and identity.job_id == job['job_id']
        and identity.client_interaction_id == job.get('client_interaction_id') == expected['client_interaction_id']
        and identity.transcript_sha256 == job.get('transcript_sha256') == expected['transcript_sha256'],
        'source_identity_changed',
    )
    marker_id = lifecycle.digest({'uid': uid, 'cid': cid})
    tag = {
        'contract': lifecycle.CONTRACT,
        'account_epoch': account['epoch'],
        'route_sha256': account['route_sha256'],
        'source_created_at': snapshot_created,
        'first_completed_at': completed,
        'binding_sha256': lifecycle.digest(binding),
        'source_binding': binding,
        'capture_generation': repaired['capture_generation'],
        'capture_owner_sha256': lifecycle.native.sha(repaired['capture_owner_token']),
        'marker_id': marker_id,
    }
    marker = {k: job[k] for k in lifecycle.IDENTITY}
    marker.update(
        contract=lifecycle.CONTRACT,
        state='prepared',
        tag=tag,
        epoch=policy['epoch'],
        authorization_basis=spec['contract'],
        explicit_batch_id=spec['batch_id'],
        explicit_request_sha256=sha256(spec['request_ref'].encode()).hexdigest(),
    )
    audit = {
        'contract': spec['contract'],
        'batch_id': spec['batch_id'],
        'owner_sha256': spec['owner_sha256'],
        'conversation_id': cid,
        'job_id': job['job_id'],
        'request_ref_sha256': sha256(spec['request_ref'].encode()).hexdigest(),
        'prepared_at': now,
        'prior_source_sha256': expected['source_snapshot_sha256'],
        'prior_job_sha256': expected['job_snapshot_sha256'],
        'preserved_stock_version': expected['stock_version'],
        'preserved_transcript_sha256': identity.transcript_sha256,
        'retained_first_completed_at': completed,
        'account_epoch': account['epoch'],
        'route_sha256': account['route_sha256'],
        'account_owner_sha256': account['account_owner_sha256'],
        'profile_sha256': account['profile_sha256'],
        'prior_status': job['status'],
        'prior_dispatch_state': job['dispatch_state'],
        'prior_attempt_count': 0,
        'prior_admission_rejection_stage': job.get('admission_rejection_stage'),
        'source_patch_fields': ['uid'],
        'stock_or_transcript_writes': 0,
        'provider_calls_in_preparation': 0,
        'quarantine_writes': 0,
    }
    update = {
        'status': 'pending',
        'dispatch_state': 'never_started',
        'next_attempt_at': now,
        'updated_at': now,
        'admission_epoch': policy['epoch'],
        'account_routing_contract': 1,
        lifecycle.TAG: tag,
        'explicit_saved_backlog_batch': spec['batch_id'],
    }
    return {'uid': uid}, update, marker, audit


def _body(tx, db, uid, cid, proof, now, lifecycle, activation, product, build_identity, decode):
    spec = specification()
    need(cid in spec['records'], 'source_not_in_approved_batch')
    need(sha256(uid.encode()).hexdigest() == spec['owner_sha256'], 'wrong_owner')
    expected = spec['records'][cid]
    ref = db.collection('users').document(uid).collection('conversations').document(cid)
    source = ref.get(transaction=tx, timeout=5, retry=None)
    raw = source.to_dict() if source.exists else {}
    job_ref = db.collection(activation.JOBS).document(expected['job_id'])
    job_snap = job_ref.get(transaction=tx, timeout=5, retry=None)
    from google.cloud.firestore_v1 import FieldFilter

    source_jobs = list(
        db.collection(activation.JOBS)
        .where(filter=FieldFilter('uid', '==', uid))
        .where(filter=FieldFilter('conversation_id', '==', cid))
        .select(['uid', 'conversation_id', 'job_id'])
        .limit(2)
        .get(transaction=tx, timeout=5, retry=None)
    )
    need(len(source_jobs) == 1 and source_jobs[0].id == expected['job_id'], 'additional_or_missing_source_job')
    account = lifecycle.account_ref(db, uid).get(transaction=tx, timeout=5, retry=None)
    tenant = activation.tenant_ref(db, uid).get(transaction=tx, timeout=5, retry=None).to_dict() or {}
    policy = activation.reference(db).get(transaction=tx, timeout=5, retry=None).to_dict() or {}
    need(
        lifecycle.read_account(tx, db, uid, tenant, now, policy=policy) is not None, 'current_account_authority_invalid'
    )
    capture = (
        db.collection('users')
        .document(uid)
        .collection('capture_authority')
        .document('current')
        .get(transaction=tx, timeout=5, retry=None)
        .to_dict()
        or {}
    )
    marker_id = lifecycle.digest({'uid': uid, 'cid': cid})
    marker_ref = db.collection(lifecycle.ADMISSIONS).document(marker_id)
    audit_id = lifecycle.digest({'batch': spec['batch_id'], 'uid': uid, 'cid': cid})
    audit_ref = db.collection(AUDITS).document(audit_id)
    need(not marker_ref.get(transaction=tx, timeout=5, retry=None).exists, 'existing_admission_no_replay')
    need(not audit_ref.get(transaction=tx, timeout=5, retry=None).exists, 'explicit_operation_already_prepared')
    source_patch, job_patch, marker, audit = plan(
        spec=spec,
        cid=cid,
        uid=uid,
        proof=proof,
        lifecycle=lifecycle,
        product=product,
        build_identity=build_identity,
        account=account.to_dict() or {},
        account_created=account.create_time,
        policy=policy,
        tenant=tenant,
        raw=raw,
        decoded=decode(raw, uid),
        snapshot_path=source.reference.path,
        snapshot_created=source.create_time,
        job=job_snap.to_dict() or {},
        active_capture=capture,
        now=now,
    )
    # Four atomic writes; no provider action in this function. The ordinary worker
    # may claim the authorized pending job after commit, with its unchanged gates.
    tx.update(ref, source_patch)
    tx.update(job_ref, job_patch)
    tx.create(marker_ref, marker)
    tx.create(audit_ref, audit)
    return {
        'status': 'prepared_for_existing_worker',
        'cid': cid,
        'job_id': expected['job_id'],
        'batch_id': spec['batch_id'],
        'provider_outcome': 'not_yet_observed',
    }


def _one_transaction(db, uid, cid, proof, now, lifecycle, activation, product, build_identity, decode):
    tx = db.transaction(max_attempts=1)
    api = db._firestore_api
    begun = api.begin_transaction(
        request={'database': db._database_string, 'options': tx._options_protobuf(None)},
        metadata=db._rpc_metadata,
        retry=None,
        timeout=5,
    )
    tx._id = begun.transaction
    try:
        result = _body(tx, db, uid, cid, proof, now, lifecycle, activation, product, build_identity, decode)
    except BaseException:
        try:
            api.rollback(
                request={'database': db._database_string, 'transaction': tx._id},
                metadata=db._rpc_metadata,
                retry=None,
                timeout=5,
            )
        except Exception:
            pass
        finally:
            tx._clean_up()
        raise
    try:
        api.commit(
            request={'database': db._database_string, 'transaction': tx._id, 'writes': tx._write_pbs},
            metadata=db._rpc_metadata,
            retry=None,
            timeout=10,
        )
    except BaseException:
        tx._clean_up()
        raise RepairOutcomeUnknown('commit_unknown_readonly_reconciliation_no_retry') from None
    tx._clean_up()
    return result


async def prepare_one(db, *, uid, cid):
    """Atlas-only private entry after approved exact deployment and route checks.

    One record first; observe same-source/current-version outcome before the next.
    No public API, callback dispatch, policy/enrollment edit or automatic batch loop.
    Lost/cancelled result after thread submission is UNKNOWN: do not invoke again.
    """
    from database import normal_account_lifecycle as lifecycle, normal_enrichment_activation as activation
    from database import normal_product_capture_provenance as product
    from database.conversations import _prepare_conversation_for_read
    from models.hermes_cloud_enrichment_contract import build_enrichment_identity
    from ella.services.ai_consent import assert_current_ai_consent

    need(sha256(uid.encode()).hexdigest() == specification()['owner_sha256'], 'wrong_owner')
    need(assert_current_ai_consent(uid) == uid, 'current_consent_required')
    proof = await asyncio.wait_for(lifecycle.verify_route(uid, None), 20)
    return await asyncio.to_thread(
        _one_transaction,
        db,
        uid,
        cid,
        proof,
        datetime.now(timezone.utc),
        lifecycle,
        activation,
        product,
        build_enrichment_identity,
        _prepare_conversation_for_read,
    )
