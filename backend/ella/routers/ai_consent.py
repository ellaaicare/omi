"""Authenticated AI/data-sharing consent API."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ella.services.ai_consent import (
    AiConsentService,
    ConsentIdempotencyConflict,
    ConsentPolicyMismatch,
    ConsentSubmission,
    get_ai_consent_service,
)
from ella.services.consent_authority import submit_with_managed_cloud_authority
from database.managed_cloud_consent import ManagedCloudAuthorityUnavailable
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

router = APIRouter(tags=["AI Consent"])


class AiConsentSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["granted", "declined", "revoked"]
    policy_version: str = Field(min_length=1, max_length=80)
    processor_set_hash: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=8, max_length=128)
    app_version: str = Field(min_length=1, max_length=80)
    build_number: str = Field(min_length=1, max_length=40)
    locale: str = Field(min_length=2, max_length=40)
    scope_version: str = Field(default="", max_length=100)
    scope_hash: str = Field(default="", max_length=100)


@router.get("/v1/users/ai-consent/policy")
def get_ai_consent_policy():
    """Public metadata so disclosure can occur before Firebase sign-in."""
    return AiConsentService.policy()


@router.get("/v1/users/ai-consent")
def get_ai_consent_status(uid: str = Depends(get_exact_firebase_uid)):
    return get_ai_consent_service().status(uid)


@router.get("/v1/users/ai-consent/receipts/{receipt_id}")
def get_ai_consent_receipt(receipt_id: str, uid: str = Depends(get_exact_firebase_uid)):
    receipt = get_ai_consent_service().receipt(uid, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail={"code": "ai_consent_receipt_not_found"})
    return {"receipt": receipt}


@router.post("/v1/users/ai-consent")
async def submit_ai_consent(
    request: AiConsentSubmissionRequest,
    uid: str = Depends(get_exact_firebase_uid),
):
    try:
        return await submit_with_managed_cloud_authority(
            uid=uid,
            service=get_ai_consent_service(),
            submission=ConsentSubmission(
                decision=request.decision,
                policy_version=request.policy_version,
                processor_set_hash=request.processor_set_hash,
                request_id=request.request_id,
                app_version=request.app_version,
                build_number=request.build_number,
                locale=request.locale,
                scope_version=request.scope_version,
                scope_hash=request.scope_hash,
            ),
        )
    except ConsentPolicyMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ai_consent_policy_mismatch",
                "required_policy": AiConsentService.policy(),
            },
        ) from exc
    except ConsentIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ai_consent_idempotency_conflict"}) from exc
    except ManagedCloudAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "managed_cloud_consent_authority_unavailable"},
        ) from exc
