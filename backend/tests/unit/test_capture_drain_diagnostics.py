"""Offline production receive-loop guards; no sockets, providers or storage.

Successor rejection cannot establish a historical predecessor's durable save.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_capture_incident_1210_regressions import _nested_code, _nested_function
from test_capture_protocol_v2_live import _Document, _Transaction, _authority, _conversation, _load_capture_protocol


class _Disconnect(Exception):
    pass


class _ConsentRejected(Exception):
    pass


async def _attempt(*, wrong_field=None, takeover=False, redis_released=True, timeout=False):
    protocol = _load_capture_protocol()
    authority, conversation = _Document(_authority()), _Document(_conversation())
    transaction = _Transaction()
    logs, acknowledgments, errors = [], [], []
    body = dict(
        type='capture_drain',
        protocol_version=2,
        conversation_id='capture-a',
        generation='generation-a',
        owner_token='owner-a',
    )
    if wrong_field:
        body[wrong_field] = 'wrong-fixture-value'

    async def receive():
        return {'text': json.dumps(body)}

    async def acknowledge(event):
        acknowledgments.append(event)
        return True

    fail_timeout = timeout

    async def flush(finish, tasks, complete, *, timeout):
        assert timeout == 10.0
        if takeover:
            authority.data = _authority('capture-b', 'generation-b', 'owner-b')
        if fail_timeout:
            return False
        await finish()
        if tasks:
            await asyncio.gather(*tasks)
        assert complete.is_set()
        return True

    def mark_drained(uid, conversation_id, generation, owner):
        return protocol._mark_drained_transaction.to_wrap(
            transaction, authority, conversation, conversation_id, generation, owner, datetime.now(timezone.utc)
        )

    mark, release, closed = Mock(side_effect=mark_drained), Mock(return_value=redis_released), Mock()
    code = _nested_code('routers/transcribe.py', 'receive_data')
    values = dict.fromkeys(code.co_freevars)
    values.update(
        sample_rate=16000,
        websocket=SimpleNamespace(receive=receive),
        websocket_active=True,
        websocket_close_code=1001,
        accepting_capture=True,
        capture_drained=False,
        current_conversation_id='capture-a',
        uid='fixture-user',
        generation_id='generation-a',
        owner_token='owner-a',
        session_id='owner-a',
        delivery_receipt=SimpleNamespace(drain_requested=False, drain_completed=False),
        ambient_deadlines=SimpleNamespace(close=closed),
        _delivery_log=lambda event, **metadata: logs.append((event, metadata)),
        _asend_message_event=acknowledge,
        _capture_buffers_contain_conversation=lambda _: False,
        capture_buffers_changed=asyncio.Event(),
        image_chunks={},
        use_custom_stt=True,
        ai_consent_egress_rejected=asyncio.Event(),
    )
    globals_ = dict(
        asyncio=asyncio,
        json=json,
        time=time,
        valid_capture_drain_body=protocol.valid_capture_drain_body,
        flush_capture_before_drained=flush,
        mark_capture_drained=mark,
        redis_db=SimpleNamespace(release_owned_in_progress_conversation_id=release),
        conversations_db=SimpleNamespace(list_capture_persistence_batches=lambda *_: []),
        drain_capture_persistence_batches=lambda *_: None,
        MessageServiceStatusEvent=lambda **fields: fields,
        CAPTURE_PROTOCOL_VERSION=2,
        WebSocketDisconnect=_Disconnect,
        AiConsentWebSocketRejected=_ConsentRejected,
        print=lambda *args, **kwargs: errors.append(args),
    )
    receive_loop = _nested_function('routers/transcribe.py', 'receive_data', globals_, values)
    await asyncio.wait_for(receive_loop(None, None, None, None, None, None), timeout=3)
    state = {key: cell.cell_contents for key, cell in zip(code.co_freevars, receive_loop.__closure__)}
    assert not errors, errors  # Broad production exception handler must not hide fixture failures.
    return SimpleNamespace(
        state=state,
        logs=logs,
        acknowledgments=acknowledgments,
        mark=mark,
        release=release,
        closed=closed,
        authority=authority,
        conversation=conversation,
        transaction=transaction,
    )


def _assert_rejected(result, reason, close_code=1008):
    assert result.logs == [('capture_drain_rejected', {'reason': reason})]
    assert result.state['websocket_close_code'] == close_code
    assert result.state['capture_drained'] is False
    assert result.state['delivery_receipt'].drain_requested is True
    assert result.state['delivery_receipt'].drain_completed is False
    assert result.acknowledgments == []
    for private_value in ('fixture-user', 'capture-a', 'capture-b', 'owner-a', 'owner-b', 'generation-a'):
        assert private_value not in json.dumps(result.logs)


@pytest.mark.parametrize('field', ['protocol_version', 'conversation_id', 'generation', 'owner_token'])
def test_drain_tuple_mismatch_is_distinct_and_never_changes_ownership(field):
    result = asyncio.run(_attempt(wrong_field=field))
    _assert_rejected(result, 'tuple_mismatch')
    result.mark.assert_not_called()
    result.release.assert_not_called()
    result.closed.assert_not_called()
    assert result.transaction.updates == []


def test_successor_takeover_during_drain_never_releases_or_acknowledges_successor():
    result = asyncio.run(_attempt(takeover=True))
    _assert_rejected(result, 'authority_rejected')
    result.mark.assert_called_once_with('fixture-user', 'capture-a', 'generation-a', 'owner-a')
    result.release.assert_not_called()
    assert result.authority.data['conversation_id'] == 'capture-b'
    assert result.authority.data['generation'] == 'generation-b'
    assert result.authority.data['owner_token'] == 'owner-b'
    assert result.authority.data['state'] == 'active'
    assert result.conversation.data['capture_state'] == 'active'
    assert result.transaction.updates == []


def test_redis_owner_release_failure_never_claims_completed_drain():
    result = asyncio.run(_attempt(redis_released=False))
    _assert_rejected(result, 'redis_owner_release_rejected')
    result.release.assert_called_once_with('fixture-user', 'capture-a', 'owner-a')
    assert len(result.transaction.updates) == 2


def test_persistence_timeout_remains_1011_without_authority_or_redis_release():
    result = asyncio.run(_attempt(timeout=True))
    _assert_rejected(result, 'persistence_timeout', close_code=1011)
    result.mark.assert_not_called()
    result.release.assert_not_called()
    assert result.transaction.updates == []


def test_successful_drain_retains_exact_acknowledgment_and_no_failure_diagnostic():
    result = asyncio.run(_attempt())
    assert result.logs == []
    assert result.state['capture_drained'] is True
    assert result.state['delivery_receipt'].drain_completed is True
    result.release.assert_called_once_with('fixture-user', 'capture-a', 'owner-a')
    assert result.acknowledgments == [
        dict(
            status='capture_protocol_drained',
            protocol_version=2,
            conversation_id='capture-a',
            generation='generation-a',
            owner_token='owner-a',
        )
    ]
