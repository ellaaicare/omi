"""Internal one-source repair of a proven never-dispatched normal sync completion.

No HTTP entry, model dispatch, source replay, old-disposition rearm or quarantine
clear. The owner separately verifies exact old worker/canonical closure artifacts.
Normal singleton claim/preflight/begin/finish remain the only execution path.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from uuid import UUID
import hashlib
from google.cloud.firestore_v1 import transactional
from database import normal_source_disposition as prior

COLLECTION = 'ella_normal_enrichment_completed_source_recovery'


def ref(db, uid, cid):
    key = hashlib.sha256((uid+'\x1f'+cid).encode()).hexdigest()
    return db.collection(COLLECTION).document(key)


@dataclass(frozen=True)
class CompletedSourceRecovery:
    owner_sha256: str
    job_id: str
    conversation_id: str
    transcript_sha256: str
    source_before_sha256: str
    source_after_sha256: str
    job_before_sha256: str
    sync_segment_id: str
    sync_receipt_sha256: str
    completed_at: datetime
    active_summary_version_id: str
    old_job_id: str
    old_conversation_id: str
    old_job_sha256: str
    old_source_sha256: str
    old_tenant_sha256: str
    policy_sha256: str
    epoch: str
    prior_disposition_sha256: str
    closure_artifact_sha256: str
    parent_scope_sha256: str
    budget_freeze_sha256: str
    six_job_preimages: tuple
    not_before: datetime
    expires_at: datetime


def shape(g, uid, now):
    if type(g) is not CompletedSourceRecovery:return False
    try:
        if g.owner_sha256 != hashlib.sha256(uid.encode()).hexdigest():return False
        if any(str(UUID(v))!=v for v in (g.conversation_id,g.old_conversation_id,g.epoch)):return False
        if g.conversation_id==g.old_conversation_id:return False
        if not g.job_id or not g.old_job_id or g.job_id==g.old_job_id or not g.sync_segment_id or not g.active_summary_version_id:return False
        pins=(g.owner_sha256,g.transcript_sha256,g.source_before_sha256,g.source_after_sha256,g.job_before_sha256,g.sync_receipt_sha256,g.old_job_sha256,g.old_source_sha256,g.old_tenant_sha256,g.policy_sha256,g.prior_disposition_sha256,g.closure_artifact_sha256,g.parent_scope_sha256)
        if not all(prior.pin(v) for v in pins) or g.budget_freeze_sha256!=prior.BUDGET_FREEZE:return False
        if any(not isinstance(v,datetime) or v.tzinfo is None for v in (g.completed_at,g.not_before,g.expires_at)):return False
        if not g.completed_at<=g.not_before<=now<g.expires_at:return False
        if not timedelta(0)<g.expires_at-g.not_before<=timedelta(seconds=600):return False
        if g.not_before-g.completed_at>timedelta(hours=2):return False
        return type(g.six_job_preimages) is tuple and len(g.six_job_preimages)==6 and len({j for j,h in g.six_job_preimages})==6 and all(type(j) is str and j and prior.pin(h) for j,h in g.six_job_preimages)
    except (ValueError,TypeError,AttributeError):return False


def frozen_old(transaction, db, uid, g, policy, tenant):
    from database import normal_enrichment_activation as a
    if (tenant.get('uid')!=uid or tenant.get('quarantined_job_id')!=g['old_job_id'] or tenant.get('additional_quarantined_job_ids') or uid not in policy.get('quarantined_tenants',[]) or policy.get('epoch')!=g['epoch']):return False
    snap=prior.ref(db,uid).get(transaction=transaction);d=snap.to_dict() if snap.exists else {}
    if d.get('state')!='consumed' or prior.image(d)!=g['prior_disposition_sha256']:return False
    return prior.old_matches(transaction,db,uid,g)


def exact_source(transaction, db, uid, g, *, installed):
    from database import normal_enrichment_activation as a
    from database.conversations import _prepare_conversation_for_transcript_hash
    from models.hermes_cloud_enrichment_contract import transcript_source
    snap=prior.source_ref(db,uid,g['conversation_id']).get(transaction=transaction);raw=snap.to_dict() if snap.exists else {}
    if prior.image(raw)!=g['source_after_sha256' if installed else 'source_before_sha256']:return False
    c=_prepare_conversation_for_transcript_hash(raw,uid)
    if not isinstance(c,dict) or c.get('id')!=g['conversation_id'] or c.get('uid',uid)!=uid or c.get('source')!='omi' or c.get('status')!='completed' or c.get('discarded') is not False:return False
    if c.get('active_summary_version_id')!=g['active_summary_version_id'] or not c.get('summary_versions'):return False
    if any(c.get(k) for k in ('semantic_producer_executions_v1','semantic_publication_operations','semantic_publication_receipts','semantic_retained_completion_jobs_v1','summary_writeback_receipt','capture_generation','capture_owner_token','capture_owner')):return False
    if installed and c.get('normal_source_completed_at')!=g['completed_at']:return False
    if not installed and c.get('normal_source_completed_at'):return False
    if transcript_source(c)[1]!=g['transcript_sha256']:return False
    sr=db.collection('users').document(uid).collection('sync_v2_segments').document(g['sync_segment_id']).get(transaction=transaction)
    receipt=sr.to_dict() if sr.exists else {}
    return (prior.image(receipt)==g['sync_receipt_sha256'] and receipt.get('state')=='done' and receipt.get('conversation_id')==g['conversation_id'] and receipt.get('reserved_conversation_id')==g['conversation_id'] and receipt.get('completed_at')==g['completed_at'])


@transactional
def install(transaction, db, uid, grant, *, now):
    from database import normal_enrichment_activation as a
    if not shape(grant,uid,now):raise ValueError('completed_source_scope_invalid')
    g=asdict(grant);r=ref(db,uid,g['conversation_id'])
    if r.get(transaction=transaction).exists:raise ValueError('completed_source_already_disposed_no_rearm')
    ps=a.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=a.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    jr=db.collection(a.JOBS).document(g['job_id']);js=jr.get(transaction=transaction);job=js.to_dict() if js.exists else {}
    if not a.valid_policy(policy,now) or prior.image(policy)!=g['policy_sha256'] or prior.image(tenant)!=g['old_tenant_sha256'] or grant.expires_at>policy['expires_at'] or not policy['source_completion_boundary']<=grant.completed_at<=now:raise ValueError('completed_source_policy_drift')
    if not frozen_old(transaction,db,uid,g,policy,tenant) or tenant.get('active_job_id') or not exact_source(transaction,db,uid,g,installed=False):raise ValueError('completed_source_preimage_drift')
    if (prior.image(job)!=g['job_before_sha256'] or job.get('uid')!=uid or job.get('job_id')!=g['job_id'] or job.get('conversation_id')!=g['conversation_id'] or job.get('transcript_sha256')!=g['transcript_sha256'] or type(job.get('account_routing_contract')) is not int or job['account_routing_contract']!=1 or job.get('status')!='pending' or type(job.get('attempt_count')) is not int or job['attempt_count']!=0):raise ValueError('completed_source_job_not_fresh')
    if any(job.get(k) is not None for k in ('lease_token','lease_expires_at','admission_epoch','source_completed_at','dispatch_state','dispatch_epoch','canary_epoch','dispatch_budget','claimed_at','completed_at','receipt','semantic_start','source_consumed','operation','retry_safety')):raise ValueError('completed_source_prior_dispatch_hold')
    # All reads precede these writes. Only one additive disposition, the exact
    # unstarted new job and its existing source's missing completion metadata.
    transaction.set(r,{'version':1,'state':'prepared','grant':g})
    transaction.update(prior.source_ref(db,uid,g['conversation_id']),{'normal_source_completed_at':grant.completed_at})
    transaction.update(jr,{'admission_epoch':grant.epoch,'source_completed_at':grant.completed_at,'dispatch_state':'never_started','retry_safety':None})
    return True


def eligible(transaction, db, current, policy, tenant, now, *, consumed=False, admission=False):
    try:
        uid=current['uid'];cid=current['conversation_id'];r=ref(db,uid,cid);snap=r.get(transaction=transaction);d=snap.to_dict() if snap.exists else {};g=d.get('grant') or {}
        typed=CompletedSourceRecovery(**{**g,'six_job_preimages':tuple(tuple(v) for v in g['six_job_preimages'])})
        if not shape(typed,uid,now) or d.get('version')!=1 or d.get('state')!=('consumed' if consumed else 'prepared'):return None
        if (current.get('job_id')!=g['job_id'] or current.get('transcript_sha256')!=g['transcript_sha256'] or current.get('admission_epoch')!=g['epoch'] or current.get('source_completed_at')!=g['completed_at'] or g['expires_at']>policy['expires_at']):return None
        if not frozen_old(transaction,db,uid,g,policy,tenant) or not exact_source(transaction,db,uid,g,installed=True):return None
        if consumed:
            if d.get('job_id')!=current['job_id'] or d.get('lease_token')!=current['lease_token'] or current.get('attempt_count')!=1:return None
        elif type(current.get('attempt_count')) is not int or current['attempt_count']!=0:return None
        return r
    except (KeyError,ValueError,TypeError,AttributeError):return None
