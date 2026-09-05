"""Account-bound diagnostic ingest, projection, and audited support reads."""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from database.account_diagnostics import (
    DiagnosticAccountAuthority,
    DiagnosticAccountAuthorityChanged,
    DiagnosticAccountNotFound,
    DiagnosticRateLimitExceeded,
    DiagnosticSupportGrantInvalid,
    DiagnosticSupportGrantLimitExceeded,
    PostgresAccountDiagnosticsRepository,
)
from database.honcho_attestation import authority_credential
from utils.ella.account_diagnostics import (
    AccountStateProjectionV1,
    DiagnosticEventBatchV1,
    account_binding_fingerprint,
    event_from_record,
    generate_support_code,
    project_account_state,
    support_code_hash,
)
from ella.services.ai_consent import get_ai_consent_service
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

_OPERATOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


class DiagnosticIngestReceiptV1(BaseModel):
    schema_version: str = "ella.diagnostic_ingest_receipt.v1"
    accepted: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    evidence_only: bool = True


class DiagnosticSupportGrantRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnostic_session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    evidence_window_hours: int = Field(default=1, ge=1, le=24)
    expires_in_seconds: int = Field(default=600, ge=60, le=900)


class DiagnosticSupportGrantReceiptV1(BaseModel):
    schema_version: str = "ella.diagnostic_support_grant.v1"
    grant_id: str
    support_code: str
    diagnostic_session_id: str
    expires_at: datetime
    single_use: bool = True
    evidence_only: bool = True


class DiagnosticSupportExchangeRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    support_code: str = Field(min_length=19, max_length=19)
    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")


class DiagnosticSupportProjectionV1(BaseModel):
    schema_version: str = "ella.diagnostic_support_projection.v1"
    case_id: str
    operator_id: str
    projection: AccountStateProjectionV1
    evidence_only: bool = True


def require_diagnostic_operator(
    authorization: str | None = Header(default=None),
    x_ella_operator_id: str | None = Header(default=None, alias="X-Ella-Operator-Id"),
) -> str:
    """Authenticate the dedicated read-only support role, never legacy ADMIN_KEY."""
    expected = authority_credential("ELLA_DIAGNOSTICS_OPERATOR_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail={"code": "diagnostic_operator_auth_not_configured"})
    scheme, _, presented = str(authorization or "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not presented
        or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail={"code": "diagnostic_operator_auth_invalid"})
    operator_id = str(x_ella_operator_id or "").strip()
    if _OPERATOR_ID_RE.fullmatch(operator_id) is None:
        raise HTTPException(status_code=403, detail={"code": "diagnostic_operator_id_required"})
    return operator_id


def _evidence_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Ella-Diagnostic-Authority"] = "evidence-only"


def _current_consent_material(uid: str, consent_status: Callable[[str], dict[str, Any]]) -> tuple[str, str]:
    result = consent_status(uid)
    consent = result.get("consent") if isinstance(result, dict) else None
    if result.get("authorized") is not True or not isinstance(consent, dict):
        raise HTTPException(status_code=409, detail={"code": "diagnostic_account_authority_unavailable"})
    profile_binding_id = str(consent.get("profile_binding_id") or "")
    receipt_id = str(consent.get("receipt_id") or "")
    if not profile_binding_id or not receipt_id:
        raise HTTPException(status_code=409, detail={"code": "diagnostic_account_authority_stale"})
    return profile_binding_id, receipt_id


def create_account_diagnostics_router(
    repository: PostgresAccountDiagnosticsRepository,
    *,
    consent_status: Callable[[str], dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["Ella Diagnostics"])
    read_consent_status = consent_status or get_ai_consent_service().status
    now = clock or (lambda: datetime.now(timezone.utc))

    async def current_authority(uid: str) -> tuple[DiagnosticAccountAuthority, str, str, str]:
        try:
            authority = await repository.resolve_account_authority(uid)
        except DiagnosticAccountNotFound as exc:
            raise HTTPException(status_code=404, detail={"code": "diagnostic_account_not_found"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        try:
            profile_binding_id, receipt_id = _current_consent_material(uid, read_consent_status)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_account_authority_unavailable"}) from exc
        fingerprint = account_binding_fingerprint(
            uid=uid,
            profile_binding_id=profile_binding_id,
            binding_revision=authority.binding_revision,
            consent_receipt_id=receipt_id,
        )
        return authority, fingerprint, profile_binding_id, receipt_id

    @router.post(
        "/v1/ella/diagnostics/events",
        response_model=DiagnosticIngestReceiptV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ingest_diagnostic_events(
        payload: DiagnosticEventBatchV1,
        response: Response,
        uid: str = Depends(get_exact_firebase_uid),
    ) -> DiagnosticIngestReceiptV1:
        _evidence_headers(response)
        authority, expected_fingerprint, profile_binding_id, receipt_id = await current_authority(uid)
        if any(
            not hmac.compare_digest(event.account_binding_fingerprint, expected_fingerprint) for event in payload.events
        ):
            raise HTTPException(status_code=409, detail={"code": "diagnostic_account_binding_stale"})
        try:
            accepted, duplicates = await repository.append_events(
                authority,
                payload.events,
                uid=uid,
                profile_binding_id=profile_binding_id,
                consent_receipt_id=receipt_id,
                expected_fingerprint=expected_fingerprint,
            )
        except DiagnosticAccountAuthorityChanged as exc:
            raise HTTPException(status_code=409, detail={"code": "diagnostic_account_binding_stale"}) from exc
        except DiagnosticRateLimitExceeded as exc:
            raise HTTPException(status_code=429, detail={"code": "diagnostic_rate_limit_exceeded"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        return DiagnosticIngestReceiptV1(accepted=accepted, duplicates=duplicates)

    @router.get(
        "/v1/ella/diagnostics/projection/{diagnostic_session_id}",
        response_model=AccountStateProjectionV1,
    )
    async def get_diagnostic_projection(
        diagnostic_session_id: str,
        response: Response,
        uid: str = Depends(get_exact_firebase_uid),
    ) -> AccountStateProjectionV1:
        _evidence_headers(response)
        if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", diagnostic_session_id) is None:
            raise HTTPException(status_code=422, detail={"code": "diagnostic_session_id_invalid"})
        authority, _, _, _ = await current_authority(uid)
        try:
            records = await repository.list_session_events(authority, diagnostic_session_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        return project_account_state(
            diagnostic_session_id,
            [event_from_record(record) for record in records],
            now=now(),
        )

    @router.post(
        "/v1/ella/diagnostics/support-grants",
        response_model=DiagnosticSupportGrantReceiptV1,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_diagnostic_support_grant(
        payload: DiagnosticSupportGrantRequestV1,
        response: Response,
        uid: str = Depends(get_exact_firebase_uid),
    ) -> DiagnosticSupportGrantReceiptV1:
        _evidence_headers(response)
        authority, expected_fingerprint, profile_binding_id, receipt_id = await current_authority(uid)
        try:
            records = await repository.list_session_events(authority, payload.diagnostic_session_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        if not records:
            raise HTTPException(status_code=404, detail={"code": "diagnostic_session_not_found"})
        hmac_key = authority_credential("ELLA_DIAGNOSTICS_SUPPORT_HMAC_KEY")
        if not hmac_key:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_support_grants_not_configured"})
        issued_at = now().astimezone(timezone.utc)
        expires_at = issued_at + timedelta(seconds=payload.expires_in_seconds)
        code = generate_support_code()
        try:
            grant_id = await repository.create_support_grant(
                authority,
                diagnostic_session_id=payload.diagnostic_session_id,
                code_hash=support_code_hash(code, hmac_key=hmac_key),
                evidence_not_before=issued_at - timedelta(hours=payload.evidence_window_hours),
                evidence_not_after=issued_at,
                expires_at=expires_at,
                uid=uid,
                profile_binding_id=profile_binding_id,
                consent_receipt_id=receipt_id,
                expected_fingerprint=expected_fingerprint,
            )
        except DiagnosticAccountAuthorityChanged as exc:
            raise HTTPException(status_code=409, detail={"code": "diagnostic_account_binding_stale"}) from exc
        except DiagnosticSupportGrantLimitExceeded as exc:
            raise HTTPException(status_code=429, detail={"code": "diagnostic_support_grant_limit"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        return DiagnosticSupportGrantReceiptV1(
            grant_id=grant_id,
            support_code=code,
            diagnostic_session_id=payload.diagnostic_session_id,
            expires_at=expires_at,
        )

    @router.delete(
        "/v1/ella/diagnostics/support-grants/{grant_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke_diagnostic_support_grant(
        grant_id: uuid.UUID,
        response: Response,
        uid: str = Depends(get_exact_firebase_uid),
    ) -> Response:
        _evidence_headers(response)
        authority, _, _, _ = await current_authority(uid)
        try:
            revoked = await repository.revoke_support_grant(authority, str(grant_id))
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        if not revoked:
            raise HTTPException(status_code=404, detail={"code": "diagnostic_support_grant_not_found"})
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.post(
        "/v1/ella/operator/diagnostics/support-code/exchange",
        response_model=DiagnosticSupportProjectionV1,
    )
    async def exchange_diagnostic_support_code(
        payload: DiagnosticSupportExchangeRequestV1,
        response: Response,
        operator_id: str = Depends(require_diagnostic_operator),
    ) -> DiagnosticSupportProjectionV1:
        _evidence_headers(response)
        hmac_key = authority_credential("ELLA_DIAGNOSTICS_SUPPORT_HMAC_KEY")
        if not hmac_key:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_support_grants_not_configured"})
        try:
            code_hash = support_code_hash(payload.support_code, hmac_key=hmac_key)
            diagnostic_session_id, records = await repository.consume_support_grant(
                code_hash=code_hash,
                operator_id=operator_id,
                case_id=payload.case_id,
                reason=payload.reason.strip(),
            )
        except (ValueError, DiagnosticSupportGrantInvalid) as exc:
            raise HTTPException(status_code=404, detail={"code": "diagnostic_support_code_invalid"}) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "diagnostic_store_unavailable"}) from exc
        return DiagnosticSupportProjectionV1(
            case_id=payload.case_id,
            operator_id=operator_id,
            projection=project_account_state(
                diagnostic_session_id,
                [event_from_record(record) for record in records],
                now=now(),
            ),
        )

    return router
