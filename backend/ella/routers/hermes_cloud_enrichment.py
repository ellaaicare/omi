"""Authenticated first-party OMI enrichment boundary for Hermes Cloud."""

from __future__ import annotations

import hmac
import os
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ella.services.hermes_cloud_enrichment import HermesCloudEnrichmentService
from ella.services.runtime_errors import ProvisioningError
from utils.ella.exact_firebase_auth import (
    ELLA_SUBJECT_UID_HEADER,
    EllaRequestAuthority,
    get_exact_service_authority,
)


class HermesCloudEnrichmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(min_length=1, max_length=256)
    outbox_job_id: Optional[str] = Field(
        default=None,
        min_length=68,
        max_length=68,
        pattern=r"^hce_[0-9a-f]{64}$",
    )
    client_interaction_id: Optional[str] = Field(
        default=None,
        min_length=79,
        max_length=79,
        pattern=r"^omi-enrichment:[0-9a-f]{64}$",
    )
    transcript_sha256: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


def _require_service_token(presented: Optional[str], subject_uid: Optional[str]) -> EllaRequestAuthority:
    expected = os.getenv("ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(
            status_code=503,
            detail={"code": "hermes_cloud_enrichment_auth_not_configured"},
        )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_hermes_cloud_enrichment_service_credential"},
        )
    return get_exact_service_authority(
        provided_service_key=presented,
        configured_service_key=expected,
        service_subject_uid=subject_uid,
        service="hermes_cloud_enrichment",
    )


def _http_error(exc: ProvisioningError) -> HTTPException:
    return HTTPException(
        status_code=503 if exc.retryable else 409,
        detail={"code": exc.code},
    )


def _public_result(
    result: Any,
    *,
    outbox_job_id: Optional[str],
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "applied",
        "conversation_id": result.conversation_id,
        "runtime_binding_id": result.runtime_binding_id,
        "runtime_interaction_id": result.runtime_interaction_id,
        "active_summary_version_id": result.active_summary_version_id,
        "canonical_user_event_id": result.canonical_user_event_id,
        "canonical_assistant_event_id": result.canonical_assistant_event_id,
        "transcript_sha256": result.transcript_sha256,
        "summary_sha256": result.summary_sha256,
        "provider_response_present": result.provider_response_present,
        "duplicate": result.duplicate,
        "outbox_job_id": outbox_job_id,
        "client_interaction_id": result.client_interaction_id,
        "content_free": True,
    }


def create_hermes_cloud_enrichment_router(
    service_factory: Callable[[], Awaitable[HermesCloudEnrichmentService]],
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/ella/internal/hermes-cloud/enrichment",
        tags=["Hermes Cloud Enrichment"],
    )

    async def _service() -> HermesCloudEnrichmentService:
        return await service_factory()

    @router.post("/run")
    async def run(
        payload: HermesCloudEnrichmentIn,
        x_service_token: Optional[str] = Header(
            default=None,
            alias="X-Ella-Hermes-Cloud-Enrichment-Token",
        ),
        subject_uid: Optional[str] = Header(default=None, alias=ELLA_SUBJECT_UID_HEADER),
    ) -> dict[str, Any]:
        authority = _require_service_token(x_service_token, subject_uid)
        payload.uid = authority.require_uid(payload.uid, feature="Hermes Cloud enrichment")
        try:
            result = await (await _service()).enrich(
                uid=payload.uid,
                conversation_id=payload.conversation_id,
                allow_shadow=False,
                expected_client_interaction_id=payload.client_interaction_id,
                expected_transcript_sha256=payload.transcript_sha256,
            )
        except ProvisioningError as exc:
            raise _http_error(exc) from exc
        return _public_result(
            result,
            outbox_job_id=payload.outbox_job_id,
        )

    return router
