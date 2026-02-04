# Ella Backend API Extensions

**Purpose**: This directory contains ALL Ella-specific backend code, kept modular for easy upstream merges.

**Last Updated**: January 11, 2026

---

## Overview

The Ella extensions add healthcare-focused features to the OMI backend:

1. **n8n/Letta Processing** - Routes summary/memory generation to Letta agents via n8n
2. **Push Notifications with Audio** - Firestore-based notifications with TTS playback
3. **Voice V2 (Grok V2V)** - Ultra-low latency voice-to-voice via Grok API

---

## Directory Structure

```
backend/
├── ella/                          # ALL Ella code (this directory)
│   ├── README.md                  # This file
│   ├── __init__.py                # Extension loader
│   ├── config.py                  # Feature flags, endpoints
│   │
│   ├── adapters/                  # Swap-in replacements for upstream functions
│   │   ├── __init__.py
│   │   ├── summary_adapter.py     # Routes to n8n instead of OpenAI
│   │   ├── memory_adapter.py      # Routes to n8n instead of OpenAI
│   │   └── notification_adapter.py # Adds audio to push notifications
│   │
│   ├── routers/                   # Ella-specific API endpoints
│   │   ├── __init__.py
│   │   ├── callbacks.py           # /api/ella/callback/* (n8n webhooks)
│   │   ├── voice_v2.py            # /v2/voice (Grok V2V)
│   │   └── testing.py             # /api/v1/testing/* (E2E tests)
│   │
│   ├── services/                  # Business logic
│   │   ├── __init__.py
│   │   ├── n8n_client.py          # HTTP client for n8n webhooks
│   │   └── grok_pipeline.py       # Grok V2V pipeline logic
│   │
│   └── docs/                      # Ella-specific documentation
│       ├── N8N_INTEGRATION.md
│       ├── VOICE_V2_PROTOCOL.md
│       └── UPSTREAM_HOOKS.md
│
├── main.py                        # Upstream + Ella import (5 lines added)
└── ...                            # Rest of upstream code
```

---

## How It Works

### 1. Extension Loading

The **only change** to upstream `main.py`:

```python
# main.py (upstream file)
app = FastAPI()
# ... upstream routers ...

# === ELLA EXTENSION POINT ===
try:
    from ella import register_ella_extensions
    register_ella_extensions(app)
except ImportError:
    pass  # Ella not installed, vanilla mode
# === END ELLA ===
```

### 2. Adapter Pattern

Ella adapters implement the same interface as upstream functions but route to different backends:

```python
# Example: Summary generation
# Upstream: calls OpenAI directly
# Ella: calls n8n → Letta agent → returns same format

from ella import get_adapter

# In upstream processing code:
ella_summary = get_adapter("summary")
if ella_summary:
    result = await ella_summary(uid, conversation, transcript)
    if result:
        return result  # Ella handled it
# Fall through to upstream OpenAI...
```

### 3. Feature Flags

All features can be toggled via environment variables:

```bash
# Master switch
ELLA_ENABLED=true

# Individual features
ELLA_SUMMARY_ENABLED=true      # Use n8n for summaries
ELLA_MEMORY_ENABLED=true       # Use n8n for memories
ELLA_NOTIFICATIONS_ENABLED=true # Audio push notifications
ELLA_VOICE_V2_ENABLED=true     # Grok V2V endpoint
ELLA_TESTING_ENABLED=false     # E2E testing endpoints (dev only)
```

---

## Key Features

### Feature 1: n8n/Letta Processing

**What**: Routes conversation processing to Letta agents via n8n webhooks instead of direct OpenAI calls.

**Why**: Centralized AI brain with memory, context, and healthcare-specific prompts.

**Endpoints Called**:
- `POST https://n8n.ella-ai-care.com/webhook/summary-agent`
- `POST https://n8n.ella-ai-care.com/webhook/memory-agent`
- `POST https://n8n.ella-ai-care.com/webhook/scanner-agent`

**Files**:
- `ella/adapters/summary_adapter.py`
- `ella/adapters/memory_adapter.py`
- `ella/services/n8n_client.py`

---

### Feature 2: Push Notifications with Audio

**What**: Stores notifications in Firestore with audio URL, iOS app plays audio on receive.

**Why**: Proactive healthcare alerts with voice messages.

**Firestore Structure**:
```json
{
  "uid": "user123",
  "message": "Time for your medication",
  "audio_url": "https://storage.../tts/abc123.mp3",
  "play_audio": true,
  "urgency": "MEDICATION",
  "timestamp": "2026-01-11T12:00:00Z"
}
```

**Files**:
- `ella/adapters/notification_adapter.py`
- `ella/routers/callbacks.py` (receives n8n notification triggers)

---

### Feature 3: Voice V2 (Grok V2V)

**What**: WebSocket endpoint for ultra-low latency voice conversations via Grok's realtime API.

**Why**: ~500ms response time vs 2-3s with traditional STT→LLM→TTS pipeline.

**Endpoint**: `WS /v2/voice?uid={uid}&pipeline_mode=grok_v2v`

**Architecture**:
```
iOS App ←→ Ella Backend ←→ Ella Grok Proxy ←→ Grok API
   16kHz PCM      24kHz PCM        24kHz PCM
```

**Files**:
- `ella/routers/voice_v2.py`
- `ella/services/grok_pipeline.py`

---

## Upstream Merge Process

### When Upstream Updates

```bash
# 1. Fetch upstream
git fetch upstream

# 2. Create merge branch
git checkout -b merge/upstream-$(date +%Y%m%d)

# 3. Merge (ella/ won't conflict - it's ours only)
git merge upstream/main

# 4. Check hook points still work
grep -r "from ella import" .
grep -r "get_adapter" utils/

# 5. Test
ELLA_ENABLED=true python -c "from ella import register_ella_extensions; print('OK')"
pytest tests/

# 6. Merge to main
git checkout main
git merge merge/upstream-$(date +%Y%m%d)
```

### Hook Points (Files We Modify in Upstream)

These are the ONLY upstream files with Ella hooks:

| File | Hook Type | Lines Added |
|------|-----------|-------------|
| `main.py` | Router registration | 5 |
| `utils/llm/conversation_processing.py` | Adapter call | 8 |
| `utils/llm/memories.py` | Adapter call | 8 |
| `utils/notifications.py` | Adapter call | 8 |

**Total upstream modifications**: ~30 lines

---

## Testing

### Unit Tests
```bash
pytest ella/tests/
```

### Integration Tests
```bash
# Requires n8n to be running
ELLA_ENABLED=true pytest ella/tests/integration/
```

### Verify Ella Loads
```bash
python -c "
from ella import register_ella_extensions, ELLA_ENABLED
print(f'Ella enabled: {ELLA_ENABLED}')
print('Adapters:', list(get_all_adapters().keys()))
"
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ELLA_ENABLED` | `true` | Master switch for all Ella features |
| `ELLA_N8N_BASE_URL` | `https://n8n.ella-ai-care.com` | n8n webhook base URL |
| `ELLA_SUMMARY_ENABLED` | `true` | Use n8n for summary generation |
| `ELLA_MEMORY_ENABLED` | `true` | Use n8n for memory extraction |
| `ELLA_SCANNER_ENABLED` | `true` | Send real-time transcripts to scanner |
| `ELLA_NOTIFICATIONS_ENABLED` | `true` | Audio push notifications |
| `ELLA_VOICE_V2_ENABLED` | `true` | Enable /v2/voice endpoint |
| `ELLA_TESTING_ENABLED` | `false` | E2E testing endpoints |
| `GROK_V2V_PROXY_URL` | `wss://voice.ella-ai-care.com/ws` | Grok proxy WebSocket URL |

---

## Rollback

To disable all Ella features and run vanilla OMI:

```bash
# Option 1: Environment variable
ELLA_ENABLED=false uvicorn main:app

# Option 2: Remove import from main.py
# Comment out: from ella import register_ella_extensions
```

---

## File Migration Map

When consolidating from old structure:

| Old Location | New Location |
|--------------|--------------|
| `routers/ella.py` | `ella/routers/callbacks.py` |
| `utils/ella/*` | `ella/adapters/*` + `ella/services/*` |
| `integrations/pipecat/pipeline/grok_v2v_pipeline.py` | `ella/services/grok_pipeline.py` |
| `integrations/pipecat/services/n8n_client.py` | `ella/services/n8n_client.py` |
| `routers/voice_v2.py` | `ella/routers/voice_v2.py` |

---

## Contact

- **Backend Developer**: Claude-Backend-Developer
- **Documentation**: This file + `ella/docs/`
- **Issues**: github.com/ellaaicare/omi/issues

---

*This module is designed for minimal upstream conflict and maximum maintainability.*
