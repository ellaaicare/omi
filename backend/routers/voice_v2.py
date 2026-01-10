"""
Voice Mode v2 Router - Pipecat Integration

This is a thin router that delegates to the integrations/pipecat module.
All business logic is in the integration module for modularity.

Supports two pipeline modes (configurable via VOICE_PIPELINE_MODE env var or query param):
- "pipecat": Default STT→LLM→TTS pipeline (2-3s latency)
- "grok_v2v": Grok voice-to-voice API (~500ms latency)

Endpoint: wss://api.ella-ai-care.com/v2/voice
"""

import uuid
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from integrations.pipecat import PipelineConfig
from integrations.pipecat.pipeline.manual_pipeline import run_manual_voice_session
from integrations.pipecat.pipeline.grok_v2v_pipeline import run_grok_v2v_session


router = APIRouter()


@router.websocket("/v2/voice")
async def voice_v2_endpoint(
    websocket: WebSocket,
    uid: str = Query(..., description="Firebase user ID"),
    session_id: Optional[str] = Query(None, description="Session ID (auto-generated if not provided)"),
    pipeline_mode: Optional[str] = Query(None, description="Pipeline mode: 'pipecat' (default) or 'grok_v2v'"),
):
    """
    Voice mode endpoint with configurable pipeline.

    Supports two pipeline modes:
    - "pipecat" (default): STT→LLM→TTS pipeline (2-3s latency)
    - "grok_v2v": Grok voice-to-voice API (~500ms latency)

    This endpoint handles real-time voice conversations. It provides:
    - Server-side VAD (Voice Activity Detection) via Silero
    - Automatic end-of-speech detection
    - Interruption/barge-in support
    - Integration with Ella's n8n agents

    Query Parameters:
        uid: Firebase user ID (required)
        session_id: Optional session identifier (generated if not provided)
        pipeline_mode: 'pipecat' or 'grok_v2v' (overrides env var)

    Protocol:
        - Client sends raw PCM16 audio at 16kHz (both modes)
        - Server sends TTS audio chunks back (24kHz for both modes)
        - VAD handles turn-taking automatically (pipecat mode)

    Example:
        # Default (pipecat) mode:
        ws = websocket.connect("wss://api.ella-ai-care.com/v2/voice?uid=abc123")

        # Ultra-low latency (grok_v2v) mode:
        ws = websocket.connect("wss://api.ella-ai-care.com/v2/voice?uid=abc123&pipeline_mode=grok_v2v")
    """
    await websocket.accept()

    session_id = session_id or str(uuid.uuid4())
    config = PipelineConfig()

    # Determine pipeline mode: query param > env var > default
    mode = pipeline_mode or config.voice_pipeline_mode

    print(f"🎤 Voice v2 connection: uid={uid}, session={session_id[:8]}, mode={mode}")

    try:
        if mode == "grok_v2v":
            # Ultra-low latency Grok Voice-to-Voice mode (~500ms)
            await run_grok_v2v_session(
                websocket=websocket,
                uid=uid,
                session_id=session_id,
                config=config,
            )
        else:
            # Default Pipecat pipeline (STT→LLM→TTS, 2-3s latency)
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
        "version": "2.2.0",  # Added Grok V2V support
        "endpoint": "/v2/voice",
        "pipeline_mode": config.voice_pipeline_mode,
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
        "grok_v2v": {
            "enabled": config.grok_v2v.enabled,
            "proxy_url": config.grok_v2v.proxy_url,
            "model": config.grok_v2v.model,
            "input_sample_rate": config.grok_v2v.input_sample_rate,
            "output_sample_rate": config.grok_v2v.output_sample_rate,
        },
        "dependencies": {
            "deepgram_key_set": bool(config.stt.api_key),
            "openai_key_set": bool(config.tts.api_key),
            "groq_key_set": bool(config.llm.api_key),
            "xai_key_set": bool(config.grok_v2v.api_key),
        },
        "features": {
            "tts_streaming": "Stream TTS audio for lower TTFB",
            "barge_in": "Interrupt AI with speech to cancel TTS",
            "grok_v2v": "Ultra-low latency voice-to-voice (~500ms)",
        },
    }
