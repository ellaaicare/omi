"""Two-phase, operator-prebound fictional transcript producer.

Only normal app-authenticated test owner can submit the exact reviewed bytes.
Preparation creates a normal source then binds its actual Firestore create_time.
Completion uses the ordinary stock-processing producer, never a forged terminal
proof, direct model call, outbox insert or direct enriched-summary write.
"""
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from google.cloud.firestore_v1 import transactional
from database import normal_enrichment_activation as activation
from database import normal_future_source_disposition as guard


class ProducerHold(ValueError):
    pass


def parse_submission(raw):
    if type(raw) is not bytes or len(raw)>65536:
        raise ProducerHold('test_transcript_body_bound')
    def pairs(values):
        result={}
        for k,v in values:
            if k in result:raise ProducerHold('test_transcript_duplicate_field')
            result[k]=v
        return result
    def nonfinite(_):raise ProducerHold('test_transcript_nonfinite')
    try:value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=nonfinite)
    except ProducerHold:raise
    except (ValueError,UnicodeError) as e:raise ProducerHold('test_transcript_json_invalid') from e
    if type(value) is not dict or set(value)!={'request_id','text'}:
        raise ProducerHold('test_transcript_closed_fields')
    try:valid_id=guard._uuid(value['request_id'])
    except (ValueError,TypeError,AttributeError):valid_id=False
    if (not valid_id or type(value['text']) is not str or not 1<=len(value['text'])<=12000
            or not value['text'].strip()):raise ProducerHold('test_transcript_input_invalid')
    try:value['text'].encode('utf-8')
    except UnicodeError as e:raise ProducerHold('test_transcript_unicode_invalid') from e
    return value


def _read_context(tx,db,uid,request_id,now):
    if guard.string_hash(uid)!=guard.OWNER_HASH or not guard.aware(now):
        raise ProducerHold('test_transcript_owner_or_clock')
    ps=activation.reference(db).get(transaction=tx);policy=ps.to_dict() if ps.exists else {}
    ts=activation.tenant_ref(db,uid).get(transaction=tx);tenant=ts.to_dict() if ts.exists else {}
    found=guard.read_scope(tx,db,uid,policy,tenant,now)
    if found is None or now>=policy[guard.QUALIFICATION]['admission_cutoff']:
        raise ProducerHold('test_transcript_scope_closed')
    scope,cutover=found
    if request_id!=scope['producer_request_id']:
        raise ProducerHold('test_transcript_request_not_prebound')
    return policy,scope,cutover


@transactional
def create_source(tx,db,uid,request_id,text,*,now,build_document,prepare_document):
    policy,scope,cutover=_read_context(tx,db,uid,request_id,now)
    if guard.string_hash(text)!=scope['producer_text_sha256']:
        raise ProducerHold('test_transcript_bytes_not_reviewed')
    opref=guard.producer_ref(db,uid,request_id);ops=opref.get(transaction=tx);op=ops.to_dict() if ops.exists else None
    ref=db.collection('users').document(uid).collection('conversations').document(scope['producer_source_id'])
    snap=ref.get(transaction=tx);stored=snap.to_dict() if snap.exists else {}
    if op is not None:
        if (op.get('metadata')!=guard.producer_metadata(scope) or not snap.exists
                or not guard.producer_document_matches(stored,uid,scope)
                or op.get('state') not in {'created','prepared','completion_attempted','completed'}):
            raise ProducerHold('test_transcript_replay_mismatch')
        return {'source_id':ref.id,'state':op['state'],'created':False}
    q=policy[guard.QUALIFICATION]
    if (snap.exists or q['approved_source_id_sha256'] is not None
            or q['admissions_consumed']!=0 or q['claims_consumed']!=0):
        raise ProducerHold('test_transcript_source_or_budget_used')
    user=db.collection('users').document(uid).get(transaction=tx,field_paths=('data_protection_level',))
    profile=user.to_dict() if user.exists else {}
    level=profile.get('data_protection_level','standard')
    if level not in {'standard','enhanced'}:raise ProducerHold('test_transcript_protection_unknown')
    doc=build_document(uid,scope,text,now)
    if (not guard.producer_document_matches(doc,uid,scope) or doc.get('status')!='processing'
            or doc.get('active_summary_version_id') or doc.get('capture_protocol_version')
            or doc.get('normal_source_completed_at')):
        raise ProducerHold('test_transcript_server_document_invalid')
    encoded=prepare_document(doc,uid,level)
    tx.create(ref,encoded)
    tx.create(opref,{'metadata':guard.producer_metadata(scope),'state':'created','created_at':now})
    return {'source_id':ref.id,'state':'created','created':True}


@transactional
def claim_completion(tx,db,uid,request_id,*,now):
    policy,scope,cutover=_read_context(tx,db,uid,request_id,now)
    q=policy[guard.QUALIFICATION];opref=guard.producer_ref(db,uid,request_id)
    ops=opref.get(transaction=tx);op=ops.to_dict() if ops.exists else {}
    ref=db.collection('users').document(uid).collection('conversations').document(scope['producer_source_id'])
    snap=ref.get(transaction=tx);d=snap.to_dict() if snap.exists else {}
    if (op.get('state')!='prepared' or op.get('metadata')!=guard.producer_metadata(scope)
            or not snap.exists or not cutover<snap.create_time<=now
            or op.get('source_created_at')!=snap.create_time or q['approved_source_created_at']!=snap.create_time
            or q['approved_source_id_sha256']!=guard.string_hash(ref.id)
            or q['admissions_consumed']!=0 or q['claims_consumed']!=0
            or not guard.producer_document_matches(d,uid,scope) or d.get('status')!='processing'
            or d.get('active_summary_version_id') or d.get('normal_source_completed_at')
            or d.get('initial_processing_claimed_at') is not None
            or d.get('initial_processing_claim_token') is not None):
        raise ProducerHold('test_transcript_completion_not_single_ready_source')
    from database.qualification_stock_supervisor import STOCK_SECONDS
    deadline=min(now+timedelta(seconds=STOCK_SECONDS),scope['expires_at'])
    nonce=str(uuid4())
    tx.update(opref,{'state':'completion_attempted','completion_attempted_at':now,
                     'stock_worker_nonce':nonce,'stock_deadline_at':deadline})
    return {'source_id':ref.id,'stored_document':d,'worker_nonce':nonce,'deadline':deadline,
            'consumer':('muse' if scope['route_policy']=='exact-test-Muse-summary-v2' else 'hermes')}


@transactional
def start_stock_worker(tx,db,uid,request_id,*,source_id,worker_nonce,deadline,now):
    policy,scope,cutover=_read_context(tx,db,uid,request_id,now)
    opref=guard.producer_ref(db,uid,request_id);ops=opref.get(transaction=tx);op=ops.to_dict() if ops.exists else {}
    ref=db.collection('users').document(uid).collection('conversations').document(source_id)
    snap=ref.get(transaction=tx);d=snap.to_dict() if snap.exists else {}
    if (source_id!=scope['producer_source_id'] or op.get('state')!='completion_attempted'
            or op.get('metadata')!=guard.producer_metadata(scope)
            or type(worker_nonce) is not str or op.get('stock_worker_nonce')!=worker_nonce
            or not guard.aware(deadline) or op.get('stock_deadline_at')!=deadline or now>=deadline
            or op.get('stock_worker_started_at') is not None
            or not snap.exists or snap.create_time!=op.get('source_created_at')
            or policy[guard.QUALIFICATION]['approved_source_created_at']!=snap.create_time
            or policy[guard.QUALIFICATION]['approved_source_id_sha256']!=guard.string_hash(source_id)
            or not guard.producer_document_matches(d,uid,scope) or d.get('status')!='processing'
            or d.get('initial_processing_claimed_at') is not None
            or d.get('initial_processing_claim_token') is not None
            or d.get('active_summary_version_id') or d.get('normal_source_completed_at')):
        raise ProducerHold('test_transcript_stock_worker_binding_hold')
    tx.update(opref,{'stock_worker_started_at':now})
    return {'stored_document':d}


def record_stock_execution(db,uid,request_id,worker_nonce,execution):
    """One bounded metadata CAS. Never reset attempt/state or publish a summary."""
    from google.cloud.firestore_v1 import LastUpdateOption
    ref=guard.producer_ref(db,uid,request_id);snap=ref.get(timeout=4,retry=None)
    op=snap.to_dict() if snap.exists else {}
    if (not snap.exists or op.get('stock_worker_nonce')!=worker_nonce
            or op.get('state') not in {'completion_attempted','completed'}
            or op.get('stock_execution') is not None):
        raise ProducerHold('test_transcript_execution_receipt_unknown')
    fields={k:execution[k] for k in ('local_status','local_group_absent','remote_provider_drain',
                                   'retry_allowed','elapsed_monotonic_seconds')}
    for k in ('exit_code','leader_reaped','receipt_diagnostic','failure_diagnostic'):
        if k in execution:fields[k]=execution[k]
    ref.update({'stock_execution':fields,'stock_supervisor_finished_at':datetime.now(timezone.utc)},
               option=LastUpdateOption(snap.update_time),timeout=4,retry=None)


@transactional
def record_completion(tx,db,uid,request_id,*,source_id,now):
    # Receipt-only after an attempt. Expiry/retirement never grants another effect.
    opref=guard.producer_ref(db,uid,request_id);ops=opref.get(transaction=tx);op=ops.to_dict() if ops.exists else {}
    ss=guard.scope_ref(db).get(transaction=tx);scope=ss.to_dict() if ss.exists else {}
    immutable={k:v for k,v in scope.items() if k not in {'scope_sha256','retired_at'}}
    state=scope.get('state');immutable['state']='active'
    retired_valid=(state=='active' and 'retired_at' not in scope) or (
        state=='retired' and guard.aware(scope.get('retired_at')) and guard.aware(scope.get('expires_at'))
        and scope['retired_at']>=scope['expires_at'])
    ref=db.collection('users').document(uid).collection('conversations').document(source_id)
    snap=ref.get(transaction=tx);d=snap.to_dict() if snap.exists else {}
    if (guard.string_hash(uid)!=guard.OWNER_HASH or not guard.aware(now)
            or not ss.exists or not retired_valid or scope.get('uid')!=uid
            or scope.get('contract')!=guard.CONTRACT or scope.get('epoch')!=guard.EPOCH
            or scope.get('producer')!=guard.PRODUCER or scope.get('scope_sha256')!=guard.digest(immutable)
            or request_id!=scope.get('producer_request_id') or source_id!=scope.get('producer_source_id')
            or op.get('state')!='completion_attempted' or op.get('metadata')!=guard.producer_metadata(scope)
            or not snap.exists or snap.create_time!=op.get('source_created_at')
            or not guard.producer_document_matches(d,uid,scope)
            or not guard.aware(d.get('normal_source_completed_at'))
            or not snap.create_time<=d['normal_source_completed_at']<=now
            or d.get('status')!='completed' or not d.get('active_summary_version_id')):
        raise ProducerHold('test_transcript_completion_readback_unknown')
    if scope.get('route_policy')=='exact-test-Muse-summary-v2':
        from database.conversations import _prepare_conversation_for_read
        readable=_prepare_conversation_for_read(d,uid)
        guard.seal_test_muse_completion(tx,db,uid,readable,scope,now)
    tx.update(opref,{'state':'completed','completed_at':now,'active_summary_version_id_at_readback':d['active_summary_version_id']})
    return {'source_id':source_id,'state':'source_completed','observed_active_summary_version_id':d['active_summary_version_id'],
            'hermes_enrichment':('not_admitted_muse_pending_readback_required'
                                 if scope.get('route_policy')=='exact-test-Muse-summary-v2'
                                 else 'separate_normal_worker_readback_required')}


def build_document(uid,scope,text,now):
    from models.conversation import Conversation,Structured,ConversationSource,ConversationStatus
    from models.transcript_segment import TranscriptSegment
    from uuid import UUID,uuid5
    segment=TranscriptSegment(id=str(uuid5(UUID(scope['producer_request_id']),'synthetic-text-segment')),
                              text=text,speaker='SPEAKER_00',is_user=False,start=0,end=0)
    document = Conversation(id=scope['producer_source_id'],created_at=now,started_at=now,finished_at=now,
                        source=ConversationSource.omi,status=ConversationStatus.processing,language='en',
                        structured=Structured(title='',overview='',emoji='🧪',category='other',action_items=[],events=[]),
                        transcript_segments=[segment],discarded=False,explicit_keep=True,
                        external_data={'qualification_transcript':guard.producer_metadata(scope)}).dict()
    # Conversation's wire model has no uid field: durable owner is server-bound.
    return {**document,'uid':uid}


class TranscriptProducer:
    def __init__(self,db):self.db=db
    def prepare(self,uid,body):
        from ella.services.ai_consent import assert_current_ai_consent
        from database.conversations import _prepare_conversation_for_write
        assert_current_ai_consent(uid)
        value=parse_submission(body);now=datetime.now(timezone.utc)
        result=create_source(self.db.transaction(),self.db,uid,value['request_id'],value['text'],now=now,
                             build_document=build_document,prepare_document=_prepare_conversation_for_write)
        if result['state'] in {'created','prepared'}:
            guard.bind_reviewed_source(self.db.transaction(),self.db,uid,result['source_id'],now=datetime.now(timezone.utc))
            result['state']='prepared'
        return {**result,'classification':'backend_synthetic_transcript_not_hardware_capture'}
    def complete(self,uid,request_id):
        from ella.services.ai_consent import assert_current_ai_consent
        from database.qualification_stock_supervisor import run_stock
        assert_current_ai_consent(uid)
        value=claim_completion(self.db.transaction(),self.db,uid,request_id,now=datetime.now(timezone.utc))
        execution=run_stock(uid,request_id,value['source_id'],value['worker_nonce'],value['deadline'],expected_consumer=value['consumer'])
        try:record_stock_execution(self.db,uid,request_id,value['worker_nonce'],execution)
        except Exception as e:raise ProducerHold('test_transcript_execution_receipt_unknown') from e
        if execution['local_status']!='completed' or not execution['local_group_absent']:
            code='test_transcript_stock_'+execution['local_status']
            if not execution['local_group_absent']:code='test_transcript_stock_local_cleanup_unknown'
            raise ProducerHold(code)
        return execution['result']
