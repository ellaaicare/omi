# Quick Answers for Letta Team

## 1. How does Omi distinguish memory vs realtime payloads?

### Answer: **Check if `structured` field exists**

```javascript
// Memory payload (most common - what you want)
if (payload.structured && payload.transcript_segments) {
  // This is a MEMORY payload - complete conversation with AI processing
}

// Realtime payload (deprecated - skip these)
if (payload.session_id && !payload.structured) {
  // This is a REALTIME payload - streaming segments during recording
  // SKIP THESE - Omi team recommends ignoring realtime payloads
}
```

### Recommended Approach:
**Just filter out realtime payloads and only process memory payloads:**

```javascript
const isMemory = payload.structured && payload.transcript_segments;
const isRealtime = payload.session_id && !payload.structured;

if (isRealtime) {
  return { skip: true, reason: 'realtime-not-supported' };
}

if (isMemory) {
  // Process this!
}
```

---

## 2. What field contains the user identifier?

### Answer: **`uid` is passed as a QUERY PARAMETER in the webhook URL**

**How it works:** Omi automatically appends `?uid={user_id}` to your webhook URL when sending the request.

**Example:**
```
Your webhook URL: https://your-n8n.com/webhook/omi-memory
Actual request:   https://your-n8n.com/webhook/omi-memory?uid=user_abc123
```

**How to extract in n8n:**
```javascript
// In n8n Code node
const uid = $input.item.json.query.uid;  // User ID from query parameter

// OR access via webhook node query params
const uid = $node["Webhook"].json.query.uid;
```

**Note:** The webhook payload body contains the conversation/memory data, but the `uid` comes from the URL query parameter, NOT from the JSON body.

---

## 3. What field contains the actual transcript/content?

### Answer: **`transcript_segments` array**

```javascript
// Full structure
{
  "transcript_segments": [
    {
      "text": "The actual words spoken",        // ← THIS IS THE CONTENT
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,                         // true = Omi wearer spoke
      "person_id": "person_xyz789",            // null if unknown person
      "start": 0.0,                            // seconds from start
      "end": 3.5                               // seconds from start
    }
  ]
}
```

### How to extract transcript:

```javascript
// Method 1: Simple concatenation
const transcript = payload.transcript_segments
  .map(seg => seg.text)
  .join(' ');

// Method 2: With speaker labels (recommended)
const transcript = payload.transcript_segments
  .map(seg => {
    const speaker = seg.is_user ? 'User' : `Speaker ${seg.speaker_id}`;
    return `${speaker}: ${seg.text}`;
  })
  .join('\n');

// Method 3: With timestamps
const transcript = payload.transcript_segments
  .map(seg => {
    const speaker = seg.is_user ? 'User' : `Speaker ${seg.speaker_id}`;
    const time = `[${seg.start.toFixed(1)}s]`;
    return `${time} ${speaker}: ${seg.text}`;
  })
  .join('\n');
```

---

## Complete Example n8n Code Node

```javascript
// COMPLETE EXAMPLE - Copy this into your n8n Code node
const payload = $input.item.json;

// 1. Skip realtime payloads
if (payload.session_id && !payload.structured) {
  return [{ json: { skip: true, reason: 'realtime' } }];
}

// 2. Skip discarded conversations
if (payload.discarded === true) {
  return [{ json: { skip: true, reason: 'discarded' } }];
}

// 3. Extract user ID from query parameter (Omi sends ?uid=...)
const uid = payload.query?.uid || $node["Webhook"].json.query?.uid || 'unknown-user';

// 4. Build transcript with speaker labels
let transcript = '';
if (payload.transcript_segments) {
  transcript = payload.transcript_segments
    .map(seg => {
      const speaker = seg.is_user ? 'User' : `Speaker ${seg.speaker_id}`;
      return `${speaker}: ${seg.text}`;
    })
    .join('\n');
}

// 5. Extract metadata
const title = payload.structured?.title || 'Untitled';
const overview = payload.structured?.overview || '';
const category = payload.structured?.category || 'other';
const emoji = payload.structured?.emoji || '💬';

// 6. Calculate duration
const duration = payload.started_at && payload.finished_at
  ? Math.round((new Date(payload.finished_at) - new Date(payload.started_at)) / 60000)
  : 0;

// 7. Build rich message for Letta
const lettaMessage = `${emoji} ${title}

Category: ${category}
Duration: ${duration} minutes

📝 Overview:
${overview}

💬 Transcript:
${transcript}

---
Source: Omi wearable device`;

// 8. Return formatted for Letta
return [{
  json: {
    message: {
      text: lettaMessage  // ← This is what Letta reads
    },
    user_id: uid,  // ← User ID from query parameter
    metadata: {
      category: category,
      duration: duration,
      has_action_items: (payload.structured?.action_items?.length || 0) > 0,
      has_photos: (payload.photos?.length || 0) > 0
    }
  }
}];
```

---

## Sample Payloads for Testing

### Memory Payload (Use This)
```json
{
  "created_at": "2025-01-16T15:30:45.123Z",
  "started_at": "2025-01-16T15:25:00.000Z",
  "finished_at": "2025-01-16T15:30:30.000Z",
  "structured": {
    "title": "Product Launch Planning",
    "overview": "Discussion about Q2 launch strategy",
    "emoji": "🚀",
    "category": "work",
    "action_items": [],
    "events": []
  },
  "transcript_segments": [
    {
      "text": "Let's discuss the new product launch",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "start": 0.0,
      "end": 3.5
    }
  ],
  "photos": [],
  "discarded": false
}
```

### Realtime Payload (Skip This)
```json
{
  "session_id": "session_live_abc123",
  "segments": [
    {
      "text": "Some words being spoken right now",
      "speaker": "SPEAKER_00",
      "is_user": true,
      "start": 45.2,
      "end": 48.7
    }
  ]
}
```

---

## Testing Your Integration

### Step 1: Send Test Payload
```bash
curl -X POST https://your-n8n.com/webhook/omi-test \
  -H "Content-Type: application/json" \
  -d @test-payload.json
```

### Step 2: Check n8n Execution
- Open n8n → Your workflow → "Executions" tab
- See exactly what data was received
- Debug each node's output

### Step 3: Test with Real Omi App
- Open Omi app → Settings → Apps → Create
- Paste your webhook URL
- Record a short conversation
- Check if it appears in Letta

---

## Architecture Recommendation

```
┌─────────────────────────────────────────────────┐
│  Omi App (User 1)  →  Webhook: /omi-memory?uid=user1  │
│  Omi App (User 2)  →  Webhook: /omi-memory?uid=user2  │
│  Omi App (User 3)  →  Webhook: /omi-memory?uid=user3  │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│              n8n Workflow                       │
│  1. Extract uid from query parameter           │
│  2. Skip realtime/discarded payloads           │
│  3. Build transcript from segments             │
│  4. Route to correct Letta agent by uid        │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│         Letta Agent (per user)                  │
│  - Route by uid to user-specific agents        │
│  - OR use single agent with uid context        │
└─────────────────────────────────────────────────┘
```

---

## Need More Info?

📖 See `OMI_PAYLOAD_DOCUMENTATION.md` for:
- Complete field descriptions
- All available categories
- Action items structure
- Photo data format
- Geolocation fields

🔧 See workflow files for:
- Working examples
- Error handling patterns
- Multi-agent routing
- Response formatting

💬 Questions?
- Omi Discord: http://discord.omi.me
- Letta Discord: https://discord.gg/letta
