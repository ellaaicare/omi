from types import SimpleNamespace

import pytest

from scripts import imessage_retained_runtime_admin as admin


def _manifest() -> dict:
    return {
        "contract": admin.MANIFEST_CONTRACT,
        "profile_name": "plato-eval",
        "agent_id": "plato-eval",
        "workspace_root": "/Users/ellaai/.hermes/profiles/plato-eval/workspace",
        "internal_gateway_url": "http://127.0.0.1:8657",
        "gateway_port": 8657,
        "service_label": "ai.hermes.gateway-plato-eval",
        "credential_ref": "ELLA_IMESSAGE_PLATO_EVAL_GATEWAY_TOKEN",
        "honcho_workspace": "plato-eval",
        "observed_peer": "plato-eval-owner",
        "observer_peer": "plato-eval-observer",
        "template_version": "retained-v1",
        "model_policy_version": "retained-model-v1",
        "voice_policy_version": "retained-voice-v1",
    }


def test_manifest_pins_preserved_profile_and_exact_loopback_authority(monkeypatch):
    monkeypatch.delenv("ELLA_HERMES_PROFILES_ROOT", raising=False)

    spec = admin._parse_manifest(_manifest())

    assert spec.profile_name == "plato-eval"
    assert spec.gateway_port == 8657
    assert spec.credential_ref == "ELLA_IMESSAGE_PLATO_EVAL_GATEWAY_TOKEN"

    wrong_profile = {**_manifest(), "profile_name": "other-retained-agent"}
    with pytest.raises(admin.AdminInputError, match="imessage_retained_profile_invalid"):
        admin._parse_manifest(wrong_profile)

    public_endpoint = {**_manifest(), "internal_gateway_url": "https://example.invalid:8657"}
    with pytest.raises(admin.AdminInputError, match="imessage_retained_gateway_not_loopback"):
        admin._parse_manifest(public_endpoint)


def test_protected_manifest_refuses_non_root_or_insecure_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(admin.os, "geteuid", lambda: 501)
    with pytest.raises(admin.AdminInputError, match="imessage_retained_admin_root_required"):
        admin._load_protected_json(str(path), kind="manifest")

    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        admin,
        "_file_stat",
        lambda _path: SimpleNamespace(st_uid=0, st_mode=0o100644),
    )
    with pytest.raises(admin.AdminInputError, match="imessage_retained_manifest_file_insecure"):
        admin._load_protected_json(str(path), kind="manifest")


def test_health_receipt_is_content_free_exact_and_manifest_bound():
    manifest_sha256 = "a" * 64
    body = {
        "contract": admin.HEALTH_CONTRACT,
        "manifest_sha256": manifest_sha256,
        "profile_home_match": True,
        "workspace_match": True,
        "service_match": True,
        "endpoint_match": True,
        "credential_ref_resolved": True,
        "health_status": "healthy",
    }

    admin._parse_health(body, manifest_sha256=manifest_sha256)

    with pytest.raises(admin.AdminInputError, match="imessage_retained_health_receipt_mismatch"):
        admin._parse_health({**body, "endpoint_match": False}, manifest_sha256=manifest_sha256)
    with pytest.raises(admin.AdminInputError, match="imessage_retained_health_receipt_invalid"):
        admin._parse_health({**body, "endpoint": "http://forbidden"}, manifest_sha256=manifest_sha256)
