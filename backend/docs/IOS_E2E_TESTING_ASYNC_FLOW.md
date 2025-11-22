# iOS E2E Testing - Async Flow Pattern

**Date**: November 22, 2025
**Status**: ✅ Deployed
**Commit**: `0e7bf46fa`

---

## 🎯 **Overview**

Backend test endpoints now return **realistic async processing status** instead of placeholder data, matching the production flow. iOS can poll for results using `conversation_id`, just like the live app.

---

## 📋 **New Response Format**

### **Before (Placeholder Data) ❌**

```json
{
  "agent_response": {
    "memories": [
      {
        "content": "Test memory from: I had lunch...",
        "category": "interesting",
        "_placeholder": true
      }
    ]
  }
}
```

**Problem**: iOS couldn't distinguish between real Letta data and fake placeholder data.

---

### **After (Async Processing Status) ✅**

```json
{
  "agent_response": {
    "status": "processing",
    "message": "Memory extraction submitted to Ella agents for processing",
    "conversation_id": "test_async_flow",
    "polling_endpoint": "/v3/memories?conversation_id=test_async_flow",
    "expected_completion_time_seconds": 30,
    "_debug": {
      "backend_status": "✅ Backend received request and submitted to n8n successfully",
      "flow": "Async processing - n8n will call backend callback when complete",
      "callback_endpoint": "/v1/ella/memory",
      "how_to_get_results": "Poll GET /v3/memories?conversation_id=test_async_flow"
    }
  }
}
```

**Benefit**: iOS knows the request was submitted and can poll for results.

---

## 🔄 **Testing Flow (Matches Production)**

### **Step 1: Submit Test Request**

```bash
curl -X POST https://api.ella-ai-care.com/v1/test/memory-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon",
    "conversation_id": "test_async_flow",
    "uid": "test_user_123",
    "debug": true
  }'
```

**Response**:
```json
{
  "agent_response": {
    "status": "processing",
    "conversation_id": "test_async_flow",
    "polling_endpoint": "/v3/memories?conversation_id=test_async_flow"
  }
}
```

---

### **Step 2: Wait for Processing (20-30 seconds)**

n8n processes the request asynchronously:
1. Letta extracts memories
2. n8n calls backend callback: `POST /v1/ella/memory`
3. Backend stores memories in Firestore

---

### **Step 3: Poll for Results**

```bash
curl https://api.ella-ai-care.com/v3/memories?conversation_id=test_async_flow
```

**Response (when complete)**:
```json
[
  {
    "id": "mem_123",
    "content": "User had lunch with Sarah at noon",
    "category": "interesting",
    "created_at": "2025-11-22T00:54:00Z"
  }
]
```

---

## 📱 **iOS Implementation Pattern**

### **Memory Agent Test**

```swift
func testMemoryAgent() async throws {
    // Step 1: Submit request
    let response = try await api.testMemoryAgent(
        text: "I had lunch with Sarah at noon",
        conversationId: "test_conv_\(UUID())",
        uid: testUserId,
        debug: true
    )

    // Step 2: Check status
    guard response.agent_response.status == "processing" else {
        throw TestError.unexpectedStatus
    }

    let conversationId = response.agent_response.conversation_id

    // Step 3: Poll for results (max 35 seconds)
    for i in 0..<7 {
        try await Task.sleep(nanoseconds: 5_000_000_000) // 5 seconds

        let memories = try await api.getMemories(conversationId: conversationId)

        if !memories.isEmpty {
            print("✅ Memories created: \(memories.count)")
            return
        }

        print("⏳ Waiting for memories... (\(i+1)/7)")
    }

    throw TestError.memoryNotCreated
}
```

---

### **Summary Agent Test**

```swift
func testSummaryAgent() async throws {
    // Step 1: Submit request
    let response = try await api.testSummaryAgent(
        text: "Today I had a productive day working on the backend.",
        conversationId: "test_conv_\(UUID())",
        uid: testUserId,
        debug: true
    )

    // Step 2: Check status
    guard response.agent_response.status == "processing" else {
        throw TestError.unexpectedStatus
    }

    let conversationId = response.agent_response.conversation_id

    // Step 3: Poll for results (max 35 seconds)
    for i in 0..<7 {
        try await Task.sleep(nanoseconds: 5_000_000_000) // 5 seconds

        let conversations = try await api.getConversations(conversationId: conversationId)

        if let conversation = conversations.first, conversation.status == "completed" {
            print("✅ Summary created: \(conversation.title)")
            return
        }

        print("⏳ Waiting for summary... (\(i+1)/7)")
    }

    throw TestError.summaryNotCreated
}
```

---

## 🔧 **Available Test Endpoints**

### **1. Scanner Agent** (Urgency Detection)
```
POST /v1/test/scanner-agent
```

**Response**:
```json
{
  "agent_response": {
    "status": "processing",
    "urgency_level": "unknown",
    "note": "Scanner typically responds in <5 seconds"
  }
}
```

---

### **2. Memory Agent** (Memory Extraction)
```
POST /v1/test/memory-agent
```

**Response**:
```json
{
  "agent_response": {
    "status": "processing",
    "conversation_id": "...",
    "polling_endpoint": "/v3/memories?conversation_id=..."
  }
}
```

**Poll for results**:
```
GET /v3/memories?conversation_id={conversation_id}
```

---

### **3. Summary Agent** (Daily Summaries)
```
POST /v1/test/summary-agent
```

**Response**:
```json
{
  "agent_response": {
    "status": "processing",
    "conversation_id": "...",
    "polling_endpoint": "/v1/conversations?conversation_id=..."
  }
}
```

**Poll for results**:
```
GET /v1/conversations?conversation_id={conversation_id}
```

---

## ⏱️ **Expected Timing**

| Agent | Typical Response Time | Max Wait Time |
|-------|----------------------|---------------|
| Scanner | <5 seconds | 10 seconds |
| Memory | 20-30 seconds | 35 seconds |
| Summary | 20-30 seconds | 35 seconds |

**Recommendation**: Poll every 5 seconds for up to 35 seconds (7 attempts).

---

## 🐛 **Debugging**

### **Check Backend Logs**

```bash
ssh root@100.101.168.91 "journalctl -u omi-backend -f | grep -E '(Scanner|Memory|Summary|test_user_123)'"
```

**Look for**:
- `✅ Backend received request and submitted to n8n successfully`
- `⚠️ n8n returned empty response, returning processing status`
- `✅ Memory callback received from n8n` (when callback arrives)

---

### **Check Firestore Directly**

```python
from google.cloud import firestore
db = firestore.Client()

# Check memories
memories = db.collection('users').document('test_user_123') \
    .collection('memories') \
    .where('conversation_id', '==', 'test_async_flow') \
    .stream()

for mem in memories:
    print(f"Memory: {mem.to_dict()['content']}")

# Check conversations
conv = db.collection('conversations').document('test_async_flow').get()
if conv.exists:
    print(f"Conversation: {conv.to_dict()['title']}")
```

---

### **Common Issues**

**Issue**: Poll returns empty after 35 seconds

**Possible Causes**:
1. n8n webhook not working (check backend logs)
2. Letta agent returning empty (n8n team issue)
3. Callback failed (check `/v1/ella/memory` endpoint logs)

**Solution**: Check backend logs and contact n8n/Letta team if webhooks returning empty.

---

**Issue**: iOS test shows "passed" but no data in Firestore

**Solution**: This was the OLD behavior with placeholders. New behavior returns processing status, iOS must poll for results.

---

## 📊 **Test Results Verification**

### **Success Criteria**

✅ **Backend submitted request**: `status: "processing"` returned
✅ **Polling endpoint provided**: iOS knows where to check for results
✅ **Conversation ID included**: iOS can track specific request
✅ **Results appear in Firestore**: After 20-30 seconds, data is stored
✅ **iOS can retrieve results**: Polling endpoint returns data

---

### **Example Test Log**

```
🧪 [TEST] Memory Agent Test Starting
📤 Submitting to /v1/test/memory-agent
✅ Received processing status (conversation_id: test_conv_abc123)
⏳ Polling /v3/memories?conversation_id=test_conv_abc123
⏳ Attempt 1/7: No memories yet
⏳ Attempt 2/7: No memories yet
⏳ Attempt 3/7: No memories yet
⏳ Attempt 4/7: No memories yet
⏳ Attempt 5/7: No memories yet
✅ Attempt 6/7: Found 2 memories!
🎉 TEST PASSED - Memories created successfully
```

---

## 🔗 **Related Documentation**

- **Backend Implementation**: `routers/testing.py` (lines 250-274, 349-372, 433-456)
- **Conversation ID Fix**: `docs/CONVERSATION_ID_FIX_MERGE_GUIDE.md`
- **E2E Test Suite**: `scripts/test_n8n_e2e_full_v2.py`
- **iOS Debug Flags**: `docs/IOS_DEBUG_FLAG_INTEGRATION.md`

---

## 📝 **Git Commits**

**This Change**: `0e7bf46fa` - "fix(testing): return async processing status instead of placeholder data"

**Related Commits**:
- `dcf05f8c4` - Added conversation_id to n8n calls
- `bcbcb839c` - Created conversation ID fix merge guide
- `4ace74eaa` - Fixed memory callback parameter bug

---

**Next**: iOS team can now implement realistic E2E tests that match production async flow!
