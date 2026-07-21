"""Hermes-only Ella onboarding orchestration and public receipts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from database.ella_provisioning import (
    EllaProvisioningRepository,
    ProvisioningSchemaNotReadyError,
)

DEFAULT_TARGET_SCHEMA_VERSION = "hermes-user-v1"
DEFAULT_TEMPLATE_VERSION = "hermes-user-v1"
DEFAULT_MODEL_POLICY_VERSION = "frontier-v1"
DEFAULT_VOICE_POLICY_VERSION = "ella-voice-v1"
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
ALLOWED_CREDENTIAL_ENV_RE = re.compile(r"^(HERMES_API_SERVER_KEY|ELLA_HERMES_GATEWAY_KEY_[A-Z0-9_]+)$")
logger = logging.getLogger("ella.provisioning")
TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_PROVISION_TIMEOUT_SECONDS = 180.0
MIN_PROVISION_TIMEOUT_SECONDS = 30.0
MAX_PROVISION_TIMEOUT_SECONDS = 300.0


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, detail: Optional[dict[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    email: str
    name: str
    timezone: str


def rollout_enabled(global_flag: str, uid_allowlist: str, uid: Optional[str] = None) -> bool:
    """Enable a rollout globally or for an exact Firebase UID canary."""
    if os.getenv(global_flag, "false").strip().lower() in TRUE_VALUES:
        return True
    if not uid:
        return False
    allowed_uids = {value.strip() for value in os.getenv(uid_allowlist, "").split(",") if value.strip()}
    return uid in allowed_uids


def provisioning_enabled(uid: Optional[str] = None) -> bool:
    return rollout_enabled(
        "ELLA_HERMES_PROVISIONING_ENABLED",
        "ELLA_HERMES_PROVISIONING_ENABLED_UIDS",
        uid,
    )


def provision_timeout_seconds() -> float:
    """Return a bounded deadline that covers observed cold Hermes starts."""
    raw = os.getenv("ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_PROVISION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS=%r; using %.0fs",
            raw,
            DEFAULT_PROVISION_TIMEOUT_SECONDS,
        )
        return DEFAULT_PROVISION_TIMEOUT_SECONDS
    if not math.isfinite(value):
        logger.warning(
            "Non-finite ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS=%r; using %.0fs",
            raw,
            DEFAULT_PROVISION_TIMEOUT_SECONDS,
        )
        return DEFAULT_PROVISION_TIMEOUT_SECONDS
    return min(MAX_PROVISION_TIMEOUT_SECONDS, max(MIN_PROVISION_TIMEOUT_SECONDS, value))


def stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_receipt(job: dict[str, Any], binding: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    state = str(job.get("state") or "pending")
    stage = str(job.get("stage") or "identity_ready")
    public_state = {
        "pending": "queued",
        "provisioning": "provisioning",
        "ready": "ready",
        "degraded": "degraded",
        "blocked": "blocked",
    }.get(state, "degraded")
    public_stage = {
        "identity_ready": "preparing_account",
        "profile_ready": "preparing_memory",
        "runtime_ready": "starting_assistant",
        "smoke_passed": "validating",
        "active": "ready",
    }.get(stage, "validating")
    job_id = str(job["id"])
    result = {
        "job_id": job_id,
        "state": public_state,
        "stage": public_stage,
        "retryable": bool(job.get("retryable", True)),
        "retry_after_ms": 2000 if public_state in {"queued", "provisioning", "degraded"} else None,
        "support_code": f"ELLA-{job_id.split('-')[0].upper()}",
        "target_schema_version": job["target_schema_version"],
        "binding_state": "active" if binding and binding.get("active") else "inactive",
        "binding_revision": int(binding.get("revision", 0)) if binding else 0,
        "effective_policy_revision": (
            f"{binding['model_policy_version']}:{binding['voice_policy_version']}" if binding else None
        ),
    }
    if job.get("error_code"):
        result["error_code"] = job["error_code"]
    return result


def validate_gateway_credential_ref(credential_ref: Optional[str]) -> str:
    if not credential_ref or not credential_ref.startswith("env:"):
        raise ProvisioningError("invalid_credential_reference", retryable=False)
    variable = credential_ref[4:]
    if not ALLOWED_CREDENTIAL_ENV_RE.fullmatch(variable):
        raise ProvisioningError("invalid_credential_reference", retryable=False)
    return variable


def resolve_gateway_credential(credential_ref: Optional[str]) -> str:
    variable = validate_gateway_credential_ref(credential_ref)
    value = os.getenv(variable, "")
    if not value:
        raise ProvisioningError("gateway_credential_unavailable", retryable=True)
    return value


def validate_internal_gateway_url(value: str) -> str:
    """Accept only loopback, tailnet, or explicitly allowlisted internal gateways."""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisioningError("invalid_internal_gateway_url", retryable=False)

    allowlisted = {
        item.strip().lower() for item in os.getenv("ELLA_HERMES_GATEWAY_ALLOWED_HOSTS", "").split(",") if item.strip()
    }
    allowed = host in {"localhost", "127.0.0.1", "::1"} or host in allowlisted
    if not allowed:
        try:
            allowed = ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            allowed = False
    if not allowed:
        raise ProvisioningError("invalid_internal_gateway_url", retryable=False)
    return value.rstrip("/")


class HermesProvisionClient:
    def __init__(self, *, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or os.getenv("ELLA_HERMES_PROVISION_API_URL", "http://100.76.138.56:8210")).rstrip(
            "/"
        )
        self.token = token if token is not None else os.getenv("ELLA_HERMES_PROVISION_API_TOKEN", "")

    async def provision(self, identity: VerifiedIdentity, target_schema_version: str) -> dict[str, Any]:
        if not self.token:
            raise ProvisioningError("provision_service_credential_unavailable", retryable=True)
        payload = {
            "userId": identity.uid,
            "omiUid": identity.uid,
            "firebaseUid": identity.uid,
            "email": identity.email,
            "label": identity.name,
            "timezone": identity.timezone,
            "hermes_only": True,
            "targetSchemaVersion": target_schema_version,
        }
        try:
            async with httpx.AsyncClient(timeout=provision_timeout_seconds()) as client:
                response = await client.post(
                    f"{self.base_url}/provision",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ProvisioningError("provision_service_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProvisioningError("provision_service_unavailable", retryable=True) from exc

        if response.status_code == 409:
            raise ProvisioningError("runtime_capacity", retryable=True)
        if response.status_code in {401, 403}:
            raise ProvisioningError("provision_service_auth_failed", retryable=False)
        if response.status_code >= 500:
            raise ProvisioningError("provision_service_unavailable", retryable=True)
        if response.status_code >= 400:
            raise ProvisioningError(
                "provision_request_rejected", retryable=False, detail={"status": response.status_code}
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise ProvisioningError("invalid_provision_receipt", retryable=True) from exc
        if result.get("provisionMode") != "hermes_only" or result.get("mode") != "hermes_only":
            raise ProvisioningError("non_hermes_provision_rejected", retryable=False)
        return result


def extract_runtime_binding(
    result: dict[str, Any],
    uid: str,
    expected_template_version: Optional[str] = None,
) -> dict[str, Any]:
    raw = result.get("runtimeBinding") or result.get("runtime_binding")
    if not isinstance(raw, dict):
        raise ProvisioningError("runtime_receipt_missing", retryable=True)

    provider = str(raw.get("provider") or "").lower()
    profile_name = str(raw.get("profileName") or raw.get("profile_name") or "")
    agent_id = str(raw.get("agentId") or raw.get("agent_id") or "")
    workspace_root = raw.get("workspaceRoot") or raw.get("workspace_root")
    internal_gateway_url = raw.get("internalGatewayUrl") or raw.get("internal_gateway_url")
    gateway_port = raw.get("gatewayPort") or raw.get("gateway_port")
    service_label = raw.get("serviceLabel") or raw.get("service_label")
    credential_ref = raw.get("credentialRef") or raw.get("credential_ref")
    health_state = str(raw.get("healthState") or raw.get("health_state") or "").lower()
    health_receipt = raw.get("healthReceipt") or raw.get("health_receipt") or {}
    if not isinstance(health_receipt, dict):
        raise ProvisioningError("invalid_health_receipt", retryable=True)
    smoke_values = []
    for source, key in (
        (raw, "smokePassed"),
        (raw, "smoke_passed"),
        (health_receipt, "smokePassed"),
        (health_receipt, "smoke_passed"),
    ):
        if key in source:
            smoke_values.append(source[key])
    smoke_passed = bool(smoke_values) and all(value is True for value in smoke_values)

    if provider != "hermes":
        raise ProvisioningError("invalid_runtime_provider", retryable=False)
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise ProvisioningError("invalid_profile_name", retryable=False)
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise ProvisioningError("invalid_agent_id", retryable=False)
    plato_uid = os.getenv("ELLA_PLATO_UID", "").strip()
    if profile_name == "plato-eval" and uid != plato_uid:
        raise ProvisioningError("plato_binding_forbidden", retryable=False)
    if not all([agent_id, workspace_root, internal_gateway_url, gateway_port, service_label, credential_ref]):
        raise ProvisioningError("runtime_receipt_incomplete", retryable=True)
    if health_state != "healthy" or not smoke_passed:
        raise ProvisioningError("runtime_smoke_incomplete", retryable=True)

    honcho = raw.get("honcho") if isinstance(raw.get("honcho"), dict) else {}
    honcho_workspace = honcho.get("workspace")
    observed_peer = honcho.get("observedPeer") or honcho.get("observed_peer")
    observer_peer = honcho.get("observerPeer") or honcho.get("observer_peer")
    if not all([honcho_workspace, observed_peer, observer_peer]):
        raise ProvisioningError("honcho_receipt_incomplete", retryable=True)

    profiles_root = os.getenv("ELLA_HERMES_PROFILES_ROOT", "/Users/ellaai/.hermes/profiles")
    expected_workspace = posixpath.normpath(f"{profiles_root.rstrip('/')}/{profile_name}/workspace")
    if posixpath.normpath(str(workspace_root)) != expected_workspace:
        raise ProvisioningError("workspace_ownership_mismatch", retryable=False)

    try:
        parsed_port = int(gateway_port)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("invalid_gateway_port", retryable=False) from exc
    gateway_url = validate_internal_gateway_url(str(internal_gateway_url))
    if not 1024 <= parsed_port <= 65535 or urlparse(gateway_url).port != parsed_port:
        raise ProvisioningError("gateway_port_mismatch", retryable=False)
    validate_gateway_credential_ref(str(credential_ref))

    template_version = str(raw.get("templateVersion") or DEFAULT_TEMPLATE_VERSION)
    if expected_template_version and template_version != expected_template_version:
        raise ProvisioningError("runtime_template_version_mismatch", retryable=True)

    return {
        "role": "user",
        "provider": "hermes",
        "profile_name": profile_name,
        "agent_id": agent_id,
        "workspace_root": str(workspace_root),
        "internal_gateway_url": gateway_url,
        "gateway_port": parsed_port,
        "service_label": str(service_label),
        "credential_ref": str(credential_ref),
        "honcho_workspace": str(honcho_workspace),
        "observed_peer": str(observed_peer),
        "observer_peer": str(observer_peer),
        "template_version": template_version,
        "model_policy_version": str(raw.get("modelPolicyVersion") or DEFAULT_MODEL_POLICY_VERSION),
        "voice_policy_version": str(raw.get("voicePolicyVersion") or DEFAULT_VOICE_POLICY_VERSION),
        "health_state": "healthy",
        "health_receipt": health_receipt,
    }


class ProvisioningCoordinator:
    def __init__(
        self,
        repository: EllaProvisioningRepository,
        client: Optional[HermesProvisionClient] = None,
    ):
        self.repository = repository
        self.client = client or HermesProvisionClient()

    async def ensure_job(
        self,
        *,
        identity: VerifiedIdentity,
        target_schema_version: str,
        client_request_id: Optional[str],
        request_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]], bool]:
        try:
            await self.repository.assert_schema_ready()
        except ProvisioningSchemaNotReadyError as exc:
            logger.error("Ella provisioning schema is incomplete: %s", ", ".join(exc.missing))
            raise ProvisioningError("provisioning_schema_not_ready", retryable=True) from exc

        await self.repository.ensure_user_identity(
            uid=identity.uid,
            email=identity.email,
            name=identity.name,
            timezone_name=identity.timezone,
        )
        job = await self.repository.acquire_job(
            uid=identity.uid,
            target_schema_version=target_schema_version,
            client_request_id=client_request_id,
            request_payload_hash=stable_payload_hash(request_payload),
        )
        try:
            await self.repository.ensure_omi_user_document(
                uid=identity.uid,
                email=identity.email,
                name=identity.name,
                timezone_name=identity.timezone,
            )
        except Exception:
            logger.exception("OMI identity initialization failed for uid=%s", identity.uid)
            job = await self.repository.update_job(
                job_id=str(job["id"]),
                state="degraded",
                stage="identity_ready",
                retryable=True,
                error_code="omi_identity_unavailable",
            )
            return job, None, False
        binding = await self.repository.resolve_active_runtime(
            identity.uid,
            template_version=target_schema_version,
        )
        if binding:
            if str(binding.get("user_status") or "") != "ACTIVE":
                await self.repository.activate_user(identity.uid)
            if job.get("state") != "ready":
                job = await self.repository.update_job(
                    job_id=str(job["id"]),
                    state="ready",
                    stage="active",
                    retryable=False,
                    receipt={"type": "active_binding_reconciled", "binding_revision": binding["revision"]},
                )
            return job, binding, False
        if not provisioning_enabled(identity.uid):
            job = await self.repository.update_job(
                job_id=str(job["id"]),
                state="degraded",
                stage="identity_ready",
                retryable=True,
                error_code="provisioning_disabled",
            )
            return job, None, False
        claimed = await self.repository.claim_job(str(job["id"]))
        return claimed or job, None, claimed is not None

    async def process_claimed_job(self, *, job: dict[str, Any], identity: VerifiedIdentity) -> None:
        try:
            result = await self.client.provision(identity, str(job["target_schema_version"]))
            binding_data = extract_runtime_binding(
                result,
                identity.uid,
                expected_template_version=str(job["target_schema_version"]),
            )
            binding = await self.repository.stage_runtime_binding(uid=identity.uid, binding=binding_data)
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="provisioning",
                stage="smoke_passed",
                retryable=True,
                receipt={
                    "type": "runtime_smoke_passed",
                    "profile": binding["profile_name"],
                    "binding_revision": binding["revision"],
                },
            )
            activated = await self.repository.activate_runtime_binding(uid=identity.uid, provider="hermes")
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="ready",
                stage="active",
                retryable=False,
                receipt={
                    "type": "binding_activated",
                    "binding_revision": activated["revision"],
                },
            )
        except ProvisioningError as exc:
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="degraded" if exc.retryable else "blocked",
                stage="runtime_ready",
                retryable=exc.retryable,
                error_code=exc.code,
                error_detail=exc.detail,
            )
        except Exception:
            logger.exception("Unexpected Hermes provisioning failure for uid=%s", identity.uid)
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="degraded",
                stage="runtime_ready",
                retryable=True,
                error_code="provisioning_internal_error",
            )
