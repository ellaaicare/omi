"""Canonical timeline context helpers for Ella chat and Guardian paths."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from utils.ella.canonical_auth import canonical_event_service_headers
from utils.ella.time_context import annotate_event_time, build_time_context, timezone_name

DEFAULT_TIMELINE_URL = os.getenv("ELLA_CANONICAL_TIMELINE_URL", "http://127.0.0.1:8000/v1/ella/timeline")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("ELLA_CANONICAL_TIMELINE_TIMEOUT", "5"))
DEFAULT_CONTEXT_CHANNELS = [
    "omi",
    "ios_chat",
    "ios_voice",
    "imessage",
    "telegram",
    "guardian",
    "memory",
    "observer_memory",
    "companion_observation",
    "grok_conversation",
    "companion_note",
    "companion_summary",
    "companion_idea",
]


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_time(event: dict[str, Any]) -> str:
    return str(event.get("started_at") or event.get("created_at") or event.get("timestamp") or "")


def _event_title(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
    return str(event.get("title") or structured.get("title") or event.get("channel") or "event")


def _event_text(event: dict[str, Any], max_chars: int = 900) -> str:
    text = str(event.get("text") or event.get("overview") or event.get("summary") or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "..."


def _role_for_event(event: dict[str, Any]) -> str:
    role = str(event.get("role") or "").lower()
    if role in {"assistant", "ai"}:
        return "assistant"
    return "user"


async def fetch_canonical_timeline(
    uid: str,
    *,
    limit: int = 50,
    channels: Optional[list[str]] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    user_timezone: Optional[str] = None,
    timeout: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Read canonical timeline over HTTP per the unified memory contract."""
    if not uid:
        return []

    effective_limit = max(1, min(int(limit or 50), 500))
    fetch_limit = effective_limit
    before_dt = None
    if before:
        before_dt = _parse_iso(before)
        fetch_limit = min(max(effective_limit * 3, effective_limit), 500)

    params: dict[str, Any] = {"uid": uid, "limit": fetch_limit}
    if channels:
        params["channels"] = ",".join(channel for channel in channels if channel)
    if since:
        params["since"] = since
    if user_timezone:
        params["timezone"] = user_timezone

    async with httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(
            DEFAULT_TIMELINE_URL,
            params=params,
            headers=canonical_event_service_headers(uid),
        )
    response.raise_for_status()

    payload = response.json()
    events = payload if isinstance(payload, list) else payload.get("events") or payload.get("timeline") or []
    normalized = [annotate_event_time(event, tz_name=user_timezone) for event in events if isinstance(event, dict)]
    if before_dt:
        normalized = [
            event
            for event in normalized
            if (event_dt := _parse_iso(_event_time(event))) is not None and event_dt < before_dt
        ]
    return normalized[-effective_limit:]


def format_canonical_context(
    events: list[dict[str, Any]],
    *,
    max_chars: int = 6000,
    user_timezone: Optional[str] = None,
) -> str:
    if not events:
        return ""
    time_context = build_time_context(user_timezone)
    tz_name = time_context["user_timezone"]
    lines = [
        "Recent canonical timeline context, oldest to newest.",
        (
            f"Current user-local time: {time_context['now_local']} "
            f"({tz_name}); current UTC: {time_context['now_utc']}."
        ),
        "Each event includes user-local time first, then canonical UTC.",
    ]
    for event in events:
        if not event.get("started_at_local"):
            event = annotate_event_time(event, tz_name=user_timezone)
        timestamp = _event_time(event)
        local = event.get("started_at_local") or timestamp
        relative = event.get("relative_to_now") or ""
        channel = str(event.get("channel") or "unknown")
        title = _event_title(event)
        text = _event_text(event)
        prefix = f"- {local} ({tz_name}; UTC {timestamp}; {relative}) [{channel}] {title}"
        if text:
            lines.append(f"{prefix}: {text}")
        else:
            lines.append(prefix)
    context = "\n".join(lines)
    if len(context) <= max_chars:
        return context
    return context[-max_chars:]


def canonical_events_to_chat_turns(events: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for event in list(events)[-limit:]:
        content = _event_text(event, max_chars=500)
        if not content:
            continue
        turns.append({"role": _role_for_event(event), "content": content})
    turns.reverse()
    return turns


def canonical_events_to_server_messages(events: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in reversed(list(events)[-limit:]):
        messages.append(
            {
                "id": str(event.get("event_id") or event.get("id") or ""),
                "created_at": _event_time(event),
                "text": _event_text(event, max_chars=2000),
                "sender": "ai" if _role_for_event(event) == "assistant" else "human",
                "type": "text",
                "plugin_id": None,
                "from_integration": False,
                "memories": [],
                "files": [],
                "metadata": {
                    "source": "canonical_timeline",
                    "channel": event.get("channel"),
                    "provider": event.get("provider"),
                    "source_identity": event.get("source_identity"),
                    "title": _event_title(event),
                },
            }
        )
    return messages
