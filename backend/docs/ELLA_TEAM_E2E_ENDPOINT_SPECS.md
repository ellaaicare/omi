# Ella Team: n8n Endpoint Specifications for E2E Testing

**Date:** November 15, 2025
**Purpose:** Backend E2E testing endpoints need these n8n webhooks configured correctly
**Priority:** High - iOS team blocked until endpoints verified
**Contact:** Backend Developer (this doc)

---

## 🎯 Quick Summary

Backend is implementing test endpoints that will call your n8n Letta agents. We need 4 webhooks configured with specific request/response formats:

1. **Scanner Agent** - Urgency detection
2. **Memory Agent** - Memory extraction
3. **Summary Agent** - Daily summaries
4. **Chat Agent** - Conversational AI

---

## 📝 Required Endpoints

### **1. Scanner Agent**

**URL:** `https://n8n.ella-ai-care.com/webhook/scanner-agent`

**Request from Backend:**
```json
POST /webhook/scanner-agent
Content-Type: application/json

{
  "text": "I'm having chest pain",
  "conversation_id": "conv_123",
  "user_id": "uid_456"
}
```

**Expected Response:**
```json
{
  "urgency_level": "critical" | "high" | "medium" | "low" | "none",
  "detected_event": "cardiac_emergency" | "fall_emergency" | "wake_word_detected" | null,
  "explanation": "User reported chest pain, which indicates potential cardiac emergency",
  "recommended_action": "Call 911 immediately",
  "confidence": 0.95
}
```

**Requirements:**
- ✅ Synchronous response (respond within 10 seconds)
- ✅ No authentication required (or specify what's needed)
- ✅ Handle any UID (no user lookup needed for testing)

---

### **2. Memory Agent**

**URL:** `https://n8n.ella-ai-care.com/webhook/memory-agent`

**Request from Backend:**
```json
POST /webhook/memory-agent
Content-Type: application/json

{
  "text": "I had lunch with Sarah at noon and we discussed the new project",
  "conversation_id": "conv_123",
  "user_id": "uid_456"
}
```

**Expected Response:**
```json
{
  "memories": [
    {
      "content": "Had lunch with Sarah",
      "category": "social" | "work" | "health" | "interesting",
      "timestamp": "2025-11-15T12:00:00Z",
      "participants": ["Sarah"],
      "visibility": "private" | "public",
      "tags": ["lunch", "meeting"]
    },
    {
      "content": "Discussed new project with Sarah",
      "category": "work",
      "timestamp": "2025-11-15T12:00:00Z",
      "tags": ["project", "work"]
    }
  ],
  "total_memories": 2
}
```

**Requirements:**
- ✅ Synchronous response (respond within 10 seconds)
- ✅ Return empty array if no memories extracted
- ✅ Handle any UID

---

### **3. Summary Agent**

**URL:** `https://n8n.ella-ai-care.com/webhook/summary-agent`

**Request from Backend:**
```json
POST /webhook/summary-agent
Content-Type: application/json

{
  "conversation_id": "conv_123",
  "user_id": "uid_456",
  "date": "2025-11-15"
}
```

**Expected Response:**
```json
{
  "title": "Morning Health Check-In",
  "overview": "User discussed various health topics including medication schedule and upcoming doctor appointment",
  "emoji": "💊",
  "category": "health" | "work" | "social" | "general",
  "key_points": [
    "Took morning medication",
    "Scheduled doctor appointment for next week",
    "Feeling better than yesterday"
  ],
  "sentiment": "positive" | "neutral" | "negative",
  "action_items": [
    {
      "description": "Schedule follow-up with doctor",
      "due_at": "2025-11-22T14:00:00Z",
      "priority": "high"
    }
  ],
  "events": [
    {
      "title": "Doctor Appointment",
      "start": "2025-11-22T14:00:00Z",
      "duration": 30
    }
  ]
}
```

**Requirements:**
- ✅ Synchronous response (respond within 15 seconds)
- ✅ Can handle non-existent conversation_id (return empty/default summary)
- ✅ Handle any UID

---

### **4. Chat Agent**

**URL:** `https://n8n.ella-ai-care.com/webhook/chat-agent`

**IMPORTANT:** Use generic name `chat-agent`, NOT `omi-realtime` or any "omi-" prefix

#### **4A. Synchronous Mode (Preferred for Testing)**

**Request from Backend:**
```json
POST /webhook/chat-agent
Content-Type: application/json

{
  "text": "What's the weather today?",
  "uid": "uid_456",
  "session_id": "session_123"
}
```

**Expected Response (Synchronous):**
```json
{
  "text": "I don't have access to real-time weather data, but I can help you find that information...",
  "urgency_level": "low" | "medium" | "high" | "critical",
  "action_items": [],
  "context_used": ["user_location", "previous_conversations"],
  "confidence": 0.85
}
```

**Requirements:**
- ✅ Respond within 30 seconds
- ✅ Synchronous response (wait for agent processing)
- ✅ Handle any UID

#### **4B. Asynchronous Mode (Optional, for Production)**

**Request from Backend:**
```json
POST /webhook/chat-agent
Content-Type: application/json

{
  "text": "What's the weather today?",
  "uid": "uid_456",
  "session_id": "session_123",
  "callback_url": "https://api.ella-ai-care.com/v1/test/chat-callback/job_789",
  "callback_timeout": 120
}
```

**Expected Response (Immediate):**
```json
{
  "status": "processing",
  "job_id": "job_789",
  "message": "Chat agent processing request"
}
```

**Callback to Backend (when done):**
```json
POST https://api.ella-ai-care.com/v1/test/chat-callback/job_789
Content-Type: application/json

{
  "text": "I don't have access to real-time weather data...",
  "urgency_level": "low",
  "action_items": [],
  "metadata": {
    "agent_id": "chat_agent_v2",
    "model": "gpt-4-turbo",
    "confidence": 0.85,
    "processing_time_ms": 1250
  }
}
```

**Requirements:**
- ✅ If `callback_url` provided, use async mode
- ✅ If NO `callback_url`, use synchronous mode (4A)
- ✅ POST result to callback_url when complete

---

## 🔑 UID Routing & Lookup

**Question:** Do your endpoints need to look up user data via UID?

**For Testing:**
- ✅ Endpoints should accept ANY UID (even test UIDs like "test_user_123")
- ✅ If user lookup fails, still process the request with default/generic agent
- ✅ No authentication required for testing endpoints

**For Production:**
- If you need UID→agent mapping via PostgreSQL, that's fine
- Just ensure test UIDs don't crash the system
- Gracefully handle unknown UIDs

---

## 🧪 Testing Your Endpoints

You can test manually with curl before backend integration:

**Test Scanner Agent:**
```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/scanner-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I am having chest pain",
    "conversation_id": "test_123",
    "user_id": "test_user"
  }'

# Expected: {"urgency_level": "critical", "detected_event": "cardiac_emergency", ...}
```

**Test Memory Agent:**
```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/memory-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon",
    "conversation_id": "test_123",
    "user_id": "test_user"
  }'

# Expected: {"memories": [{...}], "total_memories": 1}
```

**Test Summary Agent:**
```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/summary-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "test_123",
    "user_id": "test_user",
    "date": "2025-11-15"
  }'

# Expected: {"title": "...", "overview": "...", "emoji": "...", ...}
```

**Test Chat Agent (Sync):**
```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/chat-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "What is the weather today?",
    "uid": "test_user",
    "session_id": "test_session"
  }'

# Expected: {"text": "...", "urgency_level": "low", ...}
```

**Test Chat Agent (Async):**
```bash
curl -X POST "https://n8n.ella-ai-care.com/webhook/chat-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "What is the weather today?",
    "uid": "test_user",
    "session_id": "test_session",
    "callback_url": "https://webhook.site/YOUR_TEST_URL"
  }'

# Expected: {"status": "processing", "job_id": "..."}
# Then check webhook.site for callback
```

---

## ⚠️ Common Issues to Check

### 1. **CORS Headers** (if testing from browser)
- Ensure n8n returns proper CORS headers
- Or specify if backend should handle CORS

### 2. **Authentication**
- Are webhooks public or require auth?
- If auth required: What headers/tokens do we need?

### 3. **Timeouts**
- Scanner/Memory: Should respond within 10 seconds
- Summary: Should respond within 15 seconds
- Chat (sync): Should respond within 30 seconds
- Chat (async): Callback within 120 seconds

### 4. **Error Responses**
What should we expect on errors?

**Preferred format:**
```json
{
  "error": "Agent processing failed",
  "details": "User not found in database",
  "status_code": 500
}
```

### 5. **Rate Limiting**
- Any rate limits we should be aware of?
- Backend will be making test requests frequently during development

---

## 📊 Verification Checklist

Please verify and reply:

- [ ] **Scanner Agent** - Endpoint exists, responds correctly
- [ ] **Memory Agent** - Endpoint exists, responds correctly
- [ ] **Summary Agent** - Endpoint exists, responds correctly
- [ ] **Chat Agent** - Endpoint exists at `/webhook/chat-agent` (NOT omi-realtime)
- [ ] **Chat Sync Mode** - Works without callback_url (responds immediately)
- [ ] **Chat Async Mode** - Works with callback_url (optional, nice-to-have)
- [ ] **UID Handling** - Accepts test UIDs without crashing
- [ ] **Response Times** - Meet timeout requirements above
- [ ] **Response Formats** - Match JSON schemas above
- [ ] **Error Handling** - Returns proper error JSON when things fail

---

## 📞 Questions for Ella Team

1. **Authentication** - Do we need API keys or tokens? If yes, what headers?

2. **Chat Endpoint URL** - Confirm it's `/webhook/chat-agent` (not `/omi-realtime`)

3. **Synchronous Support** - Can chat agent respond synchronously (within 30s) without callback?

4. **UID Routing** - Do you need PostgreSQL lookup for UIDs? Or can you handle test UIDs?

5. **Error Formats** - What JSON format do you return on errors?

6. **Rate Limits** - Any limits we should be aware of during testing?

7. **CORS** - Do your endpoints support CORS for browser testing?

---

## 🚀 Timeline

**Backend Developer** needs confirmation by **end of day** to start implementation.

**Please respond with:**
1. ✅ All endpoints verified and working
2. 🔧 Which endpoints need updates (with ETA)
3. ❌ Which endpoints don't exist yet (with ETA)

---

## 📝 Contact

**Questions?** Reply to this thread or contact:
- Backend Developer: (in this chat)
- PM Agent: Claude-PM
- Discord: `#backend` or `#ella-team` channel

---

**Thank you!** This will enable comprehensive E2E testing of all Letta agents from iOS. 🚀
