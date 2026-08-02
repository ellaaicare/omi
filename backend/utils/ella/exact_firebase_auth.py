"""Strict Firebase and narrowly scoped service authentication for Ella routes."""

import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth


@dataclass(frozen=True)
class EllaRequestAuthority:
    """Authority established before any user-scoped downstream work begins."""

    firebase_uid: Optional[str] = None
    service: Optional[str] = None

    @property
    def is_service(self) -> bool:
        return self.service is not None

    def require_uid(self, claimed_uid: Optional[str], *, feature: str) -> str:
        normalized_claim = (claimed_uid or "").strip()
        if self.firebase_uid is not None:
            return require_matching_firebase_uid(self.firebase_uid, normalized_claim, feature=feature)
        if not normalized_claim:
            raise HTTPException(status_code=400, detail=f"{feature} UID is required")
        return normalized_claim


def get_exact_firebase_uid(authorization: Optional[str] = Header(None)) -> str:
    """Return the Firebase subject without admin-key or local-dev fallbacks."""
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail="Missing or invalid Firebase bearer")
    try:
        decoded = firebase_auth.verify_id_token(parts[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase bearer")
    uid = str(decoded.get("uid") or "").strip() if isinstance(decoded, dict) else ""
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid Firebase bearer subject")
    return uid


def require_matching_firebase_uid(authenticated_uid: str, claimed_uid: Optional[str], *, feature: str) -> str:
    """Reject a caller-supplied subject that differs from the verified token."""
    normalized_claim = (claimed_uid or "").strip()
    if normalized_claim and normalized_claim != authenticated_uid:
        raise HTTPException(status_code=403, detail=f"{feature} UID does not match authenticated user")
    return authenticated_uid


def get_firebase_or_service_authority(
    *,
    authorization: Optional[str],
    provided_service_key: Optional[str],
    configured_service_key: Optional[str],
    service: str,
) -> EllaRequestAuthority:
    """Authenticate either an exact Firebase bearer or one named service.

    Supplying a service header always selects the service-auth path. A missing
    configured secret or mismatched key is rejected instead of falling back to
    a bearer identity.
    """
    if provided_service_key is not None:
        configured = configured_service_key or ""
        if not configured or not provided_service_key or not secrets.compare_digest(provided_service_key, configured):
            raise HTTPException(status_code=403, detail=f"Invalid {service} service credential")
        return EllaRequestAuthority(service=service)
    return EllaRequestAuthority(firebase_uid=get_exact_firebase_uid(authorization))
