"""Internal prospective single-source exception; never resolves old quarantine.

No HTTP/header/model input. The owner must independently qualify closure artifacts
and the exact caller scope before invoking install with this exact typed grant.
Ordinary admission/claim/begin remain the sole execution path.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from uuid import UUID
import hashlib
import json
from google.cloud.firestore_v1 import transactional

COLLECTION='ella_normal_enrichment_next_source_disposition'
BUDGET_FREEZE='5b2689a366b6ee51e9cab605d3f678478b072637df57c9b065f47900a2b20da0'


def image(value):
    def canonical(v):
        # Bytes have a non-JSON type tag, distinct from every prior JSON value.
        # All nonbytes retain exact prior canonical bytes, including datetime.
        if type(v) is bytes:return '!storage_bytes('+v.hex()+')'
        if isinstance(v,datetime):
            if v.tzinfo is None:raise ValueError('disposition_naive_time')
            return canonical({'$datetime':v.isoformat()})
        if type(v) is dict:
            if any(type(k) is not str for k in v):raise ValueError('disposition_key')
            return '{'+','.join(json.dumps(k)+':'+canonical(v[k]) for k in sorted(v))+'}'
        if type(v) is list:return '['+','.join(canonical(x) for x in v)+']'
        if v is None or type(v) in (str,bool,int,float):return json.dumps(v,allow_nan=False,separators=(',',':'))
        raise ValueError('disposition_image_type')
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ref(db,uid):
    # One lifetime reviewed disposition per quarantined tenant, no expiry re-arm.
    return db.collection(COLLECTION).document(hashlib.sha256(uid.encode()).hexdigest())


def source_ref(db,uid,cid):return db.collection('users').document(uid).collection('conversations').document(cid)


def pin(value):return type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)


@dataclass(frozen=True)
class NextSourceDisposition:
    owner_sha256:str
    old_job_id:str
    old_conversation_id:str
    old_job_sha256:str
    old_source_sha256:str
    old_tenant_sha256:str
    policy_sha256:str
    epoch:str
    new_conversation_id:str
    new_transcript_sha256:str
    parent_scope_sha256:str
    budget_freeze_sha256:str
    closure_artifact_sha256:str
    generator_request_sha256:str
    verifier_request_sha256:str
    generator_worker_receipt_sha256:str
    verifier_worker_receipt_sha256:str
    generator_canonical_sha256:str
    verifier_canonical_sha256:str
    six_job_preimages:tuple
    not_before:datetime
    expires_at:datetime


def grant_shape(g,uid,now):
    if type(g) is not NextSourceDisposition:return False
    if g.owner_sha256!=hashlib.sha256(uid.encode()).hexdigest() or g.budget_freeze_sha256!=BUDGET_FREEZE:return False
    pins=(g.owner_sha256,g.old_job_sha256,g.old_source_sha256,g.old_tenant_sha256,g.policy_sha256,g.new_transcript_sha256,g.parent_scope_sha256,g.closure_artifact_sha256,g.generator_request_sha256,g.verifier_request_sha256,g.generator_worker_receipt_sha256,g.verifier_worker_receipt_sha256,g.generator_canonical_sha256,g.verifier_canonical_sha256)
    if not all(pin(x) for x in pins):return False
    try:
        if any(str(UUID(x))!=x for x in (g.epoch,g.old_conversation_id,g.new_conversation_id)):return False
        if g.old_conversation_id==g.new_conversation_id:return False
        if not g.old_job_id or not isinstance(g.not_before,datetime) or not isinstance(g.expires_at,datetime):return False
        if g.not_before.tzinfo is None or g.expires_at.tzinfo is None or not g.not_before<=now<g.expires_at:return False
        if not timedelta(0)<g.expires_at-g.not_before<=timedelta(seconds=600):return False
        if type(g.six_job_preimages) is not tuple or len(g.six_job_preimages)!=6:return False
        return len({j for j,h in g.six_job_preimages})==6 and all(type(j) is str and j and pin(h) for j,h in g.six_job_preimages)
    except (ValueError,TypeError,AttributeError):return False


def old_matches(transaction,db,uid,g):
    from database import normal_enrichment_activation as a
    old=db.collection(a.JOBS).document(g['old_job_id']).get(transaction=transaction)
    source=source_ref(db,uid,g['old_conversation_id']).get(transaction=transaction)
    od=old.to_dict() if old.exists else {}
    sd=source.to_dict() if source.exists else {}
    if (image(od)!=g['old_job_sha256'] or image(sd)!=g['old_source_sha256'] or od.get('uid')!=uid or od.get('conversation_id')!=g['old_conversation_id'] or od.get('status')!='quarantined' or od.get('dispatch_state')!='unknown_quarantined' or od.get('attempt_count')!=1 or od.get('last_error_code')!='normal_enrichment_timeout_unknown'):return False
    for jid,h in g['six_job_preimages']:
        snap=db.collection(a.JOBS).document(jid).get(transaction=transaction)
        doc=snap.to_dict() if snap.exists else {}
        if image(doc)!=h or doc.get('status')!='pending' or doc.get('attempt_count')!=0 or doc.get('uid')==uid:return False
    return True


@transactional
def install(transaction,db,uid,grant,*,now):
    from database import normal_enrichment_activation as a
    if not grant_shape(grant,uid,now):raise ValueError('next_source_scope_invalid')
    r=ref(db,uid)
    if r.get(transaction=transaction).exists:raise ValueError('next_source_already_disposed_no_rearm')
    ps=a.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=a.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    g=asdict(grant)
    if (not a.valid_policy(policy,now) or image(policy)!=grant.policy_sha256 or policy['epoch']!=grant.epoch or grant.expires_at>policy['expires_at'] or image(tenant)!=grant.old_tenant_sha256 or tenant.get('uid')!=uid or tenant.get('quarantined_job_id')!=grant.old_job_id or tenant.get('active_job_id') or tenant.get('additional_quarantined_job_ids') or uid not in policy['quarantined_tenants'] or not old_matches(transaction,db,uid,g)):raise ValueError('next_source_preimage_drift')
    if source_ref(db,uid,grant.new_conversation_id).get(transaction=transaction).exists:raise ValueError('next_source_not_fresh')
    # Exactly one additive immutable grant; no old/policy/tenant/queue write.
    transaction.set(r,{'version':1,'state':'prepared','grant':g})
    return True


def eligible(transaction,db,current,policy,tenant,now,*,consumed=False,admission=False):
    """Reads before writes. Closed failure returns no exception authorization."""
    try:
        uid=current['uid'];snap=ref(db,uid).get(transaction=transaction)
        d=snap.to_dict() if snap.exists else {};g=d.get('grant',{})
        if type(d.get('version')) is not int or d.get('version')!=1:return None
        if d.get('state')!=('consumed' if consumed else 'prepared'):return None
        if not grant_shape(NextSourceDisposition(**{**g,'six_job_preimages':tuple(tuple(x) for x in g['six_job_preimages'])}),uid,now):return None
        if (g['epoch']!=policy['epoch'] or g['expires_at']>policy['expires_at'] or tenant.get('quarantined_job_id')!=g['old_job_id'] or uid not in policy['quarantined_tenants'] or tenant.get('additional_quarantined_job_ids') or current.get('conversation_id')!=g['new_conversation_id'] or current.get('transcript_sha256')!=g['new_transcript_sha256'] or current.get('admission_epoch',policy['epoch'])!=policy['epoch'] or not g['not_before']<=current['source_completed_at']<=now or not old_matches(transaction,db,uid,g)):return None
        if tenant.get('uid') != uid:return None
        if not admission:
            from models.hermes_cloud_enrichment_contract import transcript_source
            from database.conversations import _prepare_conversation_for_transcript_hash
            fresh = source_ref(db,uid,g['new_conversation_id']).get(transaction=transaction)
            raw_source = fresh.to_dict() if fresh.exists else {}
            source = _prepare_conversation_for_transcript_hash(raw_source,uid)
            if not isinstance(source,dict):return None
            if (source.get('id')!=g['new_conversation_id'] or source.get('source')!='external_integration' or source.get('status')!='completed' or source.get('discarded') is not False or source.get('normal_source_completed_at')!=current['source_completed_at'] or any(source.get(k) for k in ('capture_generation','capture_owner_token','capture_owner')) or transcript_source(source)[1]!=g['new_transcript_sha256']):return None
        if consumed and (d.get('job_id')!=current['job_id'] or d.get('lease_token')!=current['lease_token'] or current.get('attempt_count')!=1):return None
        if not consumed and current.get('attempt_count',0)!=0:return None
        return ref(db,uid)
    except (KeyError,TypeError,ValueError,AttributeError):return None
