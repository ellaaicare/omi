import importlib.util
from pathlib import Path
import sys
import types

import pytest
import requests


def _load_webhooks(monkeypatch):
    redis_stub = types.ModuleType('database.redis_db')
    redis_stub.get_user_webhook_db = lambda *_args: 'https://example.invalid/conversation'
    redis_stub.user_webhook_status_db = lambda *_args: True
    redis_stub.disable_user_webhook_db = lambda *_args: None
    redis_stub.enable_user_webhook_db = lambda *_args: None
    redis_stub.set_user_webhook_db = lambda *_args: None
    monkeypatch.setitem(sys.modules, 'database.redis_db', redis_stub)

    notifications_db_stub = types.ModuleType('database.notifications')
    monkeypatch.setitem(sys.modules, 'database.notifications', notifications_db_stub)

    users_db_stub = types.ModuleType('database.users')
    users_db_stub.get_user_profile = lambda *_args: {'name': 'Synthetic user'}
    users_db_stub.get_people_by_ids = lambda *_args: []
    monkeypatch.setitem(sys.modules, 'database.users', users_db_stub)

    notifications_stub = types.ModuleType('utils.notifications')
    notifications_stub.send_notification = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, 'utils.notifications', notifications_stub)

    module_path = Path(__file__).parents[2] / 'utils' / 'webhooks.py'
    spec = importlib.util.spec_from_file_location('capture_webhook_delivery_module', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Conversation:
    def as_dict_cleaned_dates(self):
        return {'id': 'conversation-a', 'transcript_segments': []}


class _Response:
    status_code = 503

    def raise_for_status(self):
        raise requests.HTTPError('HTTP 503')


def test_capture_developer_webhook_failure_is_not_acknowledged(monkeypatch):
    webhooks = _load_webhooks(monkeypatch)
    monkeypatch.setattr(webhooks.requests, 'post', lambda *_args, **_kwargs: _Response())

    with pytest.raises(requests.HTTPError):
        webhooks.conversation_created_webhook(
            'uid-a',
            _Conversation(),
            idempotency_key='capture-operation-a',
        )


def test_legacy_developer_webhook_failure_remains_best_effort(monkeypatch):
    webhooks = _load_webhooks(monkeypatch)
    monkeypatch.setattr(webhooks.requests, 'post', lambda *_args, **_kwargs: _Response())

    webhooks.conversation_created_webhook('uid-a', _Conversation())
