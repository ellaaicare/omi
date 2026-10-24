"""Separate Thin target. No model/provisioning/Hermes dependency or Main writes.
SOURCE candidate: authenticated router registration/deployment remains separate.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime,timezone
from ella.services.thin_cloudbot_consent_contract import (
    TARGET, POLICY, FIELDS, STATE_FIELDS, ContractHold, policy, hexhash,
    receipt_digest, validate_current_grant, aware,
)
COLLECTION='ella_thin_cloudbot_consent'
SUBMISSION_FIELDS={'decision','policy_version','processor_set_hash','scope_version',
 'scope_hash','request_id','app_version','build_number','locale','account_epoch_token','expected_current_receipt_id'}
class ThinConsentHold(ValueError):
    def __init__(self,code):super().__init__(code);self.code=code

def need(v,code):
    if not v:raise ThinConsentHold(code)

def sha(v):return hashlib.sha256(v.encode('utf8')).hexdigest()

def account_context(uid,auth_time,main_state,main_receipt,now):
    """Read existing shared account-deletion epoch, never require a Main grant.
    Inputs come from verified Firebase identity and transactionally read server
    documents, never request JSON. Main declined/revoked is not a Thin grant.
    A deleted/recreated same UID requires a newer verified Firebase auth_time.
    """
    need(type(uid) is str and bool(uid) and '/' not in uid,'thin_owner_invalid')
    need(type(auth_time) is int and auth_time>0,'thin_authenticated_auth_time_required')
    base='initial'
    if main_state or main_receipt:
        need(type(main_state) is dict and type(main_receipt) is dict
             and main_receipt.get('subject_uid')==uid,'thin_account_authority_unavailable')
        keys=('receipt_id','decision','account_epoch_hash','deletion_phase','deletion_completed_at')
        need(all(main_state.get(k)==main_receipt.get(k) for k in keys)
             and type(main_state.get('receipt_id')) is str and bool(main_state['receipt_id']),
             'thin_account_authority_unavailable')
        decision=main_state.get('decision')
        need(decision in {'granted','declined','revoked','deleted'},'thin_account_authority_unavailable')
        parent=main_state.get('account_epoch_hash') or ''
        if decision=='deleted':
            completed=aware(main_state.get('deletion_completed_at'));token=main_receipt.get('account_epoch_token')
            need(main_state.get('deletion_phase')=='completed' and completed is not None
                 and completed<=now and auth_time>int(completed.timestamp()),'thin_account_deleted')
            need(type(token) is str and bool(token) and parent==
                 'sha256:'+sha('ella-ai-consent-account-epoch-v1\x1f'+token),
                 'thin_account_authority_unavailable')
        need(type(parent) is str and (parent=='' or re.fullmatch(r'sha256:[0-9a-f]{64}',parent)),
             'thin_account_authority_unavailable')
        base=parent or 'initial'
    epoch=sha('ella-thin-cloudbot-account-epoch-v1\x1f'+uid+'\x1f'+base)
    return {'owner_sha256':sha(uid),'epoch_hash':epoch,'epoch_token':epoch,
            'profile_binding_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'ella-thin-owner-consent-v1:'+uid+':'+epoch))}

def submission(body,context,*,require_current_policy=True):
    need(type(body) is dict and set(body)==SUBMISSION_FIELDS,'thin_decision_shape')
    need(body['decision'] in {'granted','declined','revoked'},'thin_decision_invalid')
    for key in ('policy_version','scope_version'):
        need(type(body[key]) is str and 1<=len(body[key])<=128,'thin_policy_mismatch')
    for key in ('processor_set_hash','scope_hash'):
        need(type(body[key]) is str and re.fullmatch(r'sha256:[0-9a-f]{64}',body[key]),'thin_policy_mismatch')
    if require_current_policy:
        for key in ('policy_version','processor_set_hash','scope_version','scope_hash'):
            need(body[key]==POLICY[key],'thin_policy_mismatch')
    for key,low,high in [('request_id',8,128),('app_version',1,80),('build_number',1,40),('locale',2,40)]:
        value=body[key];need(type(value) is str and low<=len(value)<=high
                           and all(32<ord(c)<127 for c in value),'thin_metadata_invalid')
    need('/' not in body['request_id'],'thin_request_identity')
    expected=body['expected_current_receipt_id']
    need(type(expected) is str and (expected=='' or re.fullmatch(r'tccr_[0-9a-f]{32}',expected)),'thin_expected_current_receipt_invalid')
    need(body['account_epoch_token']==context['epoch_token'],'thin_account_epoch_mismatch')
    return dict(body)

def public_receipt(value,context,now):
    need(type(value) is dict and set(value)==FIELDS,'thin_receipt_shape')
    need(value['consent_target']==TARGET and value['subject_owner_sha256']==context['owner_sha256']
         and value['account_epoch_hash']==context['epoch_hash']
         and value['profile_binding_id']==context['profile_binding_id'],'thin_receipt_owner_or_epoch')
    need(value['decision'] in {'granted','declined','revoked'},'thin_decision_invalid')
    # Historical receipt integrity is independent of CURRENT policy authorization.
    # Its exact pointer/digest remains mandatory; admission validates current POLICY.
    for key in ('policy_version','scope_version'):
        need(type(value[key]) is str and 1<=len(value[key])<=128,'thin_policy_shape')
    for key in ('processor_set_hash','scope_hash'):
        need(type(value[key]) is str and re.fullmatch(r'sha256:[0-9a-f]{64}',value[key]),'thin_policy_shape')
    ids=value['processor_ids']
    need(type(ids) is list and bool(ids) and all(type(i) is str and 0<len(i)<=128 for i in ids) and len(ids)==len(set(ids)),'thin_policy_shape')
    for key,low,high in [('app_version',1,80),('build_number',1,40),('locale',2,40)]:
        item=value[key];need(type(item) is str and low<=len(item)<=high and all(32<ord(c)<127 for c in item),'thin_receipt_metadata_invalid')
    at=aware(value['server_decided_at']);need(at is not None and at<=now,'thin_server_timestamp_invalid')
    need(type(value['receipt_id']) is str and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',value['receipt_id'])
         and type(value['request_id']) is str and 8<=len(value['request_id'])<=128,'thin_receipt_identity')
    return json.loads(json.dumps(value))

def validate_pointer(state,receipt):
    need(type(state) is dict and set(state)==STATE_FIELDS,'thin_current_state_shape')
    need(all(state[k]==receipt[k] for k in STATE_FIELDS-{'receipt_sha256'})
         and hexhash(state['receipt_sha256']) and state['receipt_sha256']==receipt_digest(receipt),
         'thin_current_receipt_mismatch')

class FirestoreThinConsentRepository:
    def __init__(self,db,*,transaction_runner=None):self.db=db;self.runner=transaction_runner
    def _run(self,fn):
        if self.runner is not None:return self.runner(fn)
        from google.cloud.firestore_v1 import transactional
        return transactional(fn)(self.db.transaction())
    def _read(self,tx,uid,auth_time,now,receipt_id=None):
        user=self.db.collection('users').document(uid);snap=user.get(transaction=tx);state=(snap.to_dict() or {}).get('ai_consent') if snap.exists else None
        rid=(state or {}).get('receipt_id');main_receipt=None
        if rid:
            rs=user.collection('ai_consent_receipts').document(rid).get(transaction=tx);main_receipt=rs.to_dict() if rs.exists else None
        context=account_context(uid,auth_time,state,main_receipt,now)
        current_ref=self.db.collection(COLLECTION).document(uid);current=current_ref.get(transaction=tx);current_doc=current.to_dict() or {};thin=current_doc.get('current_state') if current.exists else None
        lifecycle=current_doc.get('lifecycle')
        if lifecycle is not None:
            need(type(lifecycle) is dict and set(lifecycle)=={'request_id','retired_at','epoch_hash','erasure_state'},'thin_lifecycle_invalid')
            need(lifecycle['erasure_state']=='completed','thin_account_erasure_pending')
            at=aware(lifecycle['retired_at'])
            need(at is not None and at<=now and hexhash(lifecycle['epoch_hash']),'thin_lifecycle_invalid')
            need(auth_time>int(at.timestamp()),'thin_account_deleted')
            epoch=sha('ella-thin-cloudbot-account-epoch-v1\x1f'+uid+'\x1fthin-deletion:'+lifecycle['epoch_hash'])
            context={'owner_sha256':sha(uid),'epoch_hash':epoch,'epoch_token':epoch,
                'profile_binding_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'ella-thin-owner-consent-v1:'+uid+':'+epoch))}
        chosen=receipt_id or (thin or {}).get('receipt_id');private=None
        if chosen:
            need(type(chosen) is str and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',chosen),'thin_receipt_identity')
            rs=current_ref.collection('receipts').document(chosen).get(transaction=tx);private=rs.to_dict() if rs.exists else None
            if private:need(private.get('owner_uid')==uid,'thin_receipt_owner_mismatch')
        return context,thin,private,current_ref
    def read(self,uid,auth_time,now,receipt_id=None):
        return self._run(lambda tx:self._read(tx,uid,auth_time,now,receipt_id)[:3])
    def assert_witness_in_transaction(self,tx,uid,auth_time,witness,now):
        context,state,private,ref=self._read(tx,uid,auth_time,now)
        receipt=public_receipt((private or {}).get('receipt'),context,now)
        validate_current_grant(state=state,receipt=receipt,
            authenticated_owner_sha256=context['owner_sha256'],
            current_account_epoch_hash=context['epoch_hash'],account_active=True,now=now)
        expected={'consent_target':TARGET,'owner_sha256':context['owner_sha256'],
            'account_epoch_hash':context['epoch_hash'],'receipt_id':receipt['receipt_id'],
            'receipt_sha256':receipt_digest(receipt),'profile_binding_id':receipt['profile_binding_id']}
        need(type(witness) is dict and witness==expected,'thin_admission_witness_changed')
        return expected
    def decide(self,uid,auth_time,body,now,*,reconcile=False):
        def effect(tx):
            context,state,private,ref=self._read(tx,uid,auth_time,now)
            value=submission(body,context,require_current_policy=not reconcile);request_ref=ref.collection('requests').document(sha(value['request_id']));old=request_ref.get(transaction=tx)
            fingerprint=sha(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False))
            existing=state if state and state.get('account_epoch_hash')==context['epoch_hash'] else None
            current_id=existing.get('receipt_id') if existing else None
            if existing:
                current=public_receipt((private or {}).get('receipt'),context,now);validate_pointer(existing,current)
            current_doc=ref.get(transaction=tx).to_dict() or {}
            generation=current_doc.get('generation',0) if existing else 0
            need(type(generation) is int and generation>=0,'thin_generation_invalid')
            operation_id='tccr_'+sha(TARGET+':'+uid+':'+value['request_id'])[:32]
            if old.exists:
                row=old.to_dict();need(row.get('owner_uid')==uid and row.get('epoch_hash')==context['epoch_hash']
                     and row.get('fingerprint')==fingerprint,'thin_idempotency_conflict')
                receipt=None
                if row['state']=='applied':
                    rs=ref.collection('receipts').document(row['receipt_id']).get(transaction=tx)
                    need(rs.exists and rs.to_dict().get('owner_uid')==uid,'thin_receipt_unavailable')
                    receipt=public_receipt(rs.to_dict()['receipt'],context,now)
                state_name='superseded' if row['state']=='applied' and current_id!=operation_id else row['state']
                return {'receipt':receipt,'idempotent_replay':True,'decision_operation':{
                    'contract':'thin-cloudbot-consent-decision-v1','state':state_name,
                    'operation_receipt_id':operation_id,'resulting_current_receipt_id':row.get('receipt_id'),
                    'current_receipt_id':current_id,'generation':row['generation'],'may_reexecute':False}}
            apply=not reconcile and value['expected_current_receipt_id']==(current_id or '')
            receipt=None;next_generation=generation
            if apply:
                next_generation=generation+1
                receipt={'consent_target':TARGET,'subject_owner_sha256':context['owner_sha256'],
                 'account_epoch_hash':context['epoch_hash'],'profile_binding_id':context['profile_binding_id'],
                 'receipt_id':operation_id,'request_id':value['request_id'],'decision':value['decision'],
                 **{k:POLICY[k] for k in ('policy_version','processor_set_hash','scope_version','scope_hash','processor_ids')},
                 'server_decided_at':now.isoformat(),
                 **{k:value[k] for k in ('app_version','build_number','locale')}}
                pointer={k:receipt[k] for k in STATE_FIELDS if k!='receipt_sha256'};pointer['receipt_sha256']=receipt_digest(receipt)
                tx.create(ref.collection('receipts').document(operation_id),
                          {'owner_uid':uid,'receipt':receipt,'verified_auth_time':auth_time,
                           'metadata':{k:value[k] for k in ('app_version','build_number','locale')}})
                tx.set(ref,{'current_state':pointer,'generation':next_generation,'lifecycle':current_doc.get('lifecycle')})
            # Reconcile absence creates a terminal NOT_APPLIED tombstone. A delayed
            # original POST conflicts/re-reads this record and can never execute.
            # CAS conflict also closes only this decision, never the current grant.
            tx.create(request_ref,{'owner_uid':uid,'epoch_hash':context['epoch_hash'],
                      'fingerprint':fingerprint,'state':'applied' if apply else 'not_applied',
                      'receipt_id':operation_id if apply else None,'generation':next_generation})
            return {'receipt':receipt,'idempotent_replay':False,'decision_operation':{
                'contract':'thin-cloudbot-consent-decision-v1','state':'applied' if apply else 'not_applied',
                'operation_receipt_id':operation_id,'resulting_current_receipt_id':operation_id if apply else None,
                'current_receipt_id':operation_id if apply else current_id,
                'generation':next_generation,'may_reexecute':False}}
        return self._run(effect)

class ThinCloudbotConsentService:
    def __init__(self,repository,*,clock=lambda:datetime.now(timezone.utc)):
        self.repository=repository;self.clock=clock
    def status(self,uid,auth_time):
        now=self.clock();context,state,private=self.repository.read(uid,auth_time,now)
        receipt=(private or {}).get('receipt');authority='not_accepted'
        if state and receipt and state.get('account_epoch_hash')==context['epoch_hash']:
            receipt=public_receipt(receipt,context,now);validate_pointer(state,receipt)
            try:
                validate_current_grant(state=state,receipt=receipt,
                  authenticated_owner_sha256=context['owner_sha256'],
                  current_account_epoch_hash=context['epoch_hash'],account_active=True,now=now)
                authority='authorized'
            except ContractHold:
                authority='revoked' if state.get('decision')=='revoked' else 'declined' if state.get('decision')=='declined' else 'policy_outdated' if any(receipt[k]!=POLICY[k] for k in ('policy_version','processor_set_hash','scope_version','scope_hash','processor_ids')) else 'unavailable'
        else:state=receipt=None
        return {'consent_target':TARGET,'policy':policy(),'subject_owner_sha256':context['owner_sha256'],
                'account_epoch_token':context['epoch_token'],'account_epoch_hash':context['epoch_hash'],
                'profile_binding_id':context['profile_binding_id'],
                'authority_state':authority,'authorized':authority=='authorized',
                'current_state':state,'receipt':receipt,'consent':receipt if receipt else {'decision':'not_recorded','receipt_id':None},
                'observed_at_utc':now.isoformat()}
    def receipt(self,uid,auth_time,receipt_id):
        context,state,private=self.repository.read(uid,auth_time,self.clock(),receipt_id)
        receipt=(private or {}).get('receipt')
        if not receipt or receipt.get('account_epoch_hash')!=context['epoch_hash']:return None
        return public_receipt(receipt,context,self.clock())
    def _decision(self,uid,auth_time,body,*,reconcile):
        result=self.repository.decide(uid,auth_time,body,self.clock(),reconcile=reconcile)
        view=self.status(uid,auth_time);context={'owner_sha256':view['subject_owner_sha256'],'epoch_hash':view['account_epoch_hash'],'profile_binding_id':view['profile_binding_id']}
        if result['receipt'] is not None:result['receipt']=public_receipt(result['receipt'],context,self.clock())
        # Current status is observed separately after operation commit; if another
        # decision superseded it, show that fact rather than promote old authority.
        current=(view.get('current_state') or {}).get('receipt_id')
        result['decision_operation']['current_receipt_id']=current
        if result['decision_operation']['state']=='applied' and current!=result['decision_operation']['operation_receipt_id']:
            result['decision_operation']['state']='superseded'
        return {**view,**result,'current_receipt':view.get('receipt'),'status':view}
    def submit(self,uid,auth_time,body):return self._decision(uid,auth_time,body,reconcile=False)
    def reconcile(self,uid,auth_time,body):return self._decision(uid,auth_time,body,reconcile=True)
    def require_current(self,uid,auth_time):
        value=self.status(uid,auth_time);need(value['authorized'],'thin_current_consent_required')
        return {'consent_target':TARGET,'owner_sha256':value['subject_owner_sha256'],
                'account_epoch_hash':value['account_epoch_hash'],
                'receipt_id':value['receipt']['receipt_id'],'receipt_sha256':receipt_digest(value['receipt']),
                'profile_binding_id':value['receipt']['profile_binding_id']}
