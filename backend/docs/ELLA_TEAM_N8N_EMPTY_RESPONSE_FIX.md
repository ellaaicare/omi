# n8n Webhooks Returning Empty Responses - Fix Required

**Date:** November 16, 2025
**Status:** 🔴 BLOCKING iOS E2E Testing
**Issue:** All n8n webhooks return `HTTP 200 OK` with **empty body**

---

## ✅ Backend Already Adapted to Your Format

Backend is sending **EXACTLY** what you specified in your format doc:

### Scanner Agent (Example Request from Backend):
```json
POST https://n8n.ella-ai-care.com/webhook/scanner-agent
Content-Type: application/json

{
  "uid": "test_user_123",
  "segments": [
    {
      "text": "I am having chest pain",
      "speaker": "SPEAKER_00",
      "speakerId": 0,
      "is_user": true,
      "start": 0.0,
      "end": 0.0
    }
  ]
}
```

### What n8n Returns:
```
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 0

(empty body)
```

**Expected:** JSON response like `{"urgency_level": "critical", ...}`
**Actual:** Nothing (0 bytes)

---

## 🔧 One-Liner for Ella Team

**Your n8n workflows are missing "Respond to Webhook" nodes with configured response bodies - check that each workflow (scanner-agent, memory-agent, summary-agent) ends with "Respond to Webhook" node that returns JSON, and workflows are activated.**

---

## 🧪 Test Your Webhooks Directly

```bash
# Scanner Agent
curl -X POST https://n8n.ella-ai-care.com/webhook/scanner-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test_user",
    "segments": [{"text": "I have chest pain", "is_user": true, "speaker": "SPEAKER_00", "speakerId": 0, "start": 0.0, "end": 0.0}]
  }'

# Expected: JSON with urgency_level, detected_event, etc.
# Actual: (empty)
```

```bash
# Memory Agent
curl -X POST https://n8n.ella-ai-care.com/webhook/memory-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test_user",
    "segments": [{"text": "I had lunch with Sarah", "is_user": true, "speaker": "SPEAKER_00", "speakerId": 0, "start": 0.0, "end": 0.0}],
    "structured": {"title": "Test", "overview": "Test conversation"}
  }'

# Expected: JSON with memories array
# Actual: (empty)
```

```bash
# Summary Agent
curl -X POST https://n8n.ella-ai-care.com/webhook/summary-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test_user",
    "transcript": "I had a great day today",
    "started_at": "2025-11-16T00:00:00Z"
  }'

# Expected: JSON with title, overview, emoji, etc.
# Actual: (empty)
```

---

## 📋 n8n Workflow Checklist

For **each webhook** (scanner-agent, memory-agent, summary-agent):

- [ ] Workflow exists and is **activated** (green toggle ON)
- [ ] Webhook node configured with correct path
- [ ] Letta agent call node configured correctly
- [ ] **"Respond to Webhook" node exists** at end of workflow
- [ ] Response configured in "Respond to Webhook" node
- [ ] Test with curl command above - should return JSON

---

## 🎯 What Backend Needs Back

### Scanner Agent Response:
```json
{
  "urgency_level": "critical" | "high" | "medium" | "low" | "none",
  "detected_event": "cardiac_emergency" | "fall_emergency" | "wake_word_detected" | "none",
  "explanation": "User reported chest pain indicating potential cardiac emergency",
  "recommended_action": "Call 911 immediately",
  "confidence": 0.95
}
```

### Memory Agent Response:
```json
{
  "memories": [
    {
      "content": "[2025-11-16 12:00] 👥 Had lunch with Sarah",
      "category": "social",
      "visibility": "private",
      "tags": ["lunch", "social"]
    }
  ],
  "memory_count": 1
}
```

### Summary Agent Response:
```json
{
  "title": "Morning Health Check-In",
  "overview": "User discussed health topics and medication routine",
  "emoji": "💊",
  "category": "health",
  "action_items": [],
  "events": []
}
```

---

## ⏱️ Current Status

**Backend:** ✅ Deployed, sending correct format, waiting for n8n responses
**n8n Webhooks:** ⚠️ Returning 200 OK but empty body
**iOS App:** ⚠️ Seeing placeholders (can test UI but not real agent responses)

**Blocking:** iOS cannot test real agent functionality until n8n webhooks return data

---

## 🚀 Once Fixed

When n8n starts returning JSON responses:
1. Backend will automatically use real data (placeholder flag disappears)
2. iOS will see actual agent responses
3. E2E testing pipeline complete

**No backend changes needed** - backend is already ready and waiting!

---

**Created:** November 16, 2025, 01:46 UTC
**For:** Ella n8n Team
**Priority:** High - Blocking iOS E2E testing
