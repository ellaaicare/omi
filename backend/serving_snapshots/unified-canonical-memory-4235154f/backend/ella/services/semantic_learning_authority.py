"""Read-only reconstruction of a server-issued v3 learning grant, never v2 proof."""
from __future__ import annotations
import asyncio
import copy
import json
import re
from dataclasses import asdict
from ella.services.semantic_learning_v3 import (LearningBinding,GRANT_CONTRACT,digest,encode,bytes_digest,
    generator_request,verifier_request,validated_delta,hold,ID)
from ella.services.semantic_learning_ledger import POLICY_FIELD,OPERATIONS_FIELD,RECEIPTS_FIELD


def reconstruct_learning_grant(*,doc,uid,conversation_id,receipt_id,current_source,current_transcript_hash,
                               current_runtime_digest,current_context):
    if (not isinstance(doc,dict) or doc.get('id')!=conversation_id or doc.get('uid',uid)!=uid
            or not isinstance(receipt_id,str) or not ID.fullmatch(receipt_id)):
        hold('learning_owned_receipt_required')
    receipt=(doc.get(RECEIPTS_FIELD) or {}).get(receipt_id)
    if not isinstance(receipt,dict) or receipt.get('learning_receipt_id')!=receipt_id or receipt.get('status') not in {'issued','applied'}:
        hold('learning_issued_receipt_required')
    grant=receipt.get('grant')
    if not isinstance(grant,dict) or grant.get('contract')!=GRANT_CONTRACT or grant.get('target')!='MEMORY.md':
        hold('learning_grant_invalid')
    try:binding=LearningBinding(**grant['binding']).validate()
    except (KeyError,TypeError):hold('learning_binding_invalid')
    policy=doc.get(POLICY_FIELD)
    if (not isinstance(policy,dict) or type(policy.get('enabled')) is not bool or type(policy.get('admissions_open')) is not bool
            or (receipt['status']=='issued' and (policy['enabled'] is not True or policy['admissions_open'] is not True))
            or policy.get('uid')!=uid or policy.get('conversation_id')!=conversation_id
            or policy.get('revision')!=binding.learning_policy_revision
            or binding.uid!=uid or binding.conversation_id!=conversation_id
            or digest(current_source)!=binding.source_sha256 or current_transcript_hash!=binding.transcript_sha256
            or current_runtime_digest!=binding.runtime_authority_digest):hold('learning_current_authority_changed')
    ops=doc.get(OPERATIONS_FIELD) or {};gen=ops.get(grant.get('generator_operation_id'));ver=ops.get(grant.get('verifier_operation_id'))
    for item,role,operation_id in ((gen,'learning_generator',grant.get('generator_operation_id')),(ver,'learning_verifier',grant.get('verifier_operation_id'))):
        if (not isinstance(item,dict) or item.get('operation_id')!=operation_id or item.get('status')!='completed' or item.get('role')!=role
                or item.get('binding')!=asdict(binding) or item.get('request_sha256')!=digest(item.get('request'))
                or not item.get('runtime_interaction_id') or not item.get('canonical_assistant_event_id') or not item.get('provider_response_id')):
            hold('learning_completed_operation_required')
    if (gen['operation_id']==ver['operation_id'] or gen['runtime_interaction_id']==ver['runtime_interaction_id']
            or gen['canonical_assistant_event_id']==ver['canonical_assistant_event_id'] or gen['provider_response_id']==ver['provider_response_id']):hold('learning_distinct_operations_required')
    gp=gen['request'];context=gp.get('companion_context');source=gp.get('canonical_source');raw=gp.get('transcript_segments')
    if not isinstance(context,dict) or not isinstance(context.get('documents'),dict):hold('learning_context_invalid')
    prior=context['documents'].get('MEMORY.md')
    if not isinstance(prior,str):hold('learning_memory_invalid')
    inputs=dict(binding=binding,canonical_source=source,transcript_segments=raw,companion_context=context,memory_prior_bytes=prior.encode())
    indices=gp.get('owner_attributed_segment_indices')
    if not isinstance(indices,list):hold('learning_attribution_invalid')
    expected_gp=generator_request(**inputs,owner_attributed_segment_indices=tuple(indices))
    expected_vp=verifier_request(generator_request=expected_gp,candidate=gen['result'])
    if gp!=expected_gp or ver['request']!=expected_vp:hold('learning_operation_request_changed')
    checked=validated_delta(**inputs,candidate=gen['result'],verdict=ver['result'],owner_attributed_segment_indices=tuple(indices))
    if checked['status']!='proposed_learning':hold('learning_empty_has_no_grant')
    expected={'contract':GRANT_CONTRACT,'binding':asdict(binding),'target':'MEMORY.md',
              **{key:checked[key] for key in ('facts','append_delta_utf8','memory_after_sha256')},
              'generator_operation_id':gen['operation_id'],'verifier_operation_id':ver['operation_id']}
    if grant!=expected or receipt_id!=digest({'domain':'ella-learning-receipt-v3','grant':expected}):
        hold('learning_grant_reconstruction_failed')
    if receipt['status']=='applied':
        completed=receipt.get('host_completion_receipt')
        if (not isinstance(completed,dict) or completed.get('status')!='completed'
                or completed.get('grant_sha256')!=digest({'learning_receipt_id':receipt_id,**grant})
                or completed.get('memory_after_sha256')!=grant['memory_after_sha256']
                or not isinstance(completed.get('context_revision'),str) or not completed['context_revision']
                or not isinstance(completed.get('context_sha256'),str)
                or not re.fullmatch('[a-f0-9]{64}',completed['context_sha256'])):
            hold('learning_applied_completion_unconfirmed')
    # A postappend read/replay is permitted only for the exact deterministic
    # append with every other context document unchanged. No new write is armed.
    current_docs=current_context.get('documents') if isinstance(current_context,dict) else None
    if not isinstance(current_docs,dict) or current_context.get('owner_uid')!=uid:hold('learning_context_changed')
    before=context['documents'];after={**before,'MEMORY.md':prior+grant['append_delta_utf8']}
    if current_docs==before:
        if current_context['revision']!=binding.context_revision:hold('learning_context_changed')
    elif current_docs==after:
        if bytes_digest(current_docs['MEMORY.md'].encode())!=grant['memory_after_sha256']:hold('learning_context_changed')
    else:hold('learning_context_changed')
    return {'learning_receipt_id':receipt_id,**copy.deepcopy(grant)},(gen,ver),binding


async def read_learning_authority(*,uid,conversation_id,receipt_id,repository=None):
    from database.conversations import get_conversation
    from ella.services.ai_consent import assert_current_ai_consent
    from ella.services.semantic_runtime_authority import resolve_semantic_runtime as resolve_isolated_runtime,semantic_runtime_authority_identity as runtime_authority_identity
    from ella.services.canonical_summary_source import canonical_source_from_conversation
    from models.hermes_cloud_enrichment_contract import transcript_source
    from ella.services.semantic_host_context import context_reader
    from ella.routers.canonical_events import PostgresCanonicalEventStore
    from ella.services.semantic_host_context import HostReadBudget
    host_read_budget=HostReadBudget()
    async def sample():
        if await asyncio.to_thread(assert_current_ai_consent,uid)!=uid:hold('learning_consent_changed')
        runtime=await resolve_isolated_runtime(uid,repository=repository,target_mode='hermes-cloud-transcript',host_read_budget=host_read_budget)
        if runtime is None or runtime.uid!=uid or runtime.provider!='hermes' or runtime.status!='active':hold('learning_runtime_changed')
        identity=runtime_authority_identity(runtime)
        doc=await asyncio.to_thread(get_conversation,uid,conversation_id)
        if not isinstance(doc,dict):hold('learning_owned_source_required')
        # IMPORTANT: host re-entrant context requests would recursively call this
        # grant endpoint. Context is intentionally supplied by the host only at
        # its own apply guard, not fetched back from here. Backend reconstructs
        # the stored preimage; host proves actual prior/post context locally.
        receipt=(doc.get(RECEIPTS_FIELD) or {}).get(receipt_id) or {}
        stored=(doc.get(OPERATIONS_FIELD) or {}).get((receipt.get('grant') or {}).get('generator_operation_id')) or {}
        context=(stored.get('request') or {}).get('companion_context')
        source=canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=doc)
        grant,ops,binding=reconstruct_learning_grant(doc=doc,uid=uid,conversation_id=conversation_id,receipt_id=receipt_id,
            current_source=source,current_transcript_hash=transcript_source(doc)[1],current_runtime_digest=identity.digest,current_context=context)
        store=PostgresCanonicalEventStore()
        # Exact current canonical user existence is independent of model output.
        user_id=binding.canonical_user_event_id
        jobs=doc.get('semantic_retained_completion_jobs_v1') or {}
        candidates=[v.get('canonical_user') for v in jobs.values() if isinstance(v,dict)
                    and isinstance(v.get('canonical_user'),dict) and v['canonical_user'].get('event_id')==user_id]
        if len(candidates)!=1:hold('learning_canonical_user_required')
        from ella.services.retained_canonical_user import confirm_retained_canonical_user
        await confirm_retained_canonical_user(uid=uid,receipt=candidates[0],expected_transcript=binding.transcript_sha256,event_store=store)
        for operation in ops:
            event_id=operation['canonical_assistant_event_id']
            if not event_id.startswith('semantic-hermes:') or not event_id.endswith(':assistant'):hold('learning_canonical_operation_required')
            event=await store.get_event(uid=uid,event_id=event_id,source_identity=event_id.removesuffix(':assistant'))
            if (not isinstance(event,dict) or event.get('uid')!=uid or event.get('canonical_identity')!=uid
                    or event.get('role')!='assistant' or event.get('channel')!='semantic_'+operation['role']
                    or (event.get('metadata') or {}).get('request_sha256')!=operation['request_sha256']
                    or (event.get('source_ref') or {}).get('gateway_response_id')!=operation['runtime_interaction_id']
                    or (event.get('source_ref') or {}).get('provider_response_id')!=operation['provider_response_id']):
                hold('learning_canonical_operation_required')
            try:result=json.loads(event.get('text') or '')
            except (ValueError,TypeError):hold('learning_canonical_operation_required')
            if result!=operation['result']:hold('learning_canonical_result_changed')
        return grant
    async with asyncio.timeout(20):
        first=await sample()
        if len(encode(first))>32768:hold('learning_grant_capacity')
        if await sample()!=first:hold('learning_authority_changed')
        if await asyncio.to_thread(assert_current_ai_consent,uid)!=uid:hold('learning_consent_changed')
        return first
