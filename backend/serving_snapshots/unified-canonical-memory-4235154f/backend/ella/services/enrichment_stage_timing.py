"""Opt-in closed, content-free diagnostics; no authority or deadline changes."""

import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

_LOG = logging.getLogger('ella.enrichment.stage_timing')
_EMIT_LOCK = threading.RLock()  # Serialize complete records across all traces/threads.
_CURRENT = contextvars.ContextVar('ella_enrichment_stage_trace', default=None)
_PARENT = contextvars.ContextVar('ella_enrichment_parent_span', default=None)
_ALLOWED = frozenset(
    [
        'retained_completion_enrichment._enrich',
        'retained_completion_enrichment.await.118',
        'retained_completion_enrichment.await.120',
        'retained_completion_enrichment.await.139',
        'retained_completion_enrichment.await.144',
        'retained_completion_enrichment.await.145',
        'retained_completion_enrichment.await.147',
        'retained_completion_enrichment.await.151',
        'retained_completion_enrichment.await.155',
        'retained_completion_enrichment.await.156',
        'retained_completion_enrichment.await.158',
        'retained_completion_enrichment.await.171',
        'retained_completion_enrichment.await.176',
        'retained_completion_enrichment.await.177',
        'retained_completion_enrichment.await.179',
        'retained_completion_enrichment.await.195',
        'retained_completion_enrichment.await.198',
        'retained_completion_enrichment.await.211',
        'retained_completion_enrichment.await.213',
        'retained_completion_enrichment.await.244',
        'retained_completion_enrichment.await.249',
        'retained_completion_enrichment.await.262',
        'retained_completion_enrichment.await.268',
        'retained_completion_enrichment.await.271',
        'retained_completion_enrichment.await.296',
        'retained_completion_enrichment.await.318',
        'retained_completion_enrichment.await.321',
        'retained_completion_enrichment.await.326',
        'retained_completion_enrichment.await.68',
        'retained_completion_enrichment.await.72',
        'retained_completion_enrichment.await.82',
        'retained_completion_enrichment.await.93',
        'semantic_host_context.exchange',
        'semantic_host_context.host_context',
        'semantic_producer_ledger._fresh',
        'semantic_producer_ledger.await.289',
        'semantic_producer_ledger.await.290',
        'semantic_producer_ledger.await.293',
        'semantic_producer_ledger.await.299',
        'semantic_producer_ledger.await.52',
        'semantic_producer_ledger.current',
        'semantic_producer_ledger.execute_once',
        'semantic_producer_ledger.issue_publication',
        'semantic_retained_integration.await.31',
        'semantic_retained_integration.await.33',
        'semantic_retained_integration.await.57',
        'semantic_retained_integration.await.58',
        'semantic_retained_integration.await.61',
        'semantic_retained_integration.await.66',
        'semantic_retained_integration.await.69',
        'semantic_retained_integration.current',
        'semantic_self_hosted_runtime.__call__',
        'semantic_self_hosted_runtime._authority',
        'semantic_self_hosted_runtime.await.109',
        'semantic_self_hosted_runtime.await.111',
        'semantic_self_hosted_runtime.await.115',
        'semantic_self_hosted_runtime.await.119',
        'semantic_self_hosted_runtime.await.155',
        'semantic_self_hosted_runtime.await.158',
        'semantic_self_hosted_runtime.await.159',
        'semantic_self_hosted_runtime.await.181',
        'semantic_self_hosted_runtime.await.226',
        'semantic_self_hosted_runtime.await.227',
        'semantic_self_hosted_runtime.await.238',
        'semantic_self_hosted_runtime.await.250',
        'operation',
    ]
)
_MAX_EVENTS = 1000


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _emit(trace, event):
    # Diagnostics must never replace the original success, exception or cancellation.
    try:
        with trace['lock']:
            if trace['events'] >= _MAX_EVENTS:
                return
            trace['events'] += 1
        with _EMIT_LOCK:
            print('ella_enrichment_stage ' + json.dumps(event, sort_keys=True, allow_nan=False), flush=True)
    except BaseException:
        pass


@contextmanager
def operation(uid, conversation_id):
    trace = None
    try:
        expected = os.environ.get('ELLA_ENRICHMENT_STAGE_TIMING_ACCOUNT_SHA256', '')
        if (
            len(expected) == 64
            and all(c in '0123456789abcdef' for c in expected)
            and isinstance(uid, str)
            and hashlib.sha256(uid.encode()).hexdigest() == expected
        ):
            trace = {
                'id': uuid.uuid4().hex,
                'account': expected,  # Private context only; never exported to events.
                'source': hashlib.sha256(conversation_id.encode()).hexdigest(),
                'lock': threading.Lock(),
                'events': 0,
                'origin_ns': time.monotonic_ns(),
            }
    except Exception:
        pass
    existing = _CURRENT.get()
    if trace is not None and existing is not None and existing.get('account') == trace['account'] and existing.get('source') == trace['source']:
        yield  # Preserve the outer operation trace across identical-owner nested calls.
        return
    token = _CURRENT.set(trace)
    parent = _PARENT.set(None)
    try:
        with stage('operation'):
            yield
    finally:
        _PARENT.reset(parent)
        _CURRENT.reset(token)


@contextmanager
def stage(label):
    trace = _CURRENT.get()
    if trace is None or label not in _ALLOWED:
        yield
        return
    span = uuid.uuid4().hex
    parent = _PARENT.get()
    started = time.monotonic_ns()
    base = {
        'version': 1,
        'trace_id': trace['id'],
        'source_sha256': trace['source'],
        'span_id': span,
        'parent_span_id': parent,
        'stage': label,
    }
    _emit(trace, {**base, 'event': 'start', 'utc': _utc(), 'offset_ns': max(0, started - trace['origin_ns'])})
    token = _PARENT.set(span)
    outcome = 'returned'
    try:
        yield
    except BaseException:
        outcome = 'raised'  # No exception name, value, traceback or arguments.
        raise
    finally:
        _PARENT.reset(token)
        _emit(
            trace,
            {
                **base,
                'event': 'end',
                'utc': _utc(),
                'outcome': outcome,
                'elapsed_ns': max(0, time.monotonic_ns() - started),
                'offset_ns': max(0, time.monotonic_ns() - trace['origin_ns']),
            },
        )


async def await_stage(label, awaitable):
    with stage(label):
        return await awaitable
