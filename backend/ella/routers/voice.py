"""
Ella Voice Router - Voice session management for Ella Voice Service.

Endpoints:
- POST /v1/voice/session - Issue session token for V2V connection
- POST /v1/voice/tts - Text-to-speech synthesis (TTS providers only)
- POST /v1/voice/context - Assemble user context for voice proxy at call start
- GET /v1/voice/providers - List available voice providers
- GET /v1/voice/config - Get voice configuration
- GET /v1/voice/health - Health check
- POST /v1/voice/search - Unified cross-source search with role-based privacy

Provider types:
  tts  — Text-to-speech (request/response, POST /v1/voice/tts)
  v2v  — Voice-to-voice (WebSocket, bidirectional audio streaming via /session)
"""

import asyncio
import base64
import hmac
import json
import os
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, List, Literal, Optional

import asyncpg
import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel

from database.conversations import _decrypt_conversation_data
from ella.services.provisioning import ProvisioningError, rollout_enabled
from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_bindings_enabled
from ella.services.voice_honcho import (
    fetch_voice_honcho_context,
    resolve_voice_honcho_target,
    search_voice_honcho,
)
from utils.ella.canonical_context import fetch_canonical_timeline
from utils.other import endpoints as auth

# JWT handling
try:
    import jwt
except ImportError:
    jwt = None

# Auth dependency - not used yet (for future Firebase auth integration)
# from utils.other.storage import verify_firebase_token
verify_firebase_token = None  # Will be implemented when integrating with Firebase auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voice", tags=["voice"])

# Configuration
ELLA_VOICE_ENDPOINT = os.getenv("ELLA_VOICE_ENDPOINT", "wss://voice.ella-ai-care.com/ws")
ELLA_SESSION_SECRET = os.getenv("ELLA_SESSION_SECRET", "")
ELLA_API_BASE = os.getenv("ELLA_API_BASE", "https://api.ella-ai-care.com")
SESSION_EXPIRY_HOURS = int(os.getenv("ELLA_SESSION_EXPIRY_HOURS", "1"))
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_TTS_VOICE_ID = os.getenv("XAI_TTS_VOICE_ID", "eve")
XAI_TTS_LANGUAGE = os.getenv("XAI_TTS_LANGUAGE", "en")
XAI_TTS_OPTIMIZE_STREAMING_LATENCY = int(os.getenv("XAI_TTS_OPTIMIZE_STREAMING_LATENCY", "1"))
INWORLD_API_KEY = os.getenv("INWORLD_API_KEY", "")
ELLA_TTS_URL = os.getenv("ELLA_TTS_URL", "http://100.76.138.56:8930")
ELLA_KOKORO_TTS_URL = os.getenv("ELLA_KOKORO_TTS_URL", "http://100.76.138.56:8931")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")
HERMES_VOICE_MEMORY_URL = os.getenv("HERMES_VOICE_MEMORY_URL", "http://100.76.138.56:8210/v1/voice/memory/lookup")
HERMES_VOICE_MEMORY_TOKEN = os.getenv("HERMES_VOICE_MEMORY_TOKEN", PROVISION_API_TOKEN)
HERMES_PROVISION_API_URL = os.getenv("ELLA_HERMES_PROVISION_API_URL", "http://100.76.138.56:8210")
HERMES_PROVISION_API_TOKEN = os.getenv("ELLA_HERMES_PROVISION_API_TOKEN", "").strip()
VOICE_PROXY_SERVICE_TOKEN = os.getenv("ELLA_VOICE_PROXY_SERVICE_TOKEN", "").strip()
VOICE_PROXY_SERVICE_HEADER = "X-Ella-Voice-Proxy-Token"
VOICE_SESSION_AUDIENCE = "ella-voice-proxy"
VOICE_HONCHO_PROFILE_RESOLUTION_TIMEOUT_SECONDS = float(
    os.getenv("ELLA_VOICE_HONCHO_PROFILE_RESOLUTION_TIMEOUT_SECONDS", "0.35")
)
VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS = float(
    os.getenv("ELLA_VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS", "15")
)
ALLOW_LEGACY_VOICE_SESSION_TOKENS = os.getenv("ELLA_ALLOW_LEGACY_VOICE_SESSION_TOKENS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEFAULT_GATEWAY_URL = os.getenv("OPENCLAW_URL", "http://100.76.138.56:19001")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
_VOICE_HONCHO_PROFILE_NEGATIVE_CACHE: dict[str, float] = {}


def isolated_voice_routing_enabled(uid: Optional[str] = None) -> bool:
    """Keep isolated users off the legacy OpenClaw voice proxy until cutover."""
    return rollout_enabled(
        "ELLA_ISOLATED_VOICE_ROUTING_ENABLED",
        "ELLA_ISOLATED_VOICE_ROUTING_ENABLED_UIDS",
        uid,
    )


@dataclass(frozen=True)
class VoiceProxyPrincipal:
    uid: str
    session_id: str
    provider: str
    voice_mode: str
    isolated_runtime: bool
    scope_kind: Optional[str] = None
    conversation_id: Optional[str] = None
    active_summary_version_id: Optional[str] = None
    can_reinterpret: bool = False


def _voice_proxy_bearer(request: Request) -> str:
    authorization = str(request.headers.get("Authorization") or "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "voice_session_token_required"})
    return authorization[7:].strip()


def authenticate_voice_proxy_request(request: Request, requested_uid: str) -> VoiceProxyPrincipal:
    """Require service auth and bind the body UID to the signed Firebase subject."""
    if not VOICE_PROXY_SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail={"code": "voice_proxy_service_auth_not_configured"})
    presented_service_token = str(request.headers.get(VOICE_PROXY_SERVICE_HEADER) or "")
    if not presented_service_token or not hmac.compare_digest(
        presented_service_token.encode("utf-8"),
        VOICE_PROXY_SERVICE_TOKEN.encode("utf-8"),
    ):
        raise HTTPException(status_code=403, detail={"code": "voice_proxy_service_auth_invalid"})
    if not jwt or not ELLA_SESSION_SECRET:
        raise HTTPException(status_code=503, detail={"code": "voice_session_auth_not_configured"})

    session_token = _voice_proxy_bearer(request)
    try:
        claims = jwt.decode(
            session_token,
            ELLA_SESSION_SECRET,
            algorithms=["HS256"],
            issuer="omi-backend",
            audience=VOICE_SESSION_AUDIENCE,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "sub",
                    "uid",
                    "jti",
                    "voice_mode",
                    "provider",
                    "isolated_runtime",
                ]
            },
        )
    except jwt.MissingRequiredClaimError as exc:
        if not ALLOW_LEGACY_VOICE_SESSION_TOKENS:
            raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"}) from exc
        try:
            claims = jwt.decode(
                session_token,
                ELLA_SESSION_SECRET,
                algorithms=["HS256"],
                options={"require": ["exp", "uid"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as legacy_exc:
            raise HTTPException(status_code=401, detail={"code": "voice_session_expired"}) from legacy_exc
        except jwt.InvalidTokenError as legacy_exc:
            raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"}) from legacy_exc
        if any(key in claims for key in ("sub", "aud", "isolated_runtime")):
            raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"}) from exc
        if claims.get("iss") and claims["iss"] != "omi-backend":
            raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"}) from exc
        subject = str(claims.get("uid") or "").strip()
        if not subject or str(requested_uid or "").strip() != subject:
            raise HTTPException(status_code=403, detail={"code": "voice_session_ownership_mismatch"})
        return VoiceProxyPrincipal(
            uid=subject,
            session_id=str(claims.get("jti") or ""),
            provider=str(claims.get("provider") or ""),
            voice_mode=str(claims.get("voice_mode") or ""),
            isolated_runtime=False,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail={"code": "voice_session_expired"}) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"}) from exc

    subject = str(claims.get("sub") or "").strip()
    claim_uid = str(claims.get("uid") or "").strip()
    requested_uid = str(requested_uid or "").strip()
    if not subject or claim_uid != subject or requested_uid != subject:
        raise HTTPException(status_code=403, detail={"code": "voice_session_ownership_mismatch"})
    if (
        not str(claims.get("jti") or "").strip()
        or not str(claims.get("provider") or "").strip()
        or not str(claims.get("voice_mode") or "").strip()
        or not isinstance(claims.get("isolated_runtime"), bool)
    ):
        raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"})

    scope_kind = claims.get("scope_kind")
    scope_claim_keys = {
        "scope_kind",
        "conversation_id",
        "active_summary_version_id",
        "can_reinterpret",
    }
    if scope_kind is None:
        if any(key in claims for key in scope_claim_keys):
            raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"})
    elif (
        scope_kind != "memory"
        or not str(claims.get("conversation_id") or "").strip()
        or not isinstance(claims.get("active_summary_version_id"), str)
        or not str(claims.get("active_summary_version_id") or "").strip()
        or not isinstance(claims.get("can_reinterpret"), bool)
    ):
        raise HTTPException(status_code=401, detail={"code": "voice_session_invalid"})

    return VoiceProxyPrincipal(
        uid=subject,
        session_id=str(claims.get("jti") or ""),
        provider=str(claims.get("provider") or ""),
        voice_mode=str(claims.get("voice_mode") or ""),
        isolated_runtime=claims.get("isolated_runtime") is True,
        scope_kind=scope_kind,
        conversation_id=str(claims.get("conversation_id") or "").strip() or None,
        active_summary_version_id=(
            str(claims.get("active_summary_version_id"))
            if scope_kind == "memory"
            else None
        ),
        can_reinterpret=claims.get("can_reinterpret") is True,
    )


async def _resolve_voice_runtime(principal: VoiceProxyPrincipal):
    """Resolve an isolated session to the exact active Hermes receipt."""
    bindings_enabled = runtime_bindings_enabled(principal.uid)
    voice_enabled = isolated_voice_routing_enabled(principal.uid)
    if bindings_enabled != voice_enabled:
        raise HTTPException(status_code=409, detail={"code": "voice_runtime_claim_stale"})
    if principal.isolated_runtime != bindings_enabled:
        raise HTTPException(status_code=409, detail={"code": "voice_runtime_claim_stale"})
    if not bindings_enabled:
        return None
    try:
        return await resolve_isolated_runtime(principal.uid)
    except ProvisioningError as exc:
        raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc


async def _resolve_voice_honcho_binding(uid: str, runtime: Any = None):
    """Resolve Honcho without blocking the event loop or failing the voice path."""
    if runtime is not None:
        return resolve_voice_honcho_target(uid, runtime)

    now = time.monotonic()
    if _VOICE_HONCHO_PROFILE_NEGATIVE_CACHE.get(uid, 0) > now:
        return None, "honcho_profile_resolution_cached_unavailable"

    try:
        target, reason = await asyncio.wait_for(
            asyncio.to_thread(resolve_voice_honcho_target, uid, runtime),
            timeout=VOICE_HONCHO_PROFILE_RESOLUTION_TIMEOUT_SECONDS,
        )
        if target:
            _VOICE_HONCHO_PROFILE_NEGATIVE_CACHE.pop(uid, None)
            return target, reason
        _VOICE_HONCHO_PROFILE_NEGATIVE_CACHE[uid] = (
            time.monotonic() + VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS
        )
        return None, reason
    except asyncio.TimeoutError:
        _VOICE_HONCHO_PROFILE_NEGATIVE_CACHE[uid] = (
            time.monotonic() + VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS
        )
        logger.warning(
            "[FLOW:VOICE-HONCHO] target resolution timed out uid=%s timeout=%.3fs",
            uid,
            VOICE_HONCHO_PROFILE_RESOLUTION_TIMEOUT_SECONDS,
        )
        return None, "honcho_profile_resolution_timeout"
    except Exception as exc:
        _VOICE_HONCHO_PROFILE_NEGATIVE_CACHE[uid] = (
            time.monotonic() + VOICE_HONCHO_PROFILE_NEGATIVE_CACHE_TTL_SECONDS
        )
        logger.warning(
            "[FLOW:VOICE-HONCHO] target resolution degraded uid=%s error=%s",
            uid,
            type(exc).__name__,
        )
        return None, "honcho_profile_resolution_unavailable"


def _hermes_workspace_headers(uid: str) -> dict[str, str]:
    if not HERMES_PROVISION_API_TOKEN:
        raise HTTPException(status_code=503, detail={"code": "hermes_service_auth_not_configured"})
    return {
        "Authorization": f"Bearer {HERMES_PROVISION_API_TOKEN}",
        "X-Ella-Owner-Uid": uid,
        "Content-Type": "application/json",
    }


# Database connection pool (lazy-initialized, shared pattern with other Ella routers)
_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database="ella_ai",
            min_size=2,
            max_size=10,
        )
    return _pool


# Voice interaction rules — injected into voice proxy system prompt
VOICE_INTERACTION_RULES = """
Voice Interaction Rules:
- One or two short sentences only. Never three or more.
- Sound like a warm friend chatting, not an AI giving a report.
- Never fabricate information. Say "I'm not sure" if you don't know.
- If a tool returns data, summarize it casually in one sentence.
- When using ask_ella tool, say a brief filler first like "Let me check" before calling.
- Keep things natural and brief.
"""

# V2V provider registry — maps provider ID to endpoint and metadata
V2V_PROVIDERS = {
    "grok-voice": {
        "name": "Grok Voice (V2V)",
        "description": "Voice-to-voice via Grok Realtime API, sub-second latency",
        "default_mode": "v4",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",
        "key_check": lambda: bool(ELLA_SESSION_SECRET),
    },
    "gemini-live": {
        "name": "Gemini Live compatibility (V2V)",
        "description": "Compatibility label for older app builds; use gemini-native-live for provider-native voice",
        "default_mode": "gemini-live",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",  # Same proxy, different mode
        "key_check": lambda: bool(ELLA_SESSION_SECRET),
    },
    "gemini-native-live": {
        "name": "Gemini Native Live (V2V)",
        "description": "Provider-native Gemini Live speech-to-speech through Ella voice proxy",
        "default_mode": "gemini-native-live-v1",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",
        "key_check": lambda: bool(ELLA_SESSION_SECRET),
    },
    "openai-native-realtime": {
        "name": "OpenAI Native Realtime (V2V)",
        "description": "Provider-native OpenAI Realtime speech-to-speech through Ella voice proxy",
        "default_mode": "openai-native-realtime-v1",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",
        "key_check": lambda: bool(ELLA_SESSION_SECRET),
    },
}


# ============================================================================
# Request/Response Models
# ============================================================================


class VoiceSessionScope(BaseModel):
    """Non-authoritative client scope; all memory content is resolved server-side."""

    kind: Literal["memory"]
    conversation_id: str
    expected_active_summary_version_id: Optional[str] = None


class VoiceSessionRequest(BaseModel):
    """Request for creating a V2V voice session."""

    uid: Optional[str] = None
    provider: str = "grok-voice"
    voice_mode: Optional[str] = None
    session_scope: Optional[VoiceSessionScope] = None


class VoiceSessionResponse(BaseModel):
    """Response containing session token and connection details."""

    session_token: str
    voice_endpoint: str
    expires_in: int  # seconds
    audio_format: dict
    provider: str
    voice_mode: str
    session_id: str
    session_scope: Optional[dict] = None


class VoiceConfigResponse(BaseModel):
    """Voice configuration for iOS client."""

    sample_rate: int = 24000
    channels: int = 1
    encoding: str = "pcm_int16"
    byte_order: str = "little_endian"


DEFAULT_ELEVENLABS_VOICE_ID = "pFZP5JQG7iQjIQuC4Bku"


def _iso_value(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _load_voice_memory_scope(uid: str, scope: VoiceSessionScope) -> Optional[dict[str, Any]]:
    """Load one user-owned canonical memory without probing other users."""
    from database import conversations as conversations_db
    from database import users as users_db

    conversation_id = str(scope.conversation_id or "").strip()
    if not conversation_id:
        return None
    version_result = conversations_db.ensure_voice_memory_summary_version(
        uid,
        conversation_id,
        scope.expected_active_summary_version_id,
    )
    status = str(version_result.get("status") or "")
    if status == "stale":
        raise ValueError("voice_session_scope_stale")
    if status == "version_unavailable":
        raise ValueError("voice_session_scope_version_unavailable")
    if status == "not_found":
        return None
    conversation = version_result.get("conversation")
    if not isinstance(conversation, dict):
        raise RuntimeError("voice_session_scope_invalid_result")

    active_version_id = str(conversation.get("active_summary_version_id") or "")
    if not active_version_id:
        raise ValueError("voice_session_scope_version_unavailable")

    structured = conversation.get("structured")
    structured = structured if isinstance(structured, dict) else {}
    person_ids: list[str] = []
    for segment in conversation.get("transcript_segments") or []:
        if not isinstance(segment, dict):
            continue
        person_id = str(segment.get("person_id") or "").strip()
        if person_id and person_id not in person_ids:
            person_ids.append(person_id)
    people: list[str] = []
    if person_ids:
        try:
            for person in users_db.get_people_by_ids(uid, person_ids):
                if not isinstance(person, dict):
                    continue
                name = str(person.get("name") or "").strip()
                if name and name not in people:
                    people.append(name)
        except Exception as exc:
            logger.warning(
                "[FLOW:VOICE-SCOPE] people lookup degraded uid=%s conversation=%s error=%s",
                uid,
                conversation_id,
                type(exc).__name__,
            )

    occurred_at = (
        conversation.get("started_at")
        or conversation.get("created_at")
        or conversation.get("finished_at")
    )
    title = str(structured.get("title") or "Untitled memory").strip()
    overview = str(structured.get("overview") or "").strip()
    topic_query = " ".join(
        part
        for part in (
            title,
            overview[:700],
            " ".join(people[:12]),
            _iso_value(occurred_at)[:10],
        )
        if part
    )[:1200]

    return {
        "kind": "memory",
        "conversation_id": conversation_id,
        "active_summary_version_id": active_version_id,
        "can_reinterpret": not bool(conversation.get("is_locked")),
        "title": title[:300],
        "overview": overview[:1600],
        "emoji": str(structured.get("emoji") or "")[:16],
        "category": str(structured.get("category") or "")[:80],
        "people": people[:12],
        "occurred_at": _iso_value(occurred_at),
        "topic_query": topic_query,
        "instruction": (
            "This is a memory-scoped conversation. Help the user reminisce and "
            "answer questions normally. Do not treat every statement as a correction. "
            "Memory changes are considered asynchronously after the session."
        ),
    }


async def _resolve_voice_memory_scope(uid: str, scope: VoiceSessionScope) -> dict[str, Any]:
    try:
        resolved = await asyncio.to_thread(_load_voice_memory_scope, uid, scope)
    except ValueError as exc:
        if str(exc) == "voice_session_scope_stale":
            raise HTTPException(status_code=409, detail={"code": "voice_session_scope_stale"}) from exc
        if str(exc) == "voice_session_scope_version_unavailable":
            raise HTTPException(
                status_code=409,
                detail={"code": "voice_session_scope_version_unavailable"},
            ) from exc
        raise
    except Exception as exc:
        logger.warning(
            "[FLOW:VOICE-SCOPE] lookup unavailable uid=%s conversation=%s error=%s",
            uid,
            scope.conversation_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail={"code": "voice_session_scope_unavailable"}) from exc
    if not resolved:
        # Missing and non-owned IDs intentionally share one response.
        raise HTTPException(status_code=404, detail={"code": "voice_session_scope_not_found"})
    return resolved


async def _resolve_principal_memory_scope(principal: VoiceProxyPrincipal) -> Optional[dict[str, Any]]:
    if principal.scope_kind != "memory":
        return None
    resolved = await _resolve_voice_memory_scope(
        principal.uid,
        VoiceSessionScope(
            kind="memory",
            conversation_id=principal.conversation_id or "",
            expected_active_summary_version_id=principal.active_summary_version_id,
        ),
    )
    if resolved["can_reinterpret"] != principal.can_reinterpret:
        raise HTTPException(status_code=409, detail={"code": "voice_session_scope_stale"})
    return resolved


class TtsRequest(BaseModel):
    """Request for text-to-speech synthesis."""

    text: str
    voice_id: str = DEFAULT_ELEVENLABS_VOICE_ID  # Lily voice


# ============================================================================
# Helper Functions
# ============================================================================


async def _inworld_tts(text: str, api_key: str, voice_id: str = "Serena") -> bytes:
    """Call Inworld TTS via HTTP streaming endpoint, return MP3 bytes.

    Uses POST /tts/v1/voice:stream (~0.8-1.1s) instead of WebSocket per-request (~3s).
    Override defaults via env vars:
      INWORLD_VOICE_ID     — voice name (default: Serena)
      INWORLD_SPEAKING_RATE — float 0.5-1.5 (default: 1.1)
    Full voice list: Luna, Claire, Evelyn, Serena, Riley, Chloe, Julia, Abby, Tessa,
                     Kayla, Kelsey, Mia, Naomi, Olivia, Jessica, Lauren, Ashley,
                     Dennis, Blake, Alex, Avery (see GET /tts/v1/voices for full list).
    """
    voice = os.getenv("INWORLD_VOICE_ID", voice_id)
    payload = {
        "text": text,
        "voiceId": voice,
        "modelId": "inworld-tts-1.5-mini",
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": float(os.getenv("INWORLD_SPEAKING_RATE", "1.1")),
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.inworld.ai/tts/v1/voice:stream",
            headers={"Authorization": f"Basic {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Inworld API error {response.status_code}: {response.text[:200]}")

    # Response is newline-delimited JSON chunks, each with base64 audioContent
    audio_chunks = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            audio_b64 = msg.get("result", {}).get("audioContent")
            if audio_b64:
                audio_chunks.append(base64.b64decode(audio_b64))
        except (json.JSONDecodeError, KeyError):
            continue

    if not audio_chunks:
        raise RuntimeError("Inworld returned no audio chunks")
    return b"".join(audio_chunks)


def create_session_token(
    uid: str,
    firebase_uid: str,
    display_name: Optional[str] = None,
    voice_mode: str = "v3-rich",
    provider: str = "grok-voice",
    isolated_runtime: bool = False,
    session_id: Optional[str] = None,
    session_scope: Optional[dict[str, Any]] = None,
) -> str:
    """
    Create a JWT session token for V2V connection.

    Token payload includes:
    - uid: Omi user ID
    - firebase_uid: Firebase UID for user lookup
    - name: Display name for personalization
    - voice_mode: V2V mode (v3-rich, gemini-live, etc.)
    - provider: V2V provider ID
    - context_url: Where proxy can fetch user context
    - callback_url: Where proxy posts session results
    - exp: Expiration time
    - iss: Issuer (omi-backend)
    """
    if not jwt:
        raise HTTPException(status_code=500, detail="JWT library not available")

    if not ELLA_SESSION_SECRET:
        raise HTTPException(status_code=500, detail="Session secret not configured")

    payload = {
        "sub": firebase_uid,
        "uid": uid,
        "firebase_uid": firebase_uid,
        "name": display_name or "User",
        "voice_mode": voice_mode,
        "provider": provider,
        "isolated_runtime": isolated_runtime,
        "context_url": f"{ELLA_API_BASE}/v1/users/{uid}/context",
        "callback_url": f"{ELLA_API_BASE}/v1/ella/voice-session",
        "aud": VOICE_SESSION_AUDIENCE,
        "jti": session_id or str(uuid.uuid4()),
        "exp": datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS),
        "iat": datetime.utcnow(),
        "iss": "omi-backend",
    }
    if session_scope:
        active_summary_version_id = str(session_scope.get("active_summary_version_id") or "").strip()
        if not active_summary_version_id:
            raise ValueError("memory scope requires an active summary version")
        payload.update(
            {
                "scope_kind": session_scope["kind"],
                "conversation_id": session_scope["conversation_id"],
                "active_summary_version_id": active_summary_version_id,
                "can_reinterpret": bool(session_scope["can_reinterpret"]),
            }
        )

    return jwt.encode(payload, ELLA_SESSION_SECRET, algorithm="HS256")


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/providers")
async def get_voice_providers():
    """
    List available voice providers for iOS settings toggle.

    Provider types:
      tts  — Text-to-speech (request/response, POST /v1/voice/tts)
      v2v  — Voice-to-voice (WebSocket, bidirectional audio streaming)
    """
    providers = [
        {
            "id": "elevenlabs",
            "name": "ElevenLabs",
            "type": "tts",
            "description": "Cloud TTS, Lily voice",
            "available": bool(ELEVENLABS_API_KEY),
        },
        {
            "id": "kokoro",
            "name": "Kokoro (local)",
            "type": "tts",
            "description": "Local TTS on Mac Mini, fast",
            "available": True,
        },
        {
            "id": "fish-audio-s2",
            "name": "Fish Audio S2",
            "type": "tts",
            "description": "Fish Audio via local server",
            "available": True,
        },
        {
            "id": "inworld",
            "name": "Inworld TTS",
            "type": "tts",
            "description": "Inworld TTS WebSocket, ~120-200ms, $5-10/1M chars",
            "available": bool(INWORLD_API_KEY),
        },
        {
            "id": "xai-tts",
            "name": "xAI TTS",
            "type": "tts",
            "description": "Grok/xAI one-shot TTS for queued Guardian playback",
            "available": bool(XAI_API_KEY),
        },
        {
            "id": "grok-voice",
            "name": "Grok Voice (V2V)",
            "type": "v2v",
            "description": "Voice-to-voice via Grok Realtime API, sub-second latency",
            "available": V2V_PROVIDERS["grok-voice"]["key_check"](),
            "requires_session": True,
            "session_endpoint": "/v1/voice/session",
        },
        {
            "id": "gemini-live",
            "name": "Gemini Live compatibility (V2V)",
            "type": "v2v",
            "description": "Compatibility label for older app builds",
            "available": V2V_PROVIDERS["gemini-live"]["key_check"](),
            "requires_session": True,
            "session_endpoint": "/v1/voice/session",
        },
        {
            "id": "gemini-native-live",
            "name": "Gemini Native Live (V2V)",
            "type": "v2v",
            "description": "Provider-native Gemini Live speech-to-speech",
            "available": V2V_PROVIDERS["gemini-native-live"]["key_check"](),
            "requires_session": True,
            "session_endpoint": "/v1/voice/session",
        },
        {
            "id": "openai-native-realtime",
            "name": "OpenAI Native Realtime (V2V)",
            "type": "v2v",
            "description": "Provider-native OpenAI Realtime speech-to-speech",
            "available": V2V_PROVIDERS["openai-native-realtime"]["key_check"](),
            "requires_session": True,
            "session_endpoint": "/v1/voice/session",
        },
    ]
    return {"providers": providers}


@router.post("/session", response_model=VoiceSessionResponse)
async def create_voice_session(
    body: Optional[VoiceSessionRequest] = None,
    uid: Optional[str] = None,
    provider: Optional[str] = None,
    voice_mode: Optional[str] = None,
    authenticated_uid: str = Depends(auth.get_current_user_uid),
):
    """
    Create a voice session token for connecting to V2V service.

    Accepts params via JSON body (iOS) or query params (curl/testing).
    Body fields take priority over query params.

    Flow:
    1. iOS calls this endpoint with uid and provider
    2. Backend validates and issues short-lived JWT
    3. iOS connects to voice_endpoint with: ?token=<jwt>&mode=<voice_mode>
    4. Proxy validates token and starts session with the right provider

    Args:
        uid: User ID (required)
        provider: V2V provider — "grok-voice" (default) or "gemini-live"
        voice_mode: Override mode (defaults to provider's default mode)

    Returns:
        session_token: JWT for proxy authentication
        voice_endpoint: WebSocket URL to connect to (with mode query param)
        provider: Which V2V provider this session uses
        voice_mode: The mode that will be used
        expires_in: Token lifetime in seconds
        audio_format: Required audio configuration
    """
    # Merge body and query params (body takes priority)
    requested_uid = (body.uid if body else None) or uid
    if requested_uid and requested_uid != authenticated_uid:
        raise HTTPException(status_code=403, detail={"code": "ownership_mismatch"})
    uid = authenticated_uid
    provider = (body.provider if body else None) or provider or "grok-voice"
    voice_mode = (body.voice_mode if body else None) or voice_mode
    requested_scope = body.session_scope if body else None

    runtime_bound = runtime_bindings_enabled(uid)
    voice_rollout_enabled = isolated_voice_routing_enabled(uid)
    if voice_rollout_enabled and not runtime_bound:
        raise HTTPException(status_code=503, detail={"code": "isolated_voice_runtime_required"})
    isolated_runtime = runtime_bound and voice_rollout_enabled
    if runtime_bound:
        try:
            await resolve_isolated_runtime(uid)
        except ProvisioningError as exc:
            raise HTTPException(status_code=503 if exc.retryable else 409, detail={"code": exc.code}) from exc
        if not isolated_runtime:
            # Runtime-bound users must never fall through to legacy voice while
            # their explicit isolated-voice rollout gate remains disabled.
            raise HTTPException(status_code=503, detail={"code": "isolated_voice_not_ready"})
    memory_scope = (
        await _resolve_voice_memory_scope(uid, requested_scope)
        if requested_scope
        else None
    )

    # Validate provider
    if provider not in V2V_PROVIDERS:
        valid = list(V2V_PROVIDERS.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown V2V provider: {provider!r}. Valid: {valid}",
        )

    provider_info = V2V_PROVIDERS[provider]

    # Check provider availability
    if not provider_info["key_check"]():
        raise HTTPException(
            status_code=503,
            detail=f"Provider {provider!r} is not configured (missing API key)",
        )

    # Resolve voice mode
    resolved_mode = voice_mode or provider_info["default_mode"]

    _start = time.time()

    # Look up user display name from DB
    user_display_name = None
    try:
        pool = await _get_pool()
        row = await pool.fetchrow(
            "SELECT name FROM users WHERE omi_uid = $1", uid
        )
        if row:
            user_display_name = row["name"]
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-SESSION] name lookup failed for {uid}: {e}")

    try:
        session_id = str(uuid.uuid4())
        token = create_session_token(
            uid=uid,
            firebase_uid=uid,
            display_name=user_display_name,
            voice_mode=resolved_mode,
            provider=provider,
            isolated_runtime=isolated_runtime,
            session_id=session_id,
            session_scope=memory_scope,
        )

        # Build endpoint URL with mode query param
        endpoint = os.getenv(provider_info["endpoint_env"], ELLA_VOICE_ENDPOINT)
        endpoint_with_mode = f"{endpoint}?mode={resolved_mode}"

        _elapsed = int((time.time() - _start) * 1000)
        print(
            f"[FLOW:VOICE-SESSION] uid={uid} provider={provider} mode={resolved_mode} "
            f"endpoint={endpoint} expiry={SESSION_EXPIRY_HOURS}h latency={_elapsed}ms",
            flush=True,
        )

        return VoiceSessionResponse(
            session_token=token,
            voice_endpoint=endpoint_with_mode,
            expires_in=SESSION_EXPIRY_HOURS * 3600,
            audio_format={
                "sample_rate": 24000,
                "channels": 1,
                "encoding": "pcm_int16",
                "byte_order": "little_endian",
            },
            provider=provider,
            voice_mode=resolved_mode,
            session_id=session_id,
            session_scope=(
                {
                    "kind": memory_scope["kind"],
                    "conversation_id": memory_scope["conversation_id"],
                    "active_summary_version_id": memory_scope["active_summary_version_id"],
                    "can_reinterpret": memory_scope["can_reinterpret"],
                }
                if memory_scope
                else None
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FLOW:VOICE-SESSION] uid={uid} provider={provider} error={e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config", response_model=VoiceConfigResponse)
async def get_voice_config():
    """
    Get audio configuration for V2V voice sessions.

    iOS must use these exact settings for compatibility:
    - 24kHz sample rate
    - Mono channel
    - PCM Int16 encoding
    - Little-endian byte order
    """
    return VoiceConfigResponse(sample_rate=24000, channels=1, encoding="pcm_int16", byte_order="little_endian")


@router.post("/tts")
async def synthesize_speech(
    request: TtsRequest,
    x_tts_provider: Optional[str] = Header(default=None, alias="X-TTS-Provider"),
):
    """
    Synthesize speech from text.

    Routes to TTS backend based on X-TTS-Provider header:
      elevenlabs         — ElevenLabs API (default)
      fish-audio         — Fish Audio via local ella-tts server (Mac Mini :8930)
      fish-audio-s1      — Fish Audio S1 (alias)
      fish-audio-s2      — Fish Audio S2 (alias)
      kokoro             — Kokoro-82M via local ella-tts server (Mac Mini :8930)
      xai-tts            — xAI TTS REST API, useful for Grok-matched Guardian one-shots
      inworld            — Inworld TTS WebSocket (~120-200ms, $5-10/1M chars)

    V2V providers (grok-voice, gemini-live) return 422 directing iOS to use
    the /session endpoint instead — V2V replaces the entire STT→LLM→TTS chain.
    """
    _start = time.time()
    text_len = len(request.text)
    provider = (x_tts_provider or "elevenlabs").lower()
    text = request.text[:500]  # Cap at 500 chars
    print(f"[FLOW:VOICE-TTS] header=X-TTS-Provider raw={x_tts_provider!r} resolved={provider}", flush=True)

    # --- V2V providers — redirect to session flow ---
    if provider in V2V_PROVIDERS:
        print(f"[FLOW:VOICE-TTS] {provider} requested — redirect to session flow", flush=True)
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"{provider} uses WebSocket V2V, not TTS",
                "use_endpoint": "/v1/voice/session",
                "provider_type": "v2v",
                "provider": provider,
            },
        )

    # --- Local ella-tts proxy (Fish Audio or Kokoro) ---
    if provider in ("fish-audio", "fish-audio-s1", "fish-audio-s2", "kokoro"):
        local_tts_url = ELLA_KOKORO_TTS_URL if provider == "kokoro" else ELLA_TTS_URL
        print(f"[FLOW:VOICE-TTS] provider={provider} text_len={text_len} url={local_tts_url}", flush=True)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{local_tts_url}/v1/audio/speech",
                    json={"model": provider, "input": text, "voice": "nova", "response_format": "mp3"},
                    timeout=30.0,
                )
            _elapsed = int((time.time() - _start) * 1000)
            if response.status_code != 200:
                print(f"[FLOW:VOICE-TTS] ERROR provider={provider} status={response.status_code} latency={_elapsed}ms", flush=True)
                raise HTTPException(status_code=502, detail=f"ella-tts error: {response.status_code}")
            audio_size = len(response.content)
            print(f"[FLOW:VOICE-TTS] OK provider={provider} audio_bytes={audio_size} latency={_elapsed}ms", flush=True)
            return Response(content=response.content, media_type=response.headers.get("content-type", "audio/mpeg"))
        except httpx.TimeoutException:
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] TIMEOUT provider={provider} latency={_elapsed}ms", flush=True)
            raise HTTPException(status_code=504, detail=f"{provider} TTS timed out")
        except HTTPException:
            raise
        except Exception as e:
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] ERROR provider={provider} error={e} latency={_elapsed}ms", flush=True)
            raise HTTPException(status_code=500, detail=str(e))

    # --- xAI TTS (Grok one-shot audio, REST) ---
    if provider == "xai-tts":
        if not XAI_API_KEY:
            print(f"[FLOW:VOICE-TTS] ERROR provider=xai-tts key_missing=true", flush=True)
            raise HTTPException(status_code=500, detail="XAI_API_KEY not configured")
        voice_id = request.voice_id if request.voice_id != DEFAULT_ELEVENLABS_VOICE_ID else XAI_TTS_VOICE_ID
        print(f"[FLOW:VOICE-TTS] provider=xai-tts voice={voice_id} language={XAI_TTS_LANGUAGE} text_len={text_len}", flush=True)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.x.ai/v1/tts",
                    headers={
                        "Authorization": f"Bearer {XAI_API_KEY}",
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json={
                        "text": text,
                        "voice_id": voice_id,
                        "language": XAI_TTS_LANGUAGE,
                        "codec": "mp3",
                        "optimize_streaming_latency": XAI_TTS_OPTIMIZE_STREAMING_LATENCY,
                        "text_normalization": True,
                    },
                    timeout=30.0,
                )
            _elapsed = int((time.time() - _start) * 1000)
            if response.status_code != 200:
                print(
                    f"[FLOW:VOICE-TTS] ERROR provider=xai-tts status={response.status_code} "
                    f"latency={_elapsed}ms body={response.text[:200]}",
                    flush=True,
                )
                raise HTTPException(status_code=502, detail=f"xAI TTS error: {response.status_code}")
            print(
                f"[FLOW:VOICE-TTS] OK provider=xai-tts voice={voice_id} audio_bytes={len(response.content)} latency={_elapsed}ms",
                flush=True,
            )
            return Response(content=response.content, media_type=response.headers.get("content-type", "audio/mpeg"))
        except httpx.TimeoutException:
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] TIMEOUT provider=xai-tts latency={_elapsed}ms", flush=True)
            raise HTTPException(status_code=504, detail="xAI TTS timed out")
        except HTTPException:
            raise
        except Exception as e:
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] ERROR provider=xai-tts error={e} latency={_elapsed}ms", flush=True)
            raise HTTPException(status_code=500, detail=str(e))

    # --- Inworld TTS (WebSocket, ~120-200ms) ---
    if provider == "inworld":
        if not INWORLD_API_KEY:
            print(f"[FLOW:VOICE-TTS] ERROR provider=inworld key_missing=true", flush=True)
            raise HTTPException(status_code=500, detail="INWORLD_API_KEY not configured")
        print(f"[FLOW:VOICE-TTS] provider=inworld model=tts-1.5-mini text_len={text_len}", flush=True)
        try:
            audio = await _inworld_tts(text, INWORLD_API_KEY)
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] OK provider=inworld audio_bytes={len(audio)} latency={_elapsed}ms", flush=True)
            return Response(content=audio, media_type="audio/mpeg")
        except Exception as e:
            _elapsed = int((time.time() - _start) * 1000)
            print(f"[FLOW:VOICE-TTS] ERROR provider=inworld error={e} latency={_elapsed}ms", flush=True)
            raise HTTPException(status_code=502, detail=f"Inworld TTS error: {e}")

    # --- Reject unknown providers (no silent fallbacks) ---
    if provider != "elevenlabs":
        print(f"[FLOW:VOICE-TTS] ERROR unknown provider={provider!r}", flush=True)
        raise HTTPException(status_code=400, detail=f"Unknown TTS provider: {provider!r}. Valid: elevenlabs, fish-audio, fish-audio-s1, fish-audio-s2, kokoro, inworld, xai-tts, grok-voice, gemini-live")

    # --- ElevenLabs ---
    if not ELEVENLABS_API_KEY:
        print(f"[FLOW:VOICE-TTS] ERROR provider=elevenlabs key_missing=true text_len={text_len}", flush=True)
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY not configured")

    print(f"[FLOW:VOICE-TTS] provider=elevenlabs voice={request.voice_id} text_len={text_len}", flush=True)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{request.voice_id}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=30.0,
            )

        _elapsed = int((time.time() - _start) * 1000)

        if response.status_code != 200:
            print(f"[FLOW:VOICE-TTS] ERROR provider=elevenlabs status={response.status_code} latency={_elapsed}ms body={response.text[:200]}", flush=True)
            raise HTTPException(status_code=502, detail=f"ElevenLabs error: {response.status_code}")

        audio_size = len(response.content)
        print(f"[FLOW:VOICE-TTS] OK provider=elevenlabs voice={request.voice_id} audio_bytes={audio_size} latency={_elapsed}ms", flush=True)

        return Response(content=response.content, media_type="audio/mpeg")

    except httpx.TimeoutException:
        _elapsed = int((time.time() - _start) * 1000)
        print(f"[FLOW:VOICE-TTS] TIMEOUT provider=elevenlabs latency={_elapsed}ms", flush=True)
        raise HTTPException(status_code=504, detail="ElevenLabs TTS timed out")
    except HTTPException:
        raise
    except Exception as e:
        _elapsed = int((time.time() - _start) * 1000)
        print(f"[FLOW:VOICE-TTS] ERROR provider=elevenlabs error={e} latency={_elapsed}ms", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def voice_health():
    """Health check for voice endpoints."""
    v2v_status = {
        pid: {
            "available": info["key_check"](),
            "default_mode": info["default_mode"],
        }
        for pid, info in V2V_PROVIDERS.items()
    }

    return {
        "status": "ok",
        "service": "ella-voice",
        "voice_endpoint": ELLA_VOICE_ENDPOINT,
        "session_secret_configured": bool(ELLA_SESSION_SECRET),
        "tts_providers": ["elevenlabs", "fish-audio", "kokoro", "inworld", "xai-tts"],
        "tts_elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "tts_xai_configured": bool(XAI_API_KEY),
        "tts_local_url": ELLA_TTS_URL,
        "v2v_providers": v2v_status,
    }


# ============================================================================
# Voice Context Assembly
# ============================================================================




async def _fetch_recent_conversations(uid: str, limit: int = 5) -> str:
    """Fetch recent OMI conversation summaries from Firestore for voice context.
    Returns formatted string of recent conversations (title + overview)."""
    try:
        # Direct Firestore query — simpler than get_conversations() to avoid
        # composite index requirements (discarded + created_at needs index)
        from google.cloud import firestore
        db = firestore.Client()
        convos_ref = (
            db.collection("users").document(uid).collection("conversations")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        convos = [doc.to_dict() for doc in convos_ref.stream()]
        
        if not convos:
            return ""
        
        parts = []
        for c in convos:
            structured = c.get("structured", {})
            title = structured.get("title", "Untitled")
            overview = structured.get("overview", "")
            emoji = structured.get("emoji", "")
            created = c.get("created_at")
            
            # Format timestamp
            ts = ""
            if created:
                if hasattr(created, 'strftime'):
                    ts = created.strftime("%b %d %I:%M %p")
                else:
                    ts = str(created)[:16]
            
            entry = f"- {emoji} {title}"
            if overview:
                # Truncate long overviews
                overview_short = overview[:200] + "..." if len(overview) > 200 else overview
                entry += f": {overview_short}"
            if ts:
                entry += f" ({ts})"
            parts.append(entry)
        
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Conversation fetch failed: {e}")
        return ""


async def _fetch_memory_context(
    gateway_url: str,
    gateway_token: str,
    user_name: str = "user",
    timeout_seconds: float = 2.0,
) -> str:
    """Fetch recent memory/agent context via OpenClaw memory_search.
    Returns formatted string of relevant memory snippets."""
    if not gateway_token:
        return ""
    
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "Content-Type": "application/json",
    }
    
    results_all = []
    queries = [f"{user_name} recent activity today schedule important"]
    
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            for query in queries:
                try:
                    resp = await client.post(
                        f"{gateway_url}/tools/invoke",
                        headers=headers,
                        json={"tool": "memory_search", "args": {"query": query}},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("ok"):
                            import json as json_mod
                            results_text = data["result"]["content"][0]["text"]
                            results = json_mod.loads(results_text).get("results", [])
                            for r in results[:3]:
                                if r.get("score", 0) > 0.3:
                                    snippet = r.get("snippet", "")[:300]
                                    if snippet and snippet not in [s for s, _ in results_all]:
                                        results_all.append((snippet, r.get("score", 0)))
                except Exception as e:
                    logger.debug(f"[FLOW:VOICE-CONTEXT] Memory search query failed: {e}")
                    continue
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Memory context fetch failed: {e}")
        return ""
    
    if not results_all:
        return ""
    
    # Sort by score, deduplicate, take top 5
    results_all.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    unique = []
    for snippet, score in results_all:
        key = snippet[:80]
        if key not in seen:
            seen.add(key)
            unique.append(snippet)
        if len(unique) >= 5:
            break
    
    return "\n---\n".join(unique)

def _extract_caregiver_section(user_profile: str) -> str:
    """Extract caregiver-relevant notes from USER.md content."""
    if not user_profile:
        return ""
    # Look for caregiver section header
    sections = user_profile.split("##")
    for section in sections:
        if any(kw in section.lower() for kw in ["caregiver", "care team", "family"]):
            return section.strip()[:500]
    return ""


@router.post("/context")
async def get_voice_context(request: Request):
    """
    Assemble voice context for a user from postgres + provision API workspace files.
    Called by the voice proxy at session start to build the system prompt.

    Request body:
        uid (str, required): OMI user ID (Firebase UID / omi_uid)
        context_budget (int, optional): Max chars for context sections (default: 8000)
        memory_timeout_seconds (float, optional): Best-effort OpenClaw memory timeout (default: 2.0)

    Returns structured context: user info, soul, user profile, conditions,
    medications, recent voice summaries, dynamic rules, and voice rules.
    """
    body = await request.json()
    uid = body.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")
    principal = authenticate_voice_proxy_request(request, uid)
    uid = principal.uid
    runtime = await _resolve_voice_runtime(principal)
    memory_scope = await _resolve_principal_memory_scope(principal)
    honcho_target, honcho_target_reason = await _resolve_voice_honcho_binding(uid, runtime)

    budget = body.get("context_budget", 8000)
    memory_timeout_seconds = float(body.get("memory_timeout_seconds", 2.0))
    _start = time.time()

    # 1. DB lookup. Isolated users never resolve routing through agent_clusters.
    pool = await _get_pool()
    try:
        if runtime:
            row = await pool.fetchrow(
                """
                SELECT u.id, u.name, u.conditions, u.medications, u.guardian_mode
                FROM users u
                WHERE u.omi_uid = $1
                """,
                uid,
            )
        else:
            row = await pool.fetchrow(
                """
                SELECT u.id, u.name, u.conditions, u.medications, u.guardian_mode,
                       ac.agents
                FROM users u
                LEFT JOIN agent_clusters ac ON ac.user_id = u.id
                WHERE u.omi_uid = $1
                """,
                uid,
            )
    except Exception as e:
        logger.error(f"[FLOW:VOICE-CONTEXT] DB error for uid={uid}: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"User not found for uid: {uid}")

    # Parse agents JSONB (asyncpg may return as str or dict)
    agents_raw = row.get("agents") if hasattr(row, "get") else None
    if isinstance(agents_raw, str):
        agents = json.loads(agents_raw)
    elif isinstance(agents_raw, dict):
        agents = agents_raw
    else:
        agents = {}

    if runtime:
        agent_id = runtime.agent_id
        gateway_url = ""
        gateway_token = ""
        workspace_api_url = HERMES_PROVISION_API_URL
        workspace_headers = _hermes_workspace_headers(uid)
    else:
        agent_id = agents.get("userAgentId", "")
        gateway_url = agents.get("gatewayUrl", DEFAULT_GATEWAY_URL)
        gateway_token = agents.get("gatewayToken", OPENCLAW_GATEWAY_TOKEN)
        workspace_api_url = PROVISION_API_URL
        workspace_headers = {
            "Authorization": f"Bearer {PROVISION_API_TOKEN}",
            "Content-Type": "application/json",
        }

    # 2. Read workspace files from provision API
    files = {}
    if agent_id and workspace_headers.get("Authorization") != "Bearer ":
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{workspace_api_url}/workspace/{agent_id}/files",
                    headers=workspace_headers,
                )
                if resp.status_code == 200:
                    file_list = resp.json().get("files", [])
                    files = {f.get("name", f.get("filename", "")): f.get("content", "") for f in file_list}
                    logger.info(f"[FLOW:VOICE-CONTEXT] Loaded {len(files)} workspace files " f"for agent={agent_id}")
                else:
                    logger.warning(
                        f"[FLOW:VOICE-CONTEXT] Provision API returned " f"{resp.status_code} for agent={agent_id}"
                    )
        except Exception as e:
            logger.warning(f"[FLOW:VOICE-CONTEXT] Provision API error: {e}")

    # 3. Read dynamic scanner rules
    dynamic_rules = []
    try:
        rules = await pool.fetch(
            """
            SELECT rule_text FROM guardian_dynamic_rules
            WHERE uid = $1 AND is_active = true
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            uid,
        )
        dynamic_rules = [r["rule_text"] for r in rules]
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Dynamic rules error: {e}")

    # 4. Fetch recent OMI summaries, canonical cross-channel timeline, and
    # best-effort agent memory. The canonical timeline is the most important
    # realtime voice context because it includes iMessage, app chat, voice, and
    # OMI-derived events in chronological order.
    recent_conversations = ""
    recent_timeline = ""
    memory_context = ""
    honcho_context_result: dict[str, Any] = {
        "available": False,
        "reason": honcho_target_reason or "missing_companion_honcho_target",
        "context": "",
    }
    try:
        conv_task = asyncio.create_task(_fetch_recent_conversations(uid, limit=8))
        timeline_task = asyncio.create_task(
            _fetch_recent_canonical_timeline(
                uid,
                limit=40,
                max_chars=9000,
                scope_kind=principal.scope_kind,
                conversation_id=principal.conversation_id,
            )
        )
        if runtime:
            mem_task = asyncio.create_task(asyncio.sleep(0, result=""))
        else:
            mem_task = asyncio.create_task(
                _fetch_memory_context(
                    gateway_url,
                    gateway_token,
                    row["name"] or "user",
                    timeout_seconds=memory_timeout_seconds,
                )
            )
        honcho_query = (
            memory_scope.get("topic_query")
            if memory_scope
            else (
                "Recent themes, personal facts, relationships, preferences, "
                "plans, and details useful for a supportive voice conversation."
            )
        )
        if honcho_target:
            honcho_task = asyncio.create_task(
                fetch_voice_honcho_context(
                    honcho_target,
                    query=str(honcho_query or ""),
                    top_k=16 if memory_scope else 12,
                )
            )
        else:
            honcho_task = asyncio.create_task(asyncio.sleep(0, result=honcho_context_result))
        (
            recent_conversations,
            recent_timeline,
            memory_context,
            honcho_context_result,
        ) = await asyncio.gather(
            conv_task,
            timeline_task,
            mem_task,
            honcho_task,
        )
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Parallel context fetch error: {e}")

    # 5. Assemble context (trim to budget)
    soul = files.get("SOUL.md", "")[:3000]
    user_profile = files.get("USER.md", "")[:2000]
    # Read last 2 days of daily voice logs (today + yesterday, UTC)
    voice_summaries = ""
    if agent_id and workspace_headers.get("Authorization") != "Bearer ":
        try:
            _today = datetime.utcnow()
            _yesterday = _today - timedelta(days=1)
            _voice_parts = []
            async with httpx.AsyncClient(timeout=10.0) as _vc:
                for _d in [_yesterday, _today]:
                    _vpath = f"voice/{_d.strftime('%Y-%m-%d')}.md"
                    _vresp = await _vc.get(
                        f"{workspace_api_url}/workspace/{agent_id}/files/{_vpath}",
                        headers=workspace_headers,
                    )
                    if _vresp.status_code == 200:
                        _vcontent = _vresp.json().get("content", "").strip()
                        if _vcontent:
                            _voice_parts.append(_vcontent)
            if _voice_parts:
                voice_summaries = "\n\n".join(_voice_parts)
                if len(voice_summaries) > 1500:
                    voice_summaries = voice_summaries[-1500:]
        except Exception as _ve:
            logger.warning(f"[FLOW:VOICE-CONTEXT] Voice daily log read error: {_ve}")
    memory = files.get("MEMORY.md", "")[:500]

    # Additional workspace files for richer context
    tools_md = files.get("TOOLS.md", "")[:500]  # Agent capabilities awareness
    shared_reports = ""
    for fname, fcontent in files.items():
        if fname.startswith("shared/reports/") and fcontent:
            shared_reports += fcontent[:800] + "\n"
            if len(shared_reports) > 1500:
                break

    # conditions and medications are text[] arrays in postgres
    conditions_list = list(row["conditions"] or [])
    medications_list = list(row["medications"] or [])

    _elapsed = int((time.time() - _start) * 1000)
    print(
        f"[FLOW:VOICE-CONTEXT] uid={uid} user={row['name']} agent={agent_id} "
        f"files={len(files)} rules={len(dynamic_rules)} "
        f"convos={len(recent_conversations)} timeline={len(recent_timeline)} mem={len(memory_context)} "
        f"honcho={honcho_context_result.get('reason')} "
        f"latency={_elapsed}ms",
        flush=True,
    )

    response = {
        "user_name": row["name"],
        "user_agent_id": agent_id,
        "runtime": {
            "provider": "hermes" if runtime else "legacy",
            "agent_id": agent_id,
            "binding_revision": runtime.revision if runtime else None,
        },
        "soul": soul,
        "user_profile": user_profile,
        "memory": memory,
        "conditions": conditions_list,
        "medications": medications_list,
        "recent_voice_summaries": voice_summaries,
        "recent_timeline": recent_timeline,
        "recent_conversations": recent_conversations,
        "memory_context": memory_context,
        "honcho_context": str(honcho_context_result.get("context") or ""),
        "honcho_status": {
            key: value
            for key, value in honcho_context_result.items()
            if key != "context"
        },
        "memory_scope": memory_scope,
        "shared_reports": shared_reports,
        "caregiver_notes": _extract_caregiver_section(user_profile),
        "dynamic_rules": dynamic_rules,
        "voice_rules": VOICE_INTERACTION_RULES,
    }
    if not runtime:
        # Legacy credentials remain restricted to this dual-authenticated
        # proxy endpoint while the existing Plato path is still enabled.
        response["gateway_url"] = gateway_url
        response["gateway_token"] = gateway_token
    return response


@router.post("/tool")
async def execute_voice_tool(request: Request):
    """Run a bounded read-only tool against the session's exact Hermes runtime."""
    body = await request.json()
    uid = str(body.get("uid") or "").strip()
    tool_name = str(body.get("tool_name") or "").strip()
    arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
    if not uid:
        raise HTTPException(status_code=400, detail={"code": "uid_required"})

    principal = authenticate_voice_proxy_request(request, uid)
    runtime = await _resolve_voice_runtime(principal)
    if not runtime:
        raise HTTPException(status_code=409, detail={"code": "isolated_runtime_required"})
    if tool_name != "ask_ella":
        raise HTTPException(status_code=400, detail={"code": "voice_tool_not_allowed"})

    prompt = str(arguments.get("query") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail={"code": "voice_tool_query_required"})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{HERMES_PROVISION_API_URL.rstrip('/')}/runtime/{runtime.agent_id}/chat",
                headers=_hermes_workspace_headers(principal.uid),
                json={
                    "prompt": prompt,
                    "session_id": str(body.get("session_id") or principal.session_id),
                    "max_tokens": 320,
                },
            )
        if response.status_code != 200:
            logger.warning(
                "[FLOW:VOICE-TOOL] runtime=%s uid=%s status=%s",
                runtime.agent_id,
                principal.uid,
                response.status_code,
            )
            raise HTTPException(status_code=503, detail={"code": "hermes_voice_tool_unavailable"})
        payload = response.json()
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            raise HTTPException(status_code=503, detail={"code": "hermes_voice_tool_empty"})
        return {
            "answer": answer,
            "runtime": "hermes",
            "agent_id": runtime.agent_id,
            "binding_revision": runtime.revision,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[FLOW:VOICE-TOOL] runtime=%s error=%s", runtime.agent_id, exc)
        raise HTTPException(status_code=503, detail={"code": "hermes_voice_tool_unavailable"}) from exc


@router.post("/search-omi")
async def search_omi_conversations(request: Request):
    """
    Search OMI conversations for a user. Called by Grok voice proxy during live calls
    when the user asks to look up specific past conversations.

    Body:
        uid (str): Firebase UID of the user
        query (str): Search terms (matched against title and overview)
        limit (int, optional): Max results (default: 5, max: 10)

    Returns list of matching conversations with title, overview, and timestamp.
    """
    body = await request.json()
    uid = body.get("uid", "")
    query = body.get("query", "")
    limit = min(body.get("limit", 5), 10)

    if not uid:
        raise HTTPException(status_code=400, detail="uid required")
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    principal = authenticate_voice_proxy_request(request, uid)
    await _resolve_voice_runtime(principal)
    uid = principal.uid

    logger.info(f"[FLOW:VOICE-SEARCH] uid={uid} query=\"{query}\" limit={limit}")

    try:
        from google.cloud import firestore

        db = firestore.Client()

        from datetime import datetime, timedelta
        import re as re_mod

        # Parse date hints from query for date-range filtering
        now_local = _pacific_now()
        now_local = _pacific_now()
        now = datetime.utcnow()
        date_filter_start = None
        date_filter_end = None
        keyword_terms = []
        
        query_lower = query.lower().strip()
        
        # Date patterns: "march 8", "march 8th", "3/8", "yesterday", "last week", "today"
        date_parsed = False
        
        if "yesterday" in query_lower:
            d = now - timedelta(days=1)
            date_filter_start = d.replace(hour=0, minute=0, second=0)
            date_filter_end = d.replace(hour=23, minute=59, second=59)
            date_parsed = True
            keyword_terms = [t for t in query_lower.split() if t != "yesterday"]
        elif "today" in query_lower:
            date_filter_start = now.replace(hour=0, minute=0, second=0)
            date_filter_end = now
            date_parsed = True
            keyword_terms = [t for t in query_lower.split() if t != "today"]
        elif "last week" in query_lower:
            date_filter_start = now - timedelta(days=7)
            date_filter_end = now
            date_parsed = True
            keyword_terms = [t for t in query_lower.replace("last week", "").split() if t]
        elif "last month" in query_lower:
            date_filter_start = now - timedelta(days=30)
            date_filter_end = now
            date_parsed = True
            keyword_terms = [t for t in query_lower.replace("last month", "").split() if t]
        else:
            # Try "month day" pattern: "march 8", "march 8th", "mar 8"
            month_names = {
                "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
                "nov": 11, "november": 11, "dec": 12, "december": 12,
            }
            date_match = re_mod.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december|"
                r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?",
                query_lower
            )
            if date_match:
                month_str = date_match.group(1)
                day = int(date_match.group(2))
                month = month_names.get(month_str, 0)
                if month and 1 <= day <= 31:
                    try:
                        year = now.year
                        target = datetime(year, month, day)
                        # If date is in the future, assume last year
                        if target > now:
                            target = datetime(year - 1, month, day)
                        date_filter_start = target.replace(hour=0, minute=0, second=0)
                        date_filter_end = target.replace(hour=23, minute=59, second=59)
                        date_parsed = True
                        # Remove date part from query for keyword matching
                        remaining = query_lower[:date_match.start()] + query_lower[date_match.end():]
                        keyword_terms = [t for t in remaining.split() if t]
                    except ValueError:
                        pass
        
        if not date_parsed:
            keyword_terms = query_lower.split()

        keyword_terms = _expand_query_terms(_significant_query_terms(" ".join(keyword_terms)))
        keyword_terms = _normalized_query_terms(" ".join(keyword_terms))
        
        logger.info(f"[FLOW:VOICE-SEARCH] date_filter={date_filter_start}->{date_filter_end}, keywords={keyword_terms}")
        
        # Fetch conversations — use date filter if we have one, otherwise get last 100
        if date_filter_start and date_filter_end:
            # Firestore query with date range
            convos_ref = (
                db.collection("users").document(uid).collection("conversations")
                .where("created_at", ">=", date_filter_start)
                .where("created_at", "<=", date_filter_end)
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(50)
            )
        else:
            convos_ref = (
                db.collection("users").document(uid).collection("conversations")
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(100)
            )
        convos = [doc.to_dict() for doc in convos_ref.stream()]
        
        # Client-side keyword match (skip if only date search with no keywords)
        matches = []
        
        for c in convos:
            structured = c.get("structured", {})
            title = (structured.get("title", "") or "").lower()
            overview = (structured.get("overview", "") or "").lower()
            category = (structured.get("category", "") or "").lower()
            
            # Include formatted date in searchable text
            created = c.get("created_at")
            date_str = ""
            if created and hasattr(created, "strftime"):
                date_str = created.strftime("%B %d %Y %b %A").lower()  # "march 18 2026 mar monday"
            
            searchable = f"{title} {overview} {category} {date_str}"
            
            if keyword_terms:
                # Score: how many keyword terms match
                score = _keyword_score(searchable, keyword_terms)
                if score > 0:
                    matches.append((score, c))
            else:
                # Date-only search — return all conversations from that date
                matches.append((1, c))
        
        # Sort by score (desc), then by recency
        matches.sort(key=lambda x: x[0], reverse=True)
        matches = matches[:limit]
        
        results = []
        for score, c in matches:
            structured = c.get("structured", {})
            created = c.get("created_at")
            ts = ""
            if created:
                created_utc = _parse_event_datetime_utc(created)
                if created_utc:
                    created_local = created_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
                    ts = created_local.strftime("%Y-%m-%d %I:%M %p %Z")
                elif hasattr(created, "strftime"):
                    ts = created.strftime("%Y-%m-%d %I:%M %p")
                else:
                    ts = str(created)[:16]
            
            results.append({
                "title": structured.get("title", "Untitled"),
                "overview": structured.get("overview", ""),
                "emoji": structured.get("emoji", ""),
                "category": structured.get("category", ""),
                "timestamp": ts,
                "score": score,
            })
        
        logger.info(f"[FLOW:VOICE-SEARCH] Found {len(results)} matches for \"{query}\"")
        return {"results": results, "total_searched": len(convos), "query": query}
    
    except Exception as e:
        logger.error(f"[FLOW:VOICE-SEARCH] Error: {e}")
        return {"results": [], "error": str(e), "query": query}


# ============================================================================
# Unified Search Endpoint — POST /v1/voice/search
# Role-based fan-out across workspace, OMI conversations, memories,
# voice logs, and scanner escalation logs.
# ============================================================================

# Privacy policy: maps agent_role -> source -> access level
# True = full access, False = no access, "own" = own data only, "full" = full
SEARCH_POLICY = {
    "user":      {"timeline": True, "workspace": True, "omi_full": True, "omi_meta": True, "memories": True, "voice": True, "honcho": True, "scanner": "own"},
    "caregiver": {"timeline": True, "workspace": True, "omi_full": False, "omi_meta": True, "memories": False, "voice": False, "honcho": False, "scanner": "full"},
    "scanner":   {"timeline": False, "workspace": False, "omi_full": False, "omi_meta": False, "memories": False, "voice": False, "honcho": False, "scanner": False},
    "voice":     {"timeline": True, "workspace": True, "omi_full": True, "omi_meta": True, "memories": True, "voice": True, "honcho": True, "scanner": False},
}

# Which source keys map to which request-level source names
_SOURCE_TO_POLICY_KEYS = {
    "timeline":  ["timeline"],
    "channel":   ["timeline"],
    "workspace": ["workspace"],
    "omi":       ["omi_full", "omi_meta"],
    "memories":  ["memories"],
    "voice":     ["voice"],
    "honcho":    ["honcho"],
    "scanner":   ["scanner"],
}


def _get_allowed_sources(agent_role: str, requested_sources: list = None) -> dict:
    """Return dict of policy_key -> access_level for the given role.
    Filters by requested_sources if provided."""
    policy = SEARCH_POLICY.get(agent_role, SEARCH_POLICY["scanner"])  # Default most restrictive

    if requested_sources is None:
        return {k: v for k, v in policy.items() if v}

    # Build set of policy keys that correspond to the requested source names
    allowed_policy_keys = set()
    for src in requested_sources:
        for pk in _SOURCE_TO_POLICY_KEYS.get(src, []):
            allowed_policy_keys.add(pk)

    return {k: v for k, v in policy.items() if v and k in allowed_policy_keys}


def _validate_agent_uid(agent_id: str, uid: str) -> bool:
    """Verify the agent_id is plausibly associated with this uid.
    Agent IDs follow patterns: ella-{userId}, ella-{type}-{userId},
    ella-cg-{userId}, ella-scanner-{userId}. Also accepts direct API
    calls (no agent_id) as trusted."""
    if not agent_id:
        return True  # No agent_id means direct API call (trusted)
    if not uid:
        return False
    agent_id_norm = agent_id.lower()
    uid_norm = uid.lower()
    # Agent IDs should contain the uid as a suffix component
    # Patterns: ella-{uid}, ella-user-{uid}, ella-cg-{uid}, ella-scanner-{uid}
    parts = agent_id_norm.split("-")
    if len(parts) < 2:
        return False
    # The uid should appear as the last segment(s) of the agent_id
    if agent_id_norm.endswith(uid_norm):
        return True
    # Also accept if uid appears anywhere in the agent_id (for flexibility)
    if uid_norm in agent_id_norm:
        return True
    return False


def _keyword_score(text: str, terms: list) -> int:
    """Simple keyword match score: count exact query terms found in text."""
    import re as _re

    text_lower = text.lower()
    score = 0
    for t in terms:
        term = (t or "").lower().strip()
        if not term:
            continue
        if _re.search(rf"(?<![a-z0-9]){_re.escape(term)}(?![a-z0-9])", text_lower):
            score += 1
    return score


def _conversation_transcript_text(conversation: dict, uid: str, max_chars: int = 12000) -> str:
    """Return readable transcript text from a Firestore conversation document."""
    try:
        data = _decrypt_conversation_data(conversation, uid)
    except Exception as e:
        logger.debug(f"[FLOW:UNIFIED-SEARCH] Transcript decrypt/decompress failed: {e}")
        data = conversation

    segments = data.get("transcript_segments") or []
    if not isinstance(segments, list):
        return ""

    parts = []
    for seg in segments:
        if isinstance(seg, dict):
            text = seg.get("text") or seg.get("transcript") or seg.get("content") or ""
            speaker = seg.get("speaker") or seg.get("speaker_id") or seg.get("person_id") or ""
        else:
            text = getattr(seg, "text", "") or getattr(seg, "transcript", "") or ""
            speaker = getattr(seg, "speaker", "") or getattr(seg, "speaker_id", "") or ""
        text = " ".join(str(text).split())
        if not text:
            continue
        if speaker:
            parts.append(f"{speaker}: {text}")
        else:
            parts.append(text)
        if sum(len(p) for p in parts) > max_chars:
            break
    return "\n".join(parts)[:max_chars]


def _snippet_around_terms(text: str, terms: list, max_chars: int = 900) -> str:
    if not text:
        return ""
    lower = text.lower()
    for term in terms:
        idx = lower.find(term.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 3)
            end = min(len(text), idx + len(term) + (max_chars * 2 // 3))
            return text[start:end].strip()
    return text[:max_chars].strip()


def _normalized_query_terms(query: str) -> list[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with",
        "what", "where", "when", "who", "why", "how", "did", "do", "does", "i",
        "me", "my", "you", "greg", "plato", "tell", "check", "latest", "recent",
        "memory", "memories", "after", "before", "else", "went", "go", "this",
        "omi", "conversation", "conversations", "transcript", "transcripts",
        "summary", "summaries", "morning", "afternoon", "evening", "today",
        "yesterday", "raw", "happened", "happen", "heard", "hear", "catch",
        "caught", "find", "pull", "about",
    }
    terms = [t.strip(".,?!:;()[]{}\"'’‘“”").lower() for t in query.split()]
    filtered = [t for t in terms if len(t) > 2 and t not in stop]
    return filtered or [t for t in terms if len(t) > 2]


def _significant_query_terms(query: str) -> list[str]:
    """Return only evidence-bearing terms; unlike _normalized_query_terms,
    this may return an empty list for broad temporal questions."""
    stop = {
        "the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with",
        "what", "where", "when", "who", "why", "how", "did", "do", "does", "i",
        "me", "my", "you", "greg", "plato", "tell", "check", "latest", "recent",
        "memory", "memories", "after", "before", "else", "went", "go", "this",
        "omi", "conversation", "conversations", "transcript", "transcripts",
        "summary", "summaries", "morning", "afternoon", "evening", "today",
        "yesterday", "raw", "happened", "happen", "heard", "hear", "catch",
        "caught", "find", "pull", "about",
    }
    terms = [t.strip(".,?!:;()[]{}\"'’‘“”").lower() for t in query.split()]
    return [t for t in terms if len(t) > 2 and t not in stop]


def _expand_query_terms(terms: list[str]) -> list[str]:
    expanded = list(terms)
    aliases = {
        "meisheng": ["mei", "sheng", "may", "shing"],
        "meishengs": ["mei", "sheng", "may", "shing"],
    }
    for term in terms:
        expanded.extend(aliases.get(term, []))
    return list(dict.fromkeys(expanded))


def _pacific_now() -> datetime:
    return datetime.now(ZoneInfo("America/Los_Angeles"))


def _local_window_to_utc(start_local: datetime, end_local: datetime) -> tuple[datetime, datetime]:
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _parse_relative_time_window(query: str) -> tuple[Optional[datetime], Optional[datetime], str]:
    """Parse common relative-time phrases into a naive UTC window."""
    query_lower = query.lower().strip()
    now_local = _pacific_now()
    start_local = None
    end_local = None
    remaining = query_lower

    if "this morning" in query_lower:
        start_local = now_local.replace(hour=5, minute=0, second=0, microsecond=0)
        end_local = min(now_local, now_local.replace(hour=12, minute=0, second=0, microsecond=0))
        remaining = remaining.replace("this morning", " ")
    elif "this afternoon" in query_lower:
        start_local = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        end_local = min(now_local, now_local.replace(hour=17, minute=0, second=0, microsecond=0))
        remaining = remaining.replace("this afternoon", " ")
    elif "this evening" in query_lower:
        start_local = now_local.replace(hour=17, minute=0, second=0, microsecond=0)
        end_local = now_local
        remaining = remaining.replace("this evening", " ")
    elif "yesterday" in query_lower:
        day = now_local - timedelta(days=1)
        start_local = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        remaining = remaining.replace("yesterday", " ")
    elif "today" in query_lower:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = now_local
        remaining = remaining.replace("today", " ")

    if not start_local or not end_local:
        return None, None, query

    start_utc, end_utc = _local_window_to_utc(start_local, end_local)
    return start_utc, end_utc, " ".join(remaining.split())


def _parse_event_datetime_utc(raw: object) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _should_use_voice_memory_fast_path(query: str) -> bool:
    query_lower = query.lower()
    window_start, _window_end, _remaining = _parse_relative_time_window(query)
    if window_start:
        return False
    detail_terms = (
        "omi",
        "necklace",
        "transcript",
        "transcripts",
        "conversation",
        "conversations",
        "this morning",
        "this afternoon",
        "this evening",
        "today",
        "yesterday",
    )
    return not any(term in query_lower for term in detail_terms)


async def _search_canonical_timeline(
    uid: str,
    query: str,
    limit: int,
    *,
    scope_kind: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> list:
    """Search the canonical cross-channel event ledger.

    This is the durable source for voice/app/iMessage/chat turns and should be
    preferred for recent chronological facts that may not have been promoted to
    OMI summaries or workspace memory yet.
    """
    results = []
    try:
        pool = await _get_pool()
        query_terms = _expand_query_terms(_significant_query_terms(query))
        if not query_terms:
            return results
        rows = await pool.fetch(
            """
            SELECT channel, provider, role, text, started_at, session_id, metadata
            FROM canonical_events
            WHERE lower(uid) = lower($1)
              AND text IS NOT NULL
              AND trim(text) != ''
              AND (
                    NOT (
                        COALESCE(source_ref ->> 'scope_kind', '') = 'memory'
                        OR COALESCE(metadata ->> 'scope_kind', '') = 'memory'
                    )
                    OR (
                        $2 = 'memory'
                        AND COALESCE(
                            NULLIF(source_ref ->> 'conversation_id', ''),
                            metadata ->> 'conversation_id',
                            ''
                        ) = $3
                    )
              )
            ORDER BY started_at DESC, id DESC
            LIMIT 300
            """,
            uid,
            scope_kind or "",
            conversation_id or "",
        )
        matches = []
        include_assistant = any(
            phrase in query.lower()
            for phrase in ("what did you say", "what did ella say", "your response", "assistant said", "you told me")
        )
        for row in rows:
            text = row["text"] or ""
            channel = row["channel"] or ""
            role = row["role"] or ""
            provider = row["provider"] or ""
            # Assistant turns are useful for chat continuity, but they are not
            # source evidence for memory lookup. Letting previous assistant
            # answers outrank user/OMI facts creates self-reinforcing hallucinations.
            if role == "assistant" and not include_assistant:
                continue
            searchable = f"{channel} {provider} {role} {text}"
            score = _keyword_score(searchable, query_terms)
            if score <= 0:
                continue
            if role == "user" and text.strip().endswith("?"):
                # User questions are useful conversational history, but they are
                # usually not evidence for factual recall. Avoid ranking prior
                # questions like "what did I do this morning?" as answers.
                score = max(0, score - 2)
                if score <= 0:
                    continue
            # Recency should break ties for live voice questions.
            ts = row["started_at"]
            ts_text = ts.strftime("%Y-%m-%d %I:%M %p") if ts else ""
            role_boost = 15 if role == "user" else 3
            matches.append((score, ts or datetime.min, {
                "source": "timeline",
                "title": f"{channel} {role}".strip(),
                "content": text[:700],
                "timestamp": ts_text,
                "score": score + role_boost,
                "metadata": {
                    "provenance": "canonical_event",
                    "channel": channel,
                    "provider": provider,
                    "role": role,
                    "session_id": row["session_id"] or "",
                },
            }))
        matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [m[2] for m in matches[:limit]]
    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Canonical timeline search error: {e}")
        return results


async def _search_voice_memory_pack(uid: str, query: str, limit: int) -> list:
    """Fast Hermes voice-memory lookup for realtime providers.

    This is the preferred low-latency path for live V2V tools. It reads the
    compact Hermes pack and returns quickly; heavier Firestore/workspace/Honcho
    searches remain fallback sources in the merged /v1/voice/search response.
    """
    if not HERMES_VOICE_MEMORY_URL or not HERMES_VOICE_MEMORY_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=1.8) as client:
            resp = await client.post(
                HERMES_VOICE_MEMORY_URL,
                headers={"Authorization": f"Bearer {HERMES_VOICE_MEMORY_TOKEN}"},
                json={
                    "uid": uid,
                    "query": query,
                    "scope": "recent",
                    "detail": "brief",
                    "top_k": min(max(limit, 1), 5),
                },
            )
        if resp.status_code != 200:
            logger.warning(f"[FLOW:VOICE-MEMORY] lookup returned {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        answer = (data.get("answer") or "").strip()
        if not answer or data.get("confidence") in {"low", "unknown"}:
            return []
        sources = data.get("sources") or []
        top = sources[0] if sources else {}
        confidence = data.get("confidence") or "medium"
        score = 120 if confidence == "high" else 70
        return [{
            "source": "voice_memory",
            "title": top.get("title") or "Hermes Voice Memory",
            "content": answer[:900],
            "timestamp": top.get("timestamp") or "",
            "score": score,
            "metadata": {
                "provenance": "hermes_voice_memory",
                "path": data.get("path"),
                "confidence": confidence,
                "latency_ms": data.get("latency_ms"),
                "source_ref": top.get("source_ref"),
                "channel": top.get("channel"),
            },
        }]
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-MEMORY] lookup error: {e}")
        return []


async def _fetch_recent_canonical_timeline(
    uid: str,
    limit: int = 30,
    max_chars: int = 9000,
    *,
    scope_kind: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Fetch the latest cross-channel turns for realtime voice startup context.

    This is not keyword search. Realtime voice providers need a hot chronological
    context window at session start because some native audio modes cannot safely
    call backend tools mid-turn.
    """
    try:
        pool = await _get_pool()
        rows = await pool.fetch(
            """
            SELECT channel, provider, role, text, started_at
            FROM canonical_events
            WHERE lower(uid) = lower($1)
              AND text IS NOT NULL
              AND trim(text) != ''
              AND (
                    NOT (
                        COALESCE(source_ref ->> 'scope_kind', '') = 'memory'
                        OR COALESCE(metadata ->> 'scope_kind', '') = 'memory'
                    )
                    OR (
                        $2 = 'memory'
                        AND COALESCE(
                            NULLIF(source_ref ->> 'conversation_id', ''),
                            metadata ->> 'conversation_id',
                            ''
                        ) = $3
                    )
              )
            ORDER BY started_at DESC, id DESC
            LIMIT $4
            """,
            uid,
            scope_kind or "",
            conversation_id or "",
            limit,
        )
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Canonical timeline fetch failed: {e}")
        return ""

    entries = []
    for row in reversed(rows):
        ts = row["started_at"]
        ts_text = ts.strftime("%Y-%m-%d %I:%M %p") if ts else ""
        channel = row["channel"] or "unknown"
        role = row["role"] or "unknown"
        provider = row["provider"] or ""
        text = " ".join((row["text"] or "").split())
        if not text:
            continue
        if len(text) > 700:
            text = text[:700].rstrip() + "..."
        label = f"{ts_text} [{channel}/{role}"
        if provider:
            label += f"/{provider}"
        label += "]"
        entries.append(f"- {label}: {text}")

    timeline = "\n".join(entries)
    if len(timeline) > max_chars:
        timeline = timeline[-max_chars:]
    return timeline


async def _search_workspace(
    uid: str,
    agent_id: str,
    query: str,
    limit: int,
    *,
    provision_url: Optional[str] = None,
    provision_token: Optional[str] = None,
    owner_uid: str = "",
) -> list:
    """Search workspace files via Provision API. Falls back to reading key files
    and doing keyword matching if no search endpoint exists."""
    results = []
    provision_url = (provision_url or PROVISION_API_URL).rstrip("/")
    provision_token = PROVISION_API_TOKEN if provision_token is None else provision_token
    if not agent_id or not provision_token:
        return results
    headers = {"Authorization": f"Bearer {provision_token}", "Content-Type": "application/json"}
    if owner_uid:
        headers["X-Ella-Owner-Uid"] = owner_uid

    query_terms = _expand_query_terms(_significant_query_terms(query))
    if not query_terms:
        return results

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Try the search endpoint first
            if owner_uid:
                resp = await client.post(
                    f"{provision_url}/workspace/{agent_id}/search",
                    headers=headers,
                    json={"query": query, "limit": limit},
                )
            else:
                resp = await client.get(
                    f"{provision_url}/workspace/{agent_id}/search",
                    params={"q": query},
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("results", [])[:limit]:
                    file_name = item.get("file", "") or item.get("path", "")
                    results.append(
                        {
                            "source": "workspace",
                            "title": item.get("heading", "") or file_name,
                            "content": (item.get("snippet", "") or item.get("excerpt", "") or item.get("content", ""))[
                                :500
                            ],
                            "timestamp": item.get("date", "") or "",
                            "score": item.get("score", 1),
                            "metadata": {"provenance": "hermes_workspace", "file": file_name},
                        }
                    )
                return results

            # 404 = no search endpoint; fall back to reading key files
            if resp.status_code != 404:
                logger.warning(f"[FLOW:UNIFIED-SEARCH] Provision search returned {resp.status_code}")
                return results

            # Fallback: read files list and grep
            resp2 = await client.get(
                f"{provision_url}/workspace/{agent_id}/files",
                headers=headers,
            )
            if resp2.status_code != 200:
                return results

            file_list = resp2.json().get("files", [])
            for f in file_list:
                fname = f.get("name", f.get("filename", ""))
                content = f.get("content", "")
                if not content:
                    continue
                score = _keyword_score(f"{fname} {content}", query_terms)
                if score > 0:
                    # Extract matching snippet
                    content_lower = content.lower()
                    snippet = ""
                    for term in query_terms:
                        idx = content_lower.find(term)
                        if idx >= 0:
                            start = max(0, idx - 80)
                            end = min(len(content), idx + len(term) + 120)
                            snippet = content[start:end].strip()
                            break
                    if not snippet:
                        snippet = content[:200]

                    results.append(
                        {
                            "source": "workspace",
                            "title": fname,
                            "content": snippet,
                            "timestamp": "",
                            "score": score,
                            "metadata": {"provenance": "hermes_workspace_mirror", "file": fname},
                        }
                    )

            # Sort by score desc, limit
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Workspace search error: {e}")
        return results


def _canonical_event_title(event: dict) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
    return str(event.get("title") or structured.get("title") or "OMI conversation")


def _canonical_event_timestamp(event: dict, user_timezone: str = "America/Los_Angeles") -> str:
    raw = event.get("started_at") or event.get("created_at") or event.get("timestamp") or ""
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(user_timezone))
        return local.strftime("%Y-%m-%d %I:%M %p %Z")
    except Exception:
        return str(raw)[:16]


def _canonical_omi_quality_score(metadata: dict) -> int:
    signal = metadata.get("ella_signal") if isinstance(metadata.get("ella_signal"), dict) else {}
    tags = metadata.get("ella_tags") if isinstance(metadata.get("ella_tags"), list) else []
    tags = {str(tag).lower() for tag in tags}

    score = 0
    salience = str(signal.get("salience") or "").lower()
    if salience == "high":
        score += 30
    elif salience == "medium":
        score += 18

    noise = str(signal.get("noise_level") or "").lower()
    if noise == "none":
        score += 4
    elif noise == "low":
        score += 2
    elif noise == "medium":
        score -= 6
    elif noise == "high":
        score -= 14

    if "low_signal" in tags:
        score -= 12
    if "background" in tags:
        score -= 8
    if "media" in tags:
        score -= 4
    if signal.get("contains_user_speech") is False:
        score -= 10
    return score


async def _search_canonical_omi_events(uid: str, query: str, limit: int, full_access: bool) -> list:
    """Search OMI canonical summary events via GET /v1/ella/timeline first."""
    results = []
    window_start, window_end, query_without_time = _parse_relative_time_window(query)
    timeline_limit = 300 if window_start and window_end else 120
    try:
        events = await fetch_canonical_timeline(uid, limit=timeline_limit, channels=["omi"])
    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Canonical OMI timeline fetch error: {e}")
        return results

    query_terms = _expand_query_terms(_significant_query_terms(query_without_time if window_start else query))
    matches = []
    for event in events:
        title = _canonical_event_title(event)
        text = str(event.get("text") or "")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
        started_sort = _parse_event_datetime_utc(
            event.get("started_at") or event.get("created_at") or event.get("timestamp")
        )
        if window_start and window_end:
            if not started_sort or started_sort < window_start or started_sort > window_end:
                continue

        searchable = " ".join(
            str(part or "")
            for part in (
                title,
                text,
                structured.get("overview"),
                structured.get("category"),
                structured.get("emoji"),
                event.get("provider"),
                event.get("source_identity"),
            )
        )
        score = _keyword_score(searchable, query_terms) if query_terms else 1
        if score <= 0:
            continue
        rank_score = score
        if window_start and not query_terms:
            rank_score += _canonical_omi_quality_score(metadata)

        started_sort = started_sort or datetime.min

        content = text
        if not full_access:
            content = content[:300] + ("..." if len(content) > 300 else "")
        matches.append((rank_score, started_sort, event, title, content))

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for score, _started_sort, event, title, content in matches[:limit]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        structured = metadata.get("structured") if isinstance(metadata.get("structured"), dict) else {}
        results.append({
            "source": "omi",
            "title": title,
            "content": content[:1400],
            "timestamp": _canonical_event_timestamp(event),
            "score": score + (55 if window_start else 45),
            "metadata": {
                "provenance": "canonical_event",
                "fallback": False,
                "event_id": event.get("event_id"),
                "source_identity": event.get("source_identity"),
                "channel": event.get("channel"),
                "provider": event.get("provider"),
                "emoji": structured.get("emoji", ""),
                "category": structured.get("category", ""),
                "time_window_applied": bool(window_start and window_end),
                "time_window_start_utc": window_start.isoformat() if window_start else "",
                "time_window_end_utc": window_end.isoformat() if window_end else "",
                "timestamp_timezone": "America/Los_Angeles",
            },
        })
    return results


async def _search_omi_canonical_first(uid: str, query: str, limit: int, full_access: bool) -> list:
    canonical_results = await _search_canonical_omi_events(uid, query, limit, full_access)
    if canonical_results:
        logger.info(
            f"[FLOW:UNIFIED-SEARCH] uid={uid} omi_source=canonical_event results={len(canonical_results)}"
        )
        return canonical_results

    logger.warning(
        f"[FLOW:UNIFIED-SEARCH] uid={uid} omi_source=canonical_event results=0 fallback=firestore_legacy_omi"
    )
    fallback_results = await _search_omi_conversations(uid, query, limit, full_access)
    for item in fallback_results:
        metadata = item.setdefault("metadata", {})
        metadata["provenance"] = "firestore_legacy_omi"
        metadata["fallback"] = True
    return fallback_results


async def _search_omi_conversations(uid: str, query: str, limit: int, full_access: bool) -> list:
    """Search OMI conversations in Firestore. Reuses date-parsing logic from
    search_omi_conversations endpoint.
    If full_access=False (caregiver role), returns only title/overview metadata
    with overview truncated to 100 chars."""
    results = []
    try:
        from google.cloud import firestore as _fs
        import re as _re

        db = _fs.Client()
        query_lower = query.lower().strip()
        now_local = _pacific_now()
        now = datetime.utcnow()

        # --- Date parsing (reused from search_omi_conversations) ---
        date_filter_start = None
        date_filter_end = None
        keyword_terms = []
        date_parsed = False

        if "this morning" in query_lower:
            date_filter_start, date_filter_end = _local_window_to_utc(
                now_local.replace(hour=5, minute=0, second=0, microsecond=0),
                min(now_local, now_local.replace(hour=12, minute=0, second=0, microsecond=0)),
            )
            date_parsed = True
            remaining = query_lower.replace("this morning", "")
            keyword_terms = [t for t in remaining.split() if t]
        elif "this afternoon" in query_lower:
            date_filter_start, date_filter_end = _local_window_to_utc(
                now_local.replace(hour=12, minute=0, second=0, microsecond=0),
                min(now_local, now_local.replace(hour=17, minute=0, second=0, microsecond=0)),
            )
            date_parsed = True
            remaining = query_lower.replace("this afternoon", "")
            keyword_terms = [t for t in remaining.split() if t]
        elif "this evening" in query_lower:
            date_filter_start, date_filter_end = _local_window_to_utc(
                now_local.replace(hour=17, minute=0, second=0, microsecond=0),
                now_local,
            )
            date_parsed = True
            remaining = query_lower.replace("this evening", "")
            keyword_terms = [t for t in remaining.split() if t]
        elif "yesterday" in query_lower:
            d = now_local - timedelta(days=1)
            date_filter_start, date_filter_end = _local_window_to_utc(
                d.replace(hour=0, minute=0, second=0, microsecond=0),
                d.replace(hour=23, minute=59, second=59, microsecond=999999),
            )
            date_parsed = True
            keyword_terms = [t for t in query_lower.split() if t != "yesterday"]
        elif "today" in query_lower:
            date_filter_start, date_filter_end = _local_window_to_utc(
                now_local.replace(hour=0, minute=0, second=0, microsecond=0),
                now_local,
            )
            date_parsed = True
            keyword_terms = [t for t in query_lower.split() if t != "today"]
        elif "last week" in query_lower:
            date_filter_start = now - timedelta(days=7)
            date_filter_end = now
            date_parsed = True
            keyword_terms = [t for t in query_lower.replace("last week", "").split() if t]
        elif "last month" in query_lower:
            date_filter_start = now - timedelta(days=30)
            date_filter_end = now
            date_parsed = True
            keyword_terms = [t for t in query_lower.replace("last month", "").split() if t]
        else:
            month_names = {
                "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
                "nov": 11, "november": 11, "dec": 12, "december": 12,
            }
            date_match = _re.search(
                r"(january|february|march|april|may|june|july|august|september|october|"
                r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
                r"\s+(\d{1,2})(?:st|nd|rd|th)?",
                query_lower,
            )
            if date_match:
                month_str = date_match.group(1)
                day = int(date_match.group(2))
                month = month_names.get(month_str, 0)
                if month and 1 <= day <= 31:
                    try:
                        year = now.year
                        target = datetime(year, month, day)
                        if target > now:
                            target = datetime(year - 1, month, day)
                        date_filter_start = target.replace(hour=0, minute=0, second=0)
                        date_filter_end = target.replace(hour=23, minute=59, second=59)
                        date_parsed = True
                        remaining = query_lower[: date_match.start()] + query_lower[date_match.end() :]
                        keyword_terms = [t for t in remaining.split() if t]
                    except ValueError:
                        pass

        if not date_parsed:
            keyword_terms = query_lower.split()
        else:
            keyword_terms = _expand_query_terms(_significant_query_terms(" ".join(keyword_terms)))

        # --- Firestore query ---
        if date_filter_start and date_filter_end:
            convos_ref = (
                db.collection("users").document(uid).collection("conversations")
                .where("created_at", ">=", date_filter_start)
                .where("created_at", "<=", date_filter_end)
                .order_by("created_at", direction=_fs.Query.DESCENDING)
                .limit(50)
            )
        else:
            convos_ref = (
                db.collection("users").document(uid).collection("conversations")
                .order_by("created_at", direction=_fs.Query.DESCENDING)
                .limit(100)
            )
        convos = [doc.to_dict() for doc in convos_ref.stream()]

        # --- Client-side keyword matching ---
        matches = []
        for c in convos:
            structured = c.get("structured", {})
            title = (structured.get("title", "") or "").lower()
            overview = (structured.get("overview", "") or "").lower()
            category = (structured.get("category", "") or "").lower()
            transcript_text = _conversation_transcript_text(c, uid) if full_access else ""
            transcript_lower = transcript_text.lower()

            created = c.get("created_at")
            date_str = ""
            if created and hasattr(created, "strftime"):
                date_str = created.strftime("%B %d %Y %b %A").lower()

            searchable = f"{title} {overview} {category} {date_str} {transcript_lower}"

            has_content = bool(title or overview or transcript_lower)
            if keyword_terms:
                score = sum(1 for term in keyword_terms if term in searchable)
                # If the user gave a date-style query with only generic terms
                # like "morning OMI conversations", return dated conversations
                # rather than requiring the word "morning" to appear in a summary.
                if score > 0 or date_parsed:
                    source_score = score or 1
                    if not has_content:
                        source_score -= 3
                    matches.append((source_score, created or datetime.min, c, transcript_text))
            else:
                source_score = 1
                if not has_content:
                    source_score -= 3
                matches.append((source_score, created or datetime.min, c, transcript_text))

        matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
        matches = matches[:limit]

        for score, _created_sort, c, transcript_text in matches:
            structured = c.get("structured", {})
            created = c.get("created_at")
            ts = ""
            if created:
                created_utc = _parse_event_datetime_utc(created)
                if created_utc:
                    created_local = created_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
                    ts = created_local.strftime("%Y-%m-%d %I:%M %p %Z")
                elif hasattr(created, "strftime"):
                    ts = created.strftime("%Y-%m-%d %I:%M %p")
                else:
                    ts = str(created)[:16]

            overview_text = structured.get("overview", "") or ""
            if not full_access:
                # Caregiver: truncate overview to first 100 chars, no raw content
                overview_text = overview_text[:100] + ("..." if len(overview_text) > 100 else "")
            elif transcript_text:
                snippet = _snippet_around_terms(transcript_text, keyword_terms, max_chars=1000)
                if snippet:
                    overview_text = (
                        (overview_text + "\n\nTranscript detail: " if overview_text else "Transcript detail: ")
                        + snippet
                    )[:1400]

            results.append({
                "source": "omi",
                "title": structured.get("title", "Untitled"),
                "content": overview_text,
                "timestamp": ts,
                "score": score + 18,
                "metadata": {
                    "provenance": "firestore_legacy_omi",
                    "fallback": True,
                    "emoji": structured.get("emoji", ""),
                    "category": structured.get("category", ""),
                    "has_transcript_detail": bool(transcript_text and full_access),
                    "timestamp_timezone": "America/Los_Angeles",
                },
            })

        return results

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] OMI search error: {e}")
        return results


async def _search_memories(uid: str, query: str, limit: int) -> list:
    """Search OMI memories (Firestore users/{uid}/memories collection).
    Client-side keyword matching against content and category."""
    results = []
    try:
        from google.cloud import firestore as _fs

        db = _fs.Client()
        query_terms = _expand_query_terms(_significant_query_terms(query))
        if not query_terms:
            return results

        # Fetch recent memories sorted by scoring (same pattern as database/memories.py)
        memories_ref = (
            db.collection("users").document(uid).collection("memories")
            .order_by("scoring", direction=_fs.Query.DESCENDING)
            .order_by("created_at", direction=_fs.Query.DESCENDING)
            .limit(100)
        )
        memories = [doc.to_dict() for doc in memories_ref.stream()]

        matches = []
        for mem in memories:
            if mem.get("user_review") is False:
                continue  # Excluded by user
            if mem.get("deleted", False):
                continue

            content = (mem.get("structured_memory", "") or mem.get("content", "") or "").lower()
            category = (mem.get("category", "") or "").lower()
            searchable = f"{content} {category}"

            score = sum(1 for t in query_terms if t in searchable)
            if score > 0:
                matches.append((score, mem))

        matches.sort(key=lambda x: x[0], reverse=True)
        matches = matches[:limit]

        for score, mem in matches:
            created = mem.get("created_at")
            ts = ""
            if created:
                if hasattr(created, "strftime"):
                    ts = created.strftime("%Y-%m-%d %I:%M %p")
                else:
                    ts = str(created)[:16]

            display_content = mem.get("structured_memory", "") or mem.get("content", "")
            results.append({
                "source": "memories",
                "title": (mem.get("category", "") or "Memory").title(),
                "content": (display_content or "")[:500],
                "timestamp": ts,
                "score": score,
                "metadata": {
                    "category": mem.get("category", ""),
                    "id": mem.get("id", ""),
                },
            })

        return results

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Memory search error: {e}")
        return results


async def _search_voice_logs(
    uid: str,
    agent_id: str,
    query: str,
    limit: int,
    *,
    provision_url: Optional[str] = None,
    provision_token: Optional[str] = None,
    owner_uid: str = "",
) -> list:
    """Search voice daily log files (voice/YYYY-MM-DD.md) via Provision API.
    Reads the last 7 days of logs and performs keyword matching."""
    results = []
    provision_url = (provision_url or PROVISION_API_URL).rstrip("/")
    provision_token = PROVISION_API_TOKEN if provision_token is None else provision_token
    if not agent_id or not provision_token:
        return results
    headers = {"Authorization": f"Bearer {provision_token}", "Content-Type": "application/json"}
    if owner_uid:
        headers["X-Ella-Owner-Uid"] = owner_uid

    query_terms = _expand_query_terms(_significant_query_terms(query))
    if not query_terms:
        return results

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Read workspace files and filter to voice/ directory
            resp = await client.get(
                f"{provision_url}/workspace/{agent_id}/files",
                headers=headers,
            )
            if resp.status_code != 200:
                return results

            file_list = resp.json().get("files", [])

            # Filter to voice log files from last 7 days
            now = datetime.utcnow()
            voice_files = []
            for f in file_list:
                fname = f.get("name", f.get("filename", ""))
                if not fname.startswith("voice/") or not fname.endswith(".md"):
                    continue
                # Try to extract date from filename (voice/YYYY-MM-DD.md)
                try:
                    date_part = fname.replace("voice/", "").replace(".md", "")
                    file_date = datetime.strptime(date_part, "%Y-%m-%d")
                    if (now - file_date).days <= 7:
                        voice_files.append(f)
                except ValueError:
                    # Non-date voice files still included
                    voice_files.append(f)

            for f in voice_files:
                fname = f.get("name", f.get("filename", ""))
                content = f.get("content", "")
                if not content:
                    continue

                score = _keyword_score(f"{fname} {content}", query_terms)
                if score > 0:
                    # Extract snippet around first match
                    content_lower = content.lower()
                    snippet = ""
                    for term in query_terms:
                        idx = content_lower.find(term)
                        if idx >= 0:
                            start = max(0, idx - 80)
                            end = min(len(content), idx + len(term) + 120)
                            snippet = content[start:end].strip()
                            break
                    if not snippet:
                        snippet = content[:200]

                    results.append(
                        {
                            "source": "voice",
                            "title": fname,
                            "content": snippet,
                            "timestamp": fname.replace("voice/", "").replace(".md", ""),
                            "score": score,
                            "metadata": {"file": fname},
                        }
                    )

            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Voice log search error: {e}")
        return results


async def _search_scanner_logs(uid: str, query: str, limit: int, access_level: str) -> list:
    """Search scanner escalation logs in PostgreSQL.
    access_level "own" = only escalated events for this uid.
    access_level "full" = all scanner logs (escalated + non-escalated) for this uid."""
    results = []
    try:
        pool = await _get_pool()
        query_terms = query.lower().split()

        if access_level == "own":
            # User role: only see escalated events
            rows = await pool.fetch(
                """
                SELECT id, stage, category, urgency, transcript_preview,
                       result, escalated, created_at
                FROM scanner_logs
                WHERE uid = $1 AND escalated = true
                ORDER BY created_at DESC
                LIMIT $2
                """,
                uid,
                limit * 5,  # Fetch more for keyword filtering
            )
        else:
            # Caregiver "full": all scanner events (not just escalated)
            # Caregivers need full visibility into safety monitoring patterns
            rows = await pool.fetch(
                """
                SELECT id, stage, category, urgency, transcript_preview,
                       result, escalated, created_at
                FROM scanner_logs
                WHERE uid = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                uid,
                limit * 5,
            )

        matches = []
        for row in rows:
            category = (row["category"] or "").lower()
            urgency = (row["urgency"] or "").lower()
            preview = (row["transcript_preview"] or "").lower()
            result_data = row["result"] if isinstance(row["result"], dict) else {}
            summary = (result_data.get("summary", "") or "").lower()

            searchable = f"{category} {urgency} {preview} {summary}"
            score = _keyword_score(searchable, query_terms)

            # If no specific keywords, return all escalated events
            if not query_terms:
                score = 1

            if score > 0:
                ts = ""
                if row["created_at"]:
                    ts = row["created_at"].strftime("%Y-%m-%d %I:%M %p")

                display_content = result_data.get("summary", row["transcript_preview"] or "")
                matches.append((score, {
                    "source": "scanner",
                    "title": f"[{(row['urgency'] or 'info').upper()}] {row['category'] or 'alert'}",
                    "content": (display_content or "")[:500],
                    "timestamp": ts,
                    "score": score,
                    "metadata": {
                        "category": row["category"] or "",
                        "urgency": row["urgency"] or "",
                        "escalated": row["escalated"],
                        "id": str(row["id"]),
                    },
                }))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches[:limit]]

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Scanner search error: {e}")
        return results


@router.post("/search")
async def unified_search(request: Request):
    """
    Unified search across multiple data sources with role-based privacy filtering.
    Fans out to workspace files, OMI conversations, memories, voice logs, and
    scanner escalation logs based on the requesting agent's role.

    Body:
        uid (str, required): Firebase UID
        query (str, required): Search terms
        agent_role (str, required): One of "user", "caregiver", "scanner", "voice"
        agent_id (str, optional): Agent ID for workspace resolution
        sources (list, optional): Filter to specific sources. Default: all allowed
        limit (int, optional): Max results per source. Default: 5, max: 10

    Returns merged results sorted by score, with sources_searched and sources_denied.
    """
    import asyncio as _aio

    body = await request.json()
    uid = body.get("uid", "")
    query = body.get("query", "")
    agent_role = body.get("agent_role", "voice")
    agent_id = body.get("agent_id", "")
    requested_sources = body.get("sources", None)
    limit = min(body.get("limit", 5), 10)

    if not uid:
        raise HTTPException(status_code=400, detail="uid required")
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    principal = authenticate_voice_proxy_request(request, uid)
    uid = principal.uid
    runtime = await _resolve_voice_runtime(principal)
    if agent_role not in SEARCH_POLICY:
        raise HTTPException(
            status_code=400, detail=f"Invalid agent_role: {agent_role}. Must be one of: {list(SEARCH_POLICY.keys())}"
        )

    if runtime and agent_id and agent_id != runtime.agent_id:
        raise HTTPException(status_code=403, detail={"code": "voice_runtime_agent_mismatch"})
    if runtime:
        agent_id = runtime.agent_id
    elif not _validate_agent_uid(agent_id, uid):
        raise HTTPException(status_code=403, detail="agent_id does not belong to this uid")

    _start = time.time()
    honcho_target = None
    honcho_target_reason = ""
    honcho_target_resolved = False
    prefetched_voice_memory_results = None

    # Realtime voice needs a bounded fast path. If the compact Hermes memory
    # pack has a high-confidence answer, return it immediately and avoid
    # waiting on slower fallback searches. A mapped retained companion still
    # fans out to Honcho so retained and isolated voice share durable memory.
    if not runtime and agent_role == "voice" and not requested_sources and _should_use_voice_memory_fast_path(query):
        honcho_target, honcho_target_reason = await _resolve_voice_honcho_binding(uid, runtime)
        honcho_target_resolved = True
        fast_results = await _search_voice_memory_pack(uid, query, limit)
        prefetched_voice_memory_results = fast_results
        if not honcho_target and fast_results and fast_results[0].get("score", 0) >= 120:
            _elapsed = int((time.time() - _start) * 1000)
            logger.info(
                f"[FLOW:UNIFIED-SEARCH] uid={uid} role={agent_role} query=\"{query}\" "
                f"sources=['voice_memory'] results={len(fast_results)} denied=[] "
                f"latency={_elapsed}ms fast_path=true"
            )
            return {
                "results": fast_results,
                "sources_searched": ["voice_memory"],
                "sources_denied": [],
                "provenance": {"hermes_voice_memory": len(fast_results)},
                "total_results": len(fast_results),
                "query": query,
                "fast_path": True,
            }

    # Resolve agent_id from postgres if not provided
    if not agent_id and not runtime:
        try:
            pool = await _get_pool()
            row = await pool.fetchrow(
                """
                SELECT ac.agents FROM users u
                JOIN agent_clusters ac ON ac.user_id = u.id
                WHERE u.omi_uid = $1
                """,
                uid,
            )
            if row and row["agents"]:
                agents_raw = row["agents"]
                if isinstance(agents_raw, str):
                    agents_data = json.loads(agents_raw)
                elif isinstance(agents_raw, dict):
                    agents_data = agents_raw
                else:
                    agents_data = {}
                agent_id = agents_data.get("userAgentId", "")
        except Exception as e:
            logger.warning(f"[FLOW:UNIFIED-SEARCH] Agent ID lookup failed: {e}")

    # Determine allowed sources based on role
    allowed = _get_allowed_sources(agent_role, requested_sources)
    if allowed.get("honcho"):
        if not honcho_target_resolved:
            honcho_target, honcho_target_reason = await _resolve_voice_honcho_binding(uid, runtime)
        if not honcho_target:
            logger.info(
                "[FLOW:UNIFIED-SEARCH] Honcho profile unavailable uid=%s reason=%s",
                uid,
                honcho_target_reason,
            )

    # Build task list for parallel fan-out
    tasks = []
    task_source_names = []

    include_voice_memory = not runtime and (
        bool(requested_sources and "voice_memory" in requested_sources)
        or (not requested_sources and _should_use_voice_memory_fast_path(query))
    )
    if agent_role in {"voice", "user"} and include_voice_memory:
        if prefetched_voice_memory_results is None:
            tasks.append(_search_voice_memory_pack(uid, query, limit))
        else:
            tasks.append(_aio.sleep(0, result=prefetched_voice_memory_results))
        task_source_names.append("voice_memory")

    if allowed.get("timeline"):
        tasks.append(
            _search_canonical_timeline(
                uid,
                query,
                limit,
                scope_kind=principal.scope_kind,
                conversation_id=principal.conversation_id,
            )
        )
        task_source_names.append("timeline")

    if allowed.get("workspace"):
        if runtime:
            tasks.append(
                _search_workspace(
                    uid,
                    agent_id,
                    query,
                    limit,
                    provision_url=HERMES_PROVISION_API_URL,
                    provision_token=HERMES_PROVISION_API_TOKEN,
                    owner_uid=uid,
                )
            )
        else:
            tasks.append(_search_workspace(uid, agent_id, query, limit))
        task_source_names.append("workspace")

    if allowed.get("omi_full") or allowed.get("omi_meta"):
        full_access = bool(allowed.get("omi_full"))
        tasks.append(_search_omi_canonical_first(uid, query, limit, full_access))
        task_source_names.append("omi")

    if allowed.get("memories"):
        tasks.append(_search_memories(uid, query, limit))
        task_source_names.append("memories")

    if allowed.get("voice"):
        if runtime:
            tasks.append(
                _search_voice_logs(
                    uid,
                    agent_id,
                    query,
                    limit,
                    provision_url=HERMES_PROVISION_API_URL,
                    provision_token=HERMES_PROVISION_API_TOKEN,
                    owner_uid=uid,
                )
            )
        else:
            tasks.append(_search_voice_logs(uid, agent_id, query, limit))
        task_source_names.append("voice")

    if honcho_target and allowed.get("honcho"):
        tasks.append(search_voice_honcho(honcho_target, query, limit))
        task_source_names.append("honcho")

    scanner_access = allowed.get("scanner")
    if scanner_access:
        access_level = "full" if scanner_access == "full" else "own"
        tasks.append(_search_scanner_logs(uid, query, limit, access_level))
        task_source_names.append("scanner")

    # Determine which sources were denied
    all_source_names = {
        "voice_memory",
        "timeline",
        "workspace",
        "omi",
        "memories",
        "voice",
        "honcho",
        "scanner",
    }
    if requested_sources:
        requested_set = set(requested_sources)
    else:
        requested_set = all_source_names
    sources_searched = set(task_source_names)
    sources_denied = sorted(requested_set - sources_searched)

    # Execute all searches in parallel with individual timeouts
    all_results = []
    if tasks:
        gathered = await _aio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(gathered):
            src_name = task_source_names[i]
            if isinstance(result, Exception):
                logger.warning(f"[FLOW:UNIFIED-SEARCH] Source {src_name} failed: {result}")
                continue
            if isinstance(result, list):
                all_results.extend(result)

    # Sort merged results by score (desc)
    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    provenance: dict[str, int] = {}
    for item in all_results:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        key = str(metadata.get("provenance") or item.get("source") or "unknown")
        provenance[key] = provenance.get(key, 0) + 1

    _elapsed = int((time.time() - _start) * 1000)
    logger.info(
        f"[FLOW:UNIFIED-SEARCH] uid={uid} role={agent_role} query=\"{query}\" "
        f"sources={sorted(sources_searched)} results={len(all_results)} "
        f"denied={sources_denied} provenance={provenance} latency={_elapsed}ms"
    )

    return {
        "results": all_results,
        "sources_searched": sorted(sources_searched),
        "sources_denied": sources_denied,
        "provenance": provenance,
        "total_results": len(all_results),
        "query": query,
    }
