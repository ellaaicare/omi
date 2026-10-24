"""Closed causal metadata only: no exception messages, arguments, source or URLs."""
import json
import logging
import re
_TYPES=frozenset({'ValueError','TypeError','KeyError','AttributeError','NameError','RuntimeError','TimeoutError','OSError','PermissionError','FileNotFoundError','UnicodeError','UnicodeDecodeError','JSONDecodeError','HTTPStatusError','HTTPError','ReadTimeout','ConnectTimeout','ConnectError','RemoteProtocolError','ProvisioningError','SemanticPublicationRejectedError','ContractError','CompanionContextUnavailable','CanonicalUserUnconfirmed','LearningHold'})
_FILES=frozenset({'hermes_cloud_enrichment_outbox.py','retained_completion_enrichment.py','semantic_self_hosted_runtime.py','semantic_producer_ledger.py','semantic_retained_integration.py','semantic_host_context.py','runtime_resolver.py','semantic_runtime_authority.py','semantic_publication.py','semantic_contract_v1.py','qualification_stock_supervisor.py','qualification_stock_worker.py'})
_STAGES=frozenset({'normal_enrichment_dispatch','stock_worker_execute','stock_supervisor'})
_CODES=frozenset({'account_consent_required','account_runtime_not_ready','account_model_unavailable','account_capability_incomplete','account_owner_mismatch','account_runtime_changed','account_provider_unsupported','retained_completion_rejected','semantic_runtime_gateway_provenance_missing','semantic_runtime_output_shape','semantic_runtime_target_changed','semantic_runtime_policy_invalid','semantic_runtime_request_invalid','semantic_runtime_request_capacity','retained_completion_authority_unavailable','retained_completion_context_changed','retained_completion_source_changed'})

def _safe_exception(error,stage):
    # Deliberately never call str/repr(error), serialize its dict, or read body/URL/headers.
    if stage not in _STAGES:stage='stock_supervisor'
    result={'contract':'ella-causal-diagnostic-v1','stage':stage,'exception_type':type(error).__name__ if type(error).__name__ in _TYPES else 'OtherException'}
    causes=[];seen=set();cursor=error
    for _ in range(4):
        if cursor is None or id(cursor) in seen:break
        seen.add(id(cursor));causes.append(type(cursor).__name__ if type(cursor).__name__ in _TYPES else 'OtherException')
        cursor=cursor.__cause__ or cursor.__context__
    result['cause_types']=causes
    fields=getattr(error,'__dict__',{})
    if type(fields) is dict:
        code=fields.get('code')
        if type(code) is str and code in _CODES:result['closed_code']=code
        response=fields.get('response')
        status=getattr(response,'status_code',None) if type(error).__name__ in {'HTTPStatusError','HTTPError'} else fields.get('status_code')
        if type(status) is int and 100<=status<=599:result['http_status']=status
    if isinstance(error,OSError) and type(error.errno) is int:result['errno']=error.errno
    frames=[];tb=error.__traceback__
    while tb is not None:
        code=tb.tb_frame.f_code;name=code.co_filename.rsplit('/',1)[-1]
        if name in _FILES and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',code.co_name):
            frames.append({'file':name,'function':code.co_name,'line':tb.tb_lineno})
        tb=tb.tb_next
    result['frames']=frames[-8:]
    return result

def safe_exception(error,stage):
    try:
        return _safe_exception(error,stage)
    except Exception:
        # Diagnostics must never replace the original business outcome, including
        # hostile/failed metadata access. No exception content is serialized.
        return {'contract':'ella-causal-diagnostic-v1',
                'stage':stage if type(stage) is str and stage in _STAGES else 'stock_supervisor',
                'exception_type':'OtherException','cause_types':[], 'frames':[],
                'diagnostic_status':'metadata_unavailable'}

def emit(error,stage):
    value=safe_exception(error,stage)
    try:logging.getLogger('ella.causal').warning('ella_causal_diagnostic %s',json.dumps(value,sort_keys=True))
    except Exception:pass
    return value
