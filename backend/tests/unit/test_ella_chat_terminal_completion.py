"""Run the actual chat generator with isolated, synthetic collaborators.

Extract only the function AST so importing the router cannot load credentials,
initialize databases, or contact services. No provider/database calls are real.
"""

import __future__
import ast
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'ella/routers/chat.py'


def _fixture(lines, recovery_result='Synthetic recovery', recovery_error=None):
    writes = []
    calls = []

    async def recent(*args, **kwargs):
        return []

    async def temporal(*args, **kwargs):
        return 'synthetic context', []

    async def write(event):
        writes.append(event)

    async def consent(uid):
        calls.append('consent')

    runtime = SimpleNamespace(
        provider='hermes',
        profile_name='synthetic',
        gateway_url='http://invalid.test',
        gateway_token='synthetic',
        agent_id='synthetic',
    )

    async def revalidate(identity):
        calls.append('authority')
        return runtime

    async def recovery(*args, **kwargs):
        calls.append('recovery')
        if recovery_error is not None:
            raise recovery_error
        return recovery_result

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append('closed')
            return False

        async def aiter_lines(self):
            for line in lines:
                if isinstance(line, asyncio.Event):
                    await line.wait()
                    continue
                if isinstance(line, BaseException):
                    raise line
                yield line

    class Client(Response):
        def __init__(self, *args, **kwargs):
            calls.append('client')

        def stream(self, *args, **kwargs):
            calls.append('stream')
            return Response()

    class ProvisioningError(Exception):
        pass

    class TimeoutException(Exception):
        pass

    class AiConsentHTTPException(Exception):
        pass

    namespace = {
        'base64': base64,
        'json': json,
        'datetime': datetime,
        'timezone': timezone,
        'uuid4': uuid4,
        '_time': time,
        'ProvisioningError': ProvisioningError,
        'httpx': SimpleNamespace(AsyncClient=Client, TimeoutException=TimeoutException),
        'runtime_authority_identity': lambda value: 'synthetic-identity',
        'revalidate_runtime_authority': revalidate,
        '_fetch_chat_canonical_events': recent,
        '_fetch_temporal_chat_context': temporal,
        'format_canonical_context': lambda *args, **kwargs: '',
        '_write_ios_chat_canonical_event': write,
        '_ios_chat_event': lambda **kwargs: kwargs,
        '_hermes_chat_session_key': lambda uid: 'synthetic-session',
        '_hermes_chat_memory_key': lambda uid: 'synthetic-memory',
        '_hermes_chat_headers': lambda *args: {},
        '_hermes_nonstream_completion': recovery,
        '_hermes_nonstream_completion_with_current_consent': recovery,
        '_assert_current_ai_consent_async': consent,
        'AiConsentHTTPException': AiConsentHTTPException,
        'CHAT_CONTEXT_LIMIT': 1,
        'CHAT_CONTEXT_MAX_CHARS': 100,
        'CHAT_TEMPORAL_CONTEXT_MAX_CHARS': 100,
        'CHAT_USER_TIMEZONE': 'UTC',
        'HERMES_CHAT_SESSION_SCOPE': 'synthetic',
        'HERMES_CHAT_REQUEST_TIMEOUT_SECONDS': 60,
    }
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_produce_hermes_chat_events'
    )
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(SOURCE), 'exec', flags=__future__.annotations.compiler_flag), namespace)

    stream = namespace['_produce_hermes_chat_events'](
        'Synthetic question', 'synthetic-owner', turn_id='synthetic-turn', runtime=runtime
    )
    return stream, writes, calls


def _exercise(lines, **kwargs):
    stream, writes, calls = _fixture(lines, **kwargs)

    async def collect():
        return [event async for event in stream]

    return asyncio.run(collect()), writes, calls


def _delta(text='', finish=None):
    return 'data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': finish}]})


@pytest.mark.parametrize(
    'lines',
    [
        [_delta('Partial')],
        [_delta('Partial'), 'data: {malformed'],
        [_delta('Partial'), 'data: [DONE]'],
        [],
    ],
)
def test_eof_without_terminal_does_not_commit_or_retry(lines):
    events, writes, calls = _exercise(lines)
    assert events[-1] == 'data: Error: hermes_unavailable\n\n'
    assert not any(event.startswith('done: ') for event in events)
    assert [event['role'] for event in writes] == ['user']
    assert calls.count('stream') == 1
    assert 'recovery' not in calls


@pytest.mark.parametrize('reason', ['length', 'content_filter', 'tool_calls', 'unknown', 7])
def test_non_success_finish_does_not_commit_or_retry(reason):
    events, writes, calls = _exercise([_delta('Partial'), _delta(finish=reason), 'data: [DONE]'])
    assert events[-1] == 'data: Error: hermes_unavailable\n\n'
    assert not any(event.startswith('done: ') for event in events)
    assert [event['role'] for event in writes] == ['user']
    assert 'recovery' not in calls


@pytest.mark.parametrize(
    'tail',
    [
        [_delta(finish='stop')],
        [_delta(finish='stop'), 'data: [DONE]'],
        [_delta(finish='stop'), _delta(finish='stop'), 'data: [DONE]', 'data: [DONE]'],
    ],
)
def test_success_terminal_commits_exactly_once(tail):
    events, writes, calls = _exercise([_delta('Synthetic answer'), *tail])
    assert events[0] == 'data: Synthetic answer\n\n'
    assert sum(event.startswith('done: ') for event in events) == 1
    assert [event['role'] for event in writes] == ['user', 'assistant']
    assert writes[-1]['text'] == 'Synthetic answer'
    assert calls.count('stream') == 1
    assert 'recovery' not in calls


def test_valid_empty_terminal_preserves_existing_recovery_contract():
    events, writes, calls = _exercise(['data: [DONE]'])
    assert calls.count('recovery') == 1
    assert [event['role'] for event in writes] == ['user', 'assistant']
    assert writes[-1]['text'] == 'Synthetic recovery'
    assert sum(event.startswith('done: ') for event in events) == 1


def test_content_after_stop_is_not_persisted_as_completed():
    events, writes, calls = _exercise([_delta('Partial'), _delta(finish='stop'), _delta('Invalid continuation')])
    assert events[-1].startswith('data: Error: ')
    assert not any(event.startswith('done: ') for event in events)
    assert [event['role'] for event in writes] == ['user']
    assert 'recovery' not in calls


def test_content_in_same_chunk_as_stop_is_complete():
    events, writes, calls = _exercise([_delta('Synthetic final', finish='stop')])
    assert sum(event.startswith('done: ') for event in events) == 1
    assert writes[-1]['text'] == 'Synthetic final'
    assert 'recovery' not in calls


@pytest.mark.parametrize('recovery', [None, ''])
def test_empty_recovery_does_not_persist_a_completed_answer(recovery):
    events, writes, calls = _exercise(['data: [DONE]'], recovery_result=recovery)
    assert not any(event.startswith('done: ') for event in events)
    assert [event['role'] for event in writes] == ['user']
    assert calls.count('recovery') == 1


def test_recovery_failure_does_not_persist_a_completed_answer():
    events, writes, calls = _exercise(['data: [DONE]'], recovery_error=RuntimeError('synthetic failure'))
    assert events[-1].startswith('data: Error: ')
    assert not any(event.startswith('done: ') for event in events)
    assert [event['role'] for event in writes] == ['user']
    assert calls.count('recovery') == 1


@pytest.mark.parametrize('stop_seen', [False, True])
def test_cancelled_provider_read_propagates_and_closes_without_assistant_commit(stop_seen):
    async def scenario():
        blocked = asyncio.Event()
        lines = [_delta('Partial')]
        if stop_seen:
            lines.append(_delta(finish='stop'))
        lines.append(blocked)
        stream, writes, calls = _fixture(lines)
        assert await anext(stream) == 'data: Partial\n\n'
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
        assert [event['role'] for event in writes] == ['user']
        assert 'recovery' not in calls
        assert calls.count('closed') == 2

    asyncio.run(scenario())
