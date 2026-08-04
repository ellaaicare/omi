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
        headers={"X-Ella-Callback-Service-Key": "wrong"},
    )
    assert wrong.status_code == 403
    assert effects == []

    accepted = client.get(
        "/v1/ella/conversations/enrichment/reconcile-candidates?uid=uid-a",
        headers={"X-Ella-Callback-Service-Key": "callback-service-test"},
    )
    assert accepted.status_code == 200
    assert effects == ["db"]


def test_caregiver_token_generation_requires_two_distinct_configured_secrets(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("ELLA_DASHBOARD_SECRET", "dashboard-signing-test")
    monkeypatch.delenv("ELLA_CAREGIVER_SERVICE_KEY", raising=False)
    assert client.post("/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a").status_code == 503

    monkeypatch.setenv("ELLA_CAREGIVER_SERVICE_KEY", "caregiver-service-test")
    assert (
        client.post(
            "/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a",
            headers={"X-Ella-Caregiver-Service-Key": "wrong"},
        ).status_code
        == 403
    )
    accepted = client.post(
        "/v1/ella/generate-dashboard-token?uid=uid-a&caregiver_id=caregiver-a",
        headers={"X-Ella-Caregiver-Service-Key": "caregiver-service-test"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["expires_in_hours"] == 24


def test_dashboard_signing_has_no_source_fallback(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.delenv("ELLA_DASHBOARD_SECRET", raising=False)
    response = client.get("/v1/ella/caregiver-dashboard-data?token=invalid")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "caregiver_dashboard_auth_not_configured"
