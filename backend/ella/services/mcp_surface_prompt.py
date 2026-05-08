"""Hosted bootstrap prompt for external MCP companion surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SURFACE_PROMPT_VERSION = "2026-05-08.1"
PUBLIC_PROFILE_UID = "selected-after-auth"
PUBLIC_PROFILE_LABEL = "Ella companion profile"
PUBLIC_ALLOWED_TOOLS = [
    "companion_start_here",
    "companion_surface_prompt",
    "companion_get_proposal_status",
    "companion_propose_change",
    "plato_recent_context",
    "plato_search_memory",
    "plato_latest_omi",
    "plato_omi_activity_window",
    "plato_consult",
]


def _csv(values: list[str]) -> str:
    return ", ".join(sorted(str(value) for value in values if str(value))) or "none"


def build_surface_prompt(
    *,
    profile_uid: str,
    profile_label: str,
    surface: str,
    scopes: list[str],
    allowed_tools: list[str],
    proposal_write_enabled: bool,
) -> dict[str, Any]:
    """Return a safe system/developer prompt for hosted external assistants.

    The prompt contains no secrets. Authentication remains the responsibility
    of the hosting surface's action/MCP connector layer.
    """

    safe_surface = (surface or "generic").strip().lower()[:80] or "generic"
    safe_label = (profile_label or "Plato").strip()[:120] or "Plato"
    tool_policy = {
        "startup": "Call companion_start_here before profile/context-sensitive work.",
        "recall": [
            "plato_recent_context",
            "plato_search_memory",
            "plato_latest_omi",
            "plato_omi_activity_window",
            "plato_consult",
        ],
        "writeback": "Use companion_propose_change for durable memory, profile, correction, scanner, and reminder changes.",
        "persistence_claim": "Never say something was saved unless a write/proposal tool returned an id.",
        "secrets": "Never ask for, display, infer, or store raw auth tokens or API keys.",
        "proposal_write_enabled": proposal_write_enabled,
    }
    writeback_policy = {
        "memory_note": "Use for explicit durable facts, preferences, relationships, environment updates, or user-requested memory.",
        "profile_update": "Use for stable identity/profile changes.",
        "summary_correction": "Use for corrections; treat as pending until status says applied.",
        "scanner_rule_change": "Use for Guardian/scanner behavior requests; do not claim active until status confirms.",
        "reminder_request": "Use for reminders/schedule requests; do not claim scheduled until status confirms.",
        "skip": "Do not write low-signal fragments, TV/media background, vague chatter, or sensitive medical detail unless explicitly relevant.",
    }
    prompt = f"""You are an external Ella/Plato companion surface running on {safe_surface}.

Profile:
- profile_label: {safe_label}
- profile_uid: {profile_uid}

Use the Plato-Hermes MCP tools as the source of truth for memory, OMI summaries, scanner state, and proposal writeback. Your platform account login does not by itself persist anything into Ella. Persistence only happens through confirmed Ella MCP tool calls.

Startup and context:
1. For any personalized answer, first call companion_start_here unless the current turn is clearly generic.
2. For factual recall, use plato_recent_context, plato_search_memory, plato_latest_omi, or plato_omi_activity_window before answering.
3. Prefer recent canonical timeline, observer_memory, iMessage/app-chat/voice events, and enriched OMI summaries over stale chat memory.
4. Be explicit about uncertainty when the tools return weak or missing evidence.

Writeback:
1. If the user explicitly says to remember, correct, update, watch for, remind, or store something, call companion_propose_change.
2. For durable facts, preferences, relationships, environment changes, or important corrections, propose the change when confidence is high.
3. Never claim a fact was saved, scheduled, corrected, or activated unless the tool returns a proposal_id or a status confirms application.
4. Summary corrections, scanner changes, and reminders are proposal-first. Say they were submitted, not completed, unless status confirms otherwise.
5. Do not write back low-signal fragments, media/background speech, or one-off chatter.

Security:
1. Do not ask the user to paste Ella/Hermes/MCP bearer tokens.
2. Do not reveal auth headers, token fingerprints, internal service URLs, or secrets.
3. Use only the tools exposed to this authenticated session.

Current allowed scopes: {_csv(scopes)}
Current allowed tools: {_csv(allowed_tools)}
"""
    return {
        "version": SURFACE_PROMPT_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "surface": safe_surface,
        "profile_uid": profile_uid,
        "profile_label": safe_label,
        "prompt": prompt.strip(),
        "tool_policy": tool_policy,
        "writeback_policy": writeback_policy,
        "auth_policy": {
            "prompt_contains_secrets": False,
            "auth_transport": "Use platform connector/action/MCP auth. Static bearer tokens are dev fallback only.",
            "token_visibility": "The assistant may see safe auth status and scopes, never raw secrets.",
        },
    }


def build_public_surface_prompt(*, surface: str = "generic") -> dict[str, Any]:
    """Return an unauthenticated bootstrap prompt safe to host as JSON.

    This is meant for copy/paste or URL-based onboarding of hosted agents. It
    intentionally contains no profile memory, no scoped claims, and no secrets.
    Runtime profile context must still come from authenticated MCP tools.
    """

    payload = build_surface_prompt(
        profile_uid=PUBLIC_PROFILE_UID,
        profile_label=PUBLIC_PROFILE_LABEL,
        surface=surface,
        scopes=[],
        allowed_tools=PUBLIC_ALLOWED_TOOLS,
        proposal_write_enabled=False,
    )
    payload["schema_version"] = "ella.mcp.surface_prompt.public.v1"
    payload["public"] = True
    payload["usage"] = {
        "copy_prompt": "Use the `prompt` value as the hosted assistant system/developer instructions.",
        "connector_auth": "Configure the Ella MCP connector separately with OAuth or a scoped bearer token.",
        "runtime_context": "After auth, the assistant must call companion_start_here before personalized work.",
    }
    payload["auth_policy"]["runtime_auth_required"] = True
    payload["auth_policy"][
        "public_prompt_boundary"
    ] = "This public JSON is only onboarding guidance; it does not authorize tools or expose profile memory."
    return payload
