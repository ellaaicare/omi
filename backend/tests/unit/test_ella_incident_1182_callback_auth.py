import sys
from unittest.mock import MagicMock

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

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        callbacks, "get_contacts", lambda uid: [] if uid == "uid-a" else (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(callbacks, "send_notification", lambda **kwargs: kwargs["user_id"] == "uid-a")
    monkeypatch.setattr(callbacks.httpx, "AsyncClient", lambda **_kwargs: Client())
    client = _client(monkeypatch)
    response = client.get(
        "/v1/ella/emergency-contacts/uid-a",
        headers={"Authorization": "Bearer token-a"},
    )
    assert response.status_code == 200
    assert response.json() == []

    emergency = client.post(
        "/v1/ella/emergency",
        headers={"Authorization": "Bearer token-a"},
        json={"uid": "uid-a", "trigger_source": "manual_button", "audio_context_seconds": 0},
    )
    assert emergency.status_code == 200
    assert emergency.json()["push_sent"] is True


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
