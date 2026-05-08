"""Generic MCP onboarding and account-role resolution endpoints."""

from __future__ import annotations

import hashlib
import os
import time
import urllib.parse
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPIdentityResolution,
    MCPProfileGrant,
    issue_mcp_session_token,
    session_claims_to_public_resolution,
    validate_mcp_session_token,
    resolve_mcp_identity,
)
from ella.services.mcp_startup import build_startup_context
from ella.services.mcp_surface_prompt import build_public_surface_prompt, build_surface_prompt

router = APIRouter(prefix="/v1/ella/mcp", tags=["Ella MCP Onboarding"])


class MCPOAuthOnboardingRequest(BaseModel):
    firebase_id_token: str = Field(..., min_length=1)
    selected_profile_uid: str = ""
    ttl_seconds: int = Field(default=3600, ge=60, le=24 * 60 * 60)


_auth_codes: dict[str, dict[str, Any]] = {}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _token_from_authorization(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    authorization = authorization.strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _oauth_client_id() -> str:
    return _env("ELLA_MCP_OAUTH_CLIENT_ID", "ella-mcp")


def _oauth_code_ttl_seconds() -> int:
    try:
        return max(60, min(600, int(_env("ELLA_MCP_OAUTH_CODE_TTL_SECONDS", "300"))))
    except ValueError:
        return 300


def _mcp_session_ttl_seconds(value: Any = None) -> int:
    try:
        requested = int(value if value is not None else _env("ELLA_MCP_SESSION_TTL_SECONDS", "3600"))
    except (TypeError, ValueError):
        requested = 3600
    return max(60, min(24 * 60 * 60, requested))


def _allowed_static_tokens() -> set[str]:
    tokens = _csv_env("ELLA_MCP_TOKENS", _env("ELLA_MCP_TOKEN"))
    # Temporary compatibility while the existing test connector still uses the
    # older env names. Do not add profile/platform assumptions to new clients.
    tokens.extend(_csv_env("ELLA_PLATO_MCP_TOKENS", _env("ELLA_PLATO_MCP_TOKEN")))
    return set(tokens)


def _authenticate_static(authorization: Optional[str]) -> str:
    tokens = _allowed_static_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="MCP onboarding token is not configured")
    token = _token_from_authorization(authorization)
    if not token or token not in tokens:
        raise HTTPException(status_code=401, detail="Invalid or missing MCP bearer token")
    return _fingerprint(token)


def _authenticate_connector_session(
    authorization: Optional[str],
    *,
    selected_profile_uid: str = "",
) -> dict[str, Any]:
    token = _token_from_authorization(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid or missing MCP bearer token")

    try:
        claims = validate_mcp_session_token(token)
        return session_claims_to_public_resolution(claims)
    except ValueError as session_error:
        tokens = _allowed_static_tokens()
        if token in tokens:
            return resolve_static_connector_session(_fingerprint(token), selected_profile_uid)
        if not tokens and "not configured" in str(session_error):
            raise HTTPException(status_code=503, detail="MCP bearer authentication is not configured") from session_error
        raise HTTPException(status_code=401, detail="Invalid or expired MCP bearer token") from session_error


def _default_profile_uid() -> str:
    # Prefer generic configuration. Legacy fallback keeps the current connector
    # working until OAuth-backed grants are provisioned.
    return _env("ELLA_MCP_DEFAULT_PROFILE_UID", _env("ELLA_PLATO_MCP_UID", _env("ELLA_PLATO_UID")))


def _static_grants(token_fingerprint: str) -> list[MCPProfileGrant]:
    profile_uid = _default_profile_uid()
    if not profile_uid:
        return []
    role = _env("ELLA_MCP_DEFAULT_ROLE", "self")
    scopes = _csv_env("ELLA_MCP_DEFAULT_SCOPES")
    allowed_tools = _csv_env("ELLA_MCP_ALLOWED_TOOLS")
    return [
        MCPProfileGrant.from_mapping(
            {
                "grant_id": f"static:{token_fingerprint}",
                "profile_uid": profile_uid,
                "role": role,
                "profile_label": _env("ELLA_MCP_DEFAULT_PROFILE_LABEL", "Default profile"),
                "scopes": scopes,
                "allowed_tools": allowed_tools,
                "status": "active",
            }
        )
    ]


def _static_identity(token_fingerprint: str) -> ExternalConnectorIdentity:
    return ExternalConnectorIdentity(
        provider="static_bearer",
        subject=token_fingerprint,
        authenticated=True,
    )


def _identity_from_firebase_claims(decoded: dict[str, Any]) -> ExternalConnectorIdentity:
    firebase_info = decoded.get("firebase") if isinstance(decoded.get("firebase"), dict) else {}
    identities = firebase_info.get("identities") if isinstance(firebase_info.get("identities"), dict) else {}
    google_subjects = identities.get("google.com") if isinstance(identities.get("google.com"), list) else []
    google_subject = str(google_subjects[0]).strip() if google_subjects else ""
    firebase_uid = str(decoded.get("uid") or decoded.get("user_id") or decoded.get("sub") or "").strip()
    provider = "google" if google_subject or firebase_info.get("sign_in_provider") == "google.com" else "firebase"
    subject = google_subject or firebase_uid
    return ExternalConnectorIdentity(
        provider=provider,
        subject=subject,
        email=str(decoded.get("email") or "").strip().lower(),
        email_verified=bool(decoded.get("email_verified")),
        authenticated=bool(subject),
        display_name=str(decoded.get("name") or "").strip(),
    )


def _verify_firebase_identity(firebase_id_token: str) -> ExternalConnectorIdentity:
    try:
        from firebase_admin import auth as firebase_auth

        decoded = firebase_auth.verify_id_token(firebase_id_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase ID token") from exc
    identity = _identity_from_firebase_claims(decoded or {})
    if not identity.authenticated:
        raise HTTPException(status_code=401, detail="Firebase token did not contain a usable subject")
    return identity


def resolve_static_connector_session(token_fingerprint: str, selected_profile_uid: str = "") -> dict[str, Any]:
    identity = _static_identity(token_fingerprint)
    resolution = resolve_mcp_identity(
        identity,
        selected_profile_uid=selected_profile_uid,
        grants=_static_grants(token_fingerprint),
    )
    return resolution.to_public_dict(include_claims=True)


def resolve_oauth_connector_resolution(
    firebase_id_token: str,
    *,
    selected_profile_uid: str = "",
) -> MCPIdentityResolution:
    identity = _verify_firebase_identity(firebase_id_token)
    return resolve_mcp_identity(identity, selected_profile_uid=selected_profile_uid)


def resolve_oauth_connector_session(
    firebase_id_token: str,
    *,
    selected_profile_uid: str = "",
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    resolution = resolve_oauth_connector_resolution(firebase_id_token, selected_profile_uid=selected_profile_uid)
    return resolution.to_public_dict(include_claims=True, ttl_seconds=ttl_seconds)


def _issue_oauth_token_response(resolution: MCPIdentityResolution, *, ttl_seconds: int) -> dict[str, Any]:
    if not resolution.mapped:
        raise HTTPException(status_code=403, detail=resolution.to_public_dict(include_claims=False))
    token, claims = issue_mcp_session_token(resolution, ttl_seconds=ttl_seconds)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": max(0, int(claims["exp"]) - int(time.time())),
        "scope": " ".join(claims.get("scopes") or []),
        "trace_id": claims["trace_id"],
        "profile_uid": claims["profile_uid"],
        "role": claims["role"],
        "scopes": claims.get("scopes") or [],
        "allowed_tools": claims.get("allowed_tools") or [],
        "grant_id": claims["grant_id"],
        "session_claims": claims,
    }


def _store_authorization_code(resolution: MCPIdentityResolution) -> str:
    code = uuid.uuid4().hex
    _auth_codes[code] = {
        "resolution": resolution,
        "expires_at": time.time() + _oauth_code_ttl_seconds(),
    }
    return code


def _consume_authorization_code(code: str) -> MCPIdentityResolution:
    item = _auth_codes.pop(code, None)
    if not item:
        raise HTTPException(status_code=400, detail="Invalid authorization code")
    if time.time() > float(item.get("expires_at") or 0):
        raise HTTPException(status_code=400, detail="Expired authorization code")
    return item["resolution"]


async def _parse_token_request(request: Request) -> dict[str, Any]:
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        raw_body = (await request.body()).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid token request body") from exc


def _redirect_with_params(redirect_uri: str, params: dict[str, Any]) -> Response:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in {None, ""}})
    separator = "&" if "?" in redirect_uri else "?"
    return Response(status_code=302, headers={"Location": f"{redirect_uri}{separator}{query}"})


@router.get("/onboarding")
async def get_mcp_onboarding(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    selected_profile_uid: str = Query("", description="Optional profile UID selected after multi-profile mapping."),
):
    return _authenticate_connector_session(authorization, selected_profile_uid=selected_profile_uid)


@router.post("/onboarding/oauth")
async def post_mcp_oauth_onboarding(request: MCPOAuthOnboardingRequest):
    return resolve_oauth_connector_session(
        request.firebase_id_token,
        selected_profile_uid=request.selected_profile_uid,
        ttl_seconds=request.ttl_seconds,
    )


@router.get("/authorize")
async def get_mcp_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    firebase_id_token: str = Query("", description="Verified Firebase ID token supplied by the auth handoff page."),
    selected_profile_uid: str = Query("", description="Selected Ella profile UID for multi-profile identities."),
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")
    if client_id != _oauth_client_id():
        raise HTTPException(status_code=400, detail="Invalid client_id")
    if not firebase_id_token:
        return _redirect_with_params(
            redirect_uri,
            {
                "error": "login_required",
                "error_description": "A Firebase/Google auth handoff must authenticate the user before code issue.",
                "state": state,
            },
        )

    resolution = resolve_oauth_connector_resolution(
        firebase_id_token,
        selected_profile_uid=selected_profile_uid,
    )
    if not resolution.mapped:
        payload = resolution.to_public_dict(include_claims=False)
        return _redirect_with_params(
            redirect_uri,
            {
                "error": payload["state"],
                "error_description": payload["message"],
                "trace_id": payload["trace_id"],
                "state": state,
            },
        )
    code = _store_authorization_code(resolution)
    return _redirect_with_params(redirect_uri, {"code": code, "state": state})


@router.post("/token")
async def post_mcp_token(request: Request):
    data = await _parse_token_request(request)
    client_id = str(data.get("client_id") or "")
    if client_id and client_id != _oauth_client_id():
        raise HTTPException(status_code=400, detail="Invalid client_id")

    ttl_seconds = _mcp_session_ttl_seconds(data.get("ttl_seconds") or data.get("expires_in"))
    grant_type = str(data.get("grant_type") or "").strip()
    if grant_type in {"authorization_code", ""} and data.get("code"):
        resolution = _consume_authorization_code(str(data.get("code") or ""))
        return _issue_oauth_token_response(resolution, ttl_seconds=ttl_seconds)

    if grant_type in {
        "urn:ietf:params:oauth:grant-type:token-exchange",
        "firebase_id_token",
        "",
    }:
        firebase_id_token = str(data.get("firebase_id_token") or data.get("subject_token") or "").strip()
        if not firebase_id_token:
            raise HTTPException(status_code=400, detail="firebase_id_token or subject_token is required")
        resolution = resolve_oauth_connector_resolution(
            firebase_id_token,
            selected_profile_uid=str(data.get("selected_profile_uid") or ""),
        )
        return _issue_oauth_token_response(resolution, ttl_seconds=ttl_seconds)

    raise HTTPException(status_code=400, detail="Unsupported grant_type")


@router.get("/start_here")
async def get_mcp_start_here(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    selected_profile_uid: str = Query("", description="Optional profile UID selected after multi-profile mapping."),
    limit: int = Query(12, ge=1, le=50),
    channels: str = Query("", description="Optional comma-separated canonical channels."),
):
    onboarding = _authenticate_connector_session(authorization, selected_profile_uid=selected_profile_uid)
    channel_list = [item.strip() for item in channels.split(",") if item.strip()] if channels else None
    return await build_startup_context(onboarding=onboarding, limit=limit, channels=channel_list)


@router.get("/surface-prompt")
async def get_mcp_surface_prompt(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    selected_profile_uid: str = Query("", description="Optional profile UID selected after multi-profile mapping."),
    surface: str = Query("generic", description="External surface, e.g. grok, hosted-gpt, gemini."),
):
    onboarding = _authenticate_connector_session(authorization, selected_profile_uid=selected_profile_uid)
    selected = onboarding.get("selected_profile") or {}
    claims = onboarding.get("session_claims") or {}
    if onboarding.get("state") != "authenticated_mapped" or not selected:
        raise HTTPException(status_code=403, detail="No mapped Ella profile for this connector session")
    return build_surface_prompt(
        profile_uid=str(selected.get("profile_uid") or claims.get("profile_uid") or ""),
        profile_label=str(selected.get("profile_label") or "Ella profile"),
        surface=surface,
        scopes=[str(item) for item in (claims.get("scopes") or selected.get("scopes") or [])],
        allowed_tools=[str(item) for item in (claims.get("allowed_tools") or selected.get("allowed_tools") or [])],
        proposal_write_enabled="proposals:write" in set(claims.get("scopes") or selected.get("scopes") or []),
    )


@router.get("/surface-prompt/public")
async def get_public_mcp_surface_prompt(
    surface: str = Query("generic", description="External surface, e.g. grok, hosted-gpt, gemini."),
):
    return build_public_surface_prompt(surface=surface)


@router.get("/info")
async def get_mcp_onboarding_info():
    return {
        "onboarding_endpoint": "/v1/ella/mcp/onboarding",
        "oauth_onboarding_endpoint": "/v1/ella/mcp/onboarding/oauth",
        "authorization_endpoint": "/v1/ella/mcp/authorize",
        "token_endpoint": "/v1/ella/mcp/token",
        "start_here_endpoint": "/v1/ella/mcp/start_here",
        "surface_prompt_endpoint": "/v1/ella/mcp/surface-prompt",
        "public_surface_prompt_endpoint": "/v1/ella/mcp/surface-prompt/public",
        "auth": "Exchange a verified Firebase/Google identity for a short-lived MCP bearer token; static Bearer token remains dev/static fallback.",
        "token_lifetime": "Default 3600s, bounded 60s to 86400s; configure ELLA_MCP_SESSION_TTL_SECONDS.",
        "revocation": "Disable or mark mcp_identity_grants inactive to prevent future token issue; rotate ELLA_MCP_SESSION_SECRET to revoke issued sessions before expiry.",
        "states": [
            "authenticated_mapped",
            "authenticated_needs_profile_selection",
            "authenticated_needs_invite",
            "unauthenticated",
        ],
        "write_tools_enabled": False,
        "profile_neutral": True,
    }
