"""Internal policy-first voice memory adapter; no independent public authority.

Caller authenticates principal, resolves current runtime and current session scope,
then supplies the existing server SEARCH_POLICY projection, never request verdicts.
"""
from ella.services.canonical_memory import read_memory, search_memory, CanonicalMemoryUnavailable

async def prepare_search(uid, *, target_mode, allowed, role_policy, requested_sources, scope, query, limit, reader=read_memory):
    admitted=set()
    if allowed.get('timeline'):admitted.add('timeline')
    if allowed.get('omi_full') or allowed.get('omi_meta'):admitted.add('omi')
    requested=set(requested_sources or ())
    denied=sorted(requested-admitted)
    only_canonical=requested_sources is not None and requested.issubset({'omi','timeline'})
    response=None;value=None
    if not admitted:
        if scope or only_canonical:
            response={'results':[],'sources_searched':[],'sources_denied':sorted(requested),
                'provenance':{},'total_results':0,'query':query,'canonical_memory_status':'denied'}
        return value,response
    if scope and scope.get('kind')=='daily_card':
        cid=scope.get('conversation_id');version=scope.get('active_summary_version_id')
        if not cid or not version:
            if requested.intersection({'omi','timeline'}):raise CanonicalMemoryUnavailable()
            # This value came from fresh exact card/version/evidence/source checks
            # in _resolve_principal_session_scope, not cached overview input.
            response={'results':[{'source':'session_scope','title':scope.get('title',''),
                'content':scope.get('overview',''),'timestamp':scope.get('occurred_at',''),
                'score':1000,'metadata':{'card_id':scope.get('card_id'),
                'card_version':scope.get('card_version'),'card_evidence_hash':scope.get('card_evidence_hash'),
                'provenance':'current_authorized_daily_card'}}],
                'sources_searched':['session_scope'],'sources_denied':sorted(requested),
                'provenance':{'current_authorized_daily_card':1},'total_results':1,'query':query,
                'canonical_memory_status':'scope_available','scope_bound':True}
            return None,response
    cid=scope.get('conversation_id') if scope else None
    version=scope.get('active_summary_version_id') if scope else None
    if scope and (not cid or not version):raise CanonicalMemoryUnavailable()
    value=await reader(uid,target_mode=target_mode,limit=300,channels=['omi','omi_transcript'],
        conversation_id=cid,expected_summary_version=version)
    if scope or only_canonical:
        results=search_memory(value,query,limit,full_access=bool(role_policy.get('omi_full')))
        response={'results':results,'sources_searched':sorted(requested&admitted) if requested else ['session_scope'],
            'sources_denied':denied,'provenance':{'canonical_memory':len(results)},
            'total_results':len(results),'query':query,'canonical_memory_status':'available',
            'truncated':False,'possibly_more':value['possibly_more'],'scope_bound':bool(scope)}
    return value,response
