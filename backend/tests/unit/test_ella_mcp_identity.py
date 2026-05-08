import pytest

from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPProfileGrant,
    STATE_AUTHENTICATED_MAPPED,
    STATE_AUTHENTICATED_NEEDS_INVITE,
    STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION,
    STATE_UNAUTHENTICATED,
    build_session_claims,
    resolve_mcp_identity,
)


def _identity():
    return ExternalConnectorIdentity(
        provider="google",
        subject="google-sub-1",
        email="person@example.com",
        email_verified=True,
        display_name="Test Person",
    )


def _grant(profile_uid="user-1", role="self", **overrides):
    data = {
        "grant_id": f"grant-{profile_uid}",
        "profile_uid": profile_uid,
        "role": role,
        "profile_label": f"Profile {profile_uid}",
        "scopes": overrides.pop("scopes", []),
        "allowed_tools": overrides.pop("allowed_tools", ["companion_start_here", "companion_recent_context"]),
        "status": overrides.pop("status", "active"),
        **overrides,
    }
    return MCPProfileGrant.from_mapping(data)


def test_resolve_unauthenticated_identity():
    resolution = resolve_mcp_identity(ExternalConnectorIdentity.unauthenticated("google"), grants=[])

    assert resolution.state == STATE_UNAUTHENTICATED
    assert not resolution.mapped
    assert "Authentication is required" in resolution.message


def test_resolve_authenticated_unknown_identity_needs_invite():
    resolution = resolve_mcp_identity(_identity(), grants=[])

    assert resolution.state == STATE_AUTHENTICATED_NEEDS_INVITE
    assert not resolution.mapped
    assert resolution.available_grants == []


def test_single_grant_maps_and_builds_read_only_claims():
    grant = _grant(scopes=["context:read", "startup:read", "memory:write", "delete:memory"])
    resolution = resolve_mcp_identity(_identity(), grants=[grant], trace_id="trace-1")

    assert resolution.state == STATE_AUTHENTICATED_MAPPED
    assert resolution.selected_grant.profile_uid == "user-1"

    claims = build_session_claims(resolution, ttl_seconds=600)
    assert claims["sub"] == "google:google-sub-1"
    assert claims["profile_uid"] == "user-1"
    assert claims["role"] == "self"
    assert claims["trace_id"] == "trace-1"
    assert claims["scopes"] == ["context:read", "startup:read"]
    assert "memory:write" not in claims["scopes"]
    assert "delete:memory" not in claims["scopes"]


def test_multiple_grants_require_profile_selection():
    resolution = resolve_mcp_identity(_identity(), grants=[_grant("user-1"), _grant("mom-1", "caregiver")])

    assert resolution.state == STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION
    assert not resolution.mapped
    assert [grant.profile_uid for grant in resolution.available_grants] == ["user-1", "mom-1"]


def test_selected_caregiver_profile_maps_to_caregiver_claims():
    resolution = resolve_mcp_identity(
        _identity(),
        selected_profile_uid="mom-1",
        grants=[_grant("user-1"), _grant("mom-1", "caregiver")],
        trace_id="trace-2",
    )

    assert resolution.state == STATE_AUTHENTICATED_MAPPED
    assert resolution.selected_grant.role == "caregiver"

    claims = build_session_claims(resolution, ttl_seconds=600)
    assert claims["profile_uid"] == "mom-1"
    assert claims["role"] == "caregiver"
    assert "care_context:read" in claims["scopes"]
    assert all(not scope.endswith(":write") for scope in claims["scopes"])


def test_invalid_selected_profile_still_requires_selection():
    resolution = resolve_mcp_identity(
        _identity(),
        selected_profile_uid="not-authorized",
        grants=[_grant("user-1"), _grant("mom-1", "caregiver")],
    )

    assert resolution.state == STATE_AUTHENTICATED_NEEDS_PROFILE_SELECTION
    assert resolution.selected_grant is None
    assert "not authorized" in resolution.message


def test_cannot_build_claims_for_unmapped_identity():
    resolution = resolve_mcp_identity(_identity(), grants=[])

    with pytest.raises(ValueError):
        build_session_claims(resolution)
