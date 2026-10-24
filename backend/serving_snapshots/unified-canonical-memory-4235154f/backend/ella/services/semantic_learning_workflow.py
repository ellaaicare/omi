"""Explicit v3 retained learning stage; separate from summary generation/publication.

Current trusted canonical source has no positive speaker-to-owner attestation.
Therefore the production attribution adapter returns no eligible indices, even
when a raw segment claims is_user=true. This truthfully evaluates no learning;
no MEMORY mutation or model call is authorized from unverified attribution.
"""
from __future__ import annotations
import asyncio
import copy
import threading
from dataclasses import asdict
from ella.services import semantic_publication as publication
from ella.services.canonical_summary_source import canonical_source_from_conversation, canonical_source_sha256
from ella.services.semantic_learning_v3 import (LearningBinding, generator_request, verifier_request,
    issue_verified_learning, bytes_digest, hold)
from ella.services.semantic_learning_ledger import DurableLearningAuthority, POLICY_FIELD
from models.hermes_cloud_enrichment_contract import transcript_source


TRUSTED_OWNER_ATTRIBUTION_SUPPORTED = False


def trusted_owner_segment_indices(_binding):
    # Neither Firebase identity nor authenticated recording proves who spoke.
    # No reviewed server-owned positive attribution receipt exists in this source.
    return ()


def evaluate_learning(*,binding,canonical_source,transcript_segments,companion_context,
                      memory_prior_bytes,authority):
    authority.confirm_current_binding(binding)
    gp=generator_request(binding=binding,canonical_source=canonical_source,transcript_segments=transcript_segments,
        companion_context=companion_context,memory_prior_bytes=memory_prior_bytes,
        owner_attributed_segment_indices=authority.owner_attributed_segment_indices(binding))
    if not gp['owner_attributed_segment_indices']:
        authority.confirm_current_binding(binding)
        return {'status':'no_learning','reason':'positive_owner_attribution_unavailable',
                'memory_after_sha256':binding.memory_prior_sha256,'append_delta_utf8':''}
    gen=authority.execute_once(role='learning_generator',request=gp)
    vp=verifier_request(generator_request=gp,candidate=gen['result'])
    ver=authority.execute_once(role='learning_verifier',request=vp)
    return issue_verified_learning(binding=binding,canonical_source=canonical_source,
        transcript_segments=transcript_segments,companion_context=companion_context,memory_prior_bytes=memory_prior_bytes,
        generator_operation_id=gen['operation_id'],verifier_operation_id=ver['operation_id'],authority=authority)


async def run_retained_learning(**kwargs):
    # A cancelled to_thread operation cannot be forcibly stopped. This shared
    # close fence prevents subsequent new transactional mutations; an already
    # committed START/remote operation remains unknown, never reported rolled back.
    closed=threading.Event()
    try:
        async with asyncio.timeout(300):
            return await _run_retained_learning(**kwargs,_closed=closed)
    except BaseException:
        closed.set()
        raise


async def _run_retained_learning(*,uid,conversation_id,repository,read_conversation,update_conversation,
                               canonical_user_receipt,host_read_budget=None,selected=None,_closed):
    """Actual server factories; no caller verdict, credentials or operation records.

    Only the trusted selected-owner completion factory may install private v3
    policy after current authority checks. It cannot invent attribution. Empty
    eligibility does not block v2 summary.
    Positive eligible execution remains guarded by the verified runtime bridge.
    """
    from ella.services.ai_consent import assert_current_ai_consent
    from ella.services.semantic_runtime_authority import resolve_semantic_runtime as resolve_isolated_runtime,semantic_runtime_authority_identity as runtime_authority_identity,revalidate_semantic_runtime as revalidate_runtime_authority
    from ella.services.semantic_host_context import context_reader,HostReadBudget
    host_read_budget=host_read_budget or HostReadBudget()
    from ella.services.retained_canonical_user import confirm_retained_canonical_user
    from ella.routers.canonical_events import PostgresCanonicalEventStore
    from ella.services.semantic_self_hosted_runtime import create_self_hosted_runtime_bridge
    doc=await asyncio.to_thread(read_conversation,uid,conversation_id)
    if (not isinstance(doc,dict) or doc.get('id')!=conversation_id
            or doc.get('uid',uid)!=uid):
        hold('learning_policy_source_changed')
    if selected is None or selected(uid) is not True:hold('learning_owner_not_selected')
    policy=doc.get(POLICY_FIELD) if isinstance(doc,dict) else None
    if isinstance(doc,dict) and POLICY_FIELD in doc and policy is None:hold('learning_policy_closed')
    if policy is not None and (not isinstance(policy,dict) or policy.get('enabled') is not True or policy.get('admissions_open') is not True
            or policy.get('uid')!=uid or policy.get('conversation_id')!=conversation_id
            or not isinstance(policy.get('revision'),str) or not policy['revision']):hold('learning_policy_closed')
    if await asyncio.to_thread(assert_current_ai_consent,uid)!=uid:hold('learning_consent_changed')
    runtime=await resolve_isolated_runtime(uid,repository=repository,target_mode='hermes-cloud-transcript',host_read_budget=host_read_budget)
    if runtime is None or runtime.uid!=uid or runtime.provider!='hermes' or runtime.status!='active':hold('learning_runtime_required')
    identity=runtime_authority_identity(runtime)
    source=canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=doc)
    raw=copy.deepcopy(doc.get('transcript_segments') or [])
    transcript_hash=transcript_source(doc)[1]
    await confirm_retained_canonical_user(uid=uid,receipt=canonical_user_receipt,
        expected_transcript=transcript_hash,event_store=PostgresCanonicalEventStore())
    # This server has no trusted speaker-to-owner attestation adapter. A raw
    # segment's is_user flag cannot enable learning. Do not build a private
    # learning policy or repeatedly hydrate MEMORY merely to return no_learning.
    # Current owner/source, consent, runtime and canonical-event checks above
    # still apply; the caller independently hydrates and validates summary
    # context before any generation, publication or writeback.
    if not TRUSTED_OWNER_ATTRIBUTION_SUPPORTED and policy is None:
        # Preserve the policy-install preconditions even though no policy or
        # operation is needed for this truthful no-learning result.
        jobs=doc.get('semantic_retained_completion_jobs_v1') or {}
        if (not isinstance(jobs,dict) or len(jobs)!=1
                or not any(isinstance(job,dict) and job.get('status')=='started'
                    and job.get('canonical_user')==canonical_user_receipt
                    for job in jobs.values())):
            hold('learning_policy_owned_start_required')
        from ella.services.semantic_learning_ledger import OPERATIONS_FIELD, RECEIPTS_FIELD
        if OPERATIONS_FIELD in doc or RECEIPTS_FIELD in doc:
            hold('learning_policy_existing_ledger_hold')
        if _closed.is_set():hold('learning_cancelled_hold')
        return {'status':'no_learning','reason':'positive_owner_attribution_unavailable'}
    reader=context_reader(conversation_id=conversation_id,read_conversation=read_conversation,read_budget=host_read_budget)
    context=await asyncio.to_thread(reader,runtime,uid=uid,runtime_identity=identity)
    prior=context['documents']['MEMORY.md'].encode('utf-8')
    if policy is None:
        # Internal installation after exact selected owner, consent, runtime,
        # confirmed canonical source and a fresh host context. Never a client
        # field nor a bypass of an existing closed/malformed policy.
        expected_source=canonical_source_sha256(source)
        policy={'enabled':True,'admissions_open':True,'uid':uid,'conversation_id':conversation_id,
            'revision':publication.digest({'domain':'ella-learning-policy-v3','uid':uid,
                'conversation_id':conversation_id,'source':expected_source,'transcript':transcript_hash,
                'runtime':identity.digest,'canonical_user_event_id':canonical_user_receipt['event_id'],
                'context_revision':context['revision'],'memory_prior_sha256':bytes_digest(prior)})}
        if selected(uid) is not True or await asyncio.to_thread(assert_current_ai_consent,uid)!=uid:
            hold('learning_policy_authority_changed')
        fresh=await revalidate_runtime_authority(identity,repository=repository,host_read_budget=host_read_budget)
        if runtime_authority_identity(fresh)!=identity:hold('learning_policy_runtime_changed')
        def install(current_doc):
            if _closed.is_set():hold('learning_cancelled_hold')
            if (current_doc.get('id')!=conversation_id or current_doc.get('uid',uid)!=uid
                    or canonical_source_sha256(canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=current_doc))!=expected_source
                    or transcript_source(current_doc)[1]!=transcript_hash):hold('learning_policy_source_changed')
            jobs=current_doc.get('semantic_retained_completion_jobs_v1') or {}
            if (not isinstance(jobs,dict) or len(jobs)!=1
                    or not any(isinstance(job,dict) and job.get('status')=='started'
                        and job.get('canonical_user')==canonical_user_receipt for job in jobs.values())):
                hold('learning_policy_owned_start_required')
            if POLICY_FIELD in current_doc:
                if current_doc[POLICY_FIELD]!=policy:hold('learning_policy_existing_hold')
                return {},{'installed':True}
            from ella.services.semantic_learning_ledger import OPERATIONS_FIELD,RECEIPTS_FIELD
            if current_doc.get(OPERATIONS_FIELD) or current_doc.get(RECEIPTS_FIELD):hold('learning_policy_existing_ledger_hold')
            if selected(uid) is not True or reader(runtime,uid=uid,runtime_identity=identity)!=context:
                hold('learning_policy_context_changed')
            if _closed.is_set():hold('learning_cancelled_hold')
            return {POLICY_FIELD:copy.deepcopy(policy)},{'installed':True}
        installed=await asyncio.to_thread(update_conversation,uid,conversation_id,install)
        if installed is None or installed['result'].get('installed') is not True:hold('learning_policy_install_unknown')
        doc=await asyncio.to_thread(read_conversation,uid,conversation_id)
    binding=LearningBinding(uid,conversation_id,identity.digest,canonical_source_sha256(source),transcript_hash,
        canonical_user_receipt['event_id'],context['revision'],bytes_digest(prior),policy['revision'])
    loop=asyncio.get_running_loop();owned_futures=[]
    async def check_runtime():
        if _closed.is_set():hold('learning_cancelled_hold')
        if selected(uid) is not True or await asyncio.to_thread(assert_current_ai_consent,uid)!=uid:hold('learning_consent_changed')
        fresh=await revalidate_runtime_authority(identity,repository=repository,host_read_budget=host_read_budget)
        if runtime_authority_identity(fresh)!=identity:hold('learning_runtime_changed')
        await confirm_retained_canonical_user(uid=uid,receipt=canonical_user_receipt,
            expected_transcript=transcript_hash,event_store=PostgresCanonicalEventStore())
    def current(current_doc):
        if _closed.is_set():hold('learning_cancelled_hold')
        future=asyncio.run_coroutine_threadsafe(check_runtime(),loop);owned_futures.append(future)
        try:future.result(timeout=15)
        except BaseException:
            future.cancel();hold('learning_current_authority_unavailable')
        return source_context_binding(current_doc)
    def source_context_binding(current_doc):
        if _closed.is_set():hold('learning_cancelled_hold')
        snapshot=reader(runtime,uid=uid,runtime_identity=identity)
        if (snapshot!=context or current_doc.get(POLICY_FIELD)!=policy
                or current_doc.get('id')!=conversation_id or current_doc.get('uid',uid)!=uid
                or canonical_source_sha256(canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=current_doc))!=binding.source_sha256
                or transcript_source(current_doc)[1]!=binding.transcript_sha256):hold('learning_current_binding_changed')
        if _closed.is_set():hold('learning_cancelled_hold')
        return binding
    def publication_current():
        current_doc=read_conversation(uid,conversation_id);source_context_binding(current_doc)
        source_now=canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=current_doc)
        # Shared immutable runtime-scope type, NOT a v2 publication receipt or
        # admission. The learning policy is independently gated above.
        return publication.PublicationBinding(uid,conversation_id,binding.source_sha256,
            str(source_now.get('finished_at') or source_now.get('started_at') or ''),
            str(current_doc.get('active_summary_version_id') or ''),binding.learning_policy_revision,
            publication.digest(context),binding.context_revision,binding.transcript_sha256)
    pub_binding=await asyncio.to_thread(publication_current)
    bridge=create_self_hosted_runtime_bridge(binding=pub_binding,current_binding=publication_current,
        repository=repository,expected_runtime_identity=identity,timeout_seconds=90,host_read_budget=host_read_budget)
    async def actual_learning_turn(role,request):
        turn=await bridge(role=role,request=copy.deepcopy(request))
        event_id=turn.canonical_assistant_event_id
        event=await PostgresCanonicalEventStore().get_event(uid=uid,event_id=event_id,
            source_identity=event_id.removesuffix(':assistant'))
        provider_id=(event.get('source_ref') or {}).get('provider_response_id') if isinstance(event,dict) else None
        if (not isinstance(provider_id,str) or not provider_id or provider_id==turn.runtime_interaction_id
                or (event.get('source_ref') or {}).get('gateway_response_id')!=turn.runtime_interaction_id):
            hold('learning_provider_provenance_unconfirmed')
        from types import SimpleNamespace
        return SimpleNamespace(result=turn.result,runtime_interaction_id=turn.runtime_interaction_id,
            canonical_assistant_event_id=event_id,provider_response_id=provider_id)
    def execute_runtime(*,role,request):
        future=asyncio.run_coroutine_threadsafe(actual_learning_turn(role,request),loop)
        owned_futures.append(future)
        try:return future.result(timeout=95)
        except BaseException:
            future.cancel();hold('learning_actual_operation_unknown')
    authority=DurableLearningAuthority(binding=binding,read=read_conversation,update=update_conversation,
        current_binding=current,execute_runtime=execute_runtime,owner_attribution=trusted_owner_segment_indices)
    try:
        async with asyncio.timeout(240):
            result=await asyncio.to_thread(evaluate_learning,binding=binding,canonical_source=source,
                transcript_segments=raw,companion_context=context,memory_prior_bytes=prior,authority=authority)
        if 'learning_receipt_id' not in result:
            return result
        from ella.services.semantic_learning_host_client import apply_learning_on_host
        # Actual append uses only the opaque server receipt ID; not caller delta.
        await check_runtime()
        before_append_doc=await asyncio.to_thread(read_conversation,uid,conversation_id)
        if await asyncio.to_thread(current,before_append_doc)!=binding:
            hold('learning_preappend_changed')
        completion=await apply_learning_on_host(runtime=runtime,grant=result,host_read_budget=host_read_budget)
        await check_runtime()
        actual_context=await asyncio.to_thread(reader,runtime,uid=uid,runtime_identity=identity)
        if actual_context!=completion['context_snapshot']:
            hold('learning_postappend_context_changed')
        def finish_learning(current_doc):
            if _closed.is_set():hold('learning_cancelled_hold')
            if (current_doc.get('id')!=conversation_id or current_doc.get('uid',uid)!=uid
                    or current_doc.get(POLICY_FIELD)!=policy
                    or canonical_source_sha256(canonical_source_from_conversation(uid=uid,conversation_id=conversation_id,conversation=current_doc))!=binding.source_sha256
                    or transcript_source(current_doc)[1]!=binding.transcript_sha256):
                hold('learning_postappend_authority_changed')
            from ella.services.semantic_learning_ledger import RECEIPTS_FIELD
            receipts=copy.deepcopy(current_doc.get(RECEIPTS_FIELD) or {})
            record=receipts.get(result['learning_receipt_id'])
            expected_grant={k:v for k,v in result.items() if k!='learning_receipt_id'}
            if not isinstance(record,dict) or record.get('status')!='issued' or record.get('grant')!=expected_grant:
                hold('learning_postappend_receipt_changed')
            receipts[result['learning_receipt_id']]={**record,'status':'applied',
                'host_completion_receipt':copy.deepcopy(completion['receipt'])}
            if _closed.is_set():hold('learning_cancelled_hold')
            return {RECEIPTS_FIELD:receipts},{'applied':True}
        stored=await asyncio.to_thread(update_conversation,uid,conversation_id,finish_learning)
        if stored is None or stored['result'].get('applied') is not True:
            hold('learning_postappend_save_unknown')
        return {'status':'learning_applied','learning_receipt_id':result['learning_receipt_id'],
                'memory_after_sha256':result['memory_after_sha256'],
                'context_revision':actual_context['revision']}

    except BaseException:
        authority.cancel()
        for future in owned_futures:future.cancel()
        # No claim that cancellation reversed remote model/canonical operations.
        # Durable START remains held and never automatically re-arms.
        raise
