"""
Ella Voice Router - Voice session management for Ella Voice Service.

Endpoints:
- POST /v1/voice/session - Issue session token for Ella Voice connection
- GET /v1/voice/config - Get voice configuration

This implements the hybrid auth approach:
1. Omi iOS gets session token from this endpoint (with Firebase auth)
2. iOS connects directly to Ella Voice with token (lowest latency)
3. Ella Voice validates token and fetches context
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


# ============================================================================
# Request/Response Models
# ============================================================================


class VoiceSessionResponse(BaseModel):
    """Response containing session token and connection details."""

    session_token: str
    voice_endpoint: str
    expires_in: int  # seconds
    audio_format: dict


class VoiceConfigResponse(BaseModel):
    """Voice configuration for iOS client."""

    sample_rate: int = 24000
    channels: int = 1
    encoding: str = "pcm_int16"
    byte_order: str = "little_endian"


# ============================================================================
# Helper Functions
# ============================================================================


def create_session_token(uid: str, firebase_uid: str, display_name: Optional[str] = None) -> str:
    """
    Create a JWT session token for Ella Voice connection.

    Token payload includes:
    - uid: Omi user ID
    - firebase_uid: Firebase UID for user lookup in Ella's DB
    - name: Display name for personalization
    - context_url: Where Ella can fetch user context
    - callback_url: Where Ella posts session results
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


@router.post("/session", response_model=VoiceSessionResponse)
async def create_voice_session(
    uid: Optional[str] = None,
    # In production, use: current_user = Depends(get_current_user)
):
    """
    Create a voice session token for connecting to Ella Voice Service.

    Flow:
    1. iOS calls this endpoint with Firebase auth
    2. Backend validates auth and issues short-lived JWT
    3. iOS connects to Ella Voice with: ?token=<jwt>&source=omi
    4. Ella validates token and starts session

    Returns:
    - session_token: JWT for Ella Voice authentication
    - voice_endpoint: WebSocket URL to connect to
    - expires_in: Token lifetime in seconds
    - audio_format: Required audio configuration
    """
    # TODO: In production, get uid from Firebase auth
    # For now, accept uid as parameter for testing
    if not uid:
        raise HTTPException(status_code=400, detail="uid required")

    _start = time.time()

    try:
        token = create_session_token(
            uid=uid,
            firebase_uid=uid,  # In production, get from auth
            display_name=None,  # In production, get from user profile
        )

        _elapsed = int((time.time() - _start) * 1000)
        print(f"[FLOW:VOICE-SESSION] uid={uid} endpoint={ELLA_VOICE_ENDPOINT} expiry={SESSION_EXPIRY_HOURS}h latency={_elapsed}ms", flush=True)

        return VoiceSessionResponse(
            session_token=token,
            voice_endpoint=ELLA_VOICE_ENDPOINT,
            expires_in=SESSION_EXPIRY_HOURS * 3600,
            audio_format={"sample_rate": 24000, "channels": 1, "encoding": "pcm_int16", "byte_order": "little_endian"},
        )

    except Exception as e:
        logger.error(f"[FLOW:VOICE-SESSION] uid={uid} error={e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config", response_model=VoiceConfigResponse)
async def get_voice_config():
    """
    Get audio configuration for Ella Voice.

    iOS must use these exact settings for compatibility:
    - 24kHz sample rate
    - Mono channel
    - PCM Int16 encoding
    - Little-endian byte order
    """
    return VoiceConfigResponse(sample_rate=24000, channels=1, encoding="pcm_int16", byte_order="little_endian")


class TtsRequest(BaseModel):
    """Request for text-to-speech synthesis."""

    text: str
    voice_id: str = "pFZP5JQG7iQjIQuC4Bku"  # Lily voice


ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELLA_TTS_URL = os.getenv("ELLA_TTS_URL", "http://100.76.138.56:8930")


@router.post("/tts")
async def synthesize_speech(
    request: TtsRequest,
    x_tts_provider: Optional[str] = Header(default=None, alias="X-TTS-Provider"),
):
    """
    Synthesize speech from text.

    Routes to TTS backend based on X-TTS-Provider header:
      elevenlabs  — ElevenLabs API (default)
      fish-audio  — Fish Audio S1 via local ella-tts server (Mac Mini :8930)
      kokoro      — Kokoro-82M via local ella-tts server (Mac Mini :8930)
    """
    _start = time.time()
    text_len = len(request.text)
    provider = (x_tts_provider or "elevenlabs").lower()
    text = request.text[:500]  # Cap at 500 chars

    # --- Local ella-tts proxy (Fish Audio S1 or Kokoro) ---
    if provider in ("fish-audio", "kokoro"):
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

    # --- ElevenLabs (default) ---
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
    return {
        "status": "ok",
        "service": "ella-voice",
        "voice_endpoint": ELLA_VOICE_ENDPOINT,
        "session_secret_configured": bool(ELLA_SESSION_SECRET),
        "tts_elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "tts_local_url": ELLA_TTS_URL,
        "tts_providers": ["elevenlabs", "fish-audio", "kokoro"],
    }
