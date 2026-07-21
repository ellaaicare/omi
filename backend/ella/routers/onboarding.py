"""Authenticated, idempotent Ella Hermes onboarding endpoints."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from database.ella_provisioning import EllaProvisioningRepository, IdentityConflictError
from ella.services.provisioning import (
    DEFAULT_TARGET_SCHEMA_VERSION,
    ProvisioningCoordinator,
    VerifiedIdentity,
    provisioning_enabled,
    public_receipt,
)
from utils.other import endpoints as auth

logger = logging.getLogger("ella.onboarding")
router = APIRouter(prefix="/v1/ella/onboarding", tags=["ella-onboarding"])
SCHEMA_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


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
        logger.warning("Firebase user lookup failed for authenticated uid=%s: %s", uid, type(exc).__name__)
        raise HTTPException(status_code=401, detail={"code": "auth_required"}) from exc

    email = str(getattr(firebase_user, "email", "") or "").strip()
    if not email:
        raise HTTPException(status_code=409, detail={"code": "identity_missing_email"})
    name = str(getattr(firebase_user, "display_name", "") or "").strip() or email.split("@", 1)[0]
    return VerifiedIdentity(
        uid=uid,
        email=email,
        name=name,
        timezone=payload.client.timezone or "America/Los_Angeles",
    )


async def _coordinator() -> ProvisioningCoordinator:
    return ProvisioningCoordinator(await EllaProvisioningRepository.create())


def _payload_dict(payload: OnboardingEnsureRequest) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload.dict()


@router.post("/ensure")
async def ensure_onboarding(
    payload: OnboardingEnsureRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    uid: str = Depends(auth.get_current_user_uid),
) -> dict[str, Any]:
    if not provisioning_enabled():
        raise HTTPException(status_code=503, detail={"code": "provisioning_disabled"})
    if not SCHEMA_VERSION_RE.fullmatch(payload.target_schema_version):
        raise HTTPException(status_code=400, detail={"code": "invalid_target_schema_version"})
    identity = _verified_identity(uid, payload)
    coordinator = await _coordinator()
    try:
        job, binding, claimed = await coordinator.ensure_job(
            identity=identity,
            target_schema_version=payload.target_schema_version,
            client_request_id=payload.client_request_id,
            request_payload=_payload_dict(payload),
        )
    except IdentityConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc

    receipt = public_receipt(job, binding)
    if claimed:
        background_tasks.add_task(coordinator.process_claimed_job, job=job, identity=identity)
        response.status_code = 202
    elif receipt["state"] in {"queued", "provisioning", "degraded"}:
        response.status_code = 202
    elif receipt["state"] == "blocked":
        response.status_code = 503 if receipt.get("error_code") == "provisioning_disabled" else 409
    return receipt


@router.get("/status")
async def onboarding_status(
    target_schema_version: str = DEFAULT_TARGET_SCHEMA_VERSION,
    uid: str = Depends(auth.get_current_user_uid),
) -> dict[str, Any]:
    if not provisioning_enabled():
        raise HTTPException(status_code=503, detail={"code": "provisioning_disabled"})
    if not SCHEMA_VERSION_RE.fullmatch(target_schema_version):
        raise HTTPException(status_code=400, detail={"code": "invalid_target_schema_version"})
    repository = await EllaProvisioningRepository.create()
    job = await repository.get_job(uid, target_schema_version)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "setup_not_started"})
    binding = await repository.resolve_active_runtime(
        uid,
        template_version=target_schema_version,
    )
    return public_receipt(job, binding)
