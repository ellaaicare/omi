"""Prepared operator only; specific newly-admitted-source release is required.

No auto-run, HTTP surface, daemon, runtime hook or new credential. Specific future
source release and ordinary safe deployment are separate from this source file.
"""
import asyncio
from google.cloud.firestore_v1 import transactional
from datetime import datetime, timezone
from database.normal_future_source_disposition import (
    OWNER_HASH, prepare_scope, route_matches, scope_ref, string_hash,
)


async def prepare_reviewed_scope(*, db, repository, uid, approval_ref, expected_policy_sha256, protected_uid, request_id, source_id, text_sha256, consumer="hermes", muse_run_id=None, muse_operation_id=None, thin_authority=None):
    if consumer == 'muse':
        # Dedicated Thin proof, no Hermes route lookup and no global policy renewal.
        if thin_authority is None or thin_authority.uid != uid or uid == protected_uid:
            raise ValueError('thin_muse_operator_authority_required')
        from ella.services.thin_cloudbot_mcp import ThinMcpDatabase
        pickup = await asyncio.to_thread(ThinMcpDatabase(db, thin_authority,
            source_id=source_id, run_id=muse_run_id, operation_id=muse_operation_id).pickup)
        marker = pickup['pending']
        return {'source_id': source_id, 'run_id': marker['run_id'],
                'operation_id': marker['operation_id'],
                'expected_snapshot_sha256': marker['expected_snapshot_sha256'],
                'expected_active_summary_version_id': marker['expected_active_summary_version_id'],
                'normal_route_matched': False, 'thin_target_matched': True,
                'scope_installed': False, 'job_processing_invoked': False}
    if thin_authority is not None:
        raise ValueError('thin_hermes_route_forbidden')
    if string_hash(uid) != OWNER_HASH:
        raise ValueError('future_scope_owner_mismatch')
    from ella.services.normal_account_routing import resolve_route
    route = await resolve_route(uid, repository)
    if not route_matches(route):
        raise ValueError('future_scope_existing_route_changed')
    now = datetime.now(timezone.utc)
    result = await asyncio.to_thread(prepare_scope, db.transaction(), db, uid,
                                    now=now, approval_ref=approval_ref, expected_policy_sha256=expected_policy_sha256,
                                    protected_uid=protected_uid,request_id=request_id,source_id=source_id,text_sha256=text_sha256,
                                    consumer=consumer,muse_run_id=muse_run_id,muse_operation_id=muse_operation_id)
    snap = await asyncio.to_thread(scope_ref(db).get, timeout=5, retry=None)
    if not snap.exists or snap.to_dict().get('scope_sha256') != result['scope_sha256']:
        raise ValueError('future_scope_prepare_readback_unknown_no_retry')
    return {**result, 'cutover_utc': snap.create_time.isoformat(),
            'normal_route_matched': True, 'scope_installed': True,
            'job_processing_invoked': False}


@transactional
def retire_reviewed_scope(transaction, db, *, now):
    """Due-only admission retirement; preserve jobs, active leases and results."""
    from database import normal_enrichment_activation as activation
    from database.normal_future_source_disposition import EPOCH, CONTRACT, aware
    ps = activation.reference(db).get(transaction=transaction)
    policy = ps.to_dict() if ps.exists else {}
    snap = scope_ref(db).get(transaction=transaction)
    scope = snap.to_dict() if snap.exists else {}
    if (not aware(now) or not aware(policy.get('expires_at')) or now < policy['expires_at']
            or policy.get('epoch') != EPOCH or scope.get('epoch') != EPOCH
            or scope.get('contract') != CONTRACT or scope.get('expires_at') != policy['expires_at']):
        raise ValueError('one_source_retirement_scope_or_due_mismatch')
    if policy.get('state') == 'paused' and scope.get('state') == 'retired':
        return {'retired': True, 'writes': 0, 'active_reservations': len(policy.get('active', []))}
    if policy.get('state') != 'active' or scope.get('state') != 'active':
        raise ValueError('one_source_retirement_state_changed')
    from database.normal_future_source_disposition import muse_pending_retirement
    pending_update=muse_pending_retirement(transaction,db,scope,now)
    if pending_update is not None:transaction.update(*pending_update)
    transaction.update(activation.reference(db), {'state':'paused','paused_at':now})
    transaction.update(scope_ref(db), {'state':'retired','retired_at':now})
    return {'retired':True,'jobs_or_results_mutated':False,'active_reservations':len(policy.get('active', []))}
