"""Closed causal metadata only: no exception messages, arguments, source or URLs."""

import json
import logging
import re

_TYPES = frozenset(
    {
        'ValueError',
        'TypeError',
        'KeyError',
        'AttributeError',
        'NameError',
        'RuntimeError',
        'TimeoutError',
        'OSError',
        'PermissionError',
        'FileNotFoundError',
        'UnicodeError',
        'UnicodeDecodeError',
        'JSONDecodeError',
        'HTTPStatusError',
        'HTTPError',
        'ReadTimeout',
        'ConnectTimeout',
        'ConnectError',
        'RemoteProtocolError',
        'ProvisioningError',
        'SemanticPublicationRejectedError',
        'ContractError',
        'CompanionContextUnavailable',
        'CanonicalUserUnconfirmed',
        'LearningHold',
    }
)
_FILES = frozenset(
    {
        'hermes_cloud_enrichment_outbox.py',
        'retained_completion_enrichment.py',
        'semantic_self_hosted_runtime.py',
        'semantic_producer_ledger.py',
        'semantic_retained_integration.py',
        'semantic_host_context.py',
        'runtime_resolver.py',
        'semantic_runtime_authority.py',
        'semantic_publication.py',
        'semantic_contract_v1.py',
        'qualification_stock_supervisor.py',
        'qualification_stock_worker.py',
    }
)
_STAGES = frozenset({'normal_enrichment_dispatch', 'stock_worker_execute', 'stock_supervisor'})
_CODES = frozenset(
    {
        'account_consent_required',
        'account_runtime_not_ready',
        'account_model_unavailable',
        'account_capability_incomplete',
        'account_owner_mismatch',
        'account_runtime_changed',
        'account_provider_unsupported',
        'retained_completion_rejected',
        'semantic_runtime_gateway_provenance_missing',
        'semantic_runtime_output_shape',
        'semantic_runtime_target_changed',
        'semantic_runtime_policy_invalid',
        'semantic_runtime_request_invalid',
        'semantic_runtime_request_capacity',
        'retained_completion_authority_unavailable',
        'retained_completion_context_changed',
        'retained_completion_source_changed',
    }
)

_INNER_TYPES = frozenset(
    {
        'SemanticPublicationRejectedError',
        'ContractError',
        'CompanionContextUnavailable',
        'CanonicalUserUnconfirmed',
        'LearningHold',
    }
)
_INNER_CODES = _CODES | frozenset(
    {
        'retained_completion_admission_source_changed',
        'retained_completion_authority_changed',
        'retained_completion_runtime_changed',
        'retained_completion_admission_cancelled',
        'retained_completion_existing_policy_hold',
        'retained_completion_prior_operation_hold',
        'retained_completion_existing_job_hold',
        'retained_completion_reservation_unknown',
        'retained_completion_canonical_user_unconfirmed',
        'retained_completion_canonical_receipt_unknown',
        'retained_completion_learning_result_unknown',
        'retained_completion_learning_outcome_required',
        'host_backend_authority_unavailable',
        'host_backend_authority_rejected',
        'semantic_configured_model_authority_unavailable',
        'semantic_configured_model_authority_rejected',
        'semantic_authority_changed',
        'semantic_context_changed',
        'semantic_cancelled_hold',
    }
)


def _frames(error):
    result = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        name = code.co_filename.rsplit('/', 1)[-1]
        if name in _FILES and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', code.co_name):
            result.append({'file': name, 'function': code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    return result[-8:]


def sanitize_diagnostic(value):
    """Allow only the bounded closed projection on an attempt-bound job write."""
    try:
        if type(value) is not dict or set(value) - {
            'contract',
            'stage',
            'exception_type',
            'cause_types',
            'closed_code',
            'http_status',
            'errno',
            'frames',
            'cause_metadata',
            'diagnostic_status',
        }:
            return None
        if value.get('contract') != 'ella-causal-diagnostic-v1' or value.get('stage') not in _STAGES:
            return None

        def label(v):
            return type(v) is str and v in _TYPES | {'OtherException'}

        def frames(items):
            return (
                type(items) is list
                and len(items) <= 8
                and all(
                    type(r) is dict
                    and set(r) == {'file', 'function', 'line'}
                    and r['file'] in _FILES
                    and type(r['function']) is str
                    and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', r['function'])
                    and type(r['line']) is int
                    and 0 < r['line'] <= 1000000
                    for r in items
                )
            )

        if not label(value.get('exception_type')) or not frames(value.get('frames')):
            return None
        causes = value.get('cause_types')
        if type(causes) is not list or len(causes) > 4 or not all(label(x) for x in causes):
            return None
        if 'closed_code' in value and value['closed_code'] not in _CODES:
            return None
        if 'http_status' in value and not (type(value['http_status']) is int and 100 <= value['http_status'] <= 599):
            return None
        if 'errno' in value and not (type(value['errno']) is int and 0 <= value['errno'] <= 4096):
            return None
        if 'diagnostic_status' in value and value['diagnostic_status'] != 'metadata_unavailable':
            return None
        metadata = value.get('cause_metadata', [])
        if type(metadata) is not list or len(metadata) > 4:
            return None
        for item in metadata:
            if (
                type(item) is not dict
                or set(item) - {'exception_type', 'closed_code', 'frames'}
                or not label(item.get('exception_type'))
                or not frames(item.get('frames'))
            ):
                return None
            if 'closed_code' in item and (
                item['exception_type'] not in _INNER_TYPES or item['closed_code'] not in _INNER_CODES
            ):
                return None
        raw = json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))
        return json.loads(raw) if len(raw.encode()) <= 8192 else None
    except Exception:
        return None


def _safe_exception(error, stage):
    # Deliberately never call str/repr(error), serialize its dict, or read body/URL/headers.
    if stage not in _STAGES:
        stage = 'stock_supervisor'
    result = {
        'contract': 'ella-causal-diagnostic-v1',
        'stage': stage,
        'exception_type': type(error).__name__ if type(error).__name__ in _TYPES else 'OtherException',
    }
    causes = []
    metadata = []
    seen = set()
    cursor = error
    for _ in range(4):
        if cursor is None or id(cursor) in seen:
            break
        seen.add(id(cursor))
        label = type(cursor).__name__ if type(cursor).__name__ in _TYPES else 'OtherException'
        causes.append(label)
        record = {'exception_type': label, 'frames': _frames(cursor)}
        if label in _INNER_TYPES:
            args = cursor.args
            if type(args) is tuple and len(args) == 1 and type(args[0]) is str and args[0] in _INNER_CODES:
                record['closed_code'] = args[0]
        metadata.append(record)
        cursor = cursor.__cause__ or cursor.__context__
    result['cause_types'] = causes
    result['cause_metadata'] = metadata
    fields = getattr(error, '__dict__', {})
    if type(fields) is dict:
        code = fields.get('code')
        if type(code) is str and code in _CODES:
            result['closed_code'] = code
        response = fields.get('response')
        status = (
            getattr(response, 'status_code', None)
            if type(error).__name__ in {'HTTPStatusError', 'HTTPError'}
            else fields.get('status_code')
        )
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
    if isinstance(error, OSError) and type(error.errno) is int:
        result['errno'] = error.errno
    frames = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        name = code.co_filename.rsplit('/', 1)[-1]
        if name in _FILES and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', code.co_name):
            frames.append({'file': name, 'function': code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    result['frames'] = frames[-8:]
    return result


def safe_exception(error, stage):
    try:
        return _safe_exception(error, stage)
    except Exception:
        # Diagnostics must never replace the original business outcome, including
        # hostile/failed metadata access. No exception content is serialized.
        return {
            'contract': 'ella-causal-diagnostic-v1',
            'stage': stage if type(stage) is str and stage in _STAGES else 'stock_supervisor',
            'exception_type': 'OtherException',
            'cause_types': [],
            'frames': [],
            'diagnostic_status': 'metadata_unavailable',
        }


def emit(error, stage):
    value = safe_exception(error, stage)
    try:
        logging.getLogger('ella.causal').warning('ella_causal_diagnostic %s', json.dumps(value, sort_keys=True))
    except Exception:
        pass
    return value
