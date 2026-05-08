"""Generic MCP onboarding and account-role resolution endpoints."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query

from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPProfileGrant,
    resolve_mcp_identity,
)
from ella.services.mcp_startup import build_startup_context

router = APIRouter(prefix="/v1/ella/mcp", tags=["Ella MCP Onboarding"])


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


def resolve_static_connector_session(token_fingerprint: str, selected_profile_uid: str = "") -> dict[str, Any]:
    identity = _static_identity(token_fingerprint)
    resolution = resolve_mcp_identity(
        identity,
        selected_profile_uid=selected_profile_uid,
        grants=_static_grants(token_fingerprint),
    )
    return resolution.to_public_dict(include_claims=True)


@router.get("/onboarding")
async def get_mcp_onboarding(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    selected_profile_uid: str = Query("", description="Optional profile UID selected after multi-profile mapping."),
):
    token_fingerprint = _authenticate_static(authorization)
    return resolve_static_connector_session(token_fingerprint, selected_profile_uid)


@router.get("/start_here")
async def get_mcp_start_here(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    selected_profile_uid: str = Query("", description="Optional profile UID selected after multi-profile mapping."),
    limit: int = Query(12, ge=1, le=50),
    channels: str = Query("", description="Optional comma-separated canonical channels."),
):
    token_fingerprint = _authenticate_static(authorization)
    onboarding = resolve_static_connector_session(token_fingerprint, selected_profile_uid)
    channel_list = [item.strip() for item in channels.split(",") if item.strip()] if channels else None
    return await build_startup_context(onboarding=onboarding, limit=limit, channels=channel_list)


@router.get("/info")
async def get_mcp_onboarding_info():
    return {
        "onboarding_endpoint": "/v1/ella/mcp/onboarding",
        "start_here_endpoint": "/v1/ella/mcp/start_here",
        "auth": "Bearer token for dev/static fallback; OAuth-backed grants are resolved by the same identity service.",
        "states": [
            "authenticated_mapped",
            "authenticated_needs_profile_selection",
            "authenticated_needs_invite",
            "unauthenticated",
        ],
        "write_tools_enabled": False,
        "profile_neutral": True,
    }
