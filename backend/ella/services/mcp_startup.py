"""Startup context packet for external Ella MCP clients."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from utils.ella.canonical_context import DEFAULT_CONTEXT_CHANNELS, fetch_canonical_timeline, format_canonical_context
from utils.ella.time_context import annotate_event_time, build_time_context, timezone_name

DEFAULT_STARTUP_LIMIT = 12
MAX_STARTUP_LIMIT = 50

READ_ONLY_TOOL_CATALOG = [
    {
        "name": "companion_start_here",
        "status": "available",
        "scope": "startup:read",
        "description": "Return the safe startup packet for the authenticated profile.",
    },
    {
        "name": "companion_recent_context",
        "status": "planned",
        "scope": "timeline:read",
        "description": "Read recent canonical timeline events through the generic MCP surface.",
    },
    {
        "name": "companion_search_memory",
        "status": "planned",
        "scope": "memory:read",
        "description": "Search canonical timeline and memory representations.",
    },
    {
        "name": "companion_get_proposal_status",
        "status": "available",
        "scope": "startup:read",
        "description": "Read status for an auditable proposal record by ID.",
    },
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_time(event: dict[str, Any]) -> str:
    return str(event.get("started_at") or event.get("created_at") or event.get("timestamp") or "")


def _event_title(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
    return str(event.get("title") or structured.get("title") or event.get("channel") or "event")


def _event_text(event: dict[str, Any], limit: int = 500) -> str:
    text = str(event.get("text") or event.get("overview") or event.get("summary") or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "channel": event.get("channel"),
        "provider": event.get("provider"),
        "role": event.get("role"),
        "started_at": _event_time(event),
        "started_at_utc": event.get("started_at_utc") or _event_time(event),
        "started_at_local": event.get("started_at_local"),
        "started_at_local_date": event.get("started_at_local_date"),
        "started_at_local_time": event.get("started_at_local_time"),
        "started_at_timezone": event.get("started_at_timezone"),
        "relative_to_now": event.get("relative_to_now"),
        "seconds_from_now": event.get("seconds_from_now"),
        "time": event.get("time") if isinstance(event.get("time"), dict) else {},
        "title": _event_title(event),
        "text": _event_text(event),
        "source_identity": event.get("source_identity"),
        "source_ref": event.get("source_ref") if isinstance(event.get("source_ref"), dict) else {},
    }


def _latest_by_channel(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        channel = str(event.get("channel") or "unknown")
        current = latest.get(channel)
        if current is None or _event_time(event) > str(current.get("started_at") or ""):
            latest[channel] = _compact_event(event)
    return latest


def _clamp_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_STARTUP_LIMIT
    return max(1, min(parsed, MAX_STARTUP_LIMIT))


def _allowed_tool_names(session_claims: dict[str, Any]) -> set[str]:
    return {str(tool) for tool in session_claims.get("allowed_tools") or [] if str(tool)}


def _startup_tools(session_claims: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = _allowed_tool_names(session_claims)
    if not allowed:
        return READ_ONLY_TOOL_CATALOG
    return [
        {**tool, "status": tool["status"] if tool["name"] in allowed else "hidden"}
        for tool in READ_ONLY_TOOL_CATALOG
        if tool["name"] in allowed or tool["status"] == "available"
    ]


async def build_startup_context(
    *,
    onboarding: dict[str, Any],
    limit: int = DEFAULT_STARTUP_LIMIT,
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """Build a profile-neutral read-only startup packet.

    The caller supplies the already-authenticated onboarding resolution from
    `ella.services.mcp_identity`. Unmapped identities receive the onboarding
    state only, so we never leak profile context before account-role mapping.
    """
    state = str(onboarding.get("state") or "")
    selected_profile = (
        onboarding.get("selected_profile") if isinstance(onboarding.get("selected_profile"), dict) else None
    )
    session_claims = onboarding.get("session_claims") if isinstance(onboarding.get("session_claims"), dict) else {}
    if state != "authenticated_mapped" or not selected_profile:
        return {
            "schema_version": "ella.mcp.start_here.v1",
            "generated_at": _utc_now(),
            "time_context": build_time_context(),
            "onboarding": onboarding,
            "startup_ready": False,
            "reason": "identity_not_mapped",
            "writeback_policy": _writeback_policy(session_claims),
        }

    uid = str(selected_profile.get("profile_uid") or session_claims.get("profile_uid") or "")
    user_tz = timezone_name(
        str(
            selected_profile.get("timezone")
            or selected_profile.get("time_zone")
            or session_claims.get("timezone")
            or session_claims.get("time_zone")
            or ""
        )
    )
    requested_channels = channels or DEFAULT_CONTEXT_CHANNELS
    effective_limit = _clamp_limit(limit)
    events = await fetch_canonical_timeline(
        uid,
        limit=effective_limit,
        channels=requested_channels,
        user_timezone=user_tz,
    )
    events = [annotate_event_time(event, tz_name=user_tz) for event in events]
    compact_events = [_compact_event(event) for event in events]
    channel_counts = Counter(str(event.get("channel") or "unknown") for event in events)
    return {
        "schema_version": "ella.mcp.start_here.v1",
        "generated_at": _utc_now(),
        "time_context": build_time_context(user_tz),
        "startup_ready": True,
        "onboarding": {
            "state": state,
            "trace_id": onboarding.get("trace_id"),
            "selected_profile": selected_profile,
            "available_profiles": onboarding.get("available_profiles") or [],
        },
        "account": {
            "profile_uid": uid,
            "role": session_claims.get("role") or selected_profile.get("role"),
            "grant_id": session_claims.get("grant_id"),
            "scopes": session_claims.get("scopes") or [],
        },
        "memory": {
            "source": "canonical_timeline",
            "channels_requested": requested_channels,
            "event_count": len(compact_events),
            "channel_counts": dict(channel_counts),
            "latest_by_channel": _latest_by_channel(events),
            "recent_events": compact_events,
            "summary": format_canonical_context(events, max_chars=5000, user_timezone=user_tz),
        },
        "tools": {
            "read_only": True,
            "available": _startup_tools(session_claims),
        },
        "writeback_policy": _writeback_policy(session_claims),
        "escalation_boundaries": {
            "caregiver_delivery": "not_available_from_mcp",
            "emergency_actions": "not_available_from_mcp",
            "scanner_rule_changes": "proposal_only_future_scope",
            "direct_memory_mutation": "disabled",
        },
    }


def _writeback_policy(session_claims: dict[str, Any]) -> dict[str, Any]:
    scopes = set(session_claims.get("scopes") or [])
    return {
        "mode": "read_only",
        "proposal_tools_visible": "proposals:read" in scopes or "proposals:write" in scopes,
        "direct_write_enabled": False,
        "proposal_write_enabled": False,
        "blocked_until": ["ella-ai#859 role-scoped proposal tools"],
    }
