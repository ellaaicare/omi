"""Restore standard prospective Main processing through the existing worker.

Reuses the reviewed October14 V2 first-terminal/per-CID admission protocol.
Separate, explicit private policy mode; no HTTP, fallback, model, import effect,
known-backlog replay, quarantine clearing, source rewriting or new consent.
"""
import copy
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid5, NAMESPACE_URL
from google.cloud.firestore_v1 import FieldFilter, transactional
from database.normal_future_source_disposition import (
    TerminalTransition, binding, digest, string_hash, aware, IDENTITY, TAG,
    SOURCE_FIELDS as ORIGINAL_SOURCE_FIELDS,
)
from database import normal_native_source_once as native
KEY = 'ordinary_source_restoration_v2'
CONTRACT = 'ella-normal-ordinary-source-restoration-v2'
SCOPES = 'ella_normal_ordinary_source_disposition'
JOBS = 'ella_normal_ordinary_source_admissions'
OWNER_HASH = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'
EXPECTED_SIX_SHA = '89630f36e3a87c6ac03c146604c3d359d7640f5aa474a3f5c70dac4dbd0b3579'
MAX_INVENTORY = 5000
SOURCE_FIELDS = ORIGINAL_SOURCE_FIELDS + ('capture_protocol_version', 'capture_generation',
    'capture_owner_token', 'thin_cloudbot_source_binding')


def scope_ref(db, policy):
    return db.collection(SCOPES).document(policy[KEY]['scope_id'])


def policy_shape(policy):
    """No scope and malformed scope are distinct; caller enforces opt-in."""
    try:
        key = policy[KEY]
        q = policy['quotas']
        start, end = policy['source_completion_boundary'], policy['expires_at']
        return (type(key) is dict and set(key) == {'contract','owner_sha256','scope_id','scope_sha256'}
            and key['contract'] == CONTRACT and key['owner_sha256'] == OWNER_HASH
            and native.uuid(key['scope_id']) and native.pin(key['scope_sha256'])
            and policy.get('owner_scope_sha256') == [OWNER_HASH]
            and 'native_source_once' not in policy and 'one_source_qualification' not in policy
            and type(q) is dict
            and all(type(q.get(k)) is int and 1 <= q[k] <= limit for k,limit in
                (('max_concurrency',1),('max_claims_per_hour',5),
                 ('max_tenant_claims_per_hour',5),('max_attempts',1)))
            and aware(start) and aware(end) and 0 < (end-start).total_seconds() <= 172800)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def current_policy(policy, now):
    from database.normal_enrichment_activation import valid_policy
    return policy_shape(policy) and valid_policy(policy, now)


def quarantine_signature(tenant):
    return native.digest(native.quarantine_image(tenant))


def held_preimages(transaction, db, uid, tenant):
    holds = native.snapshot_holds(transaction, db, uid, tenant)
    if len(holds) != 6 or native.digest(holds) != EXPECTED_SIX_SHA:
        raise ValueError('ordinary_original_six_holds_changed')
    return holds


@transactional
def prepare_scope(transaction, db, uid, *, now, approval_ref, expected_policy_sha256,
                  expected_tenant_sha256, epoch, window_seconds):
    """Private one-shot restoration; all reads before archive/scope/policy writes.

    Existing Main permission is unchanged. New mode supports ordinary deliberate
    recordings, NOT a fictional-only classifier. Report this access-mode change
    before enabling; root must bind current owner/route/quiet and exact approval.
    No source/CID/answers are selected, no existing queue job is touched.
    """
    from database import normal_enrichment_activation as activation
    if (string_hash(uid) != OWNER_HASH or not aware(now) or not native.uuid(epoch)
            or type(window_seconds) is not int or not 0 < window_seconds <= 172800
            or type(approval_ref) is not str or not 0 < len(approval_ref) <= 200
            or not native.pin(expected_policy_sha256) or not native.pin(expected_tenant_sha256)):
        raise ValueError('ordinary_restore_private_binding_invalid')
    marker = db.collection(SCOPES).document(str(uuid5(NAMESPACE_URL, CONTRACT+':'+OWNER_HASH)))
    if marker.get(transaction=transaction, timeout=5, retry=None).exists:
        raise ValueError('ordinary_restore_already_attempted_no_rearm')
    ps = activation.reference(db).get(transaction=transaction, timeout=5, retry=None)
    old = ps.to_dict() if ps.exists else {}
    ts = activation.tenant_ref(db,uid).get(transaction=transaction, timeout=5, retry=None)
    tenant = ts.to_dict() if ts.exists else {}
    if (native.digest(old) != expected_policy_sha256 or native.digest(tenant) != expected_tenant_sha256
            or tenant.get('uid') != uid or tenant.get('active_job_id')
            or old.get('active') != [] or old.get('epoch') == epoch
            or KEY in old or uid not in old.get('quarantined_tenants', [])
            or not activation.quotas_valid(old.get('quotas'))
            or type(old.get('hour_claims')) is not int or old['hour_claims'] < 0):
        raise ValueError('ordinary_restore_preimage_or_quiet_hold')
    holds = held_preimages(transaction,db,uid,tenant)
    sources = list(db.collection('users').document(uid).collection('conversations').select([])
        .limit(MAX_INVENTORY+1).get(transaction=transaction, timeout=5, retry=None))
    jobs = list(db.collection(activation.JOBS).where(filter=FieldFilter('uid','==',uid))
        .select(['uid','conversation_id']).limit(MAX_INVENTORY+1)
        .get(transaction=transaction, timeout=5, retry=None))
    if len(sources)>MAX_INVENTORY or len(jobs)>MAX_INVENTORY:
        raise ValueError('ordinary_restore_inventory_bound')
    source_ids = {string_hash(x.id) for x in sources}; job_ids=set()
    for row in jobs:
        doc=row.to_dict() or {}
        if doc.get('uid')!=uid or not native.uuid(doc.get('conversation_id')):
            raise ValueError('ordinary_restore_inventory_identity')
        source_ids.add(string_hash(doc['conversation_id']));job_ids.add(string_hash(row.id))
    if len(source_ids)>MAX_INVENTORY or not all(string_hash(jid)in job_ids for jid in holds):
        raise ValueError('ordinary_restore_inventory_incomplete')
    archive = db.collection('ella_normal_enrichment_policy_history').document(expected_policy_sha256)
    previous = archive.get(transaction=transaction, timeout=5, retry=None)
    if previous.exists and native.digest((previous.to_dict()or{}).get('policy'))!=expected_policy_sha256:
        raise ValueError('ordinary_restore_archive_changed')
    end=now+timedelta(seconds=window_seconds)
    policy=copy.deepcopy(old)
    policy.pop('native_source_once',None);policy.pop('one_source_qualification',None)
    policy.pop('qualification_previous_policy_sha256',None)
    # Keep historical counters, quarantine IDs, tenant rows and concurrency exact.
    # Restoring normal <=5/hour changes prospective capacity, not old attempt IDs.
    policy.update(epoch=epoch, source_completion_boundary=now, expires_at=end,state='active',
        owner_scope_sha256=[OWNER_HASH],active=[],quotas={**old['quotas'],
            'max_concurrency':1,'max_claims_per_hour':5,'max_tenant_claims_per_hour':5,'max_attempts':1})
    record={'contract':CONTRACT,'state':'active','uid':uid,'epoch':epoch,'expires_at':end,
        'source_completion_boundary':now,'window_seconds':window_seconds,
        'approval_ref':approval_ref,'quarantine_signature':quarantine_signature(tenant),
        'held_preimages':holds,'excluded_source_ids_sha256':sorted(source_ids),
        'excluded_job_ids_sha256':sorted(job_ids),
        'route_policy':'existing-normal-current-approved-Hermes'}
    record['scope_sha256']=digest(record)
    policy[KEY]={'contract':CONTRACT,'owner_sha256':OWNER_HASH,'scope_id':marker.id,
                 'scope_sha256':record['scope_sha256']}
    if not current_policy(policy,now):raise ValueError('ordinary_restore_constructed_policy_invalid')
    if not previous.exists:transaction.create(archive,{'policy':copy.deepcopy(old),
        'archived_at':now,'reason':'restore_standard_future_only_Main_processing_preserve_original_holds'})
    transaction.create(marker,record);transaction.set(activation.reference(db),policy)
    return {'state':'ORDINARY_FUTURE_ONLY_SCOPE_COMMITTED','epoch':epoch,
        'scope_id':marker.id,'scope_sha256':record['scope_sha256'],
        'policy_sha256':native.digest(policy),'expires_at':end,'excluded_sources':len(source_ids),
        'excluded_jobs':len(job_ids),'source_job_tenant_grant_writes':0,
        'preserved_quarantines':6,'provider_calls':0}


def read_scope(transaction, db, uid, policy, tenant, now):
    if (string_hash(uid)!=OWNER_HASH or not current_policy(policy,now)
            or tenant.get('uid')!=uid or uid not in policy['quarantined_tenants']):return None
    snap=scope_ref(db,policy).get(transaction=transaction,timeout=5,retry=None)
    r=snap.to_dict()if snap.exists else {};cutover=snap.create_time
    if (r.get('contract')!=CONTRACT or r.get('state')!='active' or r.get('uid')!=uid
            or r.get('epoch')!=policy['epoch'] or r.get('expires_at')!=policy['expires_at']
            or r.get('source_completion_boundary')!=policy['source_completion_boundary']
            or type(r.get('window_seconds')) is not int
            or r['window_seconds']!=(policy['expires_at']-policy['source_completion_boundary']).total_seconds()
            or r.get('route_policy')!='existing-normal-current-approved-Hermes'
            or not aware(cutover) or not cutover<=now<policy['expires_at']
            or policy[KEY]['scope_sha256']!=r.get('scope_sha256')
            or r.get('scope_sha256')!=digest({k:v for k,v in r.items()if k!='scope_sha256'})
            or r.get('quarantine_signature')!=quarantine_signature(tenant)
            or any(type(r.get(k))is not list or len(r[k])>MAX_INVENTORY
                or r[k]!=sorted(set(r[k])) or any(not native.pin(x)for x in r[k])
                for k in ('excluded_source_ids_sha256','excluded_job_ids_sha256'))):return None
    try:holds=held_preimages(transaction,db,uid,tenant)
    except (ValueError,KeyError,TypeError):return None
    if holds!=r.get('held_preimages'):return None
    return r,cutover


def admission(transaction, db, payload, policy, tenant, now, transition):
    """Read all authority first; caller creates returned per-job marker last."""
    if type(transition) is not TerminalTransition or type(payload.get('account_routing_contract')) is not int or payload['account_routing_contract'] != 1:
        return None
    scope = read_scope(transaction, db, payload.get('uid'), policy, tenant, now)
    if scope is None:
        return None
    r, cutover = scope
    if (transition.uid != payload.get('uid') or transition.conversation_id != payload.get('conversation_id')
            or transition.first_completed_at != now or not cutover < transition.source_created_at
            or not transition.source_created_at <= transition.prior_updated_at <= now
            or string_hash(payload.get('conversation_id')) in r['excluded_source_ids_sha256']
            or string_hash(payload.get('job_id')) in r['excluded_job_ids_sha256']
            or any(type(payload.get(k)) is not str or not payload[k] for k in IDENTITY)):
        return None
    prior_ref=db.collection('users').document(payload['uid']).collection('conversations').document(payload['conversation_id'])
    prior=prior_ref.get(transaction=transaction,timeout=5,retry=None)
    doc=prior.to_dict()if prior.exists else{}
    if (not prior.exists or prior.create_time!=transition.source_created_at
            or prior.update_time!=transition.prior_updated_at
            or not native.source_native(doc,payload['uid'],payload['conversation_id'])
            or doc.get('status')=='completed' or doc.get('active_summary_version_id')
            or doc.get('normal_source_completed_at')):return None
    # CID-derived receipt prevents changed event/job IDs from re-admitting a source.
    ref = db.collection(JOBS).document(digest({'uid': payload['uid'], 'cid': payload['conversation_id']}))
    if ref.get(transaction=transaction).exists:
        return None
    tag = {'contract': CONTRACT, 'scope_sha256': r['scope_sha256'],
           'source_created_at': transition.source_created_at,
           'first_completed_at': now, 'binding_sha256': transition.binding_sha256,
           'marker_id': ref.id, 'capture_generation':doc['capture_generation'],
           'capture_owner_sha256':native.sha(doc['capture_owner_token'])}
    record = {k: payload[k] for k in IDENTITY}
    record.update(contract=CONTRACT, state='prepared', tag=tag, epoch=policy['epoch'], expires_at=policy['expires_at'])
    return tag, ref, record


def eligible(transaction, db, current, policy, tenant, now, *, admission=False, consumed=False):
    if admission:
        return None  # Only the trusted producer can mint the admission tag.
    tag = current.get(TAG)
    if (type(tag) is not dict or set(tag) != {'contract','scope_sha256','source_created_at',
            'first_completed_at','binding_sha256','marker_id','capture_generation','capture_owner_sha256'}
            or tag.get('contract') != CONTRACT):
        return None
    scope = read_scope(transaction, db, current.get('uid'), policy, tenant, now)
    if scope is None:
        return None
    r, cutover = scope
    if (tag.get('scope_sha256') != r['scope_sha256'] or current.get('admission_epoch') != policy['epoch']
            or current.get('source_completed_at') != tag.get('first_completed_at')
            or not all(aware(tag.get(k)) for k in ('source_created_at', 'first_completed_at'))
            or not cutover < tag['source_created_at'] <= tag['first_completed_at'] <= now
            or string_hash(current.get('conversation_id')) in r['excluded_source_ids_sha256']
            or string_hash(current.get('job_id')) in r['excluded_job_ids_sha256']
            or any(type(current.get(k)) is not str or not current[k] for k in IDENTITY)):
        return None
    marker_id = digest({'uid': current['uid'], 'cid': current['conversation_id']})
    if tag.get('marker_id') != marker_id:
        return None
    ref = db.collection(JOBS).document(marker_id)
    snap = ref.get(transaction=transaction)
    rec = snap.to_dict() if snap.exists else {}
    if (rec.get('tag') != tag or rec.get('contract') != CONTRACT or rec.get('epoch') != policy['epoch']
            or rec.get('expires_at') != policy['expires_at']
            or any(rec.get(k) != current[k] for k in IDENTITY)):
        return None
    if consumed:
        if (rec.get('state') != 'consumed' or not current.get('lease_token')
                or rec.get('lease_token') != current['lease_token']
                or type(current.get('attempt_count')) is not int or current['attempt_count'] != 1):
            return None
    elif (rec.get('state') != 'prepared' or type(current.get('attempt_count')) is not int
          or current['attempt_count'] != 0):
        return None
    source = db.collection('users').document(current['uid']).collection('conversations').document(current['conversation_id']).get(transaction=transaction, field_paths=SOURCE_FIELDS)
    if not source.exists or source.create_time != tag['source_created_at']:
        return None
    from database.conversations import _prepare_conversation_for_read
    doc = _prepare_conversation_for_read(source.to_dict(), current['uid'])
    if (not native.source_native(doc,current['uid'],current['conversation_id'])
            or not native.nonempty_transcript(doc)
            or doc.get('capture_generation')!=tag.get('capture_generation')
            or native.sha(doc.get('capture_owner_token'))!=tag.get('capture_owner_sha256')):return None
    b = binding(doc, current['uid'], current['conversation_id'])
    if (not b or digest(b) != tag['binding_sha256']
            or b['transcript_sha256'] != current['transcript_sha256']
            or b['client_interaction_id'] != current['client_interaction_id']
            or doc.get('normal_source_completed_at') != tag['first_completed_at']):
        return None
    return ref
