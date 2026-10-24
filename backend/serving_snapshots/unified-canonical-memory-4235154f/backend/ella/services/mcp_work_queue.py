"""Per-owner pending work on the existing document DB; disabled without policy.

No credentials, provider, email, memory or Feed operations. A policy is a separate
operator-controlled admission decision, never created by a tool or app sign-in.
"""
from __future__ import annotations
import copy
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from ella.services import mcp_summary_enrichment as summaries

KINDS = {"enrichment", "scanner_escalation"}
STATES = {"pending", "running", "retryable", "dead_letter", "completed"}
ERRORS = {"transport_unavailable", "provider_unavailable", "source_changed", "unsafe_summary", "consumer_failed", "delivery_unknown"}
RETRYABLE = {"transport_unavailable", "provider_unavailable"}
CONTRACT = "omi-mcp-work-v1"


class WorkError(ValueError): pass


def utcnow(): return datetime.now(timezone.utc)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value: raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise WorkError("work_identity_invalid") from error
    return value


def _hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise WorkError("work_identity_invalid")
    return value


def policy_ready(policy, kind, now):
    if not isinstance(policy, dict): raise WorkError("work_policy_not_active")
    try:
        _uuid(policy['epoch'])
        valid = (policy.get('consumer') == 'muse' and policy['state'] == 'active'
                 and isinstance(policy['kinds'],list) and set(policy['kinds']) <= KINDS
                 and len(set(policy['kinds'])) == len(policy['kinds']) and kind in policy['kinds']
                 and isinstance(policy['source_after'], datetime) and policy['source_after'].tzinfo is not None
                 and isinstance(policy['expires_at'], datetime) and policy['expires_at'].tzinfo is not None
                 and policy['source_after'] <= now < policy['expires_at']
                 and policy['expires_at']-policy['source_after'] <= timedelta(days=1)
                 and policy.get('admission_scope') in {'explicit_source_ids','new_completions'}
                 and (policy['admission_scope'] == 'new_completions' or (
                     isinstance(policy.get('source_ids'),list) and 0 < len(policy['source_ids']) <= 50
                     and all(isinstance(v,str) and v and len(v)<=256 and '/' not in v for v in policy['source_ids'])))
                 and type(policy['max_attempts']) is int and 1 <= policy['max_attempts'] <= 3
                 and type(policy['lease_seconds']) is int and 10 <= policy['lease_seconds'] <= 300
                 and type(policy['retry_delay_seconds']) is int and 10 <= policy['retry_delay_seconds'] <= 600
                 and type(policy['max_admissions']) is int and 1 <= policy['max_admissions'] <= 50
                 and type(policy['admissions']) is int and 0 <= policy['admissions'] <= policy['max_admissions'])
    except (KeyError, TypeError, ValueError): valid = False
    if not valid: raise WorkError("work_policy_not_active")


def job_identity(kind, source_id, source_hash):
    if not isinstance(kind,str) or kind not in KINDS or not isinstance(source_id, str) or not source_id or len(source_id) > 256 or '/' in source_id:
        raise WorkError("work_identity_invalid")
    _hash(source_hash)
    return hashlib.sha256((kind+'\0'+source_id+'\0'+source_hash).encode()).hexdigest()


def admission(policy, existing, *, kind, source_id, source_hash, completed_at, now):
    policy_ready(policy, kind, now)
    job_id = job_identity(kind, source_id, source_hash)
    if policy['admission_scope'] == 'explicit_source_ids' and source_id not in policy['source_ids']:
        raise WorkError('source_not_admitted')
    if not isinstance(completed_at, datetime) or completed_at.tzinfo is None or not policy['source_after'] <= completed_at <= now:
        raise WorkError("old_or_unknown_source_refused")
    if existing is not None:
        if any(existing.get(k) != v for k,v in {'job_id':job_id,'kind':kind,'source_id':source_id,
                                                'source_snapshot_sha256':source_hash,'policy_epoch':policy['epoch']}.items()):
            raise WorkError("work_identity_conflict")
        return None, None
    if policy['admissions'] >= policy['max_admissions']: raise WorkError("work_admission_budget_exhausted")
    row = {'contract':CONTRACT,'job_id':job_id,'kind':kind,'source_id':source_id,'source_snapshot_sha256':source_hash,
           'policy_epoch':policy['epoch'],'operation_id':str(uuid.uuid5(uuid.NAMESPACE_URL, policy['epoch']+':'+job_id)),
           'status':'pending','attempts':0,'lease_token':None,'lease_expires_at':None,'next_attempt_at':now,
           'created_at':now,'source_completed_at':completed_at,'updated_at':now,'last_error':None,'result':None}
    return row, {**policy,'admissions':policy['admissions']+1}


def claim(policy, row, now):
    policy_ready(policy, row['kind'], now)
    if row['policy_epoch'] != policy['epoch']: raise WorkError("work_policy_changed")
    if row['status'] in {'completed','dead_letter'}: return None
    due = (row['status'] in {'pending','retryable'} and row['next_attempt_at'] <= now)
    expired = row['status'] == 'running' and row['lease_expires_at'] <= now
    if not due and not expired: return None
    if row['attempts'] >= policy['max_attempts']:
        return {**row,'status':'dead_letter','last_error':'attempts_exhausted','lease_token':None,'lease_expires_at':None,'updated_at':now}
    return {**row,'status':'running','attempts':row['attempts']+1,'lease_token':secrets.token_urlsafe(24),
            'lease_expires_at':min(now+timedelta(seconds=policy['lease_seconds']),policy['expires_at']), 'updated_at':now}


def lease_valid(policy, row, token, epoch, now):
    policy_ready(policy,row['kind'],now)
    if (row['policy_epoch'] != epoch or policy['epoch'] != epoch or row['status'] != 'running'
            or not isinstance(token,str) or not secrets.compare_digest(row.get('lease_token') or '',token)
            or not isinstance(row['lease_expires_at'],datetime) or now >= row['lease_expires_at']):
        raise WorkError("work_lease_lost")


def fail(policy,row,*,token,epoch,error,now):
    lease_valid(policy,row,token,epoch,now)
    if not isinstance(error,str) or error not in ERRORS: raise WorkError("work_error_code_invalid")
    retry = error in RETRYABLE and row['attempts'] < policy['max_attempts']
    return {**row,'status':'retryable' if retry else 'dead_letter','last_error':error,
            'next_attempt_at':now+timedelta(seconds=policy['retry_delay_seconds']) if retry else None,
            'lease_token':None,'lease_expires_at':None,'updated_at':now}


def public(row, include_lease=False):
    result = {k:copy.deepcopy(v) for k,v in row.items() if k not in {'lease_token','completed_lease_sha256'}}
    if include_lease: result['lease_token'] = row['lease_token']
    return result


class OwnerWorkQueue:
    """Existing Firestore-compatible DB, same users/{authenticated uid} boundary.

    `transactional` is the deployed SDK decorator; injectable for synthetic tests.
    The queue writes no source text and cannot enumerate another owner.
    """
    def __init__(self, db, uid, conversations_db, transactional, clock=utcnow):
        self.db,self.uid,self.conversations,self.transactional,self.clock = db,uid,conversations_db,transactional,clock
        self.owner = db.collection('users').document(uid)

    def _refs(self,kind,job_id):
        if not isinstance(kind,str) or kind not in KINDS: raise WorkError('work_kind_invalid')
        return (self.owner.collection('ella_mcp_work_policy_v1').document('current'),
                self.owner.collection('ella_mcp_work_'+kind+'_v1').document(_hash(job_id)))

    def _run(self,kind,job_id,action):
        policy_ref,job_ref = self._refs(kind,job_id)
        def run(tx):
            p=policy_ref.get(transaction=tx); j=job_ref.get(transaction=tx)
            policy=p.to_dict() if p.exists else None; row=j.to_dict() if j.exists else None
            policy_ready(policy,kind,self.clock())
            if row is not None and (row.get('job_id') != job_id or row.get('kind') != kind
                                    or row.get('policy_epoch') != policy['epoch']):
                raise WorkError('work_identity_conflict')
            return action(tx,policy_ref,job_ref,policy,row)
        return self.transactional(run)(self.db.transaction())

    def enqueue_completed(self,kind,source_id,source_hash,completed_at):
        """Trusted backend admission, never an MCP consumer tool/backlog scan."""
        job_id=job_identity(kind,source_id,source_hash)
        def action(tx,pr,jr,p,j):
            row,policy=admission(p,j,kind=kind,source_id=source_id,source_hash=source_hash,completed_at=completed_at,now=self.clock())
            if row is not None: tx.set(jr,row); tx.set(pr,policy)
            return public(row or j)
        return self._run(kind,job_id,action)

    def list_work(self,kind,status='pending',limit=20):
        if (not isinstance(kind,str) or kind not in KINDS or not isinstance(status,str) or status not in STATES
                or type(limit) is not int or not 1<=limit<=25): raise WorkError('work_list_invalid')
        pr,_=self._refs(kind,'0'*64); s=pr.get(); policy=s.to_dict() if s.exists else None
        policy_ready(policy,kind,self.clock())
        from google.cloud.firestore_v1 import FieldFilter
        query=(self.owner.collection('ella_mcp_work_'+kind+'_v1')
               .where(filter=FieldFilter('policy_epoch','==',policy['epoch']))
               .where(filter=FieldFilter('status','==',status)).limit(limit+1))
        rows=[x.to_dict() for x in query.stream(timeout=10,retry=None)]
        return {'contract':CONTRACT,'items':[public(x) for x in rows[:limit]],'has_more':len(rows)>limit,
                'bounded_sample':True,'incremental_cursor':False}

    def get_work(self,kind,job_id):
        return self._run(kind,job_id,lambda tx,pr,jr,p,j: public(j) if j is not None else None)

    def claim_work(self,kind,job_id):
        def action(tx,pr,jr,p,j):
            if j is None: raise WorkError('work_not_found')
            changed=claim(p,j,self.clock())
            if changed is not None: tx.set(jr,changed)
            return public(changed,include_lease=True) if changed and changed['status']=='running' else None
        return self._run(kind,job_id,action)

    def fail_work(self,kind,job_id,token,epoch,error):
        def action(tx,pr,jr,p,j):
            if j is None: raise WorkError('work_not_found')
            changed=fail(p,j,token=token,epoch=epoch,error=error,now=self.clock());tx.set(jr,changed);return public(changed)
        return self._run(kind,job_id,action)

    def enrich(self,arguments,category_validator):
        """Summary/version/receipt and job completion in ONE database transaction."""
        args=copy.deepcopy(arguments); bound=args.pop('queue_claim',None)
        if not isinstance(bound,dict) or set(bound)!={'job_id','lease_token','policy_epoch'}: raise WorkError('work_claim_invalid')
        _uuid(bound['policy_epoch']); queue=self
        class BoundDatabase:
            build_summary_version_update=staticmethod(queue.conversations.build_summary_version_update)
            def update_conversation_with_builder(self,uid,cid,build):
                if uid!=queue.uid: raise WorkError('work_owner_mismatch')
                def action(tx,pr,jr,p,j):
                    if j is None or j['source_id']!=cid: raise WorkError('work_source_mismatch')
                    if (args.get('enrichment',{}).get('operation_id')!=j['operation_id'] or
                            args.get('enrichment',{}).get('expected_snapshot_sha256')!=j['source_snapshot_sha256']):
                        raise WorkError('work_source_mismatch')
                    replay = j['status'] == 'completed'
                    if replay:
                        token_hash = hashlib.sha256(str(bound['lease_token']).encode()).hexdigest()
                        if not secrets.compare_digest(j.get('completed_lease_sha256') or '', token_hash):
                            raise WorkError('work_lease_lost')
                    else:
                        lease_valid(p,j,bound['lease_token'],bound['policy_epoch'],queue.clock())
                    # Safe replay after an unknown response is READ ONLY and
                    # requires identical original request, even if the lease has
                    # since retired: exposed through exact completed job status.
                    def guarded(conversation):
                        if not replay: lease_valid(p,j,bound['lease_token'],bound['policy_epoch'],queue.clock())
                        updates,result=build(conversation)
                        if replay and (updates or not result.get('idempotent_replay')):
                            raise WorkError('work_completed_receipt_mismatch')
                        if not replay: lease_valid(p,j,bound['lease_token'],bound['policy_epoch'],queue.clock())
                        return updates,result
                    ref=queue.owner.collection(queue.conversations.conversations_collection).document(cid)
                    result=queue.conversations._update_conversation_with_builder_transaction(tx,ref,uid,guarded)
                    if result is None: raise WorkError('work_source_missing')
                    if not replay:
                        tx.set(jr,{**j,'status':'completed','result':result['result'],'lease_token':None,
                                   'completed_lease_sha256':hashlib.sha256(bound['lease_token'].encode()).hexdigest(),
                                   'lease_expires_at':None,'completed_at':queue.clock(),'updated_at':queue.clock()})
                    return result
                return queue._run('enrichment',bound['job_id'],action)
        return summaries.enrich_summary(BoundDatabase(),self.uid,args,category_validator)

    def ack_scanner(self,job_id,token,epoch,receipt_hash):
        _hash(receipt_hash)
        def action(tx,pr,jr,p,j):
            if j is None: raise WorkError('work_not_found')
            lease_valid(p,j,token,epoch,self.clock())
            row={**j,'status':'completed','result':{'delivery_receipt_sha256':receipt_hash,'verification':'consumer_reported'},
                 'lease_token':None,'lease_expires_at':None,'completed_at':self.clock(),'updated_at':self.clock()}
            tx.set(jr,row);return public(row)
        return self._run('scanner_escalation',job_id,action)


def configured_queue(uid):
    # No policy/grant creation and no fallback to an operator/private principal.
    if os.environ.get('ELLA_MCP_WORK_QUEUE_ENABLED') != 'true':
        raise WorkError('work_queue_disabled')
    from database._client import db
    import database.conversations as conversations
    from google.cloud.firestore_v1 import transactional
    return OwnerWorkQueue(db,uid,conversations,transactional)



def tools():
    kind={'type':'string','enum':sorted(KINDS)}; job={'type':'string','pattern':'^[0-9a-f]{64}$'}
    token={'type':'string'}; epoch={'type':'string','format':'uuid'}
    def spec(name,description,properties,required):
        return {'name':name,'description':description,'inputSchema':{'type':'object','additionalProperties':False,
                'properties':properties,'required':required}}
    return [
        spec('ella_list_work','List a bounded owner-only status sample; requires an active per-user policy. Not a correction feed.',
             {'kind':kind,'status':{'type':'string','enum':sorted(STATES)},'limit':{'type':'integer','minimum':1,'maximum':25}},['kind']),
        spec('ella_get_work','Get exact owner job state and durable result; contains no transcript body.',{'kind':kind,'job_id':job},['kind','job_id']),
        spec('ella_claim_work','Atomically claim one owner job; bounded expiring lease and attempts; only claim response exposes lease token.',
             {'kind':kind,'job_id':job},['kind','job_id']),
        spec('ella_fail_work','Retire a matching live lease with a closed error. Ambiguous delivery is dead-lettered, never blindly retried.',
             {'kind':kind,'job_id':job,'lease_token':token,'policy_epoch':epoch,'error':{'type':'string','enum':sorted(ERRORS)}},
             ['kind','job_id','lease_token','policy_epoch','error']),
        spec('ella_ack_scanner_work','Acknowledge a consumer-reported scanner receipt; does not send or prove delivery of email.',
             {'job_id':job,'lease_token':token,'policy_epoch':epoch,'receipt_sha256':job},
             ['job_id','lease_token','policy_epoch','receipt_sha256']),
    ]


def execute(queue,name,args):
    schemas={s['name']:s['inputSchema'] for s in tools()}
    schema=schemas.get(name)
    if schema is None or not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema['required'])-set(args):
        raise WorkError('work_request_invalid')
    if name=='ella_list_work': return queue.list_work(args['kind'],args.get('status','pending'),args.get('limit',20))
    if name=='ella_get_work': return queue.get_work(args['kind'],args['job_id'])
    if name=='ella_claim_work': return queue.claim_work(args['kind'],args['job_id'])
    if name=='ella_fail_work': return queue.fail_work(args['kind'],args['job_id'],args['lease_token'],args['policy_epoch'],args['error'])
    return queue.ack_scanner(args['job_id'],args['lease_token'],args['policy_epoch'],args['receipt_sha256'])


def enqueue_from_stock(transaction, db, uid, conversation, *, now, source_completed_at):
    """Optional dispatch selection at the existing stock completion outbox hook.

    Absent/inactive policy leaves the existing Hermes path unchanged. Active
    Muse policy selects only NEW completions; no old outbox rows are read/reset.
    This function is called before the parent conversation write, in its SAME
    transaction. All reads precede writes. No new worker/infrastructure needed.
    """
    # Default-off preserves even the legacy transaction's read set. Activation
    # needs a separate reviewed feature flag AND an explicit owner policy.
    if os.environ.get('ELLA_MCP_WORK_QUEUE_ENABLED') != 'true': return None
    owner=db.collection('users').document(uid)
    pr=owner.collection('ella_mcp_work_policy_v1').document('current')
    snapshot=pr.get(transaction=transaction)
    policy=snapshot.to_dict() if snapshot.exists else None
    try: policy_ready(policy,'enrichment',now)
    except WorkError: return None
    cid=conversation.get('id')
    try:
        expected=summaries.preconditions(uid,cid,conversation)
        source_hash=expected['expected_snapshot_sha256']
        job_id=job_identity('enrichment',cid,source_hash)
        jr=owner.collection('ella_mcp_work_enrichment_v1').document(job_id)
        saved=jr.get(transaction=transaction)
        row,changed=admission(policy,saved.to_dict() if saved.exists else None,kind='enrichment',source_id=cid,
                              source_hash=source_hash,completed_at=source_completed_at,now=now)
    except (WorkError,summaries.EnrichmentError) as error:
        # Preserve stock summary completion. Caller can expose the admission
        # disposition; no model dispatch/backlog fallback for a selected owner.
        return {'consumer':'muse','job_id':None,'status':'not_admitted','reason':str(error)}
    if row is not None:
        transaction.set(jr,row);transaction.set(pr,changed)
    return {'consumer':'muse','job_id':job_id,'status':(row or saved.to_dict())['status']}
