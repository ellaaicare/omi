# Letta API Endpoints

**Version**: 1.0 | **Updated**: December 5, 2025

API endpoints for Letta agent tools to access OMI conversation data.

---

## Authentication

All endpoints require the `secret-key` header with either:
- `INTERNAL_API_KEY` (preferred for Letta)
- `ADMIN_KEY`

```bash
-H "secret-key: ${INTERNAL_API_KEY}"
```

---

## Endpoints

### GET /v1/ella/conversations/{conversation_id}

Get a single conversation with full transcript.

**Parameters:**
| Name | Location | Required | Description |
|------|----------|----------|-------------|
| `conversation_id` | path | Yes | Conversation ID (from rolling_summaries [ID] tag) |
| `uid` | query | Yes | User's OMI UID |

**Example:**
```bash
curl -X GET "https://api.ella-ai-care.com/v1/ella/conversations/1764972191258?uid=5aGC5YE9BnhcSoTxxtT4ar6ILQy2" \
  -H "secret-key: ${INTERNAL_API_KEY}"
```

**Response:**
```json
{
  "id": "1764972191258",
  "created_at": "2025-12-05T22:05:17.451675+00:00",
  "started_at": "2025-12-05T22:05:17.451675+00:00",
  "finished_at": "2025-12-05T22:05:17.451675+00:00",
  "status": "completed",
  "source": "voice_mode_v2",
  "language": "en",
  "structured": {
    "title": "Cafe Check-in",
    "overview": "User at Blue Bottle Palo Alto enjoying a NOLA...",
    "emoji": "☕",
    "category": "personal",
    "action_items": [],
    "events": []
  },
  "transcript_segments": [
    {
      "text": "i'm at the blue bottle in palo alto",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "role": "user",
      "start": 1764972227.45,
      "end": 1764972228.45,
      "source": "voice_mode_v2"
    },
    {
      "text": "Nice, Blue Bottle in Palo Alto is cozy! What're you sipping today?",
      "speaker": "SPEAKER_01",
      "speaker_id": 1,
      "is_user": false,
      "role": "assistant",
      "start": 1764972230.90,
      "end": 1764972231.90,
      "source": "voice_mode_v2"
    }
  ]
}
```

---

### GET /v1/ella/conversations

List recent conversations for a user.

**Parameters:**
| Name | Location | Required | Default | Description |
|------|----------|----------|---------|-------------|
| `uid` | query | Yes | - | User's OMI UID |
| `limit` | query | No | 10 | Max conversations to return |
| `offset` | query | No | 0 | Pagination offset |
| `include_transcript` | query | No | false | Include full transcript_segments |

**Example:**
```bash
curl -X GET "https://api.ella-ai-care.com/v1/ella/conversations?uid=5aGC5YE9BnhcSoTxxtT4ar6ILQy2&limit=5" \
  -H "secret-key: ${INTERNAL_API_KEY}"
```

**Response:**
```json
[
  {
    "id": "1764972191258",
    "created_at": "2025-12-05T22:05:17.451675+00:00",
    "status": "completed",
    "structured": {
      "title": "Cafe Check-in",
      "overview": "User at Blue Bottle...",
      "emoji": "☕",
      "category": "personal"
    }
  },
  {
    "id": "1764900169504",
    "created_at": "2025-12-05T02:04:16.000000+00:00",
    "status": "completed",
    "structured": {
      "title": "Phone UI Test",
      "overview": "Testing phone interface...",
      "emoji": "📱",
      "category": "other"
    }
  }
]
```

---

## Letta Tool Implementation

### Python Tool for Letta Agents

```python
import requests
from typing import Optional, List, Dict, Any

INTERNAL_API_KEY = "your-internal-api-key"
BASE_URL = "https://api.ella-ai-care.com"


def get_conversation(conversation_id: str, uid: str) -> Dict[str, Any]:
    """
    Fetch full conversation transcript from OMI backend.

    Use this when you need more context than the one-line summary provides.
    Get conversation_id from the [ID] tag in rolling_summaries.

    Args:
        conversation_id: The conversation ID (e.g., "1764972191258")
        uid: User's OMI UID

    Returns:
        Full conversation dict with transcript_segments, structured summary, etc.
    """
    response = requests.get(
        f"{BASE_URL}/v1/ella/conversations/{conversation_id}",
        headers={"secret-key": INTERNAL_API_KEY},
        params={"uid": uid}
    )
    response.raise_for_status()
    return response.json()


def list_conversations(
    uid: str,
    limit: int = 10,
    include_transcript: bool = False
) -> List[Dict[str, Any]]:
    """
    List recent conversations for a user.

    Args:
        uid: User's OMI UID
        limit: Max conversations to return (default: 10)
        include_transcript: Include full transcript_segments (default: False)

    Returns:
        List of conversation dicts with structured summaries
    """
    response = requests.get(
        f"{BASE_URL}/v1/ella/conversations",
        headers={"secret-key": INTERNAL_API_KEY},
        params={
            "uid": uid,
            "limit": limit,
            "include_transcript": include_transcript
        }
    )
    response.raise_for_status()
    return response.json()


def format_transcript(conversation: Dict[str, Any]) -> str:
    """
    Format transcript_segments into readable text.

    Args:
        conversation: Full conversation dict from get_conversation()

    Returns:
        Formatted transcript string
    """
    segments = conversation.get("transcript_segments", [])
    lines = []
    for seg in segments:
        role = "User" if seg.get("is_user") else "Ella"
        text = seg.get("text", "")
        lines.append(f"{role}: {text}")
    return "\n".join(lines)
```

### Example Usage in Letta Agent

```python
# When user asks about a past conversation
conversation_id = "1764972191258"  # From rolling_summaries [ID] tag
uid = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"

# Fetch full conversation
conv = get_conversation(conversation_id, uid)

# Get structured summary
title = conv["structured"]["title"]
overview = conv["structured"]["overview"]

# Get full transcript
transcript = format_transcript(conv)

print(f"Title: {title}")
print(f"Overview: {overview}")
print(f"\nTranscript:\n{transcript}")
```

---

## Response Fields

### Conversation Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique conversation ID |
| `created_at` | datetime | When conversation was created |
| `started_at` | datetime | When conversation started |
| `finished_at` | datetime | When conversation ended |
| `status` | string | `completed`, `in_progress`, `processing`, `failed` |
| `source` | string | `voice_mode_v2`, `omi`, `friend`, etc. |
| `language` | string | Language code (e.g., "en") |
| `structured` | object | AI-generated summary |
| `transcript_segments` | array | Full transcript with timestamps |

### Structured Object

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Conversation title |
| `overview` | string | Brief summary |
| `emoji` | string | Representative emoji |
| `category` | string | Category (personal, health, work, etc.) |
| `action_items` | array | Extracted action items |
| `events` | array | Extracted calendar events |

### TranscriptSegment Object

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Spoken text |
| `speaker` | string | Speaker ID (SPEAKER_00, SPEAKER_01) |
| `speaker_id` | int | Numeric speaker ID |
| `is_user` | bool | True if user spoke, False if assistant |
| `role` | string | "user" or "assistant" |
| `start` | float | Segment start timestamp |
| `end` | float | Segment end timestamp |
| `source` | string | Audio source |

---

## Error Responses

| Status | Description |
|--------|-------------|
| 403 | Invalid API key |
| 404 | Conversation not found |
| 500 | Server error |

```json
{"detail": "Conversation conv-123 not found for user user-456"}
```

---

## Related Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/trigger-incoming-call` | Trigger incoming call push notification |
| `POST /v1/ella/notification` | Send TTS notification |
| `POST /v1/ella/memory` | Store memory from Letta |
| `POST /v1/ella/conversation` | Update conversation from Letta |

See `docs/ELLA_CALLBACK_ENDPOINTS.md` for full documentation.
