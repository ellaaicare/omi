# OMI App/Plugin System - Complete Reference

**Last Updated**: January 2026
**Purpose**: Document the native app system for Ella integration

---

## Overview

OMI has a built-in app marketplace where apps can:
- Process conversations (summary templates)
- Provide chat interfaces
- Receive real-time webhooks (transcripts, audio, memories)
- Send proactive notifications

**Ella should use this system** instead of custom developer webhooks.

---

## App Capabilities

Apps can have multiple capabilities (comma-separated):

| Capability | Description | Ella Use |
|------------|-------------|----------|
| `chat` | Chat interface with custom system prompt | Ella chat assistant |
| `memories` | Summary template (appears in reprocess dropdown) | Ella-style summaries |
| `external_integration` | Webhook callbacks on events | Wake word scanning |
| `proactive_notification` | Send notifications to user | TTS alerts |
| `persona` | Full persona clone (EXCLUSIVE - cannot combine) | Not used |

**Ella App Capabilities**: `chat`, `memories`, `external_integration`, `proactive_notification`

---

## External Integration URLs

| URL Field | Purpose | When Called |
|-----------|---------|-------------|
| `webhook_url` | Receives trigger events (transcripts, memories) | On configured trigger |
| `app_home_url` | Dashboard/config page for the app | User visits app settings |
| `setup_completed_url` | Verify user has completed setup | When enabling app |
| `chat_tools_manifest_url` | JSON manifest of custom chat tools | When loading chat |
| `auth_steps[].url` | OAuth authorization URLs | During app connection |

### Webhook URL

Receives POST requests when triggers fire:

**transcript_processed trigger:**
```json
{
    "session_id": "user_uid",
    "segments": [
        {"text": "...", "speaker": "SPEAKER_00", "is_user": true, "start": 0.0, "end": 3.5}
    ],
    "conversation_id": "conv-123"
}
```

**memory_creation trigger:**
```json
{
    "id": "conv-123",
    "title": "Meeting notes",
    "overview": "...",
    "structured": {...},
    "transcript_segments": [...]
}
```

### Webhook Response

Apps can return:
```json
{
    "message": "Text shown as notification",
    "notification": {
        "prompt": "Generate notification from this context",
        "params": ["user_context"],
        "context": {"question": "..."}
    }
}
```

### Setup Completed URL

Called when user enables app. Expected response:
```json
{"is_setup_completed": true}
```

If false, app won't be enabled.

### Chat Tools Manifest URL

Returns dynamic chat tools:
```json
{
    "tools": [
        {
            "name": "ask_ella",
            "description": "Ask Ella a question",
            "endpoint": "https://n8n.ella-ai-care.com/chat-tool",
            "method": "POST",
            "auth_required": true,
            "status_message": "Thinking...",
            "parameters": {
                "properties": {
                    "query": {"type": "string", "description": "The question"}
                },
                "required": ["query"]
            }
        }
    ]
}
```

---

## Trigger Types

| Trigger | Fires When | Payload |
|---------|------------|---------|
| `transcript_processed` | Real-time during conversation | Transcript segments |
| `memory_creation` | After conversation saved | Full conversation object |
| `audio_bytes` | At configured intervals | Raw audio data |
| (none) | No automatic triggers | Manual API calls only |

---

## App Prompts

| Prompt Field | Used For |
|--------------|----------|
| `chat_prompt` | System prompt for chat interface |
| `memory_prompt` | System prompt for conversation summarization |

---

## Default Summary App Selection

When a conversation is created, the summary app is selected by priority:

1. **User's preferred app** - `redis_db.get_user_preferred_app(uid)`
2. **First suggested app** - AI analyzes conversation and suggests apps
3. **Global defaults** - `conversation_summary_app_ids` Redis set

### Setting Ella as Default

**Per-user (recommended):**
```python
from database.redis_db import set_user_preferred_app
set_user_preferred_app(uid, 'ella-ai-agent')
```

**Or via API:**
```bash
curl -X PUT "https://api.ella-ai-care.com/v1/users/preferences/app?app_id=ella-ai-agent" \
  -H "Authorization: Bearer {token}"
```

**Global default (all users without preference):**
```python
from database.redis_db import add_conversation_summary_app_id
add_conversation_summary_app_id('ella-ai-agent')
```

---

## App Registration

### Create App (Firestore)

```python
from database.apps import upsert_app_to_db

app_data = {
    "id": "ella-ai-agent",
    "name": "Ella AI",
    "author": "Ella AI Care Team",
    "description": "Your caring AI companion",
    "image": "https://...",
    "capabilities": ["chat", "memories", "external_integration", "proactive_notification"],
    "category": "health",
    "approved": True,
    "enabled": True,

    # Chat capability
    "chat_prompt": "You are Ella, a caring AI companion...",

    # Memories capability (summary template)
    "memory_prompt": "Summarize focusing on emotional moments...",

    # External integration
    "external_integration": {
        "triggers_on": "transcript_processed",
        "webhook_url": "https://n8n.ella-ai-care.com/webhook/transcript",
        "app_home_url": "https://n8n.ella-ai-care.com/ella-config",
        "setup_completed_url": "https://n8n.ella-ai-care.com/verify-setup",
        "chat_tools_manifest_url": "https://n8n.ella-ai-care.com/tools-manifest",
        "actions": [
            {"action": "read_memories"},
            {"action": "read_conversations"},
            {"action": "create_facts"}
        ]
    },

    # Proactive notifications
    "proactive_notification": {
        "scopes": ["daily_summary", "user_context"]
    }
}

upsert_app_to_db(app_data)
```

### Enable App for User (Redis)

```python
from database.redis_db import enable_app
enable_app(uid, 'ella-ai-agent')
```

---

## Integration Actions

Apps can request these actions on user data:

| Action | Description |
|--------|-------------|
| `read_memories` | Read user's memories/facts |
| `read_conversations` | Read user's conversations |
| `create_facts` | Create new memories/facts |
| `create_memory` | Create new conversation |

Actions require Bearer token authentication via integration API:
```
POST /v2/integrations/{app_id}/user/memories
Authorization: Bearer {app_api_key}
```

---

## Backend Flow

### Conversation Creation Flow

```
1. Audio received via WebSocket (pusher.py)
2. Transcription via STT provider
3. process_conversation() called
4. _get_structured() generates summary
5. _trigger_apps() runs summary apps
6. trigger_external_integrations() fires webhooks
7. Results stored in Firestore
```

### Real-time Integration Flow

```
1. Transcript segment received
2. realtime_transcript_webhook() checks developer webhooks
3. trigger_realtime_integrations() checks app webhooks
4. All enabled apps with transcript_processed trigger receive POST
5. App responses processed (messages, notifications)
```

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/models/app.py` | App schema & validation |
| `backend/routers/apps.py` | App CRUD endpoints |
| `backend/utils/app_integrations.py` | Webhook triggering logic |
| `backend/utils/conversations/process_conversation.py` | Conversation processing |
| `backend/database/apps.py` | Firestore app storage |
| `backend/database/redis_db.py` | Redis app state |
| `backend/scripts/create_ella_ai_app.py` | Ella app registration |

---

## Ella n8n Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/webhook/transcript` | Receives real-time transcripts |
| `/webhook/summary-agent` | Custom summary generation |
| `/webhook/memory-agent` | Memory processing |
| `/ella-config` | User configuration page |
| `/verify-setup` | Setup verification |
| `/tools-manifest` | Dynamic chat tools |

---

## Testing

### Test App Webhook

```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/transcript" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-uid",
    "segments": [{"text": "Hey Ella, how are you?", "speaker": "SPEAKER_00"}],
    "conversation_id": "test-conv"
  }'
```

### Check App Status

```python
from database.apps import get_app_by_id_db
app = get_app_by_id_db('ella-ai-agent')
print(app)
```

### Check User's Enabled Apps

```python
from database.redis_db import get_enabled_apps
apps = get_enabled_apps(uid)
print(apps)
```
