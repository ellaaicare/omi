# Ella Callback API Endpoints - Complete Reference

**Date:** November 16, 2025
**Base URL:** `https://api.ella-ai-care.com`
**Purpose:** API endpoints for Ella/Letta agents to push processed data back to OMI backend

---

## Overview

These endpoints allow Ella's n8n/Letta agents to send processed results (memories, summaries, notifications) back to OMI infrastructure. The backend acts as a thin storage/routing layer between Ella and iOS devices.

**Architecture Flow:**
```
OMI Device → Backend WebSocket → Transcript → n8n Scanner Webhook → Letta Agents
                                                                          ↓
                                                                    Process Data
                                                                          ↓
iOS App ← Firestore ← Backend Callback Endpoints ← n8n Callback ← Letta Response
```

---

## Authentication

**All Ella callback endpoints are currently OPEN (no auth required)** for ease of integration.

🔒 **Production TODO:** Add shared secret authentication via header:
```
X-Ella-Secret: <shared_secret_key>
```

---

## 1. Memory Callback Endpoint

**Endpoint:** `POST /v1/ella/memory`
**Purpose:** Ella's memory agent sends extracted facts/memories here
**Flow:** Backend stores in Firestore → iOS app polls `GET /v3/memories`

### Request Body

```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "memories": [
    {
      "content": "User takes blood pressure medication daily at 8am",
      "category": "system",
      "visibility": "private",
      "tags": ["medication", "health", "routine"]
    },
    {
      "content": "User's daughter Sarah lives in Boston",
      "category": "interesting",
      "visibility": "private",
      "tags": ["family", "sarah", "boston"]
    }
  ]
}
```

### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uid` | string | ✅ | Firebase user ID |
| `conversation_id` | string | ❌ | Associated conversation (optional) |
| `memories` | array | ✅ | List of memory objects |
| `memories[].content` | string | ✅ | The memory/fact text |
| `memories[].category` | string | ✅ | `"interesting"` or `"system"` |
| `memories[].visibility` | string | ❌ | Default: `"private"` |
| `memories[].tags` | array | ❌ | Tags for search (optional) |

### Response

```json
{
  "status": "success",
  "count": 2,
  "message": "Stored 2 memories for user user-123"
}
```

### Example curl

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/memory" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "conversation_id": "conv-456",
    "memories": [
      {
        "content": "User prefers morning appointments",
        "category": "system",
        "tags": ["preferences", "appointments"]
      }
    ]
  }'
```

### Notes
- **Category Rules:**
  - `"system"` - Important facts: medications, allergies, preferences, schedules
  - `"interesting"` - General life events: activities, social interactions, hobbies
- **Tags:** Used for iOS app search functionality
- Backend automatically adds `id`, `created_at`, `updated_at` timestamps
- Duplicate detection: Ella should dedupe before sending (backend doesn't check)

---

## 2. Conversation Summary Callback Endpoint

**Endpoint:** `POST /v1/ella/conversation`
**Purpose:** Ella's summary agent sends structured conversation summary here
**Flow:** Backend updates Firestore conversation → iOS app polls `GET /v1/conversations`

### Request Body

```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "structured": {
    "title": "Morning Health Check-In",
    "overview": "User discussed morning routine, took medication on time, and mentioned feeling energetic today. Plans to go for a walk later.",
    "emoji": "💊",
    "category": "health",
    "action_items": [
      {
        "description": "Schedule follow-up appointment with Dr. Smith",
        "due_at": "2025-11-20T10:00:00Z"
      }
    ],
    "events": [
      {
        "title": "Walk in the park",
        "description": "User mentioned planning a walk",
        "start": "2025-11-16T15:00:00Z",
        "duration": 30
      }
    ]
  }
}
```

### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uid` | string | ✅ | Firebase user ID |
| `conversation_id` | string | ✅ | Conversation ID to update |
| `structured.title` | string | ✅ | 2-5 word conversation title |
| `structured.overview` | string | ✅ | 2-3 sentence summary |
| `structured.emoji` | string | ✅ | Single emoji (e.g., "💊") |
| `structured.category` | string | ✅ | Category (see below) |
| `structured.action_items` | array | ❌ | Optional action items |
| `structured.events` | array | ❌ | Optional calendar events |

### Category Options

```python
"health"      # Health, medical, medication
"social"      # Social interactions, conversations
"work"        # Work-related activities
"personal"    # Personal matters, hobbies
"learning"    # Education, learning, reading
"finance"     # Financial discussions
"travel"      # Travel plans, trips
"other"       # Default fallback
```

### Action Item Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | ✅ | Task description |
| `due_at` | string | ❌ | ISO 8601 due date (optional) |

### Event Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | ✅ | Event title |
| `description` | string | ❌ | Event description (optional) |
| `start` | string | ✅ | ISO 8601 start time |
| `duration` | integer | ❌ | Duration in minutes (default: 60) |

### Response

```json
{
  "status": "success",
  "conversation_id": "conv-456",
  "message": "Updated conversation summary for user-123"
}
```

### Example curl

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/conversation" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "conversation_id": "conv-456",
    "structured": {
      "title": "Evening Walk Discussion",
      "overview": "User plans to take a walk in the park this evening.",
      "emoji": "🚶",
      "category": "personal",
      "action_items": [],
      "events": []
    }
  }'
```

### Notes
- **Title Guidelines:** Short, descriptive (e.g., "Morning Medication Routine")
- **Overview Guidelines:** 2-3 sentences, cover main topics discussed
- **Emoji Guidelines:** Single emoji representing conversation theme
- Backend updates conversation status to `"completed"` automatically
- iOS app shows summaries in timeline/history view

---

## 3. Push Notification Callback Endpoint

**Endpoint:** `POST /v1/ella/notification`
**Purpose:** Ella's scanner sends urgent notifications/responses to user's device
**Flow:** Backend generates TTS audio → Sends FCM push → iOS plays audio

### Request Body

```json
{
  "uid": "user-123",
  "message": "I noticed you mentioned chest pain. Are you feeling okay? Should I call for help?",
  "urgency": "EMERGENCY",
  "generate_audio": true,
  "metadata": {
    "trigger": "chest_pain_keyword",
    "confidence": 0.95,
    "conversation_id": "conv-456"
  }
}
```

### Request Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uid` | string | ✅ | Firebase user ID |
| `message` | string | ✅ | Message to send (text + TTS) |
| `urgency` | string | ✅ | Urgency level (see below) |
| `generate_audio` | boolean | ❌ | Generate TTS? (default: true) |
| `metadata` | object | ❌ | Debug/tracking metadata |

### Urgency Levels

| Level | Description | iOS Behavior |
|-------|-------------|--------------|
| `EMERGENCY` | Medical crisis | Alert sound + badge + audio playback |
| `QUESTION` | User asked Ella something | Silent push + audio playback |
| `WAKE_WORD` | "Hey Ella" detected | Silent push + audio playback |
| `INTERESTING` | Worth noting | Silent push |
| `NORMAL` | Low priority | Background update |

### Response

```json
{
  "status": "success",
  "message_id": "fcm-12345abcde",
  "audio_url": "https://storage.googleapis.com/omi-dev-ca005.firebasestorage.app/tts_audio/user-123/audio-456.mp3",
  "duration_seconds": 3.2,
  "urgency": "EMERGENCY"
}
```

### Response Schema

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"success"` or `"no_token"` |
| `message_id` | string | FCM message ID |
| `audio_url` | string | GCS URL for TTS audio file |
| `duration_seconds` | float | Audio duration |
| `urgency` | string | Echo of request urgency |

### Example curl

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/notification" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "message": "Good morning! Did you take your medication?",
    "urgency": "QUESTION",
    "generate_audio": true
  }'
```

### TTS Audio Details

**Provider:** OpenAI TTS API
**Voice:** Nova (female, caring tone)
**Model:** HD (high quality)
**Speed:** 1.0x (normal)
**Format:** MP3
**Storage:** Google Cloud Storage (public URLs)
**Caching:** Redis (1 hour TTL for duplicate text)

### Error Responses

**No FCM Token:**
```json
{
  "status": "no_token",
  "message": "User has no FCM token registered"
}
```

**TTS Generation Failed:**
```json
{
  "status": "error",
  "detail": "TTS generation failed: <error details>"
}
```

### Notes
- **FCM Token:** User must have iOS app installed and granted push permissions
- **TTS Audio:** Automatically generated if `generate_audio=true`
- **Caching:** Identical messages reuse cached audio (saves cost)
- **Silent Push:** iOS wakes app in background, plays audio without UI
- **Emergency Sound:** Only `EMERGENCY` urgency plays alert sound

---

## 4. Health Check Endpoint

**Endpoint:** `GET /v1/ella/health`
**Purpose:** Verify Ella integration endpoints are operational

### Response

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

### Example curl

```bash
curl "https://api.ella-ai-care.com/v1/ella/health"
```

---

## 5. Direct Memory Creation (Alternative)

**Endpoint:** `POST /v3/memories`
**Purpose:** Direct memory creation (requires Firebase auth token)
**Flow:** iOS app or external integrations with user auth

### Request Body

```json
{
  "content": "User mentioned feeling anxious about upcoming appointment",
  "category": "system",
  "visibility": "private",
  "tags": ["anxiety", "appointments"]
}
```

### Authentication

**Required Header:**
```
Authorization: Bearer <firebase_id_token>
```

### Notes
- **Use `/v1/ella/memory` instead** for Ella callbacks (no auth required)
- This endpoint auto-detects category if not provided
- Requires valid Firebase auth token
- Used by iOS app for user-created memories

---

## 6. Direct Notification (Admin-Only)

**Endpoint:** `POST /v1/notification`
**Purpose:** Send push notification with admin key

### Request Body

```json
{
  "uid": "user-123",
  "title": "Medication Reminder",
  "body": "Time to take your morning medication"
}
```

### Authentication

**Required Header:**
```
X-Secret-Key: <ADMIN_KEY>
```

### Notes
- **Use `/v1/ella/notification` instead** for Letta callbacks (includes TTS)
- This endpoint doesn't generate TTS audio
- Requires admin secret key (not suitable for Ella)

---

## Complete Integration Example

### Scenario: User Says "I have chest pain"

**1. Scanner Agent Detects Emergency**

OMI backend sends to n8n:
```json
POST https://n8n.ella-ai-care.com/webhook/scanner-agent
{
  "uid": "user-123",
  "segments": [
    {
      "text": "I have chest pain",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 2.5,
      "source": "omi"
    }
  ]
}
```

**2. Letta Agent Generates Response**

Letta scanner agent returns:
```json
{
  "urgency_level": "critical",
  "urgency_type": "medical",
  "message": "I heard you mention chest pain. This could be serious. Are you okay? Should I call for help?",
  "action_needed": true,
  "confidence": 0.95
}
```

**3. n8n Sends Notification Callback**

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/notification" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "message": "I heard you mention chest pain. This could be serious. Are you okay? Should I call for help?",
    "urgency": "EMERGENCY",
    "generate_audio": true,
    "metadata": {
      "trigger": "chest_pain_keyword",
      "confidence": 0.95,
      "letta_agent": "scanner-agent"
    }
  }'
```

**4. Backend Response**

```json
{
  "status": "success",
  "message_id": "fcm-abc123",
  "audio_url": "https://storage.googleapis.com/.../audio-456.mp3",
  "duration_seconds": 4.1,
  "urgency": "EMERGENCY"
}
```

**5. iOS Device Receives Push**

- FCM push wakes app in background
- App downloads TTS audio from GCS
- App plays audio with alert sound
- User hears Ella's caring response
- User can tap notification to open app

**6. Conversation Ends → Summary Callback**

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/conversation" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "conversation_id": "conv-456",
    "structured": {
      "title": "Emergency Health Alert",
      "overview": "User reported chest pain. Ella offered assistance and asked if emergency services were needed.",
      "emoji": "🚨",
      "category": "health",
      "action_items": [
        {
          "description": "Follow up on chest pain incident",
          "due_at": "2025-11-17T09:00:00Z"
        }
      ],
      "events": []
    }
  }'
```

**7. Memory Extraction Callback**

```bash
curl -X POST "https://api.ella-ai-care.com/v1/ella/memory" \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "user-123",
    "conversation_id": "conv-456",
    "memories": [
      {
        "content": "User experienced chest pain on November 16, 2025",
        "category": "system",
        "tags": ["health", "emergency", "chest-pain"]
      }
    ]
  }'
```

---

## Error Handling

### HTTP Status Codes

| Code | Meaning | Action |
|------|---------|--------|
| 200 | Success | Data stored/sent successfully |
| 400 | Bad Request | Check request body format |
| 403 | Forbidden | Check authentication (if required) |
| 500 | Server Error | Backend error, retry later |

### Common Errors

**Invalid UID:**
```json
{
  "detail": "User user-123 not found"
}
```

**Invalid Conversation ID:**
```json
{
  "detail": "Conversation conv-456 not found"
}
```

**Invalid Category:**
```json
{
  "detail": "Category 'invalid' not allowed. Must be: health, social, work, etc."
}
```

### Retry Logic

For transient errors (5xx):
- Retry with exponential backoff: 1s, 2s, 4s, 8s
- Max retries: 3
- Timeout: 10 seconds per request

---

## Rate Limits

**Current:** None (unlimited)
**Production TODO:** Implement per-UID rate limiting

Suggested limits:
- Memory callbacks: 100/minute per UID
- Conversation callbacks: 20/minute per UID
- Notification callbacks: 10/minute per UID

---

## Monitoring & Debugging

### Backend Logs

Monitor callback activity:
```bash
ssh root@100.101.168.91 "journalctl -u omi-backend -f | grep -E '(💾|📝|🚨)'"
```

**Log Symbols:**
- 💾 Memory callback
- 📝 Conversation callback
- 🚨 Notification callback
- ✅ Success
- ❌ Error

### Example Logs

```
💾 Ella Memory Callback - UID: user-123, Count: 2
  ✅ Saved: User takes blood pressure medication daily...
  ✅ Saved: User's daughter Sarah lives in Boston...

📝 Ella Conversation Callback - UID: user-123, ID: conv-456
  Title: Morning Health Check-In
  Category: health
  ✅ Updated conversation conv-456

🚨 Ella Notification Callback - UID: user-123
  Message: I noticed you mentioned chest pain...
  Urgency: EMERGENCY
  🎵 Generating TTS audio...
  ✅ TTS audio: https://storage.googleapis.com/.../audio.mp3
  ✅ Push notification sent: fcm-abc123
```

---

## API Endpoint Summary Table

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/v1/ella/memory` | POST | None | Store extracted memories |
| `/v1/ella/conversation` | POST | None | Update conversation summary |
| `/v1/ella/notification` | POST | None | Send push notification + TTS |
| `/v1/ella/health` | GET | None | Health check |
| `/v3/memories` | POST | Firebase | Direct memory creation (iOS app) |
| `/v1/notification` | POST | Admin Key | Admin-only push (no TTS) |

---

## OpenAPI/Swagger Documentation

Full interactive API documentation available at:
**https://api.ella-ai-care.com/docs**

Search for "ella" tag to see all Ella-specific endpoints.

---

## Testing Endpoints

Use E2E testing endpoints to verify integration:

**Scanner Agent Test:**
```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/scanner-agent" \
  -H "Authorization: Bearer dev_testing_key_12345test_user_123" \
  -H "Content-Type: application/json" \
  -d '{"text":"I have chest pain","uid":"test_user_123","source":"omi","debug":true}'
```

See `docs/E2E_TESTING_ENDPOINTS.md` for complete testing guide.

---

## Deployment Status

✅ **Production:** All endpoints deployed and operational
✅ **Base URL:** https://api.ella-ai-care.com
✅ **Service:** `omi-backend.service` (systemd)
✅ **VPS:** 100.101.168.91 (Vultr)
✅ **Logs:** `journalctl -u omi-backend -f`

---

## Support & Contact

**Documentation:**
- E2E Testing: `docs/E2E_TESTING_ENDPOINTS.md`
- Data Storage: `docs/DATA_STORAGE_AND_SEARCH_ARCHITECTURE.md`
- Letta Architecture: `docs/LETTA_ARCHITECTURE_ANALYSIS.md`
- Letta Q&A: `docs/LETTA_TEAM_QUESTIONS_ANSWERED.md`

**Backend Git:**
- Branch: `feature/e2e-agent-testing-unified`
- Latest Commits: Source field enhancement (`a51a07b6c`)

**Questions:** Contact OMI backend team via Discord or project channels

---

**Last Updated:** November 16, 2025, 21:15 UTC
**Maintained By:** OMI Backend Development Team
**Version:** 1.0.0
