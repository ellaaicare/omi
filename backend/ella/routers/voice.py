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

import base64
import json
import os
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

import asyncpg
import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel

from database.conversations import _decrypt_conversation_data

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
INWORLD_API_KEY = os.getenv("INWORLD_API_KEY", "")
ELLA_TTS_URL = os.getenv("ELLA_TTS_URL", "http://100.76.138.56:8930")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")
HERMES_VOICE_MEMORY_URL = os.getenv("HERMES_VOICE_MEMORY_URL", "http://100.76.138.56:8210/v1/voice/memory/lookup")
HERMES_VOICE_MEMORY_TOKEN = os.getenv("HERMES_VOICE_MEMORY_TOKEN", PROVISION_API_TOKEN)
DEFAULT_GATEWAY_URL = os.getenv("OPENCLAW_URL", "http://100.76.138.56:19001")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")

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


class VoiceSessionRequest(BaseModel):
    """Request for creating a V2V voice session."""

    uid: str
    provider: str = "grok-voice"
    voice_mode: Optional[str] = None


class VoiceSessionResponse(BaseModel):
    """Response containing session token and connection details."""

    session_token: str
    voice_endpoint: str
    expires_in: int  # seconds
    audio_format: dict
    provider: str
    voice_mode: str


class VoiceConfigResponse(BaseModel):
    """Voice configuration for iOS client."""

    sample_rate: int = 24000
    channels: int = 1
    encoding: str = "pcm_int16"
    byte_order: str = "little_endian"


class TtsRequest(BaseModel):
    """Request for text-to-speech synthesis."""

    text: str
    voice_id: str = "pFZP5JQG7iQjIQuC4Bku"  # Lily voice


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
        "uid": uid,
        "firebase_uid": firebase_uid,
        "name": display_name or "User",
        "voice_mode": voice_mode,
        "provider": provider,
        "context_url": f"{ELLA_API_BASE}/v1/users/{uid}/context",
        "callback_url": f"{ELLA_API_BASE}/v1/ella/voice-session",
        "exp": datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS),
        "iat": datetime.utcnow(),
        "iss": "omi-backend",
    }

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
    uid = (body.uid if body else None) or uid
    provider = (body.provider if body else None) or provider or "grok-voice"
    voice_mode = (body.voice_mode if body else None) or voice_mode

    if not uid:
        raise HTTPException(status_code=400, detail="uid required")

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
        token = create_session_token(
            uid=uid,
            firebase_uid=uid,
            display_name=user_display_name,
            voice_mode=resolved_mode,
            provider=provider,
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
        print(f"[FLOW:VOICE-TTS] provider={provider} text_len={text_len} url={ELLA_TTS_URL}", flush=True)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{ELLA_TTS_URL}/v1/audio/speech",
                    json={"input": text, "voice": "nova", "response_format": "mp3"},
                    timeout=30.0,
                )
            _elapsed = int((time.time() - _start) * 1000)
            if response.status_code != 200:
                print(f"[FLOW:VOICE-TTS] ERROR provider={provider} status={response.status_code} latency={_elapsed}ms", flush=True)
                raise HTTPException(status_code=502, detail=f"ella-tts error: {response.status_code}")
            audio_size = len(response.content)
            print(f"[FLOW:VOICE-TTS] OK provider={provider} audio_bytes={audio_size} latency={_elapsed}ms", flush=True)
            return Response(content=response.content, media_type="audio/mpeg")
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
        raise HTTPException(status_code=400, detail=f"Unknown TTS provider: {provider!r}. Valid: elevenlabs, fish-audio, fish-audio-s1, fish-audio-s2, kokoro, inworld, grok-voice, gemini-live")

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
        "tts_providers": ["elevenlabs", "fish-audio", "kokoro", "inworld"],
        "tts_elevenlabs_configured": bool(ELEVENLABS_API_KEY),
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

    budget = body.get("context_budget", 8000)
    memory_timeout_seconds = float(body.get("memory_timeout_seconds", 2.0))
    _start = time.time()

    # 1. DB lookup — user + agent cluster
    pool = await _get_pool()
    try:
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
    agents_raw = row["agents"]
    if isinstance(agents_raw, str):
        agents = json.loads(agents_raw)
    elif isinstance(agents_raw, dict):
        agents = agents_raw
    else:
        agents = {}

    agent_id = agents.get("userAgentId", "")
    gateway_url = agents.get("gatewayUrl", DEFAULT_GATEWAY_URL)
    gateway_token = agents.get("gatewayToken", OPENCLAW_GATEWAY_TOKEN)

    # 2. Read workspace files from provision API
    files = {}
    if agent_id and PROVISION_API_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{PROVISION_API_URL}/workspace/{agent_id}/files",
                    headers={"Authorization": f"Bearer {PROVISION_API_TOKEN}"},
                )
                if resp.status_code == 200:
                    file_list = resp.json().get("files", [])
                    files = {
                        f.get("name", f.get("filename", "")): f.get("content", "")
                        for f in file_list
                    }
                    logger.info(
                        f"[FLOW:VOICE-CONTEXT] Loaded {len(files)} workspace files "
                        f"for agent={agent_id}"
                    )
                else:
                    logger.warning(
                        f"[FLOW:VOICE-CONTEXT] Provision API returned "
                        f"{resp.status_code} for agent={agent_id}"
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
    try:
        import asyncio as _aio
        conv_task = _aio.create_task(_fetch_recent_conversations(uid, limit=8))
        timeline_task = _aio.create_task(_fetch_recent_canonical_timeline(uid, limit=40, max_chars=9000))
        mem_task = _aio.create_task(
            _fetch_memory_context(
                gateway_url,
                gateway_token,
                row["name"] or "user",
                timeout_seconds=memory_timeout_seconds,
            )
        )
        recent_conversations, recent_timeline, memory_context = await _aio.gather(
            conv_task,
            timeline_task,
            mem_task,
        )
    except Exception as e:
        logger.warning(f"[FLOW:VOICE-CONTEXT] Parallel context fetch error: {e}")

    # 5. Assemble context (trim to budget)
    soul = files.get("SOUL.md", "")[:3000]
    user_profile = files.get("USER.md", "")[:2000]
    # Read last 2 days of daily voice logs (today + yesterday, UTC)
    voice_summaries = ""
    if agent_id and PROVISION_API_TOKEN:
        try:
            _today = datetime.utcnow()
            _yesterday = _today - timedelta(days=1)
            _voice_parts = []
            async with httpx.AsyncClient(timeout=10.0) as _vc:
                for _d in [_yesterday, _today]:
                    _vpath = f"voice/{_d.strftime('%Y-%m-%d')}.md"
                    _vresp = await _vc.get(
                        f"{PROVISION_API_URL}/workspace/{agent_id}/files/{_vpath}",
                        headers={"Authorization": f"Bearer {PROVISION_API_TOKEN}"},
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
        f"latency={_elapsed}ms",
        flush=True,
    )

    return {
        "user_name": row["name"],
        "user_agent_id": agent_id,
        "gateway_url": gateway_url,
        "gateway_token": gateway_token,
        "soul": soul,
        "user_profile": user_profile,
        "memory": memory,
        "conditions": conditions_list,
        "medications": medications_list,
        "recent_voice_summaries": voice_summaries,
        "recent_timeline": recent_timeline,
        "recent_conversations": recent_conversations,
        "memory_context": memory_context,
        "shared_reports": shared_reports,
        "caregiver_notes": _extract_caregiver_section(user_profile),
        "dynamic_rules": dynamic_rules,
        "voice_rules": VOICE_INTERACTION_RULES,
    }


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
                if hasattr(created, "strftime"):
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
    "user":      {"timeline": True, "workspace": True, "omi_full": True, "omi_meta": True, "memories": True, "voice": True, "scanner": "own"},
    "caregiver": {"timeline": True, "workspace": True, "omi_full": False, "omi_meta": True, "memories": False, "voice": False, "scanner": "full"},
    "scanner":   {"timeline": False, "workspace": False, "omi_full": False, "omi_meta": False, "memories": False, "voice": False, "scanner": False},
    "voice":     {"timeline": True, "workspace": True, "omi_full": True, "omi_meta": True, "memories": True, "voice": True, "scanner": False},
}

# Which source keys map to which request-level source names
_SOURCE_TO_POLICY_KEYS = {
    "timeline":  ["timeline"],
    "channel":   ["timeline"],
    "workspace": ["workspace"],
    "omi":       ["omi_full", "omi_meta"],
    "memories":  ["memories"],
    "voice":     ["voice"],
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
        "summary", "summaries", "morning", "today", "yesterday", "raw",
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
        "summary", "summaries", "morning", "today", "yesterday", "raw",
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


async def _search_canonical_timeline(uid: str, query: str, limit: int) -> list:
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
            ORDER BY started_at DESC, id DESC
            LIMIT 300
            """,
            uid,
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


async def _fetch_recent_canonical_timeline(uid: str, limit: int = 30, max_chars: int = 9000) -> str:
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
            ORDER BY started_at DESC, id DESC
            LIMIT $2
            """,
            uid,
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


async def _search_workspace(uid: str, agent_id: str, query: str, limit: int) -> list:
    """Search workspace files via Provision API. Falls back to reading key files
    and doing keyword matching if no search endpoint exists."""
    results = []
    if not agent_id or not PROVISION_API_TOKEN:
        return results

    query_terms = _expand_query_terms(_significant_query_terms(query))
    if not query_terms:
        return results

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Try the search endpoint first
            resp = await client.get(
                f"{PROVISION_API_URL}/workspace/{agent_id}/search",
                params={"q": query},
                headers={"Authorization": f"Bearer {PROVISION_API_TOKEN}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("results", [])[:limit]:
                    results.append({
                        "source": "workspace",
                        "title": item.get("file", ""),
                        "content": (item.get("snippet", "") or item.get("content", ""))[:500],
                        "timestamp": "",
                        "score": item.get("score", 1),
                        "metadata": {"file": item.get("file", "")},
                    })
                return results

            # 404 = no search endpoint; fall back to reading key files
            if resp.status_code != 404:
                logger.warning(
                    f"[FLOW:UNIFIED-SEARCH] Provision search returned {resp.status_code}"
                )
                return results

            # Fallback: read files list and grep
            resp2 = await client.get(
                f"{PROVISION_API_URL}/workspace/{agent_id}/files",
                headers={"Authorization": f"Bearer {PROVISION_API_TOKEN}"},
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

                    results.append({
                        "source": "workspace",
                        "title": fname,
                        "content": snippet,
                        "timestamp": "",
                        "score": score,
                        "metadata": {"file": fname},
                    })

            # Sort by score desc, limit
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]

    except Exception as e:
        logger.warning(f"[FLOW:UNIFIED-SEARCH] Workspace search error: {e}")
        return results


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
                if hasattr(created, "strftime"):
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
                    "emoji": structured.get("emoji", ""),
                    "category": structured.get("category", ""),
                    "has_transcript_detail": bool(transcript_text and full_access),
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


async def _search_voice_logs(uid: str, agent_id: str, query: str, limit: int) -> list:
    """Search voice daily log files (voice/YYYY-MM-DD.md) via Provision API.
    Reads the last 7 days of logs and performs keyword matching."""
    results = []
    if not agent_id or not PROVISION_API_TOKEN:
        return results

    query_terms = _expand_query_terms(_significant_query_terms(query))
    if not query_terms:
        return results

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Read workspace files and filter to voice/ directory
            resp = await client.get(
                f"{PROVISION_API_URL}/workspace/{agent_id}/files",
                headers={"Authorization": f"Bearer {PROVISION_API_TOKEN}"},
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

                    results.append({
                        "source": "voice",
                        "title": fname,
                        "content": snippet,
                        "timestamp": fname.replace("voice/", "").replace(".md", ""),
                        "score": score,
                        "metadata": {"file": fname},
                    })

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
    if agent_role not in SEARCH_POLICY:
        raise HTTPException(status_code=400, detail=f"Invalid agent_role: {agent_role}. Must be one of: {list(SEARCH_POLICY.keys())}")

    # Validate agent-uid association
    if not _validate_agent_uid(agent_id, uid):
        raise HTTPException(status_code=403, detail="agent_id does not belong to this uid")

    _start = time.time()

    # Realtime voice needs a bounded fast path. If the compact Hermes memory
    # pack has a high-confidence answer, return it immediately and avoid
    # waiting on slower Firestore/workspace/Honcho fallback searches.
    if agent_role == "voice" and not requested_sources:
        fast_results = await _search_voice_memory_pack(uid, query, limit)
        if fast_results and fast_results[0].get("score", 0) >= 120:
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
                "total_results": len(fast_results),
                "query": query,
                "fast_path": True,
            }

    # Resolve agent_id from postgres if not provided
    if not agent_id:
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

    # Build task list for parallel fan-out
    tasks = []
    task_source_names = []

    if agent_role in {"voice", "user"} and (not requested_sources or "voice_memory" in requested_sources):
        tasks.append(_search_voice_memory_pack(uid, query, limit))
        task_source_names.append("voice_memory")

    if allowed.get("timeline"):
        tasks.append(_search_canonical_timeline(uid, query, limit))
        task_source_names.append("timeline")

    if allowed.get("workspace"):
        tasks.append(_search_workspace(uid, agent_id, query, limit))
        task_source_names.append("workspace")

    if allowed.get("omi_full") or allowed.get("omi_meta"):
        full_access = bool(allowed.get("omi_full"))
        tasks.append(_search_omi_conversations(uid, query, limit, full_access))
        task_source_names.append("omi")

    if allowed.get("memories"):
        tasks.append(_search_memories(uid, query, limit))
        task_source_names.append("memories")

    if allowed.get("voice"):
        tasks.append(_search_voice_logs(uid, agent_id, query, limit))
        task_source_names.append("voice")

    scanner_access = allowed.get("scanner")
    if scanner_access:
        access_level = "full" if scanner_access == "full" else "own"
        tasks.append(_search_scanner_logs(uid, query, limit, access_level))
        task_source_names.append("scanner")

    # Determine which sources were denied
    all_source_names = {"voice_memory", "timeline", "workspace", "omi", "memories", "voice", "scanner"}
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

    _elapsed = int((time.time() - _start) * 1000)
    logger.info(
        f"[FLOW:UNIFIED-SEARCH] uid={uid} role={agent_role} query=\"{query}\" "
        f"sources={sorted(sources_searched)} results={len(all_results)} "
        f"denied={sources_denied} latency={_elapsed}ms"
    )

    return {
        "results": all_results,
        "sources_searched": sorted(sources_searched),
        "sources_denied": sources_denied,
        "total_results": len(all_results),
        "query": query,
    }
