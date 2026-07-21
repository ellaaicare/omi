import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, Response
from pydantic import ValidationError

from ella.routers import onboarding


def test_ensure_contract_forbids_caller_supplied_identity():
    with pytest.raises(ValidationError):
        onboarding.OnboardingEnsureRequest(uid="attacker", client={"platform": "ios"})
    with pytest.raises(ValidationError):
        onboarding.OnboardingEnsureRequest(client={"platform": "ios", "gateway_token": "secret"})


def test_verified_identity_comes_from_firebase_subject(monkeypatch):
    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda uid: SimpleNamespace(email="verified@example.com", display_name="Verified User"),
    )
    payload = onboarding.OnboardingEnsureRequest(client={"timezone": "America/New_York"})

    identity = onboarding._verified_identity("firebase-subject", payload)

    assert identity.uid == "firebase-subject"
    assert identity.email == "verified@example.com"
    assert identity.name == "Verified User"
    assert identity.timezone == "America/New_York"


def test_verified_identity_requires_firebase_email(monkeypatch):
    monkeypatch.setattr(onboarding.auth, "get_user", lambda uid: SimpleNamespace(email=None, display_name=None))
    with pytest.raises(onboarding.HTTPException) as error:
        onboarding._verified_identity("firebase-subject", onboarding.OnboardingEnsureRequest())
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "identity_missing_email"}


def test_ensure_schedules_only_authenticated_subject(monkeypatch):
    captured = {}

    class FakeCoordinator:
        async def ensure_job(self, **kwargs):
            captured.update(kwargs)
            return (
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "target_schema_version": "hermes-user-v1",
                    "state": "provisioning",
                    "stage": "profile_ready",
                    "retryable": True,
                },
                None,
                True,
            )

        async def process_claimed_job(self, **kwargs):
            return None

    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda uid: SimpleNamespace(email="verified@example.com", display_name="Verified User"),
    )
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")

    async def fake_coordinator():
        return FakeCoordinator()

    monkeypatch.setattr(onboarding, "_coordinator", fake_coordinator)
    response = Response()
    tasks = BackgroundTasks()
    result = asyncio.run(
        onboarding.ensure_onboarding(
            onboarding.OnboardingEnsureRequest(client_request_id="request-a"),
            tasks,
            response,
            uid="firebase-subject",
        )
    )

    assert response.status_code == 202
    assert result["state"] == "provisioning"
    assert captured["identity"].uid == "firebase-subject"
    assert len(tasks.tasks) == 1


def test_disabled_endpoint_returns_without_touching_database(monkeypatch):
    async def forbidden_coordinator():
        raise AssertionError("disabled onboarding must not touch provisioning storage")

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setattr(onboarding, "_coordinator", forbidden_coordinator)

    with pytest.raises(onboarding.HTTPException) as error:
        asyncio.run(
            onboarding.ensure_onboarding(
                onboarding.OnboardingEnsureRequest(),
                BackgroundTasks(),
                Response(),
                uid="firebase-subject",
            )
        )
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "provisioning_disabled"}
