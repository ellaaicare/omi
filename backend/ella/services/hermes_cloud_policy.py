"""Server-owned Hermes Cloud approval and processor-consent gates."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ella.services import ai_consent
from ella.services.runtime_errors import ProvisioningError

APPROVAL_SCHEMA_VERSION = "ella-hermes-cloud-approval-v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
TRUE_VALUES = {"1", "true", "yes", "on"}
MANAGED_CLOUD_PROCESSORS = {
    "nous-hermes-cloud",
    "hermes-profile-memory",
    "openai-codex",
    "photon",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _synthetic_uids() -> set[str]:
    return {value.strip() for value in os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "").split(",") if value.strip()}


def cloud_synthetic_only() -> bool:
    return os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true").strip().lower() in TRUE_VALUES


def assert_cloud_operator_gate() -> None:
    """Allow vendor preflight only in synthetic mode or after cloud consent is deployed."""
    if cloud_synthetic_only():
        return
    _assert_deployed_cloud_consent_policy()


def assert_cloud_identity_gate(
    uid: str,
    *,
    profile_class: Optional[str] = None,
    profile_uid: Optional[str] = None,
    runtime_provider: Optional[str] = None,
    model_route: Optional[str] = None,
    memory_provider: Optional[str] = None,
    photon_scope: Optional[str] = None,
) -> str:
    """Return the server-owned grant epoch for an allowed cloud identity."""
    if cloud_synthetic_only():
        assert_cloud_synthetic_identity_gate(uid, profile_class=profile_class)
        return (
            "synthetic:" + hashlib.sha256(f"{ai_consent.CURRENT_POLICY_VERSION}\x1f{uid}".encode("utf-8")).hexdigest()
        )
    _assert_deployed_cloud_consent_policy()
    try:
        return ai_consent.assert_managed_cloud_consent(
            uid,
            profile_uid=str(profile_uid or ""),
            runtime_provider=str(runtime_provider or ""),
            model_route=str(model_route or ""),
            memory_provider=str(memory_provider or ""),
            photon_scope=str(photon_scope or ""),
        )
    except ai_consent.ManagedCloudConsentError as exc:
        raise ProvisioningError(exc.code, retryable=False) from exc


def assert_cloud_synthetic_identity_gate(
    uid: str,
    *,
    profile_class: Optional[str],
) -> None:
    """Reject synthetic canary traffic unless both rollout and DB authority agree."""
    if not cloud_synthetic_only():
        return
    if uid not in _synthetic_uids():
        raise ProvisioningError("hermes_cloud_synthetic_identity_required", retryable=False)
    if str(profile_class or "").strip().lower() != "synthetic":
        raise ProvisioningError("hermes_cloud_synthetic_profile_required", retryable=False)


def _assert_deployed_cloud_consent_policy() -> tuple[str, str, str, str]:
    required_version = os.getenv("ELLA_HERMES_CLOUD_CONSENT_POLICY_VERSION", "").strip()
    required_hash = os.getenv("ELLA_HERMES_CLOUD_CONSENT_PROCESSOR_SET_HASH", "").strip()
    required_scope_version = os.getenv("ELLA_HERMES_CLOUD_CONSENT_SCOPE_VERSION", "").strip()
    required_scope_hash = os.getenv("ELLA_HERMES_CLOUD_CONSENT_SCOPE_HASH", "").strip()
    if (
        not required_version
        or not required_hash
        or not required_scope_version
        or not required_scope_hash
        or required_version != ai_consent.CURRENT_POLICY_VERSION
        or required_hash != ai_consent.CURRENT_PROCESSOR_SET_HASH
        or required_scope_version != ai_consent.CURRENT_SCOPE_VERSION
        or required_scope_hash != ai_consent.CURRENT_SCOPE_HASH
        or not MANAGED_CLOUD_PROCESSORS.issubset(
            {str(processor.get("id") or "") for processor in ai_consent.PROCESSORS if isinstance(processor, dict)}
        )
    ):
        raise ProvisioningError("hermes_cloud_consent_policy_not_deployed", retryable=False)
    return required_version, required_hash, required_scope_version, required_scope_hash


@dataclass(frozen=True)
class ApprovedRuntimeManifest:
    policy_commit_sha: str
    lane_s_review_url: str
    prompt_pack_version: str
    model_policy_version: str
    expected_model: str
    model_context_window_tokens: int
    allowed_tools: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    artifact_sha256: dict[str, str]
    manifest_sha256: str

    def binding_receipt(self, observed: dict[str, str]) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "policy_commit_sha": self.policy_commit_sha,
            "lane_s_review_url": self.lane_s_review_url,
            "prompt_pack_version": self.prompt_pack_version,
            "model_policy_version": self.model_policy_version,
            "expected_model": self.expected_model,
            "model_context_window_tokens": self.model_context_window_tokens,
            "approval_manifest_sha256": self.manifest_sha256,
            "content_free": True,
        }
        for name, expected in self.artifact_sha256.items():
            receipt[f"{name}_sha256"] = expected
            receipt[f"observed_{name}_sha256"] = observed[name]
        return receipt


class ApprovedRuntimeManifestStore:
    """Load an HMAC-authenticated policy manifest owned by the server."""

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        signing_key: Optional[str] = None,
        reader: Callable[[Path], str] = lambda path: path.read_text(encoding="utf-8"),
    ):
        self.path = path or os.getenv("ELLA_HERMES_CLOUD_APPROVED_MANIFEST_PATH", "")
        self.signing_key = signing_key or os.getenv("ELLA_HERMES_CLOUD_APPROVAL_SIGNING_KEY", "")
        self.reader = reader

    def load(self) -> ApprovedRuntimeManifest:
        path = Path(self.path)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ProvisioningError("hermes_cloud_approval_manifest_missing", retryable=False)
        mode = path.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ProvisioningError("hermes_cloud_approval_manifest_permissions", retryable=False)
        if len(self.signing_key) < 32:
            raise ProvisioningError("hermes_cloud_approval_key_missing", retryable=False)
        try:
            raw = json.loads(self.reader(path))
        except (OSError, ValueError, TypeError) as exc:
            raise ProvisioningError("hermes_cloud_approval_manifest_invalid", retryable=False) from exc
        if not isinstance(raw, dict):
            raise ProvisioningError("hermes_cloud_approval_manifest_invalid", retryable=False)
        signature = str(raw.pop("signature_hmac_sha256", "")).lower()
        serialized = _stable_json(raw).encode("utf-8")
        expected_signature = hmac.new(
            self.signing_key.encode("utf-8"),
            serialized,
            hashlib.sha256,
        ).hexdigest()
        if not SHA256_RE.fullmatch(signature) or not hmac.compare_digest(signature, expected_signature):
            raise ProvisioningError("hermes_cloud_approval_signature_invalid", retryable=False)
        if raw.get("schema_version") != APPROVAL_SCHEMA_VERSION or raw.get("review_disposition") != "approved":
            raise ProvisioningError("hermes_cloud_policy_not_approved", retryable=False)

        policy_commit_sha = str(raw.get("policy_commit_sha") or "").lower()
        lane_s_review_url = str(raw.get("lane_s_review_url") or "")
        if not GIT_SHA_RE.fullmatch(policy_commit_sha) or not lane_s_review_url.startswith(
            "https://github.com/ellaaicare/ella-ai/"
        ):
            raise ProvisioningError("hermes_cloud_policy_approval_invalid", retryable=False)

        artifacts = raw.get("artifact_sha256")
        if not isinstance(artifacts, dict):
            raise ProvisioningError("hermes_cloud_approval_artifacts_invalid", retryable=False)
        normalized_artifacts: dict[str, str] = {}
        for name in ("soul", "agents", "model_policy"):
            digest = str(artifacts.get(name) or "").lower()
            if not SHA256_RE.fullmatch(digest):
                raise ProvisioningError("hermes_cloud_approval_artifacts_invalid", retryable=False)
            normalized_artifacts[name] = digest

        prompt_pack_version = str(raw.get("prompt_pack_version") or "").strip()
        model_policy_version = str(raw.get("model_policy_version") or "").strip()
        expected_model = str(raw.get("expected_model") or "").strip()
        model_context_window_tokens = raw.get("model_context_window_tokens")
        allowed_tools = tuple(sorted({str(value) for value in raw.get("allowed_tools") or [] if str(value)}))
        required_capabilities = tuple(
            sorted({str(value) for value in raw.get("required_capabilities") or [] if str(value)})
        )
        if (
            not prompt_pack_version
            or not model_policy_version
            or not expected_model
            or "responses_api" not in required_capabilities
            or "session_key_header" not in required_capabilities
        ):
            raise ProvisioningError("hermes_cloud_approval_policy_incomplete", retryable=False)
        if (
            isinstance(model_context_window_tokens, bool)
            or not isinstance(model_context_window_tokens, int)
            or model_context_window_tokens <= 0
        ):
            raise ProvisioningError(
                "hermes_cloud_approval_model_context_invalid",
                retryable=False,
            )
        return ApprovedRuntimeManifest(
            policy_commit_sha=policy_commit_sha,
            lane_s_review_url=lane_s_review_url,
            prompt_pack_version=prompt_pack_version,
            model_policy_version=model_policy_version,
            expected_model=expected_model,
            model_context_window_tokens=model_context_window_tokens,
            allowed_tools=allowed_tools,
            required_capabilities=required_capabilities,
            artifact_sha256=normalized_artifacts,
            manifest_sha256=hashlib.sha256(serialized).hexdigest(),
        )


def observed_artifacts(candidate: dict[str, Any], manifest: ApprovedRuntimeManifest) -> dict[str, str]:
    observed = candidate.get("observed_prompt_artifacts")
    if not isinstance(observed, dict):
        raise ProvisioningError("hermes_cloud_observed_artifacts_missing", retryable=False)
    normalized: dict[str, str] = {}
    for name, expected in manifest.artifact_sha256.items():
        digest = str(observed.get(f"{name}_sha256") or "").lower()
        if not SHA256_RE.fullmatch(digest) or not hmac.compare_digest(digest, expected):
            raise ProvisioningError("prompt_artifact_checksum_mismatch", retryable=False)
        normalized[name] = digest
    return normalized
