"""Generic MCP connector identity and profile-role resolution.

This module is intentionally independent of any one external client, agent,
or profile name. It resolves an authenticated external identity into one
selected Ella profile/role scope, or into an onboarding state that can be
shown by connector setup.
"""

from __future__ import annotations

import logging
import os
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("ella.mcp_identity")

STATE_AUTHENTICATED_MAPPED = "authenticated_mapped"
STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION = "authenticated_needs_profile_selection"
STATE_AUTHENTICATED_NEEDS_INVITE = "authenticated_needs_invite"
STATE_UNAUTHENTICATED = "unauthenticated"

ROLE_SELF = "self"
ROLE_CAREGIVER = "caregiver"
ROLE_ADMIN = "admin"

READ_ONLY_SCOPES = {
    "admin:read",
    "care_context:read",
    "context:read",
    "memory:read",
    "profile:read",
    "startup:read",
    "timeline:read",
    "tools:read",
}

PROPOSAL_SCOPES = {
    "proposals:read",
    "proposals:write",
    "rules:propose",
}

DEFAULT_ROLE_SCOPES = {
    ROLE_SELF: ["context:read", "timeline:read", "memory:read", "profile:read", "startup:read", "tools:read"],
    ROLE_CAREGIVER: ["care_context:read", "context:read", "profile:read", "startup:read", "timeline:read"],
    ROLE_ADMIN: ["admin:read", "profile:read", "startup:read", "tools:read"],
}


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def normalize_email(email: Any) -> str:
    return _clean_string(email).lower()


def _normalized_role(role: Any) -> str:
    value = _clean_string(role).lower()
    if value in {ROLE_SELF, ROLE_CAREGIVER, ROLE_ADMIN}:
        return value
    return ROLE_SELF


def _read_only_scopes(scopes: Iterable[Any], role: str) -> list[str]:
    requested = [_clean_string(scope) for scope in scopes if _clean_string(scope)]
    if not requested:
        requested = DEFAULT_ROLE_SCOPES.get(role, DEFAULT_ROLE_SCOPES[ROLE_SELF])
    return sorted(scope for scope in set(requested) if scope in READ_ONLY_SCOPES)


@dataclass(frozen=True)
class ExternalConnectorIdentity:
    provider: str
    subject: str
    email: str = ""
    email_verified: bool = False
    authenticated: bool = True
    display_name: str = ""

    @classmethod
    def unauthenticated(cls, provider: str = "") -> "ExternalConnectorIdentity":
        return cls(provider=provider, subject="", authenticated=False)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ExternalConnectorIdentity":
        return cls(
            provider=_clean_string(data.get("provider")),
            subject=_clean_string(data.get("subject") or data.get("sub")),
            email=normalize_email(data.get("email")),
            email_verified=bool(data.get("email_verified")),
            authenticated=bool(data.get("authenticated", True)),
            display_name=_clean_string(data.get("display_name") or data.get("name")),
        )

    @property
    def provider_subject(self) -> str:
        return f"{self.provider}:{self.subject}" if self.provider and self.subject else ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "subject": self.subject,
            "email": self.email if self.email_verified else "",
            "email_verified": self.email_verified,
            "authenticated": self.authenticated,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class MCPProfileGrant:
    grant_id: str
    profile_uid: str
    role: str = ROLE_SELF
    profile_label: str = ""
    status: str = "active"
    scopes: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "MCPProfileGrant":
        role = _normalized_role(data.get("role"))
        scopes = data.get("scopes") or data.get("allowed_scopes") or []
        allowed_tools = data.get("allowed_tools") or data.get("tools") or []
        return cls(
            grant_id=_clean_string(data.get("grant_id") or data.get("id") or data.get("document_id")),
            profile_uid=_clean_string(data.get("profile_uid") or data.get("uid") or data.get("user_id")),
            role=role,
            profile_label=_clean_string(data.get("profile_label") or data.get("display_name") or data.get("label")),
            status=_clean_string(data.get("status") or "active").lower(),
            scopes=_read_only_scopes(scopes, role),
            allowed_tools=sorted({_clean_string(tool) for tool in allowed_tools if _clean_string(tool)}),
            metadata=dict(data.get("metadata") or {}),
        )

    @property
    def active(self) -> bool:
        return self.status == "active" and bool(self.profile_uid)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "profile_uid": self.profile_uid,
            "role": self.role,
            "profile_label": self.profile_label,
            "scopes": list(self.scopes),
            "allowed_tools": list(self.allowed_tools),
        }


@dataclass(frozen=True)
class MCPIdentityResolution:
    state: str
    trace_id: str
    identity: ExternalConnectorIdentity
    selected_grant: Optional[MCPProfileGrant] = None
    available_grants: list[MCPProfileGrant] = field(default_factory=list)
    message: str = ""

    @property
    def mapped(self) -> bool:
        return self.state == STATE_AUTHENTICATED_MAPPED and self.selected_grant is not None

    def to_public_dict(self, include_claims: bool = False, ttl_seconds: int = 3600) -> dict[str, Any]:
        payload = {
            "state": self.state,
            "trace_id": self.trace_id,
            "identity": self.identity.to_public_dict(),
            "selected_profile": self.selected_grant.to_public_dict() if self.selected_grant else None,
            "available_profiles": [grant.to_public_dict() for grant in self.available_grants],
            "message": self.message,
        }
        if include_claims and self.mapped:
            payload["session_claims"] = build_session_claims(self, ttl_seconds=ttl_seconds)
        return payload


def load_identity_grants(identity: ExternalConnectorIdentity) -> list[MCPProfileGrant]:
    """Load durable grants for an external identity from Firestore.

    Collection shape is intentionally generic:
    `mcp_identity_grants/{grant_id}` with fields such as provider_subject,
    email_norm, profile_uid, role, scopes, allowed_tools, and status.
    """
    if not identity.authenticated:
        return []

    collection_name = os.getenv("ELLA_MCP_IDENTITY_GRANTS_COLLECTION", "mcp_identity_grants")
    rows: dict[str, dict[str, Any]] = {}
    try:
        from database._client import db

        if identity.provider_subject:
            docs = (
                db.collection(collection_name)
                .where("provider_subject", "==", identity.provider_subject)
                .where("status", "==", "active")
                .stream()
            )
            for doc in docs:
                row = doc.to_dict() or {}
                row["document_id"] = doc.id
                rows[doc.id] = row

        if identity.email_verified and identity.email:
            docs = (
                db.collection(collection_name)
                .where("email_norm", "==", identity.email)
                .where("status", "==", "active")
                .stream()
            )
            for doc in docs:
                row = doc.to_dict() or {}
                row["document_id"] = doc.id
                rows[doc.id] = row
    except Exception as exc:
        logger.warning("mcp_identity grant lookup failed: %s", exc)
        return []

    return [grant for row in rows.values() if (grant := MCPProfileGrant.from_mapping(row)).active]


def _grant_document_id(identity: ExternalConnectorIdentity, profile_uid: str, role: str) -> str:
    seed = f"{identity.provider_subject}:{profile_uid}:{_normalized_role(role)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def grant_to_firestore_data(
    *,
    identity: ExternalConnectorIdentity,
    profile_uid: str,
    role: str = ROLE_SELF,
    profile_label: str = "",
    scopes: Optional[list[str]] = None,
    allowed_tools: Optional[list[str]] = None,
    status: str = "active",
    grant_id: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    """Build the durable grant record stored in `mcp_identity_grants`.

    Proposal/write scopes are intentionally filtered out by `MCPProfileGrant`.
    A later writeback PR must explicitly add role-scoped proposal permissions.
    """
    normalized_role = _normalized_role(role)
    grant = MCPProfileGrant.from_mapping(
        {
            "grant_id": grant_id or _grant_document_id(identity, profile_uid, normalized_role),
            "profile_uid": profile_uid,
            "role": normalized_role,
            "profile_label": profile_label,
            "scopes": scopes or [],
            "allowed_tools": allowed_tools or [],
            "status": status,
        }
    )
    if not identity.authenticated or not identity.provider_subject:
        raise ValueError("authenticated external identity with provider and subject is required")
    if not grant.active:
        raise ValueError("active profile_uid is required")

    now = datetime.now(timezone.utc)
    data = {
        "grant_id": grant.grant_id,
        "provider": identity.provider,
        "subject": identity.subject,
        "provider_subject": identity.provider_subject,
        "email_norm": identity.email if identity.email_verified else "",
        "email_verified": identity.email_verified,
        "display_name": identity.display_name,
        "profile_uid": grant.profile_uid,
        "profile_label": grant.profile_label,
        "role": grant.role,
        "scopes": grant.scopes,
        "allowed_tools": grant.allowed_tools,
        "status": status,
        "created_by": created_by,
        "updated_at": now,
    }
    return data


def provision_identity_grant(
    *,
    identity: ExternalConnectorIdentity,
    profile_uid: str,
    role: str = ROLE_SELF,
    profile_label: str = "",
    scopes: Optional[list[str]] = None,
    allowed_tools: Optional[list[str]] = None,
    status: str = "active",
    grant_id: str = "",
    created_by: str = "",
) -> MCPProfileGrant:
    """Create or update a durable MCP identity grant.

    This is a backend/admin helper, not an external MCP tool. It only writes
    grant metadata and does not enable proposal writeback, delivery, scanner,
    or memory mutation.
    """
    data = grant_to_firestore_data(
        identity=identity,
        profile_uid=profile_uid,
        role=role,
        profile_label=profile_label,
        scopes=scopes,
        allowed_tools=allowed_tools,
        status=status,
        grant_id=grant_id,
        created_by=created_by,
    )
    collection_name = os.getenv("ELLA_MCP_IDENTITY_GRANTS_COLLECTION", "mcp_identity_grants")
    from database._client import db

    ref = db.collection(collection_name).document(data["grant_id"])
    snapshot = ref.get()
    if not getattr(snapshot, "exists", False):
        data["created_at"] = data["updated_at"]
    ref.set(data, merge=True)
    return MCPProfileGrant.from_mapping(data)


def resolve_mcp_identity(
    identity: ExternalConnectorIdentity,
    *,
    selected_profile_uid: str = "",
    grants: Optional[list[MCPProfileGrant]] = None,
    trace_id: str = "",
) -> MCPIdentityResolution:
    trace_id = trace_id or str(uuid.uuid4())
    if not identity.authenticated or not identity.provider or not identity.subject:
        return MCPIdentityResolution(
            state=STATE_UNAUTHENTICATED,
            trace_id=trace_id,
            identity=identity,
            message="Authentication is required before MCP tools can be exposed.",
        )

    active_grants = [
        grant for grant in (grants if grants is not None else load_identity_grants(identity)) if grant.active
    ]
    if not active_grants:
        return MCPIdentityResolution(
            state=STATE_AUTHENTICATED_NEEDS_INVITE,
            trace_id=trace_id,
            identity=identity,
            message="Identity is authenticated but is not authorized for an Ella profile yet.",
        )

    selected_profile_uid = _clean_string(selected_profile_uid)
    if selected_profile_uid:
        selected = next((grant for grant in active_grants if grant.profile_uid == selected_profile_uid), None)
        if selected:
            return MCPIdentityResolution(
                state=STATE_AUTHENTICATED_MAPPED,
                trace_id=trace_id,
                identity=identity,
                selected_grant=selected,
                available_grants=active_grants,
                message="Identity is mapped to the selected profile.",
            )
        return MCPIdentityResolution(
            state=STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION,
            trace_id=trace_id,
            identity=identity,
            available_grants=active_grants,
            message="Selected profile is not authorized for this identity.",
        )

    if len(active_grants) == 1:
        return MCPIdentityResolution(
            state=STATE_AUTHENTICATED_MAPPED,
            trace_id=trace_id,
            identity=identity,
            selected_grant=active_grants[0],
            available_grants=active_grants,
            message="Identity is mapped to one profile.",
        )

    return MCPIdentityResolution(
        state=STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION,
        trace_id=trace_id,
        identity=identity,
        available_grants=active_grants,
        message="Identity can access multiple profiles; select one before exposing profile context.",
    )


def build_session_claims(resolution: MCPIdentityResolution, *, ttl_seconds: int = 3600) -> dict[str, Any]:
    if not resolution.mapped or resolution.selected_grant is None:
        raise ValueError("Cannot build MCP session claims for an unmapped identity")
    grant = resolution.selected_grant
    now = int(time.time())
    return {
        "iss": "ella-mcp",
        "sub": resolution.identity.provider_subject,
        "aud": "ella-mcp-tools",
        "iat": now,
        "exp": now + max(60, ttl_seconds),
        "trace_id": resolution.trace_id,
        "profile_uid": grant.profile_uid,
        "role": grant.role,
        "scopes": list(grant.scopes),
        "allowed_tools": list(grant.allowed_tools),
        "grant_id": grant.grant_id,
        "external_provider": resolution.identity.provider,
    }
