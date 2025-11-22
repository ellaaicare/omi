# n8n Status Messages Integration

**Date**: November 22, 2025
**Status**: ✅ Backend Ready - Waiting for n8n Deployment
**Commit**: `ce9dfc17f`

---

## 🎯 **Overview**

Backend test endpoints are now configured to:
1. **Pass through n8n status messages** when n8n sends valid JSON responses
2. **Fall back to backend processing status** when n8n returns empty (current state)
3. **Add source indicator** in debug mode so iOS knows where data came from

---

## 📊 **Current State (Before n8n Deployment)**

### **What n8n is Returning Now**

```bash
# Scanner Agent
🔍 [Scanner] n8n status: 200
🔍 [Scanner] n8n response body: ''  ← EMPTY!
⚠️  [Scanner] n8n returned empty/invalid response
```

**All three endpoints** (scanner, memory, summary) returning HTTP 200 with empty body.

### **What iOS Sees Now**

```json
{
  "agent_response": {
    "status": "processing",
    "message": "Urgency scan submitted to Ella agents for processing",
    "_debug": {
      "n8n_response_body": "(empty)",
      "source": "backend fallback (n8n returned empty)"  ← Backend fallback
    }
  }
}
```

**Backend is providing fallback status** so iOS tests don't break.

---

## 🚀 **After n8n Deployment**

### **What n8n Will Return**

**Example Scanner Response**:
```json
{
  "status": "processing",
  "urgency_level": "low",
  "message": "Conversation analyzed, no urgent issues detected",
  "job_id": "letta_job_abc123"
}
```

**Example Memory Response**:
```json
{
  "status": "processing",
  "message": "Memory extraction submitted to Letta agent cluster",
  "job_id": "letta_job_def456",
  "estimated_completion_seconds": 25
}
```

### **What iOS Will See (After n8n Deploys)**

```json
{
  "agent_response": {
    "status": "processing",
    "urgency_level": "low",
    "message": "Conversation analyzed, no urgent issues detected",
    "job_id": "letta_job_abc123",
    "_debug": {
      "n8n_returned_status": "processing",
      "source": "n8n webhook response (not backend fallback)"  ← From n8n!
    }
  }
}
```

**Backend logs will show**:
```
✅ [Scanner] n8n returned valid JSON: {'status': 'processing', 'urgency_level': 'low', ...}
📤 [Scanner] n8n status message: processing
```

---

## 🔧 **Implementation Details**

### **Scanner Agent** (`routers/testing.py` lines 250-286)

```python
# Handle n8n responses - pass through status or return processing fallback
try:
    agent_result = response.json()
    print(f"✅ [Scanner] n8n returned valid JSON: {agent_result}")

    # If n8n returns a status field, pass it through to iOS
    if "status" in agent_result:
        print(f"📤 [Scanner] n8n status message: {agent_result.get('status')}")
        if debug:
            agent_result["_debug"] = {
                "backend_status": "✅ Backend forwarding n8n status to iOS",
                "n8n_endpoint": N8N_SCANNER_AGENT,
                "n8n_returned_status": agent_result.get("status"),
                "source": "n8n webhook response (not backend fallback)"
            }
except ValueError:
    # n8n returned empty/invalid JSON - use backend fallback
    print(f"⚠️  [Scanner] n8n returned empty, returning processing status")
    agent_result = {
        "status": "processing",
        "message": "Urgency scan submitted to Ella agents for processing",
        "_debug": {
            "source": "backend fallback (n8n returned empty)"
        }
    }
```

### **Memory Agent** (`routers/testing.py` lines 359-398)

```python
# Handle n8n responses - pass through status or return processing fallback
try:
    agent_result = response.json()
    print(f"✅ [Memory] n8n returned valid JSON: {agent_result}")

    # If n8n returns a status field, pass it through to iOS
    if "status" in agent_result:
        print(f"📤 [Memory] n8n status message: {agent_result.get('status')}")
        # Ensure conversation_id is included for iOS polling
        if "conversation_id" not in agent_result:
            agent_result["conversation_id"] = conversation_id
        if debug:
            agent_result["_debug"] = {
                "source": "n8n webhook response (not backend fallback)"
            }
except ValueError:
    # Fallback...
```

### **Summary Agent** (`routers/testing.py` lines 459-498)

Same pattern as Memory Agent.

---

## 📋 **How to Test When n8n Deploys**

### **1. Test Scanner Agent**

```bash
curl -X POST https://api.ella-ai-care.com/v1/test/scanner-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I think I might be having chest pain",
    "device_type": "omi",
    "debug": true,
    "uid": "test_user_123"
  }'
```

**Look for**:
```json
{
  "_debug": {
    "source": "n8n webhook response (not backend fallback)",  ← Should see this!
    "n8n_returned_status": "processing"
  }
}
```

**Backend logs should show**:
```
✅ [Scanner] n8n returned valid JSON: {...}
📤 [Scanner] n8n status message: processing
```

---

### **2. Test Memory Agent**

```bash
curl -X POST https://api.ella-ai-care.com/v1/test/memory-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon",
    "conversation_id": "test_conv_abc123",
    "debug": true,
    "uid": "test_user_123"
  }'
```

**Expect**:
- `source: "n8n webhook response"` in debug section
- `n8n_returned_status` field showing what n8n sent
- `conversation_id` included (even if n8n forgets to include it)

---

### **3. Test Summary Agent**

```bash
curl -X POST https://api.ella-ai-care.com/v1/test/summary-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Today I had a productive day working on the backend.",
    "conversation_id": "test_conv_def456",
    "debug": true,
    "uid": "test_user_123"
  }'
```

**Expect same pattern** as Memory Agent.

---

## 🔍 **Debugging & Monitoring**

### **Check Backend Logs for n8n Responses**

```bash
ssh root@100.101.168.91 "journalctl -u omi-backend -f | grep -E '(Scanner|Memory|Summary|✅|📤)'"
```

**Before n8n deployment**:
```
⚠️  [Scanner] n8n returned empty/invalid response
⚠️  n8n memory-agent returned empty response
⚠️  n8n summary-agent returned empty response
```

**After n8n deployment**:
```
✅ [Scanner] n8n returned valid JSON: {'status': 'processing', ...}
📤 [Scanner] n8n status message: processing
✅ [Memory] n8n returned valid JSON: {'status': 'processing', ...}
📤 [Memory] n8n status message: processing
```

---

### **iOS Debug Mode Shows Source**

**iOS developers**: Check `_debug.source` field to see data origin:

```swift
if let debug = response.agent_response._debug,
   let source = debug["source"] as? String {
    if source.contains("n8n webhook") {
        print("✅ Got real n8n status message!")
    } else if source.contains("backend fallback") {
        print("⚠️  n8n returned empty, using backend fallback")
    }
}
```

---

## 📊 **n8n Status Message Examples**

Based on what n8n team will likely send:

### **Scanner Agent**

```json
{
  "status": "processing",
  "urgency_level": "high",
  "detected_event": "medical_emergency",
  "explanation": "User mentioned chest pain and shortness of breath",
  "recommended_action": "immediate_notification",
  "confidence": 0.85,
  "job_id": "letta_scanner_xyz"
}
```

### **Memory Agent**

```json
{
  "status": "processing",
  "message": "Memory extraction submitted to Letta agent cluster",
  "job_id": "letta_memory_abc",
  "estimated_completion_seconds": 25,
  "conversation_id": "conv_123"
}
```

OR (if completed synchronously):

```json
{
  "status": "completed",
  "memories": [
    {
      "content": "User had lunch with Sarah at noon",
      "category": "interesting",
      "visibility": "private",
      "tags": ["social", "meals"]
    }
  ],
  "memory_count": 1
}
```

### **Summary Agent**

```json
{
  "status": "processing",
  "message": "Summary generation submitted to Letta",
  "job_id": "letta_summary_def",
  "estimated_completion_seconds": 30,
  "conversation_id": "conv_456"
}
```

OR (if completed synchronously):

```json
{
  "status": "completed",
  "title": "Backend Development Session",
  "overview": "Productive day working on backend improvements",
  "emoji": "💻",
  "category": "work",
  "action_items": [],
  "events": []
}
```

---

## ⚡ **Automatic Passthrough Logic**

Backend automatically:
1. **Detects valid JSON** from n8n
2. **Passes entire response** to iOS (including all fields)
3. **Adds conversation_id** if n8n forgot to include it (memory/summary only)
4. **Adds debug section** showing source when `debug=true`
5. **Falls back gracefully** if n8n returns empty (current behavior)

**No iOS code changes needed** - backend handles everything!

---

## 🧪 **Testing Checklist (After n8n Deploys)**

- [ ] Scanner returns n8n status (check `_debug.source`)
- [ ] Memory returns n8n status (check `_debug.source`)
- [ ] Summary returns n8n status (check `_debug.source`)
- [ ] Backend logs show `✅ n8n returned valid JSON`
- [ ] Backend logs show `📤 n8n status message: ...`
- [ ] iOS sees `source: "n8n webhook response"`
- [ ] conversation_id included even if n8n omits it
- [ ] Fallback still works if n8n has issues

---

## 🔗 **Related Documentation**

- **Async Flow Pattern**: `docs/IOS_E2E_TESTING_ASYNC_FLOW.md`
- **conversation_id Fix**: `docs/CONVERSATION_ID_FIX_MERGE_GUIDE.md`
- **E2E Test Results**: `docs/N8N_V5_E2E_TEST_RESULTS_NOV21.md`

---

## 📝 **Git Commits**

**This Feature**: `ce9dfc17f` - "feat(testing): pass through n8n status messages to iOS when available"

**Related**:
- `0e7bf46fa` - Return async processing status instead of placeholder data
- `dcf05f8c4` - Fixed conversation_id + disabled fallback

---

## 🚦 **Current Status Summary**

| Component | Status | Notes |
|-----------|--------|-------|
| Backend Code | ✅ Ready | Deployed to production |
| n8n Webhooks | ⏳ Pending | Still returning empty (200 OK with no body) |
| iOS Integration | ✅ Ready | Will automatically see n8n messages when deployed |
| Fallback Mechanism | ✅ Working | Backend provides processing status while n8n is empty |
| Debug Visibility | ✅ Working | `_debug.source` shows data origin |

---

**Next**: Once n8n team deploys their status messages, iOS will automatically see them with NO code changes required! 🎉
