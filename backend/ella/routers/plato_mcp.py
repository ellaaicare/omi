"""
Authenticated read-only MCP bridge for Plato/Hermes.

This endpoint is intentionally separate from the upstream OMI MCP endpoint.
It is scoped to one configured Plato profile, exposes a small read-only tool
surface for Grok custom MCP connector tests, and can be disabled by removing
the bearer token from the runtime environment.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import database.conversations as conversations_db
import database.memories as memories_db
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services import proposal_ingest
from ella.services.mcp_identity import validate_mcp_session_token
from ella.services.mcp_startup import build_startup_context
from ella.services.mcp_surface_prompt import build_surface_prompt
from utils.ella.time_context import annotate_event_time, build_time_context, local_time_fields, timezone_name

logger = logging.getLogger("ella.plato_mcp")

router = APIRouter(prefix="/v1/ella/plato", tags=["Ella Plato MCP"])

DEFAULT_PLATO_UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
DEFAULT_TIMELINE_URL = "https://api.ella-ai-care.com/v1/ella/timeline"
DEFAULT_HERMES_GATEWAY_URL = "http://100.76.138.56:8642"
DEFAULT_HERMES_AGENT_ID = "hermes"
DEFAULT_PROVISION_API_URL = "http://100.76.138.56:8200"

MAX_CONTEXT_LIMIT = 50
MAX_WINDOW_CONTEXT_LIMIT = 500
MAX_SEARCH_RESULTS = 20
MAX_WORKSPACE_SEARCH_RESULTS = 10
MAX_PROMPT_CHARS = 4000
MAX_OBSERVATION_CHARS = 8000
MAX_CONSULT_CONTEXT_CHARS = 7000
RATE_LIMIT_WINDOW_SECONDS = 60
SALIENCE_RANKS = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEARCH_STOPWORDS = {
    "about",
    "after",
    "before",
    "capture",
    "captured",
    "conversation",
    "conversations",
    "did",
    "earlier",
    "evening",
    "happen",
    "happened",
    "happens",
    "latest",
    "last",
    "morning",
    "necklace",
    "omi",
    "that",
    "this",
    "today",
    "what",
    "when",
    "where",
    "with",
}
COMPANION_PROPOSAL_TYPES = {
    "scanner_rule_change",
    "reminder_request",
    "profile_update",
    "memory_note",
    "summary_correction",
}
OBSERVATION_CHANNELS = {
    "companion_observation",
    "grok_insight",
    "grok_conversation",
    "companion_idea",
    "companion_note",
    "companion_summary",
}
_canonical_store = PostgresCanonicalEventStore()

_active_sessions: dict[str, "MCPSession"] = {}
_rate_limits: dict[str, deque[float]] = defaultdict(deque)


@dataclass
class MCPSession:
    session_id: str
    token_fingerprint: str
    created_at: datetime
    initialized: bool = False
    sse_queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)


class ToolExecutionError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.message = message
        self.code = code


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _plato_uid() -> str:
    return _env("ELLA_PLATO_MCP_UID", _env("ELLA_PLATO_UID", DEFAULT_PLATO_UID))


def _plato_canonical_identity() -> str:
    return _env("ELLA_PLATO_CANONICAL_IDENTITY", _plato_uid())


def _plato_agent_id() -> str:
    return _env("ELLA_PLATO_AGENT_ID", f"ella-omi-{_plato_uid().lower()}")


def _plato_timezone() -> str:
    return timezone_name(_env("ELLA_PLATO_TIMEZONE", _env("ELLA_USER_TIMEZONE", "")))


def _argument_timezone(arguments: dict[str, Any]) -> str:
    raw = str(arguments.get("timezone") or arguments.get("local_time_zone") or _plato_timezone()).strip()
    aliases = {
        "PT": "America/Los_Angeles",
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "PACIFIC": "America/Los_Angeles",
        "PACIFIC TIME": "America/Los_Angeles",
    }
    return timezone_name(aliases.get(raw.upper(), raw))


def _oauth_client_id() -> str:
    return _env("ELLA_PLATO_MCP_OAUTH_CLIENT_ID", "plato-grok")


def _allowed_tokens() -> set[str]:
    raw = _env("ELLA_PLATO_MCP_TOKENS", _env("ELLA_PLATO_MCP_TOKEN", ""))
    return {token.strip() for token in raw.split(",") if token.strip()}


def _token_from_authorization(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    authorization = authorization.strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


_WWW_AUTHENTICATE = (
    'Bearer resource_metadata="https://api.ella-ai-care.com/.well-known/oauth-protected-resource",'
    ' scope="context:read memory:read observations:write profile:read startup:read timeline:read tools:read"'
)


def _authenticate(authorization: Optional[str]) -> str:
    token = _token_from_authorization(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing Plato MCP bearer token",
            headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
        )

    tokens = _allowed_tokens()
    if token in tokens:
        _check_rate_limit(token)
        return _fingerprint(token)

    try:
        claims = validate_mcp_session_token(token)
    except ValueError as exc:
        if not tokens and "not configured" in str(exc):
            raise HTTPException(status_code=503, detail="Plato MCP token is not configured") from exc
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing Plato MCP bearer token",
            headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
        ) from exc

    if "tools:read" not in set(claims.get("scopes") or []):
        raise HTTPException(
            status_code=403,
            detail="MCP bearer token is missing tools:read scope",
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope",'
                    ' scope="tools:read",'
                    ' resource_metadata="https://api.ella-ai-care.com/.well-known/oauth-protected-resource"'
                )
            },
        )
    _check_rate_limit(token)
    return _fingerprint(token)


def _check_rate_limit(token: str) -> None:
    limit = int(_env("ELLA_PLATO_MCP_RATE_LIMIT_PER_MINUTE", "60"))
    if limit <= 0:
        return
    now = time.monotonic()
    fingerprint = _fingerprint(token)
    bucket = _rate_limits[fingerprint]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="Plato MCP token rate limit exceeded")
    bucket.append(now)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolExecutionError(f"Invalid datetime: {value}", code=-32602) from exc
    else:
        raise ToolExecutionError(f"Invalid datetime: {value}", code=-32602)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_duration_seconds(value: Any, default_seconds: int = 30 * 60) -> int:
    if value is None:
        return default_seconds
    if isinstance(value, (int, float)):
        return max(60, min(7 * 24 * 60 * 60, int(value)))
    text = str(value or "").strip().lower().replace("_", " ")
    if not text:
        return default_seconds
    normalized = re.sub(r"\s+", " ", text)
    patterns = [
        r"^(?:last\s+)?(\d+)\s*(minute|minutes|min|mins|m)$",
        r"^(?:last\s+)?(\d+)\s*(hour|hours|hr|hrs|h)$",
        r"^(?:last\s+)?(\d+)\s*(day|days|d)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, normalized)
        if not match:
            continue
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith(("minute", "min", "m")):
            seconds = amount * 60
        elif unit.startswith(("hour", "hr", "h")):
            seconds = amount * 60 * 60
        else:
            seconds = amount * 24 * 60 * 60
        return max(60, min(7 * 24 * 60 * 60, seconds))
    raise ToolExecutionError(f"Invalid time_range: {value}", code=-32602)


def _parse_local_date_window(arguments: dict[str, Any], tz_name: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """Parse explicit local-date / part-of-day arguments into UTC bounds."""
    local_date = str(arguments.get("local_date") or arguments.get("date") or "").strip()
    if not local_date:
        return None, None
    try:
        date_value = datetime.fromisoformat(local_date).date()
    except ValueError as exc:
        raise ToolExecutionError(f"Invalid local_date: {local_date}", code=-32602) from exc

    tz = ZoneInfo(tz_name)
    part = str(arguments.get("part_of_day") or arguments.get("day_part") or "day").strip().lower()
    ranges = {
        "day": (0, 24),
        "today": (0, 24),
        "morning": (5, 12),
        "afternoon": (12, 17),
        "evening": (17, 22),
        "night": (22, 24),
    }
    if part in {"overnight", "early_morning"}:
        start_hour, end_hour = 0, 5
    elif part in ranges:
        start_hour, end_hour = ranges[part]
    else:
        raise ToolExecutionError(f"Invalid part_of_day: {part}", code=-32602)
    since_local = datetime(date_value.year, date_value.month, date_value.day, start_hour, tzinfo=tz)
    until_local = (
        datetime(date_value.year, date_value.month, date_value.day, 0, tzinfo=tz) + timedelta(days=1)
        if end_hour == 24
        else datetime(date_value.year, date_value.month, date_value.day, end_hour, tzinfo=tz)
    )
    return since_local.astimezone(timezone.utc), until_local.astimezone(timezone.utc)


def _infer_query_time_window(query: str, tz_name: str) -> tuple[Optional[datetime], Optional[datetime], str]:
    """Infer a broad local time window for natural-language temporal recall queries."""
    text = query.lower()
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    start_of_day = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    if "this morning" in text or re.search(r"\bmorning\b", text):
        return (
            (start_of_day + timedelta(hours=5)).astimezone(timezone.utc),
            (start_of_day + timedelta(hours=12)).astimezone(timezone.utc),
            "morning",
        )
    if "this afternoon" in text or re.search(r"\bafternoon\b", text):
        return (
            (start_of_day + timedelta(hours=12)).astimezone(timezone.utc),
            (start_of_day + timedelta(hours=17)).astimezone(timezone.utc),
            "afternoon",
        )
    if "this evening" in text or re.search(r"\bevening\b", text):
        return (
            (start_of_day + timedelta(hours=17)).astimezone(timezone.utc),
            (start_of_day + timedelta(hours=22)).astimezone(timezone.utc),
            "evening",
        )
    if re.search(r"\btoday\b", text) or "latest omi" in text or "omi conversation" in text:
        return start_of_day.astimezone(timezone.utc), now_local.astimezone(timezone.utc), "today"
    return None, None, ""


def _event_datetime(item: dict[str, Any], *keys: str) -> Optional[datetime]:
    for key in keys:
        try:
            parsed = _parse_iso_datetime(item.get(key))
        except ToolExecutionError:
            parsed = None
        if parsed is not None:
            return parsed
    return None


def _event_salience(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    signal = metadata.get("ella_signal") if isinstance(metadata, dict) else {}
    salience = ""
    if isinstance(signal, dict):
        salience = str(signal.get("salience") or "").strip().lower()
    tags = metadata.get("ella_tags") if isinstance(metadata, dict) else []
    if not salience and isinstance(tags, list) and "low_signal" in tags:
        salience = "low"
    if salience not in SALIENCE_RANKS:
        salience = "medium"
    return salience


def _meets_min_salience(item: dict[str, Any], min_salience: str) -> bool:
    min_rank = SALIENCE_RANKS.get(min_salience, SALIENCE_RANKS["medium"])
    return SALIENCE_RANKS.get(_event_salience(item), SALIENCE_RANKS["medium"]) >= min_rank


def _event_duration_seconds(item: dict[str, Any]) -> Optional[int]:
    start = _event_datetime(item, "started_at", "created_at", "timestamp")
    end = _event_datetime(item, "ended_at", "finished_at")
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _is_low_salience_fragment(item: dict[str, Any]) -> bool:
    salience = _event_salience(item)
    if salience != "low":
        return False
    duration = _event_duration_seconds(item)
    text = _compact_text(item.get("text") or item.get("overview") or item.get("summary") or "", 5000)
    metadata = item.get("metadata") or {}
    segment_count = metadata.get("segment_count") if isinstance(metadata, dict) else None
    title = str(item.get("title") or "").lower()
    if duration is not None and duration >= 120:
        return False
    if duration is not None and duration <= 30:
        return True
    if len(text) <= 240:
        return True
    if isinstance(segment_count, int) and segment_count <= 2 and "brief" in title:
        return True
    return False


def _is_temporal_low_value_fragment(item: dict[str, Any]) -> bool:
    """More aggressive fragment filter for broad 'what happened this morning' recall."""
    if _is_low_salience_fragment(item):
        return True
    title = str(item.get("title") or "").lower()
    text = _compact_text(item.get("text") or item.get("overview") or item.get("summary") or "", 500)
    metadata = item.get("metadata") or {}
    tags = metadata.get("ella_tags") if isinstance(metadata, dict) else []
    if isinstance(tags, list) and "low_signal" in tags:
        return True
    if "brief" in title or "fragment" in title or "utterance" in title:
        return True
    if len(text) <= 180:
        return True
    return False


def _event_overlaps_window(item: dict[str, Any], since: datetime, until: datetime) -> bool:
    start = _event_datetime(item, "started_at", "created_at", "timestamp")
    end = _event_datetime(item, "ended_at", "finished_at") or start
    if start is None or end is None:
        return False
    return end >= since and start <= until


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _compact_text(value: Any, limit: int = 1200) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _event_display_title(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    if title:
        return title
    metadata = item.get("metadata") or {}
    structured = metadata.get("structured") if isinstance(metadata, dict) else {}
    if isinstance(structured, dict) and structured.get("title"):
        return str(structured["title"]).strip()
    text = str(item.get("text") or "").strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line:
        return _compact_text(first_line, 120)
    return "OMI conversation"


def _event_time(item: dict[str, Any]) -> str:
    return str(
        item.get("started_at") or item.get("finished_at") or item.get("created_at") or item.get("timestamp") or ""
    )


def _include_omi_channel(channels: list[str]) -> bool:
    normalized = {str(channel).strip().lower() for channel in channels}
    return not normalized or bool(normalized & {"omi", "omi_transcript", "omi_summary", "omi_conversation"})


def _event_identity(item: dict[str, Any]) -> str:
    source_ref = item.get("source_ref") or {}
    if isinstance(source_ref, dict):
        for key in ("conversation_id", "event_id", "id", "source_identity"):
            if source_ref.get(key):
                return f"source_ref:{key}:{source_ref[key]}"
    for key in ("event_id", "id", "source_identity"):
        if item.get(key):
            return f"{key}:{item[key]}"
    return f"{item.get('channel')}:{_event_time(item)}:{item.get('title')}:{item.get('text')}"


def _merge_chronological_events(*event_lists: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for events in event_lists:
        for event in events:
            if not isinstance(event, dict):
                continue
            merged.setdefault(_event_identity(event), event)
    ordered = list(merged.values())
    ordered.sort(key=_event_time, reverse=True)
    return ordered[:limit]


def _conversation_to_event(conversation: dict[str, Any]) -> dict[str, Any]:
    structured = conversation.get("structured") or {}
    title = structured.get("title") or conversation.get("title") or "OMI conversation"
    overview = structured.get("overview") or conversation.get("overview") or ""
    return annotate_event_time(
        {
            "event_id": conversation.get("id"),
            "channel": "omi",
            "provider": "omi-backend",
            "role": "user",
            "started_at": _json_safe(conversation.get("started_at") or conversation.get("created_at")),
            "ended_at": _json_safe(conversation.get("finished_at")),
            "title": title,
            "text": _compact_text(overview, 1600),
            "source_ref": {"conversation_id": conversation.get("id")},
        },
        tz_name=_plato_timezone(),
    )


def _memory_to_event(memory: dict[str, Any]) -> dict[str, Any]:
    return annotate_event_time(
        {
            "event_id": memory.get("id"),
            "channel": "memory",
            "provider": "omi-backend",
            "role": "memory",
            "started_at": _json_safe(memory.get("created_at")),
            "title": memory.get("category") or "memory",
            "text": _compact_text(memory.get("content"), 1200),
            "source_ref": {"memory_id": memory.get("id")},
        },
        tz_name=_plato_timezone(),
    )


async def _fetch_canonical_timeline(limit: int, channels: list[str], since: Optional[str]) -> list[dict[str, Any]]:
    timeline_url = _env("ELLA_PLATO_TIMELINE_URL", DEFAULT_TIMELINE_URL)
    params: dict[str, Any] = {"uid": _plato_uid(), "limit": limit}
    if channels:
        params["channels"] = ",".join(channels)
    if since:
        params["since"] = since
    params["timezone"] = _plato_timezone()
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(timeline_url, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"timeline_http_{response.status_code}")
    payload = response.json()
    events = payload if isinstance(payload, list) else payload.get("events") or payload.get("timeline") or []
    return [
        annotate_event_time(_json_safe(event), tz_name=_plato_timezone()) for event in events if isinstance(event, dict)
    ]


def _fallback_recent_context(limit: int, channels: list[str], since: Optional[str]) -> list[dict[str, Any]]:
    since_dt = _parse_iso_datetime(since)
    events: list[dict[str, Any]] = []
    include_omi = not channels or "omi" in channels or "omi_transcript" in channels
    include_memory = not channels or "memory" in channels or "memories" in channels
    if include_omi:
        conversations = conversations_db.get_conversations(
            _plato_uid(),
            limit=limit,
            offset=0,
            include_discarded=False,
            statuses=["completed"],
            start_date=since_dt,
        )
        events.extend(_conversation_to_event(conv) for conv in conversations)
    if include_memory and len(events) < limit:
        memories = memories_db.get_memories(_plato_uid(), limit=limit, offset=0, start_date=since_dt)
        events.extend(_memory_to_event(memory) for memory in memories)
    events.sort(key=_event_time, reverse=True)
    return events[:limit]


async def _recent_context(arguments: dict[str, Any]) -> dict[str, Any]:
    max_limit = MAX_WINDOW_CONTEXT_LIMIT if arguments.get("_allow_large_window") else MAX_CONTEXT_LIMIT
    limit = _clamp_int(arguments.get("limit"), 10, 1, max_limit)
    raw_channels = arguments.get("channels") or []
    channels = (
        [str(item).strip() for item in raw_channels if str(item).strip()] if isinstance(raw_channels, list) else []
    )
    since = arguments.get("since")
    try:
        events = await _fetch_canonical_timeline(limit, channels, since)
        source = "canonical_timeline"
        if _include_omi_channel(channels):
            # The canonical ledger is live, but OMI enriched summaries are not
            # yet guaranteed to be written there. Until OMI ingestion/backfill is
            # complete, merge the same enriched OMI conversations the app shows.
            fallback_events = _fallback_recent_context(limit, ["omi"], since)
            if fallback_events:
                if events:
                    events = _merge_chronological_events(events, fallback_events, limit=limit)
                    source = "canonical_timeline_with_omi_firestore_fallback"
                else:
                    events = fallback_events[:limit]
                    source = "canonical_timeline_empty_omi_firestore_fallback"
        elif not events:
            fallback_events = _fallback_recent_context(limit, channels, since)
            if fallback_events:
                events = fallback_events[:limit]
                source = "canonical_timeline_empty_firestore_fallback"
    except Exception as exc:
        logger.warning("plato_mcp timeline fallback: %s", exc)
        events = _fallback_recent_context(limit, channels, since)
        source = "omi_firestore_fallback"
    return {
        "uid": _plato_uid(),
        "canonical_identity": _plato_canonical_identity(),
        "source": source,
        "time_context": build_time_context(_plato_timezone()),
        "events": events[:limit],
    }


async def _latest_omi(arguments: dict[str, Any]) -> dict[str, Any]:
    context = await _recent_context(
        {"limit": _clamp_int(arguments.get("limit"), 10, 1, MAX_CONTEXT_LIMIT), "channels": ["omi"]}
    )
    events = [event for event in context.get("events", []) if str(event.get("channel", "")).startswith("omi")]
    latest = events[0] if events else None
    return {
        "latest": latest,
        "source": context.get("source"),
        "uid": _plato_uid(),
        "time_context": context.get("time_context") or build_time_context(_plato_timezone()),
    }


async def _omi_activity_window(arguments: dict[str, Any]) -> dict[str, Any]:
    tz_name = _argument_timezone(arguments)
    until = _parse_iso_datetime(arguments.get("until")) or datetime.now(timezone.utc)
    since = _parse_iso_datetime(arguments.get("since"))
    duration_seconds = None
    local_since, local_until = _parse_local_date_window(arguments, tz_name)
    if local_since is not None and local_until is not None:
        since = local_since
        until = local_until
    elif since is None:
        duration_seconds = _parse_duration_seconds(arguments.get("time_range"), default_seconds=30 * 60)
        since = until - timedelta(seconds=duration_seconds)
    limit = _clamp_int(arguments.get("limit"), MAX_CONTEXT_LIMIT, 1, MAX_WINDOW_CONTEXT_LIMIT)

    # Pull enough source rows for the requested local-time window instead of
    # only the last few fragments. Include a small lookback buffer so a
    # multi-minute conversation that began before the window can still overlap.
    fetch_since = since - timedelta(hours=2)
    context = await _recent_context(
        {
            "limit": max(limit, 200),
            "channels": ["omi"],
            "since": fetch_since.isoformat().replace("+00:00", "Z"),
            "_allow_large_window": True,
        }
    )
    events = [
        annotate_event_time(dict(event), tz_name=tz_name)
        for event in context.get("events", [])
        if isinstance(event, dict) and str(event.get("channel", "")).startswith("omi")
    ]
    window_events = [event for event in events if _event_overlaps_window(event, since, until)]

    meaningful: list[dict[str, Any]] = []
    low_salience_fragments: list[dict[str, Any]] = []
    for event in window_events:
        item = dict(event)
        item["title"] = _event_display_title(item)
        end_fields = local_time_fields(item.get("ended_at") or item.get("finished_at"), tz_name=tz_name, now=until)
        if end_fields["utc"]:
            item["ended_at_utc"] = end_fields["utc"]
            item["ended_at_local"] = end_fields["local"]
            item["ended_at_local_date"] = end_fields["local_date"]
            item["ended_at_local_time"] = end_fields["local_time"]
            item["ended_relative_to_now"] = end_fields["relative_to_now"]
        item["salience"] = _event_salience(item)
        item["duration_seconds"] = _event_duration_seconds(item)
        item["is_low_salience_fragment"] = _is_low_salience_fragment(item)
        if item["is_low_salience_fragment"]:
            low_salience_fragments.append(item)
        else:
            meaningful.append(item)

    return {
        "uid": _plato_uid(),
        "canonical_identity": _plato_canonical_identity(),
        "source": context.get("source"),
        "time_context": build_time_context(tz_name, now=until),
        "window": {
            "since_utc": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "until_utc": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "since_local": since.astimezone(ZoneInfo(tz_name)).isoformat(),
            "until_local": until.astimezone(ZoneInfo(tz_name)).isoformat(),
            "timezone": tz_name,
            "time_range_seconds": duration_seconds,
        },
        "counts": {
            "window_events": len(window_events),
            "meaningful_moments": len(meaningful),
            "low_salience_fragments": len(low_salience_fragments),
        },
        "meaningful_moments": meaningful,
        "low_salience_fragments": (
            low_salience_fragments if arguments.get("include_fragments", True) is not False else []
        ),
    }


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", text.lower()) if token not in SEARCH_STOPWORDS
    }


def _score_item(query_tokens: set[str], item: dict[str, Any]) -> int:
    haystack = " ".join(str(item.get(key) or "") for key in ("title", "text", "summary", "overview"))
    return len(query_tokens & _tokens(haystack))


def _workspace_search_result_to_event(result: dict[str, Any]) -> dict[str, Any]:
    file_name = str(result.get("file") or "")
    line = result.get("line")
    event = {
        "event_id": f"hermes-workspace:{file_name}:{line}",
        "channel": "hermes_workspace",
        "provider": "hermes-provision-search",
        "role": "memory",
        "started_at": result.get("started_at") or "",
        "title": result.get("title") or "Hermes workspace match",
        "text": _compact_text(result.get("excerpt"), 1800),
        "source_ref": {
            "source": "hermes_workspace",
            "file": file_name,
            "line": line,
        },
        "score": result.get("score"),
        "matched_terms": result.get("matched_terms") or [],
    }
    return annotate_event_time(event, tz_name=_plato_timezone())


async def _fetch_workspace_search(query: str, max_results: int) -> list[dict[str, Any]]:
    provision_token = _env("ELLA_PROVISION_API_TOKEN")
    if not provision_token:
        logger.warning("plato_mcp workspace search skipped: ELLA_PROVISION_API_TOKEN is not configured")
        return []
    provision_url = _env("ELLA_PROVISION_API_URL", DEFAULT_PROVISION_API_URL).rstrip("/")
    limit = max(1, min(max_results, MAX_WORKSPACE_SEARCH_RESULTS))
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{provision_url}/workspace/{_plato_agent_id()}/search",
                headers={"Authorization": f"Bearer {provision_token}"},
                json={"query": query, "limit": limit, "excerpt_chars": 1400},
            )
        if response.status_code != 200:
            logger.warning("plato_mcp workspace search HTTP %s: %s", response.status_code, response.text[:300])
            return []
        payload = response.json()
        return [
            _workspace_search_result_to_event(item) for item in (payload.get("results") or []) if isinstance(item, dict)
        ]
    except Exception as exc:
        logger.warning("plato_mcp workspace search failed: %s", exc)
        return []


def _format_consult_context(context: dict[str, Any], limit: int) -> str:
    lines = [
        f"source={context.get('source') or 'unknown'}",
        f"uid={context.get('uid') or _plato_uid()}",
    ]
    for event in (context.get("events") or [])[:limit]:
        timestamp = event.get("started_at") or event.get("created_at") or event.get("timestamp") or ""
        local = event.get("started_at_local") or timestamp
        relative = event.get("relative_to_now") or ""
        channel = event.get("channel") or ""
        title = event.get("title") or "Untitled"
        text = _compact_text(event.get("text") or event.get("overview") or event.get("summary") or "", 700)
        lines.append(
            f"- {local} ({event.get('started_at_timezone') or _plato_timezone()}; UTC {timestamp}; {relative}) [{channel}] {title}: {text}"
        )
    return _compact_text("\n".join(lines), MAX_CONSULT_CONTEXT_CHARS)


async def _search_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    query = _compact_text(arguments.get("query"), 500)
    if not query:
        raise ToolExecutionError("query is required", code=-32602)
    max_results = _clamp_int(arguments.get("max_results"), 5, 1, MAX_SEARCH_RESULTS)
    tz_name = _plato_timezone()
    inferred_since, inferred_until, inferred_label = _infer_query_time_window(query, tz_name)
    since = arguments.get("since") or (
        inferred_since.isoformat().replace("+00:00", "Z") if inferred_since is not None else None
    )
    context = await _recent_context(
        {
            "limit": max(max_results * 12, 80) if inferred_since else max(max_results * 5, 25),
            "channels": arguments.get("channels") or [],
            "since": since,
            "_allow_large_window": bool(inferred_since),
        }
    )
    events = context.get("events", [])
    if inferred_since is not None and inferred_until is not None:
        events = [event for event in events if _event_overlaps_window(event, inferred_since, inferred_until)]
    query_tokens = _tokens(query)
    ranked = [(score, idx, item) for idx, item in enumerate(events) if (score := _score_item(query_tokens, item)) > 0]
    ranked.sort(key=lambda row: (row[0], -row[1]), reverse=True)
    results = [item for _score, _idx, item in ranked[:max_results]]
    if inferred_since is not None and not results:
        meaningful_events = [item for item in events if not _is_temporal_low_value_fragment(item)]
        results = (meaningful_events or list(events))[-max_results:]
    workspace_results = await _fetch_workspace_search(query, max_results)
    if workspace_results:
        seen = {_event_identity(item) for item in workspace_results}
        results = (workspace_results + [item for item in results if _event_identity(item) not in seen])[:max_results]
    return {
        "query": query,
        "source": (
            f"{context.get('source')}_with_hermes_workspace_search" if workspace_results else context.get("source")
        ),
        "inferred_time_window": inferred_label or None,
        "time_context": context.get("time_context") or build_time_context(_plato_timezone()),
        "results": results,
    }


async def _scanner_rules(arguments: dict[str, Any]) -> dict[str, Any]:
    provision_token = _env("ELLA_PROVISION_API_TOKEN")
    if not provision_token:
        raise ToolExecutionError("ELLA_PROVISION_API_TOKEN is not configured", code=-32003)
    provision_url = _env("ELLA_PROVISION_API_URL", DEFAULT_PROVISION_API_URL).rstrip("/")
    files = arguments.get("files") or ["scanner-presets.md", "scanner-tuning.md"]
    if not isinstance(files, list):
        raise ToolExecutionError("files must be a list", code=-32602)
    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for filename in files[:4]:
            safe_name = str(filename).strip()
            if not re.fullmatch(r"[A-Za-z0-9._/-]+", safe_name) or ".." in safe_name:
                raise ToolExecutionError(f"Invalid scanner rules file: {safe_name}", code=-32602)
            response = await client.get(
                f"{provision_url}/workspace/{_plato_agent_id()}/files/{safe_name}",
                headers={"Authorization": f"Bearer {provision_token}"},
            )
            if response.status_code == 404:
                results.append({"file": safe_name, "found": False})
                continue
            if response.status_code != 200:
                raise ToolExecutionError(f"Provision API returned HTTP {response.status_code}", code=-32004)
            payload = response.json()
            content = payload.get("content") if isinstance(payload, dict) else response.text
            results.append({"file": safe_name, "found": True, "content": _compact_text(content, 5000)})
    return {"agent_id": _plato_agent_id(), "files": results}


async def _consult_plato(arguments: dict[str, Any]) -> dict[str, Any]:
    prompt = _compact_text(arguments.get("prompt"), MAX_PROMPT_CHARS)
    if not prompt:
        raise ToolExecutionError("prompt is required", code=-32602)
    mode = str(arguments.get("mode") or "brief")
    if mode not in {"brief", "normal", "deep"}:
        raise ToolExecutionError("mode must be one of: brief, normal, deep", code=-32602)
    context_limit = _clamp_int(arguments.get("context_limit"), 15, 1, MAX_CONTEXT_LIMIT)
    context = await _recent_context({"limit": context_limit})
    context_block = _format_consult_context(context, context_limit)
    workspace_results = await _fetch_workspace_search(prompt, min(5, context_limit))
    if workspace_results:
        workspace_context = _format_consult_context(
            {"source": "hermes_workspace_search", "uid": _plato_uid(), "events": workspace_results},
            len(workspace_results),
        )
        context_block = f"{context_block}\n\nDeep Hermes workspace search:\n{workspace_context}"
    token = _env("HERMES_API_SERVER_KEY", _env("API_SERVER_KEY", ""))
    if not token:
        raise ToolExecutionError("HERMES_API_SERVER_KEY is not configured", code=-32003)
    gateway_url = _env("HERMES_GATEWAY_URL", DEFAULT_HERMES_GATEWAY_URL).rstrip("/")
    agent_id = _env("HERMES_AGENT_ID", DEFAULT_HERMES_AGENT_ID)
    session_key = _env("ELLA_PLATO_MCP_HERMES_SESSION", f"grok-mcp:plato:{_plato_uid().lower()}")
    system = (
        "You are serving a read-only external MCP consult for Plato. "
        "Use the supplied current MCP context as the freshest available evidence, "
        "then use Hermes memory only to fill gaps. "
        "If current MCP context conflicts with older memory, prefer current MCP context. "
        "Do not expose internal secrets, filesystem paths, tokens, or caregiver escalation controls."
    )
    prompt = f"Current MCP context:\n{context_block}\n\nUser request:\n{prompt}"
    if mode == "brief":
        prompt = f"{prompt}\n\nAnswer in 1-3 concise sentences."
    elif mode == "deep":
        prompt = f"{prompt}\n\nUse relevant chronology and cite uncertainty clearly."
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{gateway_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Hermes-Session-Id": session_key,
            },
            json={
                "model": f"openclaw:{agent_id}",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "stream": False,
            },
        )
    if response.status_code != 200:
        raise ToolExecutionError(f"Hermes returned HTTP {response.status_code}", code=-32005)
    payload = response.json()
    choices = payload.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    return {
        "answer": text,
        "mode": mode,
        "agent_id": agent_id,
        "session": session_key,
        "context_source": (
            f"{context.get('source')}_with_hermes_workspace_search" if workspace_results else context.get("source")
        ),
        "context_events": len(context.get("events") or []) + len(workspace_results),
    }


def _proposal_write_enabled() -> bool:
    return _env_bool("ELLA_PLATO_MCP_ENABLE_PROPOSALS", False)


def _legacy_plato_onboarding() -> dict[str, Any]:
    scopes = [
        "context:read",
        "memory:read",
        "profile:read",
        "startup:read",
        "timeline:read",
        "tools:read",
        "observations:write",
    ]
    tools = [
        "companion_start_here",
        "companion_surface_prompt",
        "companion_submit_observation",
        "plato_recent_context",
        "plato_search_memory",
        "plato_latest_omi",
        "plato_omi_activity_window",
        "plato_consult",
    ]
    if _proposal_write_enabled():
        scopes.extend(["proposals:read", "proposals:write"])
        tools.extend(["companion_propose_change", "companion_get_proposal_status"])
    return {
        "state": "authenticated_mapped",
        "trace_id": str(uuid.uuid4()),
        "selected_profile": {
            "profile_uid": _plato_uid(),
            "role": "self",
            "profile_label": _env("ELLA_MCP_DEFAULT_PROFILE_LABEL", "Plato"),
            "scopes": scopes,
            "allowed_tools": tools,
        },
        "available_profiles": [],
        "session_claims": {
            "profile_uid": _plato_uid(),
            "role": "self",
            "scopes": scopes,
            "allowed_tools": tools,
            "grant_id": "legacy-plato-mcp",
            "external_provider": "static_bearer",
        },
    }


async def _companion_start_here(arguments: dict[str, Any]) -> dict[str, Any]:
    channels = arguments.get("channels") or []
    if channels and not isinstance(channels, list):
        raise ToolExecutionError("channels must be a list", code=-32602)
    return await build_startup_context(
        onboarding=_legacy_plato_onboarding(),
        limit=_clamp_int(arguments.get("limit"), 12, 1, MAX_CONTEXT_LIMIT),
        channels=[str(channel).strip() for channel in channels if str(channel).strip()],
    )


async def _companion_surface_prompt(arguments: dict[str, Any]) -> dict[str, Any]:
    onboarding = _legacy_plato_onboarding()
    selected = onboarding["selected_profile"]
    claims = onboarding["session_claims"]
    return build_surface_prompt(
        profile_uid=str(selected.get("profile_uid") or _plato_uid()),
        profile_label=str(selected.get("profile_label") or "Plato"),
        surface=_compact_text(arguments.get("surface") or "generic", 80),
        scopes=[str(item) for item in (claims.get("scopes") or [])],
        allowed_tools=[str(item) for item in (claims.get("allowed_tools") or [])],
        proposal_write_enabled=_proposal_write_enabled(),
    )


async def _companion_get_proposal_status(arguments: dict[str, Any]) -> dict[str, Any]:
    proposal_id = _compact_text(arguments.get("proposal_id"), 160)
    if not proposal_id:
        raise ToolExecutionError("proposal_id is required", code=-32602)
    try:
        return proposal_ingest.get_proposal_status(
            session_claims=_legacy_plato_onboarding()["session_claims"],
            proposal_id=proposal_id,
        )
    except proposal_ingest.ProposalIngestError as exc:
        raise ToolExecutionError(str(exc), code=-32004) from exc


async def _companion_propose_change(arguments: dict[str, Any]) -> dict[str, Any]:
    proposal_type = str(arguments.get("proposal_type") or "").strip()
    if proposal_type not in COMPANION_PROPOSAL_TYPES:
        allowed = ", ".join(sorted(COMPANION_PROPOSAL_TYPES))
        raise ToolExecutionError(f"proposal_type must be one of: {allowed}", code=-32602)
    title = _compact_text(arguments.get("title"), 240)
    description = _compact_text(arguments.get("description"), 4000)
    if not title:
        raise ToolExecutionError("title is required", code=-32602)
    if not description:
        raise ToolExecutionError("description is required", code=-32602)
    evidence = arguments.get("evidence") or []
    if evidence and not isinstance(evidence, list):
        raise ToolExecutionError("evidence must be a list", code=-32602)
    target = arguments.get("target") or {}
    requested_change = arguments.get("requested_change") or {}
    if target and not isinstance(target, dict):
        raise ToolExecutionError("target must be an object", code=-32602)
    if requested_change and not isinstance(requested_change, dict):
        raise ToolExecutionError("requested_change must be an object", code=-32602)
    confidence = arguments.get("confidence")
    parsed_confidence = None
    if confidence is not None:
        try:
            parsed_confidence = max(0.0, min(float(confidence), 1.0))
        except Exception as exc:
            raise ToolExecutionError("confidence must be a number between 0 and 1", code=-32602) from exc
    payload = {
        "title": title,
        "description": description,
        "target": target,
        "evidence": evidence,
        "requested_change": requested_change,
        "source": "plato_mcp",
        "write_policy": "proposal_only",
    }
    if parsed_confidence is not None:
        payload["confidence"] = parsed_confidence
    try:
        return proposal_ingest.create_proposal(
            session_claims=_legacy_plato_onboarding()["session_claims"],
            tool_name="companion_propose_change",
            proposal_type=proposal_type,
            payload=payload,
            idempotency_key=_compact_text(arguments.get("idempotency_key"), 240),
        )
    except proposal_ingest.ProposalIngestError as exc:
        raise ToolExecutionError(str(exc), code=-32004) from exc


def _observation_proposal_claims() -> dict[str, Any]:
    """Internal trusted claims for turning observation writes into memory proposals.

    `companion_submit_observation` is the public write surface exposed to hosted
    companion clients. The legacy proposal tool can stay hidden while this
    trusted internal bridge still uses the existing proposal/apply safety path.
    """
    claims = dict(_legacy_plato_onboarding()["session_claims"])
    scopes = {str(scope) for scope in claims.get("scopes") or [] if str(scope)}
    scopes.add("proposals:write")
    allowed_tools = {str(tool) for tool in claims.get("allowed_tools") or [] if str(tool)}
    allowed_tools.add("companion_propose_change")
    claims["scopes"] = sorted(scopes)
    claims["allowed_tools"] = sorted(allowed_tools)
    claims["trace_id"] = str(claims.get("trace_id") or f"mcp-observation:{uuid.uuid4()}")
    return claims


def _observation_memory_payload(
    *,
    event_id: str,
    channel: str,
    title: str | None,
    text: str,
) -> dict[str, Any]:
    display_title = title or f"MCP observation from {channel}"
    return {
        "title": display_title[:240],
        "description": f"Trusted companion observation submitted through MCP channel `{channel}`.",
        "target": {
            "canonical_identity": _plato_canonical_identity(),
            "uid": _plato_uid(),
            "durable_owner": "observer_memory",
        },
        "evidence": [
            {
                "event_id": event_id,
                "channel": channel,
                "provider": "mcp_companion",
                "mcp_tool": "companion_submit_observation",
            }
        ],
        "requested_change": {
            "memory": text,
            "source_text": text,
            "source_channel": channel,
            "source_title": title or "",
            "category": "mcp_companion_observation",
        },
        "source": "plato_mcp",
        "write_policy": "proposal_only",
        "confidence": 0.92,
    }


async def _companion_submit_observation(arguments: dict[str, Any]) -> dict[str, Any]:
    text = _compact_text(arguments.get("text"), MAX_OBSERVATION_CHARS)
    if not text or len(text.strip()) < 10:
        raise ToolExecutionError("text is required (minimum 10 characters)", code=-32602)
    channel = str(arguments.get("channel") or "companion_observation").strip().lower()
    channel = re.sub(r"[^a-z0-9_]", "_", channel)
    if channel not in OBSERVATION_CHANNELS:
        allowed = ", ".join(sorted(OBSERVATION_CHANNELS))
        raise ToolExecutionError(f"channel must be one of: {allowed}", code=-32602)
    title = _compact_text(arguments.get("title") or "", 240) or None
    event_id = str(arguments.get("idempotency_key") or uuid.uuid4())
    now = datetime.now(timezone.utc)
    event = CanonicalEventIn(
        uid=_plato_uid(),
        canonical_identity=_plato_canonical_identity(),
        event_id=event_id,
        channel=channel,
        provider="mcp_companion",
        role="companion",
        text=text,
        started_at=now,
        ended_at=now,
        privacy_scope="user_private",
        scan_policy="none",
        source_ref={"mcp_tool": "companion_submit_observation"},
        metadata={"title": title} if title else {},
    )
    result = await _canonical_store.write_batch([event])
    try:
        proposal_result = proposal_ingest.create_proposal(
            session_claims=_observation_proposal_claims(),
            tool_name="companion_propose_change",
            proposal_type="memory_note",
            payload=_observation_memory_payload(
                event_id=event_id,
                channel=channel,
                title=title,
                text=text,
            ),
            idempotency_key=f"mcp-observation:{event_id}:memory_note",
        )
    except proposal_ingest.ProposalIngestError as exc:
        raise ToolExecutionError(f"Observation recorded but memory proposal failed: {exc}", code=-32004) from exc
    return {
        "accepted": True,
        "event_id": event_id,
        "channel": channel,
        "inserted": result.get("inserted", 0) > 0,
        "memory_proposal": proposal_result,
        "note": (
            "Observation recorded and submitted as a durable memory proposal. "
            "Observer apply will promote it into recallable memory if accepted."
        ),
    }


_DEPRECATED_TOOLS = {"companion_propose_change", "companion_get_proposal_status"}


def _visible_tools() -> list[dict[str, Any]]:
    allowed = set(_legacy_plato_onboarding()["session_claims"].get("allowed_tools") or [])
    return [
        tool
        for tool in MCP_TOOLS
        if (tool["name"] in allowed or tool["name"] == "plato_get_scanner_rules")
        and tool["name"] not in _DEPRECATED_TOOLS
    ]


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "companion_start_here",
        "description": "Return the safe startup context packet for this authenticated companion profile.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": MAX_CONTEXT_LIMIT},
                "channels": {"type": "array", "items": {"type": "string"}, "default": []},
            },
        },
    },
    {
        "name": "companion_surface_prompt",
        "description": (
            "Return the current hosted-surface bootstrap prompt and tool/writeback policy for this profile. "
            "Use this when configuring Grok, hosted GPTs, Gemini, or other external companion surfaces."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"surface": {"type": "string", "default": "generic"}},
        },
    },
    {
        "name": "companion_get_proposal_status",
        "description": "Return the status of an auditable proposal record for this profile.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
        },
    },
    {
        "name": "companion_propose_change",
        "description": (
            "[DEPRECATED — use companion_submit_observation instead] "
            "Create an auditable proposal only; this never directly changes scanner rules, reminders, profile data, "
            "memory, summaries, caregiver delivery, or other live state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_type": {
                    "type": "string",
                    "enum": sorted(COMPANION_PROPOSAL_TYPES),
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
                "target": {"type": "object", "default": {}},
                "evidence": {"type": "array", "items": {"type": "object"}, "default": []},
                "requested_change": {"type": "object", "default": {}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "idempotency_key": {"type": "string"},
            },
            "required": ["proposal_type", "title", "description"],
        },
    },
    {
        "name": "companion_submit_observation",
        "description": (
            "Submit a free-form observation, idea, conversation summary, or insight. "
            "Just write naturally — Hermes will process it internally using its own intelligence "
            "to extract facts, update the user profile, create memories, or take other appropriate action. "
            "No need to structure the data; just describe what you observed or learned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Free-form text: a conversation summary, idea, insight, observation, "
                        "or anything noteworthy about the user. Write naturally."
                    ),
                },
                "channel": {
                    "type": "string",
                    "enum": sorted(OBSERVATION_CHANNELS),
                    "default": "companion_observation",
                    "description": "Category label for this observation.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional short title or subject line.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional key to prevent duplicate submissions.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "plato_recent_context",
        "description": "Read recent Plato timeline context from canonical events, with OMI Firestore fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": MAX_CONTEXT_LIMIT},
                "channels": {"type": "array", "items": {"type": "string"}, "default": []},
                "since": {"type": "string", "description": "Optional ISO timestamp lower bound."},
            },
        },
    },
    {
        "name": "plato_search_memory",
        "description": "Search recent Plato context for matching timeline or memory snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "since": {"type": "string"},
                "channels": {"type": "array", "items": {"type": "string"}, "default": []},
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
            },
            "required": ["query"],
        },
    },
    {
        "name": "plato_latest_omi",
        "description": "Return the latest indexed OMI/necklace conversation summary for Plato.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": MAX_CONTEXT_LIMIT}},
        },
    },
    {
        "name": "plato_omi_activity_window",
        "description": (
            "Return OMI/necklace conversations in a local-time activity window, split into meaningful moments "
            "and low-salience fragments. Use this for questions like 'what happened in OMI in the last 30 minutes', "
            "'what happened this morning', or 'what did OMI capture today'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string",
                    "default": "30 minutes",
                    "description": "Relative window such as 'last 30 minutes', '2 hours', or '1 day'.",
                },
                "since": {"type": "string", "description": "Optional ISO timestamp lower bound."},
                "until": {"type": "string", "description": "Optional ISO timestamp upper bound; defaults to now."},
                "local_date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD in the user's local timezone for day/part-of-day windows.",
                },
                "part_of_day": {
                    "type": "string",
                    "enum": ["day", "morning", "afternoon", "evening", "night", "overnight"],
                    "description": "Optional local day segment; requires local_date.",
                },
                "timezone": {"type": "string", "default": "America/Los_Angeles"},
                "local_time_zone": {"type": "string", "description": "Alias accepted for timezone, e.g. PDT."},
                "limit": {
                    "type": "integer",
                    "default": MAX_CONTEXT_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_WINDOW_CONTEXT_LIMIT,
                },
                "include_fragments": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "plato_get_scanner_rules",
        "description": "Read Plato scanner rule files from the Hermes/OpenClaw workspace through the provision API.",
        "inputSchema": {
            "type": "object",
            "properties": {"files": {"type": "array", "items": {"type": "string"}, "default": ["scanner-presets.md"]}},
        },
    },
    {
        "name": "plato_consult",
        "description": "Ask the Hermes Plato agent for a constrained read-only answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["brief", "normal", "deep"], "default": "brief"},
                "context_limit": {
                    "type": "integer",
                    "default": 15,
                    "minimum": 1,
                    "maximum": MAX_CONTEXT_LIMIT,
                    "description": "Number of freshest MCP context events to include before consulting Hermes.",
                },
            },
            "required": ["prompt"],
        },
    },
]

_TOOL_HANDLERS = {
    "companion_start_here": _companion_start_here,
    "companion_surface_prompt": _companion_surface_prompt,
    "companion_get_proposal_status": _companion_get_proposal_status,
    "companion_propose_change": _companion_propose_change,
    "companion_submit_observation": _companion_submit_observation,
    "plato_recent_context": _recent_context,
    "plato_search_memory": _search_memory,
    "plato_latest_omi": _latest_omi,
    "plato_omi_activity_window": _omi_activity_window,
    "plato_get_scanner_rules": _scanner_rules,
    "plato_consult": _consult_plato,
}


def _mcp_response(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _mcp_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _argument_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in {"prompt", "query", "correction_text", "fact"}:
            summary[key] = {"chars": len(str(value or ""))}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = {"items": len(value)}
        elif isinstance(value, dict):
            summary[key] = {"keys": sorted(str(k) for k in value.keys())[:10]}
    return summary


def _audit_tool_call(
    *,
    trace_id: str,
    token_fingerprint: str,
    tool_name: str,
    arguments: dict[str, Any],
    started: float,
    status: str,
    error: str = "",
) -> None:
    logger.info(
        "plato_mcp_tool_call %s",
        json.dumps(
            {
                "trace_id": trace_id,
                "caller": "grok_mcp",
                "token_fingerprint": token_fingerprint,
                "uid": _plato_uid(),
                "tool": tool_name,
                "arguments": _argument_summary(arguments),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": status,
                "error": error[:160],
            },
            sort_keys=True,
        ),
    )


async def _handle_mcp_message(
    token_fingerprint: str,
    message: dict[str, Any],
    session: Optional[MCPSession] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        session_id = str(uuid.uuid4())
        _active_sessions[session_id] = MCPSession(
            session_id=session_id,
            token_fingerprint=token_fingerprint,
            created_at=datetime.now(timezone.utc),
            initialized=True,
        )
        return (
            _mcp_response(
                msg_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ella-plato-hermes-mcp", "version": "0.1.0"},
                },
            ),
            session_id,
        )

    if method == "notifications/initialized":
        return None, None

    if method == "tools/list":
        return _mcp_response(msg_id, {"tools": _visible_tools()}), None

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        trace_id = str(uuid.uuid4())
        started = time.monotonic()
        if not isinstance(arguments, dict):
            return _mcp_error(msg_id, -32602, "Tool arguments must be an object"), None
        visible_tool_names = {tool["name"] for tool in _visible_tools()}
        if tool_name not in _TOOL_HANDLERS or tool_name not in visible_tool_names:
            return _mcp_error(msg_id, -32601, f"Unknown tool: {tool_name}"), None
        try:
            result = await _TOOL_HANDLERS[tool_name](arguments)
            _audit_tool_call(
                trace_id=trace_id,
                token_fingerprint=token_fingerprint,
                tool_name=tool_name,
                arguments=arguments,
                started=started,
                status="ok",
            )
            result = {"trace_id": trace_id, **result}
            return _mcp_response(msg_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}), None
        except ToolExecutionError as exc:
            _audit_tool_call(
                trace_id=trace_id,
                token_fingerprint=token_fingerprint,
                tool_name=tool_name,
                arguments=arguments,
                started=started,
                status="error",
                error=exc.message,
            )
            return _mcp_error(msg_id, exc.code, exc.message), None
        except Exception as exc:
            logger.exception("Unhandled Plato MCP tool error")
            _audit_tool_call(
                trace_id=trace_id,
                token_fingerprint=token_fingerprint,
                tool_name=tool_name,
                arguments=arguments,
                started=started,
                status="error",
                error=str(exc),
            )
            return _mcp_error(msg_id, -32000, "Internal Plato MCP tool error"), None

    if method == "ping":
        return _mcp_response(msg_id, {}), None

    return _mcp_error(msg_id, -32601, f"Method not found: {method}"), None


@router.post("/mcp")
async def plato_mcp_streamable_http(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
    accept: Optional[str] = Header(None, alias="Accept"),
):
    token_fingerprint = _authenticate(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    session = None
    if mcp_session_id:
        session = _active_sessions.get(mcp_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="MCP session not found")
        if session.token_fingerprint != token_fingerprint:
            raise HTTPException(status_code=403, detail="MCP session does not belong to this token")

    messages = body if isinstance(body, list) else [body]
    if not all(isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail="MCP body must be a JSON-RPC object or array")

    if all(message.get("id") is None for message in messages):
        for message in messages:
            await _handle_mcp_message(token_fingerprint, message, session)
        return Response(status_code=202)

    responses = []
    new_session_id = None
    for message in messages:
        response, session_id = await _handle_mcp_message(token_fingerprint, message, session)
        if session_id:
            new_session_id = session_id
        if response:
            responses.append(response)

    headers = {}
    if new_session_id:
        headers["Mcp-Session-Id"] = new_session_id

    accepts_sse_only = accept and "text/event-stream" in accept and "application/json" not in accept
    if accepts_sse_only:

        async def event_generator():
            for response in responses:
                yield f"event: message\ndata: {json.dumps(response, default=str)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    content: Any = responses[0] if len(responses) == 1 else responses
    return JSONResponse(content=content, headers=headers)


@router.get("/mcp")
async def plato_mcp_sse_keepalive(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    token_fingerprint = _authenticate(authorization)
    session_id = str(uuid.uuid4())
    session = MCPSession(
        session_id=session_id,
        token_fingerprint=token_fingerprint,
        created_at=datetime.now(timezone.utc),
    )
    _active_sessions[session_id] = session
    endpoint = f"/v1/ella/plato/mcp/sse/message?session_id={urllib.parse.quote(session_id)}"

    async def event_generator():
        try:
            yield f"event: endpoint\ndata: {endpoint}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    response = await asyncio.wait_for(session.sse_queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if response is None:
                    break
                yield f"event: message\ndata: {json.dumps(response, default=str)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _active_sessions.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/mcp/sse/message")
async def plato_mcp_sse_message(
    request: Request,
    session_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    token_fingerprint = _authenticate(authorization)
    session = _active_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="MCP session not found")
    if session.token_fingerprint != token_fingerprint:
        raise HTTPException(status_code=403, detail="MCP session does not belong to this token")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    messages = body if isinstance(body, list) else [body]
    if not all(isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail="MCP body must be a JSON-RPC object or array")

    for message in messages:
        response, _ = await _handle_mcp_message(token_fingerprint, message, session)
        if response:
            await session.sse_queue.put(response)
    return Response(status_code=202)


@router.delete("/mcp")
async def plato_mcp_delete_session(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
):
    token_fingerprint = _authenticate(authorization)
    if not mcp_session_id:
        raise HTTPException(status_code=400, detail="Mcp-Session-Id header required")
    session = _active_sessions.get(mcp_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="MCP session not found")
    if session.token_fingerprint != token_fingerprint:
        raise HTTPException(status_code=403, detail="MCP session does not belong to this token")
    del _active_sessions[mcp_session_id]
    return Response(status_code=204)


@router.get("/mcp/authorize")
async def plato_mcp_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")
    if client_id != _oauth_client_id():
        raise HTTPException(status_code=400, detail="Invalid client_id")

    query = {"code": "plato_mcp"}
    if state:
        query["state"] = state
    location = f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urllib.parse.urlencode(query)}"
    return Response(status_code=302, headers={"Location": location})


@router.post("/mcp/token")
async def plato_mcp_token(request: Request):
    data: dict[str, Any]
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            raw_body = (await request.body()).decode("utf-8")
            parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
            data = {key: values[-1] if values else "" for key, values in parsed.items()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid token request body") from exc

    basic_client_id = ""
    basic_client_secret = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            basic_client_id, basic_client_secret = decoded.split(":", 1)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid client authentication")

    client_id = str(data.get("client_id") or basic_client_id or "")
    if client_id != _oauth_client_id():
        raise HTTPException(status_code=400, detail="Invalid client_id")

    client_secret = str(data.get("client_secret") or basic_client_secret or "")
    if client_secret not in _allowed_tokens():
        raise HTTPException(status_code=401, detail="Invalid client_secret")

    return {
        "access_token": client_secret,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "plato:read",
    }


@router.get("/mcp/info")
async def plato_mcp_info(request: Request):
    base_url = str(request.base_url).rstrip("/")
    endpoint = f"{base_url}/v1/ella/plato/mcp"
    return {
        "endpoint": endpoint,
        "transport": "streamable-http",
        "protocol_version": "2025-03-26",
        "profile_scope": {"uid": _plato_uid(), "canonical_identity": _plato_canonical_identity()},
        "authentication": {
            "header": "Authorization",
            "format": "Bearer <ELLA_PLATO_MCP_TOKEN>",
            "generic_onboarding_endpoint": f"{base_url}/v1/ella/mcp/onboarding",
            "oauth": {
                "client_id": _env("ELLA_MCP_OAUTH_CLIENT_ID", "ella-mcp"),
                "authorization_endpoint": f"{base_url}/v1/ella/mcp/authorize",
                "token_endpoint": f"{base_url}/v1/ella/mcp/token",
                "scopes": ["tools:read", "startup:read", "timeline:read", "memory:read"],
            },
        },
        "tools": [tool["name"] for tool in MCP_TOOLS],
        "write_tools_enabled": False,
        "rollback": "remove or rotate ELLA_PLATO_MCP_TOKEN / ELLA_PLATO_MCP_TOKENS",
    }
