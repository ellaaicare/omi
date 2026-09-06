"""Authority validation for evidence-only diagnostic correlation."""

from __future__ import annotations

import hmac
from typing import Any, Callable, Mapping

from database.account_diagnostics import (
    DiagnosticAccountAuthorityChanged,
    DiagnosticAccountNotFound,
    PostgresAccountDiagnosticsRepository,
    account_binding_fingerprint,
)
from ella.services import ai_consent
from utils.ella.account_diagnostics import CaptureDiagnosticCorrelation, DiagnosticCorrelationError


class DiagnosticCorrelationAuthorityError(RuntimeError):
    """A stable, content-free rejection raised after Firebase authentication."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def current_consent_material(
    uid: str,
    consent_status: Callable[[str], dict[str, Any]],
) -> tuple[str, str]:
    try:
        result = consent_status(uid)
    except Exception as exc:
        raise DiagnosticCorrelationAuthorityError(
            "diagnostic_account_authority_unavailable",
            retryable=True,
        ) from exc
    consent = result.get("consent") if isinstance(result, dict) else None
    if not isinstance(result, dict) or result.get("authorized") is not True or not isinstance(consent, dict):
        raise DiagnosticCorrelationAuthorityError("diagnostic_account_authority_unavailable")
    profile_binding_id = str(consent.get("profile_binding_id") or "")
    receipt_id = str(consent.get("receipt_id") or "")
    if not profile_binding_id or not receipt_id:
        raise DiagnosticCorrelationAuthorityError("diagnostic_account_binding_stale")
    return profile_binding_id, receipt_id


async def validate_capture_diagnostic_correlation(
    uid: str,
    headers: Mapping[str, str],
    *,
    repository: PostgresAccountDiagnosticsRepository | None = None,
    consent_status: Callable[[str], dict[str, Any]] | None = None,
) -> CaptureDiagnosticCorrelation | None:
    """Validate optional headers against the authenticated account's live binding."""
    try:
        correlation = CaptureDiagnosticCorrelation.from_headers(headers)
    except DiagnosticCorrelationError as exc:
        raise DiagnosticCorrelationAuthorityError(exc.code) from exc
    if correlation is None:
        return None

    diagnostics_repository = repository or PostgresAccountDiagnosticsRepository()
    try:
        authority = await diagnostics_repository.resolve_account_authority(uid)
    except DiagnosticAccountNotFound as exc:
        raise DiagnosticCorrelationAuthorityError("diagnostic_account_not_found") from exc
    except Exception as exc:
        raise DiagnosticCorrelationAuthorityError("diagnostic_store_unavailable", retryable=True) from exc

    if consent_status is None:
        consent_status = ai_consent.get_ai_consent_service().status
    profile_binding_id, receipt_id = current_consent_material(uid, consent_status)
    expected_fingerprint = account_binding_fingerprint(
        uid=uid,
        profile_binding_id=profile_binding_id,
        binding_revision=authority.binding_revision,
        consent_receipt_id=receipt_id,
    )
    if not hmac.compare_digest(correlation.account_binding_fingerprint, expected_fingerprint):
        raise DiagnosticCorrelationAuthorityError("diagnostic_account_binding_stale")
    try:
        await diagnostics_repository.validate_current_authority(
            authority,
            uid=uid,
            profile_binding_id=profile_binding_id,
            consent_receipt_id=receipt_id,
            expected_fingerprint=expected_fingerprint,
        )
    except DiagnosticAccountAuthorityChanged as exc:
        raise DiagnosticCorrelationAuthorityError("diagnostic_account_binding_stale") from exc
    except Exception as exc:
        raise DiagnosticCorrelationAuthorityError("diagnostic_store_unavailable", retryable=True) from exc
    return correlation.validated_for_binding(authority.binding_revision)
