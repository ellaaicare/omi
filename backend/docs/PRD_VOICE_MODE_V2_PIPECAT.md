# PRD: Voice Mode v2 - Pipecat Integration

**Version**: 1.0
**Date**: November 30, 2025
**Author**: OMI Backend Team
**Status**: Proposed

---

## Executive Summary

Replace our custom voice mode implementation with Pipecat, an open-source real-time voice AI framework. This provides built-in VAD, interruption handling, and a proven pipeline architecture - solving the turn-detection challenges documented in the iOS team's analysis.

**Key Benefits**:
- Built-in Silero VAD for accurate end-of-speech detection
- Native interruption/barge-in support
- Swift SDK available for iOS
- Provider-agnostic (swap TTS/STT/LLM easily)
- Battle-tested framework with active community

---

## Problem Statement

### Current v1 Challenges (from iOS Analysis)

| Issue | Impact | Root Cause |
|-------|--------|------------|
| No reliable end-of-speech detection | 3s fixed timeout feels slow | iOS ASR never sends `isFinal=true` |
| Keep-alive packets confuse silence detection | False positives | Continuous ASR mode |
| No interruption support | Can't interrupt Ella mid-response | Not implemented |
| Custom protocol | Maintenance burden | Built from scratch |

### Why Pipecat Solves This

| Feature | Pipecat | Our v1 |
|---------|---------|--------|
| VAD (Voice Activity Detection) | ✅ Silero built-in | ❌ Not used |
| End-of-speech detection | ✅ Automatic | ⚠️ 3s text-change timeout |
| Interruption handling | ✅ Native barge-in | ❌ Not supported |
| Turn-taking | ✅ SmartTurnAnalyzer | ❌ Manual |
| Provider flexibility | ✅ 25+ TTS, 18+ STT | ⚠️ OpenAI TTS only |
| iOS SDK | ✅ Swift SDK | ❌ Custom WebSocket |

---

## Proposed Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                     PIPECAT v2 ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  iOS App (Pipecat Swift SDK)                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ • Native audio capture                                       │   │
│  │ • WebSocket/WebRTC transport                                 │   │
│  │ • Audio playback with interruption                          │   │
│  └──────────────────────────┬──────────────────────────────────┘   │
│                             │                                       │
│                             ▼                                       │
│  OMI Backend (/v2/voice endpoint)                                  │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    PIPECAT PIPELINE                          │   │
│  │                                                              │   │
│  │  ┌─────────┐   ┌─────┐   ┌─────────┐   ┌─────┐   ┌───────┐ │   │
│  │  │Transport│──▶│ VAD │──▶│   STT   │──▶│ LLM │──▶│  TTS  │ │   │
│  │  │  Input  │   │Silero│   │Deepgram │   │Groq │   │OpenAI │ │   │
│  │  └─────────┘   └─────┘   └─────────┘   └──┬──┘   └───┬───┘ │   │
│  │                                           │          │      │   │
│  │                              ┌────────────┘          │      │   │
│  │                              ▼                       ▼      │   │
│  │                    ┌─────────────────┐      ┌───────────┐  │   │
│  │                    │ n8n Config Hook │      │ Transport │  │   │
│  │                    │ (system prompt, │      │  Output   │  │   │
│  │                    │  memory blocks) │      └───────────┘  │   │
│  │                    └─────────────────┘                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Endpoint Design

**v1 (Keep for backwards compatibility)**:
```
wss://api.ella-ai-care.com/v4/listen  → Current voice mode
```

**v2 (New Pipecat endpoint)**:
```
wss://api.ella-ai-care.com/v2/voice   → Pipecat pipeline
```

---

## Technical Specification

### Dependencies

```toml
# Add to requirements.txt
pipecat-ai[silero,groq,openai,websocket]>=0.0.54
```

Includes:
- `silero` - VAD for end-of-speech detection
- `groq` - LLM provider
- `openai` - TTS provider
- `websocket` - FastAPI WebSocket transport

### Pipeline Components

```python
# backend/routers/voice_v2.py

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams
)
from pipecat.services.groq import GroqLLMService
from pipecat.services.openai import OpenAITTSService
from pipecat.vad.silero import SileroVADAnalyzer

async def voice_v2_endpoint(websocket: WebSocket, uid: str):
    """Pipecat-powered voice mode endpoint."""

    # 1. Fetch config from n8n (our custom hook)
    config = await get_voice_config(uid, session_id)

    # 2. Configure transport
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                stop_secs=1.5,  # 1.5s silence = end of turn
            ),
        )
    )

    # 3. Configure services
    llm = GroqLLMService(
        model=config["agent_config"]["model"],
        api_key=os.getenv("GROQ_API_KEY"),
    )

    tts = OpenAITTSService(
        voice="nova",
        api_key=os.getenv("OPENAI_API_KEY"),
    )

    # 4. Build system prompt with n8n memory blocks
    system_prompt = build_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]

    # 5. Create pipeline
    pipeline = Pipeline([
        transport.input(),   # Audio from iOS
        llm,                 # Process with Groq
        tts,                 # Convert to speech
        transport.output(),  # Audio back to iOS
    ])

    # 6. Run pipeline
    task = PipelineTask(pipeline)
    runner = PipelineRunner()
    await runner.run(task)
```

### n8n Integration Hook

```python
# backend/utils/voice/pipecat_hooks.py

class EllaNConfigProcessor(FrameProcessor):
    """
    Custom Pipecat processor to inject n8n agent config.

    Fetches system prompt and memory blocks from n8n,
    injects into LLM context.
    """

    def __init__(self, uid: str):
        super().__init__()
        self.uid = uid
        self.config = None

    async def process_frame(self, frame: Frame):
        if isinstance(frame, StartFrame):
            # Fetch config at session start
            self.config = await get_voice_config(self.uid)

        elif isinstance(frame, LLMMessagesFrame):
            # Inject system prompt with memory blocks
            if self.config:
                frame.messages.insert(0, {
                    "role": "system",
                    "content": self.config["agent_config"]["system_prompt"]
                })

        await self.push_frame(frame)
```

### Conversation Storage Hook

```python
# backend/utils/voice/pipecat_hooks.py

class EllaConversationLogger(FrameProcessor):
    """
    Logs conversation turns to Firestore with role tags.
    Calls summary/memory agents on session end.
    """

    def __init__(self, uid: str, session_id: str):
        super().__init__()
        self.uid = uid
        self.session_id = session_id
        self.turns = []

    async def process_frame(self, frame: Frame):
        if isinstance(frame, TranscriptionFrame):
            # User speech
            self.turns.append({
                "role": "user",
                "content": frame.text,
                "timestamp": time.time()
            })

        elif isinstance(frame, LLMResponseEndFrame):
            # Assistant response
            self.turns.append({
                "role": "assistant",
                "content": frame.text,
                "timestamp": time.time()
            })

        elif isinstance(frame, EndFrame):
            # Session ended - store and call agents
            await self._store_conversation()
            await self._call_agents()

        await self.push_frame(frame)

    async def _store_conversation(self):
        """Store with role tags for context."""
        # Uses existing utils/voice/handler.py logic
        pass

    async def _call_agents(self):
        """Call n8n summary/memory agents."""
        # Uses existing utils/ella/ integration
        pass
```

---

## iOS Changes Required

### Option A: Use Pipecat Swift SDK (Recommended)

```swift
// Replace custom WebSocket with Pipecat SDK
import PipecatClientSwift

let client = PipecatClient(
    baseUrl: "wss://api.ella-ai-care.com/v2/voice",
    options: PipecatOptions(
        enableVAD: true,
        enableInterruption: true
    )
)

// Audio handling is automatic
client.start()

// Interruption is handled by SDK
client.onInterruption = {
    // Ella was interrupted
}

client.onSpeechEnd = {
    // Automatic - no more 3s timeout!
}
```

### Option B: Keep Custom WebSocket (Minimal Changes)

If iOS team prefers to keep current implementation:
1. Connect to `/v2/voice` instead of `/v4/listen`
2. Send raw audio (not text transcripts)
3. Receive audio (same as current)
4. Pipecat handles VAD/turn-detection server-side

---

## Migration Plan

### Phase 1: Backend v2 Endpoint (Week 1)

| Task | Owner | Effort |
|------|-------|--------|
| Install pipecat dependencies | Backend | 1h |
| Create `/v2/voice` router | Backend | 4h |
| Integrate n8n config hook | Backend | 2h |
| Add conversation storage hook | Backend | 2h |
| Test with WebSocket client | Backend | 2h |

### Phase 2: iOS Integration (Week 2)

| Task | Owner | Effort |
|------|-------|--------|
| Evaluate Pipecat Swift SDK | iOS | 4h |
| Implement SDK or adapt WebSocket | iOS | 1-2d |
| Test end-to-end | Both | 4h |
| Remove 3s timeout logic | iOS | 1h |

### Phase 3: Validation & Rollout (Week 3)

| Task | Owner | Effort |
|------|-------|--------|
| A/B test v1 vs v2 latency | Both | 2h |
| Test interruption handling | Both | 2h |
| Measure end-of-speech accuracy | Both | 2h |
| Gradual rollout | Both | - |

---

## Success Metrics

| Metric | v1 Current | v2 Target |
|--------|------------|-----------|
| End-of-speech detection | 3s fixed timeout | <500ms VAD-based |
| Interruption support | ❌ None | ✅ Native |
| Time to first audio | ~850ms | ~600ms |
| iOS code complexity | High (custom) | Low (SDK) |
| Backend code complexity | Medium (custom) | Low (pipeline) |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Pipecat learning curve | Delay | Start with minimal pipeline |
| n8n integration complexity | Custom code needed | Reuse existing hooks |
| iOS SDK adoption | Team preference | Offer both SDK and raw WebSocket |
| Production stability | New framework | Keep v1 as fallback |

---

## Rollback Plan

If Pipecat v2 has issues:
1. Keep `/v4/listen` (v1) running
2. iOS can switch endpoints via config
3. No data migration needed (same Firestore)

---

## Questions for Team

1. **iOS Team**: Preference for Pipecat Swift SDK vs adapting current WebSocket?
2. **n8n Team**: Any concerns with config hook approach?
3. **Timeline**: Can we start next sprint?

---

## References

- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Docs](https://docs.pipecat.ai/)
- [Pipecat Swift SDK](https://github.com/pipecat-ai/pipecat-client-swift)
- [FastAPI WebSocket Transport](https://docs.pipecat.ai/server/services/transport/fastapi-websocket)
- [iOS Voice Mode Analysis](../../../app/docs/VOICE_MODE_ASR_ANALYSIS.md)

---

**Ready for review. Feedback welcome!**
