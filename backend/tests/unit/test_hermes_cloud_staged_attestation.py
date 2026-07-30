from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ella.services.hermes_cloud_staged_attestation import (
    GLOBAL_FLAGS_REQUIRED_FALSE,
    UID_SELECTORS,
    StagedAttestationVerifier,
)
from ella.services.runtime_errors import ProvisioningError

UID = "synthetic-hermes-canary"
OWNER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 7, 29, 22, 50, tzinfo=timezone.utc)
ARTIFACT = "a" * 64


def _binding():
    return {
        "runtime_instance_id": "nous-runtime-canary",
        "template_version": "hermes-cloud-user-v1",
        "voice_policy_version": "voice-v1",
        "expected_model": "model-a",
        "allowed_tools": [],
        "required_capabilities": ["responses_api", "session_key_header"],
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "model-policy-v1",
        "prompt_artifact_receipt": {
            "model_context_window_tokens": 16384,
            "policy_commit_sha": "b" * 40,
            "approval_manifest_sha256": "c" * 64,
            "soul_sha256": ARTIFACT,
            "agents_sha256": ARTIFACT,
            "model_policy_sha256": ARTIFACT,
        },
    }


def _receipt():
    return {
        "schema_version": "ella-hermes-cloud-staged-attestation-v1",
        "attestation_id": "attestation-hermes-canary-01",
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "uid": UID,
        "account_id": OWNER_ID,
        "profile_id": OWNER_ID,
        "runtime_instance_id": "nous-runtime-canary",
        "template_version": "hermes-cloud-user-v1",
        "voice_policy_version": "voice-v1",
        "expected_model": "model-a",
        "allowed_tools": [],
        "required_capabilities": ["responses_api", "session_key_header"],
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "model-policy-v1",
        "model_context_window_tokens": 16384,
        "policy_commit_sha": "b" * 40,
        "approval_manifest_sha256": "c" * 64,
        "artifact_sha256": {
            "soul": ARTIFACT,
            "agents": ARTIFACT,
            "model_policy": ARTIFACT,
        },
        "stage": "pool_registration_and_claim_finalization",
        "content_free": True,
    }


@pytest.fixture
def staged_env(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_STAGED_ATTESTATION_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    for name in UID_SELECTORS:
        monkeypatch.setenv(name, UID)
    for name in GLOBAL_FLAGS_REQUIRED_FALSE:
        monkeypatch.setenv(name, "false")


def _protected_receipt(tmp_path, receipt=None, *, mode=0o400):
    root = tmp_path / "attestations"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    path = root / "canary.receipt.json"
    path.write_text(json.dumps(receipt or _receipt()), encoding="utf-8")
    path.chmod(mode)
    verifier = StagedAttestationVerifier(
        approved_root=str(root),
        expected_owner_uid=os.geteuid(),
        clock=lambda: NOW,
    )
    return verifier, path


def test_protected_receipt_covers_registration_and_exact_finalization(staged_env, tmp_path):
    verifier, path = _protected_receipt(tmp_path)
    registration = verifier.preflight(
        _binding(),
        receipt_ref=str(path),
        uid=UID,
        account_id=OWNER_ID,
        profile_id=OWNER_ID,
        profile_class="synthetic",
        phase="pool_registration",
    )
    database_binding = _binding()
    database_binding["allowed_tools"] = json.dumps(database_binding["allowed_tools"])
    database_binding["required_capabilities"] = json.dumps(database_binding["required_capabilities"])
    finalization = verifier.preflight(
        database_binding,
        receipt_ref=str(path),
        uid=UID,
        account_id=OWNER_ID,
        profile_id=OWNER_ID,
        profile_class="synthetic",
        phase="claim_finalization",
        prior_marker=registration["staged_attestation"],
    )

    assert registration["preflight_source"] == "server_staged_attestation"
    assert finalization["staged_attestation"]["phase"] == "claim_finalization"
    assert finalization["tools"] == []
    assert finalization["capabilities"] == ["responses_api", "session_key_header"]
    assert finalization["content_free"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("allowed_tools", "not-json"),
        ("allowed_tools", json.dumps({"tool": "wrong-shape"})),
        ("allowed_tools", json.dumps([1])),
        ("required_capabilities", json.dumps("responses_api")),
        ("required_capabilities", json.dumps(["responses_api", " "])),
    ),
)
def test_staged_receipt_rejects_malformed_database_string_lists(staged_env, tmp_path, field, value):
    verifier, path = _protected_receipt(tmp_path)
    binding = _binding()
    binding[field] = value

    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            binding,
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="claim_finalization",
        )

    assert exc.value.code == "hermes_cloud_staged_attestation_pin_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("allowed_tools", "[]"),
        ("allowed_tools", ["tool-b", "tool-a"]),
        ("required_capabilities", ["responses_api", "responses_api"]),
        ("required_capabilities", ["responses_api", 1]),
    ),
)
def test_staged_receipt_rejects_noncanonical_pin_arrays(staged_env, tmp_path, field, value):
    receipt = _receipt()
    receipt[field] = value
    verifier, path = _protected_receipt(tmp_path, receipt)

    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="pool_registration",
        )

    assert exc.value.code == "hermes_cloud_staged_attestation_pin_mismatch"


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (lambda receipt: receipt.update(expected_model="wrong"), "hermes_cloud_staged_attestation_pin_mismatch"),
        (
            lambda receipt: receipt.update(expires_at=(NOW - timedelta(seconds=1)).isoformat()),
            "hermes_cloud_staged_attestation_stale",
        ),
    ),
)
def test_staged_receipt_rejects_mismatch_and_stale(staged_env, tmp_path, mutation, code):
    receipt = _receipt()
    mutation(receipt)
    verifier, path = _protected_receipt(tmp_path, receipt)
    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="pool_registration",
        )
    assert exc.value.code == code


def test_staged_receipt_rejects_real_profile_wrong_selector_and_insecure_file(staged_env, tmp_path, monkeypatch):
    verifier, path = _protected_receipt(tmp_path, mode=0o644)
    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="pool_registration",
        )
    assert exc.value.code == "hermes_cloud_staged_attestation_permissions"

    path.chmod(0o400)
    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="real",
            phase="pool_registration",
        )
    assert exc.value.code == "hermes_cloud_staged_attestation_identity_mismatch"

    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "synthetic-other")
    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="pool_registration",
        )
    assert exc.value.code == "hermes_cloud_staged_attestation_gate_denied"


def test_staged_receipt_rejects_missing_and_replayed_prior_marker(staged_env, tmp_path):
    verifier, path = _protected_receipt(tmp_path)
    missing = path.with_name("missing.receipt.json")
    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(missing),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="pool_registration",
        )
    assert exc.value.code == "hermes_cloud_staged_attestation_missing"

    with pytest.raises(ProvisioningError) as exc:
        verifier.preflight(
            _binding(),
            receipt_ref=str(path),
            uid=UID,
            account_id=OWNER_ID,
            profile_id=OWNER_ID,
            profile_class="synthetic",
            phase="claim_finalization",
            prior_marker={"phase": "claim_finalization"},
        )
    assert exc.value.code == "hermes_cloud_staged_attestation_replay_or_drift"
