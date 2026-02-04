# Backend Endpoint Requirements PRD

**Date**: January 18, 2026
**Status**: Draft
**Owner**: Backend Team
**Purpose**: Ensure all endpoints are functional for Ella fork

---

## Overview

The Ella iOS app is a fork of OMI that points to `api.ella-ai-care.com` instead of upstream. This document lists all endpoints the app calls and their requirements.

---

## URL Configuration

### App Configuration
```
API_BASE_URL=https://api.ella-ai-care.com/
```

The app reads this from `.env` (prod) and `.dev.env` (dev) files via the Envied package.

### Single Hardcoded URL (Fixed)
```dart
// lib/services/connectivity_service.dart - UPDATED
uri: Uri.parse('https://api.ella-ai-care.com/v1/health')
```

---

## Critical Endpoints

### 1. Health Check
```
GET /v1/health
```
**Purpose**: Connectivity checking (app checks if backend is reachable)
**Response**: Any 2xx or 3xx status code
**Priority**: CRITICAL - App won't work if this fails

---

### 2. Transcription WebSocket
```
WSS /v4/listen?language={lang}&sample_rate=16000&codec={codec}&uid={uid}&include_speech_profile={bool}&stt_service={service}&conversation_timeout={sec}&source={source}
```
**Purpose**: Real-time speech-to-text streaming
**Input**: PCM16/OPUS audio frames
**Output**: JSON transcript segments
**Priority**: CRITICAL - Core recording functionality

**Parameters**:
| Param | Type | Description |
|-------|------|-------------|
| language | string | ISO language code (e.g., "en") |
| sample_rate | int | 16000 or 8000 |
| codec | string | "pcm16" or "opus" |
| uid | string | Firebase user ID |
| include_speech_profile | bool | Include speaker profile |
| stt_service | string | STT provider ("deepgram", "soniox", etc.) |
| conversation_timeout | int | Seconds before auto-ending |
| source | string | "omi" or "watch" or "phone" |

---

### 3. Memory/Conversation Summarization
```
POST /v1/conversations
```
**Purpose**: Create conversation from transcript + generate summary
**Input**: Transcript segments, audio metadata
**Output**: Structured memory with title, summary, action items
**Priority**: CRITICAL - Memory creation is core feature

**This is where summarization happens** - backend processes transcript and generates memory content.

---

### 4. Conversation CRUD
```
GET  /v1/conversations                           # List with filters
GET  /v1/conversations/{id}                      # Get single
DELETE /v1/conversations/{id}                    # Delete
PATCH /v1/conversations/{id}/title              # Update title
PATCH /v1/conversations/{id}/visibility         # Change visibility
PATCH /v1/conversations/{id}/folder             # Move to folder
POST /v1/conversations/{id}/reprocess           # Re-summarize with different app
```
**Priority**: HIGH

---

### 5. Memory CRUD (v3)
```
POST   /v3/memories                    # Create memory
GET    /v3/memories                    # List memories
GET    /v3/memories/{id}               # Get single
PATCH  /v3/memories/{id}?value={text}  # Edit memory content
DELETE /v3/memories/{id}               # Delete memory
DELETE /v3/memories                    # Delete all
PATCH  /v3/memories/{id}/visibility    # Update visibility
POST   /v3/memories/{id}/review        # Mark as reviewed
```
**Priority**: HIGH - Memory management

---

### 6. Chat/Messages
```
GET    /v2/messages                    # List messages
DELETE /v2/messages                    # Clear chat
POST   /v2/messages                    # Send message (streaming SSE)
POST   /v2/initial-message             # Get app initial message
POST   /v2/voice-messages              # Send voice message
POST   /v2/voice-message/transcribe    # Transcribe voice
```
**Priority**: HIGH - Chat interface

---

### 7. User Management
```
PATCH  /v1/users/geolocation           # Update location
DELETE /v1/users/delete-account        # Delete account
POST   /v1/users/fcm-token             # Save push token
GET    /v1/users/me/subscription       # Get subscription
PATCH  /v1/users/language              # Set language
```
**Priority**: HIGH

---

### 8. Developer Webhooks (Important for Wake Word)
```
GET    /v1/users/developer/webhook/{type}           # Get webhook URL
POST   /v1/users/developer/webhook/{type}           # Set webhook URL
POST   /v1/users/developer/webhook/{type}/enable    # Enable webhook
POST   /v1/users/developer/webhook/{type}/disable   # Disable webhook
GET    /v1/users/developer/webhooks/status          # Get all webhook status
```

**Webhook Types**:
| Type | Fires When | Use Case |
|------|------------|----------|
| `audio_bytes` | Raw audio chunks received | Audio processing |
| `realtime_transcript` | Transcript segment ready | **WAKE WORD SCANNING** |
| `memory_created` | Conversation saved | Memory enhancement |
| `day_summary` | Daily summary generated | Daily insights |

**Priority**: HIGH - This is how we can implement wake word scanning!

---

### 9. Apps/Templates System
```
GET    /v2/apps                        # List apps
POST   /v1/apps/enable                 # Enable app for user
POST   /v1/apps/disable                # Disable app
GET    /v1/apps/{id}                   # Get app details
```

**App External Integration** (for wake word):
```json
{
  "external_integration": {
    "triggers_on": "transcript_processed",
    "webhook_url": "https://n8n.ella-ai-care.com/webhook/transcript",
    "actions": ["create_facts", "read_memories"]
  }
}
```
**Priority**: MEDIUM - Alternative wake word path

---

### 10. Action Items
```
GET    /v1/action-items                # List items
POST   /v1/action-items                # Create item
PATCH  /v1/action-items/{id}           # Update item
DELETE /v1/action-items/{id}           # Delete item
PATCH  /v1/action-items/{id}/completed # Toggle completion
```
**Priority**: MEDIUM

---

### 11. TTS Generation (Ella-specific)
```
POST   /api/v1/tts/generate            # Generate TTS audio
```
**Input**: `{ "text": "...", "voice": "nova" }`
**Output**: `{ "audio_url": "https://..." }`
**Priority**: HIGH - Voice responses

---

### 12. Push Notifications
```
POST   /v1/test/tts-notification       # Test TTS push (dev only)
POST   /v1/test/notification           # Test regular push (dev only)
```
**Priority**: MEDIUM - Testing only

---

## Wake Word System Options

### Option A: Developer Webhook (Recommended)
1. Pre-configure `realtime_transcript` webhook for Ella users
2. Webhook URL: `https://n8n.ella-ai-care.com/webhook/transcript-scanner`
3. n8n scans for wake words in real-time
4. On detection, send TTS push notification

**Backend Requirement**:
- Ensure `/v1/users/developer/webhook/realtime_transcript` works
- Webhook receives: `{ "text": "...", "timestamp": "...", "uid": "..." }`

### Option B: App External Integration
1. Create "Ella Scanner" app with `triggers_on: "transcript_processed"`
2. Enable app for all Ella users by default
3. Backend calls webhook on each transcript segment

**Backend Requirement**:
- Implement app external integration webhook system
- Ensure `transcript_processed` trigger works

### Option C: On-Device (iOS Speech Framework)
1. Use iOS SFSpeechRecognizer for on-device STT
2. Scan locally for wake words
3. No backend requirement

**App Requirement**: Implement in WakeWord plugin (already skeleton exists)

---

## Endpoints To Verify

Please verify these endpoints are functional on `api.ella-ai-care.com`:

### Critical (Blocking)
- [ ] `GET /v1/health` - Returns 200
- [ ] `WSS /v4/listen` - WebSocket accepts connection and transcribes
- [ ] `POST /v1/conversations` - Creates conversation with summary
- [ ] `GET /v1/conversations` - Returns user's conversations

### High Priority
- [ ] `POST /v1/users/fcm-token` - Saves push token
- [ ] `GET /v1/users/developer/webhooks/status` - Returns webhook config
- [ ] `POST /v1/users/developer/webhook/realtime_transcript` - Sets webhook URL
- [ ] `POST /v2/messages` - Sends chat message (streaming)
- [ ] `POST /api/v1/tts/generate` - Generates TTS audio

### Medium Priority
- [ ] `PATCH /v3/memories/{id}` - Edits memory content
- [ ] `POST /v1/apps/enable` - Enables app for user
- [ ] `POST /v1/test/tts-notification` - Sends test TTS push

---

## Configuration Questions for Backend Team

1. **STT Service**: Which provider is configured? (Deepgram, Soniox, etc.)
2. **Summary Generation**: Which LLM is used for memory summarization?
3. **Webhook Delivery**: Is the webhook system active and delivering to user URLs?
4. **TTS Provider**: Which TTS service is configured? (OpenAI, ElevenLabs, etc.)
5. **FCM Configuration**: Is Firebase configured with our project credentials?

---

## Testing Commands

### Health Check
```bash
curl -s https://api.ella-ai-care.com/v1/health
```

### Webhook Status (requires auth)
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://api.ella-ai-care.com/v1/users/developer/webhooks/status
```

### TTS Test (requires auth)
```bash
curl -s -X POST https://api.ella-ai-care.com/api/v1/tts/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from Ella", "voice": "nova"}'
```

---

## Summary

| Category | Endpoint Count | Status |
|----------|----------------|--------|
| Core (health, transcription, conversations) | 5 | Verify |
| Memory CRUD | 8 | Verify |
| Chat/Messages | 6 | Verify |
| User Management | 10+ | Verify |
| Webhooks | 5 | **Critical for wake word** |
| Apps | 10+ | Verify |
| TTS | 1 | Verify |

**Total: ~50 endpoints to verify**

---

*Document created: January 18, 2026*
