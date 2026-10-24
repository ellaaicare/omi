"""Authenticated AI/data-sharing consent API."""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ella.services.ai_consent import (
    AiConsentService,
    ConsentAccountDeleted,
    ConsentAuthorityUnavailable,
    ConsentIdempotencyConflict,
    ConsentPolicyMismatch,
    ConsentSubmission,
    CURRENT_POLICY_VERSION,
    LEGACY_POLICY_VERSION_V10,
    SUPPORTED_CONSENT_POLICY_CONTRACTS,
    get_ai_consent_service,
)
from ella.services.consent_authority import ArtworkConsentErasureUnavailable, submit_with_managed_cloud_authority
from database.managed_cloud_consent import ManagedCloudAuthorityUnavailable
from utils.ella.exact_firebase_auth import (
    FirebaseTokenIdentity,
    get_exact_firebase_uid,
    get_firebase_token_identity,
)

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
    account_epoch_token: str = Field(default="", max_length=128)


@router.get("/v1/users/ai-consent/policy")
def get_ai_consent_policy(policy_version: str = LEGACY_POLICY_VERSION_V10):
    """Return legacy v10 by default; newer clients must negotiate v11."""
    if policy_version not in SUPPORTED_CONSENT_POLICY_CONTRACTS:
        raise HTTPException(status_code=404, detail={"code": "ai_consent_policy_version_not_supported"})
    return AiConsentService.policy(policy_version)


def _observe_authenticated_main_status(identity, status):
    """Closed correlation only; no request/response bodies, tokens or authority edits."""
    try:
        consent = status.get("consent") if type(status.get("consent")) is dict else {}
        policy = status.get("policy") if type(status.get("policy")) is dict else {}
        def bounded_hash(value):
            return (hashlib.sha256(value.encode("utf-8")).hexdigest()
                    if type(value) is str and 0 < len(value) <= 512 else None)
        authority = status.get("authority_state")
        decision = consent.get("decision")
        version = policy.get("version")
        valid_authority = {"authorized", "declined", "revoked", "deleted",
                           "not_accepted", "reconsent_required", "unavailable"}
        row = {
            "contract": "ella-main-consent-status-observation-v1",
            "correlation_id": uuid.uuid4().hex,
            "observed_utc": datetime.now(timezone.utc).isoformat(),
            "route": "/v1/users/ai-consent",
            "http_status": 503 if authority == "unavailable" else 200,
            "owner_ref_sha256": bounded_hash(identity.uid),
            "subject_owner_matches": type(status.get("subject_uid")) is str
                                     and status.get("subject_uid") == identity.uid,
            "authorized": status.get("authorized") if type(status.get("authorized")) is bool else None,
            "authority_state": authority if type(authority) is str and authority in valid_authority else "unknown",
            "decision": decision if type(decision) is str and decision in
                        {"granted", "declined", "revoked", "deleted", "not_recorded"} else "unknown",
            "policy_version": version if type(version) is str and version in
                              {"ai-data-processors-v10", "ai-data-processors-v11"} else "unknown",
            "profile_binding_sha256": bounded_hash(consent.get("profile_binding_id")),
            "receipt_id_sha256": bounded_hash(consent.get("receipt_id")),
            "policy_binding_matches": all(type(consent.get(k)) is str
                and bool(consent[k]) and consent[k] == policy.get(k)
                for k in ("processor_set_hash", "scope_version", "scope_hash")),
        }
        print("[MAIN-CONSENT-STATUS] " + json.dumps(row, sort_keys=True, separators=(",", ":")), flush=True)
    except BaseException:
        # Optional observation cannot change response, errors or cancellation.
        pass


@router.get("/v1/users/ai-consent")
def get_ai_consent_status(
    policy_version: str = LEGACY_POLICY_VERSION_V10,
    identity: FirebaseTokenIdentity = Depends(get_firebase_token_identity),
):
    if policy_version not in SUPPORTED_CONSENT_POLICY_CONTRACTS:
        raise HTTPException(status_code=404, detail={"code": "ai_consent_policy_version_not_supported"})
    status = get_ai_consent_service().status(
        identity.uid,
        account_epoch_auth_time=identity.auth_time,
        requested_policy_version=policy_version,
    )
    _observe_authenticated_main_status(identity, status)
    if status.get("authority_state") == "unavailable":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ai_consent_authority_unavailable",
                "authority_state": "unavailable",
                "retryable": True,
            },
        )
    return status


@router.get("/v1/users/ai-consent/receipts/{receipt_id}")
def get_ai_consent_receipt(receipt_id: str, uid: str = Depends(get_exact_firebase_uid)):
    try:
        receipt = get_ai_consent_service().receipt(uid, receipt_id)
    except ConsentAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ai_consent_authority_unavailable",
                "authority_state": "unavailable",
                "retryable": True,
            },
        ) from exc
    if receipt is None:
        raise HTTPException(status_code=404, detail={"code": "ai_consent_receipt_not_found"})
    return {"receipt": receipt}


@router.post("/v1/users/ai-consent")
async def submit_ai_consent(
    request: AiConsentSubmissionRequest,
    identity: FirebaseTokenIdentity = Depends(get_firebase_token_identity),
):
    try:
        return await submit_with_managed_cloud_authority(
            uid=identity.uid,
            verified_email=identity.verified_email,
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
                account_epoch_token=request.account_epoch_token,
                account_epoch_auth_time=identity.auth_time,
            ),
        )
    except ConsentPolicyMismatch as exc:
        required_policy_version = (
            request.policy_version
            if request.policy_version in SUPPORTED_CONSENT_POLICY_CONTRACTS
            else CURRENT_POLICY_VERSION
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ai_consent_policy_mismatch",
                "required_policy": AiConsentService.policy(required_policy_version),
            },
        ) from exc
    except ConsentIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ai_consent_idempotency_conflict"}) from exc
    except ConsentAccountDeleted as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ai_consent_account_deleted",
                "authority_state": "deleted",
                "retryable": False,
            },
        ) from exc
    except ConsentAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ai_consent_authority_unavailable",
                "authority_state": "unavailable",
                "retryable": True,
            },
        ) from exc
    except ManagedCloudAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "managed_cloud_consent_authority_unavailable"},
        ) from exc
    except ArtworkConsentErasureUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "artwork_consent_erasure_unavailable"},
        ) from exc
