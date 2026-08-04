"""Authenticated first-party boundary for the persistent Photon sidecar."""

from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from database.ella_provisioning import EllaProvisioningRepository
from ella.routers.canonical_events import PostgresCanonicalEventStore, _get_pool
from ella.services.hermes_cloud_photon import (
    HermesCloudPhotonAdapter,
    PhotonDeliveryAck,
    PhotonInboundEnvelope,
    PhotonSidecarPreflight,
)
from ella.services.runtime_errors import ProvisioningError
from utils.ella.exact_firebase_auth import ELLA_SUBJECT_UID_HEADER, get_exact_service_authority


class PhotonPreflightIn(BaseModel):
    line_identity: str = Field(min_length=1, max_length=512)
    contact_identity: str = Field(min_length=1, max_length=512)
    connection_id: str = Field(min_length=1, max_length=512)
    oauth_expires_at: datetime
    allow_all: bool = False
    allowed_contact_count: int = Field(ge=0, le=10)
    attachments_enabled: bool = False
    groups_enabled: bool = False
    command_tier_version: str = Field(min_length=1, max_length=120)
    allowed_regular_commands: list[str] = Field(default_factory=list, max_length=10)


class PhotonInboundIn(BaseModel):
    line_identity: str = Field(min_length=1, max_length=512)
    contact_identity: str = Field(min_length=1, max_length=512)
    connection_id: str = Field(min_length=1, max_length=512)
    provider_message_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=32768)
    occurred_at: datetime
    conversation_initiation: bool = False
    attachment_count: int = Field(default=0, ge=0, le=100)
    group_message: bool = False
    synthetic: bool = False


class PhotonDeliveryAckIn(BaseModel):
    receipt_id: str
    delivery_idempotency_key: str
    connection_id: str = Field(min_length=1, max_length=512)
    outbound_provider_message_id: str = Field(min_length=1, max_length=512)
    acknowledged_at: datetime


def _require_sidecar_authority(presented: Optional[str], subject_uid: Optional[str]) -> str:
    expected = os.getenv("ELLA_HERMES_CLOUD_PHOTON_SIDECAR_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(
            status_code=503,
            detail={"code": "photon_sidecar_auth_not_configured"},
        )
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail={"code": "invalid_photon_sidecar_token"})
    owner_uid = os.getenv("ELLA_HERMES_CLOUD_PHOTON_INTERNAL_OWNER_UID", "").strip()
    if not owner_uid:
        raise HTTPException(status_code=503, detail={"code": "photon_owner_subject_not_configured"})
    authority = get_exact_service_authority(
        provided_service_key=presented,
        configured_service_key=expected,
        service_subject_uid=subject_uid,
        service="photon_sidecar",
    )
    return authority.require_uid(owner_uid, feature="Photon sidecar")


def _http_error(exc: ProvisioningError) -> HTTPException:
    status = 503 if exc.retryable else 409
    return HTTPException(status_code=status, detail={"code": exc.code})


def _public_result(result: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "receipt_id": result.receipt_id,
        "status": result.status,
        "duplicate": result.duplicate,
        "delivery_idempotency_key": result.delivery_idempotency_key,
        "outbound_text": result.outbound_text,
        "canonical_inbound_event_id": result.canonical_inbound_event_id,
        "canonical_outbound_event_id": result.canonical_outbound_event_id,
    }


def create_photon_router(
    adapter_factory: Optional[Callable[[], Awaitable[HermesCloudPhotonAdapter]]] = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/ella/internal/hermes-cloud/photon",
        tags=["Hermes Cloud Photon"],
    )

    async def _adapter() -> HermesCloudPhotonAdapter:
        if adapter_factory is not None:
            return await adapter_factory()
        pool = await _get_pool()
        return HermesCloudPhotonAdapter(
            repository=EllaProvisioningRepository(pool),
            event_store=PostgresCanonicalEventStore(),
        )

    @router.post("/preflight")
    async def preflight(
        payload: PhotonPreflightIn,
        x_sidecar_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Photon-Sidecar-Token",
        ),
        subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
    ) -> dict[str, Any]:
        _require_sidecar_authority(x_sidecar_token, subject_uid)
        try:
            receipt = await (await _adapter()).preflight(
                PhotonSidecarPreflight(
                    line_identity=payload.line_identity,
                    contact_identity=payload.contact_identity,
                    connection_id=payload.connection_id,
                    oauth_expires_at=payload.oauth_expires_at,
                    allow_all=payload.allow_all,
                    allowed_contact_count=payload.allowed_contact_count,
                    attachments_enabled=payload.attachments_enabled,
                    groups_enabled=payload.groups_enabled,
                    command_tier_version=payload.command_tier_version,
                    allowed_regular_commands=tuple(payload.allowed_regular_commands),
                )
            )
        except ProvisioningError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "status": "ready", "receipt": receipt}

    @router.post("/inbound")
    async def inbound(
        payload: PhotonInboundIn,
        x_sidecar_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Photon-Sidecar-Token",
        ),
        subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
    ) -> dict[str, Any]:
        _require_sidecar_authority(x_sidecar_token, subject_uid)
        try:
            result = await (await _adapter()).handle_inbound(
                PhotonInboundEnvelope(
                    line_identity=payload.line_identity,
                    contact_identity=payload.contact_identity,
                    connection_id=payload.connection_id,
                    provider_message_id=payload.provider_message_id,
                    text=payload.text,
                    occurred_at=payload.occurred_at,
                    conversation_initiation=payload.conversation_initiation,
                    attachment_count=payload.attachment_count,
                    group_message=payload.group_message,
                    synthetic=payload.synthetic,
                )
            )
        except ProvisioningError as exc:
            if exc.code == "photon_sender_not_allowed":
                return {
                    "ok": True,
                    "status": "ignored",
                    "duplicate": False,
                }
            raise _http_error(exc) from exc
        return _public_result(result)

    @router.post("/delivery-ack")
    async def delivery_ack(
        payload: PhotonDeliveryAckIn,
        x_sidecar_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Photon-Sidecar-Token",
        ),
        subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
    ) -> dict[str, Any]:
        _require_sidecar_authority(x_sidecar_token, subject_uid)
        try:
            result = await (await _adapter()).acknowledge_delivery(
                PhotonDeliveryAck(
                    receipt_id=payload.receipt_id,
                    delivery_idempotency_key=payload.delivery_idempotency_key,
                    connection_id=payload.connection_id,
                    outbound_provider_message_id=payload.outbound_provider_message_id,
                    acknowledged_at=payload.acknowledged_at,
                )
            )
        except ProvisioningError as exc:
            raise _http_error(exc) from exc
        return _public_result(result)

    return router


router = create_photon_router()
