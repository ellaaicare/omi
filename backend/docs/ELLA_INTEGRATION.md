# Ella Integration Architecture

**Date**: November 6, 2025
**Status**: ✅ Implementation Complete
**Branch**: `feature/ios-backend-integration`

## Overview

The Ella integration replaces hard-coded LLM calls in the OMI backend with calls to Ella's Letta-powered agents. This creates a centralized AI brain while preserving all existing OMI infrastructure.

**Core Principle**: Backend acts as a thin API wrapper between Ella's agents and OMI's infrastructure (TTS, STT, push notifications, Firestore storage).

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ iOS App (No Changes)                                             │
│ - Polls GET /v1/conversations                                    │
│ - Polls GET /v3/memories                                         │
│ - Receives push notifications with TTS audio                     │
└─────────────────────────────────────────────────────────────────┘
                              ↑
                              │ (Firestore + Push Notifications)
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ OMI Backend (Thin Wrapper)                                      │
│                                                                  │
│ OUTBOUND → Ella:                                                │
│ 1. Realtime chunks → Ella scanner (every 600ms)                │
│ 2. Full transcript → Ella summary agent (on conversation end)  │
│ 3. Transcript segments → Ella memory agent (on conversation end)│
│                                                                  │
│ INBOUND ← Ella:                                                 │
│ 1. POST /v1/ella/memory - Store memories in Firestore          │
│ 2. POST /v1/ella/conversation - Store summaries in Firestore   │
│ 3. POST /v1/ella/notification - Generate TTS + push to iOS     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                              │ (HTTP Webhooks)
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Ella's n8n Workflows (AI Brain)                                 │
│                                                                  │
│ 1. Scanner Agent (realtime)                                     │
│    - Groq LLM (~200ms)                                          │
│    - Detects: EMERGENCY, QUESTION, WAKE_WORD, etc.             │
│    - If alert → Main Agent → Callback to backend               │
│                                                                  │
│ 2. Summary Agent (conversation end)                             │
│    - Letta config → Generate structured summary                 │
│    - Returns: title, overview, emoji, category, action_items   │
│                                                                  │
│ 3. Memory Agent (conversation end)                              │
│    - Letta config → Extract facts from transcript               │
│    - Returns: memories with category (interesting/system)       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                              │ (Postgres Cache)
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Letta Server (Agent Config Store)                               │
│ - All agent configs stored here                                 │
│ - Scanner rules, memory extraction logic, summary templates     │
│ - NO direct inference (Ella uses Groq/OpenAI/Anthropic)        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Details

### Phase 1: Callback Endpoints

**File**: `routers/ella.py` (NEW)

Three new endpoints for Ella to send results back:

#### 1. POST /v1/ella/memory
- **Purpose**: Store memories extracted by Ella's memory agent
- **Request**:
```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "memories": [
    {
      "content": "User takes blood pressure medication daily at 8am",
      "category": "system",
      "visibility": "private",
      "tags": ["medication", "health"]
    }
  ]
}
```
- **Backend Action**:
  - Converts to `MemoryDB` objects
  - Stores in Firestore `memories` collection
  - iOS app polls `GET /v3/memories` to fetch

#### 2. POST /v1/ella/conversation
- **Purpose**: Store conversation summary generated by Ella's summary agent
- **Request**:
```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "structured": {
    "title": "Morning Health Check-In",
    "overview": "User discussed morning routine...",
    "emoji": "💊",
    "category": "health",
    "action_items": [{"description": "Schedule appointment", "due_at": "2025-11-15T10:00:00Z"}],
    "events": []
  }
}
```
- **Backend Action**:
  - Converts to `Structured` object
  - Updates Firestore `conversations` collection
  - iOS app polls `GET /v1/conversations` to fetch

#### 3. POST /v1/ella/notification
- **Purpose**: Send urgent notifications detected by Ella's scanner
- **Request**:
```json
{
  "uid": "user-123",
  "message": "I noticed you mentioned chest pain. Are you okay?",
  "urgency": "EMERGENCY",
  "generate_audio": true
}
```
- **Backend Action**:
  - Generates TTS audio (OpenAI)
  - Sends push notification (FCM/APNS)
  - iOS receives and plays audio immediately

---

### Phase 2: Replace LLM Calls

#### 1. Summary Generation

**File**: `utils/llm/conversation_processing.py:347-430`

**Function**: `get_transcript_structure()`

**Changes**:
- Added `uid` parameter
- Calls `https://n8n.ella-ai-care.com/webhook/summary-agent`
- Converts Ella's JSON response to `Structured` object
- Falls back to local LLM if Ella unavailable

**Request to Ella**:
```json
{
  "uid": "user-123",
  "transcript": "Full conversation text...",
  "started_at": "2025-11-06T12:00:00",
  "language_code": "en",
  "timezone": "America/New_York"
}
```

**Fallback**: If Ella returns non-200 status or times out (30s), uses original hard-coded GPT-4o-mini logic.

#### 2. Memory Extraction

**File**: `utils/llm/memories.py:45-82`

**Function**: `new_memories_extractor()`

**Changes**:
- Calls `https://n8n.ella-ai-care.com/webhook/memory-agent`
- Converts segments to simple dict format
- Converts Ella's JSON response to `Memory` objects
- Falls back to local LLM if Ella unavailable

**Request to Ella**:
```json
{
  "uid": "user-123",
  "segments": [
    {"text": "I take blood pressure meds", "speaker": "SPEAKER_00"},
    {"text": "Every morning at 8am", "speaker": "SPEAKER_00"}
  ]
}
```

**Fallback**: If Ella fails, uses original hard-coded gpt-4o-mini logic.

#### 3. Realtime Scanner

**File**: `routers/transcribe.py:923-942`

**Changes**:
- Added POST to Ella scanner after existing app webhook calls
- Fire-and-forget pattern (1s timeout, silent failure)
- Only sends if text is not empty

**Request to Ella**:
```json
{
  "uid": "user-123",
  "text": "I've been having chest pain",
  "timestamp": "2025-11-06T12:30:00Z"
}
```

**Resilience**: Silently fails if Ella is down (doesn't break transcription).

---

## Data Flow

### Realtime Scanning Flow

```
1. iOS device speaks → Audio
2. WebSocket → Deepgram STT → Transcript chunks (every 600ms)
3. Backend sends to existing app webhooks (unchanged)
4. Backend sends to Ella scanner (new)
   POST https://n8n.ella-ai-care.com/webhook/omi-scanner
5. Ella: Scanner agent detects urgency (EMERGENCY, QUESTION, etc.)
6. If alert: Ella calls Main Agent → Generates caring response
7. Ella POSTs to /v1/ella/notification
8. Backend generates TTS + sends push notification
9. iOS receives and plays audio
```

**Latency**: ~300ms (scanner) + ~1s (Main Agent) + ~500ms (TTS) = **~1.8s total**

### Conversation Summary Flow

```
1. Conversation ends (silence detected, manual stop, or 2min timeout)
2. Backend calls process_conversation()
3. Backend POSTs full transcript to Ella summary agent
   POST https://n8n.ella-ai-care.com/webhook/summary-agent
4. Ella: Summary agent generates structured summary
5. Ella returns summary (synchronous, 30s timeout)
6. Backend stores in Firestore
7. iOS app polls GET /v1/conversations → sees new summary
```

**Latency**: ~2-5s (Ella processing)

### Memory Extraction Flow

```
1. After summary generation, backend calls _extract_memories()
2. Backend POSTs segments to Ella memory agent
   POST https://n8n.ella-ai-care.com/webhook/memory-agent
3. Ella: Memory agent extracts facts
4. Ella returns memories (synchronous, 30s timeout)
5. Backend stores in Firestore
6. iOS app polls GET /v3/memories → sees new memories
```

**Latency**: ~2-5s (Ella processing)

---

## Fallback Strategy

All 3 integrations have **safe degradation**:

1. **If Ella returns non-200 status**: Falls back to local LLM
2. **If Ella times out (30s)**: Falls back to local LLM
3. **If Ella connection fails**: Falls back to local LLM
4. **Scanner failure**: Silently fails (doesn't break transcription)

**Result**: Zero downtime even if Ella's n8n is completely offline.

---

## What Doesn't Change

✅ WebSocket `/v4/listen` (Deepgram STT)
✅ TTS generation (OpenAI + Firebase Storage + Redis caching)
✅ Push notifications (FCM + APNS)
✅ Firestore storage structure
✅ iOS app polling logic
✅ All existing API endpoints
✅ App webhook infrastructure

**Preservation**: 95% of existing code unchanged.

---

## Testing

### Test Health Check

```bash
curl https://api.ella-ai-care.com/v1/ella/health
```

Expected:
```json
{
  "status": "healthy",
  "service": "ella-integration",
  "version": "1.0.0",
  "endpoints": [
    "POST /v1/ella/memory",
    "POST /v1/ella/conversation",
    "POST /v1/ella/notification"
  ]
}
```

### Test Memory Callback

```bash
curl -X POST https://api.ella-ai-care.com/v1/ella/memory \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "123",
    "conversation_id": "test-conv",
    "memories": [
      {
        "content": "Test memory",
        "category": "interesting",
        "visibility": "private",
        "tags": ["test"]
      }
    ]
  }'
```

### Test Notification Callback

```bash
curl -X POST https://api.ella-ai-care.com/v1/ella/notification \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "123",
    "message": "Test notification from Ella",
    "urgency": "QUESTION",
    "generate_audio": true
  }'
```

---

## Monitoring

### Backend Logs

Look for these log messages:

```
📤 Calling Ella summary agent for uid=123
✅ Ella summary agent returned: Morning Health Check-In
⚠️  Ella summary agent returned status 500, falling back to local LLM
🔄 Using local LLM for summary generation

📤 Calling Ella memory agent for uid=123
✅ Ella memory agent returned 3 memories

💾 Ella Memory Callback - UID: 123, Count: 3
📝 Ella Conversation Callback - UID: 123, ID: conv-456
🚨 Ella Notification Callback - UID: 123
```

### Ella's n8n Logs

Ella Dev should monitor:
- `/webhook/omi-scanner` - Realtime chunks received
- `/webhook/summary-agent` - Summaries generated
- `/webhook/memory-agent` - Memories extracted
- Callback success/failure to backend endpoints

---

## Future Enhancements

1. **Metrics Dashboard**: Track Ella success rate, fallback rate, latency
2. **Retry Logic**: Automatic retry with exponential backoff
3. **Circuit Breaker**: Temporarily disable Ella if failure rate > 50%
4. **A/B Testing**: Compare Ella vs local LLM quality
5. **Cost Analysis**: Track Ella API costs vs local LLM costs

---

## Deployment Checklist

### Backend (Production VPS)

- [ ] Pull latest code: `git pull origin feature/ios-backend-integration`
- [ ] Restart backend: `systemctl restart omi-backend`
- [ ] Check logs: `journalctl -u omi-backend -f`
- [ ] Test health: `curl https://api.ella-ai-care.com/v1/ella/health`

### Ella's n8n (Production)

- [ ] Deploy updated workflows with exact payload formats
- [ ] Test `/webhook/omi-scanner` with sample text
- [ ] Test `/webhook/summary-agent` with sample transcript
- [ ] Test `/webhook/memory-agent` with sample segments
- [ ] Verify callbacks to backend work

### iOS App (No Changes Needed)

- [ ] Verify app still polls `/v1/conversations` and sees summaries
- [ ] Verify app still polls `/v3/memories` and sees memories
- [ ] Verify app receives push notifications with audio
- [ ] Test end-to-end: speak → see summary → see memories → receive alert

---

## Rollback Plan

If Ella integration causes issues:

1. **Disable Ella calls temporarily**: Set env var `ELLA_ENABLED=false`
2. **OR** Comment out Ella API calls (they're clearly marked with `# ====== ELLA INTEGRATION ======`)
3. **Automatic fallback**: Local LLM will take over immediately
4. **Zero downtime**: iOS app sees no difference

---

## Team Contacts

- **Backend Dev**: (OMI Backend Dev on Discord)
- **Ella Dev**: (Ella on Discord) - Manages n8n workflows, Postgres, Letta cluster
- **Letta Dev**: (Letta Dev on Discord) - Manages Letta agent configs
- **iOS Dev**: (iOS Dev on Discord) - iOS app changes (if needed)
- **PM**: (@PM on Discord) - Project coordination

---

## Related Documentation

- Architecture proposal: Discord #general (Nov 6, 2025)
- Payload formats: Discord #general (Nov 6, 2025)
- Implementation plan: Discord #general (Nov 6, 2025)
- Original discussion: `docs/LETTA_INTEGRATION_ARCHITECTURE.md`

---

**Implementation completed**: November 6, 2025
**Estimated dev time**: ~7 hours
**Actual dev time**: ~7 hours ✅
**Risk level**: LOW (95% code preservation, safe fallbacks)
