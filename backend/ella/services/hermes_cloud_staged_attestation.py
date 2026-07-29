"""Protected synthetic-only preflight receipt for one Hermes Cloud canary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ella.services.hermes_cloud_policy import cloud_synthetic_only
from ella.services.runtime_errors import ProvisioningError

SCHEMA_VERSION = "ella-hermes-cloud-staged-attestation-v1"
APPROVED_ROOT = "/var/lib/ella/hermes-cloud-attestations"
MAX_RECEIPT_BYTES = 16_384
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
TRUE_VALUES = {"1", "true", "yes", "on"}
UID_SELECTORS = (
    "ELLA_RUNTIME_BINDINGS_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_SYNTHETIC_UIDS",
    "ELLA_AI_CONSENT_ENFORCEMENT_UIDS",
)
GLOBAL_FLAGS_REQUIRED_FALSE = (
    "ELLA_RUNTIME_BINDINGS_ENABLED",
    "ELLA_HERMES_PROVISIONING_ENABLED",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
    "ELLA_AI_CONSENT_ENFORCEMENT_ENABLED",
    "ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED",
    "ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED",
    "ELLA_ISOLATED_VOICE_ROUTING_ENABLED",
    "ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED",
    "ELLA_INVITE_APP_REVIEW_ENABLED",
)
RECEIPT_KEYS = {
    "schema_version",
    "attestation_id",
    "issued_at",
    "expires_at",
    "uid",
    "account_id",
    "profile_id",
    "runtime_instance_id",
    "template_version",
    "voice_policy_version",
    "expected_model",
    "allowed_tools",
    "required_capabilities",
    "prompt_pack_version",
    "model_policy_version",
    "model_context_window_tokens",
    "policy_commit_sha",
    "approval_manifest_sha256",
    "artifact_sha256",
    "stage",
    "content_free",
}


def _fail(code: str) -> None:
    raise ProvisioningError(code, retryable=False)


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        _fail("hermes_cloud_staged_attestation_time_invalid")
    if parsed.tzinfo is None:
        _fail("hermes_cloud_staged_attestation_time_invalid")
    return parsed.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


class StagedAttestationVerifier:
    """Read and pin one root-owned content-free receipt without network access."""

    def __init__(
        self,
        *,
        approved_root: str = APPROVED_ROOT,
        expected_owner_uid: int = 0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.approved_root = approved_root
        self.expected_owner_uid = expected_owner_uid
        self.clock = clock

    def enabled(self) -> bool:
        return os.getenv("ELLA_HERMES_CLOUD_STAGED_ATTESTATION_ENABLED", "") == "true"

    def assert_gate(self, uid: str) -> None:
        exact_selectors = all(
            {item.strip() for item in os.getenv(name, "").split(",") if item.strip()} == {uid} for name in UID_SELECTORS
        )
        globals_off = not any(
            os.getenv(name, "false").strip().lower() in TRUE_VALUES for name in GLOBAL_FLAGS_REQUIRED_FALSE
        )
        if (
            not self.enabled()
            or not cloud_synthetic_only()
            or not uid.startswith(("synthetic-", "staging-synthetic-"))
            or not exact_selectors
            or not globals_off
        ):
            _fail("hermes_cloud_staged_attestation_gate_denied")

    def _read(self, receipt_ref: str) -> tuple[dict[str, Any], str]:
        if (
            not receipt_ref
            or not os.path.isabs(receipt_ref)
            or os.path.normpath(receipt_ref) != receipt_ref
            or Path(receipt_ref).parent != Path(self.approved_root)
            or not SAFE_FILENAME_RE.fullmatch(Path(receipt_ref).name)
        ):
            _fail("hermes_cloud_staged_attestation_path_invalid")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        file_flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
            file_flags |= os.O_NOFOLLOW
        try:
            root_fd = os.open(self.approved_root, directory_flags)
        except OSError as exc:
            raise ProvisioningError("hermes_cloud_staged_attestation_missing", retryable=False) from exc
        try:
            root_stat = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_uid != self.expected_owner_uid
                or stat.S_IMODE(root_stat.st_mode) != 0o700
            ):
                _fail("hermes_cloud_staged_attestation_permissions")
            try:
                receipt_fd = os.open(Path(receipt_ref).name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise ProvisioningError("hermes_cloud_staged_attestation_missing", retryable=False) from exc
            try:
                receipt_stat = os.fstat(receipt_fd)
                if (
                    not stat.S_ISREG(receipt_stat.st_mode)
                    or receipt_stat.st_uid != self.expected_owner_uid
                    or stat.S_IMODE(receipt_stat.st_mode) not in {0o400, 0o600}
                    or receipt_stat.st_nlink != 1
                    or not 1 <= receipt_stat.st_size <= MAX_RECEIPT_BYTES
                ):
                    _fail("hermes_cloud_staged_attestation_permissions")
                raw = os.read(receipt_fd, MAX_RECEIPT_BYTES + 1)
            finally:
                os.close(receipt_fd)
        finally:
            os.close(root_fd)
        if len(raw) > MAX_RECEIPT_BYTES:
            _fail("hermes_cloud_staged_attestation_invalid")
        try:
            receipt = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvisioningError("hermes_cloud_staged_attestation_invalid", retryable=False) from exc
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
            _fail("hermes_cloud_staged_attestation_invalid")
        return receipt, hashlib.sha256(raw).hexdigest()

    def preflight(
        self,
        binding: dict[str, Any],
        *,
        receipt_ref: str,
        uid: str,
        account_id: str,
        profile_id: str,
        profile_class: str,
        phase: str,
        prior_marker: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.assert_gate(uid)
        if profile_class != "synthetic" or account_id != profile_id:
            _fail("hermes_cloud_staged_attestation_identity_mismatch")
        receipt, receipt_sha256 = self._read(receipt_ref)
        now = self.clock().astimezone(timezone.utc)
        issued_at = _parse_utc(receipt["issued_at"])
        expires_at = _parse_utc(receipt["expires_at"])
        if issued_at > now + timedelta(minutes=5) or expires_at <= now:
            _fail("hermes_cloud_staged_attestation_stale")
        try:
            canonical_account = str(uuid.UUID(str(account_id)))
            canonical_profile = str(uuid.UUID(str(profile_id)))
        except ValueError:
            _fail("hermes_cloud_staged_attestation_identity_mismatch")
        prompt_receipt = _json_object(binding.get("prompt_artifact_receipt"))
        artifacts = receipt.get("artifact_sha256")
        expected_artifacts = {
            name: str(prompt_receipt.get(f"{name}_sha256") or "") for name in ("soul", "agents", "model_policy")
        }
        if (
            receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("content_free") is not True
            or receipt.get("stage") != "pool_registration_and_claim_finalization"
            or not SAFE_ID_RE.fullmatch(str(receipt.get("attestation_id") or ""))
            or receipt.get("uid") != uid
            or receipt.get("account_id") != canonical_account
            or receipt.get("profile_id") != canonical_profile
            or receipt.get("runtime_instance_id") != str(binding.get("runtime_instance_id") or "")
            or receipt.get("template_version") != str(binding.get("template_version") or "")
            or receipt.get("voice_policy_version") != str(binding.get("voice_policy_version") or "")
            or receipt.get("expected_model") != str(binding.get("expected_model") or "")
            or receipt.get("allowed_tools") != sorted(set(binding.get("allowed_tools") or []))
            or receipt.get("required_capabilities") != sorted(set(binding.get("required_capabilities") or []))
            or receipt.get("prompt_pack_version") != str(binding.get("prompt_pack_version") or "")
            or receipt.get("model_policy_version") != str(binding.get("model_policy_version") or "")
            or receipt.get("model_context_window_tokens")
            != (
                binding.get("model_context_window_tokens")
                if binding.get("model_context_window_tokens") is not None
                else prompt_receipt.get("model_context_window_tokens")
            )
            or receipt.get("policy_commit_sha") != str(prompt_receipt.get("policy_commit_sha") or "")
            or receipt.get("approval_manifest_sha256") != str(prompt_receipt.get("approval_manifest_sha256") or "")
            or not GIT_SHA_RE.fullmatch(str(receipt.get("policy_commit_sha") or ""))
            or not SHA256_RE.fullmatch(str(receipt.get("approval_manifest_sha256") or ""))
            or artifacts != expected_artifacts
            or any(not SHA256_RE.fullmatch(value) for value in expected_artifacts.values())
        ):
            _fail("hermes_cloud_staged_attestation_pin_mismatch")
        marker = {
            "schema_version": SCHEMA_VERSION,
            "attestation_id": str(receipt["attestation_id"]),
            "receipt_ref": receipt_ref,
            "receipt_sha256": receipt_sha256,
            "uid": uid,
            "account_id": canonical_account,
            "profile_id": canonical_profile,
            "runtime_instance_id": str(receipt["runtime_instance_id"]),
            "phase": phase,
            "expires_at": expires_at.isoformat(),
            "content_free": True,
        }
        if prior_marker is not None:
            expected_prior = {**marker, "phase": "pool_registration"}
            if prior_marker != expected_prior:
                _fail("hermes_cloud_staged_attestation_replay_or_drift")
        return {
            "status": "ok",
            "model": str(binding["expected_model"]),
            "tools": sorted(set(binding.get("allowed_tools") or [])),
            "capabilities": sorted(set(binding.get("required_capabilities") or [])),
            "prompt_artifacts": prompt_receipt,
            "preflight_source": "server_staged_attestation",
            "staged_attestation": marker,
            "content_free": True,
        }
