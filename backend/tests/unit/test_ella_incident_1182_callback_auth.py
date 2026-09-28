import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault("database._client", MagicMock())
sys.modules.setdefault("database.conversations", MagicMock())
sys.modules.setdefault("database.memories", MagicMock())
sys.modules.setdefault("database.users", MagicMock())
sys.modules.setdefault("database.ella_contacts", MagicMock())
sys.modules.setdefault("utils.notifications", MagicMock())
sys.modules.setdefault("utils.other.storage", MagicMock())

from ella.routers import callbacks
from utils.ella import exact_firebase_auth


class _CaregiverPool:
    def __init__(self, rows):
        self.rows = rows
        self.fetches = []

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return self.rows


def _server_caregiver(**overrides):
    caregiver = {
        "id": "caregiver-selected",
        "owner_uid": "uid-a",
        "name": "Selected caregiver",
        "relationship": "family",
        "status": "ACTIVE",
        "is_emergency_contact": True,
        "phone": "+15555550105",
        "email": "caregiver@example.test",
        "permissions": {"receive_emergency_alerts": True},
    }
    caregiver.update(overrides)
    return caregiver


def _set_caregiver_pool(monkeypatch, rows):
    pool = _CaregiverPool(rows)

    async def get_pool():
        return pool

    monkeypatch.setattr(callbacks, "get_ella_postgres_pool", get_pool)
    monkeypatch.setattr(callbacks, "get_contacts", lambda _uid: [])
    return pool


def _verify_firebase(token):
    if token == "token-a":
        return {"uid": "uid-a"}
    if token == "token-b":
        return {"uid": "uid-b"}
    raise ValueError("invalid token")


def _client(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _verify_firebase)
    app = FastAPI()
    app.include_router(callbacks.router)
    return TestClient(app)


def _contact_body(uid):
    return {
        "uid": uid,
        "name": "Contact",
        "phone": "+15555550100",
        "relationship": "friend",
    }


def test_emergency_contacts_use_configured_shared_ella_postgres_pool(monkeypatch):
    pool = _set_caregiver_pool(monkeypatch, [_server_caregiver()])
    monkeypatch.setattr(
        callbacks,
        "_get_resolve_pool",
        lambda: (_ for _ in ()).throw(AssertionError("legacy hard-coded pool must not be used")),
    )

    contacts = asyncio.run(callbacks._server_owned_emergency_contacts("uid-a"))

    assert len(contacts) == 1
    assert pool.fetches[0][1] == ("uid-a",)


def test_emergency_delivery_uses_owner_scoped_onboarding_contact_store(monkeypatch):
    _set_caregiver_pool(monkeypatch, [])
    monkeypatch.setattr(
        callbacks,
        "get_contacts",
        lambda uid: [
            {
                "uid": uid,
                "name": "Onboarding contact",
                "phone": "+15555550106",
                "email": "onboarding@example.test",
                "relationship": "family",
                "permissions": {"emergency_contact": True},
            },
            {
                "uid": "uid-b",
                "name": "Other owner",
                "phone": "+15555550107",
                "permissions": {"emergency_contact": True},
            },
            {
                "uid": uid,
                "name": "Not an emergency contact",
                "phone": "+15555550108",
                "permissions": {"emergency_contact": False},
            },
        ],
    )

    assert asyncio.run(callbacks._server_owned_emergency_contacts("uid-a")) == [
        {
            "name": "Onboarding contact",
            "phone": "+15555550106",
            "email": "onboarding@example.test",
            "relationship": "family",
        }
    ]


def test_emergency_contact_crud_rejects_unauthenticated_and_cross_owner_before_storage(monkeypatch):
    effects = []
    monkeypatch.setattr(callbacks, "create_contact", lambda *_args, **_kwargs: effects.append("create"))
    monkeypatch.setattr(callbacks, "get_contacts", lambda *_args, **_kwargs: effects.append("list"))
    monkeypatch.setattr(callbacks, "get_contact", lambda *_args, **_kwargs: effects.append("get"))
    monkeypatch.setattr(callbacks, "update_contact", lambda *_args, **_kwargs: effects.append("update"))
    monkeypatch.setattr(callbacks, "delete_contact", lambda *_args, **_kwargs: effects.append("delete"))
    monkeypatch.setattr(callbacks, "send_notification", lambda *_args, **_kwargs: effects.append("notify"))
    client = _client(monkeypatch)

    requests = [
        ("post", "/v1/ella/emergency-contact", {"json": _contact_body("uid-b")}),
        ("get", "/v1/ella/emergency-contacts/uid-b", {}),
        ("put", "/v1/ella/emergency-contact/contact-a?uid=uid-b", {"json": {"name": "Updated"}}),
        ("delete", "/v1/ella/emergency-contact/contact-a?uid=uid-b", {}),
        (
            "post",
            "/v1/ella/emergency",
            {
                "json": {
                    "uid": "uid-b",
                    "trigger_source": "manual_button",
                    "audio_context_seconds": 0,
                }
            },
        ),
    ]
    for method, path, kwargs in requests:
        assert getattr(client, method)(path, **kwargs).status_code == 401
        assert (
            getattr(client, method)(
                path,
                headers={"Authorization": "Bearer token-a"},
                **kwargs,
            ).status_code
            == 403
        )
    assert effects == []


def test_emergency_contact_exact_owner_positive_control(monkeypatch):
    outbound = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"sms_available": False, "contacts_notified": []}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *args, **kwargs):
            outbound.append((args, kwargs))
            return Response()

    server_contact = _server_caregiver(name="Server contact", phone="+15555550100", email=None, relationship="friend")
    pool = _set_caregiver_pool(monkeypatch, [server_contact])
    monkeypatch.setattr(
        callbacks,
        "get_contacts",
        lambda uid: [{**server_contact, "uid": "uid-a"}] if uid == "uid-a" else (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(callbacks, "EMERGENCY_WEBHOOK_KEY", "configured-emergency-webhook-key")
    monkeypatch.setattr(callbacks, "send_notification", lambda **kwargs: kwargs["user_id"] == "uid-a")
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", lambda **_kwargs: Client())
    client = _client(monkeypatch)
    response = client.get(
        "/v1/ella/emergency-contacts/uid-a",
        headers={"Authorization": "Bearer token-a"},
    )
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Server contact"

    emergency = client.post(
        "/v1/ella/emergency",
        headers={"Authorization": "Bearer token-a"},
        json={
            "uid": "uid-a",
            "contacts": [{"name": "Caller supplied", "phone": "+15555550999"}],
            "audio_context_url": "https://caller.invalid/audio.mp3",
        },
    )
    assert emergency.status_code == 200
    assert emergency.json()["push_sent"] is True
    assert emergency.json()["status"] == "partial"
    assert emergency.json()["error"] == "emergency_delivery_unconfirmed"
    assert len(outbound) == 1
    assert outbound[0][1]["headers"] == {
        "Content-Type": "application/json",
        callbacks.EMERGENCY_WEBHOOK_KEY_HEADER: "configured-emergency-webhook-key",
    }
    assert outbound[0][1]["json"]["contacts"] == [
        {
            "name": "Server contact",
            "phone": "+15555550100",
            "email": None,
            "relationship": "friend",
        }
    ]
    assert "audio_context_url" not in outbound[0][1]["json"]
    caregiver_query, caregiver_args = pool.fetches[0]
    assert "JOIN caregivers c ON c.user_id = u.id" in caregiver_query
    assert "WHERE u.omi_uid = $1" in caregiver_query
    assert caregiver_args == ("uid-a",)


def test_onboarding_contact_dispatch_is_owner_bound_and_ignores_proxy_environment(monkeypatch):
    stored_contacts = {}
    target_requests = []
    proxy_requests = []

    def create_stored_contact(uid, data):
        contact = {**data, "id": "contact-created", "uid": uid}
        stored_contacts.setdefault(uid, []).append(contact)
        return contact

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            target_requests.append(
                {
                    "path": self.path,
                    "authority": self.headers.get(callbacks.EMERGENCY_WEBHOOK_KEY_HEADER),
                    "payload": json.loads(body),
                }
            )
            response = json.dumps(
                {
                    "sms_available": True,
                    "contacts_notified": [{"name": "Onboarding contact", "method": "sms", "status": "delivered"}],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            return None

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            proxy_requests.append(self.path)
            self.send_response(502)
            self.end_headers()

        def log_message(self, *_args):
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    target_thread.start()
    proxy_thread.start()

    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    _set_caregiver_pool(monkeypatch, [])
    monkeypatch.setattr(callbacks, "create_contact", create_stored_contact)
    monkeypatch.setattr(callbacks, "get_contacts", lambda uid: list(stored_contacts.get(uid, [])))
    monkeypatch.setattr(callbacks, "EMERGENCY_WEBHOOK_KEY", "configured-emergency-webhook-key")
    monkeypatch.setattr(callbacks, "send_notification", lambda **_kwargs: None)
    monkeypatch.setattr(callbacks.ELLA_CONFIG, "n8n_base_url", f"http://127.0.0.1:{target.server_port}")
    monkeypatch.setattr(callbacks.ELLA_CONFIG, "emergency_endpoint", "/emergency")

    client = _client(monkeypatch)
    try:
        created = client.post(
            "/v1/ella/emergency-contact",
            headers={"Authorization": "Bearer token-a"},
            json={
                "uid": "uid-a",
                "name": "Onboarding contact",
                "phone": "+15555550106",
                "relationship": "family",
            },
        )
        dispatched = client.post(
            "/v1/ella/emergency",
            headers={"Authorization": "Bearer token-a"},
            json={
                "uid": "uid-a",
                "contacts": [{"name": "Caller supplied", "phone": "+15555550999"}],
            },
        )
    finally:
        target.shutdown()
        proxy.shutdown()
        target.server_close()
        proxy.server_close()
        target_thread.join(timeout=2.0)
        proxy_thread.join(timeout=2.0)

    assert created.status_code == 201
    assert dispatched.status_code == 200
    assert dispatched.json()["status"] == "success"
    assert proxy_requests == []
    assert len(target_requests) == 1
    assert target_requests[0]["path"] == "/emergency"
    assert target_requests[0]["authority"] == "configured-emergency-webhook-key"
    assert target_requests[0]["payload"]["uid"] == "uid-a"
    assert target_requests[0]["payload"]["contacts"] == [
        {
            "name": "Onboarding contact",
            "phone": "+15555550106",
            "email": None,
            "relationship": "family",
        }
    ]


def test_emergency_webhook_fails_closed_without_authority(monkeypatch):
    effects = []
    _set_caregiver_pool(monkeypatch, [_server_caregiver(name="Server contact", phone="+15555550100")])
    monkeypatch.setattr(callbacks, "EMERGENCY_WEBHOOK_KEY", "")
    monkeypatch.setattr(callbacks, "send_notification", lambda **kwargs: effects.append(("notify", kwargs)))
    monkeypatch.setattr(
        callbacks.httpx,
        "AsyncClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("missing authority must fail before egress")),
    )
    client = _client(monkeypatch)

    response = client.post(
        "/v1/ella/emergency",
        headers={"Authorization": "Bearer token-a"},
        json={"uid": "uid-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["contacts_notified"] == []
    assert response.json()["error"] == "emergency_webhook_authority_unavailable"
    assert effects[0][1]["body"] == "Your emergency request was received."


@pytest.mark.parametrize(
    ("delivery_statuses", "expected_status", "expected_error"),
    [
        (["failed", "error"], "partial", "emergency_delivery_unconfirmed"),
        (["failed", "queued"], "pending", "emergency_delivery_pending"),
        (["failed", "delivered"], "success", None),
    ],
)
def test_emergency_status_requires_confirmed_caregiver_delivery(
    monkeypatch,
    delivery_statuses,
    expected_status,
    expected_error,
):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "sms_available": True,
                "contacts_notified": [
                    {
                        "name": f"Caregiver {index}",
                        "method": "sms",
                        "status": status,
                    }
                    for index, status in enumerate(delivery_statuses)
                ],
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    _set_caregiver_pool(monkeypatch, [_server_caregiver()])
    monkeypatch.setattr(callbacks, "EMERGENCY_WEBHOOK_KEY", "configured-emergency-webhook-key")
    monkeypatch.setattr(callbacks, "send_notification", lambda **_kwargs: None)
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", lambda **_kwargs: Client())

    response = _client(monkeypatch).post(
        "/v1/ella/emergency",
        headers={"Authorization": "Bearer token-a"},
        json={"uid": "uid-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert response.json()["error"] == expected_error
    assert [contact["status"] for contact in response.json()["contacts_notified"]] == delivery_statuses


@pytest.mark.parametrize(
    "caregiver",
    [
        {
            "id": "caregiver-cleared",
            "owner_uid": "uid-a",
            "status": "active",
            "is_emergency_contact": False,
            "phone": "+15555550101",
            "permissions": {"receive_emergency_alerts": True},
        },
        {
            "id": "caregiver-inactive",
            "owner_uid": "uid-a",
            "status": "invited",
            "is_emergency_contact": True,
            "phone": "+15555550102",
            "permissions": {"receive_emergency_alerts": True},
        },
        {
            "id": "caregiver-denied",
            "owner_uid": "uid-a",
            "status": "active",
            "is_emergency_contact": True,
            "phone": "+15555550103",
            "permissions": {"receive_emergency_alerts": False},
        },
        {
            "id": "caregiver-cross-owner",
            "owner_uid": "uid-b",
            "status": "active",
            "is_emergency_contact": True,
            "phone": "+15555550104",
            "permissions": {"receive_emergency_alerts": True},
        },
    ],
)
def test_emergency_delivery_excludes_cleared_inactive_denied_and_cross_owner_caregivers(monkeypatch, caregiver):
    _set_caregiver_pool(monkeypatch, [caregiver])

    assert asyncio.run(callbacks._server_owned_emergency_contacts("uid-a")) == []


def test_emergency_delivery_uses_selected_active_owner_caregiver(monkeypatch):
    _set_caregiver_pool(monkeypatch, [_server_caregiver()])

    assert asyncio.run(callbacks._server_owned_emergency_contacts("uid-a")) == [
        {
            "name": "Selected caregiver",
            "phone": "+15555550105",
            "email": "caregiver@example.test",
            "relationship": "family",
        }
    ]


def test_emergency_owner_receipt_precedes_async_caregiver_lookup(monkeypatch):
    effects = []

    monkeypatch.setattr(
        callbacks,
        "send_notification",
        lambda **_kwargs: effects.append("owner_receipt"),
    )

    async def load_contacts(_uid):
        effects.append("caregiver_lookup")
        return []

    monkeypatch.setattr(callbacks, "_server_owned_emergency_contacts", load_contacts)
    monkeypatch.setattr(callbacks, "EMERGENCY_WEBHOOK_KEY", "configured-emergency-webhook-key")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/ella/emergency",
        headers={"Authorization": "Bearer token-a"},
        json={"uid": "uid-a"},
    )

    assert response.status_code == 200
    assert effects == ["owner_receipt", "caregiver_lookup"]


def test_first_party_caregiver_routes_derive_owner_and_reject_caller_uid(monkeypatch):
    effects = []

    monkeypatch.setattr(
        callbacks,
        "get_caregivers",
        lambda uid: effects.append(("list", uid)) or [{"id": "caregiver-a", "name": "Caregiver"}],
    )
    monkeypatch.setattr(
        callbacks,
        "create_caregiver",
        lambda uid, data: effects.append(("invite", uid))
        or {
            **data,
            "id": "caregiver-a",
            "invite_code": "123456",
            "status": "invited",
            "invite_expires_at": "2026-08-11T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        callbacks,
        "get_emergency_caregiver_id",
        lambda uid: effects.append(("get-emergency", uid)) or "caregiver-a",
    )
    monkeypatch.setattr(
        callbacks,
        "set_emergency_caregiver",
        lambda uid, caregiver_id: effects.append(("set-emergency", uid)) or caregiver_id,
    )
    monkeypatch.setattr(
        callbacks,
        "update_caregiver",
        lambda uid, _caregiver_id, data: effects.append(("permissions", uid, data))
        or {
            "id": "caregiver-a",
            "permissions": {
                "receive_emergency_alerts": False,
                "receive_daily_summary": data["permissions.receive_daily_summary"],
                "daily_summary_email": data["permissions.daily_summary_email"],
            },
        },
    )
    monkeypatch.setattr(
        callbacks,
        "refresh_caregiver_invite",
        lambda uid, _caregiver_id: effects.append(("resend", uid))
        or {
            "id": "caregiver-a",
            "invite_code": "654321",
            "status": "invited",
            "invite_expires_at": "2026-08-11T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        callbacks,
        "delete_caregiver",
        lambda uid, _caregiver_id: effects.append(("delete", uid)) or True,
    )
    client = _client(monkeypatch)
    headers = {"Authorization": "Bearer token-a"}

    responses = [
        client.get("/v1/ella/caregivers", headers=headers),
        client.post(
            "/v1/ella/caregivers/invite",
            headers=headers,
            json={
                "name": "Caregiver",
                "email": "caregiver@example.test",
                "relationship": "friend",
                "permissions": {"receive_daily_summary": True, "daily_summary_email": True},
            },
        ),
        client.get("/v1/ella/caregivers/emergency-contact", headers=headers),
        client.put(
            "/v1/ella/caregivers/emergency-contact",
            headers=headers,
            json={"caregiver_id": "caregiver-a"},
        ),
        client.put(
            "/v1/ella/caregivers/caregiver-a/permissions",
            headers=headers,
            json={"receive_daily_summary": True, "daily_summary_email": True},
        ),
        client.post("/v1/ella/caregivers/caregiver-a/resend-invite", headers=headers),
        client.delete("/v1/ella/caregivers/caregiver-a", headers=headers),
    ]

    assert [response.status_code for response in responses] == [200, 201, 200, 200, 200, 200, 204]
    assert responses[4].json()["permissions"]["receive_emergency_alerts"] is False
    assert effects == [
        ("list", "uid-a"),
        ("invite", "uid-a"),
        ("get-emergency", "uid-a"),
        ("set-emergency", "uid-a"),
        (
            "permissions",
            "uid-a",
            {
                "permissions.receive_daily_summary": True,
                "permissions.daily_summary_email": True,
            },
        ),
        ("resend", "uid-a"),
        ("delete", "uid-a"),
    ]

    effects.clear()
    caller_uid = client.post(
        "/v1/ella/caregivers/invite",
        headers={"Authorization": "Bearer token-b"},
        json={
            "uid": "uid-a",
            "name": "Caregiver",
            "email": "caregiver@example.test",
            "relationship": "friend",
        },
    )
    assert caller_uid.status_code == 422
    assert effects == []


def test_caregiver_routes_reject_unauth_admin_and_service_before_storage(monkeypatch):
    effects = []
    monkeypatch.setattr(callbacks, "get_caregivers", lambda uid: effects.append(uid) or [])
    monkeypatch.setenv("ADMIN_KEY", "unit-admin-key:")
    monkeypatch.setenv("ELLA_ADMIN_SUBJECT_ALLOWLIST", "uid-a")
    client = _client(monkeypatch)

    for headers in (
        {},
        {"Authorization": "Bearer unit-admin-key:uid-a"},
        {"X-Ella-Caregiver-Service-Key": "caregiver-service-test", "X-Ella-Subject-Uid": "uid-a"},
    ):
        assert client.get("/v1/ella/caregivers", headers=headers).status_code == 401
    assert effects == []


def test_internal_callback_service_fails_closed_and_positive_control_is_scoped(monkeypatch):
    effects = []

    def fetch(*_args, **_kwargs):
        effects.append("db")
        return []

    monkeypatch.setattr(callbacks.conversations_db, "get_conversations", fetch)
    monkeypatch.setattr(callbacks.conversations_db, "get_conversations_without_photos", fetch)
    client = _client(monkeypatch)

    monkeypatch.delenv("ELLA_CALLBACK_SERVICE_KEY", raising=False)
    missing_config = client.get("/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a")
    assert missing_config.status_code == 503
    assert effects == []

    monkeypatch.setenv("ELLA_CALLBACK_SERVICE_KEY", "callback-service-test")
    wrong = client.get(
        "/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a",
        headers={"X-Ella-Callback-Service-Key": "wrong", "X-Ella-Subject-Uid": "uid-a"},
    )
    assert wrong.status_code == 403
    assert effects == []

    unbound = client.get(
        "/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a",
        headers={"X-Ella-Callback-Service-Key": "callback-service-test"},
    )
    assert unbound.status_code == 403
    assert effects == []

    cross_owner = client.get(
        "/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a",
        headers={
            "X-Ella-Callback-Service-Key": "callback-service-test",
            "X-Ella-Subject-Uid": "uid-b",
        },
    )
    assert cross_owner.status_code == 403
    assert effects == []

    accepted = client.get(
        "/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a",
        headers={
            "X-Ella-Callback-Service-Key": "callback-service-test",
            "X-Ella-Subject-Uid": "uid-a",
        },
    )
    assert accepted.status_code == 200
    assert effects == ["db"]


def test_callback_routes_reject_unbound_wrong_and_nonservice_authority_before_effects(monkeypatch):
    effects = []
    monkeypatch.setenv("ELLA_CALLBACK_SERVICE_KEY", "callback-service-test")
    monkeypatch.setattr(callbacks, "assert_current_ai_consent", lambda uid: effects.append(("consent", uid)))
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: effects.append(("read", uid, conversation_id)) or {},
    )
    monkeypatch.setattr(callbacks, "send_notification", lambda **kwargs: effects.append(("push", kwargs["user_id"])))
    client = _client(monkeypatch)

    requests = (
        ("get", "/v1/ella/conversation/conversation-a/data?uid=uid-a", {}),
        (
            "post",
            "/v1/ella/notification",
            {"json": {"uid": "uid-a", "message": "Test", "generate_audio": False}},
        ),
        ("post", "/v1/ella/daily-summary", {"json": {"uid": "uid-a"}}),
    )
    denied_headers = (
        {},
        {"Authorization": "Bearer token-a"},
        {"X-Ella-Callback-Service-Key": "wrong", "X-Ella-Subject-Uid": "uid-a"},
        {"X-Ella-Callback-Service-Key": "callback-service-test"},
        {"X-Ella-Callback-Service-Key": "callback-service-test", "X-Ella-Subject-Uid": "uid-b"},
    )
    for method, path, kwargs in requests:
        for headers in denied_headers:
            assert getattr(client, method)(path, headers=headers, **kwargs).status_code == 403
    assert effects == []


def test_callback_routes_accept_only_matching_bound_subject(monkeypatch):
    effects = []

    class Response:
        status_code = 200
        text = "ok"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            effects.append(("daily-summary", "uid-a"))
            return Response()

    monkeypatch.setenv("ELLA_CALLBACK_SERVICE_KEY", "callback-service-test")
    monkeypatch.setattr(callbacks, "assert_current_ai_consent", lambda uid: effects.append(("consent", uid)))
    monkeypatch.setattr(
        callbacks.conversations_db,
        "get_conversation",
        lambda uid, conversation_id: effects.append(("read", uid, conversation_id)) or {},
    )
    monkeypatch.setattr(callbacks, "send_notification", lambda **kwargs: effects.append(("push", kwargs["user_id"])))
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", lambda **_kwargs: Client())
    client = _client(monkeypatch)
    headers = {
        "X-Ella-Callback-Service-Key": "callback-service-test",
        "X-Ella-Subject-Uid": "uid-a",
    }

    assert (
        client.get(
            "/v1/ella/conversation/conversation-a/data?uid=uid-a",
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/ella/notification",
            headers=headers,
            json={"uid": "uid-a", "message": "Test", "generate_audio": False},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/ella/daily-summary",
            headers=headers,
            json={"uid": "uid-a"},
        ).status_code
        == 200
    )
    assert effects == [
        ("read", "uid-a", "conversation-a"),
        ("consent", "uid-a"),
        ("push", "uid-a"),
        ("daily-summary", "uid-a"),
    ]


def test_caregiver_token_generation_requires_two_distinct_configured_secrets(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("ELLA_DASHBOARD_SECRET", "dashboard-signing-test")
    monkeypatch.delenv("ELLA_CAREGIVER_SERVICE_KEY", raising=False)
    assert client.post("/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a").status_code == 503

    monkeypatch.setenv("ELLA_CAREGIVER_SERVICE_KEY", "caregiver-service-test")
    assert (
        client.post(
            "/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a",
            headers={"X-Ella-Caregiver-Service-Key": "wrong", "X-Ella-Subject-Uid": "uid-a"},
        ).status_code
        == 403
    )
    accepted = client.post(
        "/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a",
        headers={
            "X-Ella-Caregiver-Service-Key": "caregiver-service-test",
            "X-Ella-Subject-Uid": "uid-a",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["expires_in_hours"] == 24


def test_dashboard_signing_has_no_source_fallback(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.delenv("ELLA_DASHBOARD_SECRET", raising=False)
    response = client.get("/v1/ella/caregiver-dashboard-data?token=invalid")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "caregiver_dashboard_auth_not_configured"
