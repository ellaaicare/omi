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
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ella.config import ELLA_CONFIG
from ella.routers.resolve import resolve_user_routing
from ella.routers.trace import RouteTrace, record_trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-chat"])

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_CHAT_MODEL = os.getenv("ELLA_GROK_CHAT_MODEL", "grok-3-mini")

OPENCLAW_URL = os.getenv("OPENCLAW_URL", "http://100.67.113.120:19001")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")

ELLA_SYSTEM_PROMPT = (
    "You are Ella, a warm and caring AI companion for elderly users. "
    "You speak clearly and simply, with patience and warmth. "
    "You help with daily life questions, provide companionship, and gently encourage healthy habits. "
    "Keep responses concise and easy to understand. "
    "If someone seems confused or distressed, respond with extra gentleness and reassurance."
)


class EllaChatRequest(BaseModel):
    uid: str
    message: str
    conversation_id: str = ""


def _resolve_debug_level(header_value: str = None) -> int:
    """Resolve the effective debug level from header override or config."""
    if header_value is not None:
        try:
            level = int(header_value)
            if 0 <= level <= 4:
                return level
            logger.warning(f"[FLOW:CHAT] Invalid debug level in header: {header_value}, using config default")
        except (ValueError, TypeError):
            logger.warning(f"[FLOW:CHAT] Non-integer debug level in header: {header_value}, using config default")
    return ELLA_CONFIG.debug_level


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
    import time as _time
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
                    print(f"[FLOW:CHAT-L2] ERROR provider=xai status={response.status_code} latency={_elapsed}ms", flush=True)
                    error_data = json.dumps({"error": f"Grok API error: {response.status_code}"})
                    yield f"data: {error_data}\n\n"
                    return
                chunk_count = 0
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        chunk_count += 1
                        yield f"{line}\n\n"
                _elapsed = int((_time.time() - _start) * 1000)
                print(f"[FLOW:CHAT-L2] OK provider=xai model={XAI_CHAT_MODEL} chunks={chunk_count} latency={_elapsed}ms", flush=True)
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
    import time as _time
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
                print(f"[FLOW:CHAT-L3] OK provider=n8n status=200 latency={_elapsed}ms reply={reply_preview}", flush=True)
                data = json.dumps({"choices": [{"delta": {"content": reply_text}}]})
                yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
            else:
                print(f"[FLOW:CHAT-L3] ERROR provider=n8n status={response.status_code} latency={_elapsed}ms", flush=True)
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
    import time as _time
    _start = _time.time()

    if not OPENCLAW_GATEWAY_TOKEN:
        print(f"[FLOW:CHAT-L4] ERROR provider=openclaw token_missing=true uid={uid}", flush=True)
        yield "data: Error: OPENCLAW_GATEWAY_TOKEN not configured\n\n"
        return

    # Dynamic agent resolution with tracing
    _l4_trace = RouteTrace()
    _l4_trace.endpoint = "/v1/ella/chat/stream -> Level 4"
    _l4_trace.uid = uid
    _l4_trace.debug_level = 4
    if client_info:
        _l4_trace.client_type = client_info.get("type", "")
        _l4_trace.client_version = client_info.get("version", "")
        _l4_trace.client_ip = client_info.get("ip", "")
        _l4_trace.client_route = client_info.get("route", "")
        _l4_trace.client_headers = client_info.get("headers", {})

    resolved = await resolve_user_routing(uid)
    if resolved and resolved.get("routing"):
        agent_id = resolved["routing"]["agentId"]
        gateway_url = resolved["routing"]["gatewayUrl"]
        session_key = resolved["routing"]["sessionKey"]
        _l4_trace.resolved_agent = agent_id
        _l4_trace.resolved_gateway = gateway_url
        _l4_trace.resolved_session_key = session_key
        _l4_trace.resolve_source = "database"
        print(f"[FLOW:CHAT-L4] provider=openclaw uid={uid} agent={agent_id} gateway={gateway_url} source=database", flush=True)
    else:
        agent_id = "main"
        gateway_url = OPENCLAW_URL
        session_key = f"ella:{uid}"
        _l4_trace.resolved_agent = "main (FALLBACK)"
        _l4_trace.resolved_gateway = gateway_url
        _l4_trace.resolved_session_key = session_key
        _l4_trace.resolve_source = "fallback"
        _l4_trace.notes.append("WARNING: No cluster found, using fallback")
        print(f"[FLOW:CHAT-L4] provider=openclaw uid={uid} agent=main source=FALLBACK (no cluster)", flush=True)

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
                    "messages": [
                        {"role": "user", "content": user_message},
                    ],
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
            print(f"[FLOW:CHAT-L4] ERROR provider=openclaw agent={agent_id} status={response.status_code} latency={_elapsed}ms keepalives={keepalive_count}", flush=True)
            yield f"data: Error: OpenClaw returned {response.status_code}\n\n"
            return

        result = response.json()
        choices = result.get("choices", [])
        reply = choices[0]["message"]["content"] if choices else "No response from OpenClaw"

        # Strip <think>...</think> blocks that some reasoning models include
        reply = re.sub(r'<think>.*?</think>\s*', '', reply, flags=re.DOTALL).strip()

        _l4_trace.openclaw_status = response.status_code
        _l4_trace.openclaw_latency_ms = _elapsed
        _l4_trace.total_latency_ms = _elapsed
        _l4_trace.response_status = 200
        record_trace(_l4_trace)

        reply_preview = reply[:80] if reply else "(empty)"
        print(f"[FLOW:CHAT-L4] OK provider=openclaw agent={agent_id} uid={uid} latency={_elapsed}ms keepalives={keepalive_count} reply={reply_preview}", flush=True)

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
        print(f"[FLOW:CHAT-L4] TIMEOUT provider=openclaw agent={agent_id} latency={_elapsed}ms", flush=True)
        yield "data: Error: OpenClaw request timed out\n\n"
    except Exception as e:
        _elapsed = int((_time.time() - _start) * 1000)
        print(f"[FLOW:CHAT-L4] ERROR provider=openclaw agent={agent_id} error={e} latency={_elapsed}ms", flush=True)
        yield f"data: Error: {str(e)}\n\n"


@router.post("/chat/stream")
async def ella_chat_stream(
    request: EllaChatRequest,
    raw_request: Request,
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
    import time as _time
    _trace_start = _time.time()

    debug_level = _resolve_debug_level(x_ella_debug_level)

    # Comprehensive flow entry log
    client_ip = raw_request.client.host if raw_request.client else "unknown"
    client_type = x_ella_client_type or "unknown"
    print(f"[FLOW:CHAT] uid={request.uid} level={debug_level} client={client_type} ip={client_ip} msg_len={len(request.message)}", flush=True)

    # Create routing trace
    trace = RouteTrace()
    trace.endpoint = "/v1/ella/chat/stream"
    trace.method = "POST"
    trace.client_ip = client_ip
    trace.client_type = x_ella_client_type or ""
    trace.client_version = x_ella_client_version or ""
    trace.client_route = x_ella_route or ""
    trace.uid = request.uid
    trace.debug_level = debug_level
    trace.notes.append(f"message_length={len(request.message)}")
    # Capture all X-Ella-* headers for debugging
    trace.client_headers = {
        k: v for k, v in raw_request.headers.items()
        if k.lower().startswith("x-ella-")
    }

    if debug_level == 1:
        trace.resolved_agent = "N/A (ACK)"
        trace.resolve_source = "level_1_ack"
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_1_ack(request.message),
            media_type="text/event-stream",
        )

    if debug_level == 2:
        trace.resolved_agent = "grok-direct"
        trace.resolve_source = "level_2_grok"
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_2_grok(request.message),
            media_type="text/event-stream",
        )

    if debug_level == 3:
        trace.resolved_agent = "n8n-webhook"
        trace.resolve_source = "level_3_n8n"
        trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
        record_trace(trace)
        return StreamingResponse(
            _stream_level_3_n8n(request.message, request.uid, request.conversation_id),
            media_type="text/event-stream",
        )

    if debug_level == 4:
        # L4 traces are recorded inside _stream_level_4_openclaw with full resolution data
        return StreamingResponse(
            _stream_level_4_openclaw(request.message, request.uid, {
                "type": x_ella_client_type or "",
                "version": x_ella_client_version or "",
                "ip": raw_request.client.host if raw_request.client else "",
                "route": x_ella_route or "",
                "headers": {k: v for k, v in raw_request.headers.items() if k.lower().startswith("x-ella-")},
            }),
            media_type="text/event-stream",
        )

    # Level 0 (production): Direct Grok call as default production path
    print(f"[FLOW:CHAT-L0] uid={request.uid} production path -> grok direct", flush=True)
    trace.resolved_agent = "grok-direct (level 0)"
    trace.resolve_source = "level_0_production"
    trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
    record_trace(trace)
    return StreamingResponse(
        _stream_level_2_grok(request.message),
        media_type="text/event-stream",
    )


# === Chat History Endpoint (added 2026-03-10 for iOS #301) ===

class EllaChatHistoryRequest(BaseModel):
    """Query params come as Pydantic model for POST, but we use GET with Query."""
    pass


PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")


@router.get("/chat/history")
async def ella_chat_history(
    uid: str,
    limit: int = 50,
    before: str = None,
):
    """Return recent chat messages for a user from OpenClaw session history.

    Resolves the user's agent routing, then proxies to the Mac Mini provision
    API which reads JSONL session files directly.

    Returns messages in reverse-chronological order (newest first) in the
    iOS ServerMessage schema format.

    Args:
        uid: Firebase UID (omi_uid)
        limit: Max messages to return (default 50, max 200)
        before: ISO timestamp — only return messages before this time
    """
    import time as _time
    _start = _time.time()

    limit = min(limit, 200)

    # Resolve user to get their OpenClaw user ID
    resolved = await resolve_user_routing(uid)
    if not resolved:
        logger.warning(f"[FLOW:HISTORY] uid={uid} user_not_found")
        return {"messages": [], "hasMore": False}

    routing = resolved.get("routing")
    if not routing:
        logger.warning(f"[FLOW:HISTORY] uid={uid} no_routing")
        return {"messages": [], "hasMore": False}

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
            logger.info(f"[FLOW:HISTORY] uid={uid} agent={agent_id} messages={msg_count} latency={_elapsed}ms")
            return result
        elif response.status_code == 404:
            logger.warning(f"[FLOW:HISTORY] uid={uid} agent={agent_id} no_sessions latency={_elapsed}ms")
            return {"messages": [], "hasMore": False}
        else:
            logger.error(f"[FLOW:HISTORY] uid={uid} agent={agent_id} status={response.status_code} latency={_elapsed}ms")
            return {"messages": [], "hasMore": False}

    except httpx.TimeoutException:
        _elapsed = int((_time.time() - _start) * 1000)
        logger.error(f"[FLOW:HISTORY] uid={uid} timeout latency={_elapsed}ms")
        return {"messages": [], "hasMore": False}
    except Exception as e:
        _elapsed = int((_time.time() - _start) * 1000)
        logger.error(f"[FLOW:HISTORY] uid={uid} error={e} latency={_elapsed}ms")
        return {"messages": [], "hasMore": False}
