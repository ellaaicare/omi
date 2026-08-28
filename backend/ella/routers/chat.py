"""
Ella Chat Router

Provides a streaming chat endpoint with configurable debug levels:
  Level 0 (production): Normal flow — routes through OMI's graph chat (Grok via XAI_API_KEY)
  Level 1 (ACK):        Hardcoded acknowledgment response. No LLM call. For UI testing.
  Level 2 (Grok LLM):   Direct Grok API call. For testing LLM without n8n/OpenClaw.
  Level 3 (n8n):         Route through n8n webhook for full pipeline testing.
  Level 4 (OpenClaw):    Direct to OpenClaw gateway's OpenAI-compatible endpoint.

Debug level is set via:
  - Environment variable: ELLA_DEBUG_LEVEL=0 (default)
  - Request header: X-Ella-Debug-Level: 2 (overrides env var)

Endpoints:
- POST /v1/ella/chat/stream - Stream a chat response with debug level routing
"""

import asyncio
import base64
import json
import logging
import os
import re
import hashlib
import time as _time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ella.config import ELLA_CONFIG
from database.ella_provisioning import EllaProvisioningRepository
from database.honcho_attestation import authority_credential
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.routers.resolve import resolve_user_routing
from ella.routers.trace import RouteTrace, record_trace
from ella.services.hermes_session import canonical_omi_session_key, safe_session_component
from ella.services.hermes_cloud_runtime import (
    HermesCloudRuntimeService,
    HermesCloudTurnRequest,
)
from ella.services.ai_consent import require_current_ai_consent
from ella.services.provisioning import ProvisioningError
from ella.services.runtime_resolver import (
    IsolatedRuntime,
    revalidate_runtime_authority,
    resolve_isolated_runtime,
    retained_owner_uid_configured,
    runtime_authority_identity,
    runtime_authority_enabled,
)
from utils.ella.canonical_context import (
    DEFAULT_CONTEXT_CHANNELS,
    canonical_events_to_server_messages,
    fetch_canonical_timeline,
    format_canonical_context,
)
from utils.ella.exact_firebase_auth import get_exact_firebase_uid, require_matching_firebase_uid
from utils.ella.time_context import timezone_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-chat"])

XAI_API_KEY = authority_credential("XAI_API_KEY", strip=False)
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_CHAT_MODEL = os.getenv("ELLA_GROK_CHAT_MODEL", "grok-3-mini")

OPENCLAW_URL = os.getenv("OPENCLAW_URL", "http://100.67.113.120:19001")
OPENCLAW_GATEWAY_TOKEN = authority_credential("OPENCLAW_GATEWAY_TOKEN", strip=False)
CHAT_PLATFORM = os.getenv("ELLA_CHAT_PLATFORM", "hermes").strip().lower()
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_URL", "").strip().rstrip("/")
HERMES_GATEWAY_TOKEN = authority_credential("HERMES_API_SERVER_KEY", "API_SERVER_KEY", strip=False)
HERMES_AGENT_ID = os.getenv("HERMES_AGENT_ID", "hermes")
HERMES_MODEL = os.getenv("HERMES_MODEL", "").strip()
HERMES_CHAT_SESSION_EPOCH = os.getenv("ELLA_CHAT_HERMES_SESSION_EPOCH", "").strip()
HERMES_CHAT_SESSION_SCOPE = os.getenv("ELLA_CHAT_HERMES_SESSION_SCOPE", "canonical").strip().lower()
HERMES_CHAT_REQUEST_TIMEOUT_SECONDS = float(os.getenv("ELLA_CHAT_HERMES_REQUEST_TIMEOUT_SECONDS", "60"))
HERMES_CHAT_KEEPALIVE_SECONDS = max(1.0, float(os.getenv("ELLA_CHAT_HERMES_KEEPALIVE_SECONDS", "5")))
CHAT_STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
CHAT_CONTEXT_LIMIT = int(os.getenv("ELLA_CHAT_CANONICAL_CONTEXT_LIMIT", "25"))
CHAT_CONTEXT_MAX_CHARS = int(os.getenv("ELLA_CHAT_CANONICAL_CONTEXT_MAX_CHARS", "6000"))
CHAT_TEMPORAL_CONTEXT_LIMIT = int(os.getenv("ELLA_CHAT_TEMPORAL_CONTEXT_LIMIT", "250"))
CHAT_TEMPORAL_CONTEXT_MAX_CHARS = int(os.getenv("ELLA_CHAT_TEMPORAL_CONTEXT_MAX_CHARS", "9000"))
CHAT_CLIENT_TYPE_ALLOWLIST = frozenset({"ios", "android", "web"})
CHAT_SERVER_ROUTE_CATEGORY = "chat_stream"
CHAT_USER_TIMEZONE = timezone_name(os.getenv("ELLA_USER_TIMEZONE", os.getenv("ELLA_PLATO_TIMEZONE", "")))
CHAT_CONTEXT_CHANNELS = [
    channel.strip()
    for channel in os.getenv("ELLA_CHAT_CANONICAL_CHANNELS", ",".join(DEFAULT_CONTEXT_CHANNELS)).split(",")
    if channel.strip()
]
_canonical_event_store = PostgresCanonicalEventStore()
_hermes_chat_turn_tasks: dict[tuple[str, str], asyncio.Task[list[str]]] = {}

ELLA_SYSTEM_PROMPT = (
    "You are Ella, a warm and caring AI companion for elderly users. "
    "You speak clearly and simply, with patience and warmth. "
    "You help with daily life questions, provide companionship, and gently encourage healthy habits. "
    "Keep responses concise and easy to understand. "
    "If someone seems confused or distressed, respond with extra gentleness and reassurance."
)


def _retained_owner_chat_configured(uid: str) -> bool:
    """Permit the legacy retained route only with an explicit owner and full config."""
    return bool(retained_owner_uid_configured(uid) and HERMES_GATEWAY_URL and HERMES_GATEWAY_TOKEN and HERMES_MODEL)


def _hermes_chat_memory_key(uid: str) -> str:
    """Stable Hermes/Honcho long-term memory scope for this authenticated user."""

    return canonical_omi_session_key(uid)


def _hermes_chat_session_key(uid: str) -> str:
    safe_uid = safe_session_component(uid.lower())
    if HERMES_CHAT_SESSION_SCOPE in {"canonical", "shared", "cross_channel", "cross-channel"}:
        return _hermes_chat_memory_key(uid)
    if HERMES_CHAT_SESSION_EPOCH:
        epoch = HERMES_CHAT_SESSION_EPOCH
    else:
        epoch = datetime.now(timezone.utc).astimezone(ZoneInfo(CHAT_USER_TIMEZONE)).strftime("daily-%Y%m%d")
    return f"ella:omi:{safe_uid}:ios-chat:{epoch}"


def _hermes_chat_headers(
    session_id: str,
    session_key: str | None = None,
    gateway_token: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {gateway_token or HERMES_GATEWAY_TOKEN}",
        "Content-Type": "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    if session_key:
        headers["X-Hermes-Session-Key"] = session_key
    return headers


async def _hermes_nonstream_completion(
    messages: list[dict],
    session_key: str,
    memory_key: str | None = None,
    *,
    gateway_url: str = HERMES_GATEWAY_URL,
    gateway_token: str = HERMES_GATEWAY_TOKEN,
    agent_id: str = HERMES_MODEL,
) -> str:
    recovery_session = f"{session_key}:empty-recovery"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{gateway_url}/v1/chat/completions",
            headers=_hermes_chat_headers(recovery_session, memory_key or session_key, gateway_token),
            json={
                "model": agent_id,
                "messages": messages,
                "stream": False,
            },
        )
    if response.status_code != 200:
        body = response.text[:160]
        print(
            f"[FLOW:CHAT-HERMES] EMPTY_RECOVERY_ERROR status={response.status_code} body={body!r}",
            flush=True,
        )
        return ""
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    delta = choices[0].get("delta") or {}
    return str(message.get("content") or delta.get("content") or "").strip()


class EllaChatRequest(BaseModel):
    uid: str = ""
    message: str
    conversation_id: str = ""
    client_message_id: str = ""
    client_sent_at: str = ""


def _parse_client_sent_at(value: str = "") -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _canonical_turn_id(uid: str, request: EllaChatRequest, started_at: datetime) -> str:
    if request.client_message_id:
        return request.client_message_id
    digest = hashlib.sha256(
        "|".join(
            (
                uid,
                request.conversation_id or "no-conversation",
                request.message,
                request.client_sent_at or started_at.isoformat(),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"server-{digest}"


def _ios_chat_event(
    *,
    uid: str,
    turn_id: str,
    role: str,
    text: str,
    session_key: str,
    started_at: datetime,
    ended_at: datetime = None,
    client_info: dict = None,
) -> CanonicalEventIn:
    source_identity = f"ios_chat:{uid}:{turn_id}"
    return CanonicalEventIn(
        uid=uid,
        canonical_identity=uid,
        event_id=f"{source_identity}:{role}",
        session_id=session_key,
        channel="ios_chat",
        provider="omi-ios-chat",
        role=role,
        text=text,
        started_at=started_at,
        ended_at=ended_at,
        privacy_scope="user_private",
        scan_policy="immediate" if role == "user" else "none",
        source_ref={
            "source_identity": source_identity,
            "client_message_id": turn_id,
            "message_id": f"{turn_id}:{role}",
            "source": "ios_chat",
        },
        metadata={
            "adapter": "ios-chat",
            "client": client_info or {},
            "session_strategy": HERMES_CHAT_SESSION_SCOPE,
            "hermes_session_key": session_key,
        },
    )


async def _write_ios_chat_canonical_event(event: CanonicalEventIn) -> None:
    try:
        result = await _canonical_event_store.write_batch([event])
        logger.info(
            "[FLOW:CHAT-CANONICAL-WRITE] uid=%s role=%s event_id=%s inserted=%s duplicates=%s",
            event.uid,
            event.role,
            event.event_id,
            result.get("inserted"),
            result.get("duplicates"),
        )
    except Exception as exc:
        logger.warning(
            "[FLOW:CHAT-CANONICAL-WRITE] uid=%s role=%s event_id=%s error=%s",
            event.uid,
            event.role,
            event.event_id,
            exc,
        )


def _resolve_debug_level(header_value: str = None) -> int:
    """Resolve the effective debug level from header override or config."""
    if header_value is not None:
        try:
            level = int(header_value)
            if 0 <= level <= 4:
                return level
            logger.warning("[FLOW:CHAT] Invalid debug level header; using config default")
        except (ValueError, TypeError):
            logger.warning("[FLOW:CHAT] Non-integer debug level header; using config default")
    return ELLA_CONFIG.debug_level


def _bounded_client_type(header_value: str | None) -> str:
    normalized = str(header_value or "").strip().lower()
    return normalized if normalized in CHAT_CLIENT_TYPE_ALLOWLIST else "other"


def _server_owned_client_metadata(header_value: str | None) -> dict[str, str]:
    """Return the only caller-derived category allowed past the auth boundary."""
    return {
        "type": _bounded_client_type(header_value),
        "route": CHAT_SERVER_ROUTE_CATEGORY,
    }


async def _fetch_chat_canonical_events(
    uid: str,
    *,
    limit: int,
    before: str = None,
    channels: list[str] | None = None,
    since: str = None,
    user_timezone: str = None,
) -> list[dict]:
    try:
        events = await fetch_canonical_timeline(
            uid,
            limit=limit,
            channels=channels or CHAT_CONTEXT_CHANNELS,
            since=since,
            before=before,
            user_timezone=user_timezone,
        )
        logger.info("[FLOW:CANONICAL-CONTEXT] uid=%s events=%s source=timeline", uid, len(events))
        return events
    except Exception as e:
        logger.warning(
            "[FLOW:CANONICAL-CONTEXT] uid=%s unavailable=%s fallback=migration_legacy_history",
            uid,
            e,
        )
        return []


def _chat_temporal_window(user_message: str) -> tuple[str | None, datetime | None, datetime | None]:
    """Detect user-local recall windows that shallow recent context often misses."""
    text = user_message.lower()
    temporal_terms = (
        "this morning",
        "morning",
        "today",
        "earlier",
        "latest omi",
        "last omi",
        "omi conversation",
        "necklace",
        "captured",
    )
    if not any(term in text for term in temporal_terms):
        return None, None, None
    tz = ZoneInfo(CHAT_USER_TIMEZONE)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    start_of_day = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    if "morning" in text:
        return (
            "same-day morning OMI context",
            (start_of_day + timedelta(hours=5)).astimezone(timezone.utc),
            (start_of_day + timedelta(hours=12)).astimezone(timezone.utc),
        )
    return "same-day OMI context", start_of_day.astimezone(timezone.utc), now_local.astimezone(timezone.utc)


def _event_datetime(event: dict, key: str = "started_at") -> datetime | None:
    raw = event.get(key) or event.get("created_at") or event.get("timestamp")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filter_events_window(events: list[dict], since: datetime, until: datetime) -> list[dict]:
    filtered = []
    for event in events:
        started = _event_datetime(event, "started_at")
        ended = _event_datetime(event, "ended_at") or started
        if started is None or ended is None:
            continue
        if ended >= since and started <= until:
            filtered.append(event)
    return filtered


def _is_low_value_omi_event(event: dict) -> bool:
    title = str(event.get("title") or "").lower()
    text = str(event.get("text") or event.get("overview") or event.get("summary") or "").strip()
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    tags = metadata.get("ella_tags") if isinstance(metadata.get("ella_tags"), list) else []
    if "low_signal" in tags:
        return True
    if "brief" in title or "fragment" in title or "utterance" in title:
        return True
    if len(text) <= 180:
        return True
    return False


async def _fetch_temporal_chat_context(uid: str, user_message: str) -> tuple[str, list[dict]]:
    label, since, until = _chat_temporal_window(user_message)
    if not label or since is None or until is None:
        return "", []
    events = await _fetch_chat_canonical_events(
        uid,
        limit=CHAT_TEMPORAL_CONTEXT_LIMIT,
        channels=["omi"],
        since=(since - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        user_timezone=CHAT_USER_TIMEZONE,
    )
    window_events = _filter_events_window(events, since, until)
    meaningful_events = [event for event in window_events if not _is_low_value_omi_event(event)]
    return label, meaningful_events or window_events


async def _stream_level_1_ack(user_message: str):
    """Level 1: Immediate ACK response. No LLM call."""
    print(f"[FLOW:CHAT-L1] ACK mode, no LLM call", flush=True)
    ack_text = (
        f"Hello! I'm Ella, your AI companion. I received your message: " f"'{user_message}'. (Debug mode - Level 1 ACK)"
    )
    data = json.dumps({"choices": [{"delta": {"content": ack_text}}]})
    yield f"data: {data}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_level_2_grok(user_message: str):
    """Level 2: Direct Grok API call via xAI."""
    _start = _time.time()

    if not XAI_API_KEY:
        print(f"[FLOW:CHAT-L2] ERROR provider=xai key_missing=true", flush=True)
        error_data = json.dumps({"error": "XAI_API_KEY not configured"})
        yield f"data: {error_data}\n\n"
        return

    print(f"[FLOW:CHAT-L2] provider=xai model={XAI_CHAT_MODEL} streaming=true", flush=True)

    async with httpx.AsyncClient() as client:
        try:
            async with client.stream(
                "POST",
                f"{XAI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": XAI_CHAT_MODEL,
                    "messages": [
                        {"role": "system", "content": ELLA_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": True,
                    "temperature": 0.7,
                },
                timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    _elapsed = int((_time.time() - _start) * 1000)
                    print(
                        f"[FLOW:CHAT-L2] ERROR provider=xai status={response.status_code} latency={_elapsed}ms",
                        flush=True,
                    )
                    error_data = json.dumps({"error": f"Grok API error: {response.status_code}"})
                    yield f"data: {error_data}\n\n"
                    return
                chunk_count = 0
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        chunk_count += 1
                        yield f"{line}\n\n"
                _elapsed = int((_time.time() - _start) * 1000)
                print(
                    f"[FLOW:CHAT-L2] OK provider=xai model={XAI_CHAT_MODEL} chunks={chunk_count} latency={_elapsed}ms",
                    flush=True,
                )
        except httpx.TimeoutException:
            _elapsed = int((_time.time() - _start) * 1000)
            print(f"[FLOW:CHAT-L2] TIMEOUT provider=xai latency={_elapsed}ms", flush=True)
            error_data = json.dumps({"error": "Grok API timeout"})
            yield f"data: {error_data}\n\n"
        except Exception as e:
            _elapsed = int((_time.time() - _start) * 1000)
            print(f"[FLOW:CHAT-L2] ERROR provider=xai error={e} latency={_elapsed}ms", flush=True)
            error_data = json.dumps({"error": f"Grok streaming error: {str(e)}"})
            yield f"data: {error_data}\n\n"


async def _stream_level_3_n8n(user_message: str, uid: str, conversation_id: str):
    """Level 3: Route through n8n webhook for full pipeline testing."""
    _start = _time.time()

    webhook_url = ELLA_CONFIG.n8n_chat_webhook
    if not webhook_url:
        # Fall back to constructing from base URL
        webhook_url = f"{ELLA_CONFIG.n8n_base_url}/webhook/ella-chat"

    print(f"[FLOW:CHAT-L3] provider=n8n uid={uid} webhook={webhook_url}", flush=True)

    payload = {
        "uid": uid,
        "message": user_message,
        "conversation_id": conversation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "ella-chat-debug-3",
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30.0,
            )

            _elapsed = int((_time.time() - _start) * 1000)

            if response.status_code == 200:
                result = response.json()
                reply_text = result.get("reply", result.get("message", result.get("text", str(result))))
                reply_preview = reply_text[:80] if reply_text else "(empty)"
                print(
                    f"[FLOW:CHAT-L3] OK provider=n8n status=200 latency={_elapsed}ms reply={reply_preview}", flush=True
                )
                data = json.dumps({"choices": [{"delta": {"content": reply_text}}]})
                yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
            else:
                print(
                    f"[FLOW:CHAT-L3] ERROR provider=n8n status={response.status_code} latency={_elapsed}ms", flush=True
                )
                error_data = json.dumps(
                    {"error": f"n8n webhook returned {response.status_code}: {response.text[:200]}"}
                )
                yield f"data: {error_data}\n\n"

        except httpx.TimeoutException:
            _elapsed = int((_time.time() - _start) * 1000)
            print(f"[FLOW:CHAT-L3] TIMEOUT provider=n8n latency={_elapsed}ms", flush=True)
            error_data = json.dumps({"error": "n8n webhook timed out (30s)"})
            yield f"data: {error_data}\n\n"
        except Exception as e:
            _elapsed = int((_time.time() - _start) * 1000)
            print(f"[FLOW:CHAT-L3] ERROR provider=n8n error={e} latency={_elapsed}ms", flush=True)
            error_data = json.dumps({"error": f"n8n webhook error: {str(e)}"})
            yield f"data: {error_data}\n\n"


async def _stream_level_4_openclaw(user_message: str, uid: str, client_info: dict = None):
    """Level 4: Direct to OpenClaw gateway's OpenAI-compatible endpoint.

    Dynamically resolves the user's agent from the database instead of
    hardcoding 'openclaw:main'. Falls back to 'openclaw:main' if no
    cluster is provisioned.

    Emits OMI-compatible SSE format:
      data: <text chunk>          (streaming content, can be multiple)
      done: <base64 ServerMessage JSON>  (final message)

    Sends SSE keep-alive comments (: keepalive) every 5s while waiting
    for OpenClaw to prevent proxy/client idle timeouts.
    """
    _start = _time.time()

    if not OPENCLAW_GATEWAY_TOKEN:
        print("[FLOW:CHAT-L4] error=token_missing", flush=True)
        yield "data: Error: OPENCLAW_GATEWAY_TOKEN not configured\n\n"
        return

    # Dynamic agent resolution with tracing
    _l4_trace = RouteTrace()
    del client_info
    _l4_trace.endpoint_class = "chat"
    _l4_trace.method = "POST"
    _l4_trace.debug_level = 4

    resolved = await resolve_user_routing(uid)
    if resolved and resolved.get("routing"):
        agent_id = resolved["routing"]["agentId"]
        gateway_url = resolved["routing"]["gatewayUrl"]
        session_key = resolved["routing"]["sessionKey"]
        print("[FLOW:CHAT-L4] routing=database", flush=True)
    else:
        agent_id = "main"
        gateway_url = OPENCLAW_URL
        session_key = f"ella:{uid}"
        print("[FLOW:CHAT-L4] routing=fallback", flush=True)

    canonical_events = await _fetch_chat_canonical_events(uid, limit=CHAT_CONTEXT_LIMIT)
    canonical_context = format_canonical_context(canonical_events, max_chars=CHAT_CONTEXT_MAX_CHARS)
    temporal_label, temporal_events = await _fetch_temporal_chat_context(uid, user_message)
    temporal_context = format_canonical_context(
        temporal_events,
        max_chars=CHAT_TEMPORAL_CONTEXT_MAX_CHARS,
        user_timezone=CHAT_USER_TIMEZONE,
    )
    messages = []
    if canonical_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Use this canonical timeline as the freshest available user context. "
                    "It may include OMI, iOS chat, voice, iMessage, Guardian, Telegram, and memory events. "
                    "Prefer it over older OpenClaw session history when answering.\n\n"
                    f"{canonical_context}"
                ),
            }
        )
    else:
        print("[FLOW:CHAT-L4] canonical_context=empty", flush=True)
    if temporal_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"Additional {temporal_label}. Use this when the user asks about today, this morning, "
                    "or OMI/necklace captures; do not claim the window is empty if events are listed here.\n\n"
                    f"{temporal_context}"
                ),
            }
        )
    messages.append({"role": "user", "content": user_message})

    # Use asyncio.Task so we can yield keep-alives while waiting
    async def _call_openclaw():
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"{gateway_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
                    "x-openclaw-scopes": "operator.write",
                    "x-openclaw-session-key": session_key,
                },
                json={
                    "model": f"openclaw:{agent_id}",
                    "messages": messages,
                    "stream": False,
                    "user": session_key,
                },
                timeout=90.0,
            )

    task = asyncio.create_task(_call_openclaw())

    # Send SSE keep-alive comments every 5s while OpenClaw is processing
    keepalive_count = 0
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            keepalive_count += 1
            yield ": keepalive\n\n"

    try:
        response = task.result()

        _elapsed = int((_time.time() - _start) * 1000)

        if response.status_code != 200:
            print(f"[FLOW:CHAT-L4] error=provider_status latency_ms={_elapsed}", flush=True)
            yield f"data: Error: OpenClaw returned {response.status_code}\n\n"
            return

        result = response.json()
        choices = result.get("choices", [])
        reply = choices[0]["message"]["content"] if choices else "No response from OpenClaw"

        # Strip <think>...</think> blocks that some reasoning models include
        reply = re.sub(r'<think>.*?</think>\s*', '', reply, flags=re.DOTALL).strip()

        _l4_trace.total_latency_ms = _elapsed
        _l4_trace.response_status = 200
        record_trace(_l4_trace)

        print(f"[FLOW:CHAT-L4] status=ok latency_ms={_elapsed} keepalives={keepalive_count}", flush=True)

        # Emit in OMI format: data: <text> then done: <base64 json>
        encoded_reply = reply.replace(chr(10), '__CRLF__')
        chunk_size = 800
        for i in range(0, len(encoded_reply), chunk_size):
            yield f"data: {encoded_reply[i:i+chunk_size]}\n\n"

        # Build the final ServerMessage JSON matching OMI schema
        msg = {
            "id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "text": reply,
            "sender": "ai",
            "type": "text",
            "plugin_id": None,
            "from_integration": False,
            "memories": [],
            "files": [],
            "ask_for_nps": False,
        }
        done_b64 = base64.b64encode(json.dumps(msg).encode()).decode()
        yield f"done: {done_b64}\n\n"

    except httpx.TimeoutException:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-L4] error=timeout latency_ms={_elapsed}", flush=True)
        yield "data: Error: OpenClaw request timed out\n\n"
    except Exception:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-L4] error=unexpected latency_ms={_elapsed}", flush=True)
        yield "data: Error: OpenClaw request failed\n\n"


async def _produce_hermes_chat_events(
    user_message: str,
    uid: str,
    client_info: dict = None,
    *,
    turn_id: str = "",
    client_sent_at: datetime = None,
    runtime: IsolatedRuntime | None = None,
):
    """Finish one Hermes turn and collect only terminal-safe SSE events."""
    _start = _time.time()
    if runtime is None and not _retained_owner_chat_configured(uid):
        yield "data: Error: hermes_runtime_required\n\n"
        return
    if runtime is None and await runtime_authority_enabled(uid):
        yield "data: Error: isolated runtime required\n\n"
        return

    runtime_identity = None
    if runtime is not None:
        try:
            runtime_identity = runtime_authority_identity(runtime)
        except ProvisioningError as exc:
            yield f"data: Error: {exc.code}\n\n"
            return
    elif not HERMES_GATEWAY_TOKEN:
        print(f"[FLOW:CHAT-HERMES] ERROR token_missing=true uid={uid}", flush=True)
        yield "data: Error: HERMES_API_SERVER_KEY not configured\n\n"
        return

    session_key = _hermes_chat_session_key(uid)
    user_started_at = client_sent_at or datetime.now(timezone.utc)
    turn_id = turn_id or f"server-{uuid4()}"
    text = []
    canonical_events = await _fetch_chat_canonical_events(uid, limit=CHAT_CONTEXT_LIMIT)
    canonical_context = format_canonical_context(canonical_events, max_chars=CHAT_CONTEXT_MAX_CHARS)
    temporal_label, temporal_events = await _fetch_temporal_chat_context(uid, user_message)
    temporal_context = format_canonical_context(
        temporal_events,
        max_chars=CHAT_TEMPORAL_CONTEXT_MAX_CHARS,
        user_timezone=CHAT_USER_TIMEZONE,
    )
    messages = []
    if canonical_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Use this canonical timeline as the freshest available user context. "
                    "It may include OMI, iOS chat, voice, iMessage, Guardian, Telegram, and memory events. "
                    "Prefer it over older Hermes/OpenClaw session history when answering.\n\n"
                    f"{canonical_context}"
                ),
            }
        )
    else:
        print(
            f"[FLOW:CHAT-HERMES] uid={uid} canonical_context=empty fallback=hermes_session_history_migration",
            flush=True,
        )
    if temporal_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"Additional {temporal_label}. Use this when the user asks about today, this morning, "
                    "or OMI/necklace captures; do not claim the window is empty if events are listed here.\n\n"
                    f"{temporal_context}"
                ),
            }
        )
    await _write_ios_chat_canonical_event(
        _ios_chat_event(
            uid=uid,
            turn_id=turn_id,
            role="user",
            text=user_message,
            session_key=session_key,
            started_at=user_started_at,
            client_info=client_info,
        )
    )
    messages.append({"role": "user", "content": user_message})
    memory_key = _hermes_chat_memory_key(uid)
    print(
        f"[FLOW:CHAT-HERMES] uid={uid} profile={runtime.profile_name if runtime else 'legacy-shared'} session={session_key} memory_key={memory_key} session_strategy={HERMES_CHAT_SESSION_SCOPE} turn_id={turn_id} canonical_events={len(canonical_events)} temporal_events={len(temporal_events)}",
        flush=True,
    )

    try:
        send_runtime = runtime
        if runtime_identity is not None:
            send_runtime = await revalidate_runtime_authority(runtime_identity)
            if send_runtime.provider != "hermes":
                raise ProvisioningError("self_hosted_runtime_required", retryable=False)
        gateway_url = send_runtime.gateway_url if send_runtime else HERMES_GATEWAY_URL
        gateway_token = send_runtime.gateway_token if send_runtime else HERMES_GATEWAY_TOKEN
        agent_id = send_runtime.agent_id if send_runtime else HERMES_MODEL
        if not gateway_token:
            raise ProvisioningError("hermes_runtime_credential_missing", retryable=False)
        async with httpx.AsyncClient(timeout=HERMES_CHAT_REQUEST_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST",
                f"{gateway_url}/v1/chat/completions",
                headers=_hermes_chat_headers(session_key, memory_key, gateway_token),
                json={
                    "model": agent_id,
                    "messages": messages,
                    "stream": True,
                },
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    _elapsed = int((_time.time() - _start) * 1000)
                    print(
                        f"[FLOW:CHAT-HERMES] ERROR status={response.status_code} latency={_elapsed}ms",
                        flush=True,
                    )
                    yield f"data: Error: Hermes API returned {response.status_code}\n\n"
                    return

                terminal_seen = False
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason is not None:
                        terminal_seen = True
                        if finish_reason != "stop":
                            raise RuntimeError("hermes_stream_incomplete")
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        text.append(content)
                        yield f"data: {content.replace(chr(10), '__CRLF__')}\n\n"

                if text and not terminal_seen:
                    raise RuntimeError("hermes_stream_missing_terminal")

        full_text = "".join(text).strip()
        if not full_text:
            recovery_runtime = send_runtime
            if runtime_identity is not None:
                recovery_runtime = await revalidate_runtime_authority(runtime_identity)
                if recovery_runtime.provider != "hermes":
                    raise ProvisioningError("self_hosted_runtime_required", retryable=False)
            recovery_text = await _hermes_nonstream_completion(
                messages,
                session_key,
                memory_key,
                gateway_url=recovery_runtime.gateway_url if recovery_runtime else HERMES_GATEWAY_URL,
                gateway_token=recovery_runtime.gateway_token if recovery_runtime else HERMES_GATEWAY_TOKEN,
                agent_id=recovery_runtime.agent_id if recovery_runtime else HERMES_MODEL,
            )
            if recovery_text:
                text.append(recovery_text)
                full_text = recovery_text
                print(
                    f"[FLOW:CHAT-HERMES] EMPTY_RECOVERY_OK uid={uid} session={session_key}",
                    flush=True,
                )
                yield f"data: {recovery_text.replace(chr(10), '__CRLF__')}\n\n"
            else:
                print(
                    f"[FLOW:CHAT-HERMES] EMPTY_RESPONSE uid={uid} session={session_key}",
                    flush=True,
                )
        if full_text:
            if runtime_identity is not None:
                completion_runtime = await revalidate_runtime_authority(runtime_identity)
                if completion_runtime.provider != "hermes":
                    raise ProvisioningError("self_hosted_runtime_required", retryable=False)
            assistant_started_at = datetime.now(timezone.utc)
            await _write_ios_chat_canonical_event(
                _ios_chat_event(
                    uid=uid,
                    turn_id=turn_id,
                    role="assistant",
                    text=full_text,
                    session_key=session_key,
                    started_at=assistant_started_at,
                    ended_at=assistant_started_at,
                    client_info=client_info,
                )
            )
            msg = {
                "id": str(uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "text": full_text,
                "sender": "ai",
                "type": "text",
                "plugin_id": None,
                "from_integration": False,
                "memories": [],
                "files": [],
                "ask_for_nps": False,
            }
            yield f"done: {base64.b64encode(json.dumps(msg).encode()).decode()}\n\n"

        _elapsed = int((_time.time() - _start) * 1000)
        print(
            f"[FLOW:CHAT-HERMES] OK uid={uid} turn_id={turn_id} chars={len(full_text)} latency={_elapsed}ms",
            flush=True,
        )

    except ProvisioningError as exc:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-HERMES] AUTHORITY_ERROR uid={uid} code={exc.code} latency={_elapsed}ms", flush=True)
        yield f"data: Error: {exc.code}\n\n"
    except httpx.TimeoutException:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-HERMES] TIMEOUT uid={uid} latency={_elapsed}ms", flush=True)
        yield "data: Error: Hermes request timed out\n\n"
    except Exception:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-HERMES] ERROR uid={uid} error=unexpected latency={_elapsed}ms", flush=True)
        yield "data: Error: hermes_unavailable\n\n"


async def _collect_hermes_chat_events(*args, **kwargs) -> list[str]:
    events = [event async for event in _produce_hermes_chat_events(*args, **kwargs)]
    if any(event.startswith("done: ") for event in events):
        return events
    errors = [event for event in events if event.startswith("data: Error: ")]
    return errors[-1:] or ["data: Error: hermes_stream_incomplete\n\n"]


def _release_hermes_chat_turn(key: tuple[str, str], task: asyncio.Task[list[str]]) -> None:
    if _hermes_chat_turn_tasks.get(key) is task:
        _hermes_chat_turn_tasks.pop(key, None)
    try:
        failure = task.exception()
    except asyncio.CancelledError:
        return
    if failure is not None:
        logger.error("Detached Hermes chat turn failed: %s", type(failure).__name__)


async def _stream_hermes_chat(
    user_message: str,
    uid: str,
    client_info: dict = None,
    *,
    turn_id: str = "",
    client_sent_at: datetime = None,
    runtime: IsolatedRuntime | None = None,
):
    """Keep an authenticated Hermes turn alive if its SSE subscriber disconnects."""

    turn_id = turn_id or f"server-{uuid4()}"
    key = (uid, turn_id)
    task = _hermes_chat_turn_tasks.get(key)
    if task is None:
        task = asyncio.create_task(
            _collect_hermes_chat_events(
                user_message,
                uid,
                client_info,
                turn_id=turn_id,
                client_sent_at=client_sent_at,
                runtime=runtime,
            )
        )
        _hermes_chat_turn_tasks[key] = task
        task.add_done_callback(lambda completed, task_key=key: _release_hermes_chat_turn(task_key, completed))

    while not task.done():
        try:
            events = await asyncio.wait_for(asyncio.shield(task), timeout=HERMES_CHAT_KEEPALIVE_SECONDS)
            break
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
    else:
        events = await asyncio.shield(task)

    for event in events:
        yield event


async def _stream_hermes_cloud_chat(
    user_message: str,
    uid: str,
    client_info: dict,
    *,
    turn_id: str,
    client_sent_at: datetime,
    runtime: IsolatedRuntime,
):
    """Execute one durable cloud turn; vendor credentials stay server-side."""
    canonical_events = await _fetch_chat_canonical_events(uid, limit=CHAT_CONTEXT_LIMIT)
    canonical_context = format_canonical_context(
        canonical_events,
        max_chars=CHAT_CONTEXT_MAX_CHARS,
    )
    temporal_label, temporal_events = await _fetch_temporal_chat_context(uid, user_message)
    temporal_context = format_canonical_context(
        temporal_events,
        max_chars=CHAT_TEMPORAL_CONTEXT_MAX_CHARS,
        user_timezone=CHAT_USER_TIMEZONE,
    )
    instructions = [ELLA_SYSTEM_PROMPT]
    if canonical_context:
        instructions.append(
            "The canonical timeline below is the freshest server-authoritative context. "
            "Use only relevant facts and do not claim absent information is present.\n\n"
            f"{canonical_context}"
        )
    if temporal_context:
        instructions.append(f"Additional {temporal_label}:\n\n{temporal_context}")

    try:
        repository = await EllaProvisioningRepository.create()
        result = await HermesCloudRuntimeService(
            repository=repository,
            event_store=_canonical_event_store,
        ).run_turn(
            runtime,
            HermesCloudTurnRequest(
                uid=uid,
                client_interaction_id=turn_id,
                correlation_id=f"ios-chat:{turn_id}",
                channel="ios_chat",
                user_input=user_message,
                instructions="\n\n".join(instructions),
                started_at=client_sent_at,
                client_metadata=client_info or {},
            ),
        )
        yield f"data: {result.text.replace(chr(10), '__CRLF__')}\n\n"
        message = {
            "id": result.canonical_assistant_event_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "text": result.text,
            "sender": "ai",
            "type": "text",
            "plugin_id": None,
            "from_integration": False,
            "memories": [],
            "files": [],
            "ask_for_nps": False,
        }
        yield f"done: {base64.b64encode(json.dumps(message).encode()).decode()}\n\n"
        logger.info(
            "[FLOW:CHAT-HERMES-CLOUD] uid=%s binding=%s duplicate=%s response=%s",
            uid,
            runtime.binding_id,
            result.duplicate,
            bool(result.response_id),
        )
    except ProvisioningError as exc:
        logger.warning(
            "[FLOW:CHAT-HERMES-CLOUD] uid=%s binding=%s code=%s retryable=%s",
            uid,
            runtime.binding_id,
            exc.code,
            exc.retryable,
        )
        error_text = "Ella is temporarily unavailable. Please try again."
        yield f"data: {error_text}\n\n"
        # OMI's SSE parser requires one terminal frame for every HTTP 200
        # stream, including fail-closed provider admission failures.
        message = {
            "id": f"hermes-error:{turn_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "text": error_text,
            "sender": "ai",
            "type": "text",
            "plugin_id": None,
            "from_integration": False,
            "memories": [],
            "files": [],
            "ask_for_nps": False,
        }
        encoded = base64.b64encode(json.dumps(message).encode()).decode()
        yield f"done: {encoded}\n\n"


@router.post("/chat/stream")
async def ella_chat_stream(
    request: EllaChatRequest,
    raw_request: Request,
    authenticated_uid: str = Depends(require_current_ai_consent),
    x_ella_debug_level: str = Header(None, alias="X-Ella-Debug-Level"),
    x_ella_client_type: str = Header(None, alias="X-Ella-Client-Type"),
    x_ella_client_version: str = Header(None, alias="X-Ella-Client-Version"),
    x_ella_route: str = Header(None, alias="X-Ella-Route"),
):
    """
    Stream a chat response from Ella with configurable debug levels.

    Debug levels:
      0 = production (Grok via configured LLM clients)
      1 = ACK (hardcoded response, no LLM)
      2 = Grok direct (xAI API, no graph routing)
      3 = n8n webhook (full pipeline)
      4 = OpenClaw (dynamic per-user agent routing)

    Set via env ELLA_DEBUG_LEVEL or header X-Ella-Debug-Level.
    """
    _trace_start = _time.time()

    if request.uid and request.uid != authenticated_uid:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})
    uid = authenticated_uid

    runtime = None
    try:
        runtime = await resolve_isolated_runtime(uid, target_mode="hermes-cloud-chat")
    except ProvisioningError as exc:
        raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc

    if runtime is None and not _retained_owner_chat_configured(uid):
        raise HTTPException(status_code=409, detail={"code": "hermes_runtime_required"})

    debug_level = _resolve_debug_level(x_ella_debug_level)

    # Discard raw version, route, and debug metadata before every downstream
    # branch. Only the fixed client enum and server-owned route category remain.
    client_metadata = _server_owned_client_metadata(x_ella_client_type)
    del x_ella_client_version, x_ella_route
    print(f"[FLOW:CHAT] level={debug_level} request_received=true", flush=True)

    # Create routing trace
    trace = RouteTrace()
    trace.endpoint_class = "chat"
    trace.method = "POST"
    trace.debug_level = debug_level
    client_sent_at = _parse_client_sent_at(request.client_sent_at)
    turn_id = _canonical_turn_id(uid, request, client_sent_at)

    if CHAT_PLATFORM == "hermes" or runtime is not None:
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        stream = (
            _stream_hermes_cloud_chat(
                request.message,
                uid,
                client_metadata,
                turn_id=turn_id,
                client_sent_at=client_sent_at,
                runtime=runtime,
            )
            if runtime and runtime.provider == "hermes_cloud"
            else _stream_hermes_chat(
                request.message,
                uid,
                client_metadata,
                turn_id=turn_id,
                client_sent_at=client_sent_at,
                runtime=runtime,
            )
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers=CHAT_STREAM_HEADERS,
        )

    if debug_level == 1:
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_1_ack(request.message),
            media_type="text/event-stream",
        )

    if debug_level == 2:
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_2_grok(request.message),
            media_type="text/event-stream",
        )

    if debug_level == 3:
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_3_n8n(request.message, uid, request.conversation_id),
            media_type="text/event-stream",
        )

    if debug_level == 4:
        # L4 records the same fixed, content-free trace schema after completion.
        return StreamingResponse(
            _stream_level_4_openclaw(
                request.message,
                uid,
                client_metadata,
            ),
            media_type="text/event-stream",
        )

    # Level 0 (production): Direct Grok call as default production path
    print("[FLOW:CHAT-L0] route=grok_direct", flush=True)
    trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
    record_trace(trace)
    return StreamingResponse(
        _stream_level_2_grok(request.message),
        media_type="text/event-stream",
    )


# === Chat History Endpoint (added 2026-03-10 for ellaaicare/ella-ai#301) ===


class EllaChatHistoryRequest(BaseModel):
    """Query params come as Pydantic model for POST, but we use GET with Query."""

    pass


PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = authority_credential("ELLA_PROVISION_API_TOKEN", strip=False)


@router.get("/chat/history")
async def ella_chat_history(
    uid: str = "",
    limit: int = 50,
    before: str = None,
    authenticated_uid: str = Depends(get_exact_firebase_uid),
):
    """Return recent chat/context messages for a user from canonical timeline.

    Canonical timeline is primary. The Mac Mini provision/OpenClaw session
    history path is kept only as a logged migration fallback.

    Returns messages in reverse-chronological order (newest first) in the
    iOS ServerMessage schema format.

    Args:
        uid: Firebase UID (omi_uid)
        limit: Max messages to return (default 50, max 200)
        before: ISO timestamp — only return messages before this time
    """
    _start = _time.time()

    uid = require_matching_firebase_uid(authenticated_uid, uid, feature="Chat history")
    runtime_bound = await runtime_authority_enabled(authenticated_uid)
    if runtime_bound:
        try:
            await resolve_isolated_runtime(authenticated_uid, target_mode="hermes-cloud-chat")
        except ProvisioningError as exc:
            raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc

    limit = min(limit, 200)

    canonical_events = await _fetch_chat_canonical_events(uid, limit=limit, before=before)
    if canonical_events:
        _elapsed = int((_time.time() - _start) * 1000)
        logger.info(
            "[FLOW:HISTORY] uid=%s source=canonical_timeline messages=%s latency=%sms",
            uid,
            len(canonical_events),
            _elapsed,
        )
        return {
            "messages": canonical_events_to_server_messages(canonical_events, limit=limit),
            "hasMore": len(canonical_events) >= limit,
            "source": "canonical_timeline",
            "fallback": False,
        }

    logger.warning(
        "[FLOW:HISTORY] uid=%s source=canonical_timeline empty fallback=provision_openclaw_history_migration",
        uid,
    )

    if runtime_bound:
        return {"messages": [], "hasMore": False, "source": "canonical_timeline_empty", "fallback": False}

    # Resolve user to get their OpenClaw user ID for migration fallback only.
    resolved = await resolve_user_routing(uid)
    if not resolved:
        logger.warning(f"[FLOW:HISTORY] uid={uid} user_not_found")
        return {"messages": [], "hasMore": False, "source": "canonical_timeline_empty", "fallback": False}

    routing = resolved.get("routing")
    if not routing:
        logger.warning(f"[FLOW:HISTORY] uid={uid} no_routing")
        return {"messages": [], "hasMore": False, "source": "canonical_timeline_empty", "fallback": False}

    # Extract the OpenClaw user ID from the workspace path or agent ID
    agent_id = routing.get("agentId", "")
    workspace = routing.get("workspace", "")

    # The provision API uses the OpenClaw userId (e.g., "omi-5agc5ye9...")
    # which is the prefix of the agent ID (e.g., "ella-omi-5agc5ye9...")
    # Extract it from the agent ID: "ella-{userId}" -> "{userId}"
    if agent_id and agent_id.startswith("ella-"):
        openclaw_user_id = agent_id[5:]  # Remove "ella-" prefix
    else:
        openclaw_user_id = agent_id

    # Build query params for Mac Mini provision API
    params = {"limit": limit}
    if before:
        params["before"] = before

    headers = {}
    if PROVISION_API_TOKEN:
        headers["Authorization"] = f"Bearer {PROVISION_API_TOKEN}"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{PROVISION_API_URL}/users/{openclaw_user_id}/history",
                params=params,
                headers=headers,
                timeout=15.0,
            )

        _elapsed = int((_time.time() - _start) * 1000)

        if response.status_code == 200:
            result = response.json()
            msg_count = len(result.get("messages", []))
            logger.warning(
                "[FLOW:HISTORY] uid=%s agent=%s messages=%s latency=%sms source=provision_openclaw_history_migration",
                uid,
                agent_id,
                msg_count,
                _elapsed,
            )
            result["source"] = "provision_openclaw_history_migration"
            result["fallback"] = True
            return result
        elif response.status_code == 404:
            logger.warning(f"[FLOW:HISTORY] uid={uid} agent={agent_id} no_sessions latency={_elapsed}ms")
            return {
                "messages": [],
                "hasMore": False,
                "source": "provision_openclaw_history_migration",
                "fallback": True,
            }
        else:
            logger.error(
                f"[FLOW:HISTORY] uid={uid} agent={agent_id} status={response.status_code} latency={_elapsed}ms"
            )
            return {
                "messages": [],
                "hasMore": False,
                "source": "provision_openclaw_history_migration",
                "fallback": True,
            }

    except httpx.TimeoutException:
        _elapsed = int((_time.time() - _start) * 1000)
        logger.error(f"[FLOW:HISTORY] uid={uid} timeout latency={_elapsed}ms")
        return {"messages": [], "hasMore": False, "source": "provision_openclaw_history_migration", "fallback": True}
    except Exception:
        _elapsed = int((_time.time() - _start) * 1000)
        logger.error(
            "[FLOW:HISTORY] code=ella_legacy_history_unavailable classification=unexpected latency=%sms",
            _elapsed,
        )
        return {"messages": [], "hasMore": False, "source": "provision_openclaw_history_migration", "fallback": True}
