"""Authenticated Ella invitation redemption API."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from database import invitations
from utils.other import endpoints as auth

router = APIRouter(prefix="/v1/invite", tags=["ella-invites"])
logger = logging.getLogger("ella.invites")


class InviteRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)


def _canonical_address(value: str) -> Optional[str]:
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return None


def _source_address(request: Request) -> str:
    direct = _canonical_address(request.client.host if request.client else "") or "unknown"
    trusted = {
        address
        for item in os.getenv("ELLA_INVITE_TRUSTED_PROXY_IPS", "").split(",")
        if (address := _canonical_address(item))
    }
    if direct not in trusted:
        return direct
    forwarded = request.headers.get("CF-Connecting-IP", "").strip()
    if not forwarded:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    return _canonical_address(forwarded) or direct


def _diagnostic_receipt() -> tuple[str, str]:
    return f"INV-{secrets.token_hex(4).upper()}", str(uuid.uuid4())


def _invalid_request() -> HTTPException:
    support_code, correlation_id = _diagnostic_receipt()
    return HTTPException(
        status_code=400,
        detail={
            "code": "invalid",
            "support_code": support_code,
            "correlation_id": correlation_id,
        },
    )


async def _validated_payload(request: Request) -> InviteRedeemRequest:
    try:
        return InviteRedeemRequest.model_validate(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise _invalid_request() from exc


@router.post(
    "/redeem",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": InviteRedeemRequest.model_json_schema(),
                }
            },
        }
    },
)
async def redeem_invite(
    request: Request,
    authenticated_uid: str = Depends(auth.get_current_user_uid),
    app_build: str = Header(default="", alias="X-Ella-App-Build"),
) -> dict:
    payload = await _validated_payload(request)
    try:
        return await invitations.redeem_invitation(
            uid=authenticated_uid,
            code=payload.code,
            source_address=_source_address(request),
            app_build=app_build,
        )
    except invitations.InviteRedemptionFailure as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    except invitations.InviteConfigurationError as exc:
        support_code, correlation_id = _diagnostic_receipt()
        logger.error(
            "Invitation redemption configuration unavailable type=%s correlation_id=%s",
            type(exc).__name__,
            correlation_id,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "support_code": support_code,
                "correlation_id": correlation_id,
            },
        ) from exc
    except Exception as exc:
        support_code, correlation_id = _diagnostic_receipt()
        logger.error(
            "Invitation redemption unavailable type=%s correlation_id=%s",
            type(exc).__name__,
            correlation_id,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "support_code": support_code,
                "correlation_id": correlation_id,
            },
        ) from exc
