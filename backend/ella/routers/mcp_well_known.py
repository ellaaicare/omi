"""
RFC 8414 + RFC 9728 OAuth discovery endpoints for MCP auto-discovery.

These well-known endpoints allow MCP clients (like Grok Custom Connector)
to auto-discover the OAuth authorization server and protected resource
metadata, enabling one-click OAuth sign-in instead of manually filling
out OAuth form fields.

- GET /.well-known/oauth-protected-resource  (RFC 9728)
- GET /.well-known/oauth-authorization-server  (RFC 8414)

See: https://github.com/ellaaicare/omi/issues/209
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["MCP OAuth Discovery"])

_BASE_URL = os.getenv("ELLA_MCP_BASE_URL", "https://api.ella-ai-care.com")


def _base_url() -> str:
    return _BASE_URL.rstrip("/")


@router.get("/.well-known/oauth-protected-resource")
async def get_oauth_protected_resource():
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata.

    Tells MCP clients which authorization server protects this resource
    and what bearer token scopes are accepted.
    """
    base = _base_url()
    return JSONResponse(
        content={
            "resource": f"{base}/v1/ella/plato/mcp",
            "authorization_servers": [f"{base}"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [
                "context:read",
                "memory:read",
                "profile:read",
                "startup:read",
                "timeline:read",
                "tools:read",
                "proposals:read",
                "proposals:write",
            ],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/.well-known/oauth-authorization-server")
async def get_oauth_authorization_server():
    """RFC 8414 — OAuth 2.0 Authorization Server Metadata.

    Provides the full OAuth server metadata so MCP clients can
    auto-discover endpoints for authorization code flow.
    """
    base = _base_url()
    return JSONResponse(
        content={
            "issuer": f"{base}",
            "authorization_endpoint": f"{base}/v1/ella/mcp/authorize",
            "token_endpoint": f"{base}/v1/ella/mcp/token",
            "response_types_supported": ["code"],
            "grant_types_supported": [
                "authorization_code",
            ],
            "scopes_supported": [
                "context:read",
                "memory:read",
                "profile:read",
                "startup:read",
                "timeline:read",
                "tools:read",
                "proposals:read",
                "proposals:write",
            ],
            "token_endpoint_auth_methods_supported": ["none"],
            "service_documentation": f"{base}/v1/ella/mcp/info",
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )
