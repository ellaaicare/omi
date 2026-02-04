# PRD: n8n Migration to Standard OMI API Endpoints

**Date:** January 18, 2026
**Author:** Backend Team
**Status:** Ready for Implementation
**Priority:** High

---

## Executive Summary

The n8n workflows currently use a mix of custom Ella endpoints and direct database access patterns. This PRD defines the migration to use **standard OMI API endpoints** for all memory and conversation operations, ensuring consistency, maintainability, and proper authentication.

---

## Current State

### n8n Webhook Endpoints (Backend → n8n)

These endpoints are called BY the backend to trigger n8n processing:

| Webhook | URL | Purpose |
|---------|-----|---------|
| Summary Agent | `POST /webhook/summary-agent` | Process transcript → structured summary |
| Memory Agent | `POST /webhook/memory-agent` | Extract memories from transcript |
| Scanner Agent | `POST /webhook/scanner-agent` | Real-time transcript scanning |

**These remain unchanged.** The backend will continue to call these webhooks.

### Problem: n8n Reading/Writing Data

When n8n workflows need to **read or write** memories/conversations, they should use the standard OMI API endpoints, NOT custom endpoints or direct database calls.

---

## Target State: Standard OMI API Endpoints

### Base URL

```
Production: https://api.omi.me
Ella VPS:   http://100.101.168.91:8000 (Tailscale)
Public:     https://api.ella-ai-care.com
```

### Authentication

All requests require Firebase Auth token in header:

```http
Authorization: Bearer <firebase_id_token>
```

Or for service-to-service calls, use the user's UID directly via the internal endpoint pattern (see below).

---

## Memory Endpoints

### Create Memory

```http
POST /v3/memories
Content-Type: application/json
Authorization: Bearer <token>

{
  "content": "User prefers morning meetings",
  "category": "preference",
  "visibility": "private"
}
```

**Response:**
```json
{
  "id": "memory_abc123",
  "content": "User prefers morning meetings",
  "category": "preference",
  "visibility": "private",
  "created_at": "2026-01-18T12:00:00Z"
}
```

### List Memories

```http
GET /v3/memories?limit=100&offset=0
Authorization: Bearer <token>
```

**Response:**
```json
{
  "memories": [
    {
      "id": "memory_abc123",
      "content": "User prefers morning meetings",
      "category": "preference",
      "visibility": "private",
      "created_at": "2026-01-18T12:00:00Z"
    }
  ],
  "total": 1
}
```

### Get Single Memory

```http
GET /v3/memories/{memory_id}
Authorization: Bearer <token>
```

### Update Memory

```http
PATCH /v3/memories/{memory_id}
Content-Type: application/json
Authorization: Bearer <token>

{
  "content": "Updated memory content"
}
```

### Delete Memory

```http
DELETE /v3/memories/{memory_id}
Authorization: Bearer <token>
```

### Change Memory Visibility

```http
PATCH /v3/memories/{memory_id}/visibility
Content-Type: application/json
Authorization: Bearer <token>

{
  "visibility": "public"  // or "private"
}
```

---

## Conversation Endpoints

### List Conversations

```http
GET /v1/conversations?limit=50&offset=0
Authorization: Bearer <token>
```

**Response:**
```json
{
  "conversations": [
    {
      "id": "conv_xyz789",
      "title": "Meeting with John",
      "created_at": "2026-01-18T10:00:00Z",
      "structured": {
        "title": "Meeting with John",
        "overview": "Discussed project timeline",
        "emoji": "📅",
        "category": "work"
      }
    }
  ]
}
```

### Get Single Conversation

```http
GET /v1/conversations/{conversation_id}
Authorization: Bearer <token>
```

### Get Conversation with Full Transcript

```http
GET /v1/conversations/{conversation_id}?include_transcript=true
Authorization: Bearer <token>
```

### Update Conversation Title

```http
PATCH /v1/conversations/{conversation_id}/title
Content-Type: application/json
Authorization: Bearer <token>

{
  "title": "New Title"
}
```

### Update Action Items

```http
PATCH /v1/conversations/{conversation_id}/action-items
Content-Type: application/json
Authorization: Bearer <token>

{
  "action_items": [
    {
      "description": "Send follow-up email",
      "completed": false
    }
  ]
}
```

### Delete Conversation

```http
DELETE /v1/conversations/{conversation_id}
Authorization: Bearer <token>
```

### Reprocess Conversation

```http
POST /v1/conversations/{conversation_id}/reprocess
Authorization: Bearer <token>
```

---

## n8n Workflow Patterns

### Pattern 1: Memory Agent Saves Memories

When the memory agent extracts memories, it should call the API to save them:

```javascript
// In n8n HTTP Request node
const memories = extractedMemories; // from previous node

for (const memory of memories) {
  await $http.post({
    url: `${API_BASE_URL}/v3/memories`,
    headers: {
      'Authorization': `Bearer ${userToken}`,
      'Content-Type': 'application/json'
    },
    body: {
      content: memory.content,
      category: memory.category || 'general',
      visibility: 'private'
    }
  });
}
```

### Pattern 2: Summary Agent Updates Conversation

When the summary agent finishes processing, update the conversation:

```javascript
// The backend already handles this via the webhook response
// Just return the structured data in the webhook response:

return {
  status: "success",
  title: processedTitle,
  overview: processedOverview,
  emoji: selectedEmoji,
  category: determinedCategory,
  action_items: extractedActionItems,
  events: extractedEvents
};
```

### Pattern 3: Reading User Context

If n8n needs to read user's existing memories for context:

```javascript
// Fetch recent memories for context
const response = await $http.get({
  url: `${API_BASE_URL}/v3/memories?limit=50`,
  headers: {
    'Authorization': `Bearer ${userToken}`
  }
});

const existingMemories = response.data.memories;
// Use for deduplication or context enrichment
```

---

## Service-to-Service Authentication

For n8n workflows that don't have a user token, use the **internal API pattern**:

### Option A: API Key Authentication (Recommended)

```http
X-API-Key: <ella_service_key>
X-User-ID: <firebase_uid>
```

### Option B: Webhook Response Pattern

Instead of n8n calling the API directly, return data in the webhook response and let the backend handle persistence:

```javascript
// n8n webhook response
return {
  status: "success",
  memories: [
    { content: "Memory 1", category: "preference" },
    { content: "Memory 2", category: "fact" }
  ],
  // Backend will save these using the UID from the original request
};
```

**This is the current pattern and is preferred** - it avoids authentication complexity.

---

## Migration Checklist

### For n8n Team

- [ ] **Audit current workflows** - Identify any direct database calls or custom endpoints
- [ ] **Update memory writes** - Use `/v3/memories` POST endpoint OR return in webhook response
- [ ] **Update memory reads** - Use `/v3/memories` GET endpoint
- [ ] **Update conversation reads** - Use `/v1/conversations` GET endpoint
- [ ] **Remove custom endpoint usage** - Deprecated endpoints will be removed
- [ ] **Test with Ella VPS** - `http://100.101.168.91:8000`
- [ ] **Test with production** - `https://api.omi.me` (when ready)

### Deprecated Endpoints (DO NOT USE)

These custom Ella endpoints are being removed:

| Deprecated | Replacement |
|------------|-------------|
| `/v1/ella/memories` | `/v3/memories` |
| `/v1/ella/conversations` | `/v1/conversations` |
| `/v1/ella/callback/*` | Return data in webhook response |

---

## Webhook Request/Response Contracts

### Summary Agent Webhook

**Request (Backend → n8n):**
```json
{
  "uid": "firebase_uid",
  "conversation_id": "conv_123",
  "transcript": "Full transcript text...",
  "started_at": "2026-01-18T10:00:00Z",
  "language_code": "en",
  "timezone": "America/Los_Angeles"
}
```

**Response (n8n → Backend):**
```json
{
  "status": "success",
  "title": "Meeting Summary",
  "overview": "Discussed project timeline and deliverables",
  "emoji": "📅",
  "category": "work",
  "action_items": [
    {
      "description": "Send proposal by Friday",
      "completed": false
    }
  ],
  "events": [
    {
      "title": "Follow-up Call",
      "start": "2026-01-20T14:00:00Z",
      "duration": 30
    }
  ]
}
```

### Memory Agent Webhook

**Request (Backend → n8n):**
```json
{
  "uid": "firebase_uid",
  "conversation_id": "conv_123",
  "transcript": "Full transcript text..."
}
```

**Response (n8n → Backend):**
```json
{
  "status": "success",
  "memories": [
    {
      "content": "User prefers morning meetings",
      "category": "preference"
    },
    {
      "content": "User's dog is named Max",
      "category": "personal"
    }
  ]
}
```

**The backend will handle saving these memories using the UID from the request.**

---

## Testing

### Test Endpoint

```bash
# Health check
curl http://100.101.168.91:8000/v1/test/health

# Test with your UID (from .env.test)
source .env.test

# List memories
curl -H "Authorization: Bearer $TOKEN" \
  "http://100.101.168.91:8000/v3/memories"

# Create memory
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "Test memory", "category": "test"}' \
  "http://100.101.168.91:8000/v3/memories"
```

---

## Timeline

| Phase | Task | Owner | ETA |
|-------|------|-------|-----|
| 1 | Audit existing n8n workflows | n8n Team | 1 day |
| 2 | Update workflows to use standard endpoints | n8n Team | 2-3 days |
| 3 | Test on Ella VPS | Both | 1 day |
| 4 | Remove deprecated Ella endpoints | Backend | After n8n migration |
| 5 | Production deployment | Both | TBD |

---

## Questions / Support

- **Backend issues:** Check `ella/docs/` for documentation
- **API questions:** Reference `routers/memories.py` and `routers/conversations.py`
- **Authentication issues:** Contact backend team

---

**Document Version:** 1.0
**Last Updated:** January 18, 2026
