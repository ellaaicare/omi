"""Hermes-only Ella onboarding orchestration and public receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from database import app_settings as app_settings_db
from database import voice_canary as voice_canary_db
from database.honcho_attestation import (
    ATTESTATION_TTL_SECONDS,
    HonchoAttestationError,
    create_challenge,
    observed_runtime_fields,
    verify_attestation,
)
from database.ella_provisioning import (
    EllaProvisioningRepository,
    ProvisioningSchemaNotReadyError,
    RuntimePoolClaimError,
)
from database.runtime_targets import (
    CLOUD_RUNTIME_MODEL,
    CLOUD_RUNTIME_PROVIDER,
    RuntimeTargetLineage,
    SELF_HOSTED_RUNTIME_MODEL,
    SELF_HOSTED_RUNTIME_PROVIDER,
    SELF_HOSTED_RUNTIME_TARGET_MODES,
)
from ella.services.ai_consent import (
    CURRENT_POLICY_VERSION,
    CURRENT_PROCESSOR_SET_HASH,
    CURRENT_SCOPE_HASH,
    CURRENT_SCOPE_VERSION,
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
)
from ella.services.hermes_cloud import (
    HermesCloudClient,
    HermesCloudPreflight,
    RuntimePoolAlertPublisher,
)
from ella.services.hermes_cloud_policy import (
    cloud_synthetic_only,
    current_cloud_authority,
)
from ella.services.hermes_cloud_staged_attestation import StagedAttestationVerifier
from ella.services.runtime_errors import ProvisioningError
from ella.utils.provision_authority import (
    ProvisionAuthority,
    ProvisionAuthorityError,
    ProvisionAuthoritySnapshot,
    hermes_provision_authority,
)

DEFAULT_TARGET_SCHEMA_VERSION = "hermes-user-v1"
CLOUD_TARGET_SCHEMA_VERSION = "hermes-cloud-user-v1"
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
DEFAULT_ATTESTATION_VERIFICATION_GRACE_SECONDS = 30.0
ATTESTATION_VERIFICATION_GRACE_ENV = "ELLA_HERMES_PROVISION_ATTESTATION_VERIFICATION_GRACE_SECONDS"
ATTESTATION_CLOCK_SKEW_MARGIN_SECONDS = 1.0
MAX_PROVISION_RESPONSE_BYTES = 262_144
PROVISION_CONFLICT_FALLBACK_CODE = "provision_request_conflict"
PROVISION_CONFLICT_CODES = frozenset(
    {
        "agent_owner_mapping_conflict",
        "agent_workspace_mapping_conflict",
        "agent_workspace_outside_profiles_root",
        "honcho_config_invalid",
        "honcho_identity_conflict",
        "honcho_profile_map_alias_conflict",
        "honcho_profile_map_conflict",
        "invalid_profile_name",
        "legacy_caregiver_profile_requires_migration",
        "legacy_profile_requires_migration",
        "plato_runtime_rollback_forbidden",
        "profile_ownership_conflict",
        "profile_ownership_invalid",
        "registry_identity_conflict",
        "runtime_profile_identity_conflict",
        "runtime_profile_identity_unmanaged",
        "runtime_profile_config_invalid",
        "runtime_policy_conflict",
        "runtime_reservation_missing",
        "runtime_profile_soul_drift",
        "unowned_profile_exists",
        "unsafe_existing_profile_capabilities",
        "unsafe_profile_ownership_path",
        "unsafe_profile_path",
    }
)
RETAINED_COMPATIBILITY_POLICY_REVISION = "retained-compatibility-v1"
DEFAULT_CLOUD_POOL_CLAIM_LEASE_SECONDS = 120
DEFAULT_CLOUD_POOL_LOW_WATER = 2


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    email: str
    name: str
    timezone: str


@dataclass(frozen=True)
class ProvisionDeadline:
    provider_timeout_seconds: float
    verification_grace_seconds: float
    total_seconds: float


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


def cloud_provisioning_enabled(uid: Optional[str] = None) -> bool:
    return rollout_enabled(
        "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
        "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
        uid,
    )


def self_hosted_provisioning_configured() -> bool:
    """Return the operator master switch without granting any account access."""
    return os.getenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "false").strip().lower() in TRUE_VALUES


def self_hosted_fresh_uid_relax_enabled() -> bool:
    """INTERIM relax (Plato, 2026-08-07, elle-ai #1189).

    Return True when the operator has asked us to admit brand-new self-hosted
    UIDs that hold no matching invitation admission yet. This is explicitly
    temporary: payload is to get a fully functional fresh user -> provision
    hermes -> isolated account with chat/voice/memories -> delete-account
    working end-to-end. Once E2E is proven, invitation gating is re-added by
    unsetting/unsetting this flag (the strict path below is kept intact).
    """
    return os.getenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "false").strip().lower() in TRUE_VALUES


def current_self_hosted_runtime_lineage() -> RuntimeTargetLineage:
    """Return the exact invitation consent lineage required by local Hermes."""
    return RuntimeTargetLineage(
        policy_version=CURRENT_POLICY_VERSION,
        processor_set_hash=CURRENT_PROCESSOR_SET_HASH,
        scope_version=CURRENT_SCOPE_VERSION,
        scope_hash=CURRENT_SCOPE_HASH,
    ).validate()


def self_hosted_provisioning_enabled(
    uid: Optional[str] = None,
    *,
    admission: Optional[dict[str, Any]] = None,
) -> bool:
    """Admit only the exact UID backed by current invitation authority."""
    if not self_hosted_provisioning_configured() or not uid:
        return False
    if admission is None and self_hosted_fresh_uid_relax_enabled():
        # INTERIM relax (Plato #1189): a brand-new self-hosted UID that has no
        # admission record yet is admitted so the fresh-user flow (consent ->
        # ensure -> provision -> isolated account) can run end-to-end. Invitation
        # gating is re-added by unsetting ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID.
        return True
    if not admission:
        return False
    return hmac.compare_digest(str(admission.get("omi_uid") or ""), uid) and _self_hosted_invitation_matches(admission)


async def self_hosted_invitation_admission(
    uid: str,
    *,
    repository: Optional[EllaProvisioningRepository] = None,
) -> Optional[dict[str, Any]]:
    """Load current account-scoped invitation authority without caching revocable state."""
    if not self_hosted_provisioning_configured() or not uid:
        return None
    try:
        repository = repository or await EllaProvisioningRepository.create()
        admission = await repository.get_self_hosted_invitation_admission(uid)
    except ProvisioningSchemaNotReadyError as exc:
        raise ProvisioningError("provisioning_schema_not_ready", retryable=True) from exc
    except ProvisioningError:
        raise
    except Exception as exc:
        raise ProvisioningError("self_hosted_invitation_authority_unavailable", retryable=True) from exc
    if not self_hosted_provisioning_enabled(uid, admission=admission):
        return None
    return admission


async def self_hosted_runtime_authority_required(
    uid: str,
    *,
    repository: Optional[EllaProvisioningRepository] = None,
) -> bool:
    """Detect sticky invitation ownership independently of rollout switches."""
    if not uid:
        return False
    try:
        repository = repository or await EllaProvisioningRepository.create()
        return await repository.has_invitation_owned_self_hosted_runtime(uid)
    except ProvisioningSchemaNotReadyError as exc:
        raise ProvisioningError("provisioning_schema_not_ready", retryable=True) from exc
    except ProvisioningError:
        raise
    except Exception as exc:
        raise ProvisioningError("self_hosted_invitation_authority_unavailable", retryable=True) from exc


def any_provisioning_enabled(
    uid: Optional[str] = None,
    *,
    self_hosted_admission: Optional[dict[str, Any]] = None,
) -> bool:
    return (
        cloud_provisioning_enabled(uid)
        or provisioning_enabled(uid)
        or self_hosted_provisioning_enabled(uid, admission=self_hosted_admission)
    )


def _self_hosted_invitation_matches(admission: Optional[dict[str, Any]]) -> bool:
    if not admission:
        return False
    fallback_policy = admission.get("fallback_policy")
    if isinstance(fallback_policy, str):
        try:
            fallback_policy = json.loads(fallback_policy)
        except json.JSONDecodeError:
            return False
    return bool(
        str(admission.get("consent_policy_version") or "") == CURRENT_POLICY_VERSION
        and str(admission.get("consent_processor_set_hash") or "") == CURRENT_PROCESSOR_SET_HASH
        and str(admission.get("consent_scope_version") or "") == CURRENT_SCOPE_VERSION
        and str(admission.get("consent_scope_hash") or "") == CURRENT_SCOPE_HASH
        and list(admission.get("provider_allowlist") or []) == [SELF_HOSTED_RUNTIME_PROVIDER]
        and list(admission.get("model_allowlist") or []) == [SELF_HOSTED_RUNTIME_MODEL]
        and list(admission.get("mode_allowlist") or []) == list(SELF_HOSTED_RUNTIME_TARGET_MODES)
        and fallback_policy == {"enabled": False, "order": []}
    )


def effective_target_schema_version(uid: str, requested: str) -> str:
    if not cloud_provisioning_enabled(uid):
        return requested
    if requested not in {DEFAULT_TARGET_SCHEMA_VERSION, CLOUD_TARGET_SCHEMA_VERSION}:
        raise ProvisioningError("cloud_target_schema_version_required", retryable=False)
    return CLOUD_TARGET_SCHEMA_VERSION


def cloud_pool_claim_lease_seconds() -> int:
    raw = os.getenv("ELLA_HERMES_CLOUD_POOL_CLAIM_LEASE_SECONDS", "").strip()
    try:
        parsed = int(raw) if raw else DEFAULT_CLOUD_POOL_CLAIM_LEASE_SECONDS
    except ValueError:
        parsed = DEFAULT_CLOUD_POOL_CLAIM_LEASE_SECONDS
    return min(600, max(30, parsed))


def cloud_pool_low_water_threshold() -> int:
    raw = os.getenv("ELLA_HERMES_CLOUD_POOL_LOW_WATER", "").strip()
    try:
        parsed = int(raw) if raw else DEFAULT_CLOUD_POOL_LOW_WATER
    except ValueError:
        parsed = DEFAULT_CLOUD_POOL_LOW_WATER
    return min(20, max(1, parsed))


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


def attestation_verification_grace_seconds() -> float:
    """Return explicit transport-completion, verification, and clock-skew grace."""
    raw = os.getenv(ATTESTATION_VERIFICATION_GRACE_ENV, "")
    if not raw:
        return DEFAULT_ATTESTATION_VERIFICATION_GRACE_SECONDS
    if raw != raw.strip():
        raise ProvisioningError("honcho_attestation_window_invalid", retryable=False)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ProvisioningError("honcho_attestation_window_invalid", retryable=False) from exc
    if not math.isfinite(value) or value <= 0:
        raise ProvisioningError("honcho_attestation_window_invalid", retryable=False)
    return value


def provision_deadline() -> ProvisionDeadline:
    """Snapshot one monotonic transaction budget strictly inside proof freshness."""
    timeout = provision_timeout_seconds()
    grace = attestation_verification_grace_seconds()
    total = timeout + grace
    if total + ATTESTATION_CLOCK_SKEW_MARGIN_SECONDS >= ATTESTATION_TTL_SECONDS:
        raise ProvisioningError("honcho_attestation_window_invalid", retryable=False)
    return ProvisionDeadline(
        provider_timeout_seconds=timeout,
        verification_grace_seconds=grace,
        total_seconds=total,
    )


def validated_provision_timeout_seconds() -> float:
    """Return the per-operation transport cap from the validated total budget."""
    return provision_deadline().provider_timeout_seconds


def validated_provision_deadline_seconds() -> float:
    """Return the monotonic deadline for the complete provisioning transaction."""
    return provision_deadline().total_seconds


def provision_idempotency_key(challenge: dict[str, Any]) -> str:
    """Return one content-free stable provider key for this job/binding pair."""
    required = ("firebase_uid", "runtime_target_id", "binding_id", "job_id")
    values = tuple(str(challenge.get(field) or "") for field in required)
    if not all(values):
        raise ProvisioningError("honcho_attestation_context_incomplete", retryable=False)
    digest = hashlib.sha256()
    digest.update(b"ella-hermes-provision-idempotency-v1")
    for value in values:
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
    return f"ella-hermes-v1-{digest.hexdigest()}"


def stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_self_hosted_grok_voice_mode(
    uid: str,
    *,
    fresh_entitlement_created: bool = False,
) -> None:
    """Seed Firestore voice_mode=grok-voice only when a fresh DB row was inserted.

    The public onboarding receipt surfaces ``effective_voice_mode`` from the
    Firestore-backed voice settings (onboarding._resolved_voice_mode ->
    app_settings.get_voice_settings), and the client routes to the Grok lane off
    that receipt field.

    Gated on ``fresh_entitlement_created`` so the two side effects stay atomic
    for genuinely fresh users: the receipt only advertises grok-voice when
    ``seed_voice_entitlement_if_absent`` actually inserted an active row (i.e.
    the entitlement really permits grok). A returning self-hosted user with an
    existing revoked/suspended (non-grok) entitlement row, or an operator-seeded
    row, is left untouched -- auto-provision never writes their voice mode. The
    only-if-absent write also preserves an existing user voice choice. Never
    allowed to break provisioning on a settings write.
    """
    if not uid or not fresh_entitlement_created:
        return
    try:
        current = app_settings_db.get_voice_settings(uid) or {}
        if str((current.get("voice_mode") or "").strip()):
            return  # preserve existing choice; do not clobber
        app_settings_db.save_voice_settings(uid, {"voice_mode": "grok-voice"})
    except Exception:  # noqa: BLE001 - settings seed must never fail provisioning
        logger.warning(
            "grok voice-mode provisioning seed failed for uid=%s; DB entitlement still seeded",
            uid,
        )


def public_receipt(
    job: dict[str, Any],
    binding: Optional[dict[str, Any]] = None,
    effective_voice_mode: str = "",
) -> dict[str, Any]:
    state = str(job.get("state") or "pending")
    stage = str(job.get("stage") or "identity_ready")
    public_state = {
        "pending": "queued",
        "provisioning": "provisioning",
        "ready": "ready",
        "retryable": "retryable",
        "rolling_back": "rolling_back",
        "manual_intervention": "manual_intervention",
        "degraded": "retryable",
        "blocked": "blocked",
    }.get(state, "manual_intervention")
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
        "retry_after_ms": 2000 if public_state in {"queued", "provisioning", "retryable"} else None,
        "support_code": f"ELLA-{job_id.split('-')[0].upper()}",
        "target_schema_version": job["target_schema_version"],
        "binding_state": "active" if binding and binding.get("active") else "inactive",
        "binding_revision": int(binding.get("revision", 0)) if binding else 0,
        "effective_policy_revision": (
            f"{binding['model_policy_version']}:{binding['voice_policy_version']}" if binding else None
        ),
        "runtime_provider": str(binding.get("provider") or "") if binding else None,
        "runtime_status": str(binding.get("status") or "") if binding else None,
        "effective_voice_mode": str(effective_voice_mode or ""),
    }
    if job.get("error_code"):
        result["error_code"] = job["error_code"]
    return result


def retained_compatibility_receipt(target_schema_version: str) -> dict[str, Any]:
    """Describe an existing routed account without exposing legacy runtime details."""
    return {
        "job_id": "retained-compatibility",
        "state": "ready",
        "stage": "ready",
        "retryable": False,
        "retry_after_ms": None,
        "support_code": "",
        "target_schema_version": target_schema_version,
        "binding_state": "active",
        "binding_revision": 1,
        "effective_policy_revision": RETAINED_COMPATIBILITY_POLICY_REVISION,
        "compatibility_mode": "retained",
    }


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


async def _bounded_provision_response_body(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise ProvisioningError("invalid_provision_receipt", retryable=True) from exc
        if declared_size < 0 or declared_size > MAX_PROVISION_RESPONSE_BYTES:
            raise ProvisioningError("invalid_provision_receipt", retryable=True)

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_PROVISION_RESPONSE_BYTES:
            body.clear()
            raise ProvisioningError("invalid_provision_receipt", retryable=True)
    return bytes(body)


def _provision_conflict_code(response_body: bytes) -> str:
    try:
        payload = json.loads(response_body)
    except (RecursionError, UnicodeDecodeError, ValueError):
        return PROVISION_CONFLICT_FALLBACK_CODE
    if not isinstance(payload, dict):
        return PROVISION_CONFLICT_FALLBACK_CODE
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return PROVISION_CONFLICT_FALLBACK_CODE
    code = detail.get("code")
    if not isinstance(code, str) or code not in PROVISION_CONFLICT_CODES:
        return PROVISION_CONFLICT_FALLBACK_CODE
    return code


class HermesProvisionClient:
    @staticmethod
    def resolve_authority(expected_snapshot: ProvisionAuthoritySnapshot | None = None) -> ProvisionAuthority:
        try:
            return hermes_provision_authority(expected_snapshot)
        except ProvisionAuthorityError as exc:
            raise ProvisioningError(exc.code, retryable=False) from exc

    @classmethod
    def snapshot_authority(
        cls,
        expected_snapshot: ProvisionAuthoritySnapshot | None = None,
    ) -> ProvisionAuthoritySnapshot:
        return cls.resolve_authority(expected_snapshot).snapshot()

    async def provision(
        self,
        identity: VerifiedIdentity,
        target_schema_version: str,
        *,
        attestation_challenge: dict[str, Any],
        authority_snapshot: ProvisionAuthoritySnapshot | None = None,
        timeout_seconds: float | None = None,
        deadline_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        entry_snapshot = self.snapshot_authority(authority_snapshot)
        send_snapshot: ProvisionAuthoritySnapshot | None = None
        timeout_seconds = validated_provision_timeout_seconds() if timeout_seconds is None else timeout_seconds
        idempotency_key = provision_idempotency_key(attestation_challenge)
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                payload_authority = self.resolve_authority(entry_snapshot)
                payload = {
                    "userId": identity.uid,
                    "omiUid": identity.uid,
                    "firebaseUid": identity.uid,
                    "email": identity.email,
                    "label": identity.name,
                    "timezone": identity.timezone,
                    "hermes_only": True,
                    "targetSchemaVersion": target_schema_version,
                    "honchoAttestationChallenge": attestation_challenge,
                }
                authority = self.resolve_authority(payload_authority.snapshot())
                send_snapshot = authority.snapshot()
                async with client.stream(
                    "POST",
                    f"{authority.base_url}/provision",
                    headers={
                        "Authorization": f"Bearer {authority.token}",
                        "Idempotency-Key": idempotency_key,
                    },
                    json=payload,
                ) as response:
                    self.resolve_authority(send_snapshot)

                    if response.status_code == 409:
                        try:
                            response_body = await _bounded_provision_response_body(response)
                        except ProvisioningError as exc:
                            self.resolve_authority(send_snapshot)
                            if deadline_check is not None:
                                deadline_check()
                            raise ProvisioningError(
                                PROVISION_CONFLICT_FALLBACK_CODE,
                                retryable=False,
                                detail={"status": 409},
                            ) from exc
                        self.resolve_authority(send_snapshot)
                        if deadline_check is not None:
                            deadline_check()
                        conflict_code = _provision_conflict_code(response_body)
                        if deadline_check is not None:
                            deadline_check()
                        raise ProvisioningError(
                            conflict_code,
                            retryable=False,
                            detail={"status": 409},
                        )
                    if 300 <= response.status_code < 400:
                        raise ProvisioningError(
                            "provision_request_rejected", retryable=False, detail={"status": response.status_code}
                        )
                    if response.status_code in {401, 403}:
                        raise ProvisioningError("provision_service_auth_failed", retryable=False)
                    if response.status_code >= 500:
                        raise ProvisioningError("provision_service_unavailable", retryable=True)
                    if response.status_code >= 400:
                        raise ProvisioningError(
                            "provision_request_rejected", retryable=False, detail={"status": response.status_code}
                        )
                    response_body = await _bounded_provision_response_body(response)
                    self.resolve_authority(send_snapshot)
                    if deadline_check is not None:
                        deadline_check()
        except httpx.TimeoutException as exc:
            if send_snapshot is not None:
                self.resolve_authority(send_snapshot)
            raise ProvisioningError("provision_service_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            if send_snapshot is not None:
                self.resolve_authority(send_snapshot)
            raise ProvisioningError("provision_service_unavailable", retryable=True) from exc

        try:
            result = json.loads(response_body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProvisioningError("invalid_provision_receipt", retryable=True) from exc
        if deadline_check is not None:
            deadline_check()
        if not isinstance(result, dict):
            raise ProvisioningError("invalid_provision_receipt", retryable=True)
        if result.get("provisionMode") != "hermes_only" or result.get("mode") != "hermes_only":
            raise ProvisioningError("non_hermes_provision_rejected", retryable=False)
        return result


def extract_runtime_binding(
    result: dict[str, Any],
    uid: str,
    *,
    expected_attestation_challenge: dict[str, Any],
    expected_template_version: Optional[str] = None,
    now: int | None = None,
) -> dict[str, Any]:
    raw = result.get("runtimeBinding") or result.get("runtime_binding")
    if not isinstance(raw, dict):
        raise ProvisioningError("runtime_receipt_missing", retryable=True)

    provider = str(raw.get("provider") or "").lower()
    profile_name = str(raw.get("profileName") or raw.get("profile_name") or "")
    agent_id = str(raw.get("agentId") or raw.get("agent_id") or "")
    raw_mode = str(raw.get("runtimeTargetMode") or raw.get("runtime_target_mode") or "").strip()
    runtime_target_mode = raw_mode or None
    if runtime_target_mode is not None and runtime_target_mode not in SELF_HOSTED_RUNTIME_TARGET_MODES:
        raise ProvisioningError("invalid_runtime_target_mode", retryable=False)
    workspace_root = raw.get("workspaceRoot") or raw.get("workspace_root")
    internal_gateway_url = raw.get("internalGatewayUrl") or raw.get("internal_gateway_url")
    gateway_port = raw.get("gatewayPort") or raw.get("gateway_port")
    service_label = raw.get("serviceLabel") or raw.get("service_label")
    credential_ref = raw.get("credentialRef") or raw.get("credential_ref")
    health_state = str(raw.get("healthState") or raw.get("health_state") or "").lower()
    provider_health_receipt = raw.get("healthReceipt") or raw.get("health_receipt") or {}
    if not isinstance(provider_health_receipt, dict):
        raise ProvisioningError("invalid_health_receipt", retryable=True)
    smoke_values = []
    for source, key in (
        (raw, "smokePassed"),
        (raw, "smoke_passed"),
        (provider_health_receipt, "smokePassed"),
        (provider_health_receipt, "smoke_passed"),
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
    expected_honcho_config = posixpath.normpath(f"{profiles_root.rstrip('/')}/{profile_name}/honcho.json")

    profile_map = result.get("honchoProfileMap") or result.get("honcho_profile_map")
    provisioning_receipt = result.get("provisioningReceipt") or result.get("provisioning_receipt")
    receipt_honcho = provisioning_receipt.get("honcho") if isinstance(provisioning_receipt, dict) else None
    validation = receipt_honcho.get("validation") if isinstance(receipt_honcho, dict) else None
    profile_map_target = profile_map.get("target") if isinstance(profile_map, dict) else None
    validation_target = validation.get("target") if isinstance(validation, dict) else None
    if not all(
        [
            isinstance(profile_map, dict),
            isinstance(profile_map_target, dict),
            isinstance(validation, dict),
            isinstance(validation_target, dict),
        ]
    ):
        raise ProvisioningError("honcho_runtime_proof_incomplete", retryable=True)

    config_paths = (
        profile_map.get("honchoConfigPath") or profile_map.get("honcho_config_path"),
        validation.get("configPath") or validation.get("config_path"),
    )
    expected_target = {
        "workspace": str(honcho_workspace),
        "observed_peer_id": str(observed_peer),
        "observer_peer_id": str(observer_peer),
        "hermesProfile": profile_name,
    }
    target_fields = {
        "workspace": ("workspace",),
        "observed_peer_id": ("observed_peer_id", "observedPeer", "peerName"),
        "observer_peer_id": ("observer_peer_id", "observerPeer", "aiPeer"),
        "hermesProfile": ("hermesProfile", "hermes_profile", "profile"),
    }

    def target_value(target: dict[str, Any], keys: tuple[str, ...]) -> str:
        return str(next((target.get(key) for key in keys if target.get(key) is not None), ""))

    proof_mismatch = (
        profile_map.get("status") != "ok"
        or validation.get("ok") is not True
        or validation.get("mapped") is not True
        or str(validation.get("profile") or "") != profile_name
        or any(posixpath.normpath(str(path or "")) != expected_honcho_config for path in config_paths)
        or any(
            target_value(profile_map_target, target_fields[field]) != expected
            for field, expected in expected_target.items()
        )
        or any(
            target_value(validation_target, target_fields[field]) != expected
            for field, expected in expected_target.items()
            if field != "hermesProfile"
        )
    )
    if proof_mismatch:
        raise ProvisioningError("honcho_runtime_proof_mismatch", retryable=True)

    try:
        parsed_port = int(gateway_port)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("invalid_gateway_port", retryable=False) from exc
    gateway_url = validate_internal_gateway_url(str(internal_gateway_url))
    if not 1024 <= parsed_port <= 65535 or urlparse(gateway_url).port != parsed_port:
        raise ProvisioningError("gateway_port_mismatch", retryable=False)
    validate_gateway_credential_ref(str(credential_ref))

    attestation = result.get("honchoAttestation") or result.get("honcho_attestation")
    verified_at = int(time.time() if now is None else now)
    try:
        verified_attestation = verify_attestation(
            attestation,
            expected_challenge=expected_attestation_challenge,
            observed=observed_runtime_fields(
                profile_name=profile_name,
                config_path=expected_honcho_config,
                workspace_root=str(workspace_root),
                honcho_workspace=str(honcho_workspace),
                observed_peer_id=str(observed_peer),
                observer_peer_id=str(observer_peer),
                gateway_port=parsed_port,
                gateway_target=gateway_url,
                credential_ref=str(credential_ref),
                agent_id=agent_id,
                service_label=str(service_label),
            ),
            now=verified_at,
            require_fresh=True,
        )
    except HonchoAttestationError as exc:
        raise ProvisioningError(
            exc.code,
            retryable=True,
        ) from exc
    health_receipt = {
        "smoke_passed": True,
        "honcho_isolation": {
            "attestation": verified_attestation,
            "verified_at": verified_at,
        },
    }

    template_version = str(raw.get("templateVersion") or DEFAULT_TEMPLATE_VERSION)
    if expected_template_version and template_version != expected_template_version:
        raise ProvisioningError("runtime_template_version_mismatch", retryable=True)

    return {
        "role": "user",
        "provider": "hermes",
        "profile_name": profile_name,
        "agent_id": agent_id,
        "runtime_target_mode": runtime_target_mode,
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
        "binding_id": str(expected_attestation_challenge["binding_id"]),
    }


class ProvisioningCoordinator:
    def __init__(
        self,
        repository: EllaProvisioningRepository,
        client: Optional[HermesProvisionClient] = None,
        *,
        cloud_client: Any = None,
        honcho_client: Any = None,
        alert_publisher: Any = None,
        runtime_admission: Any = None,
        staged_attestation_verifier: Any = None,
        clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ):
        self.repository = repository
        self.client = client or HermesProvisionClient()
        self.cloud_client = cloud_client
        self.honcho_client = honcho_client
        self.alert_publisher = alert_publisher
        self.runtime_admission = runtime_admission
        self.staged_attestation_verifier = staged_attestation_verifier
        self.clock = clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic

    def _authority_snapshot(
        self,
        expected_snapshot: ProvisionAuthoritySnapshot | None = None,
    ) -> ProvisionAuthoritySnapshot | None:
        if not isinstance(self.client, HermesProvisionClient):
            return None
        return self.client.snapshot_authority(expected_snapshot)

    async def _repository_call(
        self,
        authority_snapshot: ProvisionAuthoritySnapshot | None,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if authority_snapshot is not None:
            self.client.resolve_authority(authority_snapshot)
        result = await operation(*args, **kwargs)
        if authority_snapshot is not None:
            self.client.resolve_authority(authority_snapshot)
        return result

    async def ensure_job(
        self,
        *,
        identity: VerifiedIdentity,
        target_schema_version: str,
        client_request_id: Optional[str],
        request_payload: dict[str, Any],
        authority_snapshot: ProvisionAuthoritySnapshot | None = None,
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]], bool]:
        cloud_required = cloud_provisioning_enabled(identity.uid)
        legacy_required = provisioning_enabled(identity.uid)
        self_hosted_configured = self_hosted_provisioning_configured() and not cloud_required
        invitation_admission = None
        invitation_owned = False
        if (
            not cloud_required
            and (self_hosted_configured or legacy_required)
            and isinstance(self.client, HermesProvisionClient)
        ):
            authority_snapshot = self._authority_snapshot(authority_snapshot)
        else:
            authority_snapshot = None
        try:
            await self._repository_call(authority_snapshot, self.repository.assert_schema_ready)
            if cloud_required:
                await self.repository.assert_cloud_schema_ready()
            elif self_hosted_configured or legacy_required:
                await self._repository_call(
                    authority_snapshot,
                    self.repository.assert_self_hosted_invite_schema_ready,
                )
        except ProvisioningSchemaNotReadyError as exc:
            logger.error("Ella provisioning schema is incomplete: %s", ", ".join(exc.missing))
            raise ProvisioningError("provisioning_schema_not_ready", retryable=True) from exc

        if not cloud_required and (self_hosted_configured or legacy_required):
            try:
                invitation_owned = await self._repository_call(
                    authority_snapshot,
                    self.repository.has_invitation_owned_self_hosted_runtime,
                    identity.uid,
                )
                if self_hosted_configured:
                    invitation_admission = await self._repository_call(
                        authority_snapshot,
                        self.repository.get_self_hosted_invitation_admission,
                        identity.uid,
                    )
            except Exception as exc:
                if isinstance(exc, ProvisioningError):
                    raise
                raise ProvisioningError("self_hosted_invitation_authority_unavailable", retryable=True) from exc

        self_hosted_required = self_hosted_provisioning_enabled(
            identity.uid,
            admission=invitation_admission,
        )
        invitation_owned = invitation_owned or invitation_admission is not None
        if invitation_owned and not self_hosted_configured:
            raise ProvisioningError("self_hosted_invitation_runtime_disabled", retryable=True)
        if invitation_owned and not self_hosted_required:
            raise ProvisioningError("invitation_authority_required", retryable=False)
        if self_hosted_configured and not self_hosted_required and not legacy_required:
            raise ProvisioningError("invitation_authority_required", retryable=False)
        if self_hosted_required and invitation_admission is None:
            # INTERIM relax (Plato #1189): a fresh self-hosted UID admitted
            # without an admission record has no invitation chain to resolve.
            # The attestation/context is derived from the job/user identity
            # downstream (see _process_claimed_job_with_deadline fallbacks),
            # so we do not hard-fail here. This branch only runs while
            # ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID is set.
            if not self_hosted_fresh_uid_relax_enabled():
                assert invitation_admission is not None
        elif self_hosted_required:
            # The current invitation, entitlement, consent epoch, and target
            # chain were resolved before identity, job, or provider side effects.
            assert invitation_admission is not None

        await self._repository_call(
            authority_snapshot,
            self.repository.ensure_user_identity,
            uid=identity.uid,
            email=identity.email,
            name=identity.name,
            timezone_name=identity.timezone,
        )
        request_payload_hash = stable_payload_hash(request_payload)
        job = await self._repository_call(
            authority_snapshot,
            self.repository.acquire_job,
            uid=identity.uid,
            target_schema_version=target_schema_version,
            client_request_id=client_request_id,
            request_payload_hash=request_payload_hash,
        )
        try:
            await self._repository_call(
                authority_snapshot,
                self.repository.ensure_omi_user_document,
                uid=identity.uid,
                email=identity.email,
                name=identity.name,
                timezone_name=identity.timezone,
            )
        except Exception:
            logger.error("OMI identity initialization failed")
            job = await self._repository_call(
                authority_snapshot,
                self.repository.update_job,
                job_id=str(job["id"]),
                state="degraded",
                stage="identity_ready",
                retryable=True,
                error_code="omi_identity_unavailable",
            )
            return job, None, False
        if cloud_required:
            profile_class = await self.repository.get_cloud_profile_class(identity.uid)
            authority = current_cloud_authority(
                identity.uid,
                profile_class=profile_class,
                profile_uid=identity.uid,
                runtime_provider=CLOUD_RUNTIME_PROVIDER,
                model_route=f"openai-codex/{CLOUD_RUNTIME_MODEL}",
                memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
                photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
            )
            binding = await self._repository_call(
                authority_snapshot,
                self.repository.resolve_active_runtime,
                identity.uid,
                template_version=target_schema_version,
                target_mode="hermes-cloud-chat",
                required_provider=CLOUD_RUNTIME_PROVIDER,
                authority_lineage=authority.lineage,
                model=CLOUD_RUNTIME_MODEL,
            )
        else:
            binding = await self._repository_call(
                authority_snapshot,
                self.repository.resolve_active_runtime,
                identity.uid,
                template_version=target_schema_version,
                target_mode="hermes-chat" if self_hosted_required else None,
                required_provider="hermes",
                authority_lineage=(current_self_hosted_runtime_lineage() if self_hosted_required else None),
                model=SELF_HOSTED_RUNTIME_MODEL if self_hosted_required else CLOUD_RUNTIME_MODEL,
            )
        if binding:
            if cloud_required and str(binding.get("provider") or "").lower() != "hermes_cloud":
                raise ProvisioningError("cloud_runtime_binding_conflict", retryable=False)
            if str(binding.get("user_status") or "") != "ACTIVE":
                await self._repository_call(authority_snapshot, self.repository.activate_user, identity.uid)
            if job.get("state") != "ready":
                job = await self._repository_call(
                    authority_snapshot,
                    self.repository.update_job,
                    job_id=str(job["id"]),
                    state="ready",
                    stage="active",
                    retryable=False,
                    receipt={"type": "active_binding_reconciled", "binding_revision": binding["revision"]},
                )
            return job, binding, False
        if not cloud_required and not legacy_required and not self_hosted_required:
            job = await self._repository_call(
                authority_snapshot,
                self.repository.update_job,
                job_id=str(job["id"]),
                state="degraded",
                stage="identity_ready",
                retryable=True,
                error_code="provisioning_disabled",
            )
            return job, None, False
        claimed = await self._repository_call(
            authority_snapshot,
            self.repository.claim_job,
            str(job["id"]),
        )
        return claimed or job, None, claimed is not None

    async def process_claimed_job(
        self,
        *,
        job: dict[str, Any],
        identity: VerifiedIdentity,
        authority_snapshot: ProvisionAuthoritySnapshot | None = None,
    ) -> None:
        if cloud_provisioning_enabled(identity.uid):
            await self._process_cloud_claimed_job(job=job, identity=identity)
            return

        progress: dict[str, Any] = {
            "authority_snapshot": authority_snapshot,
            "provider_work_started": False,
        }
        try:
            deadline = provision_deadline()
        except ProvisioningError as exc:
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="blocked",
                stage="runtime_ready",
                retryable=False,
                error_code=exc.code,
            )
            return

        progress["deadline_at"] = self.monotonic_clock() + deadline.total_seconds

        def deadline_check() -> None:
            if self.monotonic_clock() >= progress["deadline_at"]:
                raise TimeoutError("provision_transaction_timeout")

        try:
            async with asyncio.timeout(deadline.total_seconds):
                await self._process_claimed_job_with_deadline(
                    job=job,
                    identity=identity,
                    deadline=deadline,
                    deadline_check=deadline_check,
                    progress=progress,
                    authority_snapshot=authority_snapshot,
                )
        except TimeoutError:
            await self._record_interrupted_provision(
                job=job,
                progress=progress,
                error_code="provision_transaction_timeout",
            )
        except asyncio.CancelledError:
            await self._record_interrupted_provision(
                job=job,
                progress=progress,
                error_code="provision_transaction_cancelled",
            )
            raise

    async def _record_interrupted_provision(
        self,
        *,
        job: dict[str, Any],
        progress: dict[str, Any],
        error_code: str,
    ) -> None:
        provider_work_started = bool(progress.get("provider_work_started"))
        error_detail = {"reconciliation": "retry_same_job_binding"} if provider_work_started else {}
        receipt = (
            {
                "type": "runtime_attestation_reconciliation_required",
                "same_job_binding_required": True,
                "content_free": True,
            }
            if provider_work_started
            else None
        )
        try:
            await self._repository_call(
                progress.get("authority_snapshot"),
                self.repository.update_job,
                job_id=str(job["id"]),
                state="retryable",
                stage="runtime_ready",
                retryable=True,
                error_code=error_code,
                error_detail=error_detail,
                receipt=receipt,
            )
        except Exception:
            logger.error("Unable to persist provisioning interruption state")

    async def _process_claimed_job_with_deadline(
        self,
        *,
        job: dict[str, Any],
        identity: VerifiedIdentity,
        deadline: ProvisionDeadline,
        deadline_check: Callable[[], None],
        progress: dict[str, Any],
        authority_snapshot: ProvisionAuthoritySnapshot | None = None,
    ) -> None:
        authority_snapshot = self._authority_snapshot(authority_snapshot)
        progress["authority_snapshot"] = authority_snapshot
        try:
            invitation_admission = None
            invitation_owned = False
            legacy_required = provisioning_enabled(identity.uid)
            await self._repository_call(
                authority_snapshot,
                self.repository.assert_self_hosted_invite_schema_ready,
            )
            try:
                invitation_owned = await self._repository_call(
                    authority_snapshot,
                    self.repository.has_invitation_owned_self_hosted_runtime,
                    identity.uid,
                )
            except Exception as exc:
                if isinstance(exc, ProvisioningError):
                    raise
                raise ProvisioningError("self_hosted_invitation_authority_unavailable", retryable=True) from exc
            if self_hosted_provisioning_configured():
                try:
                    invitation_admission = await self._repository_call(
                        authority_snapshot,
                        self.repository.get_self_hosted_invitation_admission,
                        identity.uid,
                    )
                except Exception as exc:
                    if isinstance(exc, ProvisioningError):
                        raise
                    raise ProvisioningError("self_hosted_invitation_authority_unavailable", retryable=True) from exc
            self_hosted_required = self_hosted_provisioning_enabled(
                identity.uid,
                admission=invitation_admission,
            )
            invitation_owned = invitation_owned or invitation_admission is not None
            if invitation_owned and not self_hosted_provisioning_configured():
                raise ProvisioningError("self_hosted_invitation_runtime_disabled", retryable=True)
            if invitation_owned and not self_hosted_required:
                raise ProvisioningError("invitation_authority_required", retryable=False)
            if self_hosted_provisioning_configured() and not self_hosted_required and not legacy_required:
                raise ProvisioningError("invitation_authority_required", retryable=False)
            if not self_hosted_required and not legacy_required:
                raise ProvisioningError("provisioning_disabled", retryable=False)
            account_owner_id = str((invitation_admission or {}).get("user_id") or job.get("user_id") or "").strip()
            runtime_target_id = str(
                (invitation_admission or {}).get("attestation_runtime_target_id") or f"job:{job['id']}"
            ).strip()
            if not account_owner_id or not runtime_target_id:
                raise ProvisioningError("honcho_attestation_context_incomplete", retryable=False)
            binding_id = await self._repository_call(
                authority_snapshot,
                self.repository.prepare_runtime_binding_identity,
                uid=identity.uid,
                provider="hermes",
                role="user",
            )
            deadline_check()
            try:
                attestation_challenge = create_challenge(
                    firebase_uid=identity.uid,
                    account_owner_id=account_owner_id,
                    runtime_target_id=runtime_target_id,
                    binding_id=str(binding_id),
                    job_id=str(job["id"]),
                    now=int(self.clock()),
                )
            except HonchoAttestationError as exc:
                raise ProvisioningError(
                    exc.code,
                    retryable=exc.code == "honcho_attestation_key_unavailable",
                ) from exc
            if isinstance(self.client, HermesProvisionClient):
                progress["provider_work_started"] = True
                result = await self.client.provision(
                    identity,
                    str(job["target_schema_version"]),
                    attestation_challenge=attestation_challenge,
                    authority_snapshot=authority_snapshot,
                    timeout_seconds=deadline.provider_timeout_seconds,
                    deadline_check=deadline_check,
                )
                self.client.resolve_authority(authority_snapshot)
            else:
                progress["provider_work_started"] = True
                result = await self.client.provision(
                    identity,
                    str(job["target_schema_version"]),
                    attestation_challenge=attestation_challenge,
                )
            deadline_check()
            binding_data = extract_runtime_binding(
                result,
                identity.uid,
                expected_attestation_challenge=attestation_challenge,
                expected_template_version=str(job["target_schema_version"]),
                now=int(self.clock()),
            )
            # Mode-source decision: a provider-pinned mode (validated by
            # extract_runtime_binding) wins; absent mode defaults the user chat
            # lane to 'hermes-chat'. Without a valid mode, every first chat
            # turn raises self_hosted_runtime_target_mode_required (the fresh
            # signup replay defect this corrects).
            if binding_data.get("runtime_target_mode") is None and binding_data.get("role", "user") == "user":
                binding_data["runtime_target_mode"] = "hermes-chat"
            deadline_check()
            binding = await self._repository_call(
                authority_snapshot,
                self.repository.stage_runtime_binding,
                uid=identity.uid,
                binding=binding_data,
            )
            deadline_check()
            await self._repository_call(
                authority_snapshot,
                self.repository.update_job,
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
            deadline_check()
            # Seed the fresh self-hosted user's grok voice entitlement (DB,
            # create-if-absent) and Firestore voice_mode (only-if-absent) so the
            # first voice/chat session is not entitlement-gated and the public
            # receipt carries effective_voice_mode=grok-voice for the client.
            voice_entitlement_created = await self._repository_call(
                authority_snapshot,
                self.repository.seed_voice_entitlement_if_absent,
                uid=identity.uid,
            )
            # Firestore voice_mode only when a fresh DB entitlement row was
            # inserted or the exact auto-provisioned quarantine row was safely
            # recovered -- keeps operator-seeded voice state untouched.
            _ensure_self_hosted_grok_voice_mode(
                identity.uid,
                fresh_entitlement_created=bool(voice_entitlement_created),
            )
            deadline_check()
            invitation_target_required = invitation_admission is not None
            activated = await self._repository_call(
                authority_snapshot,
                self.repository.activate_runtime_binding,
                uid=identity.uid,
                provider="hermes",
                require_invitation_target=invitation_target_required,
                authority_lineage=(current_self_hosted_runtime_lineage() if invitation_target_required else None),
                model=SELF_HOSTED_RUNTIME_MODEL,
            )
            deadline_check()
            await self._repository_call(
                authority_snapshot,
                self.repository.update_job,
                job_id=str(job["id"]),
                state="ready",
                stage="active",
                retryable=False,
                receipt={
                    "type": "binding_activated",
                    "binding_revision": activated["revision"],
                },
            )
            deadline_check()
        except TimeoutError:
            raise
        except ProvisioningError as exc:
            if exc.code == "hermes_provision_authority_drift":
                return
            reconciliation_required = bool(progress.get("provider_work_started")) and exc.detail.get("status") != 409
            retryable = exc.retryable or reconciliation_required
            error_detail = dict(exc.detail)
            receipt = None
            if reconciliation_required:
                error_detail["reconciliation"] = "retry_same_job_binding"
                receipt = {
                    "type": "runtime_attestation_reconciliation_required",
                    "same_job_binding_required": True,
                    "content_free": True,
                }
            try:
                await self._repository_call(
                    authority_snapshot,
                    self.repository.update_job,
                    job_id=str(job["id"]),
                    state="retryable" if reconciliation_required else ("degraded" if retryable else "blocked"),
                    stage="runtime_ready",
                    retryable=retryable,
                    error_code=exc.code,
                    error_detail=error_detail,
                    receipt=receipt,
                )
            except ProvisioningError as authority_error:
                if authority_error.code == "hermes_provision_authority_drift":
                    return
                raise
        except RuntimePoolClaimError as exc:
            if progress.get("provider_work_started"):
                await self._record_interrupted_provision(job=job, progress=progress, error_code=exc.code)
                return
            try:
                await self._repository_call(
                    authority_snapshot,
                    self.repository.update_job,
                    job_id=str(job["id"]),
                    state="blocked",
                    stage="runtime_ready",
                    retryable=False,
                    error_code=exc.code,
                )
            except ProvisioningError as authority_error:
                if authority_error.code == "hermes_provision_authority_drift":
                    return
                raise
        except Exception:
            logger.error("Unexpected Hermes provisioning failure")
            if progress.get("provider_work_started"):
                await self._record_interrupted_provision(
                    job=job,
                    progress=progress,
                    error_code="provision_transaction_interrupted",
                )
                return
            try:
                await self._repository_call(
                    authority_snapshot,
                    self.repository.update_job,
                    job_id=str(job["id"]),
                    state="degraded",
                    stage="runtime_ready",
                    retryable=True,
                    error_code="provisioning_internal_error",
                )
            except ProvisioningError as authority_error:
                if authority_error.code == "hermes_provision_authority_drift":
                    return
                raise

    async def _process_cloud_claimed_job(
        self,
        *,
        job: dict[str, Any],
        identity: VerifiedIdentity,
    ) -> None:
        cloud_client = self.cloud_client or HermesCloudClient()
        honcho_client = self.honcho_client
        alert_publisher = self.alert_publisher or RuntimePoolAlertPublisher()
        runtime_admission = self.runtime_admission or voice_canary_db.evaluate_runtime_activation
        binding: Optional[dict[str, Any]] = None
        claim_token = ""
        try:
            pool_policy = await self.repository.get_cloud_pool_admission_policy()
            if not pool_policy:
                pool_state = await self.repository.reconcile_cloud_pool_alert(
                    threshold=cloud_pool_low_water_threshold()
                )
                await self._publish_pool_alert(alert_publisher, pool_state)
                await self.repository.update_job(
                    job_id=str(job["id"]),
                    state="retryable",
                    stage="profile_ready",
                    retryable=True,
                    error_code="runtime_pool_empty",
                    error_detail={
                        "available": int(pool_state["available"]),
                        "threshold": cloud_pool_low_water_threshold(),
                    },
                )
                return
            expected_model = str(pool_policy["model"])
            required_profile_class = "synthetic" if cloud_synthetic_only() else "real"

            async def current_authority():
                current_profile_class = await self.repository.get_cloud_profile_class(identity.uid)
                return current_cloud_authority(
                    identity.uid,
                    profile_class=current_profile_class,
                    profile_uid=identity.uid,
                    runtime_provider=str(pool_policy["provider"]),
                    model_route=f"openai-codex/{expected_model}",
                    memory_provider=MANAGED_CLOUD_MEMORY_PROVIDER,
                    photon_scope=MANAGED_CLOUD_PHOTON_SCOPE,
                )

            await current_authority()
            admission = await runtime_admission(
                uid=identity.uid,
                provider=str(pool_policy["provider"]),
                model=expected_model,
            )
            if not admission.allowed:
                raise ProvisioningError(
                    f"runtime_admission_{admission.code}",
                    retryable=False,
                )
            admitted_entitlement_revision = int((admission.entitlement or {}).get("revision") or 0)
            if admitted_entitlement_revision <= 0:
                raise ProvisioningError(
                    "runtime_admission_contract_invalid",
                    retryable=False,
                )

            binding = await self.repository.claim_cloud_pool_binding(
                uid=identity.uid,
                job_id=str(job["id"]),
                lease_seconds=cloud_pool_claim_lease_seconds(),
                admitted_entitlement_revision=admitted_entitlement_revision,
                provider=str(pool_policy["provider"]),
                model=expected_model,
                required_profile_class=required_profile_class,
            )
            pool_state = await self.repository.reconcile_cloud_pool_alert(threshold=cloud_pool_low_water_threshold())
            await self._publish_pool_alert(alert_publisher, pool_state)
            if not binding:
                await self.repository.update_job(
                    job_id=str(job["id"]),
                    state="retryable",
                    stage="profile_ready",
                    retryable=True,
                    error_code="runtime_pool_empty",
                    error_detail={
                        "available": int(pool_state["available"]),
                        "threshold": cloud_pool_low_water_threshold(),
                    },
                )
                return

            claim_token = str(binding.get("claim_token") or "")
            if not claim_token:
                raise ProvisioningError("runtime_pool_claim_incomplete", retryable=False)
            if str(binding.get("expected_model") or "") != expected_model:
                raise ProvisioningError("runtime_pool_policy_changed", retryable=False)

            side_effect_admission = await runtime_admission(
                uid=identity.uid,
                provider=str(pool_policy["provider"]),
                model=expected_model,
            )
            if not side_effect_admission.allowed:
                raise ProvisioningError(
                    f"runtime_admission_{side_effect_admission.code}",
                    retryable=False,
                )
            side_effect_revision = int((side_effect_admission.entitlement or {}).get("revision") or 0)
            if side_effect_revision != admitted_entitlement_revision:
                raise ProvisioningError(
                    "runtime_admission_entitlement_stale",
                    retryable=False,
                )

            await current_authority()
            registration_health = binding.get("health_receipt") or {}
            if isinstance(registration_health, str):
                try:
                    registration_health = json.loads(registration_health)
                except json.JSONDecodeError:
                    registration_health = {}
            staged_marker = (
                registration_health.get("staged_attestation") if isinstance(registration_health, dict) else None
            )
            if isinstance(staged_marker, dict):
                staged_verifier = self.staged_attestation_verifier or StagedAttestationVerifier()
                staged_receipt = staged_verifier.preflight(
                    binding,
                    receipt_ref=str(staged_marker.get("receipt_ref") or ""),
                    uid=identity.uid,
                    account_id=str(binding.get("user_id") or ""),
                    profile_id=str(binding.get("user_id") or ""),
                    profile_class=str(binding.get("profile_class") or ""),
                    phase="claim_finalization",
                    prior_marker=staged_marker,
                )
                preflight = HermesCloudPreflight(
                    model=str(staged_receipt["model"]),
                    tools=tuple(staged_receipt["tools"]),
                    capabilities=tuple(staged_receipt["capabilities"]),
                    receipt=staged_receipt,
                )
            else:
                preflight = await cloud_client.preflight(binding)
            final_authority = await current_authority()
            health_receipt = {
                **preflight.receipt,
                "memory": {
                    "provider": MANAGED_CLOUD_MEMORY_PROVIDER,
                    "scope": "profile",
                    "owner": "hermes_cloud",
                    "account_profile_bound": True,
                    "content_free": True,
                },
                **final_authority.lineage.as_dict(),
                "admission_revision": admitted_entitlement_revision,
            }
            activated = await self.repository.finalize_cloud_pool_claim(
                uid=identity.uid,
                job_id=str(job["id"]),
                claim_token=claim_token,
                admitted_entitlement_revision=admitted_entitlement_revision,
                authority_lineage=final_authority.lineage,
                health_receipt=health_receipt,
                status=os.getenv("ELLA_HERMES_CLOUD_INITIAL_STATUS", "shadow").strip().lower(),
                provider=str(pool_policy["provider"]),
                model=expected_model,
            )
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="ready",
                stage="active",
                retryable=False,
                receipt={
                    "type": "hermes_cloud_binding_activated",
                    "binding_revision": int(activated["revision"]),
                    "runtime_instance_sha256": hashlib.sha256(
                        str(activated["runtime_instance_id"]).encode("utf-8")
                    ).hexdigest(),
                    "content_free": True,
                },
            )
        except (ProvisioningError, RuntimePoolClaimError) as exc:
            code = exc.code
            retryable = exc.retryable if isinstance(exc, ProvisioningError) else False
            if binding and claim_token:
                await self._rollback_cloud_claim(
                    job=job,
                    identity=identity,
                    binding=binding,
                    claim_token=claim_token,
                    honcho_client=honcho_client,
                    error_code=code,
                    retryable=retryable,
                )
            else:
                await self.repository.update_job(
                    job_id=str(job["id"]),
                    state="retryable" if retryable else "blocked",
                    stage="profile_ready",
                    retryable=retryable,
                    error_code=code,
                    error_detail=getattr(exc, "detail", {}),
                )
        except Exception:
            logger.error("Unexpected Hermes Cloud provisioning failure")
            if binding and claim_token:
                await self._rollback_cloud_claim(
                    job=job,
                    identity=identity,
                    binding=binding,
                    claim_token=claim_token,
                    honcho_client=honcho_client,
                    error_code="cloud_provisioning_internal_error",
                    retryable=False,
                )
            else:
                await self.repository.update_job(
                    job_id=str(job["id"]),
                    state="blocked",
                    stage="profile_ready",
                    retryable=False,
                    error_code="cloud_provisioning_internal_error",
                )

    async def _publish_pool_alert(self, publisher: Any, pool_state: dict[str, Any]) -> None:
        try:
            alert = pool_state.get("alert")
            delivered = bool(alert and not alert.get("delivered_at") and await publisher.publish(pool_state))
            if delivered:
                await self.repository.mark_cloud_pool_alert_delivered(str(alert["id"]))
        except Exception:
            logger.exception("Runtime pool alert mirror failed; durable outbox remains pending")

    async def _rollback_cloud_claim(
        self,
        *,
        job: dict[str, Any],
        identity: VerifiedIdentity,
        binding: dict[str, Any],
        claim_token: str,
        honcho_client: Any,
        error_code: str,
        retryable: bool,
    ) -> None:
        rollback_start_error = ""
        try:
            await self.repository.update_job(
                job_id=str(job["id"]),
                state="rolling_back",
                stage="runtime_ready",
                retryable=False,
                error_code=error_code,
                receipt={
                    "type": "cloud_claim_rollback_started",
                    "claim_token_sha256": hashlib.sha256(claim_token.encode("utf-8")).hexdigest(),
                    "content_free": True,
                },
            )
        except Exception:
            logger.error("Cloud rollback start receipt failed; continuing quarantine")
            rollback_start_error = "cloud_rollback_start_receipt_failed"
        side_effects = await self.repository.get_cloud_side_effects(str(job["id"]))
        cleanup_receipt: dict[str, Any] = {
            "status": "not_required",
            "runtime_credentials": "no_claim_credentials_issued",
            "memory_provider": MANAGED_CLOUD_MEMORY_PROVIDER,
            "content_free": True,
        }
        cleanup_error = ""
        try:
            if side_effects and honcho_client is not None:
                cleanup_receipt = await honcho_client.cleanup_profile(binding, side_effects)
        except Exception as exc:
            logger.error("Cloud claim cleanup failed")
            cleanup_error = getattr(exc, "code", "cloud_claim_cleanup_failed")

        quarantine_error = ""
        try:
            quarantined = await self.repository.quarantine_cloud_pool_claim(
                uid=identity.uid,
                job_id=str(job["id"]),
                claim_token=claim_token,
                reason=error_code,
                health_receipt={
                    "status": "failed",
                    "code": error_code,
                    "cleanup_status": cleanup_receipt.get("status"),
                    "content_free": True,
                },
            )
            if not quarantined:
                quarantine_error = "runtime_pool_quarantine_lost"
        except Exception:
            logger.error("Cloud runtime quarantine failed")
            quarantine_error = "runtime_pool_quarantine_failed"

        if cleanup_error or quarantine_error:
            await self.repository.record_cloud_rollback(
                job_id=str(job["id"]),
                state="manual_intervention",
                retryable=False,
                error_code=cleanup_error or quarantine_error,
                rollback_receipt={
                    "status": "manual_intervention",
                    "original_error": error_code,
                    "cleanup_error": cleanup_error or None,
                    "quarantine_error": quarantine_error or None,
                    "rollback_start_error": rollback_start_error or None,
                    "side_effect_count": len(side_effects),
                    "content_free": True,
                },
            )
            return
        await self.repository.record_cloud_rollback(
            job_id=str(job["id"]),
            state="retryable" if retryable else "blocked",
            retryable=retryable,
            error_code=error_code,
            rollback_receipt={
                **cleanup_receipt,
                "original_error": error_code,
                "rollback_start_error": rollback_start_error or None,
                "side_effect_count": len(side_effects),
                "quarantined": True,
                "content_free": True,
            },
        )
