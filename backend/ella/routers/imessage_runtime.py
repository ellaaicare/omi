"""Transport-only boundary for verified self-hosted iMessage text DMs."""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from database.honcho_attestation import authority_credential
from ella.services.imessage_runtime import (
    ImessageDeliveryIdentity,
    ImessageInbound,
    ImessageRuntimeError,
    ImessageRuntimeService,
)

NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class StrictTransportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransportIdentityIn(StrictTransportModel):
    line_identity: str = Field(min_length=1, max_length=512)
    contact_identity: str = Field(min_length=1, max_length=512)
    connection_id: str = Field(min_length=1, max_length=512)


class InboundMessageIn(TransportIdentityIn):
    provider_message_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=32_768)
    occurred_at: datetime
    attachment_count: int = Field(default=0, ge=0, le=100)
    group_message: bool = False


class DeliveryIdentityIn(TransportIdentityIn):
    receipt_id: uuid.UUID
    delivery_idempotency_key: uuid.UUID
    binding_generation: int = Field(ge=1)


class DeliveryAckIn(DeliveryIdentityIn):
    outbound_provider_message_id: str = Field(min_length=1, max_length=512)


class DeliveryUncertainIn(DeliveryIdentityIn):
    error_code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")


def _require_transport(presented: Optional[str]) -> None:
    expected = authority_credential("ELLA_IMESSAGE_TRANSPORT_TOKEN", strip=False) or ""
    if len(expected) < 32 or expected != expected.strip():
        raise HTTPException(
            status_code=503,
            detail={"code": "imessage_transport_auth_not_configured"},
            headers=NO_STORE_HEADERS,
        )
    if not presented or presented != presented.strip() or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_imessage_transport_token"},
            headers=NO_STORE_HEADERS,
        )


def _http_error(exc: ImessageRuntimeError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "retryable": exc.retryable},
        headers=NO_STORE_HEADERS,
    )


def _mark_no_store(response: Response) -> None:
    response.headers.update(NO_STORE_HEADERS)


def create_imessage_runtime_router(
    service_factory: Optional[Callable[[], Awaitable[ImessageRuntimeService]]] = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/ella/internal/imessage", tags=["Ella iMessage Runtime"])

    async def service() -> ImessageRuntimeService:
        return await service_factory() if service_factory is not None else await ImessageRuntimeService.create()

    def identity(payload: DeliveryIdentityIn) -> ImessageDeliveryIdentity:
        return ImessageDeliveryIdentity(
            line_identity=payload.line_identity,
            contact_identity=payload.contact_identity,
            connection_id=payload.connection_id,
            receipt_id=str(payload.receipt_id),
            delivery_idempotency_key=str(payload.delivery_idempotency_key),
            binding_generation=payload.binding_generation,
        )

    @router.post("/heartbeat")
    async def heartbeat(
        payload: TransportIdentityIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).heartbeat(**payload.model_dump())
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/inbound")
    async def inbound(
        payload: InboundMessageIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).ingest(ImessageInbound(**payload.model_dump()))
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/delivery/start")
    async def delivery_start(
        payload: DeliveryIdentityIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).start_delivery(identity(payload))
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/delivery/ack")
    async def delivery_ack(
        payload: DeliveryAckIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).acknowledge_delivery(
                identity(payload),
                outbound_provider_message_id=payload.outbound_provider_message_id,
            )
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/delivery/uncertain")
    async def delivery_uncertain(
        payload: DeliveryUncertainIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).mark_delivery_uncertain(
                identity(payload),
                error_code=payload.error_code,
            )
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/delivery/reconcile")
    async def delivery_reconcile(
        payload: DeliveryIdentityIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).reconcile_pre_send_delivery(identity(payload))
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    @router.post("/deregister")
    async def deregister(
        payload: TransportIdentityIn,
        response: Response,
        token: Optional[str] = Header(default=None, alias="X-Ella-Imessage-Transport-Token"),
    ) -> dict:
        _require_transport(token)
        _mark_no_store(response)
        try:
            return await (await service()).deregister(**payload.model_dump())
        except ImessageRuntimeError as exc:
            raise _http_error(exc) from exc

    return router


router = create_imessage_runtime_router()
