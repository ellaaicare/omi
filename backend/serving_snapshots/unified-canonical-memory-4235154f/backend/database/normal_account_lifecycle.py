"""Prospective per-account processing authority; no import I/O or auto-enable.

Only the authenticated server lifecycle may enroll a verified existing route.
The global switch and each account remain separately pausable. Enrollment is a
persistent processing choice, not a consent grant or agent provisioning API.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from google.cloud.firestore_v1 import FieldFilter, transactional
from database.normal_future_source_disposition import digest, aware, IDENTITY, SOURCE_FIELDS
from database.normal_product_capture_provenance import binding
from database import normal_product_capture_provenance as product
from database import normal_native_source_once as native

CONTRACT = 'ella-normal-account-lifecycle-v1'
MODE = 'per_account_lifecycle_v1'
ACCOUNTS = 'ella_normal_account_processing'
ADMISSIONS = 'ella_normal_account_source_admissions'
TAG = 'normal_account_lifecycle_admission'
MAX_INVENTORY = 5000
ROUTE_FIELDS = ('uid','provider','binding_id','account_user_id','profile_user_id',
                'profile_name','agent_id','workspace_root','honcho_workspace',
                'observed_peer','observer_peer')
SOURCE_FIELDS = SOURCE_FIELDS + ('capture_protocol_version','capture_generation',
                                 'capture_owner_token','thin_cloudbot_source_binding')


def owner(uid):
    from ella.services.normal_account_routing import admitted_subject
    if not admitted_subject(uid):
        raise ValueError('account_processing_owner_invalid')
    return native.sha(uid)


def route_image(uid, runtime):
    """Existing resolver result only; no alias-based or email-based selection."""
    if (runtime.uid != uid or runtime.status != 'active' or runtime.provider != 'hermes'
            or not native.uuid(runtime.account_user_id)
            or runtime.profile_user_id != runtime.account_user_id):
        raise ValueError('account_processing_route_owner_mismatch')
    image = {k:getattr(runtime,k,None) for k in ROUTE_FIELDS}
    if any(type(v) is not str or not v for v in image.values()):
        raise ValueError('account_processing_route_incomplete')
    return image


@dataclass(frozen=True)
class VerifiedRoute:
    uid: str
    image_sha256: str
    account_owner_sha256: str
    profile_sha256: str


async def verify_route(uid, repository):
    """No provisioning or model request; existing consent+owner resolver only."""
    from ella.services.normal_account_routing import resolve_route
    runtime = await resolve_route(uid, repository)
    image = route_image(uid, runtime)
    return VerifiedRoute(uid,digest(image),native.sha(runtime.account_user_id),
                         native.sha(runtime.profile_name))


def account_ref(db,uid):
    return db.collection(ACCOUNTS).document(owner(uid))


def policy_shape(policy):
    from database.normal_enrichment_activation import quotas_valid
    try:
        return (type(policy) is dict and type(policy.get('version')) is int
            and policy['version']==3 and policy.get('mode')==MODE
            and type(policy.get('enrollment_enabled')) is bool
            and type(policy.get('lifecycle_authorization_ref')) is str
            and 0<len(policy['lifecycle_authorization_ref'].strip())<=200
            and type(policy.get('state')) is str and policy['state'] in {'active','paused'}
            and native.uuid(policy.get('epoch')) and aware(policy.get('source_completion_boundary'))
            and quotas_valid(policy.get('quotas')) and policy['quotas']['max_attempts']==1
            and type(policy.get('active')) is list and len(policy['active'])<=4
            and all(type(x) is dict and all(type(x.get(k)) is str and bool(x[k])
                for k in ('uid','job_id','lease_token')) for x in policy['active'])
            and type(policy.get('quarantined_tenants')) is list
            and len(policy['quarantined_tenants'])<=16
            and all(type(x) is str and bool(x) for x in policy['quarantined_tenants'])
            and len(set(policy['quarantined_tenants']))==len(policy['quarantined_tenants'])
            and type(policy.get('hour')) is int and policy['hour']>=0
            and type(policy.get('hour_claims')) is int and 0<=policy['hour_claims']<=100
            and not any(k in policy for k in ('owner_scope_sha256','expires_at',
                'one_source_qualification','native_source_once','ordinary_source_restoration_v2')))
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
        return False


def utc_now():
    return datetime.now(timezone.utc)


def valid_policy(policy,now):
    return (policy_shape(policy) and policy['state'] == 'active' and aware(now)
            and policy['source_completion_boundary'] <= now)


def quarantine_image(tenant,uid):
    return {'uid':tenant.get('uid',uid),'quarantined_job_id':tenant.get('quarantined_job_id'),
            'additional_quarantined_job_ids':tenant.get('additional_quarantined_job_ids',[])}


def held_preimages(transaction,db,uid,tenant):
    from database import normal_enrichment_activation as activation
    if tenant and tenant.get('uid') != uid:
        raise ValueError('account_processing_tenant_owner_mismatch')
    primary=tenant.get('quarantined_job_id'); other=tenant.get('additional_quarantined_job_ids',[])
    if type(other) is not list or len(other)>15:
        raise ValueError('account_processing_hold_invalid')
    ids=([primary] if primary is not None else [])+other
    if any(type(x) is not str or not x for x in ids) or len(set(ids))!=len(ids):
        raise ValueError('account_processing_hold_invalid')
    result={}
    for jid in ids:
        snap=db.collection(activation.JOBS).document(jid).get(transaction=transaction,timeout=5,retry=None)
        row=snap.to_dict() if snap.exists else {}
        if (not snap.exists or row.get('uid')!=uid or row.get('job_id')!=jid
                or row.get('status')!='quarantined' or row.get('dispatch_state')!='unknown_quarantined'
                or row.get('lease_token') is not None or row.get('lease_expires_at') is not None):
            raise ValueError('account_processing_hold_unresolved_or_changed')
        result[jid]=digest(row)
    return result


@transactional
def enroll(transaction,db,proof,*,now,authorization_ref,expected_epoch=None):
    """Private supported-server enrollment. No public raw-UID/body authority.

    Never invoked at import, startup, finalization or discovery. Caller must have
    the explicit ongoing-processing authorization before this first write. A
    paused/existing record is returned unchanged, never silently reenabled.
    """
    from database import normal_enrichment_activation as activation
    if (type(proof) is not VerifiedRoute or not aware(now)
            or not all(native.pin(x) for x in (proof.image_sha256,proof.account_owner_sha256,proof.profile_sha256))
            or not native.uuid(expected_epoch)
            or type(authorization_ref) is not str or not 0<len(authorization_ref)<=200):
        raise ValueError('account_processing_verified_enrollment_required')
    uid=proof.uid; ref=account_ref(db,uid)
    ps=activation.reference(db).get(transaction=transaction,timeout=5,retry=None)
    policy=ps.to_dict() if ps.exists else {}
    if (not valid_policy(policy,now) or policy.get('enrollment_enabled') is not True
            or policy.get('epoch')!=expected_epoch
            or policy.get('lifecycle_authorization_ref')!=authorization_ref):
        raise ValueError('account_processing_enrollment_authority_changed')
    current=ref.get(transaction=transaction,timeout=5,retry=None)
    if current.exists:
        old=current.to_dict() or {}
        if (old.get('contract')!=CONTRACT or type(old.get('state')) is not str
                or old['state'] not in {'active','paused'}
                or old.get('uid')!=uid or old.get('route_sha256')!=proof.image_sha256
                or old.get('account_owner_sha256')!=proof.account_owner_sha256
                or old.get('profile_sha256')!=proof.profile_sha256):
            raise ValueError('account_processing_existing_mapping_changed')
        return {'state':old.get('state'),'created':False,'writes':0}
    ts=activation.tenant_ref(db,uid).get(transaction=transaction,timeout=5,retry=None)
    tenant=ts.to_dict() if ts.exists else {}
    if tenant.get('active_job_id') or any(x.get('uid')==uid for x in policy['active']):
        raise ValueError('account_processing_not_quiet')
    holds=held_preimages(transaction,db,uid,tenant)
    sources=list(db.collection('users').document(uid).collection('conversations').select([])
        .limit(MAX_INVENTORY+1).get(transaction=transaction,timeout=5,retry=None))
    jobs=list(db.collection(activation.JOBS).where(filter=FieldFilter('uid','==',uid))
        .select(['uid','conversation_id']).limit(MAX_INVENTORY+1)
        .get(transaction=transaction,timeout=5,retry=None))
    if len(sources)>MAX_INVENTORY or len(jobs)>MAX_INVENTORY:
        raise ValueError('account_processing_inventory_bound')
    excluded={native.sha(x.id) for x in sources}
    for snap in jobs:
        row=snap.to_dict() or {}
        if row.get('uid')!=uid or not native.uuid(row.get('conversation_id')):
            raise ValueError('account_processing_inventory_owner_invalid')
        excluded.add(native.sha(row['conversation_id']))
    if len(excluded)>MAX_INVENTORY:
        raise ValueError('account_processing_inventory_bound')
    from uuid import uuid4
    record={'contract':CONTRACT,'uid':uid,'state':'active','epoch':str(uuid4()),
        'route_sha256':proof.image_sha256,'account_owner_sha256':proof.account_owner_sha256,
        'profile_sha256':proof.profile_sha256,'enabled_at':now,'authorization_ref':authorization_ref,
        'quarantine_sha256':digest(quarantine_image(tenant,uid)),'held_preimages':holds,
        'excluded_source_ids_sha256':sorted(excluded)}
    transaction.create(ref,record)
    return {'state':'active','created':True,'writes':1,'account_epoch':record['epoch']}


def read_account(transaction,db,uid,tenant,now):
    snap=account_ref(db,uid).get(transaction=transaction,timeout=5,retry=None)
    r=snap.to_dict() if snap.exists else {}
    if (r.get('contract')!=CONTRACT or r.get('uid')!=uid or r.get('state')!='active'
            or not native.uuid(r.get('epoch')) or not aware(r.get('enabled_at'))
            or not aware(snap.create_time) or not r['enabled_at']<=snap.create_time<=now
            or not all(native.pin(r.get(k)) for k in ('route_sha256','account_owner_sha256','profile_sha256'))
            or r.get('quarantine_sha256')!=digest(quarantine_image(tenant,uid))
            or type(r.get('excluded_source_ids_sha256')) is not list
            or len(r['excluded_source_ids_sha256'])>MAX_INVENTORY
            or r['excluded_source_ids_sha256']!=sorted(set(r['excluded_source_ids_sha256']))
            or not all(native.pin(x) for x in r['excluded_source_ids_sha256'])):
        return None
    try:holds=held_preimages(transaction,db,uid,tenant)
    except ValueError:return None
    if r.get('held_preimages')!=holds:return None
    return r,snap.create_time


def admission(transaction,db,payload,policy,tenant,now,transition):
    from database.normal_future_source_disposition import TerminalTransition
    if (not valid_policy(policy,now) or type(transition) is not TerminalTransition
            or type(payload.get('account_routing_contract')) is not int
            or payload['account_routing_contract']!=1
            or any(type(payload.get(k)) is not str or not payload[k] for k in IDENTITY)):
        return None
    uid=payload['uid'];cid=payload['conversation_id']
    if type(transition.source_binding) is not dict or digest(transition.source_binding)!=transition.binding_sha256:return None
    authority=read_account(transaction,db,uid,tenant,now)
    if authority is None:return None
    r,cutover=authority
    if (transition.uid!=uid or transition.conversation_id!=cid or transition.first_completed_at!=now
            or not max(cutover,policy['source_completion_boundary'])<transition.source_created_at
            or not transition.source_created_at<=transition.prior_updated_at<=now
            or native.sha(cid) in r['excluded_source_ids_sha256']):return None
    prior=db.collection('users').document(uid).collection('conversations').document(cid).get(
        transaction=transaction,timeout=5,retry=None)
    doc=prior.to_dict() if prior.exists else {}
    if (not prior.exists or prior.create_time!=transition.source_created_at
            or prior.update_time!=transition.prior_updated_at or not product.source_native(doc,uid,cid)
            or doc.get('status')=='completed' or doc.get('active_summary_version_id')
            or doc.get('normal_source_completed_at')
            or doc.get('source') != transition.source_binding.get('source_family')):return None
    ref=db.collection(ADMISSIONS).document(digest({'uid':uid,'cid':cid}))
    if ref.get(transaction=transaction,timeout=5,retry=None).exists:return None
    tag={'contract':CONTRACT,'account_epoch':r['epoch'],'route_sha256':r['route_sha256'],
        'source_created_at':transition.source_created_at,'first_completed_at':now,
        'binding_sha256':transition.binding_sha256,'source_binding':transition.source_binding,'capture_generation':doc['capture_generation'],
        'capture_owner_sha256':native.sha(doc['capture_owner_token']),'marker_id':ref.id}
    record={k:payload[k] for k in IDENTITY}
    record.update(contract=CONTRACT,state='prepared',tag=tag,epoch=policy['epoch'])
    return tag,ref,record


def eligible(transaction,db,current,policy,tenant,now,*,admission=False,consumed=False):
    if admission or not valid_policy(policy,now):return None
    tag=current.get(TAG)
    if type(tag) is not dict or tag.get('contract')!=CONTRACT:return None
    uid=current.get('uid');cid=current.get('conversation_id')
    authority=read_account(transaction,db,uid,tenant,now)
    if authority is None:return None
    r,cutover=authority
    if (tag.get('account_epoch')!=r['epoch'] or tag.get('route_sha256')!=r['route_sha256']
            or current.get('admission_epoch')!=policy['epoch']
            or not all(aware(tag.get(k)) for k in ('source_created_at','first_completed_at'))
            or not max(cutover,policy['source_completion_boundary'])<tag['source_created_at']<=tag['first_completed_at']<=now
            or current.get('source_completed_at')!=tag['first_completed_at']
            or native.sha(cid) in r['excluded_source_ids_sha256']):return None
    marker=digest({'uid':uid,'cid':cid})
    if tag.get('marker_id')!=marker:return None
    ref=db.collection(ADMISSIONS).document(marker)
    snap=ref.get(transaction=transaction,timeout=5,retry=None); rec=snap.to_dict() if snap.exists else {}
    if (rec.get('contract')!=CONTRACT or rec.get('tag')!=tag or rec.get('epoch')!=policy['epoch']
            or any(rec.get(k)!=current.get(k) for k in IDENTITY)):return None
    if consumed:
        if (rec.get('state')!='consumed' or not current.get('lease_token')
                or rec.get('lease_token')!=current['lease_token'] or current.get('attempt_count')!=1):return None
    elif rec.get('state')!='prepared' or current.get('attempt_count')!=0:return None
    snap=db.collection('users').document(uid).collection('conversations').document(cid).get(
        transaction=transaction,field_paths=SOURCE_FIELDS,timeout=5,retry=None)
    if not snap.exists or snap.create_time!=tag['source_created_at']:return None
    from database.conversations import _prepare_conversation_for_read
    doc=_prepare_conversation_for_read(snap.to_dict(),uid)
    b=binding(doc,uid,cid)
    if (not product.source_native(doc,uid,cid) or not native.nonempty_transcript(doc) or not b
            or digest(b)!=tag.get('binding_sha256') or b['transcript_sha256']!=current.get('transcript_sha256')
            or b['client_interaction_id']!=current.get('client_interaction_id')
            or doc.get('normal_source_completed_at')!=tag['first_completed_at']
            or doc.get('capture_generation')!=tag.get('capture_generation')
            or native.sha(doc.get('capture_owner_token'))!=tag.get('capture_owner_sha256')):return None
    return ref


def require_route(job,runtime):
    tag=job.get(TAG)
    if tag is not None and (type(tag) is not dict or tag.get('route_sha256')!=digest(route_image(job['uid'],runtime))):
        raise ValueError('account_processing_mapping_changed')


def processing_status(db,uid,*,now=None):
    """Read-only status; no enrollment, backfill or enable on successful login."""
    from database import normal_enrichment_activation as activation
    now=now or datetime.now(timezone.utc)
    ps=activation.reference(db).get(timeout=3,retry=None); policy=ps.to_dict() if ps.exists else {}
    snap=account_ref(db,uid).get(timeout=3,retry=None); r=snap.to_dict() if snap.exists else {}
    if not policy_shape(policy):return {'state':'paused','error_code':'account_processing_lifecycle_not_configured'}
    if policy['state']=='paused':return {'state':'paused','error_code':'account_transcript_processing_paused'}
    if not valid_policy(policy,now):return {'state':'paused','error_code':'account_processing_policy_not_current'}
    if not snap.exists:return {'state':'not_configured','error_code':'account_processing_not_configured'}
    if r.get('state')=='paused':return {'state':'paused','error_code':'account_processing_paused'}
    ts=activation.tenant_ref(db,uid).get(timeout=3,retry=None);tenant=ts.to_dict() if ts.exists else {}
    authority=read_account(None,db,uid,tenant,now)
    if authority is None:return {'state':'blocked','error_code':'account_processing_authority_invalid'}
    return {'state':'processing' if tenant.get('active_job_id') else 'available','error_code':None}


def assert_existing_mapping(db,uid,runtime):
    """Read-only binding congruence; missing enrollment is reported by status."""
    from database.normal_enrichment_activation import read_policy
    policy=read_policy(db)
    if policy.get('mode')!=MODE:return
    if not policy_shape(policy):raise ValueError('account_processing_lifecycle_invalid')
    image=route_image(uid,runtime)
    snap=account_ref(db,uid).get(timeout=3,retry=None)
    if not snap.exists:return
    r=snap.to_dict() or {}
    if (r.get('contract')!=CONTRACT or r.get('uid')!=uid or r.get('route_sha256')!=digest(image)
            or r.get('account_owner_sha256')!=native.sha(runtime.account_user_id)
            or r.get('profile_sha256')!=native.sha(runtime.profile_name)):
        from ella.services.runtime_errors import ProvisioningError
        raise ProvisioningError('account_processing_mapping_changed',retryable=False)


async def enroll_authenticated_account(uid,repository,*,authorization_ref,expected_epoch):
    """Private server adapter; caller must be existing authenticated lifecycle.

    Mounted only under explicitly active/enrollment-enabled lifecycle configuration;
    transaction repeats that authority after the resolver await.
    Never accepts a caller-supplied agent/profile/destination or raw route proof.
    """
    import asyncio
    from database._client import db
    proof=await verify_route(uid,repository)
    return await asyncio.to_thread(enroll,db.transaction(),db,proof,
        now=datetime.now(timezone.utc),authorization_ref=authorization_ref,expected_epoch=expected_epoch)


@transactional
def pause_account(transaction,db,uid,*,expected_epoch,now):
    if not aware(now) or not native.uuid(expected_epoch):raise ValueError('account_processing_pause_invalid')
    ref=account_ref(db,uid); snap=ref.get(transaction=transaction,timeout=5,retry=None)
    r=snap.to_dict() if snap.exists else {}
    if (r.get('contract')!=CONTRACT or r.get('uid')!=uid or r.get('epoch')!=expected_epoch
            or r.get('state') not in {'active','paused'}):raise ValueError('account_processing_pause_conflict')
    if r['state']=='active':transaction.update(ref,{'state':'paused','paused_at':now})
    return {'state':'paused','epoch':expected_epoch}


@transactional
def prepare_global_paused(transaction,db,*,expected_policy_sha256,now,epoch,authorization_ref):
    """Explicit code-only migration helper; never active after preparation.

    Existing worker reservations/unknown outcomes must be reconciled separately.
    Existing policy archived, every job/tenant/source/credential untouched.
    """
    from database import normal_enrichment_activation as activation
    if (not native.pin(expected_policy_sha256) or not aware(now) or not native.uuid(epoch)
            or type(authorization_ref) is not str or not 0<len(authorization_ref)<=200):
        raise ValueError('account_processing_migration_invalid')
    ref=activation.reference(db);snap=ref.get(transaction=transaction,timeout=5,retry=None)
    old=snap.to_dict() if snap.exists else {}
    if (digest(old)!=expected_policy_sha256 or old.get('state')!='paused'
            or old.get('active')!=[] or old.get('epoch')==epoch
            or not activation.quotas_valid(old.get('quotas')) or old.get('mode')==MODE):
        raise ValueError('account_processing_migration_preimage_or_quiet')
    archive=db.collection('ella_normal_enrichment_policy_history').document(expected_policy_sha256)
    prior=archive.get(transaction=transaction,timeout=5,retry=None)
    if prior.exists and digest((prior.to_dict()or{}).get('policy'))!=expected_policy_sha256:
        raise ValueError('account_processing_migration_archive_conflict')
    policy={k:old[k] for k in ('active','quarantined_tenants','hour','hour_claims','quotas')}
    policy.update(version=3,mode=MODE,state='paused',epoch=epoch,source_completion_boundary=now,
        lifecycle_authorization_ref=authorization_ref,enrollment_enabled=False,paused_at=now)
    policy['quotas']={**policy['quotas'],'max_attempts':1}
    if not policy_shape(policy):raise ValueError('account_processing_migration_shape')
    if not prior.exists:transaction.create(archive,{'policy':old,'archived_at':now,'reason':CONTRACT})
    transaction.set(ref,policy)
    return {'state':'paused','epoch':epoch,'policy_sha256':digest(policy),'source_job_tenant_writes':0}


def require_dispatch_context(uid,db):
    """Require an actual durable started lease, not just a Python context value."""
    from database import normal_enrichment_activation as activation
    if activation.read_policy(db).get('mode')!=MODE:return
    from database.normal_future_source_disposition import _worker_job
    from ella.services.runtime_errors import ProvisioningError
    def reject():raise ProvisioningError('account_processing_claimed_source_required',retryable=False)
    context=_worker_job.get()
    if type(context) is not dict or context.get('uid')!=uid or TAG not in context:reject()
    snap=db.collection(activation.JOBS).document(context.get('job_id','')).get(timeout=3,retry=None)
    job=snap.to_dict() if snap.exists else {}
    now=utc_now()
    if (not snap.exists or any(job.get(k)!=context.get(k) for k in IDENTITY)
            or job.get(TAG)!=context.get(TAG) or job.get('status')!='running'
            or job.get('dispatch_state')!='started_unknown' or not job.get('lease_token')
            or job['lease_token']!=context.get('lease_token')
            or not aware(job.get('lease_expires_at')) or job['lease_expires_at']<=now):reject()
    cid=job['conversation_id']
    source=db.collection('users').document(uid).collection('conversations').document(cid).get(timeout=3,retry=None)
    if not source.exists:reject()
    from database.conversations import _prepare_conversation_for_read
    doc=_prepare_conversation_for_read(source.to_dict(),uid)
    require_callback_source(db,uid,cid,doc)


def require_callback_source(db,uid,cid,doc,*,now=None):
    """No source-bearing callback for an old/unclaimed product source.

    Retained START metadata legitimately appears after begin; compare the same
    canonical source_pin used by retained enrichment, not an edited transcript.
    No transcript normalization or stored-source mutation is performed.
    """
    from database import normal_enrichment_activation as activation
    policy=activation.read_policy(db)
    if policy.get('mode')!=MODE:return
    now=now or utc_now()
    from ella.services.runtime_errors import ProvisioningError
    from models.hermes_cloud_enrichment_contract import build_enrichment_identity
    from ella.services.retained_completion_enrichment import source_pin
    def reject():raise ProvisioningError('account_processing_callback_source_not_active',retryable=False)
    if not valid_policy(policy,now):reject()
    identity=build_enrichment_identity(uid=uid,conversation_id=cid,conversation=doc)
    snap=db.collection(activation.JOBS).document(identity.job_id).get(timeout=3,retry=None)
    job=snap.to_dict() if snap.exists else {}
    ts=activation.tenant_ref(db,uid).get(timeout=3,retry=None);tenant=ts.to_dict() if ts.exists else {}
    authority=read_account(None,db,uid,tenant,now)
    tag=job.get(TAG)
    if (authority is None or type(tag) is not dict or job.get('uid')!=uid
            or job.get('conversation_id')!=cid or job.get('status')!='running'
            or job.get('dispatch_state')!='started_unknown' or job.get('admission_epoch')!=policy['epoch']
            or not job.get('lease_token') or not aware(job.get('lease_expires_at'))
            or job['lease_expires_at']<=now or tenant.get('active_job_id')!=job.get('job_id')
            or not any(x.get('job_id')==job.get('job_id') and x.get('lease_token')==job['lease_token']
                for x in policy['active'])
            or tag.get('account_epoch')!=authority[0]['epoch']
            or tag.get('route_sha256')!=authority[0]['route_sha256']):reject()
    marker=db.collection(ADMISSIONS).document(digest({'uid':uid,'cid':cid})).get(timeout=3,retry=None)
    rec=marker.to_dict() if marker.exists else {}
    if (rec.get('state')!='consumed' or rec.get('tag')!=tag or rec.get('lease_token')!=job['lease_token']
            or any(rec.get(k)!=job.get(k) for k in IDENTITY)):reject()
    source=db.collection('users').document(uid).collection('conversations').document(cid).get(
        field_paths=SOURCE_FIELDS,timeout=3,retry=None)
    from database.conversations import _prepare_conversation_for_read
    live=_prepare_conversation_for_read(source.to_dict(),uid) if source.exists else {}
    if (not source.exists or source.create_time!=tag.get('source_created_at')
            or not product.source_native(doc,uid,cid) or not product.source_native(live,uid,cid)
            or live.get('capture_generation')!=tag.get('capture_generation')
            or doc.get('source')!=live.get('source')
            or doc.get('capture_generation')!=live.get('capture_generation')
            or native.sha(live.get('capture_owner_token'))!=tag.get('capture_owner_sha256')
            or doc.get('capture_owner_token')!=live.get('capture_owner_token')):reject()
    pin=source_pin(live,uid,cid)
    if source_pin(doc,uid,cid)!=pin:reject()
    actual={'source_family':live.get('source'),'source_sha256':pin[0],'source_version':pin[1],
        'stock_summary_version_sha256':digest(pin[2]),'transcript_sha256':identity.transcript_sha256,
        'client_interaction_id':identity.client_interaction_id}
    if actual!=tag.get('source_binding') or digest(actual)!=tag.get('binding_sha256'):reject()
    # Bounded store reads may cross expiry; never authorize using the old sample.
    if utc_now()>=job['lease_expires_at']:reject()
