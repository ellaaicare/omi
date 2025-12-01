# Pipecat Voice Mode Integration

Self-contained Pipecat integration for real-time voice conversations with Ella AI.

## Overview

This module provides server-side voice processing using the [Pipecat](https://github.com/pipecat-ai/pipecat) framework:

- **Silero VAD**: Automatic voice activity detection and end-of-speech
- **Deepgram STT**: Speech-to-text transcription
- **Groq LLM**: Fast language model inference
- **OpenAI TTS**: Natural text-to-speech

## Quick Start

```python
from integrations.pipecat import run_voice_session

@app.websocket("/v2/voice")
async def voice_endpoint(websocket: WebSocket, uid: str):
    await websocket.accept()
    await run_voice_session(websocket, uid)
```

## Architecture

```
integrations/pipecat/
├── __init__.py           # Public API exports
├── README.md             # This file
├── requirements.txt      # Pipecat-specific deps
│
├── pipeline/             # Core pipeline
│   ├── builder.py        # Pipeline factory
│   └── config.py         # Configuration classes
│
├── processors/           # Custom Pipecat processors
│   ├── ella_config.py    # n8n config injection
│   └── conversation_logger.py  # Turn logging
│
├── services/             # External service clients
│   ├── n8n_client.py     # n8n webhook client
│   └── firestore_client.py  # Firestore storage
│
└── tests/                # Module tests
```

## Configuration

All configuration is in `pipeline/config.py`:

```python
from integrations.pipecat import PipelineConfig, VADConfig

# Custom configuration
config = PipelineConfig(
    vad=VADConfig(stop_secs=2.0),  # Longer silence threshold
)

await run_voice_session(websocket, uid, config=config)
```

## Environment Variables

```bash
DEEPGRAM_API_KEY=xxx   # Required for STT
OPENAI_API_KEY=xxx     # Required for TTS
GROQ_API_KEY=xxx       # Required for LLM
```

## n8n Integration

The module integrates with n8n webhooks:

- `/webhook/voice-config` - Fetch persona and memory blocks
- `/webhook/memory-agent` - Extract memories from conversation
- `/webhook/summary-agent` - Generate conversation summary

## Modularity

This module is designed to be extractable:

1. **No cross-imports**: Doesn't import from `utils/` or other `routers/`
2. **Dependency injection**: Clients passed as parameters
3. **Own requirements**: `requirements.txt` for pipecat deps
4. **Self-contained**: Can be copied to a separate package

## License

Pipecat is licensed under BSD 2-Clause License.
See `THIRD_PARTY_LICENSES.md` for details.

## References

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [PRD](../docs/PRD_PIPECAT_BACKEND.md)
- [GitHub Discussion #4](https://github.com/ellaaicare/omi/discussions/4)
