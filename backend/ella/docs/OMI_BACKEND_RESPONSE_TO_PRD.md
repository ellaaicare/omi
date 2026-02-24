# OMI Backend Response to Ella-Letta PRD

**Date**: January 18, 2026
**From**: OMI Backend Team
**Re**: ELLA_LETTA_INTEGRATION_PRD.md and related docs
**Status**: UPDATED with clarifications

---

## Summary

Reviewed the Ella team's PRD. Key takeaway: **Use existing OMI endpoints, not new `/v1/ella/*` endpoints.** Most infrastructure already exists.

---

## 1. Endpoint Consolidation - USE DEFAULT OMI ENDPOINTS

### ✅ Decision: No new `/v1/ella/*` endpoints

The old system used custom `/ella` endpoints, but we're consolidating to standard OMI endpoints:

| PRD Request | Use Instead | Status |
|-------------|-------------|--------|
| `POST /v1/ella/memory` | `POST /v3/memories` | ✅ Exists |
| `POST /v1/ella/voice-session` | `POST /v1/conversations` | ✅ Exists |
| `GET /v1/users/{uid}/context` | Letta memory blocks | Per PRD design |

### Memory Endpoints (Standard)

```
POST /v3/memories                    - Create memory
GET  /v3/memories                    - List memories
GET  /v3/memories/{id}               - Get single
PATCH /v3/memories/{id}              - Update
DELETE /v3/memories/{id}             - Delete
```

### Conversation Endpoints (Standard)

```
POST /v1/conversations               - Create/finalize
GET  /v1/conversations               - List
GET  /v1/conversations/{id}          - Get single
POST /v1/conversations/{id}/reprocess - Reprocess
```

---

## 2. Authentication - How It Works

### Standard OMI Auth Pattern

Location: `utils/other/endpoints.py:16`

```python
def get_current_user_uid(authorization: str = Header(None)):
    # Option 1: Admin Key (service-to-service)
    if authorization and os.getenv('ADMIN_KEY') in authorization:
        return authorization.split(os.getenv('ADMIN_KEY'))[1]

    # Option 2: Firebase ID Token (user auth)
    token = authorization.split(' ')[1]
    decoded_token = auth.verify_id_token(token)
    return decoded_token['uid']
```

### For n8n → Backend Calls

**Use Admin Key pattern:**
```http
Authorization: <ADMIN_KEY><firebase_uid>
```

Example:
```http
POST /v3/memories
Authorization: sk_ella_abc123xyz5aGC5YE9BnhcSoTxxtT4ar6ILQy2
Content-Type: application/json

{"content": "User prefers morning calls", "category": "preference"}
```

The `ADMIN_KEY` is set in `.env` on VPS. n8n should have this key configured.

### For iOS → Backend Calls

**Use Firebase ID Token:**
```http
Authorization: Bearer <firebase_id_token>
```

---

## 3. Voice Session Endpoints

### Status: OURS (Ella Fork)

The voice session endpoints are from our fork, not upstream OMI:

Location: `ella/routers/voice.py`

```
POST /v1/voice/session/start     - Start voice session (issues JWT)
POST /v1/voice/session/end       - End session
GET  /v1/voice/session/{id}      - Get session info
```

### Auth for Voice

Uses `ELLA_SESSION_SECRET` for JWT signing:
```python
jwt.encode(payload, ELLA_SESSION_SECRET, algorithm="HS256")
```

### Voice System Note

- **New system**: Grok Voice-to-Voice (preferred)
- **Fallback**: Pipecat (exists, can keep as fallback)

---

## 4. Storage - Single Source of Truth

### ✅ Decision: OMI = Storage, Letta = Agent Summaries

| Data Type | Primary Store | Notes |
|-----------|---------------|-------|
| Memories | OMI `/v3/memories` | Single source of truth |
| Conversations | OMI `/v1/conversations` | Full transcripts + structured |
| User Profile | OMI Firestore | Basic user data |
| Agent Context | Letta memory blocks | For LLM context only |
| Agent Summaries | Letta archival | Agent's perspective |

**Key Principle**: OMI stores the data. Letta stores agent interpretations/summaries.

---

## 5. Two-Way Calling

### Outbound (iOS → Voice)
- ✅ Works via current voice session flow
- iOS calls `/v1/voice/session/start` → connects to Grok

### Inbound (Backend → iOS)
- Uses TTS push notification (just fixed today!)
- `send_tts_audio_notification()` in `utils/notifications.py`
- Requires iOS to be registered for push notifications

**Note**: Consult iOS team on inbound call UX.

---

## 6. What Already Works (No Changes Needed)

| Component | Location | Status |
|-----------|----------|--------|
| Memory CRUD | `routers/memories.py` | ✅ Ready |
| Conversation CRUD | `routers/conversations.py` | ✅ Ready |
| LLM Context Injection | `utils/llm/clients.py` | ✅ Ready |
| TTS Push Notifications | `utils/notifications.py` | ✅ Fixed today |
| Voice Session JWT | `ella/routers/voice.py` | ✅ Ready |

---

## 7. Minimal Backend Work Needed

| Task | Effort | Notes |
|------|--------|-------|
| Document ADMIN_KEY for n8n | 0.5 day | Add to n8n config |
| Verify voice endpoints work with new flow | 1 day | May need minor updates |
| Remove deprecated `/v1/ella/*` routes | 0.5 day | Cleanup |

### NOT Needed

- ❌ New `/v1/ella/memory` endpoint (use `/v3/memories`)
- ❌ New `/v1/ella/voice-session` endpoint (use `/v1/conversations`)
- ❌ Batch memory creation (single memory API is sufficient)
- ❌ New user context endpoint (Letta blocks are source)

---

## 8. API Contract Summary for n8n Team

### Create Memory

```http
POST /v3/memories
Authorization: <ADMIN_KEY><uid>
Content-Type: application/json

{
  "content": "User prefers morning calls",
  "category": "preference",
  "visibility": "private"
}
```

### Get Memories

```http
GET /v3/memories?limit=50
Authorization: <ADMIN_KEY><uid>
```

### Create Conversation

```http
POST /v1/conversations
Authorization: <ADMIN_KEY><uid>
Content-Type: application/json

{
  "started_at": "2026-01-18T10:00:00Z",
  "finished_at": "2026-01-18T10:30:00Z",
  "transcript_segments": [...],
  "source": "voice"
}
```

---

## 9. Environment Variables Reference

```bash
# Backend auth
ADMIN_KEY=sk_ella_xxx          # For service-to-service auth

# Voice session
ELLA_SESSION_SECRET=xxx        # JWT signing for voice sessions

# Push notifications
IOS_BUNDLE_ID=com.greg.friendapp  # Must match Firebase Console
```

---

## 10. Next Steps

1. **n8n Team**: Configure `ADMIN_KEY` in n8n workflows
2. **n8n Team**: Update endpoints to use `/v3/memories`, `/v1/conversations`
3. **Backend**: Remove deprecated `/v1/ella/*` routes after migration
4. **iOS Team**: Confirm inbound call flow requirements

---

## Files Reference

| File | Purpose |
|------|---------|
| `routers/memories.py` | Memory CRUD (standard) |
| `routers/conversations.py` | Conversation CRUD (standard) |
| `ella/routers/voice.py` | Voice session endpoints (ours) |
| `utils/other/endpoints.py` | Auth middleware (ADMIN_KEY + Firebase) |
| `utils/notifications.py` | Push notifications (TTS) |
| `utils/llm/clients.py` | LLM context injection |

---

*OMI Backend Team*
*Updated: January 18, 2026*
