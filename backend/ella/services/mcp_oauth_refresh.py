"""Default-OFF renewable sessions for the exact signed summary-tool resource."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from database.mcp_oauth_refresh import MCPRefreshRepository, read_refresh_authority
from ella.services.ai_consent import AiConsentService, FirestoreConsentRepository
from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPIdentityResolution,
    MCPProfileGrant,
    SUMMARY_OPERATION_TOOLS,
    STATE_AUTHENTICATED_MAPPED,
    issue_mcp_session_token,
)
from ella.services.runtime_resolver import runtime_authority_identity
from ella.services.summary_runtime import require_summary_runtime

# The existing DCR registry remains process-local. Restart requires reauthorization.
registered_clients: dict[str, dict[str, Any]] = {}
RENEWAL_CLAIMS = frozenset({"refresh_family_id", "refresh_authority_digest"})
ACCESS_SCOPES = frozenset({"tools:read", "memory:read", "summaries:write"})


def refresh_enabled() -> bool:
    return os.getenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "false").strip().lower() == "true"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def token_digest(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise ValueError("invalid_grant")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _client_digest(client_id: str) -> str:
    registration = registered_clients.get(client_id)
    if (
        not registration
        or registration.get("token_endpoint_auth_method") != "none"
        or not isinstance(registration.get("redirect_uris"), list)
        or not isinstance(registration.get("grant_types"), list)
        or not isinstance(registration.get("response_types"), list)
        or "refresh_token" not in registration.get("grant_types", [])
        or "authorization_code" not in registration.get("grant_types", [])
    ):
        raise ValueError("invalid_client")
    return _digest(
        {
            key: registration.get(key)
            for key in (
                "client_id",
                "redirect_uris",
                "grant_types",
                "response_types",
                "token_endpoint_auth_method",
            )
        }
    )


def require_refresh_authorization(client_id: str, redirect_uri: str, challenge: str, method: str) -> None:
    if not refresh_enabled():
        raise ValueError("unsupported_grant_type")
    _client_digest(client_id)
    if redirect_uri not in registered_clients[client_id].get("redirect_uris", []):
        raise ValueError("invalid_request")
    if method != "S256" or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None:
        raise ValueError("invalid_request")


@dataclass(frozen=True)
class _SnapshotConsentRepository:
    state: dict[str, Any]
    receipt: dict[str, Any]

    def get_current(self, _uid: str):
        return self.state, self.receipt


async def capture_authority(
    *,
    provider: str,
    subject: str,
    uid: str,
    grant_id: str,
    client_id: str,
    scopes: list[str],
    tools: list[str] | None = None,
) -> tuple[dict[str, Any], MCPIdentityResolution]:
    registration_digest = _client_digest(client_id)
    snapshot = read_refresh_authority(
        FirestoreConsentRepository._configured_db(),
        uid=uid,
        grant_id=grant_id,
        collection=os.getenv("ELLA_MCP_IDENTITY_GRANTS_COLLECTION", "mcp_identity_grants"),
    )
    grant = MCPProfileGrant.from_mapping({**snapshot.grant, "document_id": grant_id})
    if (
        not provider
        or provider == "static_bearer"
        or not subject
        or snapshot.grant.get("provider_subject") != f"{provider}:{subject}"
        or grant.grant_id != grant_id
        or grant.profile_uid != uid
        or grant.role != "self"
        or not grant.active
        or not scopes
        or "tools:read" not in scopes
        or not set(scopes) <= ACCESS_SCOPES & set(grant.scopes)
        or not isinstance(snapshot.grant.get("scopes"), list)
        or not set(scopes) <= set(snapshot.grant["scopes"])
    ):
        raise ValueError("invalid_grant")
    expires_at = snapshot.grant.get("expires_at")
    if expires_at is not None and (
        not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc)
    ):
        raise ValueError("invalid_grant")
    status = AiConsentService(_SnapshotConsentRepository(snapshot.state, snapshot.receipt)).status(uid)
    if status.get("authorized") is not True or status.get("authority_state") != "authorized":
        raise ValueError("invalid_grant")
    runtime = await require_summary_runtime(uid)
    if (
        grant.metadata.get("runtime_binding_id") != runtime.binding_id
        or grant.metadata.get("profile_user_id") != runtime.profile_user_id
    ):
        raise ValueError("invalid_grant")
    allowed = set(grant.allowed_tools) & set(runtime.allowed_tools) & set(SUMMARY_OPERATION_TOOLS)
    allowed = {
        tool
        for tool in allowed
        if ("memory:read" if tool == "companion_get_conversation_summary" else "summaries:write") in scopes
    }
    chosen = sorted(allowed if tools is None else set(tools))
    if not chosen or not set(chosen) <= allowed:
        raise ValueError("invalid_grant")
    # Catch grant/consent replacement during the awaited runtime resolution.
    final = read_refresh_authority(
        FirestoreConsentRepository._configured_db(),
        uid=uid,
        grant_id=grant_id,
        collection=os.getenv("ELLA_MCP_IDENTITY_GRANTS_COLLECTION", "mcp_identity_grants"),
    )
    if final.revisions != snapshot.revisions or _client_digest(client_id) != registration_digest:
        raise ValueError("invalid_grant")
    material = {
        "provider": provider,
        "subject": subject,
        "profile_uid": uid,
        "grant_id": grant_id,
        "client_id": client_id,
        "client_digest": registration_digest,
        "account_user_id": runtime.account_user_id,
        "profile_user_id": runtime.profile_user_id,
        "runtime_digest": runtime_authority_identity(runtime).digest,
        "source_revisions": list(snapshot.revisions),
        "scopes": sorted(set(scopes)),
        "tools": chosen,
    }
    selected = MCPProfileGrant(
        grant_id=grant_id,
        profile_uid=uid,
        profile_label=grant.profile_label,
        role="self",
        scopes=material["scopes"],
        allowed_tools=chosen,
        metadata=grant.metadata,
    )
    return material, MCPIdentityResolution(
        state=STATE_AUTHENTICATED_MAPPED,
        trace_id=secrets.token_hex(16),
        identity=ExternalConnectorIdentity(provider=provider, subject=subject),
        selected_grant=selected,
    )


async def _current(material: dict[str, Any]):
    return await capture_authority(
        provider=material["provider"],
        subject=material["subject"],
        uid=material["profile_uid"],
        grant_id=material["grant_id"],
        client_id=material["client_id"],
        scopes=material["scopes"],
        tools=material["tools"],
    )


def _response(row: dict[str, Any], resolution: MCPIdentityResolution, secret: str, ttl_seconds: int) -> dict[str, Any]:
    expiry = min(int(time.time()) + min(ttl_seconds, 3600), int(row["expires_at"].timestamp()))
    access_token, claims = issue_mcp_session_token(
        resolution,
        ttl_seconds=ttl_seconds,
        refresh_family_id=str(row["id"]),
        refresh_authority_digest=row["authority_digest"],
        access_expires_at=expiry,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": max(0, expiry - int(time.time())),
        "refresh_token": secret,
        "scope": " ".join(claims["scopes"]),
        "session_claims": claims,
    }


async def _confirmed_response(repository: MCPRefreshRepository, row: dict[str, Any], secret: str, ttl_seconds: int):
    try:
        current, resolution = await _current(row["authority"])
        final = await repository.read(str(row["id"]))
        if (
            not hmac.compare_digest(_digest(current), row["authority_digest"])
            or final is None
            or not final["usable"]
            or final["authority_digest"] != row["authority_digest"]
        ):
            raise ValueError("invalid_grant")
        return _response(final, resolution, secret, ttl_seconds)
    except Exception:
        await repository.revoke(str(row["id"]))
        raise


async def issue_refresh_family(resolution: MCPIdentityResolution, *, client_id: str, scope: str, ttl_seconds: int):
    if not refresh_enabled() or resolution.selected_grant is None:
        raise ValueError("unsupported_grant_type")
    scopes = sorted(set(scope.split()) - {"offline_access"})
    if not set(scopes) <= set(resolution.selected_grant.scopes):
        raise ValueError("invalid_scope")
    tools = sorted(
        tool
        for tool in set(resolution.selected_grant.allowed_tools) & set(SUMMARY_OPERATION_TOOLS)
        if ("memory:read" if tool == "companion_get_conversation_summary" else "summaries:write") in scopes
    )
    material, _ = await capture_authority(
        provider=resolution.identity.provider,
        subject=resolution.identity.subject,
        uid=resolution.selected_grant.profile_uid,
        grant_id=resolution.selected_grant.grant_id,
        client_id=client_id,
        scopes=scopes,
        tools=tools,
    )
    repository = await MCPRefreshRepository.create()
    secret = secrets.token_urlsafe(32)
    row = await repository.create_family(material, _digest(material), token_digest(secret))
    return await _confirmed_response(repository, row, secret, ttl_seconds)


async def renew_refresh_family(*, token: str, client_id: str, scope: str, ttl_seconds: int):
    if not refresh_enabled():
        raise ValueError("unsupported_grant_type")
    _client_digest(client_id)
    repository = await MCPRefreshRepository.create()
    old_digest = token_digest(token)
    row = await repository.find(old_digest)
    if row is None or row["client_id"] != client_id:
        raise ValueError("invalid_grant")
    # This foundation renews the exact original ceiling; no scope expansion or drift.
    if scope and set(scope.split()) != set(row["authority"]["scopes"]):
        raise ValueError("invalid_scope")
    try:
        current, _ = await _current(row["authority"])
        if not hmac.compare_digest(_digest(current), row["authority_digest"]):
            raise ValueError("invalid_grant")
    except Exception:
        await repository.revoke(str(row["id"]))
        raise
    secret = secrets.token_urlsafe(32)
    rotated = await repository.rotate(
        old_digest, token_digest(secret), client_id=client_id, authority_digest=_digest(current)
    )
    return await _confirmed_response(repository, rotated, secret, ttl_seconds)


async def validate_renewable_session(claims: dict[str, Any]) -> None:
    if not RENEWAL_CLAIMS.intersection(claims):
        return
    if not refresh_enabled() or not RENEWAL_CLAIMS <= claims.keys():
        raise ValueError("invalid_grant")
    repository = await MCPRefreshRepository.create()
    row = await repository.read(claims["refresh_family_id"])
    if row is None or not row["usable"]:
        raise ValueError("invalid_grant")
    material = row["authority"]
    if (
        claims.get("sub") != f"{material['provider']}:{material['subject']}"
        or claims.get("external_provider") != material["provider"]
        or claims.get("role") != "self"
        or claims.get("profile_uid") != material["profile_uid"]
        or claims.get("grant_id") != material["grant_id"]
        or set(claims.get("scopes") or []) != set(material["scopes"])
        or set(claims.get("allowed_tools") or []) != set(material["tools"])
        or claims.get("refresh_authority_digest") != row["authority_digest"]
        or type(claims.get("exp")) is not int
        or claims["exp"] > row["expires_at"].timestamp()
        or claims["exp"] <= time.time()
    ):
        raise ValueError("invalid_grant")
    try:
        current, _ = await _current(material)
        final = await repository.read(str(row["id"]))
        if final is None or not final["usable"] or not hmac.compare_digest(_digest(current), row["authority_digest"]):
            raise ValueError("invalid_grant")
    except Exception:
        await repository.revoke(str(row["id"]))
        raise
