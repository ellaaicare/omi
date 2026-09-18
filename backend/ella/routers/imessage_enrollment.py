"""Authenticated app and transport-only routes for iMessage enrollment."""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from database.honcho_attestation import authority_credential
from ella.services.imessage_enrollment import (
    ImessageEnrollmentError,
    ImessageEnrollmentService,
    consent_policy,
)
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

router = APIRouter(tags=["iMessage Enrollment"])
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class ImessageConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["granted", "declined", "revoked"]
    policy_version: str = Field(min_length=1, max_length=80)
    processor_set_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    scope_version: str = Field(min_length=1, max_length=100)
    scope_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    request_id: uuid.UUID
    app_version: str = Field(min_length=1, max_length=80)
    build_number: str = Field(min_length=1, max_length=40)


class EnrollmentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handset_e164: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    consent_receipt_id: uuid.UUID
    idempotency_key: uuid.UUID


class EnrollmentRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=1)
    idempotency_key: uuid.UUID


class InboundProofRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_destination: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    handset_e164: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    code: str = Field(pattern=r"^[0-9]{6}$")
    provider_message_id: str = Field(min_length=1, max_length=256)
    line_identity: str = Field(min_length=1, max_length=256)
    contact_identity: str = Field(min_length=1, max_length=256)


async def get_imessage_enrollment_service() -> ImessageEnrollmentService:
    try:
        return await ImessageEnrollmentService.create()
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "imessage_enrollment_unavailable"}) from exc


def require_imessage_transport(
    provided: Annotated[str | None, Header(alias="X-Ella-Imessage-Transport-Token")] = None,
) -> None:
    configured = authority_credential("ELLA_IMESSAGE_TRANSPORT_TOKEN", strip=False) or ""
    if len(configured) < 32 or configured != configured.strip():
        raise HTTPException(status_code=503, detail={"code": "imessage_transport_auth_not_configured"})
    if not provided or not secrets.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail={"code": "invalid_imessage_transport_credential"})


def _raise_service_error(exc: ImessageEnrollmentError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code},
        headers=NO_STORE_HEADERS,
    ) from exc


def _mark_no_store(response: Response) -> None:
    response.headers.update(NO_STORE_HEADERS)


@router.get("/v1/ella/imessage/consent/policy")
def get_imessage_consent_policy(response: Response) -> dict:
    _mark_no_store(response)
    return consent_policy()


@router.post("/v1/ella/imessage/consent")
async def submit_imessage_consent(
    request: ImessageConsentRequest,
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: ImessageEnrollmentService = Depends(get_imessage_enrollment_service),
) -> dict:
    _mark_no_store(response)
    try:
        return await service.submit_consent(
            uid=uid,
            decision=request.decision,
            policy_version=request.policy_version,
            processor_set_hash=request.processor_set_hash,
            scope_version=request.scope_version,
            scope_hash=request.scope_hash,
            request_id=request.request_id,
            app_version=request.app_version,
            build_number=request.build_number,
        )
    except ImessageEnrollmentError as exc:
        _raise_service_error(exc)


@router.get("/v1/ella/imessage/enrollment")
async def get_imessage_enrollment(
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: ImessageEnrollmentService = Depends(get_imessage_enrollment_service),
) -> dict:
    _mark_no_store(response)
    try:
        return await service.status(uid=uid)
    except ImessageEnrollmentError as exc:
        _raise_service_error(exc)


@router.post("/v1/ella/imessage/enrollment/start")
async def start_imessage_enrollment(
    request: EnrollmentStartRequest,
    uid: str = Depends(get_exact_firebase_uid),
    service: ImessageEnrollmentService = Depends(get_imessage_enrollment_service),
) -> JSONResponse:
    try:
        body, created = await service.start(
            uid=uid,
            handset_e164=request.handset_e164,
            consent_receipt_id=request.consent_receipt_id,
            idempotency_key=request.idempotency_key,
        )
    except ImessageEnrollmentError as exc:
        _raise_service_error(exc)
    return JSONResponse(
        status_code=201 if created else 200,
        content=body,
        headers=NO_STORE_HEADERS,
    )


@router.post("/v1/ella/imessage/enrollment/revoke")
async def revoke_imessage_enrollment(
    request: EnrollmentRevokeRequest,
    response: Response,
    uid: str = Depends(get_exact_firebase_uid),
    service: ImessageEnrollmentService = Depends(get_imessage_enrollment_service),
) -> dict:
    _mark_no_store(response)
    try:
        return await service.revoke(
            uid=uid,
            expected_generation=request.expected_generation,
            idempotency_key=request.idempotency_key,
        )
    except ImessageEnrollmentError as exc:
        _raise_service_error(exc)


@router.post(
    "/v1/ella/internal/imessage/proof",
    dependencies=[Depends(require_imessage_transport)],
)
async def verify_imessage_proof(
    request: InboundProofRequest,
    response: Response,
    service: ImessageEnrollmentService = Depends(get_imessage_enrollment_service),
) -> dict:
    _mark_no_store(response)
    try:
        return await service.verify_proof(
            assigned_destination=request.assigned_destination,
            handset_e164=request.handset_e164,
            code=request.code,
            provider_message_id=request.provider_message_id,
            line_identity=request.line_identity,
            contact_identity=request.contact_identity,
        )
    except ImessageEnrollmentError as exc:
        _raise_service_error(exc)
