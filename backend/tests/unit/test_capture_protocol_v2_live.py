import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BACKEND = Path(__file__).resolve().parents[2]


class _Snapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Document:
    def __init__(self, data=None):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Transaction:
    def __init__(self):
        self.sets = []
        self.updates = []

    def set(self, ref, data):
        self.sets.append((ref, data))

    def update(self, ref, data):
        self.updates.append((ref, data))


def _load_capture_protocol():
    spec = importlib.util.spec_from_file_location(
        'capture_protocol_v2_live_test_module',
        BACKEND / 'utils' / 'conversations' / 'capture_protocol.py',
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'database._client': MagicMock(db=MagicMock())}):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def capture_protocol():
    return _load_capture_protocol()


def _authority(conversation_id='capture-a', generation='generation-a', owner='owner-a', state='active'):
    return {
        'protocol_version': 2,
        'conversation_id': conversation_id,
        'generation': generation,
        'owner_token': owner,
        'state': state,
        'lease_expires_at': datetime.now(timezone.utc) + timedelta(seconds=30),
    }


def _conversation(
    conversation_id='capture-a',
    generation='generation-a',
    owner='owner-a',
    state='active',
    status='in_progress',
):
    return {
        'id': conversation_id,
        'status': status,
        'capture_owner_id': owner if state == 'active' else None,
        'capture_protocol_version': 2,
        'capture_generation': generation,
        'capture_owner_token': owner,
        'capture_state': state,
    }


def _updated(document, transaction, ref):
    result = dict(document)
    for updated_ref, values in transaction.updates:
        if updated_ref is ref:
            result.update(values)
    for set_ref, values in transaction.sets:
        if set_ref is ref:
            result = dict(values)
    return result


def test_capture_protocol_rejects_pre_v2_and_unattested_v2_before_creation(capture_protocol, monkeypatch):
    class _Socket:
        def __init__(self):
            self.closes = []

        async def close(self, *, code, reason):
            self.closes.append((code, reason))

    async def attempt(version):
        socket = _Socket()
        accepted = await capture_protocol.require_capture_protocol_before_creation(socket, version)
        return accepted, socket.closes

    monkeypatch.setenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', 'legacy_workers_drained')
    for version in (0, 1):
        accepted, closes = asyncio.run(attempt(version))
        assert accepted is False
        assert closes == [
            (
                capture_protocol.CAPTURE_PROTOCOL_UPGRADE_CLOSE_CODE,
                capture_protocol.CAPTURE_PROTOCOL_UPGRADE_REASON,
            )
        ]

    monkeypatch.delenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', raising=False)
    accepted, closes = asyncio.run(attempt(2))
    assert accepted is False
    assert closes == [
        (
            capture_protocol.CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_CLOSE_CODE,
            capture_protocol.CAPTURE_PROTOCOL_ROLLOUT_BLOCKED_REASON,
        )
    ]

    monkeypatch.setenv('CAPTURE_PROTOCOL_V2_ROLLOUT_STATE', 'legacy_workers_drained')
    accepted, closes = asyncio.run(attempt(2))
    assert accepted is True
    assert closes == []


@pytest.mark.parametrize('codec', ['opus_fs320', 'pcm16'])
def test_capture_ready_receipt_is_complete_for_each_production_codec(codec):
    from models.message_event import MessageServiceStatusEvent

    receipt = MessageServiceStatusEvent(
        status='capture_protocol_ready',
        protocol_version=2,
        conversation_id='capture-a',
        generation='generation-a',
        owner_token='owner-a',
    ).to_json()

    assert codec in {'opus_fs320', 'pcm16'}
    assert receipt == {
        'type': 'service_status',
        'status': 'capture_protocol_ready',
        'status_text': None,
        'protocol_version': 2,
        'conversation_id': 'capture-a',
        'generation': 'generation-a',
        'owner_token': 'owner-a',
    }


def test_rotation_installs_successor_and_drains_only_exact_predecessor(capture_protocol):
    now = datetime.now(timezone.utc)
    authority_ref = _Document(_authority())
    predecessor_ref = _Document(_conversation())
    successor_ref = _Document(
        {
            'id': 'capture-b',
            'status': 'in_progress',
            'capture_owner_id': 'owner-a',
        }
    )
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        authority_ref,
        successor_ref,
        'capture-b',
        'generation-a',
        'owner-a',
        now,
        'capture-a',
        predecessor_ref,
        False,
    )

    assert installed is True
    assert _updated(predecessor_ref.data, transaction, predecessor_ref)['capture_state'] == 'drained'
    successor = _updated(successor_ref.data, transaction, successor_ref)
    assert successor['capture_state'] == 'active'
    assert successor['capture_generation'] == 'generation-a'
    assert successor['capture_owner_token'] == 'owner-a'
    assert _updated(authority_ref.data, transaction, authority_ref)['conversation_id'] == 'capture-b'


def test_rotation_rejects_stale_generation_without_writes(capture_protocol):
    transaction = _Transaction()
    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        _Document(_authority(generation='generation-b', owner='owner-b')),
        _Document({'id': 'capture-b', 'status': 'in_progress', 'capture_owner_id': 'owner-a'}),
        'capture-b',
        'generation-a',
        'owner-a',
        datetime.now(timezone.utc),
        'capture-a',
        _Document(_conversation()),
        False,
    )

    assert installed is False
    assert transaction.updates == []
    assert transaction.sets == []


def test_expired_predecessor_allows_new_generation_reconnect(capture_protocol):
    now = datetime.now(timezone.utc)
    authority = _authority(generation='generation-a', owner='owner-a')
    authority['lease_expires_at'] = now - timedelta(seconds=1)
    predecessor = _conversation(generation='generation-a', owner='owner-a')
    predecessor['capture_owner_id'] = 'owner-b'
    predecessor['capture_lease_expires_at'] = now - timedelta(seconds=1)
    authority_ref = _Document(authority)
    predecessor_ref = _Document(predecessor)
    successor_ref = _Document(
        {
            'id': 'capture-b',
            'status': 'in_progress',
            'capture_owner_id': 'owner-b',
        }
    )
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        authority_ref,
        successor_ref,
        'capture-b',
        'generation-b',
        'owner-b',
        now,
        'capture-a',
        predecessor_ref,
        False,
    )

    assert installed is True
    assert _updated(predecessor, transaction, predecessor_ref)['capture_state'] == 'drained'
    successor = _updated(successor_ref.data, transaction, successor_ref)
    assert successor['capture_state'] == 'active'
    assert successor['capture_generation'] == 'generation-b'
    assert successor['capture_owner_token'] == 'owner-b'
    current_authority = _updated(authority, transaction, authority_ref)
    assert current_authority['conversation_id'] == 'capture-b'
    assert current_authority['generation'] == 'generation-b'
    assert current_authority['owner_token'] == 'owner-b'

    authority_ref.data = current_authority
    predecessor_ref.data = _updated(predecessor, transaction, predecessor_ref)
    successor_ref.data = successor
    retry_transaction = _Transaction()
    retry_install = capture_protocol._install_authority_transaction.to_wrap(
        retry_transaction,
        authority_ref,
        successor_ref,
        'capture-b',
        'generation-b',
        'owner-b',
        now,
        'capture-a',
        predecessor_ref,
        False,
    )

    assert retry_install is True
    assert retry_transaction.updates == []
    assert retry_transaction.sets == []


def test_expired_authority_allows_legacy_predecessor_rotation(capture_protocol):
    now = datetime.now(timezone.utc)
    authority = _authority(conversation_id='capture-stale', generation='generation-stale', owner='owner-stale')
    authority['lease_expires_at'] = now - timedelta(days=1)
    predecessor = {
        'id': 'capture-legacy',
        'status': 'in_progress',
        'capture_owner_id': None,
    }
    authority_ref = _Document(authority)
    predecessor_ref = _Document(predecessor)
    successor_ref = _Document(
        {
            'id': 'capture-current',
            'status': 'in_progress',
            'capture_owner_id': 'owner-current',
        }
    )
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        authority_ref,
        successor_ref,
        'capture-current',
        'generation-current',
        'owner-current',
        now,
        'capture-legacy',
        predecessor_ref,
        False,
    )

    assert installed is True
    assert _updated(predecessor, transaction, predecessor_ref)['capture_state'] == 'drained'
    successor = _updated(successor_ref.data, transaction, successor_ref)
    assert successor['capture_state'] == 'active'
    current_authority = _updated(authority, transaction, authority_ref)
    assert current_authority['conversation_id'] == 'capture-current'
    assert current_authority['generation'] == 'generation-current'
    assert current_authority['owner_token'] == 'owner-current'


def test_live_authority_rejects_legacy_predecessor_rotation_without_writes(capture_protocol):
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        _Document(_authority(conversation_id='capture-live', generation='generation-live', owner='owner-live')),
        _Document({'id': 'capture-current', 'status': 'in_progress', 'capture_owner_id': 'owner-current'}),
        'capture-current',
        'generation-current',
        'owner-current',
        datetime.now(timezone.utc),
        'capture-legacy',
        _Document({'id': 'capture-legacy', 'status': 'in_progress', 'capture_owner_id': None}),
        False,
    )

    assert installed is False
    assert transaction.updates == []
    assert transaction.sets == []


def test_drained_predecessor_allows_exact_new_generation_rotation(capture_protocol):
    now = datetime.now(timezone.utc)
    authority = _authority(generation='generation-a', owner='owner-a', state='drained')
    authority['lease_expires_at'] = now - timedelta(seconds=1)
    predecessor = _conversation(
        generation='generation-a',
        owner='owner-a',
        state='drained',
    )
    predecessor['capture_owner_id'] = 'owner-b'
    predecessor['capture_lease_expires_at'] = now - timedelta(seconds=1)
    authority_ref = _Document(authority)
    predecessor_ref = _Document(predecessor)
    successor_ref = _Document(
        {
            'id': 'capture-b',
            'status': 'in_progress',
            'capture_owner_id': 'owner-b',
        }
    )
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        authority_ref,
        successor_ref,
        'capture-b',
        'generation-b',
        'owner-b',
        now,
        'capture-a',
        predecessor_ref,
        False,
    )

    assert installed is True
    successor = _updated(successor_ref.data, transaction, successor_ref)
    assert successor['capture_state'] == 'active'
    assert successor['capture_generation'] == 'generation-b'
    assert successor['capture_owner_token'] == 'owner-b'
    current_authority = _updated(authority, transaction, authority_ref)
    assert current_authority['conversation_id'] == 'capture-b'
    assert current_authority['generation'] == 'generation-b'
    assert current_authority['owner_token'] == 'owner-b'


def test_drained_predecessor_rejects_mismatch_or_invalid_lease_without_writes(capture_protocol):
    now = datetime.now(timezone.utc)
    expired = now - timedelta(seconds=1)

    for mutation in ('authority_tuple', 'conversation_state', 'authority_lease', 'conversation_lease'):
        authority = _authority(generation='generation-a', owner='owner-a', state='drained')
        authority['lease_expires_at'] = expired
        predecessor = _conversation(
            generation='generation-a',
            owner='owner-a',
            state='drained',
        )
        predecessor['capture_owner_id'] = 'owner-b'
        predecessor['capture_lease_expires_at'] = expired
        if mutation == 'authority_tuple':
            authority['conversation_id'] = 'capture-other'
        elif mutation == 'conversation_state':
            predecessor['capture_state'] = 'active'
        elif mutation == 'authority_lease':
            authority['lease_expires_at'] = 'invalid'
        else:
            predecessor['capture_lease_expires_at'] = 'invalid'
        transaction = _Transaction()

        installed = capture_protocol._install_authority_transaction.to_wrap(
            transaction,
            _Document(authority),
            _Document({'id': 'capture-b', 'status': 'in_progress', 'capture_owner_id': 'owner-b'}),
            'capture-b',
            'generation-b',
            'owner-b',
            now,
            'capture-a',
            _Document(predecessor),
            False,
        )

        assert installed is False
        assert transaction.updates == []
        assert transaction.sets == []


def test_live_predecessor_rejects_new_generation_reconnect(capture_protocol):
    now = datetime.now(timezone.utc)
    authority = _authority(generation='generation-a', owner='owner-a')
    predecessor = _conversation(generation='generation-a', owner='owner-a')
    predecessor['capture_owner_id'] = 'owner-b'
    predecessor['capture_lease_expires_at'] = now + timedelta(seconds=30)
    transaction = _Transaction()

    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        _Document(authority),
        _Document({'id': 'capture-b', 'status': 'in_progress', 'capture_owner_id': 'owner-b'}),
        'capture-b',
        'generation-b',
        'owner-b',
        now,
        'capture-a',
        _Document(predecessor),
        False,
    )

    assert installed is False
    assert transaction.updates == []
    assert transaction.sets == []


def test_missing_or_malformed_lease_rejects_new_generation_reconnect(capture_protocol):
    now = datetime.now(timezone.utc)
    expired = now - timedelta(seconds=1)
    lease_variants = [
        ({}, {'capture_lease_expires_at': expired}),
        ({'lease_expires_at': 'invalid'}, {'capture_lease_expires_at': expired}),
        ({'lease_expires_at': expired}, {}),
        ({'lease_expires_at': expired}, {'capture_lease_expires_at': 'invalid'}),
    ]

    for authority_lease, predecessor_lease in lease_variants:
        authority = _authority(generation='generation-a', owner='owner-a')
        authority.pop('lease_expires_at', None)
        authority.update(authority_lease)
        predecessor = _conversation(generation='generation-a', owner='owner-a')
        predecessor['capture_owner_id'] = 'owner-b'
        predecessor.pop('capture_lease_expires_at', None)
        predecessor.update(predecessor_lease)
        transaction = _Transaction()

        installed = capture_protocol._install_authority_transaction.to_wrap(
            transaction,
            _Document(authority),
            _Document({'id': 'capture-b', 'status': 'in_progress', 'capture_owner_id': 'owner-b'}),
            'capture-b',
            'generation-b',
            'owner-b',
            now,
            'capture-a',
            _Document(predecessor),
            False,
        )

        assert installed is False
        assert transaction.updates == []
        assert transaction.sets == []


def test_adoption_rejects_another_live_authority_without_writes(capture_protocol):
    transaction = _Transaction()
    installed = capture_protocol._install_authority_transaction.to_wrap(
        transaction,
        _Document(_authority(conversation_id='capture-a', generation='generation-a', owner='owner-a')),
        _Document({'id': 'capture-b', 'status': 'in_progress', 'capture_owner_id': 'owner-b'}),
        'capture-b',
        'generation-b',
        'owner-b',
        datetime.now(timezone.utc),
        None,
        None,
        True,
    )

    assert installed is False
    assert transaction.updates == []
    assert transaction.sets == []


def test_drain_and_finalization_require_exact_tuple_and_reach_terminal(capture_protocol):
    now = datetime.now(timezone.utc)
    authority_ref = _Document(_authority())
    conversation_ref = _Document(_conversation())
    drain_transaction = _Transaction()

    drained = capture_protocol._mark_drained_transaction.to_wrap(
        drain_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        now,
    )
    assert drained is True
    drained_authority = _updated(authority_ref.data, drain_transaction, authority_ref)
    drained_conversation = _updated(conversation_ref.data, drain_transaction, conversation_ref)
    assert drained_authority['state'] == 'drained'
    assert drained_conversation['capture_state'] == 'drained'
    assert drained_conversation['capture_owner_id'] is None

    authority_ref.data = drained_authority
    conversation_ref.data = drained_conversation
    stale_transaction = _Transaction()
    stale_outcome = capture_protocol._claim_finalization_transaction.to_wrap(
        stale_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-stale',
        'owner-a',
        'claim-stale',
        now,
    )
    assert stale_outcome == 'mismatch'
    assert stale_transaction.updates == []

    claim_transaction = _Transaction()
    outcome = capture_protocol._claim_finalization_transaction.to_wrap(
        claim_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        now,
    )
    assert outcome == 'claimed'
    authority_ref.data = _updated(authority_ref.data, claim_transaction, authority_ref)
    conversation_ref.data = _updated(conversation_ref.data, claim_transaction, conversation_ref)
    conversation_ref.data['status'] = 'completed'

    missing_receipt_transaction = _Transaction()
    assert (
        capture_protocol._complete_finalization_transaction.to_wrap(
            missing_receipt_transaction,
            authority_ref,
            conversation_ref,
            'capture-a',
            'generation-a',
            'owner-a',
            'claim-a',
            now,
        )
        is False
    )
    assert missing_receipt_transaction.updates == []

    effect_claim_transaction = _Transaction()
    effect_claim = capture_protocol._claim_finalization_effect_transaction.to_wrap(
        effect_claim_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        'integrations:external',
        now,
    )
    assert effect_claim['outcome'] == 'claimed'
    assert effect_claim['operation_token'] == capture_protocol.capture_finalization_effect_operation_token(
        'capture-a',
        'generation-a',
        'owner-a',
        'integrations:external',
    )
    authority_ref.data = _updated(authority_ref.data, effect_claim_transaction, authority_ref)
    conversation_ref.data = _updated(conversation_ref.data, effect_claim_transaction, conversation_ref)

    effect_complete_transaction = _Transaction()
    assert capture_protocol._complete_finalization_effect_transaction.to_wrap(
        effect_complete_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        'integrations:external',
        effect_claim['operation_token'],
        [],
        now,
    )
    conversation_ref.data = _updated(conversation_ref.data, effect_complete_transaction, conversation_ref)

    incomplete_post_effect_transaction = _Transaction()
    post_effect_claim = capture_protocol._claim_finalization_effect_transaction.to_wrap(
        incomplete_post_effect_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        'post:ella_postprocess_webhook',
        now,
    )
    assert post_effect_claim['outcome'] == 'claimed'
    authority_ref.data = _updated(authority_ref.data, incomplete_post_effect_transaction, authority_ref)
    conversation_ref.data = _updated(conversation_ref.data, incomplete_post_effect_transaction, conversation_ref)

    incomplete_effect_completion_transaction = _Transaction()
    assert (
        capture_protocol._complete_finalization_transaction.to_wrap(
            incomplete_effect_completion_transaction,
            authority_ref,
            conversation_ref,
            'capture-a',
            'generation-a',
            'owner-a',
            'claim-a',
            now,
        )
        is False
    )
    assert incomplete_effect_completion_transaction.updates == []

    post_effect_complete_transaction = _Transaction()
    assert capture_protocol._complete_finalization_effect_transaction.to_wrap(
        post_effect_complete_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        'post:ella_postprocess_webhook',
        post_effect_claim['operation_token'],
        None,
        now,
    )
    conversation_ref.data = _updated(conversation_ref.data, post_effect_complete_transaction, conversation_ref)

    replay_transaction = _Transaction()
    replay = capture_protocol._claim_finalization_effect_transaction.to_wrap(
        replay_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        'integrations:external',
        now,
    )
    assert replay == {
        'outcome': 'completed',
        'operation_token': effect_claim['operation_token'],
        'result': [],
    }
    assert replay_transaction.updates == []

    complete_transaction = _Transaction()
    completed = capture_protocol._complete_finalization_transaction.to_wrap(
        complete_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-a',
        now,
    )
    assert completed is True
    assert _updated(authority_ref.data, complete_transaction, authority_ref)['state'] == 'terminal'
    assert _updated(conversation_ref.data, complete_transaction, conversation_ref)['capture_state'] == 'terminal'


def test_expired_finalization_lease_is_reclaimable_and_stale_claim_cannot_commit(capture_protocol):
    now = datetime.now(timezone.utc)
    expired_at = now - timedelta(seconds=1)
    authority_ref = _Document(
        {
            **_authority(state='finalizing'),
            'finalization_claim_token': 'claim-old',
            'finalization_lease_expires_at': expired_at,
        }
    )
    conversation_ref = _Document(
        {
            **_conversation(state='finalizing'),
            'capture_finalization_claim_token': 'claim-old',
            'capture_finalization_lease_expires_at': expired_at,
        }
    )

    reclaim_transaction = _Transaction()
    assert (
        capture_protocol._claim_finalization_transaction.to_wrap(
            reclaim_transaction,
            authority_ref,
            conversation_ref,
            'capture-a',
            'generation-a',
            'owner-a',
            'claim-new',
            now,
        )
        == 'claimed'
    )
    authority_ref.data = _updated(authority_ref.data, reclaim_transaction, authority_ref)
    conversation_ref.data = _updated(conversation_ref.data, reclaim_transaction, conversation_ref)

    stale_effect_transaction = _Transaction()
    stale = capture_protocol._claim_finalization_effect_transaction.to_wrap(
        stale_effect_transaction,
        authority_ref,
        conversation_ref,
        'capture-a',
        'generation-a',
        'owner-a',
        'claim-old',
        'integrations:external',
        now,
    )
    assert stale == {'outcome': 'lost'}
    assert stale_effect_transaction.updates == []


def test_flush_capture_waits_for_delayed_tail_and_durable_buffer(capture_protocol):
    events = []

    async def exercise():
        buffers_drained = asyncio.Event()
        persistence_tasks = set()

        async def delayed_tail():
            await asyncio.sleep(0.01)
            events.append('tail-persisted')
            buffers_drained.set()

        async def finish_stt_inputs():
            events.append('provider-finished')
            persistence_tasks.add(asyncio.create_task(delayed_tail()))

        result = await capture_protocol.flush_capture_before_drained(
            finish_stt_inputs,
            persistence_tasks,
            buffers_drained,
            timeout=1,
        )
        events.append('drained')
        return result

    assert asyncio.run(exercise()) is True
    assert events == ['provider-finished', 'tail-persisted', 'drained']


def test_capture_drain_body_rejects_missing_and_cross_session_fields(capture_protocol):
    valid = {
        'type': 'capture_drain',
        'protocol_version': 2,
        'conversation_id': 'capture-a',
        'generation': 'generation-a',
        'owner_token': 'owner-a',
    }
    assert capture_protocol.valid_capture_drain_body(valid, 'capture-a', 'generation-a', 'owner-a')
    for field in ('protocol_version', 'conversation_id', 'generation', 'owner_token'):
        assert not capture_protocol.valid_capture_drain_body(
            {key: value for key, value in valid.items() if key != field},
            'capture-a',
            'generation-a',
            'owner-a',
        )
    assert not capture_protocol.valid_capture_drain_body(
        {**valid, 'owner_token': 'owner-b'},
        'capture-a',
        'generation-a',
        'owner-a',
    )
