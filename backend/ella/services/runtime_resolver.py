"""Resolve authenticated users to active, isolated Hermes runtime bindings."""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from database.ella_provisioning import EllaProvisioningRepository
from ella.services.hermes_cloud import (
    HermesCloudClient,
    validate_prompt_artifact_receipt,
)
from ella.services.hermes_cloud_policy import assert_cloud_identity_gate
from ella.services.provisioning import (
    PROFILE_NAME_RE,
    ProvisioningError,
    cloud_provisioning_enabled,
    resolve_gateway_credential,
    rollout_enabled,
    validate_internal_gateway_url,
)


def runtime_bindings_enabled(uid: Optional[str] = None) -> bool:
    return rollout_enabled(
        "ELLA_RUNTIME_BINDINGS_ENABLED",
        "ELLA_RUNTIME_BINDINGS_ENABLED_UIDS",
        uid,
    )


def runtime_authority_enabled(uid: Optional[str] = None) -> bool:
    """Return whether this user must resolve through persisted runtime authority."""
    return runtime_bindings_enabled(uid) or cloud_provisioning_enabled(uid)


@dataclass(frozen=True)
class IsolatedRuntime:
    uid: str
    binding_id: str
    provider: str
    status: str
    profile_name: str
    agent_id: str
    runtime_instance_id: str
    gateway_url: str
    gateway_token: str
    workspace_root: str
    honcho_workspace: str
    observed_peer: str
    observer_peer: str
    prompt_pack_version: str
    expected_model: str
    model_context_window_tokens: int
    allowed_tools: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    model_policy_version: str
    voice_policy_version: str
    revision: int
    policy_commit_sha: str = ""
    approval_manifest_sha256: str = ""


def runtime_from_binding(binding: dict, uid: str, *, allow_shadow: bool = False) -> IsolatedRuntime:
    if binding.get("omi_uid") != uid:
        raise ProvisioningError("runtime_ownership_mismatch", retryable=False)
    provider = str(binding.get("provider") or "").lower()
    if provider not in {"hermes", "hermes_cloud"}:
        raise ProvisioningError("invalid_runtime_provider", retryable=False)
    status = str(binding.get("status") or ("active" if binding.get("active") else "disabled")).lower()
    shadow_allowed = provider == "hermes_cloud" and status == "shadow" and allow_shadow
    if (binding.get("active") is not True and not shadow_allowed) or binding.get("health_state") != "healthy":
        raise ProvisioningError("runtime_not_ready", retryable=True)
    if provider == "hermes_cloud" and (
        status not in {"shadow", "internal_canary", "active"} or (status == "shadow" and not allow_shadow)
    ):
        raise ProvisioningError(f"hermes_cloud_{status or 'not_ready'}", retryable=status == "claiming")

    profile_name = str(binding.get("profile_name") or "")
    agent_id = str(binding.get("agent_id") or "")
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise ProvisioningError("invalid_profile_name", retryable=False)
    if not agent_id:
        raise ProvisioningError("runtime_receipt_incomplete", retryable=False)
    plato_uid = os.getenv("ELLA_PLATO_UID", "").strip()
    if profile_name == "plato-eval" and uid != plato_uid:
        raise ProvisioningError("plato_binding_forbidden", retryable=False)

    if provider == "hermes_cloud":
        assert_cloud_identity_gate(uid)
        if any(
            binding.get(field)
            for field in ("workspace_root", "internal_gateway_url", "gateway_port", "service_label", "credential_ref")
        ):
            raise ProvisioningError("cloud_binding_contains_local_runtime", retryable=False)
        gateway_url, gateway_token = HermesCloudClient.credentials(binding)
        prompt_artifact_receipt = validate_prompt_artifact_receipt(binding)
        workspace_root = ""
        runtime_instance_id = str(binding.get("runtime_instance_id") or "")
        expected_model = str(binding.get("expected_model") or "")
        model_context_window_tokens = int(prompt_artifact_receipt["model_context_window_tokens"])
        if not runtime_instance_id or not expected_model:
            raise ProvisioningError("cloud_runtime_receipt_incomplete", retryable=False)
    else:
        workspace_root = str(binding.get("workspace_root") or "")
        profiles_root = os.getenv("ELLA_HERMES_PROFILES_ROOT", "/Users/ellaai/.hermes/profiles")
        expected_workspace = posixpath.normpath(f"{profiles_root.rstrip('/')}/{profile_name}/workspace")
        if posixpath.normpath(workspace_root) != expected_workspace:
            raise ProvisioningError("workspace_ownership_mismatch", retryable=False)

        gateway_url = validate_internal_gateway_url(str(binding.get("internal_gateway_url") or ""))
        try:
            gateway_port = int(binding.get("gateway_port") or 0)
        except (TypeError, ValueError) as exc:
            raise ProvisioningError("invalid_gateway_port", retryable=False) from exc
        if not 1024 <= gateway_port <= 65535 or urlparse(gateway_url).port != gateway_port:
            raise ProvisioningError("gateway_port_mismatch", retryable=False)
        gateway_token = resolve_gateway_credential(binding.get("credential_ref"))
        runtime_instance_id = ""
        expected_model = str(binding.get("agent_id") or "")
        model_context_window_tokens = 0

    honcho_workspace = str(binding.get("honcho_workspace") or "")
    observed_peer = str(binding.get("observed_peer") or "")
    observer_peer = str(binding.get("observer_peer") or "")
    if not all([honcho_workspace, observed_peer, observer_peer]):
        raise ProvisioningError("honcho_receipt_incomplete", retryable=False)
    revision = int(binding.get("revision") or 0)
    if revision < 1:
        raise ProvisioningError("invalid_binding_revision", retryable=False)

    return IsolatedRuntime(
        uid=uid,
        binding_id=str(binding.get("id") or ""),
        provider=provider,
        status=status,
        profile_name=profile_name,
        agent_id=agent_id,
        runtime_instance_id=runtime_instance_id,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        workspace_root=workspace_root,
        honcho_workspace=honcho_workspace,
        observed_peer=observed_peer,
        observer_peer=observer_peer,
        prompt_pack_version=str(binding.get("prompt_pack_version") or binding.get("template_version") or ""),
        expected_model=expected_model,
        model_context_window_tokens=model_context_window_tokens,
        allowed_tools=tuple(sorted(str(item) for item in (binding.get("allowed_tools") or []))),
        required_capabilities=tuple(sorted(str(item) for item in (binding.get("required_capabilities") or []))),
        model_policy_version=str(binding.get("model_policy_version") or ""),
        voice_policy_version=str(binding.get("voice_policy_version") or ""),
        revision=revision,
        policy_commit_sha=(
            str(prompt_artifact_receipt.get("policy_commit_sha") or "") if provider == "hermes_cloud" else ""
        ),
        approval_manifest_sha256=(
            str(prompt_artifact_receipt.get("approval_manifest_sha256") or "") if provider == "hermes_cloud" else ""
        ),
    )


async def resolve_isolated_runtime(
    uid: str,
    repository: Optional[EllaProvisioningRepository] = None,
) -> Optional[IsolatedRuntime]:
    """Resolve persisted cloud authority first; retained bindings stay flag-gated."""
    repository = repository or await EllaProvisioningRepository.create()
    try:
        binding = await repository.resolve_active_runtime(uid)
    except Exception:
        if runtime_authority_enabled(uid):
            raise
        return None
    if binding and str(binding.get("provider") or "").lower() == "hermes_cloud":
        return runtime_from_binding(binding, uid)
    if binding and runtime_bindings_enabled(uid):
        return runtime_from_binding(binding, uid)
    if not binding:
        try:
            cloud_state = await repository.resolve_cloud_binding_state(uid)
        except Exception:
            if cloud_provisioning_enabled(uid):
                raise
            cloud_state = None
        if cloud_state:
            status = str(cloud_state.get("status") or "not_ready").lower()
            raise ProvisioningError(
                f"hermes_cloud_{status}",
                retryable=status in {"claiming", "pool_available"},
            )
    if cloud_provisioning_enabled(uid):
        raise ProvisioningError("hermes_cloud_not_provisioned", retryable=True)
    if runtime_bindings_enabled(uid):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)
    return None
