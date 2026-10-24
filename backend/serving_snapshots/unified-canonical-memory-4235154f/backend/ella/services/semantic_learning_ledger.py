"""Durable server-owned learning operations, separate from every v2 summary ledger."""
from __future__ import annotations
import copy
import threading
from dataclasses import asdict
from ella.services.semantic_learning_v3 import LearningHold, digest, encode, hold, ID

POLICY_FIELD='semantic_learning_policy_v3'
OPERATIONS_FIELD='semantic_learning_operations_v3'
RECEIPTS_FIELD='semantic_learning_receipts_v3'
MAX_OPERATIONS=8
MAX_RECEIPTS=4
MAX_RECORD_BYTES=32768
MAX_REQUEST_BYTES=196608
MAX_LEDGER_BYTES=524288


class DurableLearningAuthority:
    """All callbacks originate from the internal backend factory, never request JSON.

    current_binding is a fresh owner/source/consent/runtime/canonical/context
    guard. update uses the existing exact-owner conversation transaction builder.
    execute_runtime is the existing verified actual runtime bridge. A START is
    durable before model I/O, and any unknown/cancelled START can never re-arm.
    """
    def __init__(self,*,binding,read,update,current_binding,execute_runtime,owner_attribution):
        self.binding=binding;self.read=read;self.update=update;self.current=current_binding
        self.runtime=execute_runtime;self.attribution=owner_attribution;self.closed=threading.Event();self._proofs={}

    def _owned_policy(self,doc):
        if self.closed.is_set():hold('learning_cancelled_hold')
        if (not isinstance(doc,dict) or doc.get('id')!=self.binding.conversation_id or doc.get('uid',self.binding.uid)!=self.binding.uid):
            hold('learning_owned_source_required')
        policy=doc.get(POLICY_FIELD)
        if (not isinstance(policy,dict) or policy.get('enabled') is not True or policy.get('admissions_open') is not True
                or policy.get('uid')!=self.binding.uid or policy.get('conversation_id')!=self.binding.conversation_id
                or policy.get('revision')!=self.binding.learning_policy_revision):hold('learning_policy_closed')
    def _fresh(self,doc):
        self._owned_policy(doc)
        if self.current(doc)!=self.binding:hold('learning_authority_changed')
        if self.closed.is_set():hold('learning_cancelled_hold')

    def confirm_current_binding(self,binding):
        if binding!=self.binding:hold('learning_authority_changed')
        self._fresh(self.read(binding.uid,binding.conversation_id))

    def owner_attributed_segment_indices(self,binding):
        if binding!=self.binding:hold('learning_authority_changed')
        self._owned_policy(self.read(binding.uid,binding.conversation_id))
        return self.attribution(binding)

    def execute_once(self,*,role,request):
        if role not in {'learning_generator','learning_verifier'} or request.get('role')!=role:
            hold('learning_operation_role_invalid')
        if len(encode(request))>MAX_REQUEST_BYTES:hold('learning_request_capacity')
        request_hash=digest(request)
        key=digest({'binding':asdict(self.binding),'role':role,'request_sha256':request_hash})
        def reserve(doc):
            self._fresh(doc)
            ops=copy.deepcopy(doc.get(OPERATIONS_FIELD,{}))
            if not isinstance(ops,dict) or len(ops)>MAX_OPERATIONS:hold('learning_operation_ledger_invalid')
            old=ops.get(key)
            if old is not None:
                if (not isinstance(old,dict) or old.get('binding')!=asdict(self.binding)
                        or old.get('role')!=role or old.get('request_sha256')!=request_hash
                        or old.get('status')!='completed'):hold('learning_unknown_operation_hold')
                return {},{'replay':True,'operation':copy.deepcopy(old)}
            if len(ops)>=MAX_OPERATIONS:hold('learning_operation_capacity')
            ops[key]={'operation_id':key,'binding':asdict(self.binding),'role':role,
                      'request_sha256':request_hash,'request':copy.deepcopy(request),'status':'started'}
            if len(encode(ops))>MAX_LEDGER_BYTES:hold('learning_ledger_capacity')
            return {OPERATIONS_FIELD:ops},{'replay':False}
        reserved=self.update(self.binding.uid,self.binding.conversation_id,reserve)
        if reserved is None:hold('learning_operation_start_unknown')
        if reserved['result']['replay']:return reserved['result']['operation']
        # The bridge returns actual backend-owned canonical readback/provenance,
        # never just a semantic result or caller-supplied UUID.
        turn=self.runtime(role=role,request=copy.deepcopy(request))
        result=turn.result
        if (not isinstance(result,dict) or len(encode(result))>MAX_RECORD_BYTES
                or not isinstance(turn.runtime_interaction_id,str) or not turn.runtime_interaction_id
                or not isinstance(turn.canonical_assistant_event_id,str) or not turn.canonical_assistant_event_id
                or not isinstance(getattr(turn,'provider_response_id',None),str) or not turn.provider_response_id
                or turn.provider_response_id==turn.runtime_interaction_id):
            hold('learning_actual_operation_proof_required')
        def finish(doc):
            self._fresh(doc);ops=copy.deepcopy(doc.get(OPERATIONS_FIELD,{}));old=ops.get(key)
            if (not isinstance(old,dict) or old.get('status')!='started'
                    or old.get('request_sha256')!=request_hash or old.get('binding')!=asdict(self.binding)):
                hold('learning_operation_finish_conflict')
            ops[key]={**old,'status':'completed','result':copy.deepcopy(result),
                'runtime_interaction_id':turn.runtime_interaction_id,
                'canonical_assistant_event_id':turn.canonical_assistant_event_id,'provider_response_id':turn.provider_response_id}
            if len(encode(ops))>MAX_LEDGER_BYTES:hold('learning_ledger_capacity')
            return {OPERATIONS_FIELD:ops},{'operation':copy.deepcopy(ops[key])}
        completed=self.update(self.binding.uid,self.binding.conversation_id,finish)
        if completed is None:hold('learning_operation_finish_unknown')
        return completed['result']['operation']

    def load_completed_operation(self,*,binding,operation_id,role):
        if binding!=self.binding:hold('learning_authority_changed')
        doc=self.read(binding.uid,binding.conversation_id)
        self._owned_policy(doc)
        record=(doc.get(OPERATIONS_FIELD) or {}).get(operation_id)
        if (not isinstance(record,dict) or record.get('status')!='completed' or record.get('role')!=role
                or record.get('binding')!=asdict(binding) or not record.get('runtime_interaction_id')
                or not record.get('canonical_assistant_event_id') or not record.get('provider_response_id')):hold('learning_actual_operation_proof_required')
        self._proofs[operation_id]=copy.deepcopy(record)
        return copy.deepcopy(record)

    def issue_once(self,*,binding,grant):
        receipt_id=digest({'domain':'ella-learning-receipt-v3','grant':grant})
        def issue(doc):
            self._fresh(doc)
            ops=doc.get(OPERATIONS_FIELD,{})
            gen=ops.get(grant['generator_operation_id']);ver=ops.get(grant['verifier_operation_id'])
            if (not isinstance(gen,dict) or not isinstance(ver,dict)
                    or gen!=self._proofs.get(grant['generator_operation_id'])
                    or ver!=self._proofs.get(grant['verifier_operation_id'])
                    or gen.get('runtime_interaction_id')==ver.get('runtime_interaction_id')
                    or gen.get('canonical_assistant_event_id')==ver.get('canonical_assistant_event_id')
                    or gen.get('provider_response_id')==ver.get('provider_response_id')):
                hold('learning_distinct_actual_operations_required')
            receipts=copy.deepcopy(doc.get(RECEIPTS_FIELD,{}))
            if not isinstance(receipts,dict) or len(receipts)>MAX_RECEIPTS:hold('learning_receipt_ledger_invalid')
            old=receipts.get(receipt_id)
            if old is not None:
                if old.get('grant')!=grant:hold('learning_receipt_conflict')
                return {},{'receipt_id':receipt_id}
            if len(receipts)>=MAX_RECEIPTS:hold('learning_receipt_capacity')
            receipts[receipt_id]={'status':'issued','grant':copy.deepcopy(grant),'learning_receipt_id':receipt_id}
            return {RECEIPTS_FIELD:receipts},{'receipt_id':receipt_id}
        value=self.update(binding.uid,binding.conversation_id,issue)
        if value is None:hold('learning_receipt_issue_unknown')
        return value['result']['receipt_id']

    def cancel(self):self.closed.set()
