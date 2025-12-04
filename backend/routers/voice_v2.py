"""
Voice Mode v2 Router - Pipecat Integration

This is a thin router that delegates to the integrations/pipecat module.
All business logic is in the integration module for modularity.

Endpoint: wss://api.ella-ai-care.com/v2/voice
"""

import uuid
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from integrations.pipecat import PipelineConfig
from integrations.pipecat.pipeline.manual_pipeline import run_manual_voice_session


router = APIRouter()


@router.websocket("/v2/voice")
async def voice_v2_endpoint(
    websocket: WebSocket,
    uid: str = Query(..., description="Firebase user ID"),
    session_id: Optional[str] = Query(None, description="Session ID (auto-generated if not provided)"),
):
    """
    Pipecat-powered voice mode endpoint.

    This endpoint handles real-time voice conversations using the Pipecat
    framework. It provides:
    - Server-side VAD (Voice Activity Detection) via Silero
    - Automatic end-of-speech detection
    - Interruption/barge-in support
    - Integration with Ella's n8n agents

    Query Parameters:
        uid: Firebase user ID (required)
        session_id: Optional session identifier (generated if not provided)

    Protocol:
        - Client sends raw PCM16 audio at 16kHz
        - Server sends TTS audio chunks back
        - VAD handles turn-taking automatically

    Example:
        ws = websocket.connect("wss://api.ella-ai-care.com/v2/voice?uid=abc123")
        ws.send(audio_bytes)  # PCM16, 16kHz
        audio_response = ws.recv()  # TTS audio
    """
    await websocket.accept()

    session_id = session_id or str(uuid.uuid4())

    print(f"🎤 Voice v2 connection: uid={uid}, session={session_id[:8]}")

    try:
        # Use manual pipeline (Option B - industry standard approach)
        # Uses Pipecat services but bypasses broken transport layer
        await run_manual_voice_session(
            websocket=websocket,
            uid=uid,
            session_id=session_id,
        )

    except WebSocketDisconnect:
        print(f"🔌 Voice v2 disconnected: {session_id[:8]}")

    except Exception as e:
        print(f"❌ Voice v2 error: {e}")
        # Try to close cleanly
        try:
            await websocket.close(code=1011, reason=str(e))
        except:
            pass

    finally:
        print(f"🔚 Voice v2 session ended: {session_id[:8]}")


@router.get("/v2/voice/health")
async def voice_v2_health():
    """
    Health check for voice v2 endpoint.

    Returns configuration status and dependency availability.
    """
    config = PipelineConfig()

    return {
        "status": "ok",
        "version": "2.1.1",  # Removed fallback for debugging
        "endpoint": "/v2/voice",
        "config": {
            "vad_provider": config.vad.provider,
            "vad_stop_secs": config.vad.stop_secs,
            "stt_provider": config.stt.provider,
            "stt_model": config.stt.model,
            "tts_provider": config.tts.provider,
            "tts_voice": config.tts.voice,
            "tts_streaming": config.tts.streaming,
            "llm_provider": config.llm.provider,
            "llm_model": config.llm.model,
        },
        "dependencies": {
            "deepgram_key_set": bool(config.stt.api_key),
            "openai_key_set": bool(config.tts.api_key),
            "groq_key_set": bool(config.llm.api_key),
        },
        "features": {
            "tts_streaming": "Stream TTS audio for lower TTFB",
            "barge_in": "Interrupt AI with speech to cancel TTS",
        },
    }
