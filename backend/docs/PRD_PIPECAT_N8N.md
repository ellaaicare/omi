# PRD: Pipecat v2 n8n Integration

**Version**: 1.0
**Date**: December 1, 2025
**Owner**: n8n/Ella Team
**Status**: Approved

---

## Overview

Support Pipecat v2 voice mode integration with existing n8n webhooks. Minimal changes required - existing endpoints already support the needed functionality.

**Goal**: Ensure n8n config and agent endpoints work seamlessly with Pipecat backend.

---

## Scope

### In Scope (Phase 1)
- Verify `/webhook/voice-config` returns correct format
- Verify `/webhook/memory-agent` accepts voice transcripts
- Verify `/webhook/summary-agent` accepts voice transcripts
- Document expected request/response formats

### In Scope (Phase 2 - Optional)
- New `/webhook/voice-providers` endpoint for per-user STT/TTS preferences
- `VoiceSession` analytics table

### Out of Scope
- Changes to Letta agent configuration
- New agent types

---

## Existing Endpoints (No Changes Needed)

### 1. Voice Config Endpoint

**URL**: `POST https://n8n.ella-ai-care.com/webhook/voice-config`

**Request**:
```json
{
  "uid": "firebase-user-id"
}
```

**Response** (already correct):
```json
{
  "agent_config": {
    "system_prompt": "You are Ella, a caring AI companion...",
    "model": "llama-3.3-70b-versatile",
    "provider": "groq",
    "temperature": 0.7,
    "max_tokens": 150
  },
  "blocks": {
    "user_profile": "Greg is a software developer who...",
    "rolling_memories": "- User mentioned they have a dog named Max\n- User prefers morning meetings...",
    "rolling_summaries": "Yesterday: Discussed project deadlines...\nTwo days ago: Talked about weekend plans..."
  },
  "persona": "You are Ella, a warm and caring AI companion...",
  "user": {
    "name": "Greg",
    "timezone": "America/Los_Angeles"
  }
}
```

**Backend Usage**:
- Called once at voice session start
- Used to build system prompt with memory context
- Cached for session duration (no refresh mid-session)

---

### 2. Memory Agent Endpoint

**URL**: `POST https://n8n.ella-ai-care.com/webhook/memory-agent`

**Request** (voice mode v2 format):
```json
{
  "uid": "firebase-user-id",
  "conversation_id": "uuid-session-id",
  "transcript": "User: Hello Ella, how are you?\nAssistant: Hi Greg! I'm doing well...",
  "segments": [
    {
      "role": "user",
      "text": "Hello Ella, how are you?",
      "timestamp": 1701388800.123
    },
    {
      "role": "assistant",
      "text": "Hi Greg! I'm doing well, thanks for asking! How can I help you today?",
      "timestamp": 1701388802.456
    }
  ],
  "source": "voice_mode_v2",
  "duration_seconds": 45.2
}
```

**Response**: Same as existing memory agent response.

**Notes**:
- `source: "voice_mode_v2"` distinguishes from `/v4/listen` transcripts
- `segments` includes role tags for proper attribution
- Called asynchronously after session ends

---

### 3. Summary Agent Endpoint

**URL**: `POST https://n8n.ella-ai-care.com/webhook/summary-agent`

**Request** (same format as memory agent):
```json
{
  "uid": "firebase-user-id",
  "conversation_id": "uuid-session-id",
  "transcript": "User: Hello Ella...\nAssistant: Hi Greg!...",
  "segments": [...],
  "source": "voice_mode_v2",
  "duration_seconds": 45.2
}
```

**Response**: Same as existing summary agent response.

---

## Phase 2: Voice Providers Endpoint (Optional)

If we want per-user voice preferences:

**URL**: `GET https://n8n.ella-ai-care.com/webhook/voice-providers?uid=xxx`

**Response**:
```json
{
  "stt": {
    "provider": "deepgram",
    "model": "nova-2",
    "language": "en-US"
  },
  "tts": {
    "provider": "openai",
    "voice": "nova",
    "speed": 1.0
  },
  "vad": {
    "provider": "silero",
    "stop_secs": 1.5,
    "min_volume": 0.5
  }
}
```

**Database Schema** (already exists):
```sql
-- User.settings JSONB field can store:
{
  "voice_preferences": {
    "tts_voice": "nova",
    "tts_speed": 1.0,
    "stt_language": "en-US"
  }
}
```

**Implementation**: Simple n8n workflow that queries `User.settings` and returns defaults if not set.

---

## Verification Checklist

### Before Backend Deployment

- [ ] Test `/webhook/voice-config` returns expected format
- [ ] Test `/webhook/memory-agent` accepts `source: "voice_mode_v2"`
- [ ] Test `/webhook/summary-agent` accepts `source: "voice_mode_v2"`
- [ ] Verify role tags (`user`/`assistant`) are handled correctly

### Test Commands

```bash
# Test voice config
curl -X POST https://n8n.ella-ai-care.com/webhook/voice-config \
  -H "Content-Type: application/json" \
  -d '{"uid": "test-user-id"}'

# Test memory agent with voice format
curl -X POST https://n8n.ella-ai-care.com/webhook/memory-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test-user-id",
    "conversation_id": "test-session-123",
    "transcript": "User: Hello\nAssistant: Hi there!",
    "segments": [
      {"role": "user", "text": "Hello", "timestamp": 1701388800},
      {"role": "assistant", "text": "Hi there!", "timestamp": 1701388802}
    ],
    "source": "voice_mode_v2",
    "duration_seconds": 5.0
  }'
```

---

## Timeline

| Task | Effort | Status |
|------|--------|--------|
| Verify voice-config endpoint | 30min | Pending |
| Verify memory-agent accepts v2 format | 30min | Pending |
| Verify summary-agent accepts v2 format | 30min | Pending |
| (Phase 2) Build voice-providers endpoint | 2h | Future |
| **Total Phase 1** | **~1.5h** | |

---

## Notes

### Model Deprecation
The current `agent_config.model` returns `llama-3.1-70b-versatile` which is deprecated. Consider updating to `llama-3.3-70b-versatile` in the Letta agent configuration.

### Caching
The `AgentConfig` table caching is already in place. No changes needed for voice mode - same caching strategy applies.

### Error Handling
If webhooks fail, backend falls back to local LLM. This existing fallback behavior remains unchanged.

---

## References

- [GitHub Discussion #4](https://github.com/ellaaicare/omi/discussions/4)
- [Backend PRD](./PRD_PIPECAT_BACKEND.md)
- [Existing Ella Integration Docs](./ELLA_INTEGRATION.md)
