"""Resolve authenticated users to active, isolated Hermes runtime bindings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from database.ella_provisioning import EllaProvisioningRepository
from ella.services.provisioning import (
    PROFILE_NAME_RE,
    ProvisioningError,
    resolve_gateway_credential,
    validate_internal_gateway_url,
)


def runtime_bindings_enabled() -> bool:
    return os.getenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class IsolatedRuntime:
    uid: str
    profile_name: str
    agent_id: str
    gateway_url: str
    gateway_token: str
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

    return IsolatedRuntime(
        uid=uid,
        profile_name=profile_name,
        agent_id=agent_id,
        gateway_url=validate_internal_gateway_url(str(binding.get("internal_gateway_url") or "")),
        gateway_token=resolve_gateway_credential(binding.get("credential_ref")),
        model_policy_version=str(binding.get("model_policy_version") or ""),
        voice_policy_version=str(binding.get("voice_policy_version") or ""),
        revision=int(binding.get("revision") or 0),
    )


async def resolve_isolated_runtime(
    uid: str,
    repository: Optional[EllaProvisioningRepository] = None,
) -> Optional[IsolatedRuntime]:
    """Return None while shadow mode is disabled; otherwise fail closed."""
    if not runtime_bindings_enabled():
        return None
    repository = repository or await EllaProvisioningRepository.create()
    binding = await repository.resolve_active_runtime(uid)
    if not binding:
        raise ProvisioningError("hermes_not_provisioned", retryable=True)
    return runtime_from_binding(binding, uid)
