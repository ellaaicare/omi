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


def test_synthetic_gate_rejects_identity_outside_exact_allowlist(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-a")

    assert_cloud_identity_gate("synthetic-a")
    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate("real-user")
    assert error.value.code == "hermes_cloud_synthetic_identity_required"


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
        assert_cloud_identity_gate("user-a")
    assert error.value.code == "hermes_cloud_consent_policy_not_deployed"

    monkeypatch.setattr(
        ai_consent,
        "PROCESSORS",
        (
            *ai_consent.PROCESSORS,
            {"id": "hermes-cloud"},
            {"id": "honcho-cloud"},
        ),
    )

    class Consent:
        def status(self, uid):
            return {
                "authorized": True,
                "consent": {
                    "decision": "granted",
                    "policy_version": ai_consent.CURRENT_POLICY_VERSION,
                    "processor_set_hash": ai_consent.CURRENT_PROCESSOR_SET_HASH,
                    "receipt_id": "receipt-a",
                },
            }

    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: Consent())
    assert_cloud_identity_gate("user-a")

    monkeypatch.setenv("ELLA_HERMES_CLOUD_CONSENT_POLICY_VERSION", "stale-policy")
    with pytest.raises(ProvisioningError) as error:
        assert_cloud_identity_gate("user-a")
    assert error.value.code == "hermes_cloud_consent_policy_not_deployed"


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
