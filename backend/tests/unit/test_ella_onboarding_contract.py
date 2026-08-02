import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, Response
from pydantic import ValidationError

from ella.routers import onboarding
from ella.services.provisioning import ProvisioningError
from ella.utils.provision_authority import APPROVED_HERMES_PROVISION_URL, _authority_binding_value


def _configure_hermes_authority(monkeypatch):
    token = "onboarding-hermes-test-token"
    binding_env = "ELLA_TEST_ONBOARDING_AUTHORITY_BINDING"
    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://legacy-authority:8200")
    monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", "onboarding-legacy-test-token")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", APPROVED_HERMES_PROVISION_URL)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", token)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF", f"env:{binding_env}")
    monkeypatch.setenv(binding_env, _authority_binding_value(APPROVED_HERMES_PROVISION_URL, token))


def test_ensure_contract_forbids_caller_supplied_identity():
    with pytest.raises(ValidationError):
        onboarding.OnboardingEnsureRequest(uid="attacker", client={"platform": "ios"})
    with pytest.raises(ValidationError):
        onboarding.OnboardingEnsureRequest(client={"platform": "ios", "gateway_token": "secret"})


def test_onboarding_ensure_requires_current_ai_consent_dependency():
    route = next(route for route in onboarding.router.routes if route.path == "/v1/ella/onboarding/ensure")
    assert any(dependency.call is onboarding.require_current_ai_consent for dependency in route.dependant.dependencies)


def test_verified_identity_comes_from_firebase_subject(monkeypatch):
    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda uid: SimpleNamespace(
            email="verified@example.com",
            email_verified=True,
            display_name="Verified User",
        ),
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
    _configure_hermes_authority(monkeypatch)
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
        lambda uid: SimpleNamespace(
            email="verified@example.com",
            email_verified=True,
            display_name="Verified User",
        ),
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
    monkeypatch.delenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setattr(onboarding, "_coordinator", forbidden_coordinator)

    async def no_retained_receipt(_uid, _target_schema_version):
        return None

    monkeypatch.setattr(onboarding, "_retained_receipt", no_retained_receipt)

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


def test_disabled_endpoint_allows_existing_retained_account_without_provisioning(monkeypatch):
    async def forbidden_coordinator():
        raise AssertionError("retained onboarding must not start Hermes provisioning")

    async def retained_receipt(uid, target_schema_version):
        assert uid == "retained-user"
        assert target_schema_version == "hermes-user-v1"
        return {
            "state": "ready",
            "stage": "ready",
            "binding_state": "active",
            "binding_revision": 1,
            "effective_policy_revision": "retained-compatibility-v1",
        }

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setattr(onboarding, "_coordinator", forbidden_coordinator)
    monkeypatch.setattr(onboarding, "_retained_receipt", retained_receipt)
    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda _uid: (_ for _ in ()).throw(AssertionError("retained compatibility must not query Firebase")),
    )

    result = asyncio.run(
        onboarding.ensure_onboarding(
            onboarding.OnboardingEnsureRequest(),
            BackgroundTasks(),
            Response(),
            uid="retained-user",
        )
    )

    assert result["state"] == "ready"
    assert result["binding_state"] == "active"


def test_disabled_status_allows_existing_retained_account(monkeypatch):
    async def retained_receipt(uid, target_schema_version):
        assert uid == "retained-user"
        assert target_schema_version == "hermes-user-v1"
        return {"state": "ready", "binding_state": "active", "binding_revision": 1}

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setattr(onboarding, "_retained_receipt", retained_receipt)

    result = asyncio.run(onboarding.onboarding_status(uid="retained-user"))

    assert result == {"state": "ready", "binding_state": "active", "binding_revision": 1}


def test_retained_receipt_uses_authenticated_uid_and_public_contract(monkeypatch):
    class FakeRepository:
        async def has_active_retained_runtime(self, uid):
            assert uid == "retained-user"
            return True

    async def fake_create(**_kwargs):
        return FakeRepository()

    monkeypatch.setattr(onboarding, "runtime_bindings_enabled", lambda _uid: False)
    monkeypatch.setattr(onboarding.EllaProvisioningRepository, "create", fake_create)

    result = asyncio.run(onboarding._retained_receipt("retained-user", "hermes-user-v1"))

    assert result["state"] == "ready"
    assert result["binding_state"] == "active"
    assert result["binding_revision"] > 0
    assert result["effective_policy_revision"] == "retained-compatibility-v1"


def test_retained_receipt_rejects_unknown_schema_without_database_lookup(monkeypatch):
    async def forbidden_create(**_kwargs):
        raise AssertionError("future schemas must not use retained compatibility")

    monkeypatch.setattr(onboarding.EllaProvisioningRepository, "create", forbidden_create)

    result = asyncio.run(onboarding._retained_receipt("retained-user", "hermes-user-v2"))

    assert result is None


def test_retained_receipt_rejects_uid_in_isolated_runtime_cutover(monkeypatch):
    async def forbidden_create(**_kwargs):
        raise AssertionError("isolated runtime cutover must not use retained compatibility")

    monkeypatch.setattr(onboarding, "runtime_bindings_enabled", lambda uid: uid == "cutover-user")
    monkeypatch.setattr(onboarding.EllaProvisioningRepository, "create", forbidden_create)

    result = asyncio.run(onboarding._retained_receipt("cutover-user", "hermes-user-v1"))

    assert result is None


def test_disabled_endpoint_does_not_claim_future_schema_for_retained_account(monkeypatch):
    async def forbidden_create(**_kwargs):
        raise AssertionError("future schemas must not use retained compatibility")

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setattr(onboarding.EllaProvisioningRepository, "create", forbidden_create)

    with pytest.raises(onboarding.HTTPException) as error:
        asyncio.run(
            onboarding.ensure_onboarding(
                onboarding.OnboardingEnsureRequest(target_schema_version="hermes-user-v2"),
                BackgroundTasks(),
                Response(),
                uid="retained-user",
            )
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"code": "provisioning_disabled"}


def test_uid_allowlist_canaries_onboarding_without_global_cutover(monkeypatch):
    _configure_hermes_authority(monkeypatch)
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
                False,
            )

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", "canary-user")
    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda uid: SimpleNamespace(
            email="canary@example.com",
            email_verified=True,
            display_name="Canary",
        ),
    )

    async def fake_coordinator():
        return FakeCoordinator()

    monkeypatch.setattr(onboarding, "_coordinator", fake_coordinator)
    result = asyncio.run(
        onboarding.ensure_onboarding(
            onboarding.OnboardingEnsureRequest(),
            BackgroundTasks(),
            Response(),
            uid="canary-user",
        )
    )

    assert result["state"] == "provisioning"
    assert captured["identity"].uid == "canary-user"


def test_schema_not_ready_returns_retryable_service_unavailable(monkeypatch):
    _configure_hermes_authority(monkeypatch)

    class FakeCoordinator:
        async def ensure_job(self, **_kwargs):
            raise ProvisioningError("provisioning_schema_not_ready", retryable=True)

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        onboarding.auth,
        "get_user",
        lambda uid: SimpleNamespace(
            email="canary@example.com",
            email_verified=True,
            display_name="Canary",
        ),
    )

    async def fake_coordinator():
        return FakeCoordinator()

    monkeypatch.setattr(onboarding, "_coordinator", fake_coordinator)
    with pytest.raises(onboarding.HTTPException) as error:
        asyncio.run(
            onboarding.ensure_onboarding(
                onboarding.OnboardingEnsureRequest(),
                BackgroundTasks(),
                Response(),
                uid="canary-user",
            )
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"code": "provisioning_schema_not_ready"}


def test_authority_rejection_precedes_identity_and_repository_side_effects(monkeypatch):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.delenv("ELLA_HERMES_PROVISION_API_URL", raising=False)
    side_effects = []

    def forbidden_identity(_uid):
        side_effects.append("identity")
        raise AssertionError("authority rejection must precede identity lookup")

    async def forbidden_coordinator():
        side_effects.append("repository")
        raise AssertionError("authority rejection must precede repository creation")

    monkeypatch.setattr(onboarding.auth, "get_user", forbidden_identity)
    monkeypatch.setattr(onboarding, "_coordinator", forbidden_coordinator)

    with pytest.raises(onboarding.HTTPException) as error:
        asyncio.run(
            onboarding.ensure_onboarding(
                onboarding.OnboardingEnsureRequest(),
                BackgroundTasks(),
                Response(),
                uid="boundary-user-secret",
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail == {"code": "hermes_provision_authority_incomplete"}
    assert side_effects == []
