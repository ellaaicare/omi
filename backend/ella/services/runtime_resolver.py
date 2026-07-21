"""Resolve authenticated users to active, isolated Hermes runtime bindings."""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from database.ella_provisioning import EllaProvisioningRepository
from ella.services.provisioning import (
    PROFILE_NAME_RE,
    ProvisioningError,
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


@dataclass(frozen=True)
class IsolatedRuntime:
    uid: str
    profile_name: str
    agent_id: str
    gateway_url: str
    gateway_token: str
    workspace_root: str
    honcho_workspace: str
    observed_peer: str
    observer_peer: str
    model_policy_version: str
    voice_policy_version: str
    revision: int


def runtime_from_binding(binding: dict, uid: str) -> IsolatedRuntime:
    if binding.get("omi_uid") != uid:
        raise ProvisioningError("runtime_ownership_mismatch", retryable=False)
    if str(binding.get("provider") or "").lower() != "hermes":
        raise ProvisioningError("invalid_runtime_provider", retryable=False)
    if binding.get("active") is not True or binding.get("health_state") != "healthy":
        raise ProvisioningError("runtime_not_ready", retryable=True)

    profile_name = str(binding.get("profile_name") or "")
    agent_id = str(binding.get("agent_id") or "")
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise ProvisioningError("invalid_profile_name", retryable=False)
    if not agent_id:
        raise ProvisioningError("runtime_receipt_incomplete", retryable=False)
    plato_uid = os.getenv("ELLA_PLATO_UID", "").strip()
    if profile_name == "plato-eval" and uid != plato_uid:
        raise ProvisioningError("plato_binding_forbidden", retryable=False)

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
        profile_name=profile_name,
        agent_id=agent_id,
        gateway_url=gateway_url,
        gateway_token=resolve_gateway_credential(binding.get("credential_ref")),
        workspace_root=workspace_root,
        honcho_workspace=honcho_workspace,
        observed_peer=observed_peer,
        observer_peer=observer_peer,
        model_policy_version=str(binding.get("model_policy_version") or ""),
        voice_policy_version=str(binding.get("voice_policy_version") or ""),
        revision=revision,
    )


async def resolve_isolated_runtime(
    uid: str,
    repository: Optional[EllaProvisioningRepository] = None,
) -> Optional[IsolatedRuntime]:
    """Return None while shadow mode is disabled; otherwise fail closed."""
    if not runtime_bindings_enabled(uid):
        return None
    repository = repository or await EllaProvisioningRepository.create()
    binding = await repository.resolve_active_runtime(uid)
    if not binding:
        raise ProvisioningError("hermes_not_provisioned", retryable=True)
    return runtime_from_binding(binding, uid)
