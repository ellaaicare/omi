"""
Ella Voice Router - Voice session management for Ella Voice Service.

Endpoints:
- POST /v1/voice/session - Issue session token for V2V connection
- POST /v1/voice/tts - Text-to-speech synthesis (TTS providers only)
- POST /v1/voice/context - Assemble user context for voice proxy at call start
- GET /v1/voice/providers - List available voice providers
- GET /v1/voice/config - Get voice configuration
- GET /v1/voice/health - Health check

Provider types:
  tts  — Text-to-speech (request/response, POST /v1/voice/tts)
  v2v  — Voice-to-voice (WebSocket, bidirectional audio streaming via /session)
"""

import base64
import json
import os
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel

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
PROVISION_API_URL = os.getenv("ELLA_PROVISION_API_URL", "http://100.76.138.56:8200")
PROVISION_API_TOKEN = os.getenv("ELLA_PROVISION_API_TOKEN", "")
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
        "default_mode": "v3-rich",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",
        "key_check": lambda: bool(ELLA_SESSION_SECRET),
    },
    "gemini-live": {
        "name": "Gemini Live (V2V)",
        "description": "Voice-to-voice via Gemini Live API, multimodal",
        "default_mode": "gemini-live",
        "endpoint_env": "ELLA_VOICE_ENDPOINT",  # Same proxy, different mode
        "key_check": lambda: bool(GEMINI_API_KEY),
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
            "name": "Gemini Live (V2V)",
            "type": "v2v",
            "description": "Voice-to-voice via Gemini Live API, multimodal",
            "available": V2V_PROVIDERS["gemini-live"]["key_check"](),
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

    try:
        token = create_session_token(
            uid=uid,
            firebase_uid=uid,
            display_name=None,
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

    Returns structured context: user info, soul, user profile, conditions,
    medications, recent voice summaries, dynamic rules, and voice rules.
    """
    body = await request.json()
    uid = body.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")

    budget = body.get("context_budget", 8000)
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

    # 4. Assemble context (trim to budget)
    soul = files.get("SOUL.md", "")[:3000]
    user_profile = files.get("USER.md", "")[:2000]
    voice_summaries = files.get("voice-summaries.md", "")[:1500]
    memory = files.get("MEMORY.md", "")[:500]

    # conditions and medications are text[] arrays in postgres
    conditions_list = list(row["conditions"] or [])
    medications_list = list(row["medications"] or [])

    _elapsed = int((time.time() - _start) * 1000)
    print(
        f"[FLOW:VOICE-CONTEXT] uid={uid} user={row['name']} agent={agent_id} "
        f"files={len(files)} rules={len(dynamic_rules)} latency={_elapsed}ms",
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
        "caregiver_notes": _extract_caregiver_section(user_profile),
        "dynamic_rules": dynamic_rules,
        "voice_rules": VOICE_INTERACTION_RULES,
    }
