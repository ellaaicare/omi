import asyncio
import uuid

import pytest

from ella.services.provisioning import (
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
    extract_runtime_binding,
    public_receipt,
    resolve_gateway_credential,
    stable_payload_hash,
    validate_internal_gateway_url,
)
from ella.services.runtime_resolver import runtime_from_binding


def _job(**overrides):
    value = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "target_schema_version": "hermes-user-v1",
        "state": "pending",
        "stage": "identity_ready",
        "retryable": True,
    }
    value.update(overrides)
    return value


def _runtime_receipt(profile_name="omi-user-a"):
    return {
        "mode": "hermes_only",
        "provisionMode": "hermes_only",
        "runtimeBinding": {
            "provider": "hermes",
            "profileName": profile_name,
            "agentId": "hermes",
            "workspaceRoot": f"/Users/ellaai/.hermes/profiles/{profile_name}/workspace",
            "internalGatewayUrl": "http://100.76.138.56:8701",
            "gatewayPort": 8701,
            "serviceLabel": f"com.ella.hermes.{profile_name}",
            "credentialRef": "env:ELLA_HERMES_GATEWAY_KEY_USER_A",
            "healthState": "healthy",
            "smokePassed": True,
            "healthReceipt": {"smoke_passed": True, "probe": "synthetic"},
            "templateVersion": "hermes-user-v1",
            "modelPolicyVersion": "frontier-v1",
            "voicePolicyVersion": "ella-voice-v1",
            "honcho": {
                "workspace": "honcho-user-a",
                "observedPeer": "user-a",
                "observerPeer": "ella-user-a",
            },
        },
    }


class FakeRepository:
    def __init__(self, *, binding=None):
        self.job = _job()
        self.binding = binding
        self.staged = None
        self.identity_calls = []

    async def ensure_user_identity(self, **kwargs):
        self.identity_calls.append(kwargs)
        return {"omi_uid": kwargs["uid"]}

    async def acquire_job(self, **kwargs):
        return dict(self.job)

    async def resolve_active_runtime(self, uid):
        return self.binding

    async def claim_job(self, job_id):
        if self.job["state"] in {"ready", "blocked", "provisioning"}:
            return None
        self.job.update(state="provisioning", stage="profile_ready")
        return dict(self.job)

    async def update_job(self, **kwargs):
        self.job.update(
            state=kwargs["state"],
            stage=kwargs["stage"],
            retryable=kwargs["retryable"],
            error_code=kwargs.get("error_code"),
        )
        return dict(self.job)

    async def stage_runtime_binding(self, *, uid, binding):
        self.staged = dict(binding, omi_uid=uid, revision=1, active=False)
        return self.staged

    async def activate_runtime_binding(self, *, uid, provider):
        self.binding = dict(self.staged, active=True, revision=2)
        return self.binding


class FakeProvisionClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def provision(self, identity, target_schema_version):
        self.calls.append((identity, target_schema_version))
        return self.result


def test_payload_hash_is_stable_and_sensitive_to_values():
    assert stable_payload_hash({"b": 2, "a": 1}) == stable_payload_hash({"a": 1, "b": 2})
    assert stable_payload_hash({"a": 1}) != stable_payload_hash({"a": 2})


def test_public_receipt_does_not_expose_runtime_secrets():
    receipt = public_receipt(
        _job(state="ready", stage="active", retryable=False),
        {
            "active": True,
            "revision": 7,
            "model_policy_version": "frontier-v1",
            "voice_policy_version": "ella-voice-v1",
            "credential_ref": "env:TOP_SECRET",
            "internal_gateway_url": "http://100.76.138.56:8701",
            "workspace_root": "/private/workspace",
        },
    )

    assert receipt["state"] == "ready"
    assert receipt["binding_revision"] == 7
    serialized = str(receipt)
    assert "TOP_SECRET" not in serialized
    assert "100.76.138.56" not in serialized
    assert "/private/workspace" not in serialized


def test_gateway_credentials_are_server_env_references_only(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "secret-value")
    assert resolve_gateway_credential("env:ELLA_HERMES_GATEWAY_KEY_USER_A") == "secret-value"
    for invalid in (None, "secret-value", "file:/tmp/key", "env:PATH", "env:OPENAI_API_KEY"):
        with pytest.raises(ProvisioningError, match="invalid_credential_reference"):
            resolve_gateway_credential(invalid)


def test_internal_gateway_is_limited_to_loopback_tailnet_or_allowlist(monkeypatch):
    assert validate_internal_gateway_url("http://127.0.0.1:8701") == "http://127.0.0.1:8701"
    assert validate_internal_gateway_url("http://100.76.138.56:8701/") == "http://100.76.138.56:8701"
    with pytest.raises(ProvisioningError, match="invalid_internal_gateway_url"):
        validate_internal_gateway_url("https://example.com/runtime")
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_ALLOWED_HOSTS", "hermes.internal")
    assert validate_internal_gateway_url("http://hermes.internal:8701") == "http://hermes.internal:8701"


def test_runtime_receipt_rejects_non_hermes_and_plato_cross_user(monkeypatch):
    non_hermes = _runtime_receipt()
    non_hermes["runtimeBinding"]["provider"] = "openclaw"
    with pytest.raises(ProvisioningError, match="invalid_runtime_provider"):
        extract_runtime_binding(non_hermes, "user-a")

    monkeypatch.setenv("ELLA_PLATO_UID", "plato-owner")
    with pytest.raises(ProvisioningError, match="plato_binding_forbidden"):
        extract_runtime_binding(_runtime_receipt("plato-eval"), "user-a")
    assert extract_runtime_binding(_runtime_receipt("plato-eval"), "plato-owner")["profile_name"] == "plato-eval"


def test_runtime_receipt_requires_owned_workspace_port_and_honcho():
    wrong_workspace = _runtime_receipt()
    wrong_workspace["runtimeBinding"]["workspaceRoot"] = "/Users/ellaai/.hermes/profiles/plato-eval/workspace"
    with pytest.raises(ProvisioningError, match="workspace_ownership_mismatch"):
        extract_runtime_binding(wrong_workspace, "user-a")

    wrong_port = _runtime_receipt()
    wrong_port["runtimeBinding"]["gatewayPort"] = 8702
    with pytest.raises(ProvisioningError, match="gateway_port_mismatch"):
        extract_runtime_binding(wrong_port, "user-a")

    no_honcho = _runtime_receipt()
    no_honcho["runtimeBinding"]["honcho"] = {}
    with pytest.raises(ProvisioningError, match="honcho_receipt_incomplete"):
        extract_runtime_binding(no_honcho, "user-a")


def test_runtime_resolver_enforces_owner_health_and_credential(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "secret-value")
    binding = extract_runtime_binding(_runtime_receipt(), "user-a")
    binding.update(omi_uid="user-a", active=True, revision=4)
    runtime = runtime_from_binding(binding, "user-a")
    assert runtime.profile_name == "omi-user-a"
    assert runtime.gateway_token == "secret-value"
    with pytest.raises(ProvisioningError, match="runtime_ownership_mismatch"):
        runtime_from_binding(binding, "user-b")


def test_disabled_provisioning_stays_retryable_and_can_resume(monkeypatch):
    repository = FakeRepository()
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    job, binding, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )
    assert (job["state"], job["retryable"], claimed, binding) == ("degraded", True, False, None)

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    job, _, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-b",
            request_payload={"client": "ios"},
        )
    )
    assert job["state"] == "provisioning"
    assert claimed is True


def test_successful_provision_stages_then_activates_binding(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    repository = FakeRepository()
    client = FakeProvisionClient(_runtime_receipt())
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(repository, client)
    job, _, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )
    assert claimed is True

    asyncio.run(coordinator.process_claimed_job(job=job, identity=identity))

    assert repository.job["state"] == "ready"
    assert repository.binding["active"] is True
    assert repository.binding["provider"] == "hermes"
    assert client.calls == [(identity, "hermes-user-v1")]


def test_legacy_8210_receipt_cannot_activate(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    repository = FakeRepository()
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(
        repository,
        FakeProvisionClient({"mode": "hermes_only", "provisionMode": "hermes_only"}),
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(state="provisioning"), identity=identity))

    assert repository.job["state"] == "degraded"
    assert repository.job["error_code"] == "runtime_receipt_missing"
    assert repository.binding is None
