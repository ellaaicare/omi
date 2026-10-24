"""One owner's newly admitted normal sources; no known backlog/quarantine-clear API.

Only the existing trusted first durable stock completion producer can mint a
TerminalTransition. A reviewed operator may later create the immutable scope;
this module does not activate on import and exposes no HTTP endpoint.
"""
import copy
import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from functools import wraps

from google.cloud.firestore_v1 import FieldFilter, transactional

SCOPES = 'ella_normal_future_source_disposition'
SCOPE_ID = 'test-transcript-qualification-20261019-1b471a57'
JOBS = 'ella_normal_future_source_admissions'
CONTRACT = 'ella-normal-test-transcript-v4'
OWNER_HASH = '040d2202f2fffdc92888395713a03c13fb8a54159c90d3563d391d9789ed060a'
EPOCH = 'b3c2f580-837a-433b-8a0c-f99ba043efbc'
WINDOW_SECONDS = 1800
ADMISSION_SECONDS = 600
QUALIFICATION = 'one_source_qualification'
HELD_JOB_HASH = '4c6c241a4d7984a06def47ff06675ac5b526ce2cee20b1ddf7b0883c50cfdbdd'
HELD_PREIMAGE_HASH = 'e803b01dc430419879350175760687c12d10ecd5ba26a317256f0097b1368d85'
PROTECTED_OWNER_HASH = '6c21e84a72741b2a674597b68b4e5ddef616e1a05e455ab4fa116c4fc2688f9e'
PRODUCER = 'authenticated-test-transcript-v1'
MAX_INVENTORY = 5000
TAG = 'trusted_future_source_admission'
IDENTITY = ('job_id', 'uid', 'conversation_id', 'client_interaction_id', 'transcript_sha256')
SEMANTIC = ('semantic_publication_policy', 'semantic_producer_executions_v1',
            'semantic_publication_operations', 'semantic_publication_receipts',
            'semantic_retained_completion_jobs_v1')
SOURCE_FIELDS = ('id', 'uid', 'status', 'source', 'discarded', 'created_at', 'started_at',
                 'finished_at', 'structured', 'active_summary_version_id', 'transcript_segments',
                 'transcript_segments_compressed', 'data_protection_level',
                 'normal_source_completed_at', 'external_data', 'user_id') + SEMANTIC
_worker_job = ContextVar('normal_future_source_job', default=None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()


def held_preimage_digest(value):
    """Match the existing readback/owned-child e803 pin's spaced encoding.

    This is deliberately distinct from compact semantic/scope fingerprints.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def string_hash(value):
    return hashlib.sha256(value.encode()).hexdigest() if type(value) is str else None


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None


def scope_ref(db):
    return db.collection(SCOPES).document(SCOPE_ID)


def quarantine_signature(tenant):
    return digest({k: tenant.get(k) for k in ('uid', 'quarantined_job_id',
                                             'additional_quarantined_job_ids')})


def current_policy(policy, now):
    from database.normal_enrichment_activation import valid_policy
    q = policy.get(QUALIFICATION)
    if not valid_policy(policy, now) or policy.get('epoch') != EPOCH or type(q) is not dict:
        return False
    fields = {'scope_id','owner_ref_sha256','admission_cutoff','admitted_source_id_sha256',
              'admitted_job_id_sha256','admissions_consumed','claims_consumed','approved_source_id_sha256','approved_source_created_at'}
    return (set(q) == fields and q['scope_id'] == SCOPE_ID and q['owner_ref_sha256'] == OWNER_HASH
            and policy['expires_at'] - policy['source_completion_boundary'] == timedelta(seconds=WINDOW_SECONDS)
            and aware(q['admission_cutoff'])
            and q['admission_cutoff'] == policy['source_completion_boundary'] + timedelta(seconds=ADMISSION_SECONDS)
            and all(type(q[k]) is int and q[k] in {0,1} for k in ('admissions_consumed','claims_consumed'))
            and q['claims_consumed'] <= q['admissions_consumed']
            and ((q['approved_source_id_sha256'] is None and q['approved_source_created_at'] is None)
                 or (type(q['approved_source_id_sha256']) is str and len(q['approved_source_id_sha256'])==64 and aware(q['approved_source_created_at'])))
            and (q['admissions_consumed']==0 or q['approved_source_id_sha256']==q['admitted_source_id_sha256'])
            and ((q['admissions_consumed'] == 0 and q['admitted_source_id_sha256'] is None and q['admitted_job_id_sha256'] is None)
                 or (q['admissions_consumed'] == 1 and all(type(q[k]) is str and len(q[k]) == 64 for k in ('admitted_source_id_sha256','admitted_job_id_sha256')))))


def qualification_gate(policy, job, now, *, consumed=False):
    if not current_policy(policy, now):
        return False
    q = policy[QUALIFICATION]
    return (now < q['admission_cutoff'] and string_hash(job.get('uid')) == OWNER_HASH
            and q['admissions_consumed'] == 1 and q['claims_consumed'] == (1 if consumed else 0)
            and string_hash(job.get('conversation_id')) == q['admitted_source_id_sha256']
            and string_hash(job.get('job_id')) == q['admitted_job_id_sha256']
            and type(job.get(TAG)) is dict and job[TAG].get('source_created_at')==q['approved_source_created_at'])


def route_matches(route):
    return (route.provider == 'hermes' and route.status == 'active'
            and route.authority['provider'] == 'openai-codex'
            and route.expected_model == 'gpt-5.6-sol'
            and route.semantic_policy_sha256 == '39ec4f5170f713b5c0a5f0759ac7453186e5b2376366b66d1ab0329621533a34'
            and route.identity.digest == '544340f9641e45d0a0f7a7d7010b67c88c9c3d881cecffce6536cbaef4bf62eb'
            and route.profile_user_id == route.account_user_id
            and route.authority['config_projection_sha256'] == '9040c468d1b97dbf07d6f364d6eedcac772fe41fd1ce01ff3f03111fc3c34f55'
            and route.authority['registry_revision'] == 'cfed16f12814a3b4cdde0370ca2ee1c8d3874e97ac5b0e95a5cf0c45c3975b4d')


def binding(doc, uid, cid):
    from models.hermes_cloud_enrichment_contract import build_enrichment_identity
    from ella.services.retained_completion_enrichment import source_pin
    if (not isinstance(doc, dict) or doc.get('id') != cid or doc.get('uid', uid) != uid
            or doc.get('status') != 'completed' or doc.get('source') != 'omi'
            or doc.get('discarded') or not doc.get('active_summary_version_id')
            or any(doc.get(k) for k in SEMANTIC)):
        return None
    identity = build_enrichment_identity(uid=uid, conversation_id=cid, conversation=doc)
    pin = source_pin(doc, uid, cid)
    return {'source_sha256': pin[0], 'source_version': pin[1],
            'stock_summary_version_sha256': digest(pin[2]),
            'transcript_sha256': identity.transcript_sha256,
            'client_interaction_id': identity.client_interaction_id}


@dataclass(frozen=True)
class TerminalTransition:
    """Private in-process provenance; never accepted from a request body."""
    uid: str
    conversation_id: str
    source_created_at: datetime
    prior_updated_at: datetime
    first_completed_at: datetime
    binding_sha256: str
    source_binding: dict = None


def terminal_transition(snapshot, uid, prior, completed, now):
    """Called only in the existing first-completion server transaction branch."""
    cid = completed.get('id')
    created, updated = snapshot.create_time, snapshot.update_time
    b = binding(completed, uid, cid)
    from ella.services.normal_account_routing import admitted_subject
    if (not snapshot.exists or snapshot.id != cid or not admitted_subject(uid)
            or prior.get('id') != cid or prior.get('uid', uid) != uid
            or prior.get('status') == 'completed' or prior.get('active_summary_version_id')
            or prior.get('normal_source_completed_at') or not b
            or not all(aware(t) for t in (created, updated, now))
            or not created <= updated <= now
            or completed.get('normal_source_completed_at') != now):
        return None
    return TerminalTransition(uid, cid, created, updated, now, digest(b), b)


def _uuid(value):
    from uuid import UUID
    return type(value) is str and str(UUID(value)) == value


def producer_ref(db, uid, request_id):
    return db.collection('users').document(uid).collection('ella_test_transcript_requests_v1').document(request_id)


def _protected(transaction, db, protected_uid):
    from database import normal_enrichment_activation as activation
    if string_hash(protected_uid) != PROTECTED_OWNER_HASH:
        raise ValueError('test_transcript_protected_owner_mismatch')
    snap = activation.tenant_ref(db, protected_uid).get(transaction=transaction)
    tenant = snap.to_dict() if snap.exists else {}
    if (tenant.get('uid') != protected_uid or tenant.get('active_job_id')
            or string_hash(tenant.get('quarantined_job_id')) != HELD_JOB_HASH):
        raise ValueError('test_transcript_protected_tenant_changed')
    held = db.collection(activation.JOBS).document(tenant['quarantined_job_id']).get(transaction=transaction)
    if not held.exists or held_preimage_digest(held.to_dict()) != HELD_PREIMAGE_HASH:
        raise ValueError('test_transcript_protected_hold_changed')
    return tenant


def test_tenant_hold_pins(transaction,db,uid,tenant,policy):
    if tenant.get('uid',uid)!=uid:raise ValueError('test_transcript_tenant_identity')
    ids=[]
    if tenant.get('quarantined_job_id'):ids.append(tenant['quarantined_job_id'])
    additional=tenant.get('additional_quarantined_job_ids',[])
    if type(additional) is not list or len(additional)>16:raise ValueError('test_transcript_hold_bound')
    ids+=additional
    if any(type(x) is not str or not x for x in ids) or len(set(ids))!=len(ids):raise ValueError('test_transcript_hold_identity')
    if bool(ids)!=(uid in policy.get('quarantined_tenants',[])):raise ValueError('test_transcript_quarantine_inconsistent')
    result={}
    for jid in ids:
        snap=db.collection('ella_hermes_cloud_enrichment_outbox').document(jid).get(transaction=transaction)
        row=snap.to_dict() if snap.exists else {}
        if not snap.exists or row.get('uid')!=uid:raise ValueError('test_transcript_old_hold_identity')
        result[string_hash(jid)]=held_preimage_digest(row)
    return result


@transactional
def prepare_scope(transaction, db, uid, *, protected_uid, now, approval_ref,
                  expected_policy_sha256, request_id, source_id, text_sha256,
                  consumer="hermes", muse_run_id=None, muse_operation_id=None):
    """Private finite operator; normal Firebase producer does NOT activate this."""
    from database import normal_enrichment_activation as activation
    if consumer not in ('hermes','muse') or (consumer=='hermes' and (muse_run_id is not None or muse_operation_id is not None)):
        raise ValueError('test_transcript_consumer_invalid')
    if consumer=='muse':
        try:
            if not _uuid(muse_run_id) or not _uuid(muse_operation_id):raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise ValueError('test_transcript_muse_binding_invalid') from None
    if (string_hash(uid) != OWNER_HASH or not aware(now)
            or approval_ref != 'Sentinel_09c34c0a54d88191956d5848df893b25'
            or not _uuid(request_id) or not _uuid(source_id)
            or type(text_sha256) is not str or len(text_sha256) != 64
            or any(c not in '0123456789abcdef' for c in text_sha256)):
        raise ValueError('test_transcript_specific_release_required')
    marker = scope_ref(db)
    if marker.get(transaction=transaction).exists:
        raise ValueError('one_source_scope_already_exists')
    ps = activation.reference(db).get(transaction=transaction)
    old = ps.to_dict() if ps.exists else {}
    ts = activation.tenant_ref(db, uid).get(transaction=transaction)
    tenant = ts.to_dict() if ts.exists else {}
    protected = _protected(transaction, db, protected_uid)
    test_holds=test_tenant_hold_pins(transaction,db,uid,tenant,old)
    if (not old or digest(old) != expected_policy_sha256 or not aware(old.get('expires_at'))
            or now < old['expires_at'] or old.get('active') != []
            or tenant.get('uid', uid) != uid or tenant.get('active_job_id')
            or protected_uid not in old.get('quarantined_tenants', [])):
        raise ValueError('test_transcript_policy_or_tenant_not_idle')
    sources = list(db.collection('users').document(uid).collection('conversations').select([]).limit(MAX_INVENTORY+1).get(transaction=transaction))
    jobs = []
    for owner in (uid, protected_uid):
        jobs.extend(db.collection(activation.JOBS).where(filter=FieldFilter('uid','==',owner)).select(['uid','conversation_id']).limit(MAX_INVENTORY+1).get(transaction=transaction))
    if len(sources)>MAX_INVENTORY or len(jobs)>MAX_INVENTORY:
        raise ValueError('one_source_inventory_bound')
    source_ids={string_hash(x.id) for x in sources};job_ids=set()
    for j in jobs:
        row=j.to_dict() or {}
        if row.get('uid') not in {uid,protected_uid} or type(row.get('conversation_id')) is not str:
            raise ValueError('one_source_inventory_identity')
        source_ids.add(string_hash(row['conversation_id']));job_ids.add(string_hash(j.id))
    if (len(source_ids)>MAX_INVENTORY or HELD_JOB_HASH not in job_ids
            or string_hash(source_id) in source_ids):
        raise ValueError('one_source_incomplete_or_existing_source')
    if db.collection('users').document(uid).collection('conversations').document(source_id).get(transaction=transaction).exists:
        raise ValueError('test_transcript_source_exists')
    if producer_ref(db,uid,request_id).get(transaction=transaction).exists:
        raise ValueError('test_transcript_request_exists')
    archive=db.collection('ella_normal_enrichment_policy_history').document(digest(old))
    if archive.get(transaction=transaction).exists:
        raise ValueError('one_source_archive_already_exists_no_replay')
    end=now+timedelta(seconds=WINDOW_SECONDS)
    q={'scope_id':SCOPE_ID,'owner_ref_sha256':OWNER_HASH,'admission_cutoff':now+timedelta(seconds=ADMISSION_SECONDS),
       'admitted_source_id_sha256':None,'admitted_job_id_sha256':None,'admissions_consumed':0,'claims_consumed':0,
       'approved_source_id_sha256':None,'approved_source_created_at':None}
    policy={**old,'version':2,'epoch':EPOCH,'state':'active','source_completion_boundary':now,'expires_at':end,
            'hour':int(now.timestamp())//3600,'hour_claims':0,'active':[],QUALIFICATION:q,
            'quotas':{**old['quotas'],'max_concurrency':1,'max_claims_per_hour':1,'max_tenant_claims_per_hour':1,'max_attempts':1},
            'qualification_previous_policy_sha256':digest(old)}
    if not current_policy(policy,now):raise ValueError('one_source_policy_invalid')
    record={'contract':CONTRACT,'state':'active','uid':uid,'epoch':EPOCH,'expires_at':end,'approval_ref':approval_ref,
            'protected_uid':protected_uid,'protected_tenant_sha256':digest(protected),
            'test_quarantine_signature':quarantine_signature({**tenant,'uid':uid}),'test_held_preimages':test_holds,
            'held_job_id_sha256':HELD_JOB_HASH,'held_job_preimage_sha256':HELD_PREIMAGE_HASH,
            'excluded_source_ids_sha256':sorted(source_ids),'excluded_job_ids_sha256':sorted(job_ids),
            'route_policy':'exact-existing-test-Hermes-Codex-v1','producer':PRODUCER,
            'producer_request_id':request_id,'producer_source_id':source_id,'producer_text_sha256':text_sha256}
    if consumer=='muse':
        record.update(route_policy='exact-test-Muse-summary-v2',muse_binding={'run_id':muse_run_id,'operation_id':muse_operation_id})
    record['scope_sha256']=digest(record)
    transaction.create(archive,{'policy':old,'policy_sha256':digest(old),'archived_at':now,'reason':'approved_isolated_test_transcript_qualification'})
    transaction.create(marker,record);transaction.set(activation.reference(db),policy)
    return {'prepared':True,'epoch':EPOCH,'scope_sha256':record['scope_sha256'],'excluded_sources':len(source_ids),
            'excluded_jobs':len(job_ids),'expires_at':end.isoformat(),'admission_cutoff':q['admission_cutoff'].isoformat(),
            'previous_policy_sha256':digest(old),'jobs_or_tenants_mutated':False}


def read_scope(transaction, db, uid, policy, tenant, now):
    if (string_hash(uid) != OWNER_HASH or not current_policy(policy, now)
            or tenant.get('uid',uid) != uid):return None
    snap = scope_ref(db).get(transaction=transaction);r=snap.to_dict() if snap.exists else {};cutover=snap.create_time
    if (r.get('contract')!=CONTRACT or r.get('state')!='active' or r.get('uid')!=uid
            or r.get('epoch')!=EPOCH or r.get('expires_at')!=policy['expires_at']
            or r.get('producer')!=PRODUCER or not valid_consumer(r)
            or not aware(cutover) or not cutover<=now<policy['expires_at']
            or r.get('scope_sha256')!=digest({k:v for k,v in r.items() if k!='scope_sha256'})
            or any(type(r.get(k)) is not list or len(r[k])>MAX_INVENTORY or r[k]!=sorted(set(r[k]))
                   for k in ('excluded_source_ids_sha256','excluded_job_ids_sha256'))):return None
    try:
        protected=_protected(transaction,db,r.get('protected_uid'))
        test_holds=test_tenant_hold_pins(transaction,db,uid,tenant,policy)
    except ValueError:return None
    if test_holds!=r.get('test_held_preimages') or quarantine_signature({**tenant,'uid':uid})!=r.get('test_quarantine_signature'):return None
    if (digest(protected)!=r.get('protected_tenant_sha256')
            or r.get('held_job_id_sha256')!=HELD_JOB_HASH or r.get('held_job_preimage_sha256')!=HELD_PREIMAGE_HASH
            or r['protected_uid'] not in policy['quarantined_tenants']):return None
    return r,cutover


def producer_metadata(scope):
    return {'contract':PRODUCER,'request_id':scope['producer_request_id'],
            'text_sha256':scope['producer_text_sha256'],'source_id':scope['producer_source_id'],
            'scope_sha256':scope['scope_sha256'],'source_kind':'synthetic_text_not_physical_recording'}


def producer_document_matches(doc, uid, scope):
    from database.conversations import _prepare_conversation_for_read
    try:
        value=_prepare_conversation_for_read(doc,uid)
        segments=value.get('transcript_segments')
        from uuid import UUID,uuid5
        expected_segment_id=str(uuid5(UUID(scope['producer_request_id']),'synthetic-text-segment'))
        return (value.get('id')==scope['producer_source_id'] and value.get('uid')==uid and value.get('user_id',uid)==uid
                and value.get('source')=='omi'
                and value.get('external_data')=={'qualification_transcript':producer_metadata(scope)}
                and not any(value.get(k) for k in ('capture_protocol_version','capture_owner_token','capture_generation','capture_owner_id'))
                and type(segments) is list and len(segments)==1 and type(segments[0].get('text')) is str
                and string_hash(segments[0]['text'])==scope['producer_text_sha256']
                and segments[0].get('id')==expected_segment_id
                and segments[0].get('speaker')=='SPEAKER_00'
                and type(segments[0].get('speaker_id')) is int and segments[0]['speaker_id']==0
                and segments[0].get('is_user') is False and segments[0].get('person_id') is None
                and all(type(segments[0].get(k)) in (int,float) and segments[0][k]==0 for k in ('start','end')))
    except (ValueError,TypeError,AttributeError):return False


@transactional
def bind_reviewed_source(transaction, db, uid, cid, *, now):
    from database import normal_enrichment_activation as activation
    ps=activation.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=activation.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    result=read_scope(transaction,db,uid,policy,tenant,now)
    if result is None:raise ValueError('one_source_binding_scope_hold')
    r,cutover=result;q=policy[QUALIFICATION]
    if (now>=q['admission_cutoff'] or cid!=r['producer_source_id']
            or q['admissions_consumed']!=0 or q['claims_consumed']!=0
            or string_hash(cid) in r['excluded_source_ids_sha256']):raise ValueError('one_source_binding_ineligible')
    snap=db.collection('users').document(uid).collection('conversations').document(cid).get(transaction=transaction)
    d=snap.to_dict() if snap.exists else {};opref=producer_ref(db,uid,r['producer_request_id']);osnap=opref.get(transaction=transaction);op=osnap.to_dict() if osnap.exists else {}
    if (not snap.exists or not aware(snap.create_time) or not cutover<snap.create_time<=now
            or not producer_document_matches(d,uid,r) or d.get('status') not in {'in_progress','processing'}
            or d.get('active_summary_version_id') or d.get('normal_source_completed_at')
            or op.get('metadata')!=producer_metadata(r) or op.get('state') not in {'created','prepared'}):
        raise ValueError('one_source_binding_source_not_new_uncompleted_normal')
    if q['approved_source_id_sha256'] is not None:
        if (q['approved_source_id_sha256']==string_hash(cid) and q['approved_source_created_at']==snap.create_time
                and op.get('state')=='prepared' and op.get('source_created_at')==snap.create_time):
            return {'bound':True,'writes':0,'source_id':cid}
        raise ValueError('one_source_binding_already_used')
    transaction.update(activation.reference(db),{QUALIFICATION:{**q,'approved_source_id_sha256':string_hash(cid),'approved_source_created_at':snap.create_time}})
    transaction.update(opref,{'state':'prepared','source_created_at':snap.create_time,'prepared_at':now})
    return {'bound':True,'source_id':cid,'source_create_time':snap.create_time.isoformat(),'admission_or_claim_consumed':False}


def selects_test_consumer(transaction, db, uid, conversation, now, transition):
    """Only this pre-reviewed source selects Hermes; all other Muse routing stays unchanged."""
    metadata=(conversation.get('external_data') or {}).get('qualification_transcript')
    if metadata is None:return None
    from database import normal_enrichment_activation as activation
    ps=activation.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=activation.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    result=read_scope(transaction,db,uid,policy,tenant,now)
    if (result is None or type(transition) is not TerminalTransition):
        raise ValueError('test_transcript_consumer_not_qualified')
    r,_=result;q=policy[QUALIFICATION]
    if (metadata!=producer_metadata(r) or transition.uid!=uid or transition.conversation_id!=conversation.get('id')
            or q['approved_source_id_sha256']!=string_hash(conversation.get('id'))
            or q['approved_source_created_at']!=transition.source_created_at
            or not producer_document_matches(conversation,uid,r)):
        raise ValueError('test_transcript_consumer_binding_changed')
    return 'muse' if r['route_policy']=='exact-test-Muse-summary-v2' else 'hermes'


def selects_test_hermes(transaction, db, uid, conversation, now, transition):
    return selects_test_consumer(transaction,db,uid,conversation,now,transition)=='hermes'


def admission(transaction, db, payload, policy, tenant, now, transition):
    """Read all authority first; caller creates returned per-job marker last."""
    if type(transition) is not TerminalTransition or type(payload.get('account_routing_contract')) is not int or payload['account_routing_contract'] != 1:
        return None
    scope = read_scope(transaction, db, payload.get('uid'), policy, tenant, now)
    if scope is None:
        return None
    r, cutover = scope
    if r['route_policy']!='exact-existing-test-Hermes-Codex-v1':return None
    q=policy[QUALIFICATION]
    if (now>=q['admission_cutoff'] or q['admissions_consumed']!=0 or q['claims_consumed']!=0
            or q['approved_source_id_sha256'] is None or q['approved_source_id_sha256']!=string_hash(payload.get('conversation_id'))
            or q['approved_source_created_at']!=transition.source_created_at):
        return None
    if (transition.uid != payload.get('uid') or transition.conversation_id != payload.get('conversation_id')
            or transition.first_completed_at != now or not cutover < transition.source_created_at
            or not transition.source_created_at <= transition.prior_updated_at <= now
            or string_hash(payload.get('conversation_id')) in r['excluded_source_ids_sha256']
            or string_hash(payload.get('job_id')) in r['excluded_job_ids_sha256']
            or any(type(payload.get(k)) is not str or not payload[k] for k in IDENTITY)):
        return None
    # CID-derived receipt prevents changed event/job IDs from re-admitting a source.
    ref = db.collection(JOBS).document(digest({'uid': payload['uid'], 'cid': payload['conversation_id']}))
    if ref.get(transaction=transaction).exists:
        return None
    tag = {'contract': CONTRACT, 'scope_sha256': r['scope_sha256'],
           'source_created_at': transition.source_created_at,
           'first_completed_at': now, 'binding_sha256': transition.binding_sha256,
           'marker_id': ref.id}
    record = {k: payload[k] for k in IDENTITY}
    record.update(contract=CONTRACT, state='prepared', tag=tag, epoch=EPOCH, expires_at=policy['expires_at'])
    from database import normal_enrichment_activation as activation
    transaction.update(activation.reference(db), {QUALIFICATION:{**q,'admitted_source_id_sha256':string_hash(payload['conversation_id']),'admitted_job_id_sha256':string_hash(payload['job_id']),'admissions_consumed':1}})
    return tag, ref, record


def eligible(transaction, db, current, policy, tenant, now, *, admission=False, consumed=False):
    if admission:
        return None  # Only the trusted producer can mint the admission tag.
    if not qualification_gate(policy,current,now,consumed=consumed):
        return None
    tag = current.get(TAG)
    if type(tag) is not dict or tag.get('contract') != CONTRACT:
        return None
    scope = read_scope(transaction, db, current.get('uid'), policy, tenant, now)
    if scope is None:
        return None
    r, cutover = scope
    if r['route_policy']!='exact-existing-test-Hermes-Codex-v1':return None
    if (tag.get('scope_sha256') != r['scope_sha256'] or current.get('admission_epoch') != EPOCH
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
    if (rec.get('tag') != tag or rec.get('contract') != CONTRACT or rec.get('epoch') != EPOCH
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
    if not producer_document_matches(doc,current['uid'],r):return None
    b = binding(doc, current['uid'], current['conversation_id'])
    if (not b or digest(b) != tag['binding_sha256']
            or b['transcript_sha256'] != current['transcript_sha256']
            or b['client_interaction_id'] != current['client_interaction_id']
            or doc.get('normal_source_completed_at') != tag['first_completed_at']):
        return None
    return ref


def require_route(job, route):
    if 'normal_account_lifecycle_admission' in job:
        from database.normal_account_lifecycle import require_route as require_lifecycle_route
        require_lifecycle_route(job, route)
    if TAG in job:
        from ella.services.runtime_errors import ProvisioningError
        if string_hash(job.get('uid')) != OWNER_HASH or not route_matches(route):
            raise ProvisioningError('account_runtime_changed', retryable=False)


def worker_context(function):
    @wraps(function)
    async def wrapped(service, job):
        token = _worker_job.set(copy.deepcopy(job) if TAG in job or 'normal_account_lifecycle_admission' in job else None)
        try:
            return await function(service, job)
        finally:
            _worker_job.reset(token)
    return wrapped


def require_current_runtime(route):
    job = _worker_job.get()
    if job is not None:
        require_route(job, route)


def require_initial_source(uid, cid, doc):
    job = _worker_job.get()
    if job is None:
        return
    from ella.services.runtime_errors import ProvisioningError
    tag = job.get('normal_account_lifecycle_admission', job.get(TAG))
    b = binding(doc, uid, cid)
    if (uid != job.get('uid') or cid != job.get('conversation_id') or not b
            or digest(b) != tag.get('binding_sha256')
            or doc.get('normal_source_completed_at') != tag.get('first_completed_at')):
        raise ProvisioningError('retained_completion_rejected', retryable=False)


def valid_consumer(scope):
    """Immutable operator-only selector; never inferred from worker flags."""
    route=scope.get('route_policy')
    if route=='exact-existing-test-Hermes-Codex-v1':return 'muse_binding' not in scope
    binding=scope.get('muse_binding')
    try:
        return (route=='exact-test-Muse-summary-v2' and type(binding) is dict
                and set(binding)=={'run_id','operation_id'} and _uuid(binding['run_id']) and _uuid(binding['operation_id']))
    except (ValueError,TypeError,AttributeError):return False


def admit_test_muse(transaction, db, uid, conversation, now, transition):
    """Same trusted first-terminal transaction as stock version write.

    No Hermes outbox, generic queue flag, tool schema or scheduler activation.
    The existing exact-source pending field is persisted with the stock version.
    All reads precede writes; parent failure rolls back admission and stock.
    """
    from database import normal_enrichment_activation as activation
    from ella.services import mcp_summary_enrichment as summaries, mcp_synthetic_pending as pending
    from ella.services.mcp_source_report import transcript_projection
    ps=activation.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=activation.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    found=read_scope(transaction,db,uid,policy,tenant,now)
    if found is None:raise ValueError('test_transcript_muse_scope_hold')
    scope,cutover=found;q=policy[QUALIFICATION];cid=conversation.get('id')
    if (scope['route_policy']!='exact-test-Muse-summary-v2' or type(transition) is not TerminalTransition
            or transition.uid!=uid or transition.conversation_id!=cid or transition.first_completed_at!=now
            or not cutover<transition.source_created_at<=transition.prior_updated_at<=now
            or q['approved_source_id_sha256']!=string_hash(cid) or q['approved_source_created_at']!=transition.source_created_at
            or now>=q['admission_cutoff'] or q['admissions_consumed']!=0 or q['claims_consumed']!=0
            or string_hash(cid) in scope['excluded_source_ids_sha256'] or not producer_document_matches(conversation,uid,scope)
            or digest(binding(conversation,uid,cid))!=transition.binding_sha256
            or any(k in conversation for k in (pending.FIELD,'mcp_enrichment_admission'))):
        raise ValueError('test_transcript_muse_binding_changed')
    pre=summaries.preconditions(uid,cid,conversation,contract=summaries.CONTRACT_V2)
    operation=scope['muse_binding']['operation_id']
    if (conversation.get(summaries.LEDGER) or conversation.get(summaries.ORIGINAL)):
        raise ValueError('test_transcript_muse_prior_write')
    marker={'contract':pending.CONTRACT,**scope['muse_binding'],'owner_ref_sha256':string_hash(uid),
            'source_id':cid,'source_sha256':transcript_projection(conversation)['sha256'],
            'expected_snapshot_sha256':pre['expected_snapshot_sha256'],
            'expected_active_summary_version_id':pre['expected_active_summary_version_id'],
            'ready_at':now,'expires_at':scope['expires_at'],'state':'retired','saved_version_id':None,'saved_report_sha256':None}
    pending.validate(marker,uid,cid)
    ref=db.collection(JOBS).document(digest({'uid':uid,'cid':cid}))
    if ref.get(transaction=transaction).exists:raise ValueError('test_transcript_muse_admission_used')
    record={'contract':CONTRACT,'consumer':'muse','state':'preparing','uid':uid,'source_id':cid,
            'scope_sha256':scope['scope_sha256'],'operation_id':operation,'source_created_at':transition.source_created_at,
            'first_completed_at':now,'expires_at':scope['expires_at'],'binding':marker,
            'postprocess_stable_sha256':summaries._snapshot_hash(uid,cid,{**conversation,'apps_results':None})}
    transaction.create(ref,record)
    transaction.update(activation.reference(db),{QUALIFICATION:{**q,'admitted_source_id_sha256':string_hash(cid),
            'admitted_job_id_sha256':string_hash(operation),'admissions_consumed':1}})
    return {'consumer':'muse','job_id':operation,'status':'preparing',
            '_source_updates':{pending.FIELD:marker}}


def muse_pending_retirement(transaction, db, scope, now):
    """Due-only exact marker closure; completed summary/ledger never rewritten."""
    if scope.get('route_policy')!='exact-test-Muse-summary-v2':return None
    from ella.services import mcp_synthetic_pending as pending, mcp_summary_enrichment as summaries
    from ella.services.mcp_source_report import transcript_projection
    from database.conversations import _prepare_conversation_for_read
    immutable={k:v for k,v in scope.items() if k not in {'scope_sha256','retired_at'}};immutable['state']='active'
    if (not valid_consumer(scope) or string_hash(scope.get('uid'))!=OWNER_HASH
            or scope.get('scope_sha256')!=digest(immutable) or not aware(now) or now<scope['expires_at']):
        raise ValueError('test_transcript_muse_retire_binding')
    uid=scope['uid'];cid=scope['producer_source_id'];ref=db.collection('users').document(uid).collection('conversations').document(cid)
    snap=ref.get(transaction=transaction)
    if not snap.exists:return None
    doc=_prepare_conversation_for_read(snap.to_dict(),uid)
    if not producer_document_matches(doc,uid,scope):raise ValueError('test_transcript_muse_retire_source_changed')
    marker=doc.get(pending.FIELD)
    if marker is None:
        if doc.get('active_summary_version_id') or doc.get('status')=='completed':
            raise ValueError('test_transcript_muse_retire_marker_missing')
        return None  # Prepared/uncompleted source remains untouched.
    pending.validate(marker,uid,cid)
    admission_ref=db.collection(JOBS).document(digest({'uid':uid,'cid':cid}))
    admission_snap=admission_ref.get(transaction=transaction);rec=admission_snap.to_dict() if admission_snap.exists else {}
    if (rec.get('consumer')!='muse' or rec.get('uid')!=uid or rec.get('source_id')!=cid
            or rec.get('scope_sha256')!=scope['scope_sha256'] or rec.get('source_created_at')!=snap.create_time
            or rec.get('operation_id')!=marker['operation_id'] or rec.get('expires_at')!=scope['expires_at']
            or rec.get('state') not in {'preparing','prepared'}):
        raise ValueError('test_transcript_muse_retire_receipt_changed')
    original=rec.get('binding')
    if (type(original) is not dict or
            {k:v for k,v in marker.items() if k not in {'state','saved_version_id','saved_report_sha256'}}
            != {k:v for k,v in original.items() if k not in {'state','saved_version_id','saved_report_sha256'}}):
        raise ValueError('test_transcript_muse_retire_marker_changed')
    if (any(marker[k]!=scope['muse_binding'][k] for k in ('run_id','operation_id'))
            or marker['expires_at']!=scope['expires_at']
            or marker['source_sha256']!=transcript_projection(doc)['sha256']):
        raise ValueError('test_transcript_muse_retire_marker_changed')
    if marker['state']=='completed':
        entry=(doc.get(summaries.LEDGER) or {}).get(marker['operation_id']) or {}
        result=entry.get('result') or {}
        if (rec['state']!='prepared' or result.get('success') is not True or result.get('canonical_write') is not False
                or result.get('contract')!=summaries.CONTRACT_V2 or result.get('conversation_id')!=cid
                or result.get('operation_id')!=marker['operation_id'] or result.get('synthetic_run_id')!=marker['run_id']
                or result.get('pending_item_completed') is not True
                or result.get('source_transcript_sha256')!=marker['source_sha256']
                or result.get('source_snapshot_sha256')!=marker['expected_snapshot_sha256']
                or entry.get('based_on_version_id')!=marker['expected_active_summary_version_id']
                or result.get('summary_version_id')!=marker['saved_version_id']
                or result.get('source_report_sha256')!=marker['saved_report_sha256']):
            raise ValueError('test_transcript_muse_retire_completed_ledger_mismatch')
        return None
    if marker['state']=='retired':return None
    pre=summaries.preconditions(uid,cid,doc,contract=summaries.CONTRACT_V2)
    if (pre['expected_snapshot_sha256']!=marker['expected_snapshot_sha256']
            or pre['expected_active_summary_version_id']!=marker['expected_active_summary_version_id']):
        raise ValueError('test_transcript_muse_retire_pending_changed')
    return ref,{pending.FIELD:{**marker,'state':'retired'}}


def seal_test_muse_completion(transaction, db, uid, doc, scope, now):
    """Trusted normal-processing return boundary: full final snapshot, no new model.

    Stock created a retired/unavailable marker, so no consumer can race normal
    postcommit apps_results. Only this one completion receipt transaction opens
    the same one-use operation after verifying all other snapshot fields.
    """
    if scope.get('route_policy')!='exact-test-Muse-summary-v2':return None
    from database import normal_enrichment_activation as activation
    from ella.services import mcp_synthetic_pending as pending, mcp_summary_enrichment as summaries
    from ella.services.mcp_source_report import transcript_projection
    cid=doc.get('id');ps=activation.reference(db).get(transaction=transaction);policy=ps.to_dict() if ps.exists else {}
    ts=activation.tenant_ref(db,uid).get(transaction=transaction);tenant=ts.to_dict() if ts.exists else {}
    active=read_scope(transaction,db,uid,policy,tenant,now)
    if active is None or active[0]!=scope:raise ValueError('test_transcript_muse_seal_closed')
    ref=db.collection(JOBS).document(digest({'uid':uid,'cid':cid}));snap=ref.get(transaction=transaction);rec=snap.to_dict() if snap.exists else {}
    marker=doc.get(pending.FIELD);pending.validate(marker,uid,cid)
    if (rec.get('state')!='preparing' or rec.get('consumer')!='muse' or rec.get('uid')!=uid or rec.get('source_id')!=cid
            or rec.get('scope_sha256')!=scope['scope_sha256'] or rec.get('binding')!=marker
            or marker['state']!='retired' or rec.get('operation_id')!=scope['muse_binding']['operation_id']
            or rec.get('postprocess_stable_sha256')!=summaries._snapshot_hash(uid,cid,{**doc,'apps_results':None})
            or marker['source_sha256']!=transcript_projection(doc)['sha256']
            or marker['expected_active_summary_version_id']!=doc.get('active_summary_version_id')
            or not aware(rec.get('first_completed_at')) or not rec['first_completed_at']<=now<scope['expires_at']):
        raise ValueError('test_transcript_muse_seal_changed')
    pre=summaries.preconditions(uid,cid,doc,contract=summaries.CONTRACT_V2)
    ready={**marker,'state':'pending','ready_at':now,'expected_snapshot_sha256':pre['expected_snapshot_sha256']}
    pending.validate(ready,uid,cid)
    source_ref=db.collection('users').document(uid).collection('conversations').document(cid)
    # All reads above; receipt, ready marker and producer completion are committed together.
    transaction.update(ref,{'state':'prepared','ready_at':now,'binding':ready})
    transaction.update(source_ref,{pending.FIELD:ready,'mcp_enrichment_admission':{
        'consumer':'muse','job_id':marker['operation_id'],'status':'pending'}})
    return ready
