"""Authenticated, idempotent Ella Hermes onboarding endpoints."""

from __future__ import annotations

from ella.services.normal_account_routing import qualify_receipt, qualify_ensure_receipt, observe_enrollment_stage

import asyncio
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
    self_hosted_invitation_lineage,
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
        # Import lazily inside the function: the module-level import in this
        # router runs `database._client.db = firestore.Client()` transitively at
        # import time, which breaks test collection / jobs that lack a
        # FIRESTORE_EMULATOR_HOST (DefaultCredentialsError on import). Runtime
        # degradation is already graceful under the try/except below.
        from database import app_settings as app_settings_db

        voice = app_settings_db.get_voice_settings(uid) or {}
        raw = str(voice.get("voice_mode") or "").strip()
        if not raw:
            return ""
        return normalize_voice_mode(raw)
    except Exception:  # noqa: BLE001 - never let settings lookup break onboarding
        logger.warning("voice-settings lookup failed for uid=%s; defaulting to empty", uid)
        return ""


async def _retained_receipt_for_repository(
    uid: str,
    target_schema_version: str,
    repository: EllaProvisioningRepository,
) -> Optional[dict[str, Any]]:
    """Return compatibility only for a usable retained runtime owned by this subject."""
    if target_schema_version != DEFAULT_TARGET_SCHEMA_VERSION or runtime_bindings_enabled(uid):
        return None
    try:
        if await repository.has_active_retained_runtime(uid):
            logger.info("Using retained-account onboarding compatibility")
            qualified = await qualify_receipt(uid, retained_compatibility_receipt(target_schema_version), repository)
            if qualified['state'] == 'ready':
                return qualified
            # Incomplete compatibility is not an early terminal lifecycle.
            # ensure_job performs exact-owner, idempotent repair or returns hold.
            return None
    except Exception as exc:
        logger.error("Retained-account onboarding lookup failed")
        raise ProvisioningError("provisioning_unavailable", retryable=True) from exc
    return None


async def _retained_receipt(uid: str, target_schema_version: str) -> Optional[dict[str, Any]]:
    """Return a public receipt only for an already-routed retained account."""
    if target_schema_version != DEFAULT_TARGET_SCHEMA_VERSION or runtime_bindings_enabled(uid):
        return None
    try:
        repository = await EllaProvisioningRepository.create()
        return await _retained_receipt_for_repository(uid, target_schema_version, repository)
    except ProvisioningError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code}) from exc
    except Exception as exc:
        logger.error("Retained-account onboarding lookup failed")
        raise HTTPException(status_code=503, detail={"code": "provisioning_unavailable"}) from exc


def _payload_dict(payload: OnboardingEnsureRequest) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload.dict()


def _read_control_with_budget(read,*,transient_errors,clock,sleep,observe):
    """At most two FRESH metadata reads inside the original 3s SDK budget.

    No cached active state, SDK retry object, provider operation or deadline
    extension. A timeout using the whole budget leaves no retry allowance.
    """
    deadline=clock()+3.0
    for attempt in range(2):
        remaining=deadline-clock()
        if remaining<=0:raise TimeoutError('onboarding_control_read_budget_exhausted')
        try:
            value=read(remaining)
            if clock()>deadline:raise TimeoutError('onboarding_control_read_budget_exhausted')
        except Exception as exc:
            remaining=deadline-clock()
            retry=attempt==0 and isinstance(exc,transient_errors) and remaining>0.1
            observe('pause_read_retry' if retry else 'pause_read_error',exc)
            if not retry:raise
            sleep(min(0.05,remaining))
        else:
            if attempt:observe('pause_read_recovered',None)
            return value


def _read_onboarding_pause_control(*,uid=None,observation_id=None):
    import time
    from google.api_core.exceptions import ServiceUnavailable,Aborted,InternalServerError,DeadlineExceeded
    from database._client import db
    from database.normal_enrichment_activation import reference
    ref=reference(db)
    def read(timeout):
        snap=ref.get(timeout=timeout,retry=None)
        return snap.to_dict() if snap.exists else None
    return _read_control_with_budget(read,
        transient_errors=(ServiceUnavailable,Aborted,InternalServerError,DeadlineExceeded),
        clock=time.monotonic,sleep=time.sleep,
        observe=lambda stage,error:observe_enrollment_stage(uid,observation_id,stage,error=error))


async def _paused_ensure_status_receipt(*,uid=None,observation_id=None):
    """While normal processing is paused, ensure may only initiate status polling.

    No account/route lookup, provisioning, enrollment, credential, job or grant
    writes here. Caller authentication and current consent remain unchanged.
    Active/non-normal behavior is left to the existing authority below.
    """
    from ella.services.normal_account_routing import normal_capture_admission_enabled
    if not normal_capture_admission_enabled():return None
    try:
        control=await asyncio.wait_for(asyncio.to_thread(_read_onboarding_pause_control,uid=uid,observation_id=observation_id),timeout=3.5)
        if type(control) is not dict or type(control.get('state')) is not str:
            raise ValueError('onboarding_control_snapshot_invalid')
        state=control['state']
        if state not in {'active','paused'}:raise ValueError('onboarding_control_snapshot_invalid')
    except Exception as exc:
        observe_enrollment_stage(uid,observation_id,'pause_control_error',error=exc)
        raise HTTPException(status_code=503,detail={'code':'onboarding_pause_state_unavailable','retryable':True}) from None
    if state!='paused':return None
    return {'state':'checking','stage':'checking_existing_account','retryable':True,
        'retry_after_ms':1000,'read_only':True,'account_check_pending':True,
        'account_ready':False,'capture_ready':False,'enrichment_ready':False,
        'background_processing_paused':True,
        'enrichment_error_code':'account_transcript_processing_paused'}


async def _ensure_onboarding(
    payload: OnboardingEnsureRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    uid: str = "",
    observation_id: str = "",
) -> dict[str, Any]:
    if not SCHEMA_VERSION_RE.fullmatch(payload.target_schema_version):
        raise HTTPException(status_code=400, detail={"code": "invalid_target_schema_version"})
    read_only=await _paused_ensure_status_receipt(uid=uid,observation_id=observation_id)
    if read_only is not None:
        response.status_code=202
        return read_only
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
            if not invitation_owned:
                receipt = await _retained_receipt_for_repository(
                    uid,
                    payload.target_schema_version,
                    coordinator.repository,
                )
                if receipt:
                    response.status_code = 200 if receipt["state"] == "ready" else 503
                    return await qualify_ensure_receipt(uid,receipt,coordinator.repository,observation_id=observation_id)
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
            existing=coordinator or await _coordinator()
            return await qualify_ensure_receipt(uid,receipt,existing.repository,observation_id=observation_id)
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
    read_only=await _paused_ensure_status_receipt(uid=uid,observation_id=observation_id)
    if read_only is not None:
        response.status_code=202
        return read_only
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

    receipt = await qualify_ensure_receipt(uid, public_receipt(job, binding, effective_voice_mode=_resolved_voice_mode(uid)), coordinator.repository, observation_id=observation_id)
    if claimed:
        try:
            if authority_snapshot is not None:
                HermesProvisionClient.snapshot_authority(authority_snapshot)
            background_tasks.add_task(
                _process_then_enroll,
                coordinator=coordinator,
                observation_id=observation_id,
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
    if not claimed and receipt["state"] in {"queued", "provisioning", "retryable", "rolling_back"}:
        background_tasks.add_task(_follow_authenticated_readiness,uid=uid,
            target_schema_version=target_schema_version,repository=coordinator.repository,
            observation_id=observation_id)
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
            if not invitation_owned:
                receipt = await _retained_receipt_for_repository(uid, target_schema_version, repository)
                if receipt:
                    return receipt
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
        binding = None
        for runtime_lineage in authority.runtime_target_lineages:
            binding = await repository.resolve_active_runtime(
                uid,
                template_version=target_schema_version,
                target_mode="hermes-cloud-chat",
                required_provider=CLOUD_RUNTIME_PROVIDER,
                authority_lineage=runtime_lineage,
                model=CLOUD_RUNTIME_MODEL,
            )
            if binding:
                break
    else:
        self_hosted_required = self_hosted_provisioning_enabled(uid, admission=invitation_admission)
        authority_lineage = (
            self_hosted_invitation_lineage(invitation_admission)
            if self_hosted_required and invitation_admission is not None
            else current_self_hosted_runtime_lineage() if self_hosted_required else None
        )
        binding = await repository.resolve_active_runtime(
            uid,
            template_version=target_schema_version,
            target_mode="hermes-chat" if self_hosted_required else None,
            required_provider="hermes",
            authority_lineage=authority_lineage,
            model=SELF_HOSTED_RUNTIME_MODEL if self_hosted_required else CLOUD_RUNTIME_MODEL,
        )
    if not binding and cloud_required:
        binding = await repository.resolve_cloud_binding_state(uid)
    return await qualify_receipt(uid, public_receipt(job, binding, effective_voice_mode=_resolved_voice_mode(uid)), repository)


# Follow-up is owned by an authenticated POST /ensure. GET /status stays read-only.
# Only a server-returned ready receipt may enter the existing idempotent adapter.
_PENDING_ENROLLMENT_FOLLOWUPS = set()

async def _follow_authenticated_readiness(*,uid,target_schema_version,repository,observation_id):
    import hashlib
    key=(hashlib.sha256(uid.encode()).hexdigest(),target_schema_version)
    if key in _PENDING_ENROLLMENT_FOLLOWUPS or len(_PENDING_ENROLLMENT_FOLLOWUPS)>=32:
        observe_enrollment_stage(uid,observation_id,'followup_deferred')
        return
    _PENDING_ENROLLMENT_FOLLOWUPS.add(key)
    try:
        for attempt in range(12):
            try:
                receipt=await asyncio.wait_for(onboarding_status(
                    target_schema_version=target_schema_version,uid=uid),timeout=12)
                if receipt.get('state')=='ready':
                    await qualify_ensure_receipt(uid,receipt,repository,observation_id=observation_id)
                    return
                if receipt.get('state') not in {'queued','provisioning','retryable','rolling_back'}:
                    observe_enrollment_stage(uid,observation_id,'followup_terminal',receipt=receipt)
                    return
            except HTTPException as exc:
                observe_enrollment_stage(uid,observation_id,'followup_error',error=exc)
                return
            except Exception as exc:
                observe_enrollment_stage(uid,observation_id,'followup_error',error=exc)
                return
            if attempt<11:await asyncio.sleep(5)
        observe_enrollment_stage(uid,observation_id,'followup_exhausted')
    finally:
        _PENDING_ENROLLMENT_FOLLOWUPS.discard(key)

async def _process_then_enroll(*,coordinator,job,identity,authority_snapshot,observation_id):
    # Existing provisioning runs once. Never retry/convert a failed or cancelled
    # provisioning task into enrollment, and never send another provider request.
    await coordinator.process_claimed_job(job=job,identity=identity,authority_snapshot=authority_snapshot)
    await _follow_authenticated_readiness(uid=identity.uid,
        target_schema_version=job['target_schema_version'],repository=coordinator.repository,
        observation_id=observation_id)

@router.post('/ensure',dependencies=[Depends(require_current_ai_consent)])
async def ensure_onboarding(payload:OnboardingEnsureRequest,background_tasks:BackgroundTasks,
                            response:Response,uid:str=Depends(get_exact_firebase_uid))->dict[str,Any]:
    from uuid import uuid4
    observation_id=uuid4().hex
    observe_enrollment_stage(uid,observation_id,'entry')
    try:
        receipt=await _ensure_onboarding(payload,background_tasks,response,uid,observation_id)
    except Exception as exc:
        observe_enrollment_stage(uid,observation_id,'ensure_error',error=exc)
        raise
    observe_enrollment_stage(uid,observation_id,'response',receipt=receipt)
    return receipt
