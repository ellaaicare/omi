import hashlib
import hmac
import json

import pytest

from ella.services import ai_consent
from ella.services.hermes_cloud_policy import (
    ApprovedRuntimeManifestStore,
    assert_cloud_identity_gate,
)
from ella.services.provisioning import ProvisioningError


def _manifest():
    return {
        "schema_version": "ella-hermes-cloud-approval-v1",
        "review_disposition": "approved",
        "policy_commit_sha": "a" * 40,
        "lane_s_review_url": "https://github.com/ellaaicare/ella-ai/pull/1127",
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "models-v1",
        "expected_model": "model-a",
        "model_context_window_tokens": 16384,
        "allowed_tools": ["honcho_recall"],
        "required_capabilities": ["responses_api", "session_key_header"],
        "artifact_sha256": {
            "soul": "b" * 64,
            "agents": "c" * 64,
            "model_policy": "d" * 64,
        },
    }


def _signed_manifest(value, key):
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        **value,
        "signature_hmac_sha256": hmac.new(
            key.encode("utf-8"),
            serialized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
    }


def _managed_route(uid="user-a"):
    return {
        "profile_uid": uid,
        "runtime_provider": ai_consent.MANAGED_CLOUD_RUNTIME_PROVIDER,
        "model_route": ai_consent.MANAGED_CLOUD_MODEL_ROUTE,
        "memory_provider": ai_consent.MANAGED_CLOUD_MEMORY_PROVIDER,
        "photon_scope": ai_consent.MANAGED_CLOUD_PHOTON_SCOPE,
    }


def _deploy_v6(monkeypatch, uid="user-a"):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "false")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_CONSENT_POLICY_VERSION", ai_consent.CURRENT_POLICY_VERSION)
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_CONSENT_PROCESSOR_SET_HASH",
        ai_consent.CURRENT_PROCESSOR_SET_HASH,
    )
    monkeypatch.setenv("ELLA_HERMES_CLOUD_CONSENT_SCOPE_VERSION", ai_consent.CURRENT_SCOPE_VERSION)
    monkeypatch.setenv("ELLA_HERMES_CLOUD_CONSENT_SCOPE_HASH", ai_consent.CURRENT_SCOPE_HASH)
    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)


def test_synthetic_gate_rejects_identity_outside_exact_allowlist(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-a")

    assert_cloud_identity_gate("synthetic-a", profile_class="synthetic")
    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate("real-user", profile_class="synthetic")
    assert error.value.code == "hermes_cloud_synthetic_identity_required"


@pytest.mark.parametrize("profile_class", [None, "", "real", "synthetic,real"])
def test_synthetic_gate_requires_exact_server_profile_class(
    monkeypatch,
    profile_class,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "allowlisted-user")

    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate(
            "allowlisted-user",
            profile_class=profile_class,
        )

    assert error.value.code == "hermes_cloud_synthetic_profile_required"


def test_managed_cloud_gate_requires_exact_policy_and_current_receipt(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "false")
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_CONSENT_POLICY_VERSION",
        ai_consent.CURRENT_POLICY_VERSION,
    )
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_CONSENT_PROCESSOR_SET_HASH",
        ai_consent.CURRENT_PROCESSOR_SET_HASH,
    )
    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate("user-a", **_managed_route())
    assert error.value.code == "hermes_cloud_consent_policy_not_deployed"

    _deploy_v6(monkeypatch)
    repository = ai_consent.InMemoryConsentRepository()
    ai_consent.AiConsentService(repository).submit(
        "user-a",
        ai_consent.ConsentSubmission(
            decision="granted",
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            request_id="request-0001",
            app_version="1.0.0",
            build_number="804",
            locale="en-US",
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )
    monkeypatch.setattr(ai_consent, "_repository", repository)
    assert_cloud_identity_gate("user-a", **_managed_route())

    monkeypatch.setenv("ELLA_HERMES_CLOUD_CONSENT_POLICY_VERSION", "stale-policy")
    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate("user-a", **_managed_route())
    assert error.value.code == "hermes_cloud_consent_policy_not_deployed"


def test_real_cloud_gate_rejects_default_off_scope_drift_and_profile_switch(monkeypatch):
    _deploy_v6(monkeypatch)
    repository = ai_consent.InMemoryConsentRepository()
    ai_consent.AiConsentService(repository).submit(
        "user-a",
        ai_consent.ConsentSubmission(
            decision="granted",
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            request_id="request-0001",
            app_version="1.0.0",
            build_number="804",
            locale="en-US",
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )
    monkeypatch.setattr(ai_consent, "_repository", repository)

    monkeypatch.delenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS")
    with pytest.raises(ProvisioningError) as disabled:
        assert_cloud_identity_gate("user-a", **_managed_route())
    assert disabled.value.code == "managed_cloud_real_data_disabled"

    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", "user-a")
    drifted = _managed_route()
    drifted["model_route"] = "openai-codex/other-model"
    with pytest.raises(ProvisioningError) as drift:
        assert_cloud_identity_gate("user-a", **drifted)
    assert drift.value.code == "managed_cloud_consent_scope_drift"

    with pytest.raises(ProvisioningError) as profile:
        assert_cloud_identity_gate("user-a", **_managed_route("profile-b"))
    assert profile.value.code == "managed_cloud_consent_stale"


def test_approval_manifest_is_server_signed_and_tamper_evident(tmp_path):
    key = "server-owned-signing-key-at-least-32-bytes"
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(_signed_manifest(_manifest(), key)), encoding="utf-8")

    approved = ApprovedRuntimeManifestStore(
        path=str(path),
        signing_key=key,
    ).load()
    assert approved.policy_commit_sha == "a" * 40
    assert approved.expected_model == "model-a"
    assert approved.model_context_window_tokens == 16384

    tampered = _signed_manifest(_manifest(), key)
    tampered["expected_model"] = "candidate-model"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ProvisioningError) as error:
        ApprovedRuntimeManifestStore(path=str(path), signing_key=key).load()
    assert error.value.code == "hermes_cloud_approval_signature_invalid"


@pytest.mark.parametrize("value", [None, True, 0, -1, "16384"])
def test_approval_manifest_requires_positive_integer_model_context(
    tmp_path,
    value,
):
    key = "server-owned-signing-key-at-least-32-bytes"
    manifest = _manifest()
    if value is None:
        manifest.pop("model_context_window_tokens")
    else:
        manifest["model_context_window_tokens"] = value
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(_signed_manifest(manifest, key)), encoding="utf-8")

    with pytest.raises(ProvisioningError) as error:
        ApprovedRuntimeManifestStore(path=str(path), signing_key=key).load()

    assert error.value.code == "hermes_cloud_approval_model_context_invalid"
