# Omi Webhook Payload Documentation for Letta Integration

## Overview

Omi sends **two types** of webhook payloads to your plugins/integrations:

1. **Memory/Conversation Payloads** - Complete conversations after they finish (most common)
2. **Real-time Payloads** - Streaming transcript segments during ongoing conversations (deprecated but still supported)

## 🎯 How to Distinguish Payload Types

### Method 1: Check the Endpoint Path
Different webhook URLs = different payload types:

```
Memory Webhook:    https://your-n8n.com/webhook/memory-capture
Realtime Webhook:  https://your-n8n.com/webhook/realtime-capture
```

### Method 2: Check Payload Structure
```javascript
// Memory/Conversation payload has these fields:
if (payload.structured && payload.transcript_segments && payload.created_at) {
  // This is a MEMORY payload
}

// Realtime payload has these fields:
if (payload.session_id && payload.segments && !payload.structured) {
  // This is a REALTIME payload
}
```

---

## 📦 Payload Type 1: Memory/Conversation (Most Common)

### When It's Sent
- **After a conversation finishes** on the Omi device
- Triggered when user stops recording or device automatically detects conversation end
- Contains **complete, processed data** with AI-generated summaries

### Complete Sample Payload

```json
{
  "created_at": "2025-01-16T15:30:45.123Z",
  "started_at": "2025-01-16T15:25:00.000Z",
  "finished_at": "2025-01-16T15:30:30.000Z",

  "structured": {
    "title": "Product Launch Planning Meeting",
    "overview": "Discussion about Q2 product launch strategy, timeline, and marketing approach. Decided to target April 15th release date.",
    "emoji": "🚀",
    "category": "work",
    "action_items": [
      {
        "description": "Schedule marketing kickoff meeting for next week",
        "completed": false,
        "created_at": "2025-01-16T15:30:45.123Z",
        "due_at": null,
        "conversation_id": "conv_abc123"
      },
      {
        "description": "Prepare Q2 budget proposal by Friday",
        "completed": false,
        "created_at": "2025-01-16T15:30:45.123Z",
        "due_at": "2025-01-20T17:00:00.000Z",
        "conversation_id": "conv_abc123"
      }
    ],
    "events": [
      {
        "title": "Product Launch Review",
        "description": "Final review before launch",
        "start": "2025-04-10T14:00:00.000Z",
        "duration": 60,
        "created": false
      }
    ]
  },

  "transcript_segments": [
    {
      "text": "Hey team, let's discuss the new product launch timeline",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "person_id": "person_xyz789",
      "start": 0.0,
      "end": 3.5
    },
    {
      "text": "Sure, I think we should target Q2. What do you think about mid-April?",
      "speaker": "SPEAKER_01",
      "speaker_id": 1,
      "is_user": false,
      "person_id": null,
      "start": 4.2,
      "end": 8.7
    },
    {
      "text": "That sounds good. Let's aim for April 15th specifically.",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "person_id": "person_xyz789",
      "start": 9.1,
      "end": 12.3
    }
  ],

  "photos": [
    {
      "id": "photo_def456",
      "base64": "iVBORw0KGgoAAAANS...(base64 data)...",
      "description": "Whiteboard with Q2 timeline sketched out",
      "created_at": "2025-01-16T15:27:30.000Z",
      "discarded": false
    }
  ],

  "geolocation": {
    "google_place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
    "latitude": 37.7749,
    "longitude": -122.4194,
    "address": "123 Market St, San Francisco, CA 94103",
    "location_type": "office"
  },

  "plugins_results": [
    {
      "plugin_id": "zapier_integration",
      "content": "Task created in Asana: 'Prepare Q2 budget proposal'"
    }
  ],

  "discarded": false,
  "source": "omi",
  "language": "en"
}
```

### Key Fields Explanation

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | ISO datetime | When conversation was created |
| `started_at` | ISO datetime | When recording started |
| `finished_at` | ISO datetime | When recording ended |
| `structured.title` | string | AI-generated conversation title |
| `structured.overview` | string | AI-generated summary (1-3 sentences) |
| `structured.emoji` | string | Visual representation emoji |
| `structured.category` | enum | Category (work, personal, health, etc.) |
| `structured.action_items` | array | Extracted tasks/todos |
| `structured.events` | array | Calendar events to create |
| `transcript_segments` | array | Full conversation transcript with timing |
| `photos` | array | Photos from Omi Glass (if available) |
| `geolocation` | object | Location data (if enabled) |
| `discarded` | boolean | If `true`, user marked conversation as junk |

### Categories Available

```javascript
const CATEGORIES = [
  'work', 'personal', 'education', 'health', 'finance', 'legal',
  'philosophy', 'spiritual', 'science', 'entrepreneurship',
  'parenting', 'romance', 'travel', 'inspiration', 'technology',
  'business', 'social', 'sports', 'politics', 'literature',
  'history', 'architecture', 'music', 'weather', 'news',
  'entertainment', 'psychology', 'design', 'family',
  'economics', 'environment', 'other'
];
```

---

## 📦 Payload Type 2: Real-time (Deprecated but Supported)

### When It's Sent
- **During an ongoing conversation** (every 3-10 seconds)
- Streams transcript segments as they're being transcribed
- **Does NOT include** AI summaries, categories, or action items

### Sample Payload

```json
{
  "session_id": "session_live_abc123",
  "segments": [
    {
      "text": "So I was thinking about the marketing strategy",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "person_id": "person_xyz789",
      "start": 45.2,
      "end": 48.7
    },
    {
      "text": "Yeah, that makes sense. What channels are we considering?",
      "speaker": "SPEAKER_01",
      "speaker_id": 1,
      "is_user": false,
      "person_id": null,
      "start": 49.3,
      "end": 52.1
    }
  ]
}
```

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Unique ID for this recording session |
| `segments` | array | New transcript segments since last update |

### Important Notes
- Real-time payloads are sent **multiple times** per conversation
- Each payload contains **incremental segments** (not the full transcript)
- **No AI processing** - just raw transcription
- Omi team recommends using **Memory payloads instead** for most use cases

---

## 🔀 Multi-Agent Routing Strategy for Letta

### Recommended Approach

Route to different Letta agents based on:

1. **Category** - Different agents for work, personal, health, etc.
2. **Action Item Count** - Task-focused agent if action items present
3. **Duration** - Long-form agent for meetings, short-form for quick notes
4. **Content Type** - Photo analysis agent if photos included

### Example Routing Logic

```javascript
// In n8n workflow transform node
const payload = $input.item.json;

// Determine agent based on category
let agentId = 'agent-general';

if (payload.structured.category === 'work') {
  agentId = 'agent-work-assistant';
} else if (payload.structured.category === 'health') {
  agentId = 'agent-health-tracker';
} else if (payload.structured.category === 'personal') {
  agentId = 'agent-personal-journal';
}

// Override for action-heavy conversations
if (payload.structured.action_items && payload.structured.action_items.length > 3) {
  agentId = 'agent-task-manager';
}

// Override for photo-based memories
if (payload.photos && payload.photos.length > 0) {
  agentId = 'agent-visual-memory';
}

return {
  json: {
    agent_id: agentId,
    payload: payload
  }
};
```

---

## 🎨 Formatting for Letta

### Rich Message Format

```javascript
// Build rich context message for Letta
const buildLettaMessage = (payload) => {
  const duration = Math.round(
    (new Date(payload.finished_at) - new Date(payload.started_at)) / 60000
  );

  let message = `${payload.structured.emoji} ${payload.structured.title}\n\n`;
  message += `Category: ${payload.structured.category}\n`;
  message += `Duration: ${duration} minutes\n`;
  message += `Location: ${payload.geolocation?.address || 'Unknown'}\n\n`;

  message += `📝 Overview:\n${payload.structured.overview}\n\n`;

  // Action items
  if (payload.structured.action_items?.length > 0) {
    message += `✅ Action Items:\n`;
    payload.structured.action_items.forEach((item, i) => {
      message += `${i + 1}. ${item.description}\n`;
    });
    message += `\n`;
  }

  // Events
  if (payload.structured.events?.length > 0) {
    message += `📅 Events:\n`;
    payload.structured.events.forEach(event => {
      message += `- ${event.title} (${event.start})\n`;
    });
    message += `\n`;
  }

  // Photos
  if (payload.photos?.length > 0) {
    message += `📸 Photos:\n`;
    payload.photos.forEach((photo, i) => {
      message += `${i + 1}. ${photo.description}\n`;
    });
    message += `\n`;
  }

  // Full transcript
  message += `💬 Full Transcript:\n`;
  payload.transcript_segments.forEach(seg => {
    const speaker = seg.is_user ? 'User' : `Speaker ${seg.speaker_id}`;
    message += `${speaker}: ${seg.text}\n`;
  });

  message += `\n---\nSource: Omi wearable device`;

  return message;
};
```

---

## ⚠️ Important Gotchas

### 1. Discarded Conversations
```javascript
// ALWAYS check if conversation was discarded
if (payload.discarded === true) {
  // Skip processing - user marked as junk/spam
  return { skip: true };
}
```

### 2. Empty Transcripts
```javascript
// Check for actual content
if (!payload.transcript_segments || payload.transcript_segments.length === 0) {
  // No transcript available - skip or handle specially
  return { skip: true };
}
```

### 3. Timestamp Handling
```javascript
// Omi sends ISO 8601 strings
const createdAt = new Date(payload.created_at);
const duration = (new Date(payload.finished_at) - new Date(payload.started_at)) / 1000; // seconds
```

### 4. Speaker Identification
```javascript
// is_user = true means the Omi wearer
// speaker_id increments for other people (SPEAKER_01, SPEAKER_02, etc.)
// person_id links to Omi's contact database (if available)
```

---

## 🔧 Testing Your Integration

### 1. Test with Omi App
- Open Omi mobile app
- Settings → Apps → Your webhook URL
- Record a short conversation
- Check your n8n/webhook logs

### 2. Manual Test Payload
```bash
curl -X POST https://your-n8n.com/webhook/omi-memory \
  -H "Content-Type: application/json" \
  -d '{
    "created_at": "2025-01-16T15:30:45.123Z",
    "started_at": "2025-01-16T15:25:00.000Z",
    "finished_at": "2025-01-16T15:30:30.000Z",
    "structured": {
      "title": "Test Memory",
      "overview": "This is a test conversation",
      "emoji": "🧪",
      "category": "other",
      "action_items": [],
      "events": []
    },
    "transcript_segments": [
      {
        "text": "This is a test message",
        "speaker": "SPEAKER_00",
        "speaker_id": 0,
        "is_user": true,
        "start": 0.0,
        "end": 2.5
      }
    ],
    "photos": [],
    "discarded": false
  }'
```

---

## 📚 Additional Resources

- **Omi Plugin Docs**: https://docs.omi.me/docs/developer/apps/Introduction
- **Omi API Reference**: https://docs.omi.me/doc/developer/api
- **Letta Integration Examples**: See n8n-workflows/ directory
- **Community Discord**: http://discord.omi.me

---

## 🤝 Support

Questions? Reach out:
- **Omi Discord**: http://discord.omi.me
- **Letta Discord**: https://discord.gg/letta
- **GitHub Issues**: https://github.com/BasedHardware/omi/issues
