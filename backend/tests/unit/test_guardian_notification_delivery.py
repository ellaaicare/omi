import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))


class _ValueObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _install_import_stubs():
    messaging = types.ModuleType('firebase_admin.messaging')
    for name in (
        'AndroidConfig',
        'AndroidNotification',
        'APNSConfig',
        'APNSPayload',
        'Aps',
        'WebpushConfig',
        'WebpushNotification',
        'WebpushFCMOptions',
        'Message',
        'Notification',
    ):
        setattr(messaging, name, type(name, (_ValueObject,), {}))
    messaging.send_each = lambda messages: None

    firebase_admin = types.ModuleType('firebase_admin')
    firebase_admin.messaging = messaging
    firebase_admin.auth = types.SimpleNamespace()
    sys.modules['firebase_admin'] = firebase_admin
    sys.modules['firebase_admin.messaging'] = messaging

    database = types.ModuleType('database')
    database.__path__ = []
    sys.modules['database'] = database

    notification_db = types.ModuleType('database.notifications')
    notification_db.get_all_tokens = lambda _user_id: []
    notification_db.remove_bulk_tokens = lambda _tokens: None
    sys.modules['database.notifications'] = notification_db
    database.notifications = notification_db

    redis_db = types.ModuleType('database.redis_db')
    redis_db.set_credit_limit_notification_sent = lambda *_args: None
    redis_db.has_credit_limit_notification_been_sent = lambda *_args: False
    redis_db.set_silent_user_notification_sent = lambda *_args: None
    redis_db.has_silent_user_notification_been_sent = lambda *_args: False
    sys.modules['database.redis_db'] = redis_db

    database_auth = types.ModuleType('database.auth')
    database_auth.get_user_from_uid = lambda *_args: None
    sys.modules['database.auth'] = database_auth

    llm_notifications = types.ModuleType('utils.llm.notifications')
    llm_notifications.generate_notification_message = lambda *_args: ('title', 'body')
    llm_notifications.generate_credit_limit_notification = lambda *_args: ('title', 'body')
    llm_notifications.generate_silent_user_notification = lambda *_args: ('title', 'body')
    sys.modules['utils.llm.notifications'] = llm_notifications


_install_import_stubs()
from utils import notifications  # noqa: E402


class GuardianNotificationDeliveryTests(unittest.TestCase):
    def test_production_ella_callbacks_use_guardian_boundary(self):
        callbacks = ast.parse((BACKEND_ROOT / 'ella/routers/callbacks.py').read_text())
        functions = {
            node.name: node for node in callbacks.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in ('ella_notification', 'ella_emergency'):
            calls = {
                node.func.id
                for node in ast.walk(functions[function_name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertIn('send_guardian_notification', calls)
            self.assertNotIn('send_notification', calls)

    def test_guardian_helper_uses_data_only_background_delivery(self):
        with patch.object(notifications, '_send_to_user', return_value=1) as send:
            result = notifications.send_guardian_notification(
                'user-1',
                'Ella',
                'Check in',
                {'type': 'ella_notification', 'urgency': 'NORMAL'},
            )

        self.assertEqual(result, 1)
        args, kwargs = send.call_args
        self.assertEqual(args[0], 'user-1')
        self.assertNotIn('notification', kwargs)
        self.assertTrue(kwargs['is_background'])
        self.assertEqual(kwargs['priority'], 'high')
        self.assertEqual(kwargs['apns_topic'], 'com.ellaaicare.ella')
        self.assertEqual(kwargs['data']['type'], 'ella_notification')
        self.assertEqual(kwargs['data']['title'], 'Ella')
        self.assertEqual(kwargs['data']['body'], 'Check in')

    def test_apns_message_has_no_alert_boundary_for_type_only_payload(self):
        message = notifications._build_message(
            token='token-1',
            tag='guardian-tag',
            notification=None,
            data={'type': 'ella_notification', 'urgency': 'NORMAL'},
            is_background=True,
            priority='high',
            apns_topic='com.ellaaicare.ella',
        )

        self.assertIsNone(message.notification)
        self.assertEqual(message.data['type'], 'ella_notification')
        self.assertEqual(message.apns.headers['apns-push-type'], 'background')
        self.assertEqual(message.apns.headers['apns-priority'], '5')
        self.assertEqual(message.apns.headers['apns-topic'], 'com.ellaaicare.ella')
        self.assertTrue(message.apns.payload.aps.content_available)
        self.assertFalse(hasattr(message.android, 'notification'))


if __name__ == '__main__':
    unittest.main()
