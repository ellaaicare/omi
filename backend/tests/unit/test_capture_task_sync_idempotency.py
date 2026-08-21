from datetime import datetime, timedelta, timezone
import asyncio
import importlib
import os
import sys
import threading
import types

import pytest

os.environ.setdefault('FIRESTORE_EMULATOR_HOST', 'localhost:9999')
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'test-project')
os.environ.setdefault('ENCRYPTION_SECRET', 'test' * 8)

if 'stripe' not in sys.modules:
    stripe_stub = types.ModuleType('stripe')
    stripe_stub.api_key = None
    sys.modules['stripe'] = stripe_stub

if 'redis' not in sys.modules:
    redis_stub = types.ModuleType('redis')

    class _RedisStub:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    redis_stub.Redis = _RedisStub
    sys.modules['redis'] = redis_stub

notifications_stub = types.ModuleType('utils.notifications')
notifications_stub.send_apple_reminders_sync_push = lambda *args, **kwargs: True
sys.modules['utils.notifications'] = notifications_stub

import database.task_sync as task_sync_db
from routers import task_integrations

sys.modules.pop('utils.task_sync', None)
task_sync = importlib.import_module('utils.task_sync')


class _Snapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data)


class _ReceiptRef:
    def __init__(self, data=None):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Transaction:
    def set(self, ref, data, **kwargs):
        if kwargs.get('merge') and ref.data:
            ref.data = {**ref.data, **data}
        else:
            ref.data = dict(data)


def test_task_sync_receipt_reclaim_and_completion_are_exact_claim_cas():
    now = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
    receipt_ref = _ReceiptRef()
    transaction = _Transaction()
    identity = ('stable-operation', 'action-item-a', 'google_tasks')

    assert task_sync_db._claim_task_sync_transaction(
        transaction,
        receipt_ref,
        *identity,
        'claim-a',
        now,
        15,
    ) == {'outcome': 'claimed'}
    assert task_sync_db._claim_task_sync_transaction(
        transaction,
        receipt_ref,
        *identity,
        'claim-b',
        now + timedelta(seconds=14),
        15,
    ) == {'outcome': 'busy'}
    assert task_sync_db._claim_task_sync_transaction(
        transaction,
        receipt_ref,
        *identity,
        'claim-b',
        now + timedelta(seconds=16),
        15,
    ) == {'outcome': 'claimed'}

    result = {'synced': True, 'platform': 'google_tasks', 'external_task_id': 'external-a'}
    assert task_sync_db._complete_task_sync_transaction(
        transaction,
        receipt_ref,
        *identity,
        'claim-b',
        result,
        now + timedelta(seconds=17),
    ) == {'outcome': 'completed', 'result': result}
    assert task_sync_db._complete_task_sync_transaction(
        transaction,
        receipt_ref,
        *identity,
        'claim-a',
        {'synced': True, 'external_task_id': 'duplicate'},
        now + timedelta(seconds=18),
    ) == {'outcome': 'completed', 'result': result}
    assert receipt_ref.data['result'] == result


class _Response:
    status_code = 201

    def __init__(self, app_key):
        self.app_key = app_key

    def json(self):
        if self.app_key == 'asana':
            return {'data': {'gid': 'external-asana'}}
        return {'id': f'external-{self.app_key}'}


class _HttpClient:
    def __init__(self, app_key, requests):
        self.app_key = app_key
        self.requests = requests

    async def post(self, url, **kwargs):
        self.requests.append({'url': url, **kwargs})
        return _Response(self.app_key)


def _integration(app_key):
    integration = {
        'connected': True,
        'access_token': 'test-access-token',
        'expires_at': '2099-01-01T00:00:00+00:00',
    }
    if app_key == 'asana':
        integration['workspace_gid'] = 'workspace-a'
    if app_key == 'google_tasks':
        integration['default_list_id'] = 'task-list-a'
    if app_key == 'clickup':
        integration['list_id'] = 'clickup-list-a'
    return integration


@pytest.mark.parametrize('app_key', ['todoist', 'asana', 'google_tasks', 'clickup', 'apple_reminders'])
def test_real_auto_sync_batch_old_worker_after_successor_creates_one_external_item(monkeypatch, app_key):
    state = {'claim_calls': 0, 'live_claim': None, 'completed': False, 'result': None}
    state_lock = threading.Lock()
    predecessor_claimed = threading.Event()
    successor_completed = threading.Event()
    external_requests = []
    apple_pushes = []
    exported_updates = []

    monkeypatch.setattr(task_sync.users_db, 'get_default_task_integration', lambda uid: app_key)
    monkeypatch.setattr(task_sync.users_db, 'get_task_integration', lambda uid, platform: _integration(app_key))
    monkeypatch.setattr(
        task_sync.action_items_db,
        'update_action_item',
        lambda *args, **kwargs: exported_updates.append((args, kwargs)),
    )
    monkeypatch.setattr(
        task_integrations,
        'get_http_client',
        lambda: _HttpClient(app_key, external_requests),
    )
    monkeypatch.setattr(
        task_sync,
        'send_apple_reminders_sync_push',
        lambda **kwargs: apple_pushes.append(kwargs) or True,
    )

    def claim_task_sync(uid, operation_key, action_item_id, platform, claim_token):
        with state_lock:
            state['claim_calls'] += 1
            is_predecessor = state['claim_calls'] == 1
            state['live_claim'] = claim_token
        if is_predecessor:
            predecessor_claimed.set()
            assert successor_completed.wait(timeout=2)
        return {'outcome': 'claimed'}

    def observe_task_sync_claim(uid, operation_key, action_item_id, platform, claim_token):
        with state_lock:
            if state['completed']:
                return {'outcome': 'completed', 'result': state['result']}
            if state['live_claim'] == claim_token:
                return {'outcome': 'claimed'}
        return {'outcome': 'lost'}

    def complete_task_sync(uid, operation_key, action_item_id, platform, claim_token, result):
        with state_lock:
            if state['completed']:
                return {'outcome': 'completed', 'result': state['result']}
            if state['live_claim'] != claim_token:
                return {'outcome': 'lost'}
            state['completed'] = True
            state['result'] = result
            return {'outcome': 'completed', 'result': result}

    monkeypatch.setattr(task_sync_db, 'claim_task_sync', claim_task_sync)
    monkeypatch.setattr(task_sync_db, 'observe_task_sync_claim', observe_task_sync_claim)
    monkeypatch.setattr(task_sync_db, 'complete_task_sync', complete_task_sync)

    action_items = [{'id': 'action-item-a', 'description': 'Create exactly once'}]
    finalization_operation = 'stable-action-items-auto-sync-operation'
    predecessor_result = []

    def run_predecessor():
        predecessor_result.extend(
            asyncio.run(
                task_sync.auto_sync_action_items_batch(
                    'authenticated-user',
                    action_items,
                    idempotency_key=finalization_operation,
                )
            )
        )

    predecessor = threading.Thread(target=run_predecessor)
    predecessor.start()
    assert predecessor_claimed.wait(timeout=2)

    successor_result = asyncio.run(
        task_sync.auto_sync_action_items_batch(
            'authenticated-user',
            action_items,
            idempotency_key=finalization_operation,
        )
    )
    successor_completed.set()
    predecessor.join(timeout=2)

    assert not predecessor.is_alive()
    assert predecessor_result == successor_result
    assert successor_result[0]['synced'] is True
    assert len(apple_pushes if app_key == 'apple_reminders' else external_requests) == 1
    if app_key == 'apple_reminders':
        expected_key = task_sync_db.task_sync_operation_key(
            'authenticated-user', finalization_operation, 'action-item-a'
        )
        assert apple_pushes[0]['idempotency_key'] == expected_key
    else:
        assert len(exported_updates) >= 1
    if app_key == 'todoist':
        expected_key = task_sync_db.task_sync_operation_key(
            'authenticated-user', finalization_operation, 'action-item-a'
        )
        assert external_requests[0]['headers']['X-Request-Id'] == task_sync_db.provider_request_id(expected_key)
