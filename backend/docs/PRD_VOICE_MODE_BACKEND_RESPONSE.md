# PRD: Voice Mode - Backend Implementation Response

**Version**: 1.0
**Date**: November 29, 2025
**Author**: OMI Backend Team
**In Response To**: Ella AI Integration Team PRD

---

## Executive Summary

We accept the proposed architecture. OMI Backend will serve as the Voice Mode Orchestrator, consuming n8n's streaming text via SSE and handling all audio I/O with iOS devices.

**Key Commitment**: Backend owns WebSocket to iOS, STT, TTS, and audio streaming. n8n owns agent routing and text generation via SSE.

---

## Architecture Confirmation

```
iOS Device ←──WSS──→ OMI Backend ←──SSE──→ n8n ←──REST──→ Letta
                         │
                         ├── STT (Deepgram/On-device)
                         ├── TTS (OpenAI, modular for ElevenLabs)
                         └── Audio streaming (binary over WSS)
```

---

## Implementation Scope

### What Backend Will Implement

| Component | Description | Effort |
|-----------|-------------|--------|
| Voice mode state machine | LISTENING → TRANSCRIBING → THINKING → SPEAKING | 4h |
| SSE client | Consume `/webhook/voice-stream` responses | 2h |
| Streaming TTS | OpenAI initially, modular for ElevenLabs | 4h |
| WebSocket binary streaming | Audio chunks back to iOS | 2h |
| Voice mode events | New WebSocket message types | 2h |
| `/v1/ella/voice-call` | Ella-initiated call endpoint | 2h |

**Total: ~2-3 days**

### What We Need from n8n

1. **`/webhook/voice-stream` endpoint** - SSE streaming as specified in your PRD
2. **Endpoint ready signal** - Let us know when ready for integration testing
3. **Error event format** - Confirm SSE error events structure

---

## WebSocket Protocol (iOS ↔ Backend)

### Voice Mode Events (NEW)

```typescript
// ═══════════════════════════════════════════════════════════════
// iOS → Backend
// ═══════════════════════════════════════════════════════════════

// Start voice mode (button press or wake word)
{
  "event": "voice_mode_start",
  "trigger": "button" | "wake_word",
  "wake_word_confidence": 0.95  // optional, for wake word trigger
}

// Audio chunk (during voice mode)
{
  "event": "voice_audio",
  "data": "<base64-pcm-audio>",
  "sequence": 1,
  "is_final": false  // true = end of utterance
}

// Stop voice mode
{
  "event": "voice_mode_stop",
  "reason": "user_request" | "silence_timeout" | "error"
}

// ═══════════════════════════════════════════════════════════════
// Backend → iOS
// ═══════════════════════════════════════════════════════════════

// Voice mode activated
{
  "event": "voice_mode_active",
  "session_id": "voice-session-abc123",
  "timeout_seconds": 10
}

// Transcription update (for UI display)
{
  "event": "voice_transcription",
  "text": "What's my appointment",
  "is_final": false
}

// Agent status
{
  "event": "voice_status",
  "status": "listening" | "transcribing" | "thinking" | "speaking"
}

// Audio response chunk
{
  "event": "voice_response_audio",
  "data": "<base64-audio-chunk>",
  "sequence": 1,
  "format": "pcm16",
  "sample_rate": 24000
}

// Response complete
{
  "event": "voice_response_complete",
  "text": "Your appointment is tomorrow at 2 PM.",
  "duration_ms": 2340
}

// Voice mode ended
{
  "event": "voice_mode_ended",
  "reason": "user_request" | "silence_timeout" | "error" | "agent_farewell",
  "session_duration_seconds": 45
}

// Error
{
  "event": "voice_error",
  "code": "tts_failed" | "agent_timeout" | "transcription_failed",
  "message": "TTS generation failed, please try again"
}
```

---

## SSE Client Implementation

### Consuming n8n Stream

```python
import httpx
from httpx_sse import aconnect_sse

async def stream_from_n8n(uid: str, transcript: str, session_id: str):
    """Consume SSE stream from n8n voice endpoint."""

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with aconnect_sse(
            client,
            "POST",
            "https://n8n.ella-ai-care.com/webhook/voice-stream",
            json={
                "uid": uid,
                "transcript": transcript,
                "source": "voice",
                "session_id": session_id,
            }
        ) as event_source:
            async for sse in event_source.aiter_sse():
                if sse.event == "chunk":
                    data = json.loads(sse.data)
                    yield data["text"], data["done"]
                elif sse.event == "done":
                    data = json.loads(sse.data)
                    yield "", True
                elif sse.event == "error":
                    raise Exception(f"n8n error: {sse.data}")
```

---

## TTS Implementation (Modular)

### Base Interface

```python
# backend/utils/voice/tts_provider.py

from abc import ABC, abstractmethod
from typing import AsyncIterator

class TTSProvider(ABC):
    @abstractmethod
    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio chunks as they're generated."""
        pass

    @abstractmethod
    def get_audio_format(self) -> dict:
        """Return audio format info for client."""
        pass
```

### OpenAI Implementation (Phase 1)

```python
# backend/utils/voice/providers/openai_tts.py

class OpenAITTSProvider(TTSProvider):
    def __init__(self, voice: str = "nova"):
        self.voice = voice
        self.client = openai.AsyncOpenAI()

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        response = await self.client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text,
            response_format="pcm",  # Raw PCM for streaming
        )

        # Stream chunks
        async for chunk in response.iter_bytes(chunk_size=4096):
            yield chunk

    def get_audio_format(self) -> dict:
        return {
            "format": "pcm16",
            "sample_rate": 24000,
            "channels": 1
        }
```

### ElevenLabs Ready (Phase 2)

```python
# backend/utils/voice/providers/elevenlabs_tts.py

class ElevenLabsTTSProvider(TTSProvider):
    # Lower latency streaming, implement when needed
    pass
```

---

## Voice Mode State Machine

```python
# backend/utils/voice/state_machine.py

from enum import Enum

class VoiceState(Enum):
    INACTIVE = "inactive"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"

class VoiceModeSession:
    def __init__(self, uid: str, websocket: WebSocket):
        self.uid = uid
        self.websocket = websocket
        self.session_id = str(uuid.uuid4())
        self.state = VoiceState.INACTIVE
        self.silence_timeout = 10  # seconds, configurable
        self.last_activity = time.time()
        self.conversation_history = []  # Multi-turn context

    async def start(self):
        self.state = VoiceState.LISTENING
        await self.send_event("voice_mode_active", {
            "session_id": self.session_id,
            "timeout_seconds": self.silence_timeout
        })

    async def handle_audio(self, audio_data: bytes, is_final: bool):
        if is_final:
            self.state = VoiceState.TRANSCRIBING
            await self.send_status("transcribing")

            # Transcribe
            transcript = await self.transcribe(audio_data)
            self.conversation_history.append({"role": "user", "text": transcript})

            # Get agent response
            self.state = VoiceState.THINKING
            await self.send_status("thinking")

            response_text = ""
            async for chunk, done in stream_from_n8n(self.uid, transcript, self.session_id):
                response_text += chunk
                if done:
                    break

            # Generate and stream TTS
            self.state = VoiceState.SPEAKING
            await self.send_status("speaking")

            tts_provider = get_tts_provider()
            seq = 0
            async for audio_chunk in tts_provider.stream_audio(response_text):
                await self.send_audio(audio_chunk, seq)
                seq += 1

            self.conversation_history.append({"role": "assistant", "text": response_text})

            # Back to listening
            self.state = VoiceState.LISTENING
            await self.send_status("listening")
            self.last_activity = time.time()

    async def check_timeout(self):
        if time.time() - self.last_activity > self.silence_timeout:
            await self.end("silence_timeout")

    async def end(self, reason: str):
        self.state = VoiceState.INACTIVE
        await self.send_event("voice_mode_ended", {
            "reason": reason,
            "session_duration_seconds": int(time.time() - self.start_time)
        })
        # n8n handles pushing conversation to memory/summary
```

---

## Ella-Initiated Calls

### Endpoint

```python
# POST /v1/ella/voice-call

class VoiceCallRequest(BaseModel):
    uid: str
    reason: str  # "medication_reminder", "check_in", etc.
    urgency: str = "normal"  # "critical", "high", "normal"
    message: Optional[str] = None  # Ella's opening message

@router.post("/v1/ella/voice-call")
async def initiate_voice_call(request: VoiceCallRequest):
    """Allow n8n/Letta to initiate a voice call to user."""

    # Send push notification to iOS
    await send_voice_call_notification(
        uid=request.uid,
        reason=request.reason,
        urgency=request.urgency,
        message=request.message
    )

    return {"status": "call_initiated", "uid": request.uid}
```

### Push Notification Format

```json
{
  "action": "incoming_voice_call",
  "reason": "medication_reminder",
  "urgency": "high",
  "message": "Hi! I wanted to remind you about your medication.",
  "auto_accept": false
}
```

---

## File Structure (New)

```
backend/
├── utils/
│   └── voice/                          # NEW: Voice mode module
│       ├── __init__.py
│       ├── state_machine.py            # Voice session state
│       ├── sse_client.py               # n8n SSE consumer
│       ├── tts_provider.py             # TTS base interface
│       └── providers/
│           ├── __init__.py
│           ├── openai_tts.py           # OpenAI TTS (Phase 1)
│           └── elevenlabs_tts.py       # ElevenLabs (Phase 2)
├── routers/
│   └── transcribe.py                   # Add voice mode handling
└── docs/
    └── PRD_VOICE_MODE_BACKEND_RESPONSE.md
```

---

## Configuration

```python
# Environment variables
VOICE_MODE_ENABLED = true
VOICE_SILENCE_TIMEOUT_SECONDS = 10
VOICE_TTS_PROVIDER = "openai"  # or "elevenlabs"
VOICE_TTS_VOICE = "nova"
VOICE_SSE_ENDPOINT = "https://n8n.ella-ai-care.com/webhook/voice-stream"
```

---

## Answers to n8n Team Questions

1. **Transcription**: Using Deepgram (already integrated) or on-device ASR from iOS. Both work.

2. **TTS Voice**: Starting with OpenAI `nova` voice. Can configure per-user later.

3. **iOS Audio Format**: Opus or PCM16. Will handle both.

4. **WebSocket Protocol**: Documented above. Voice mode events are additive to existing protocol.

5. **Wake Word**: On-device (iOS handles). Backend receives `voice_mode_start` event.

6. **Rate Limits**: Monitoring. Will add caching for repeated TTS phrases.

---

## Timeline

| Week | Deliverable |
|------|-------------|
| Week 1 | Voice mode state machine + WebSocket events |
| Week 1 | SSE client integration |
| Week 2 | Streaming TTS (OpenAI) |
| Week 2 | Integration testing with n8n |
| Week 3 | Ella-initiated calls |
| Week 3 | Polish + edge cases |

---

## Dependencies

- **n8n**: `/webhook/voice-stream` SSE endpoint ready
- **iOS**: Voice mode UI + WebSocket event handling
- **Testing**: End-to-end test environment

---

## Open Items

1. **Memory/Summary**: Confirmed n8n handles pushing conversation to Letta after call ends. Backend just streams.

2. **Interruption**: Phase 2 feature. User can say "stop" mid-response.

3. **Multi-language**: Use same language as transcript for TTS.

---

**Ready to proceed. Please confirm `/webhook/voice-stream` endpoint availability for integration testing.**
