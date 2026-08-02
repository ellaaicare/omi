"""Strict Firebase bearer authentication for Ella user-facing routes."""

from typing import Optional

from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth


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
