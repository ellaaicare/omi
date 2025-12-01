# PRD: Pipecat v2 Backend Implementation

**Version**: 1.0
**Date**: December 1, 2025
**Owner**: Backend Team
**Status**: Approved
**Branch**: `feature/voice-mode-pipecat`

---

## Overview

Implement `/v2/voice` WebSocket endpoint using Pipecat framework for real-time voice conversations with Ella AI.

**Goal**: Replace fragile custom turn-detection with Pipecat's built-in VAD and pipeline architecture.

---

## Scope

### In Scope
- New `/v2/voice` WebSocket endpoint
- Pipecat pipeline with Silero VAD
- n8n config hook integration
- Conversation storage with role tags
- Session analytics tracking

### Out of Scope
- Changes to `/v4/listen` (keep as-is)
- WebRTC transport (Phase 2)
- Per-user voice preferences (n8n Phase 2)

---

## Technical Specification

### Dependencies

```txt
# Add to requirements.txt
pipecat-ai[silero,groq,openai,deepgram,websocket]>=0.0.54
```

### File Structure (Modular Design)

All Pipecat code lives in a self-contained `integrations/pipecat/` folder that can be extracted to a separate package later.

```
backend/
├── routers/
│   └── voice_v2.py                    # Thin router, delegates to integration
│
├── integrations/                       # NEW: All custom integrations
│   └── pipecat/                        # Self-contained Pipecat module
│       ├── __init__.py                 # Public API exports
│       ├── README.md                   # Module documentation
│       ├── requirements.txt            # Pipecat-specific deps
│       │
│       ├── pipeline/                   # Core pipeline
│       │   ├── __init__.py
│       │   ├── builder.py              # Pipeline factory
│       │   └── config.py               # Pipeline configuration
│       │
│       ├── processors/                 # Custom Pipecat processors
│       │   ├── __init__.py
│       │   ├── ella_config.py          # n8n config injection
│       │   ├── conversation_logger.py  # Turn logging
│       │   └── analytics.py            # Session metrics
│       │
│       ├── services/                   # Service wrappers
│       │   ├── __init__.py
│       │   ├── n8n_client.py           # n8n API client
│       │   └── firestore_client.py     # Firestore storage
│       │
│       └── tests/                      # Module tests
│           ├── __init__.py
│           ├── test_pipeline.py
│           └── test_processors.py
│
├── utils/
│   └── ella/                           # Existing Ella integration (keep)
│       ├── config.py
│       ├── scanner.py
│       ├── summary.py
│       └── memory.py
```

### Modularity Strategy

**Goal**: Keep Pipecat integration extractable to a separate package or submodule.

| Principle | Implementation |
|-----------|----------------|
| **Self-contained** | All Pipecat code in `integrations/pipecat/` |
| **No cross-imports** | Pipecat module doesn't import from `utils/` or `routers/` |
| **Dependency injection** | Pass configs/clients as parameters, not imports |
| **Own requirements** | `integrations/pipecat/requirements.txt` for pipecat deps |
| **Thin router** | `routers/voice_v2.py` just imports and delegates |

### Public API (integrations/pipecat/__init__.py)

```python
"""
Pipecat Voice Mode Integration for Ella AI.

This module is designed to be extractable to a separate package.
All Pipecat-specific code is self-contained here.

Usage:
    from integrations.pipecat import create_voice_pipeline, run_session

    pipeline = create_voice_pipeline(uid="user-123", config=config)
    await run_session(websocket, pipeline)
"""

from .pipeline.builder import create_voice_pipeline
from .pipeline.config import PipelineConfig
from .processors.ella_config import EllaConfigProcessor
from .processors.conversation_logger import ConversationLogger

__all__ = [
    "create_voice_pipeline",
    "PipelineConfig",
    "EllaConfigProcessor",
    "ConversationLogger",
]
```

### Thin Router Pattern

```python
# routers/voice_v2.py (minimal, delegates to integration)

from fastapi import WebSocket, APIRouter
from integrations.pipecat import create_voice_pipeline, run_session

router = APIRouter()

@router.websocket("/v2/voice")
async def voice_v2_endpoint(websocket: WebSocket, uid: str):
    """Pipecat voice mode endpoint. All logic in integrations/pipecat/."""
    await websocket.accept()

    # All Pipecat logic is in the integration module
    pipeline = await create_voice_pipeline(uid=uid)
    await run_session(websocket, pipeline)
```

### Future: Extract to Package

When ready to extract:

```bash
# 1. Copy to new repo
cp -r backend/integrations/pipecat/ ../pipecat-ella/

# 2. Add setup.py
# 3. Publish to PyPI or private registry

# 4. In OMI backend, replace folder with pip install
pip install pipecat-ella

# 5. Update imports
from pipecat_ella import create_voice_pipeline
```

### Upstream Merge Strategy

| Component | Location | Upstream Risk |
|-----------|----------|---------------|
| `integrations/pipecat/` | New folder | Zero - doesn't exist upstream |
| `routers/voice_v2.py` | New file | Zero - doesn't exist upstream |
| `utils/ella/` | New folder | Zero - doesn't exist upstream |
| `routers/transcribe.py` | Minimal changes | Low - only import + 5 lines |

**Merge commands**:
```bash
# Fetch upstream
git remote add upstream https://github.com/BasedHardware/omi.git
git fetch upstream

# Merge with our code protected
git merge upstream/main --strategy-option ours -m "Merge upstream, preserve Ella integrations"

# Resolve any conflicts in transcribe.py manually
```

### Endpoint Specification

```
WebSocket: wss://api.ella-ai-care.com/v2/voice

Query Parameters:
  - uid (required): Firebase user ID
  - session_id (optional): UUID, auto-generated if not provided

Authentication:
  - Same as /v4/listen (Firebase JWT or ADMIN_KEY in dev)
```

### Pipeline Architecture

```python
# backend/routers/voice_v2.py

from fastapi import WebSocket
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams
)
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.groq import GroqLLMService
from pipecat.services.openai import OpenAITTSService
from pipecat.vad.silero import SileroVADAnalyzer

@router.websocket("/v2/voice")
async def voice_v2_endpoint(websocket: WebSocket, uid: str):
    await websocket.accept()

    session_id = str(uuid.uuid4())

    # 1. Fetch config from n8n
    config = await fetch_voice_config(uid)

    # 2. Build system prompt with memory blocks
    system_prompt = build_system_prompt(config)

    # 3. Configure transport with VAD
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    stop_secs=1.5,      # 1.5s silence = end of turn
                    min_volume=0.5,
                )
            ),
        )
    )

    # 4. Configure services
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        model="nova-2",
    )

    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        model=config["agent_config"]["model"],
    )

    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        voice="nova",
    )

    # 5. Create custom processors
    config_processor = EllaConfigProcessor(config, system_prompt)
    conversation_logger = EllaConversationLogger(uid, session_id)

    # 6. Build pipeline
    pipeline = Pipeline([
        transport.input(),
        stt,
        config_processor,
        llm,
        conversation_logger,
        tts,
        transport.output(),
    ])

    # 7. Run pipeline
    task = PipelineTask(pipeline)
    runner = PipelineRunner()
    await runner.run(task)
```

### Custom Processors

```python
# backend/utils/voice/pipecat_hooks.py

from pipecat.frames.frames import (
    Frame, StartFrame, EndFrame,
    TranscriptionFrame, LLMMessagesFrame, TTSAudioRawFrame
)
from pipecat.processors.frame_processor import FrameProcessor

class EllaConfigProcessor(FrameProcessor):
    """Injects system prompt with memory blocks into LLM context."""

    def __init__(self, config: dict, system_prompt: str):
        super().__init__()
        self.config = config
        self.system_prompt = system_prompt
        self.messages = [{"role": "system", "content": system_prompt}]

    async def process_frame(self, frame: Frame, direction):
        if isinstance(frame, LLMMessagesFrame):
            # Prepend system message
            frame.messages = self.messages + frame.messages
        await self.push_frame(frame, direction)


class EllaConversationLogger(FrameProcessor):
    """
    Logs conversation turns and calls agents on session end.
    """

    def __init__(self, uid: str, session_id: str):
        super().__init__()
        self.uid = uid
        self.session_id = session_id
        self.turns = []
        self.start_time = None
        self.interruption_count = 0

    async def process_frame(self, frame: Frame, direction):
        if isinstance(frame, StartFrame):
            self.start_time = time.time()

        elif isinstance(frame, TranscriptionFrame):
            # User speech
            self.turns.append({
                "role": "user",
                "text": frame.text,
                "timestamp": time.time()
            })

        elif isinstance(frame, TTSAudioRawFrame):
            # Track assistant response (aggregate text separately)
            pass

        elif isinstance(frame, EndFrame):
            # Session ended
            await self._finalize_session()

        await self.push_frame(frame, direction)

    async def _finalize_session(self):
        """Store conversation and call n8n agents."""
        duration = time.time() - self.start_time

        # 1. Build transcript
        transcript = self._build_transcript()

        # 2. Store in Firestore
        await store_voice_conversation(
            uid=self.uid,
            session_id=self.session_id,
            transcript=transcript,
            segments=self.turns,
            source="voice_mode_v2",
            duration=duration,
        )

        # 3. Call n8n agents (async, don't block)
        asyncio.create_task(
            call_ella_agents(self.uid, self.session_id, transcript)
        )

        # 4. Store analytics
        await store_session_analytics(
            session_id=self.session_id,
            uid=self.uid,
            duration_seconds=duration,
            turn_count=len(self.turns),
            interruption_count=self.interruption_count,
        )
```

### n8n Integration

```python
# backend/utils/voice/pipecat_pipeline.py

import httpx

N8N_VOICE_CONFIG_URL = "https://n8n.ella-ai-care.com/webhook/voice-config"
N8N_MEMORY_AGENT_URL = "https://n8n.ella-ai-care.com/webhook/memory-agent"
N8N_SUMMARY_AGENT_URL = "https://n8n.ella-ai-care.com/webhook/summary-agent"

async def fetch_voice_config(uid: str) -> dict:
    """Fetch agent config and memory blocks from n8n."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            N8N_VOICE_CONFIG_URL,
            json={"uid": uid}
        )
        response.raise_for_status()
        return response.json()

def build_system_prompt(config: dict) -> str:
    """Build system prompt with persona and memory blocks."""
    blocks = config.get("blocks", {})

    return f"""
{config.get('persona', 'You are Ella, a helpful AI assistant.')}

## About the User
{blocks.get('user_profile', 'No profile available.')}

## Recent Memories
{blocks.get('rolling_memories', 'No recent memories.')}

## Recent Conversations
{blocks.get('rolling_summaries', 'No recent conversations.')}

## Instructions
- Be warm, concise, and helpful
- Reference user's memories naturally
- Keep responses short (1-3 sentences for voice)
- Ask follow-up questions to maintain conversation
""".strip()

async def call_ella_agents(uid: str, conversation_id: str, transcript: str):
    """Call memory and summary agents after session ends."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Memory agent
        await client.post(
            N8N_MEMORY_AGENT_URL,
            json={
                "uid": uid,
                "conversation_id": conversation_id,
                "transcript": transcript,
                "source": "voice_mode_v2"
            }
        )

        # Summary agent
        await client.post(
            N8N_SUMMARY_AGENT_URL,
            json={
                "uid": uid,
                "conversation_id": conversation_id,
                "transcript": transcript,
                "source": "voice_mode_v2"
            }
        )
```

### Session Analytics

```python
# backend/utils/voice/session_analytics.py

from database.conversations import conversations_db

async def store_session_analytics(
    session_id: str,
    uid: str,
    duration_seconds: float,
    turn_count: int,
    interruption_count: int,
    avg_response_latency_ms: float = None,
):
    """Store voice session analytics in Firestore."""
    analytics = {
        "session_id": session_id,
        "uid": uid,
        "duration_seconds": duration_seconds,
        "turn_count": turn_count,
        "interruption_count": interruption_count,
        "avg_response_latency_ms": avg_response_latency_ms,
        "created_at": datetime.utcnow(),
        "source": "voice_mode_v2",
    }

    # Store under user's voice_sessions subcollection
    db.collection("users").document(uid) \
        .collection("voice_sessions").document(session_id) \
        .set(analytics)
```

---

## Configuration

### Environment Variables

```bash
# Already configured
DEEPGRAM_API_KEY=xxx
GROQ_API_KEY=xxx
OPENAI_API_KEY=xxx

# n8n endpoints (already in use)
# Hardcoded in utils/voice/pipecat_pipeline.py
```

### VAD Configuration

```python
# Tunable parameters
VAD_STOP_SECS = 1.5        # Silence before end-of-turn
VAD_MIN_VOLUME = 0.5       # Minimum volume threshold
VAD_CONFIDENCE = 0.7       # VAD confidence threshold
```

---

## Testing

### Local Testing

```bash
# 1. Start backend
cd backend
source venv/bin/activate
python start_server.py

# 2. Test with websocat
websocat "ws://localhost:8000/v2/voice?uid=test123"

# 3. Send audio frames (PCM16, 16kHz)
# Or use test script
python scripts/test_voice_v2.py --uid test123
```

### Production Testing

```bash
# Health check
curl https://api.ella-ai-care.com/health

# WebSocket test (requires audio client)
# iOS app with feature flag enabled
```

---

## Deployment

### VPS Deployment

```bash
# SSH to VPS
ssh root@100.101.168.91

# Pull latest
cd /root/omi
git fetch origin feature/voice-mode-pipecat
git checkout feature/voice-mode-pipecat

# Install new deps
cd backend
source venv/bin/activate
pip install -r requirements.txt

# Restart service
systemctl restart omi-backend

# Check logs
journalctl -u omi-backend -f
```

---

## Rollback Plan

If Pipecat v2 has issues:
1. `/v4/listen` and v1 voice mode remain untouched
2. iOS can switch endpoints via feature flag
3. No data migration needed (same Firestore structure)

---

## Success Metrics

| Metric | v1 Current | v2 Target |
|--------|------------|-----------|
| End-of-speech detection | 1.5s text-change timer | <500ms VAD-based |
| Interruption support | None | Native |
| Time to first audio | ~850ms | ~600ms |
| Code complexity | High (custom) | Low (pipeline) |

---

## Timeline

| Task | Effort | Status |
|------|--------|--------|
| Install pipecat deps | 30min | Pending |
| Create /v2/voice router | 2h | Pending |
| Implement custom processors | 2h | Pending |
| Integrate n8n config hook | 1h | Pending |
| Add conversation storage | 1h | Pending |
| Add session analytics | 1h | Pending |
| Local testing | 2h | Pending |
| Deploy to VPS | 30min | Pending |
| **Total** | **~10h** | |

---

## References

- [Pipecat Docs](https://docs.pipecat.ai/)
- [FastAPI WebSocket Transport](https://docs.pipecat.ai/server/services/transport/fastapi-websocket)
- [Silero VAD](https://docs.pipecat.ai/server/services/vad/silero)
- [GitHub Discussion #4](https://github.com/ellaaicare/omi/discussions/4)
