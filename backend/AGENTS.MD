# OMI Backend - AI Agent Quick Reference

**Last Updated**: January 10, 2026
**Purpose**: Essential information for AI agents working on OMI/Ella backend

---

## System Architecture (30-Second Overview)

```
OMI Device (Bluetooth mic necklace)
    ↓ Opus audio via BLE
iOS App
    ↓ WebSocket
OMI Backend (FastAPI) ← YOU ARE HERE
    ├─ Ambient: wss://api.ella-ai-care.com/v4/listen → Deepgram STT → Firestore
    └─ Voice: wss://api.ella-ai-care.com/v2/voice → Pipecat (VAD→STT→LLM→TTS)
            ↓ webhooks
n8n Workflow Engine (https://n8n.ella-ai-care.com)
    ↓
Letta Agent Cluster (memory extraction, summaries, scanner)
    ↓ callbacks
Backend → Firestore + Pinecone
```

---

## Key Endpoints

| Endpoint | Protocol | Purpose |
|----------|----------|---------|
| `/v4/listen` | WebSocket | Ambient audio transcription |
| `/v2/voice` | WebSocket | 2-way voice conversations |
| `/v1/ella/*` | REST | Letta callbacks + tool endpoints |
| `/v1/conversations` | REST | iOS app conversation queries |
| `/v3/memories` | REST | iOS app memory queries |

---

## n8n Webhooks (Called by Backend)

| Webhook | When | Purpose |
|---------|------|---------|
| `/webhook/voice-init` | Voice start | Get persona, memory blocks, recent messages |
| `/webhook/scanner-agent` | Every 600ms | Real-time urgency detection |
| `/webhook/memory-agent` | Conversation end | Extract memories from transcript |
| `/webhook/summary-agent` | Conversation end | Generate structured summary |
| `/webhook/call-state` | Voice start/end | Track active calls |

---

## Critical Files

| File | Lines | Purpose |
|------|-------|---------|
| `routers/transcribe.py` | ~1300 | Ambient listening WebSocket |
| `routers/voice_v2.py` | ~120 | Voice mode entry point |
| `integrations/pipecat/pipeline/manual_pipeline.py` | ~800 | Voice pipeline (VAD→STT→LLM→TTS) |
| `integrations/pipecat/services/n8n_client.py` | ~270 | n8n webhook client |
| `routers/ella.py` | ~1000 | Ella callback endpoints |
| `utils/conversations/process_conversation.py` | ~500 | Conversation finalization |

---

## Voice Pipeline Config (from n8n `/webhook/voice-init`)

```json
{
  "agent_config": {
    "model": "llama-3.3-70b-versatile",
    "provider": "groq",
    "temperature": 0.7,
    "system_prompt": "You are Ella..."  // Letta owns the prompt
  },
  "blocks": {
    "user_profile": "...",
    "rolling_memories": "...",
    "rolling_summaries": "..."
  },
  "recent_messages": [{ "role": "user", "content": "..." }]
}
```

---

## Current Work (January 2026)

### Recently Completed
- Voice-init endpoint integration (Letta owns system_prompt)
- `message_limit` parameter for n8n config requests
- `recent_messages` support for conversation continuity
- Call state notifications (start/end)
- Edge ASR on-device transcription support

### Uncommitted Changes
- `config.py`: endpoint `/webhook/voice-config` → `/webhook/voice-init`
- `n8n_client.py`: added `message_limit` parameter
- `manual_pipeline.py`: uses provider/system_prompt from config, loads recent_messages

### Active Proposals
- **Issue #29**: Unified Memory Layer (Mem0 Integration) - RFC
- **Issue #30**: Grok Voice-to-Voice for 70% latency improvement

---

## Infrastructure

| Component | Location/URL |
|-----------|--------------|
| VPS | 100.101.168.91 (Tailscale) |
| API | https://api.ella-ai-care.com |
| n8n | https://n8n.ella-ai-care.com |
| Service | `systemctl status omi-backend` |
| Logs | `journalctl -u omi-backend -f` |

---

## Provider Stack

| Function | Primary | Fallback |
|----------|---------|----------|
| STT | Deepgram Nova-2 | Soniox |
| LLM | Groq (llama-3.3-70b) | xAI (grok-2-1212) |
| TTS | ElevenLabs | OpenAI |
| VAD | Silero | - |
| Vector DB | Pinecone | - |
| Primary DB | Firestore | - |

---

## Known Challenges

1. **Dual Memory Silo**: OMI (Firestore/Pinecone) vs Letta (Postgres/pgvector) - no sync
2. **Groq Rate Limits**: 30 req/min → falls back to xAI
3. **Upstream Sync**: 393 commits behind BasedHardware/omi
4. **Firebase Rules**: Still open for development

---

## Quick Commands

```bash
# Check API health
curl https://api.ella-ai-care.com/v2/voice/health

# View live logs
ssh root@100.101.168.91 "journalctl -u omi-backend -f"

# Restart backend
ssh root@100.101.168.91 "systemctl restart omi-backend"

# Test voice-init config
curl -X POST https://n8n.ella-ai-care.com/webhook/voice-init \
  -H "Content-Type: application/json" \
  -d '{"uid": "test-user", "message_limit": 10}'
```

---

## Git Protocol

**CRITICAL**: This is a downstream fork. Always use `--repo ellaaicare/omi`:
```bash
gh issue create --repo ellaaicare/omi --title "..."
gh pr create --repo ellaaicare/omi --title "..."
```

---

## Documentation

- **Full Architecture**: `docs/BACKEND_ARCHITECTURE_OVERVIEW.md` (1700+ lines)
- **Ella Integration**: `docs/ELLA_INTEGRATION.md`
- **Voice Mode PRD**: `docs/PRD_VOICE_MODE_V2_PIPECAT.md`
- **Security**: `docs/SECURITY_HIPAA_CHECKLIST.md`

---

*For comprehensive details, see `CLAUDE.md` and `docs/BACKEND_ARCHITECTURE_OVERVIEW.md`*
