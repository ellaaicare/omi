"""Authenticated, idempotent Ella Hermes onboarding endpoints."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from database.ella_provisioning import (
    EllaProvisioningRepository,
    IdentityConflictError,
    ProvisioningSchemaNotReadyError,
)
from database import app_settings as app_settings_db
from database.runtime_targets import CLOUD_RUNTIME_MODEL, CLOUD_RUNTIME_PROVIDER, SELF_HOSTED_RUNTIME_MODEL
from ella.services.ai_consent import (
    MANAGED_CLOUD_MEMORY_PROVIDER,
    MANAGED_CLOUD_PHOTON_SCOPE,
    require_current_ai_consent,
)
from ella.services.app_settings import normalize_voice_mode
from ella.services.hermes_cloud_policy import current_cloud_authority
from ella.services.provisioning import (
    DEFAULT_TARGET_SCHEMA_VERSION,
    HermesProvisionClient,
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
    any_provisioning_enabled,
    cloud_provisioning_enabled,
    current_self_hosted_runtime_lineage,
    effective_target_schema_version,
    provisioning_enabled,
    public_receipt,
    retained_compatibility_receipt,
    self_hosted_invitation_admission,
    self_hosted_provisioning_configured,
    self_hosted_provisioning_enabled,
    self_hosted_runtime_authority_required,
)
from ella.services.runtime_resolver import runtime_bindings_enabled
from utils.ella.exact_firebase_auth import get_exact_firebase_uid
from utils.other import endpoints as auth

logger = logging.getLogger("ella.onboarding")
router = APIRouter(prefix="/v1/ella/onboarding", tags=["ella-onboarding"])
SCHEMA_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_firestore_db: Any = None
TRUE_VALUES = {"1", "true", "yes", "on"}


def configure_firestore_db(firestore_db: Any) -> None:
    """Inject the already-initialized OMI Firestore client at app startup."""
    global _firestore_db
    _firestore_db = firestore_db


class OnboardingClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = "ios"
    app_version: str = "unknown"
    locale: str = "en-US"
    timezone: str = "America/Los_Angeles"


class OnboardingEnsureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_schema_version: str = DEFAULT_TARGET_SCHEMA_VERSION
    client_request_id: Optional[str] = Field(default=None, max_length=128)
    client: OnboardingClient = Field(default_factory=OnboardingClient)
    consent_receipt_id: Optional[str] = Field(default=None, max_length=256)


def _verified_identity(uid: str, payload: OnboardingEnsureRequest) -> VerifiedIdentity:
    try:
        firebase_user = auth.get_user(uid)
    except Exception as exc:
        logger.warning("Firebase user lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail={"code": "auth_required"}) from exc

    email = str(getattr(firebase_user, "email", "") or "").strip()
    if not email or getattr(firebase_user, "email_verified", None) is not True:
        raise HTTPException(status_code=409, detail={"code": "identity_missing_email"})
    name = str(getattr(firebase_user, "display_name", "") or "").strip() or email.split("@", 1)[0]
    return VerifiedIdentity(
        uid=uid,
        email=email,
        name=name,
        timezone=payload.client.timezone or "America/Los_Angeles",
    )


async def _coordinator() -> ProvisioningCoordinator:
    return ProvisioningCoordinator(await EllaProvisioningRepository.create(firestore_db=_firestore_db))


def _resolved_voice_mode(uid: str) -> str:
    """Resolve the provisioned voice mode from the per-user settings control plane.

    Reads the server-backed Firestore voice settings (the same store backed by
    GET/PATCH /v1/ella/settings). An empty result leaves the receipt's
    ``effective_voice_mode`` empty so the client falls through to its local
    pick (ElevenLabs default) — preserving today's behavior for every user
    without a provisioned value.
    """
    if not uid:
        return ""
    try:
        voice = app_settings_db.get_voice_settings(uid) or {}
        raw = str(voice.get("voice_mode") or "").strip()
        if not raw:
            return ""
        return normalize_voice_mode(raw)
    except Exception:  # noqa: BLE001 - never let settings lookup break onboarding
        logger.warning("voice-settings lookup failed for uid=%s; defaulting to empty", uid)
        return ""


async def _retained_receipt(uid: str, target_schema_version: str) -> Optional[dict[str, Any]]:
    """Return a public receipt only for an already-routed retained account."""
    if target_schema_version != DEFAULT_TARGET_SCHEMA_VERSION or runtime_bindings_enabled(uid):
        return None
    try:
        repository = await EllaProvisioningRepository.create()
        if await repository.has_active_retained_runtime(uid):
            logger.info("Using retained-account onboarding compatibility")
            return retained_compatibility_receipt(target_schema_version)
    except Exception as exc:
        logger.error("Retained-account onboarding lookup failed")
        raise HTTPException(status_code=503, detail={"code": "provisioning_unavailable"}) from exc
    return None


def _payload_dict(payload: OnboardingEnsureRequest) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload.dict()


@router.post("/ensure", dependencies=[Depends(require_current_ai_consent)])
async def ensure_onboarding(
    payload: OnboardingEnsureRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
) -> dict[str, Any]:
    if not SCHEMA_VERSION_RE.fullmatch(payload.target_schema_version):
        raise HTTPException(status_code=400, detail={"code": "invalid_target_schema_version"})
    try:
        target_schema_version = effective_target_schema_version(uid, payload.target_schema_version)
    except ProvisioningError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    self_hosted_configured = self_hosted_provisioning_configured() and not cloud_provisioning_enabled(uid)
    authority_snapshot = None
    if self_hosted_configured:
        try:
            authority_snapshot = HermesProvisionClient.snapshot_authority()
        except ProvisioningError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    coordinator = None
    invitation_admission = None
    invitation_owned = False
    if not cloud_provisioning_enabled(uid):
        try:
            coordinator = await _coordinator()
            invitation_owned = await self_hosted_runtime_authority_required(
                uid,
                repository=coordinator.repository,
            )
            if invitation_owned and not self_hosted_configured:
                raise ProvisioningError("self_hosted_invitation_runtime_disabled", retryable=True)
            if self_hosted_configured:
                invitation_admission = await self_hosted_invitation_admission(
                    uid,
                    repository=coordinator.repository,
                )
            if invitation_owned and not self_hosted_provisioning_enabled(uid, admission=invitation_admission):
                raise ProvisioningError("invitation_authority_required", retryable=False)
        except ProvisioningError as exc:
            raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "self_hosted_invitation_authority_unavailable"},
            ) from exc
    if not any_provisioning_enabled(uid, self_hosted_admission=invitation_admission):
        receipt = await _retained_receipt(uid, payload.target_schema_version)
        if receipt:
            return receipt
        raise HTTPException(status_code=503, detail={"code": "provisioning_disabled"})
    hermes_required = not cloud_provisioning_enabled(uid) and (
        self_hosted_provisioning_enabled(uid, admission=invitation_admission) or provisioning_enabled(uid)
    )
    if hermes_required and authority_snapshot is None:
        try:
            authority_snapshot = HermesProvisionClient.snapshot_authority()
        except ProvisioningError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    identity = _verified_identity(uid, payload)
    if authority_snapshot is not None:
        try:
            HermesProvisionClient.snapshot_authority(authority_snapshot)
        except ProvisioningError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    coordinator = coordinator or await _coordinator()
    try:
        job, binding, claimed = await coordinator.ensure_job(
            identity=identity,
            target_schema_version=target_schema_version,
            client_request_id=payload.client_request_id,
            request_payload={
                **_payload_dict(payload),
                "effective_target_schema_version": target_schema_version,
            },
            authority_snapshot=authority_snapshot,
        )
    except IdentityConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    except ProvisioningError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 409,
            detail={"code": exc.code},
        ) from exc

    receipt = public_receipt(job, binding, effective_voice_mode=_resolved_voice_mode(uid))
    if claimed:
        try:
            if authority_snapshot is not None:
                HermesProvisionClient.snapshot_authority(authority_snapshot)
            background_tasks.add_task(
                coordinator.process_claimed_job,
                job=job,
                identity=identity,
                authority_snapshot=authority_snapshot,
            )
        except ProvisioningError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
        response.status_code = 202
    elif receipt["state"] in {"queued", "provisioning", "retryable", "rolling_back"}:
        response.status_code = 202
    elif receipt["state"] in {"blocked", "manual_intervention"}:
        response.status_code = 503 if receipt.get("error_code") == "provisioning_disabled" else 409
    return receipt


@router.get("/status")
async def onboarding_status(
    target_schema_version: str = DEFAULT_TARGET_SCHEMA_VERSION,
    uid: str = Depends(get_exact_firebase_uid),
) -> dict[str, Any]:
    if not SCHEMA_VERSION_RE.fullmatch(target_schema_version):
        raise HTTPException(status_code=400, detail={"code": "invalid_target_schema_version"})
    try:
        target_schema_version = effective_target_schema_version(uid, target_schema_version)
    except ProvisioningError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    cloud_required = cloud_provisioning_enabled(uid)
    self_hosted_configured = self_hosted_provisioning_configured() and not cloud_required
    try:
        repository = await EllaProvisioningRepository.create()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "self_hosted_invitation_authority_unavailable"},
        ) from exc
    invitation_admission = None
    invitation_owned = False
    if not cloud_required:
        try:
            invitation_owned = await self_hosted_runtime_authority_required(uid, repository=repository)
            if invitation_owned and not self_hosted_configured:
                raise ProvisioningError("self_hosted_invitation_runtime_disabled", retryable=True)
            if self_hosted_configured:
                invitation_admission = await self_hosted_invitation_admission(uid, repository=repository)
            if invitation_owned and not self_hosted_provisioning_enabled(uid, admission=invitation_admission):
                raise ProvisioningError("invitation_authority_required", retryable=False)
        except ProvisioningError as exc:
            raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc
    if not any_provisioning_enabled(uid, self_hosted_admission=invitation_admission):
        receipt = await _retained_receipt(uid, target_schema_version)
        if receipt:
            return receipt
        raise HTTPException(status_code=503, detail={"code": "provisioning_disabled"})
    try:
        await repository.assert_schema_ready()
        if cloud_required:
            await repository.assert_cloud_schema_ready()
        elif self_hosted_provisioning_enabled(uid, admission=invitation_admission):
            await repository.assert_self_hosted_invite_schema_ready()
    except ProvisioningSchemaNotReadyError as exc:
        logger.error("Ella provisioning status schema is incomplete: %s", ", ".join(exc.missing))
        raise HTTPException(status_code=503, detail={"code": "provisioning_schema_not_ready"}) from exc
    job = await repository.get_job(uid, target_schema_version)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "setup_not_started"})
    if cloud_required:
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
            template_version=target_schema_version,
            target_mode="hermes-cloud-chat",
            required_provider=CLOUD_RUNTIME_PROVIDER,
            authority_lineage=authority.lineage,
            model=CLOUD_RUNTIME_MODEL,
        )
    else:
        self_hosted_required = self_hosted_provisioning_enabled(uid, admission=invitation_admission)
        binding = await repository.resolve_active_runtime(
            uid,
            template_version=target_schema_version,
            target_mode="hermes-chat" if self_hosted_required else None,
            required_provider="hermes",
            authority_lineage=current_self_hosted_runtime_lineage() if self_hosted_required else None,
            model=SELF_HOSTED_RUNTIME_MODEL if self_hosted_required else CLOUD_RUNTIME_MODEL,
        )
    if not binding and cloud_required:
        binding = await repository.resolve_cloud_binding_state(uid)
    return public_receipt(job, binding, effective_voice_mode=_resolved_voice_mode(uid))
