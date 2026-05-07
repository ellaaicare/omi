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
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import database.conversations as conversations_db
import database.memories as memories_db

logger = logging.getLogger("ella.plato_mcp")

router = APIRouter(prefix="/v1/ella/plato", tags=["Ella Plato MCP"])

DEFAULT_PLATO_UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
DEFAULT_TIMELINE_URL = "https://api.ella-ai-care.com/v1/ella/timeline"
DEFAULT_HERMES_GATEWAY_URL = "http://100.76.138.56:8642"
DEFAULT_HERMES_AGENT_ID = "hermes"
DEFAULT_PROVISION_API_URL = "http://100.76.138.56:8200"

MAX_CONTEXT_LIMIT = 50
MAX_SEARCH_RESULTS = 20
MAX_PROMPT_CHARS = 4000
MAX_CONSULT_CONTEXT_CHARS = 7000
RATE_LIMIT_WINDOW_SECONDS = 60

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


def _plato_uid() -> str:
    return _env("ELLA_PLATO_MCP_UID", _env("ELLA_PLATO_UID", DEFAULT_PLATO_UID))


def _plato_canonical_identity() -> str:
    return _env("ELLA_PLATO_CANONICAL_IDENTITY", _plato_uid())


def _plato_agent_id() -> str:
    return _env("ELLA_PLATO_AGENT_ID", f"ella-omi-{_plato_uid().lower()}")


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


def _authenticate(authorization: Optional[str]) -> str:
    tokens = _allowed_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="Plato MCP token is not configured")
    token = _token_from_authorization(authorization)
    if not token or token not in tokens:
        raise HTTPException(status_code=401, detail="Invalid or missing Plato MCP bearer token")
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
    return {
        "event_id": conversation.get("id"),
        "channel": "omi",
        "provider": "omi-backend",
        "role": "user",
        "started_at": _json_safe(conversation.get("started_at") or conversation.get("created_at")),
        "ended_at": _json_safe(conversation.get("finished_at")),
        "title": title,
        "text": _compact_text(overview, 1600),
        "source_ref": {"conversation_id": conversation.get("id")},
    }


def _memory_to_event(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": memory.get("id"),
        "channel": "memory",
        "provider": "omi-backend",
        "role": "memory",
        "started_at": _json_safe(memory.get("created_at")),
        "title": memory.get("category") or "memory",
        "text": _compact_text(memory.get("content"), 1200),
        "source_ref": {"memory_id": memory.get("id")},
    }


async def _fetch_canonical_timeline(limit: int, channels: list[str], since: Optional[str]) -> list[dict[str, Any]]:
    timeline_url = _env("ELLA_PLATO_TIMELINE_URL", DEFAULT_TIMELINE_URL)
    params: dict[str, Any] = {"uid": _plato_uid(), "limit": limit}
    if channels:
        params["channels"] = ",".join(channels)
    if since:
        params["since"] = since
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(timeline_url, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"timeline_http_{response.status_code}")
    payload = response.json()
    events = payload if isinstance(payload, list) else payload.get("events") or payload.get("timeline") or []
    return [_json_safe(event) for event in events if isinstance(event, dict)]


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
    limit = _clamp_int(arguments.get("limit"), 10, 1, MAX_CONTEXT_LIMIT)
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
        "events": events[:limit],
    }


async def _latest_omi(arguments: dict[str, Any]) -> dict[str, Any]:
    context = await _recent_context(
        {"limit": _clamp_int(arguments.get("limit"), 10, 1, MAX_CONTEXT_LIMIT), "channels": ["omi"]}
    )
    events = [event for event in context.get("events", []) if str(event.get("channel", "")).startswith("omi")]
    latest = events[0] if events else None
    return {"latest": latest, "source": context.get("source"), "uid": _plato_uid()}


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", text.lower())}


def _score_item(query_tokens: set[str], item: dict[str, Any]) -> int:
    haystack = " ".join(str(item.get(key) or "") for key in ("title", "text", "summary", "overview"))
    return len(query_tokens & _tokens(haystack))


def _format_consult_context(context: dict[str, Any], limit: int) -> str:
    lines = [
        f"source={context.get('source') or 'unknown'}",
        f"uid={context.get('uid') or _plato_uid()}",
    ]
    for event in (context.get("events") or [])[:limit]:
        timestamp = event.get("started_at") or event.get("created_at") or event.get("timestamp") or ""
        channel = event.get("channel") or ""
        title = event.get("title") or "Untitled"
        text = _compact_text(event.get("text") or event.get("overview") or event.get("summary") or "", 700)
        lines.append(f"- {timestamp} [{channel}] {title}: {text}")
    return _compact_text("\n".join(lines), MAX_CONSULT_CONTEXT_CHARS)


async def _search_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    query = _compact_text(arguments.get("query"), 500)
    if not query:
        raise ToolExecutionError("query is required", code=-32602)
    max_results = _clamp_int(arguments.get("max_results"), 5, 1, MAX_SEARCH_RESULTS)
    context = await _recent_context(
        {
            "limit": max(max_results * 5, 25),
            "channels": arguments.get("channels") or [],
            "since": arguments.get("since"),
        }
    )
    query_tokens = _tokens(query)
    ranked = [
        (score, idx, item)
        for idx, item in enumerate(context.get("events", []))
        if (score := _score_item(query_tokens, item)) > 0
    ]
    ranked.sort(key=lambda row: (row[0], -row[1]), reverse=True)
    return {
        "query": query,
        "source": context.get("source"),
        "results": [item for _score, _idx, item in ranked[:max_results]],
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
        "context_source": context.get("source"),
        "context_events": len(context.get("events") or []),
    }


MCP_TOOLS: list[dict[str, Any]] = [
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
    "plato_recent_context": _recent_context,
    "plato_search_memory": _search_memory,
    "plato_latest_omi": _latest_omi,
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
        return _mcp_response(msg_id, {"tools": MCP_TOOLS}), None

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        trace_id = str(uuid.uuid4())
        started = time.monotonic()
        if not isinstance(arguments, dict):
            return _mcp_error(msg_id, -32602, "Tool arguments must be an object"), None
        if tool_name not in _TOOL_HANDLERS:
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
                "client_id": _oauth_client_id(),
                "authorization_endpoint": f"{endpoint}/authorize",
                "token_endpoint": f"{endpoint}/token",
                "scopes": ["plato:read"],
            },
        },
        "tools": [tool["name"] for tool in MCP_TOOLS],
        "write_tools_enabled": False,
        "rollback": "remove or rotate ELLA_PLATO_MCP_TOKEN / ELLA_PLATO_MCP_TOKENS",
    }
