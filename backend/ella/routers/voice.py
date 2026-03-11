"""
Ella Voice Router - Voice session management for Ella Voice Service.

Endpoints:
- POST /v1/voice/session - Issue session token for V2V connection
- POST /v1/voice/tts - Text-to-speech synthesis (TTS providers only)
- GET /v1/voice/providers - List available voice providers
- GET /v1/voice/config - Get voice configuration
- GET /v1/voice/health - Health check

Provider types:
  tts  — Text-to-speech (request/response, POST /v1/voice/tts)
  v2v  — Voice-to-voice (WebSocket, bidirectional audio streaming via /session)
"""

import os
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header
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
ELLA_TTS_URL = os.getenv("ELLA_TTS_URL", "http://100.76.138.56:8930")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

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
    uid: Optional[str] = None,
    provider: str = "grok-voice",
    voice_mode: Optional[str] = None,
):
    """
    Create a voice session token for connecting to V2V service.

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

    # --- Reject unknown providers (no silent fallbacks) ---
    if provider != "elevenlabs":
        print(f"[FLOW:VOICE-TTS] ERROR unknown provider={provider!r}", flush=True)
        raise HTTPException(status_code=400, detail=f"Unknown TTS provider: {provider!r}. Valid: elevenlabs, fish-audio, fish-audio-s1, fish-audio-s2, kokoro, grok-voice, gemini-live")

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
        "tts_providers": ["elevenlabs", "fish-audio", "kokoro"],
        "tts_elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "tts_local_url": ELLA_TTS_URL,
        "v2v_providers": v2v_status,
    }
