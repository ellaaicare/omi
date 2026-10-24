"""Thin offline-upload lane: exact content identity, generic stock then Muse CAS.

Never selects nearest conversation, Main consent, Hermes, personal postprocessing,
external integration, Honcho, scanner or a global queue. A reserved unknown STT/
stock effect is permanent; an identical completed upload only returns its receipt.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid5, NAMESPACE_URL
from ella.services.thin_cloudbot_consent import need
from ella.services.thin_cloudbot_request_authority import validate_scope

FIELD = 'thin_cloudbot_source_binding'
OPS = 'ella_thin_cloudbot_capture_segments_v1'
CONTRACT = 'ella-thin-cloudbot-offline-capture-v1'
MAX_BYTES = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                   default=str).encode()).hexdigest()


def identity(authority, audio):
    need(type(audio) is bytes and 0 < len(audio) <= MAX_BYTES, 'thin_capture_audio_bound')
    key = digest({'contract': CONTRACT, 'uid': authority.uid, 'scope': authority.scope,
                  'audio_sha256': hashlib.sha256(audio).hexdigest()})
    return key, str(uuid5(NAMESPACE_URL, CONTRACT + ':source:' + key))


def run_transaction(db, effect):
    from google.cloud.firestore_v1 import transactional
    return transactional(effect)(db.transaction())


def source_ref(db, uid, cid):
    return db.collection('users').document(uid).collection('conversations').document(cid)


def reserve(db, authority, key, cid):
    def effect(tx):
        now = datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx, now=now)
        op = db.collection(OPS).document(key)
        snap = op.get(transaction=tx)
        if snap.exists:
            old = snap.to_dict()
            need(old.get('uid') == authority.uid and old.get('consent_scope') == authority.scope
                 and old.get('source_id') == cid, 'thin_capture_segment_owner_changed')
            need(old.get('state') == 'completed', 'thin_capture_attempt_unknown_no_retry')
            ref = source_ref(db, authority.uid, cid)
            stored = ref.get(transaction=tx)
            need(stored.exists and stored.to_dict().get(FIELD) == old['source_binding'],
                 'thin_capture_result_changed')
            return {'completed': True, 'source_id': cid}
        ref = source_ref(db, authority.uid, cid)
        need(not ref.get(transaction=tx).exists, 'thin_capture_no_source_adoption')
        binding = {'contract': CONTRACT, 'consent_scope': authority.scope,
                   'authority': authority.witness, 'segment_key': key, 'source_id': cid}
        tx.create(op, {'uid': authority.uid, 'consent_scope': authority.scope,
                      'source_id': cid, 'state': 'reserved', 'source_binding': binding,
                      'reserved_at': now, 'expires_at': now + timedelta(seconds=120)})
        return {'completed': False, 'source_id': cid, 'source_binding': binding,
                'expires_at': now + timedelta(seconds=120)}
    return run_transaction(db, effect)


def stage_transcript(db, authority, key, cid, reservation, segments, language, started_at):
    from models.conversation import Conversation, Structured, ConversationSource, ConversationStatus
    from database.conversations import _prepare_conversation_for_write, _prepare_conversation_for_read
    now = datetime.now(timezone.utc)
    need(segments and now < reservation['expires_at'], 'thin_capture_transcript_or_deadline')
    end = max(float(s.end) for s in segments)
    doc = Conversation(id=cid, created_at=now, started_at=started_at,
        finished_at=started_at + timedelta(seconds=end), source=ConversationSource.omi,
        status=ConversationStatus.processing, language=language or 'en', discarded=False,
        transcript_segments=segments, structured=Structured(title='', overview='', emoji='🧠', category='other')).dict()
    doc.update(uid=authority.uid, **{FIELD: reservation['source_binding']})
    def effect(tx):
        at = datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx, now=at)
        op = db.collection(OPS).document(key);snap = op.get(transaction=tx)
        old = snap.to_dict() if snap.exists else {}
        ref = source_ref(db, authority.uid, cid)
        existing = ref.get(transaction=tx)
        user = db.collection('users').document(authority.uid).get(transaction=tx)
        level = (user.to_dict() or {}).get('data_protection_level', 'standard')
        need(level in {'standard', 'enhanced'}, 'thin_capture_protection_unknown')
        need(old.get('state') == 'reserved' and old.get('source_binding') == reservation['source_binding']
             and at < old['expires_at'] and not existing.exists, 'thin_capture_transcript_cas')
        encoded = _prepare_conversation_for_write({**doc, 'data_protection_level': level}, authority.uid, level)
        normalized = _prepare_conversation_for_read(copy.deepcopy(encoded), authority.uid)
        tx.create(ref, encoded)
        tx.update(op, {'state': 'stock_started', 'source_snapshot_sha256': digest(normalized)})
        return normalized
    return run_transaction(db, effect)


def generic_stock(doc):
    """Existing configured generic model; no people/calendar/action-item lookup."""
    from models.conversation import Conversation
    from utils.llm.conversation_processing import get_transcript_structure
    source = Conversation(**doc)
    return get_transcript_structure(source.get_transcript(False, people=[]), source.started_at,
        source.language or 'en', 'UTC', photos=source.photos,
        existing_action_items=None, calendar_meeting_context=None,
        uid=None, existing_conversation_id=None).dict()


def commit_stock_and_pending(db, authority, key, cid, document, structured, *, live=None):
    from database.conversations import (_prepare_conversation_for_read,
        _prepare_conversation_for_write, build_summary_version_update)
    from ella.services import mcp_summary_enrichment as summaries, mcp_synthetic_pending as pending
    from ella.services.mcp_source_report import transcript_projection
    def effect(tx):
        now = datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx, now=now)
        op = db.collection(OPS).document(key);snap = op.get(transaction=tx)
        old = snap.to_dict() if snap.exists else {}
        ref = source_ref(db, authority.uid, cid);ss = ref.get(transaction=tx)
        current = _prepare_conversation_for_read(ss.to_dict(), authority.uid) if ss.exists else {}
        if live is not None:
            assert_live_processing(tx, db, authority, cid, current, live)
        user = db.collection('users').document(authority.uid).get(transaction=tx)
        level = current.get('data_protection_level')
        need(level in {'standard', 'enhanced'}, 'thin_capture_protection_unknown')
        need(old.get('uid') == authority.uid and old.get('consent_scope') == authority.scope
             and old.get('state') == 'stock_started' and now < old['expires_at']
             and old.get('source_binding') == document[FIELD]
             and old.get('source_snapshot_sha256') == source_snapshot(current, live=live)
             and current.get(FIELD) == document[FIELD] and not current.get('active_summary_version_id'),
             'thin_capture_stock_snapshot_changed')
        post = {**current, 'structured': copy.deepcopy(structured), 'status': 'completed',
                'discarded': False, 'updated_at': now, 'initial_processing_claimed_at': None,
                'initial_processing_claim_token': None, 'initial_processing_release_token': None}
        post.update(build_summary_version_update(current, next_structured=structured,
                    source='omi', kind='generated', activate=True))
        pre = summaries.preconditions(authority.uid, cid, post, contract=summaries.CONTRACT_V2)
        marker = {'contract': pending.CONTRACT,
                  'run_id': str(uuid5(NAMESPACE_URL, CONTRACT + ':run:' + key)),
                  'operation_id': str(uuid5(NAMESPACE_URL, CONTRACT + ':muse:' + key)),
                  'owner_ref_sha256': hashlib.sha256(authority.uid.encode()).hexdigest(),
                  'source_id': cid, 'source_sha256': transcript_projection(post)['sha256'],
                  'expected_snapshot_sha256': pre['expected_snapshot_sha256'],
                  'expected_active_summary_version_id': pre['expected_active_summary_version_id'],
                  'ready_at': now, 'expires_at': now + timedelta(minutes=15), 'state': 'pending',
                  'saved_version_id': None, 'saved_report_sha256': None}
        pending.validate(marker, authority.uid, cid)
        post[pending.FIELD] = marker
        post['mcp_enrichment_admission'] = {'consumer': 'muse', 'job_id': marker['operation_id'], 'status': 'pending'}
        # The generic summary and its exact Muse snapshot are one atomic publication.
        tx.set(ref, _prepare_conversation_for_write(post, authority.uid, level))
        tx.update(op, {'state': 'completed', 'completed_at': now,
                      'active_summary_version_id': post['active_summary_version_id'],
                      'pending_operation_id': marker['operation_id']})
        return {'source_id': cid, 'state': 'completed'}
    return run_transaction(db, effect)


def _execute_segment(db, authority, path, *, started_at, reservation, transcribe=None, stock=None):
    with open(path, 'rb') as audio:
        raw = audio.read(MAX_BYTES + 1)
    key, cid = identity(authority, raw)
    need(reservation['source_id'] == cid, 'thin_capture_child_content_changed')
    def claim_worker(tx):
        now = datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx, now=now)
        ref = db.collection(OPS).document(key); snap = ref.get(transaction=tx)
        old = snap.to_dict() if snap.exists else {}
        need(old.get('state') == 'reserved' and old.get('source_binding') == reservation['source_binding']
             and now < old['expires_at'] and not old.get('worker_started_at'), 'thin_capture_worker_once')
        tx.update(ref, {'worker_started_at': now})
    run_transaction(db, claim_worker)
    authority.revalidate()
    if transcribe is None:
        from utils.stt.pre_recorded import deepgram_prerecorded_from_bytes
        transcribe = lambda data: deepgram_prerecorded_from_bytes(data, attempts=2, return_language=True)
    words, language = transcribe(raw)
    need(bool(words), 'thin_capture_empty_stt_unknown_no_retry')
    authority.revalidate()
    from utils.stt.pre_recorded import postprocess_words
    segments = postprocess_words(words, 0)
    doc = stage_transcript(db, authority, key, cid, reservation, segments, language, started_at)
    authority.revalidate()
    need(datetime.now(timezone.utc) < reservation['expires_at'], 'thin_capture_stock_deadline')
    structured = (stock or generic_stock)(doc)
    commit_stock_and_pending(db, authority, key, cid, doc, structured)
    return 'new_memories', cid


def process_audio_segment(db, authority, path, *, started_at):
    from ella.services.thin_cloudbot_capture_supervisor import run_capture
    with open(path, 'rb') as audio:
        raw = audio.read(MAX_BYTES + 1)
    key, cid = identity(authority, raw)
    reservation = reserve(db, authority, key, cid)
    if reservation['completed']:
        return 'new_memories', cid
    # The durable reservation is before spawn: timeout/spawn/unknown never refunds it.
    return run_capture(authority, path, started_at, reservation)


def worker(packet):
    from types import SimpleNamespace
    from database._client import db
    from ella.services.thin_cloudbot_request_authority import require_request
    if type(packet) is dict and packet.get('live') is not None:
        return live_worker(packet)
    need(type(packet) is dict and set(packet) == {'uid','auth_time','scope','path','started_at','reservation'},
         'thin_capture_worker_packet')
    authority = require_request(SimpleNamespace(uid=packet['uid'], auth_time=packet['auth_time']), packet['scope'])
    reservation = dict(packet['reservation'])
    reservation['expires_at'] = datetime.fromisoformat(reservation['expires_at'])
    result = _execute_segment(db, authority, packet['path'],
        started_at=datetime.fromisoformat(packet['started_at']), reservation=reservation)
    return {'kind': result[0], 'source_id': result[1]}



def reserve_batch(db, authority, paths):
    """Original upload identity is consumed before VAD/STT: no segmentation drift replay."""
    pieces = []
    from pathlib import Path
    for path in paths:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        need(0 < len(raw) <= MAX_BYTES, 'thin_capture_audio_bound')
        pieces.append((Path(path).name, hashlib.sha256(raw).hexdigest()))
    key = digest({'uid': authority.uid, 'scope': authority.scope, 'files': sorted(pieces)})
    def effect(tx):
        now = datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx, now=now)
        ref = db.collection('ella_thin_cloudbot_capture_batches_v1').document(key)
        snap = ref.get(transaction=tx)
        if snap.exists:
            old = snap.to_dict()
            need(old.get('scope') == authority.scope and old.get('uid') == authority.uid,
                 'thin_capture_batch_binding_changed')
            need(old.get('state') == 'completed', 'thin_capture_batch_unknown_no_retry')
            return key, copy.deepcopy(old['result'])
        tx.create(ref, {'uid': authority.uid, 'scope': authority.scope, 'state': 'reserved', 'reserved_at': now})
        return key, None
    return run_transaction(db, effect)


def complete_batch(db, authority, key, result):
    def effect(tx):
        authority.revalidate_in_transaction(tx, now=datetime.now(timezone.utc))
        ref = db.collection('ella_thin_cloudbot_capture_batches_v1').document(key)
        snap = ref.get(transaction=tx);old = snap.to_dict() if snap.exists else {}
        need(old.get('uid') == authority.uid and old.get('scope') == authority.scope
             and old.get('state') == 'reserved', 'thin_capture_batch_completion_changed')
        tx.update(ref, {'state': 'completed', 'result': copy.deepcopy(result)})
    return run_transaction(db, effect)


LIVE_CONTRACT = 'ella-thin-cloudbot-live-capture-v1'

def create_live_source(db, authority, document):
    """New exact source only, consent + protected target before any STT opens."""
    from database.conversations import _prepare_conversation_for_write
    cid=document['id']
    binding={'contract':LIVE_CONTRACT,'consent_scope':authority.scope,'authority':authority.witness,
             'verified_auth_time':authority.auth_time,'source_id':cid}
    def effect(tx):
        now=datetime.now(timezone.utc)
        authority.revalidate_in_transaction(tx,now=now)
        ref=source_ref(db,authority.uid,cid)
        need(not ref.get(transaction=tx).exists,'thin_capture_no_source_adoption')
        user=db.collection('users').document(authority.uid).get(transaction=tx)
        level=(user.to_dict() or {}).get('data_protection_level','standard')
        need(level in {'standard','enhanced'},'thin_capture_protection_unknown')
        post={**copy.deepcopy(document),FIELD:binding,'uid':authority.uid,'data_protection_level':level}
        tx.create(ref,_prepare_conversation_for_write(post,authority.uid,level))
    run_transaction(db,effect)

def source_snapshot(document, *, live=None):
    if live is None:return digest(document)
    # Heartbeat expiry is mutable; source/transcript/claim/effect identities are not.
    return digest({k:v for k,v in document.items() if k not in {
        'capture_finalization_lease_expires_at','capture_lease_expires_at'}})

def assert_live_processing(tx,db,authority,cid,document,live):
    from utils.conversations.capture_protocol import (_authority_ref,_finalization_claim_is_live,
        _conversation_tuple_matches,CAPTURE_ROTATION_PROCESSING_CLAIM_TOKEN)
    from utils.conversations.capture_target import assert_target_relation
    need(type(live) is dict and set(live)=={'generation','owner_token','claim_token'},'thin_live_tuple_invalid')
    need(document.get(FIELD,{}).get('contract')==LIVE_CONTRACT
         and document[FIELD].get('authority')==authority.witness
         and document[FIELD].get('consent_scope')==authority.scope,'thin_live_target_changed')
    now=datetime.now(timezone.utc)
    if live['claim_token'] is not None:
        ar=_authority_ref(authority.uid,cid);snap=ar.get(transaction=tx);ad=snap.to_dict() if snap.exists else {}
        assert_target_relation(ar,ad,document)
        need(_finalization_claim_is_live(ad,document,cid,live['generation'],live['owner_token'],live['claim_token'],now),
             'thin_live_finalization_lease_lost')
    else:
        # The existing install CAS retired this exact predecessor while publishing
        # its same-generation successor. It clears the transient rotation token.
        ar=_authority_ref(authority.uid,cid);snap=ar.get(transaction=tx);ad=snap.to_dict() if snap.exists else {}
        assert_target_relation(ar,ad,document)
        successor=ad.get('conversation_id')
        need(type(successor) is str and successor and successor != cid,
             'thin_live_rotation_successor_missing')
        next_snap=source_ref(db,authority.uid,successor).get(transaction=tx)
        next_doc=next_snap.to_dict() if next_snap.exists else {}
        need(_conversation_tuple_matches(document,cid,live['generation'],live['owner_token'])
             and document.get('capture_state')=='drained' and not document.get('capture_owner_id')
             and document.get('status')=='processing'
             and _conversation_tuple_matches(next_doc,successor,live['generation'],live['owner_token'])
             and next_doc.get(FIELD,{}).get('authority')==authority.witness
             and next_doc.get(FIELD,{}).get('consent_scope')==authority.scope
             and ad.get('generation')==live['generation'] and ad.get('owner_token')==live['owner_token']
             and ad.get('state') in {'active','drained','terminal','finalizing'},
             'thin_live_rotation_authority_lost')


def reserve_live_stock(db,authority,cid,live):
    from database.conversations import _prepare_conversation_for_read
    key=digest({'contract':LIVE_CONTRACT,'uid':authority.uid,'scope':authority.scope,'source_id':cid,
                'generation':live['generation'],'owner_token':live['owner_token']})
    def effect(tx):
        now=datetime.now(timezone.utc);authority.revalidate_in_transaction(tx,now=now)
        ref=source_ref(db,authority.uid,cid);snap=ref.get(transaction=tx)
        need(snap.exists,'thin_live_source_missing')
        document=_prepare_conversation_for_read(snap.to_dict(),authority.uid)
        assert_live_processing(tx,db,authority,cid,document,live)
        op=db.collection(OPS).document(key);old=op.get(transaction=tx)
        if old.exists:
            value=old.to_dict() or {}
            need(value.get('uid')==authority.uid and value.get('consent_scope')==authority.scope
                 and value.get('source_binding')==document.get(FIELD) and value.get('state')=='completed'
                 and document.get('status')=='completed'
                 and value.get('active_summary_version_id')==document.get('active_summary_version_id'),
                 'thin_capture_attempt_unknown_no_retry')
            return {'completed':True,'source_id':cid}
        need(not document.get('active_summary_version_id'),'thin_live_stock_already_present')
        expires=now+timedelta(seconds=120)
        tx.create(op,{'uid':authority.uid,'consent_scope':authority.scope,'source_id':cid,
            'state':'stock_started','source_binding':document[FIELD],
            'source_snapshot_sha256':source_snapshot(document,live=live),'expires_at':expires})
        return {'source_id':cid,'key':key,'live':copy.deepcopy(live),'expires_at':expires}
    return run_transaction(db,effect)

def process_live_stock(db,authority,cid,generation,owner_token,claim_token):
    from ella.services.thin_cloudbot_capture_supervisor import run_capture
    live={'generation':generation,'owner_token':owner_token,'claim_token':claim_token}
    authority.revalidate()
    reservation=reserve_live_stock(db,authority,cid,live)
    if reservation.get('completed'):
        return 'new_memories', cid
    return run_capture(authority,None,None,reservation)

def live_worker(packet):
    from types import SimpleNamespace
    from database._client import db
    from database.conversations import _prepare_conversation_for_read
    from ella.services.thin_cloudbot_request_authority import require_request
    need(set(packet)=={'uid','auth_time','scope','live','reservation'},'thin_live_worker_packet')
    authority=require_request(SimpleNamespace(uid=packet['uid'],auth_time=packet['auth_time']),packet['scope'])
    reservation=packet['reservation'];cid=reservation['source_id'];key=reservation['key'];live=packet['live']
    need(live==reservation['live'],'thin_live_worker_tuple')
    def begin(tx):
        now=datetime.now(timezone.utc);authority.revalidate_in_transaction(tx,now=now)
        ref=source_ref(db,authority.uid,cid);snap=ref.get(transaction=tx)
        document=_prepare_conversation_for_read(snap.to_dict(),authority.uid) if snap.exists else {}
        assert_live_processing(tx,db,authority,cid,document,live)
        op=db.collection(OPS).document(key);ss=op.get(transaction=tx);old=ss.to_dict() if ss.exists else {}
        need(old.get('state')=='stock_started' and not old.get('worker_started_at')
             and now<old['expires_at'] and old.get('source_snapshot_sha256')==source_snapshot(document,live=live),
             'thin_live_worker_once')
        tx.update(op,{'worker_started_at':now})
        return document
    document=run_transaction(db,begin)
    authority.revalidate()
    structured=generic_stock(document)
    commit_stock_and_pending(db,authority,key,cid,document,structured,live=live)
    return {'kind':'new_memories','source_id':cid}


def persist_live_batch(db,uid,cid,batch_ref,payload,encrypted_payload):
    """Body-bearing buffer write is consent/deletion-fenced in its own transaction."""
    from utils.conversations.capture_target import require_document_grant
    def effect(tx):
        ref=source_ref(db,uid,cid);snap=ref.get(transaction=tx)
        need(snap.exists,'thin_capture_source_missing')
        doc=snap.to_dict() or {};now=datetime.now(timezone.utc)
        need(doc.get(FIELD,{}).get('contract')==LIVE_CONTRACT,'thin_capture_target_changed')
        require_document_grant(tx,uid,doc,now)
        need(doc.get('capture_state')=='active' and doc.get('capture_owner_id')==payload['capture_owner_id']
             and doc.get('capture_generation')==payload['capture_generation'], 'thin_capture_buffer_owner_changed')
        old=batch_ref.get(transaction=tx)
        fingerprint=digest(payload)
        if old.exists:
            need((old.to_dict() or {}).get('source_payload_sha256')==fingerprint,'thin_capture_batch_payload_changed')
            return
        tx.create(batch_ref,{'batch_id':batch_ref.id,'payload':encrypted_payload,'created_at':now,
                            'source_payload_sha256':fingerprint})
    return run_transaction(db,effect)

if __name__ == '__main__':
    import contextlib, os, sys
    fd = os.dup(1)
    try:
        raw = sys.stdin.buffer.read(8193)
        need(len(raw) <= 8192, 'thin_capture_worker_packet')
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), 1)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = worker(json.loads(raw))
        with os.fdopen(fd, 'w') as receipt:
            fd = None
            print(json.dumps(result), file=receipt, flush=True)
    except BaseException:
        sys.exit(2)
    finally:
        if fd is not None:
            os.close(fd)

