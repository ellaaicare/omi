# n8n Callback Migration Guide

**TL;DR**: Replace all Ella custom callbacks with official OMI API endpoints.

---

## Why We're Removing Custom Endpoints

1. **Redundancy**: The official OMI API already provides validated endpoints for memories, conversations, and notifications
2. **Data Corruption Risk**: Custom endpoints bypassed Pydantic validation, causing production 500 errors
3. **Maintenance Burden**: Two sets of endpoints doing the same thing creates confusion
4. **Schema Drift**: Custom endpoints can fall out of sync with official models

---

## Endpoint Migration Table

| Old Ella Endpoint | New Official Endpoint | Notes |
|-------------------|----------------------|-------|
| `POST /v1/ella/memory` | `POST /v1/memories` | Use official Memory API |
| `POST /v1/ella/conversation-summary` | `POST /v1/conversations/{id}` | PATCH to update structured data |
| `POST /v1/ella/notification` | `POST /v1/notification` | Pass `audio_url` in `data` field |
| `POST /v1/ella/voice-session` | `POST /v1/dev/user/conversations/from-segments` | Use Developer API for full conversations |

---

## Notification Endpoint (Audio Push)

**Old way** (removed):
```json
POST /v1/ella/notification
{
  "uid": "user123",
  "message": "Hello!",
  "title": "Ella",
  "audio_url": "https://storage.example.com/audio.mp3",
  "urgency": "NORMAL"
}
```

**New way** (official):
```json
POST /v1/notification
{
  "uid": "user123",
  "title": "Ella",
  "body": "Hello!",
  "data": {
    "audio_url": "https://storage.example.com/audio.mp3",
    "action": "play_audio",
    "urgency": "NORMAL"
  }
}
```

The iOS app handles the `audio_url` in the `data` field and plays it automatically.

---

## Memory Endpoint

**Old way** (removed):
```json
POST /v1/ella/memory
{
  "uid": "user123",
  "conversation_id": "conv456",
  "memories": [
    {"content": "User likes coffee", "category": "interesting"}
  ]
}
```

**New way** (official):
```json
POST /v1/memories
Authorization: Bearer <user_token>
{
  "content": "User likes coffee",
  "category": "interesting",
  "visibility": "private"
}
```

For server-to-server calls, use the Developer API with API key authentication.

---

## Authentication

Official endpoints require proper authentication:
- **User endpoints**: Firebase ID token in `Authorization: Bearer <token>`
- **Developer endpoints**: API key via `X-API-Key` header
- **Admin endpoints**: `ADMIN_KEY` environment variable

---

## Questions?

Contact the backend team if you need help migrating workflows.

**Last Updated**: January 2026
