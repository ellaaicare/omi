"""Account deletion hooks for the separate Thin target.
Retain an opaque lifecycle fence after private receipt/request erasure. It keeps
old JWTs/decision bodies invalid even when users/{uid} is later garbage-collected.
No ordinary consent decision invokes these hooks.
"""
from datetime import datetime,timezone
from ella.services.thin_cloudbot_consent import COLLECTION,need,sha

def retire_in_transaction(tx,db,uid,request_id,now):
    need(type(uid) is str and bool(uid) and '/' not in uid,'thin_owner_invalid')
    need(type(request_id) is str and 8<=len(request_id)<=128,'thin_deletion_request_invalid')
    user=db.collection('users').document(uid);snap=user.get(transaction=tx);state=(snap.to_dict() or {}).get('ai_consent')
    need(type(state) is dict and state.get('decision')=='deleted','thin_deletion_not_recorded')
    rid=state.get('receipt_id');need(type(rid) is str and bool(rid),'thin_deletion_not_recorded')
    row=user.collection('ai_consent_receipts').document(rid).get(transaction=tx);receipt=row.to_dict() or {}
    need(receipt.get('subject_uid')==uid and receipt.get('request_id')==request_id
         and receipt.get('receipt_id')==rid and receipt.get('decision')=='deleted','thin_deletion_binding_mismatch')
    ref=db.collection(COLLECTION).document(uid);old=ref.get(transaction=tx);doc=old.to_dict() or {}
    prior=doc.get('lifecycle')
    if type(prior) is dict and prior.get('request_id')==request_id:
        need(doc.get('current_state') is None,'thin_deletion_state_changed')
        return {'retired':True,'writes':0}
    generation=doc.get('generation',0);need(type(generation) is int and generation>=0,'thin_generation_invalid')
    lifecycle={'request_id':request_id,'retired_at':now.isoformat(),'erasure_state':'pending','epoch_hash':sha('ella-thin-deletion-v1\x1f'+uid+'\x1f'+request_id)}
    tx.set(ref,{'current_state':None,'generation':generation+1,'lifecycle':lifecycle})
    return {'retired':True,'writes':1}

def retire_for_account_deletion(db,uid,request_id):
    from google.cloud.firestore_v1 import transactional
    return transactional(lambda tx:retire_in_transaction(tx,db,uid,request_id,datetime.now(timezone.utc)))(db.transaction())

def erase_retired_private_history(db,uid):
    # Called only by the existing account-data erasure path, not a public route.
    ref=db.collection(COLLECTION).document(uid);snap=ref.get()
    if not snap.exists:return {'erased':0,'lifecycle_retained':False}
    doc=snap.to_dict() or {};life=doc.get('lifecycle')
    need(type(life) is dict,'thin_erasure_not_retired')
    if life.get('erasure_state')=='completed':return {'erased':0,'lifecycle_retained':True}
    need(life.get('erasure_state')=='pending' and doc.get('current_state') is None,'thin_erasure_not_retired')
    removed=0
    for collection in ('receipts','requests'):
        while True:
            rows=list(ref.collection(collection).limit(200).stream())
            if not rows:break
            batch=db.batch()
            for row in rows:batch.delete(row.reference)
            batch.commit();removed+=len(rows)
    # Capture dedup metadata is top-level and owner-bound, not under users/{uid}.
    # Sweep only the two dedicated Thin collections; never Main/personal jobs.
    from google.cloud.firestore_v1 import FieldFilter
    for collection in ('ella_thin_cloudbot_capture_segments_v1','ella_thin_cloudbot_capture_batches_v1'):
        while True:
            rows=list(db.collection(collection).where(filter=FieldFilter('uid','==',uid)).limit(200).stream())
            if not rows:break
            batch=db.batch()
            for row in rows:batch.delete(row.reference)
            batch.commit();removed+=len(rows)
    # New grants remain blocked during erasure, preventing new-receipt races.
    from google.cloud.firestore_v1 import transactional
    def finish(tx):
        current=ref.get(transaction=tx).to_dict() or {}
        need(current.get('lifecycle')==life and current.get('current_state') is None,'thin_erasure_lifecycle_changed')
        tx.set(ref,{**current,'lifecycle':{**life,'erasure_state':'completed'}})
    transactional(finish)(db.transaction())
    # Never delete root: its deletion epoch fences stale tokens after users erase.
    return {'erased':removed,'lifecycle_retained':True}
