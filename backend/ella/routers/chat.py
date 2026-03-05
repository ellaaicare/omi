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
            logger.warning(f"[Ella Chat] Invalid debug level in header: {header_value}, using config default")
        except (ValueError, TypeError):
            logger.warning(f"[Ella Chat] Non-integer debug level in header: {header_value}, using config default")
    return ELLA_CONFIG.debug_level


async def _stream_level_1_ack(user_message: str):
    """Level 1: Immediate ACK response. No LLM call."""
    ack_text = (
        f"Hello! I'm Ella, your AI companion. I received your message: " f"'{user_message}'. (Debug mode - Level 1 ACK)"
    )
    data = json.dumps({"choices": [{"delta": {"content": ack_text}}]})
    yield f"data: {data}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_level_2_grok(user_message: str):
    """Level 2: Direct Grok API call via xAI."""
    if not XAI_API_KEY:
        error_data = json.dumps({"error": "XAI_API_KEY not configured"})
        yield f"data: {error_data}\n\n"
        return

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
                    error_data = json.dumps({"error": f"Grok API error: {response.status_code}"})
                    yield f"data: {error_data}\n\n"
                    return
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield f"{line}\n\n"
        except httpx.TimeoutException:
            error_data = json.dumps({"error": "Grok API timeout"})
            yield f"data: {error_data}\n\n"
        except Exception as e:
            logger.error(f"[Ella Chat] Grok streaming error: {e}")
            error_data = json.dumps({"error": f"Grok streaming error: {str(e)}"})
            yield f"data: {error_data}\n\n"


async def _stream_level_3_n8n(user_message: str, uid: str, conversation_id: str):
    """Level 3: Route through n8n webhook for full pipeline testing."""
    webhook_url = ELLA_CONFIG.n8n_chat_webhook
    if not webhook_url:
        # Fall back to constructing from base URL
        webhook_url = f"{ELLA_CONFIG.n8n_base_url}/webhook/ella-chat"

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

            if response.status_code == 200:
                result = response.json()
                reply_text = result.get("reply", result.get("message", result.get("text", str(result))))
                data = json.dumps({"choices": [{"delta": {"content": reply_text}}]})
                yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
            else:
                error_data = json.dumps(
                    {"error": f"n8n webhook returned {response.status_code}: {response.text[:200]}"}
                )
                yield f"data: {error_data}\n\n"

        except httpx.TimeoutException:
            error_data = json.dumps({"error": "n8n webhook timed out (30s)"})
            yield f"data: {error_data}\n\n"
        except Exception as e:
            logger.error(f"[Ella Chat] n8n webhook error: {e}")
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
    if not OPENCLAW_GATEWAY_TOKEN:
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
    _l4_start = __import__("time").time()

    resolved = await resolve_user_routing(uid)
    if resolved and resolved.get("routing"):
        agent_id = resolved["routing"]["agentId"]
        gateway_url = resolved["routing"]["gatewayUrl"]
        session_key = resolved["routing"]["sessionKey"]
        _l4_trace.resolved_agent = agent_id
        _l4_trace.resolved_gateway = gateway_url
        _l4_trace.resolved_session_key = session_key
        _l4_trace.resolve_source = "database"
        logger.info(f"[Ella Chat] Resolved uid={uid} -> agent={agent_id}, gateway={gateway_url}")
    else:
        agent_id = "main"
        gateway_url = OPENCLAW_URL
        session_key = f"ella:{uid}"
        _l4_trace.resolved_agent = "main (FALLBACK)"
        _l4_trace.resolved_gateway = gateway_url
        _l4_trace.resolved_session_key = session_key
        _l4_trace.resolve_source = "fallback"
        _l4_trace.notes.append("WARNING: No cluster found, using fallback")
        logger.warning(f"[Ella Chat] No cluster for uid={uid}, falling back to openclaw:main")

    # Use asyncio.Task so we can yield keep-alives while waiting
    async def _call_openclaw():
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"{gateway_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
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
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"

    try:
        response = task.result()

        if response.status_code != 200:
            yield f"data: Error: OpenClaw returned {response.status_code}\n\n"
            return

        result = response.json()
        choices = result.get("choices", [])
        reply = choices[0]["message"]["content"] if choices else "No response from OpenClaw"

        # Strip <think>...</think> blocks that some reasoning models include
        reply = re.sub(r'<think>.*?</think>\s*', '', reply, flags=re.DOTALL).strip()

        _l4_trace.openclaw_status = response.status_code
        _l4_trace.openclaw_latency_ms = int((__import__("time").time() - _l4_start) * 1000)
        _l4_trace.total_latency_ms = _l4_trace.openclaw_latency_ms
        _l4_trace.response_status = 200
        record_trace(_l4_trace)
        logger.info(f"[Ella Chat] OpenClaw reply for uid={uid} agent={agent_id}: {reply[:100]}...")

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
        yield "data: Error: OpenClaw request timed out\n\n"
    except Exception as e:
        logger.error(f"[Ella Chat] OpenClaw error: {e}")
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
    logger.info(f"[Ella Chat] uid={request.uid}, debug_level={debug_level}, message_length={len(request.message)}")

    # Create routing trace
    trace = RouteTrace()
    trace.endpoint = "/v1/ella/chat/stream"
    trace.method = "POST"
    trace.client_ip = raw_request.client.host if raw_request.client else ""
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
    trace.resolved_agent = "grok-direct (level 0)"
    trace.resolve_source = "level_0_production"
    trace.total_latency_ms = int((_time.time() - _trace_start) * 1000)
    record_trace(trace)
    return StreamingResponse(
        _stream_level_2_grok(request.message),
        media_type="text/event-stream",
    )
