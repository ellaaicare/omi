"""Strict Firebase and narrowly scoped service authentication for Ella routes."""

import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

ELLA_SUBJECT_UID_HEADER = "X-Ella-Subject-Uid"
MAX_FIREBASE_UID_LENGTH = 128
CLIENT_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
APP_VERSION_WITH_BUILD_RE = re.compile(r"^(?P<version>\d+(?:\.\d+){0,3})(?:\+(?P<build>\d+))?$")


def _normalize_subject_uid(subject_uid: Optional[str], *, feature: str) -> str:
    normalized = subject_uid.strip() if isinstance(subject_uid, str) else ""
    if not normalized:
        raise HTTPException(status_code=403, detail={"code": "service_subject_required", "feature": feature})
    if len(normalized) > MAX_FIREBASE_UID_LENGTH or any(ord(character) < 32 for character in normalized):
        raise HTTPException(status_code=403, detail={"code": "invalid_service_subject", "feature": feature})
    return normalized


@dataclass(frozen=True)
class EllaRequestAuthority:
    """Authority established before any user-scoped downstream work begins."""

    firebase_uid: Optional[str] = None
    service: Optional[str] = None
    service_subject_uid: Optional[str] = None

    @property
    def is_service(self) -> bool:
        return self.service is not None

    def require_uid(self, claimed_uid: Optional[str], *, feature: str) -> str:
        normalized_claim = (claimed_uid or "").strip()
        if self.firebase_uid is not None:
            return require_matching_firebase_uid(self.firebase_uid, normalized_claim, feature=feature)
        bound_subject = _normalize_subject_uid(self.service_subject_uid, feature=feature)
        if normalized_claim and not secrets.compare_digest(normalized_claim, bound_subject):
            raise HTTPException(status_code=403, detail=f"{feature} UID does not match bound service subject")
        return bound_subject


@dataclass(frozen=True)
class FirebaseTokenIdentity:
    uid: str
    verified_email: str = ""


def _version_tuple(value: str) -> tuple[int, ...]:
    if not CLIENT_VERSION_RE.fullmatch(value):
        raise ValueError
    return tuple(int(part) for part in value.split("."))


def _client_gate_value(
    minimum: str,
    x_app_version: Optional[str],
    x_ella_app_build: Optional[str],
    x_ella_client_version: Optional[str],
) -> str:
    minimum_is_build = "." not in minimum
    if minimum_is_build:
        explicit_build = x_ella_app_build.strip() if isinstance(x_ella_app_build, str) else ""
        if explicit_build:
            return explicit_build
    supplied_version = next(
        (value.strip() for value in (x_app_version, x_ella_client_version) if isinstance(value, str) and value.strip()),
        "",
    )
    match = APP_VERSION_WITH_BUILD_RE.fullmatch(supplied_version)
    if match is None:
        return supplied_version
    if minimum_is_build:
        if match.group("build"):
            return match.group("build")
        return match.group("version") if "." not in match.group("version") else ""
    return match.group("version")


def require_supported_ella_client(
    x_app_version: Optional[str] = Header(default=None, alias="X-App-Version"),
    x_ella_app_build: Optional[str] = Header(default=None, alias="X-Ella-App-Build"),
    x_ella_client_version: Optional[str] = Header(default=None, alias="X-Ella-Client-Version"),
) -> None:
    minimum = os.getenv("ELLA_MIN_SUPPORTED_CLIENT_BUILD", "").strip()
    if not minimum:
        return
    supplied = _client_gate_value(minimum, x_app_version, x_ella_app_build, x_ella_client_version)
    try:
        minimum_parts = _version_tuple(minimum)
        supplied_parts = _version_tuple(supplied)
    except ValueError:
        if not CLIENT_VERSION_RE.fullmatch(minimum):
            raise HTTPException(status_code=503, detail={"code": "client_version_gate_not_configured"})
        raise HTTPException(status_code=426, detail={"code": "update_required", "minimum_build": minimum})
    width = max(len(minimum_parts), len(supplied_parts))
    if supplied_parts + (0,) * (width - len(supplied_parts)) < minimum_parts + (0,) * (width - len(minimum_parts)):
        raise HTTPException(status_code=426, detail={"code": "update_required", "minimum_build": minimum})


def get_firebase_token_identity(
    authorization: Optional[str] = Header(None),
    x_app_version: Optional[str] = Header(default=None, alias="X-App-Version"),
    x_ella_app_build: Optional[str] = Header(default=None, alias="X-Ella-App-Build"),
    x_ella_client_version: Optional[str] = Header(default=None, alias="X-Ella-Client-Version"),
) -> FirebaseTokenIdentity:
    """Return the exact Firebase subject and optional verified email."""
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
    email = str(decoded.get("email") or "").strip().lower() if isinstance(decoded, dict) else ""
    verified_email = email if decoded.get("email_verified") is True else ""
    require_supported_ella_client(x_app_version, x_ella_app_build, x_ella_client_version)
    return FirebaseTokenIdentity(uid=uid, verified_email=verified_email)


def get_exact_firebase_uid(
    authorization: Optional[str] = Header(None),
    x_app_version: Optional[str] = Header(default=None, alias="X-App-Version"),
    x_ella_app_build: Optional[str] = Header(default=None, alias="X-Ella-App-Build"),
    x_ella_client_version: Optional[str] = Header(default=None, alias="X-Ella-Client-Version"),
) -> str:
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
    require_supported_ella_client(x_app_version, x_ella_app_build, x_ella_client_version)
    return uid


def require_matching_firebase_uid(authenticated_uid: str, claimed_uid: Optional[str], *, feature: str) -> str:
    """Reject a caller-supplied subject that differs from the verified token."""
    normalized_claim = (claimed_uid or "").strip()
    if normalized_claim and not secrets.compare_digest(normalized_claim, authenticated_uid):
        raise HTTPException(status_code=403, detail=f"{feature} UID does not match authenticated user")
    return authenticated_uid


def get_firebase_or_service_authority(
    *,
    authorization: Optional[str],
    provided_service_key: Optional[str],
    configured_service_key: Optional[str],
    service: str,
    service_subject_uid: Optional[str] = None,
    x_app_version: Optional[str] = None,
    x_ella_app_build: Optional[str] = None,
    x_ella_client_version: Optional[str] = None,
) -> EllaRequestAuthority:
    """Authenticate either an exact Firebase bearer or one named service."""
    if provided_service_key is not None:
        configured = configured_service_key or ""
        if not configured or not provided_service_key or not secrets.compare_digest(provided_service_key, configured):
            raise HTTPException(status_code=403, detail=f"Invalid {service} service credential")
        return EllaRequestAuthority(
            service=service,
            service_subject_uid=_normalize_subject_uid(service_subject_uid, feature=service),
        )
    return EllaRequestAuthority(
        firebase_uid=get_exact_firebase_uid(
            authorization,
            x_app_version,
            x_ella_app_build,
            x_ella_client_version,
        )
    )


def get_exact_service_authority(
    *,
    provided_service_key: Optional[str],
    configured_service_key: Optional[str],
    service_subject_uid: Optional[str],
    service: str,
) -> EllaRequestAuthority:
    """Authenticate one service credential and bind it to exactly one subject."""
    configured = configured_service_key or ""
    if not configured:
        raise HTTPException(status_code=503, detail={"code": f"{service}_service_auth_not_configured"})
    if not provided_service_key or not secrets.compare_digest(provided_service_key, configured):
        raise HTTPException(status_code=403, detail={"code": f"invalid_{service}_service_credential"})
    return EllaRequestAuthority(
        service=service,
        service_subject_uid=_normalize_subject_uid(service_subject_uid, feature=service),
    )
