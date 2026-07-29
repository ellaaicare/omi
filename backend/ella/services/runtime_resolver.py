"""Resolve authenticated users to active, isolated Hermes runtime bindings."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)
from database.runtime_targets import (
    CLOUD_RUNTIME_MODEL,
    CLOUD_RUNTIME_PROVIDER,
    CLOUD_RUNTIME_TARGET_MODES,
    RuntimeTargetLineage,
)
from ella.services.ai_consent import (
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud import (
    HermesCloudClient,
    validate_prompt_artifact_receipt,
)
from ella.services.hermes_cloud_policy import current_cloud_authority
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
    profile_class: str = "real"
    runtime_target_id: str = ""
    runtime_target_mode: str = ""
    runtime_target_updated_at: str = ""
    target_endpoint_ref: str = ""
    target_credential_ref: str = ""
    target_entitlement_revision: int = 0
    # Canonical broker/authority owner coordinates (users.id UUIDs), not omi_uid.
    account_user_id: str = ""
    profile_user_id: str = ""


@dataclass(frozen=True)
class CloudRuntimeAuthorityIdentity:
    """Secret-free identity retained across awaited work for one exact target."""

    uid: str
    target_mode: str
    digest: str


def cloud_runtime_authority_identity(runtime: IsolatedRuntime) -> CloudRuntimeAuthorityIdentity:
    if runtime.provider != CLOUD_RUNTIME_PROVIDER:
        raise ProvisioningError("hermes_cloud_runtime_required", retryable=False)
    if (
        runtime.runtime_target_mode not in CLOUD_RUNTIME_TARGET_MODES
        or not runtime.runtime_target_id
        or not runtime.runtime_target_updated_at
        or not runtime.target_endpoint_ref
        or not runtime.target_credential_ref
        or runtime.target_entitlement_revision < 1
    ):
        raise ProvisioningError("hermes_cloud_runtime_target_identity_missing", retryable=False)
    material = {
        "uid": runtime.uid,
        "binding_id": runtime.binding_id,
        "binding_revision": runtime.revision,
        "runtime_instance_id": runtime.runtime_instance_id,
        "runtime_target_id": runtime.runtime_target_id,
        "runtime_target_mode": runtime.runtime_target_mode,
        "runtime_target_updated_at": runtime.runtime_target_updated_at,
        "target_endpoint_ref": runtime.target_endpoint_ref,
        "target_credential_ref": runtime.target_credential_ref,
        "target_entitlement_revision": runtime.target_entitlement_revision,
        "endpoint_sha256": hashlib.sha256(runtime.gateway_url.encode("utf-8")).hexdigest(),
        "credential_sha256": hashlib.sha256(runtime.gateway_token.encode("utf-8")).hexdigest(),
        "profile_class": runtime.profile_class,
        "profile_name": runtime.profile_name,
        "agent_id": runtime.agent_id,
        "expected_model": runtime.expected_model,
        "prompt_pack_version": runtime.prompt_pack_version,
        "model_policy_version": runtime.model_policy_version,
        "voice_policy_version": runtime.voice_policy_version,
        "policy_commit_sha": runtime.policy_commit_sha,
        "approval_manifest_sha256": runtime.approval_manifest_sha256,
        "allowed_tools": list(runtime.allowed_tools),
        "required_capabilities": list(runtime.required_capabilities),
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CloudRuntimeAuthorityIdentity(
        uid=runtime.uid,
        target_mode=runtime.runtime_target_mode,
        digest=digest,
    )


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
        if any(
            binding.get(field)
            for field in (
                "workspace_root",
                "internal_gateway_url",
                "gateway_port",
                "service_label",
                "credential_ref",
                "honcho_api_key_ref",
            )
        ):
            raise ProvisioningError("cloud_binding_contains_local_runtime", retryable=False)
        if str(binding.get("target_endpoint_ref") or "") != str(binding.get("api_base_url_ref") or ""):
            raise ProvisioningError("cloud_runtime_target_endpoint_mismatch", retryable=False)
        if str(binding.get("target_credential_ref") or "") != str(binding.get("api_key_ref") or ""):
            raise ProvisioningError("cloud_runtime_target_credential_mismatch", retryable=False)
        if str(binding.get("runtime_target_mode") or "") not in CLOUD_RUNTIME_TARGET_MODES:
            raise ProvisioningError("cloud_runtime_target_mode_missing", retryable=False)
        prompt_artifact_receipt = validate_prompt_artifact_receipt(binding)
        workspace_root = ""
        runtime_instance_id = str(binding.get("runtime_instance_id") or "")
        expected_model = str(binding.get("expected_model") or "")
        model_context_window_tokens = int(prompt_artifact_receipt["model_context_window_tokens"])
        if not runtime_instance_id or not expected_model:
            raise ProvisioningError("cloud_runtime_receipt_incomplete", retryable=False)
        authority = current_cloud_authority(
            uid,
            profile_class=str(binding.get("profile_class") or ""),
            profile_uid=uid,
            runtime_provider=provider,
            model_route=f"openai-codex/{expected_model}",
            memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
            photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
        )
        health_receipt = binding.get("health_receipt") or {}
        if isinstance(health_receipt, str):
            try:
                health_receipt = json.loads(health_receipt)
            except json.JSONDecodeError:
                health_receipt = {}
        stored_lineage = RuntimeTargetLineage(
            policy_version=str(
                binding.get("target_policy_version")
                or (health_receipt.get("policy_version") if isinstance(health_receipt, dict) else "")
                or ""
            ),
            processor_set_hash=str(
                binding.get("target_processor_set_hash")
                or (health_receipt.get("processor_set_hash") if isinstance(health_receipt, dict) else "")
                or ""
            ),
            scope_version=str(
                binding.get("target_scope_version")
                or (health_receipt.get("scope_version") if isinstance(health_receipt, dict) else "")
                or ""
            ),
            scope_hash=str(
                binding.get("target_scope_hash")
                or (health_receipt.get("scope_hash") if isinstance(health_receipt, dict) else "")
                or ""
            ),
        )
        if stored_lineage.validate() != authority.lineage:
            raise ProvisioningError("cloud_runtime_target_lineage_stale", retryable=False)
        gateway_url, gateway_token = HermesCloudClient.credentials(binding)
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
    if provider != "hermes_cloud" and not all([honcho_workspace, observed_peer, observer_peer]):
        raise ProvisioningError("honcho_receipt_incomplete", retryable=False)
    revision = int(binding.get("revision") or 0)
    if revision < 1:
        raise ProvisioningError("invalid_binding_revision", retryable=False)

    # Broker and shared advisory-lock scopes use users.id UUIDs from the binding
    # row (account_user_id/profile_user_id), not the OMI auth uid (omi_uid).
    account_user_id = str(binding.get("account_user_id") or "").strip()
    profile_user_id = str(binding.get("profile_user_id") or "").strip()
    if provider == "hermes_cloud" and (not account_user_id or not profile_user_id):
        raise ProvisioningError(
            "hermes_cloud_owner_coordinates_missing",
            retryable=False,
        )

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
        profile_class=str(binding.get("profile_class") or "real").strip().lower(),
        runtime_target_id=str(binding.get("runtime_target_id") or ""),
        runtime_target_mode=str(binding.get("runtime_target_mode") or ""),
        runtime_target_updated_at=str(binding.get("runtime_target_updated_at") or ""),
        target_endpoint_ref=str(binding.get("target_endpoint_ref") or ""),
        target_credential_ref=str(binding.get("target_credential_ref") or ""),
        target_entitlement_revision=int(binding.get("target_entitlement_revision") or 0),
        account_user_id=account_user_id,
        profile_user_id=profile_user_id,
    )


async def resolve_isolated_runtime(
    uid: str,
    repository: Optional[EllaProvisioningRepository] = None,
    target_mode: Optional[str] = None,
) -> Optional[IsolatedRuntime]:
    """Resolve persisted cloud authority first; retained bindings stay flag-gated."""
    cloud_required = cloud_provisioning_enabled(uid)
    retained_required = runtime_bindings_enabled(uid)
    if not cloud_required and not retained_required:
        return None
    repository = repository or await EllaProvisioningRepository.create()
    try:
        if cloud_required:
            if target_mode not in CLOUD_RUNTIME_TARGET_MODES:
                raise ProvisioningError("cloud_runtime_target_mode_required", retryable=False)
            profile_class = await repository.get_cloud_profile_class(uid)
            authority = current_cloud_authority(
                uid,
                profile_class=profile_class,
                profile_uid=uid,
                runtime_provider=CLOUD_RUNTIME_PROVIDER,
                model_route=f"openai-codex/{CLOUD_RUNTIME_MODEL}",
                memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
                photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
            )
            binding = await repository.resolve_active_runtime(
                uid,
                target_mode=target_mode,
                required_provider=CLOUD_RUNTIME_PROVIDER,
                authority_lineage=authority.lineage,
                model=CLOUD_RUNTIME_MODEL,
            )
        else:
            binding = await repository.resolve_active_runtime(
                uid,
                target_mode=target_mode,
                required_provider="hermes",
            )
    except RuntimePoolClaimError as exc:
        raise ProvisioningError(exc.code, retryable=False) from exc
    if binding:
        return runtime_from_binding(binding, uid)
    if not binding:
        try:
            cloud_state = await repository.resolve_cloud_binding_state(uid)
        except Exception:
            if cloud_required:
                raise
            cloud_state = None
        if cloud_required and cloud_state:
            status = str(cloud_state.get("status") or "not_ready").lower()
            raise ProvisioningError(
                f"hermes_cloud_{status}",
                retryable=status in {"claiming", "pool_available"},
            )
    if cloud_required:
        raise ProvisioningError("hermes_cloud_not_provisioned", retryable=True)
    if retained_required:
        raise ProvisioningError("hermes_not_provisioned", retryable=True)
    return None


async def revalidate_cloud_runtime_authority(
    identity: CloudRuntimeAuthorityIdentity,
    repository: Optional[EllaProvisioningRepository] = None,
) -> IsolatedRuntime:
    """Resolve current SQL/consent authority and reject any target identity drift."""
    current = await resolve_isolated_runtime(
        identity.uid,
        repository=repository,
        target_mode=identity.target_mode,
    )
    if current is None or current.provider != CLOUD_RUNTIME_PROVIDER:
        raise ProvisioningError("hermes_cloud_runtime_required", retryable=False)
    current_identity = cloud_runtime_authority_identity(current)
    if not hmac.compare_digest(current_identity.digest, identity.digest):
        raise ProvisioningError("hermes_cloud_runtime_authority_changed", retryable=False)
    return current
