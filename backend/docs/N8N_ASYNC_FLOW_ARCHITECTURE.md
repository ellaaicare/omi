# N8N Async Flow Architecture - Memory & Summary Agents

**Date**: November 20, 2025
**Status**: Backend supports BOTH synchronous and asynchronous flows
**Endpoints**: Memory Agent, Summary Agent

---

## 🔄 Current Architecture: TWO Flow Options

### **Option 1: SYNCHRONOUS (Current Default)**
Backend **CALLS** n8n and **WAITS** for response (blocking)

### **Option 2: ASYNCHRONOUS (Available, Not Default)**
n8n **CALLS** backend callbacks (fire-and-forget, non-blocking)

Both options are **already implemented** in backend. n8n team can choose which to use.

---

## 📊 Flow Comparison

### **Synchronous Flow** (Current Default)

```
Conversation Ends
      ↓
Backend → POST https://n8n.ella-ai-care.com/webhook/summary-agent
      |     (uid, transcript, started_at, language_code)
      |
      | [BLOCKS 30 seconds waiting for response]
      |
      ← n8n returns: {"title": "...", "overview": "...", "emoji": "..."}
      ↓
Backend stores summary in Firestore
      ↓
iOS app polls GET /v1/conversations → sees summary immediately
```

**Code Location**: `utils/llm/conversation_processing.py` lines 376-400

**Implementation**:
```python
response = requests.post(
    "https://n8n.ella-ai-care.com/webhook/summary-agent",
    json={
        "uid": uid,
        "transcript": transcript,
        "started_at": started_at.isoformat(),
        "language_code": language_code,
    },
    timeout=30  # BLOCKS for 30 seconds
)

if response.status_code == 200:
    summary_data = response.json()
    structured = Structured(
        title=summary_data['title'],
        overview=summary_data['overview'],
        emoji=summary_data['emoji'],
        category=summary_data['category'],
        action_items=summary_data.get('action_items', []),
        events=summary_data.get('events', [])
    )
    # Store immediately in Firestore
    conversations_db.update_conversation(uid, conversation_id, {"structured": structured.dict()})
```

**Pros**:
- ✅ Simple: Backend knows summary is ready immediately
- ✅ No additional callback handling needed
- ✅ Atomic: Summary stored before function returns
- ✅ iOS app sees summary on first poll

**Cons**:
- ❌ **Blocks** for up to 30 seconds (ties up backend worker)
- ❌ If n8n slow, backend waits (affects other requests)
- ❌ Timeout failures leave conversation without summary
- ❌ Not scalable for heavy load

---

### **Asynchronous Flow** (Alternative Option)

```
Conversation Ends
      ↓
Backend creates conversation with status="processing"
      ↓
Backend → POST https://n8n.ella-ai-care.com/webhook/summary-agent
      |     (uid, conversation_id, transcript, started_at)
      |
      | [RETURNS IMMEDIATELY - fire and forget]
      |
Backend function completes (no blocking) ✅
      ↓
iOS app polls GET /v1/conversations → sees status="processing"
      ↓
      ... (n8n processing in background) ...
      ↓
n8n → POST https://api.ella-ai-care.com/v1/ella/conversation  ← CALLBACK
      {
        "uid": "...",
        "conversation_id": "...",
        "structured": {
          "title": "...",
          "overview": "...",
          "emoji": "...",
          "category": "...",
          "action_items": [...],
          "events": [...]
        }
      }
      ↓
Backend receives callback, updates Firestore
      ↓
Backend sets status="completed"
      ↓
iOS app polls again → sees status="completed" with full summary
```

**Code Location**:
- Summary callback: `routers/ella.py` lines 191-280
- Memory callback: `routers/ella.py` lines 121-189

**Callback Endpoint Schema**:
```
POST https://api.ella-ai-care.com/v1/ella/conversation
Content-Type: application/json

{
  "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "conversation_id": "373c3ee8-013b-4dd4-8e8f-54664fe56630",
  "structured": {
    "title": "Morning Health Check-In",
    "overview": "User discussed morning routine and medication schedule",
    "emoji": "💊",
    "category": "health",
    "action_items": [
      {
        "description": "Schedule doctor appointment",
        "due_at": "2025-11-15T10:00:00Z"
      }
    ],
    "events": []
  }
}
```

**Response**:
```json
{
  "status": "success",
  "conversation_id": "373c3ee8-013b-4dd4-8e8f-54664fe56630",
  "message": "Updated conversation summary for 5aGC5YE9BnhcSoTxxtT4ar6ILQy2"
}
```

**Pros**:
- ✅ **Non-blocking**: Backend returns immediately
- ✅ **Scalable**: Can handle thousands of concurrent conversations
- ✅ **Resilient**: n8n failures don't affect backend
- ✅ **Decoupled**: n8n and backend can scale independently

**Cons**:
- ❌ More complex: Requires callback endpoint + status tracking
- ❌ iOS app must poll multiple times until status="completed"
- ❌ Race conditions possible (conversation deleted before callback arrives)
- ❌ No immediate feedback on success/failure

---

## 🔧 How to Switch to Async Flow

### **Step 1: Modify Backend to Use Async (Fire-and-Forget)**

**File**: `utils/llm/conversation_processing.py`

**Current (Synchronous)**:
```python
# Lines 376-400
response = requests.post(
    "https://n8n.ella-ai-care.com/webhook/summary-agent",
    json={"uid": uid, "transcript": transcript, ...},
    timeout=30  # BLOCKS
)

if response.status_code == 200:
    summary_data = response.json()
    # Store summary immediately
    conversations_db.update_conversation(uid, conversation_id, {"structured": summary_data})
else:
    # Fall back to local LLM
    local_llm_fallback()
```

**Modified (Asynchronous)**:
```python
# Set conversation to processing status
conversations_db.update_conversation(
    uid,
    conversation_id,
    {"status": "processing", "processing_started_at": datetime.utcnow().isoformat()}
)

# Fire-and-forget call to n8n (no blocking)
try:
    requests.post(
        "https://n8n.ella-ai-care.com/webhook/summary-agent",
        json={
            "uid": uid,
            "conversation_id": conversation_id,  # Include ID for callback
            "transcript": transcript,
            "started_at": started_at.isoformat(),
            "callback_url": "https://api.ella-ai-care.com/v1/ella/conversation"  # Tell n8n where to send result
        },
        timeout=2  # Short timeout (just for connection, not processing)
    )
    print(f"✅ Queued summary generation for conversation {conversation_id}")
except Exception as e:
    print(f"⚠️  Failed to queue summary: {e}")
    # Optionally: Mark conversation as failed or retry
    conversations_db.update_conversation(uid, conversation_id, {"status": "failed"})
```

**Key Changes**:
1. ✅ Set `status="processing"` before calling n8n
2. ✅ Include `conversation_id` in payload (for callback matching)
3. ✅ Include `callback_url` (tell n8n where to send result)
4. ✅ Use short timeout (2s, not 30s)
5. ✅ Don't wait for response, return immediately

---

### **Step 2: Add Status Field to Conversation Model**

**File**: `models/conversation.py`

**Current**:
```python
class Conversation(BaseModel):
    id: str
    structured: Optional[Structured] = None
    # ... other fields
```

**Modified**:
```python
class ConversationStatus(str, Enum):
    in_progress = 'in_progress'
    processing = 'processing'     # NEW: Waiting for n8n callback
    completed = 'completed'
    failed = 'failed'             # NEW: n8n processing failed

class Conversation(BaseModel):
    id: str
    status: ConversationStatus = ConversationStatus.in_progress
    structured: Optional[Structured] = None
    processing_started_at: Optional[datetime] = None  # NEW: When n8n job started
    # ... other fields
```

---

### **Step 3: Handle Callback in ella.py (Already Implemented)**

**File**: `routers/ella.py` lines 191-280

**Callback Handler** (already exists):
```python
@router.post("/v1/ella/conversation", tags=["ella"])
async def ella_conversation_callback(request: EllaConversationCallback):
    """
    n8n calls this AFTER processing summary
    """
    print(f"📝 Ella Conversation Callback - UID: {request.uid}, ID: {request.conversation_id}")

    # Convert callback data to Structured model
    structured = Structured(
        title=request.structured.title,
        overview=request.structured.overview,
        emoji=request.structured.emoji,
        category=CategoryEnum(request.structured.category),
        action_items=[...],
        events=[...]
    )

    # Update conversation in Firestore
    conversations_db.update_conversation(
        request.uid,
        request.conversation_id,
        {
            "structured": structured.dict(),
            "status": "completed"  # Mark as done
        }
    )

    return {"status": "success", "conversation_id": request.conversation_id}
```

**This endpoint already exists** - no changes needed!

---

### **Step 4: iOS App Polling Logic**

**Current iOS Behavior**:
```swift
// Poll once
let conversations = await fetchConversations()
// Expect summary to be present immediately
```

**Modified iOS Behavior (for async)**:
```swift
// Poll with status checking
func pollConversationUntilComplete(conversationId: String) async -> Conversation? {
    var attempts = 0
    let maxAttempts = 30  // 30 seconds (1s polling interval)

    while attempts < maxAttempts {
        let conversation = await fetchConversation(id: conversationId)

        switch conversation.status {
        case .completed:
            // Summary is ready
            return conversation

        case .processing:
            // Still waiting for n8n callback
            try await Task.sleep(nanoseconds: 1_000_000_000)  // 1 second
            attempts += 1

        case .failed:
            // n8n processing failed
            print("Summary generation failed")
            return nil

        default:
            break
        }
    }

    // Timeout after 30 seconds
    print("Timeout waiting for summary")
    return nil
}
```

---

## 🔄 Memory Agent: Same Two Options

### **Synchronous (Current)**

```python
# utils/llm/memories.py lines 72-100
response = requests.post(
    "https://n8n.ella-ai-care.com/webhook/memory-agent",
    json={"uid": uid, "segments": segments_data},
    timeout=120  # BLOCKS for 120 seconds
)

if response.status_code == 200:
    memories_data = response.json()['memories']
    for memory_data in memories_data:
        # Store memory immediately
        memories_db.create_memory(uid, memory_dict)
```

**Blocking time**: Up to 120 seconds

---

### **Asynchronous (Fire-and-Forget)**

**Modified Code**:
```python
# Fire-and-forget
requests.post(
    "https://n8n.ella-ai-care.com/webhook/memory-agent",
    json={
        "uid": uid,
        "conversation_id": conversation_id,
        "segments": segments_data,
        "callback_url": "https://api.ella-ai-care.com/v1/ella/memory"
    },
    timeout=2  # Just for connection
)

# Return immediately (no blocking)
print(f"✅ Queued memory extraction for conversation {conversation_id}")
```

**Callback Endpoint** (already exists): `POST /v1/ella/memory`

**Callback Schema**:
```json
POST https://api.ella-ai-care.com/v1/ella/memory

{
  "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "conversation_id": "373c3ee8-013b-4dd4-8e8f-54664fe56630",
  "memories": [
    {
      "content": "User takes blood pressure medication daily at 8am",
      "category": "system",
      "visibility": "private",
      "tags": ["medication", "health"]
    },
    {
      "content": "User's daughter Sarah is starting college next month",
      "category": "interesting",
      "visibility": "private",
      "tags": ["sarah", "daughter", "college", "family"]
    }
  ]
}
```

**Callback Handler** (already exists):
```python
# routers/ella.py lines 121-189
@router.post("/v1/ella/memory", tags=["ella"])
async def ella_memory_callback(request: EllaMemoryCallback):
    """n8n calls this AFTER extracting memories"""
    for memory_data in request.memories:
        memory_db = MemoryDB.from_memory(memory, uid=request.uid, conversation_id=request.conversation_id)
        memories_db.create_memory(request.uid, memory_db.dict())

    return {"status": "success", "count": len(request.memories)}
```

---

## 🎯 Recommendation: Which Flow to Use?

### **Use SYNCHRONOUS if:**
- ✅ Low traffic (< 100 conversations/hour)
- ✅ Want simple implementation
- ✅ iOS app expects immediate results
- ✅ n8n agents are fast (< 5 seconds)

### **Use ASYNCHRONOUS if:**
- ✅ High traffic (> 100 conversations/hour)
- ✅ Want scalability and resilience
- ✅ n8n agents are slow (> 10 seconds)
- ✅ iOS app can handle polling
- ✅ Want decoupled architecture

---

## 📝 Implementation Checklist (Switch to Async)

### **Backend Changes** (2-3 hours):
- [ ] Modify `utils/llm/conversation_processing.py` to use fire-and-forget
- [ ] Modify `utils/llm/memories.py` to use fire-and-forget
- [ ] Add `status` field to Conversation model
- [ ] Add `processing_started_at` timestamp
- [ ] Add timeout/retry logic for failed jobs
- [ ] Test callback endpoints (`/v1/ella/conversation`, `/v1/ella/memory`)

### **n8n Changes** (1-2 hours):
- [ ] Add `conversation_id` parameter to summary workflow
- [ ] Add `conversation_id` parameter to memory workflow
- [ ] Add callback HTTP nodes at end of workflows
- [ ] Configure callback URLs (from request or hardcoded)
- [ ] Test callback delivery

### **iOS Changes** (1-2 hours):
- [ ] Add status field to Conversation model
- [ ] Implement polling logic for `status="processing"`
- [ ] Add UI indicator for "Processing summary..."
- [ ] Handle timeout after 30 seconds
- [ ] Handle failed status

### **Testing**:
- [ ] Test sync → async migration (no data loss)
- [ ] Test concurrent conversations (stress test)
- [ ] Test callback failure handling
- [ ] Test iOS polling behavior
- [ ] Test timeout scenarios

---

## 🔍 Monitoring for Async Flow

### **Backend Metrics to Track**:
```python
# Conversations stuck in processing status
stuck_conversations = db.collection('conversations') \
    .where('status', '==', 'processing') \
    .where('processing_started_at', '<', datetime.utcnow() - timedelta(minutes=5)) \
    .stream()

# Count by status
processing_count = conversations.where('status', '==', 'processing').count()
completed_count = conversations.where('status', '==', 'completed').count()
failed_count = conversations.where('status', '==', 'failed').count()
```

### **Alerting Rules**:
- ⚠️ Alert if > 10 conversations stuck in "processing" for > 5 minutes
- ⚠️ Alert if callback failure rate > 5%
- ⚠️ Alert if average processing time > 30 seconds

---

## 🚀 Current Status

**Backend**: ✅ **BOTH flows already supported**
- Synchronous: Actively used (default)
- Asynchronous: Callback endpoints implemented, not yet used

**n8n**: ⚠️ **Only synchronous implemented**
- Summary agent returns response immediately
- Memory agent returns response immediately
- No callbacks sent to backend

**iOS App**: ⚠️ **Expects synchronous**
- Polls once, expects summary immediately
- No handling for `status="processing"`

---

## 📊 Performance Comparison

| Metric | Synchronous | Asynchronous |
|--------|-------------|--------------|
| Backend blocking time | 30-120s | 0s (fire-and-forget) |
| Time to iOS sees summary | Immediate | 1-30s (polling) |
| Scalability | Poor (blocks workers) | Excellent (non-blocking) |
| Complexity | Low | Medium |
| Failure handling | Timeout fallback | Retry + status tracking |
| Production-ready | ✅ Yes (current) | ⚠️ Needs iOS changes |

---

## 🎓 Summary

**Backend Already Supports Both Flows**:
1. ✅ **Synchronous** (current): Backend calls n8n, waits for response
2. ✅ **Asynchronous** (available): n8n calls backend callbacks

**Callback Endpoints** (already implemented):
- `POST /v1/ella/conversation` - Summary callback
- `POST /v1/ella/memory` - Memory callback
- `POST /v1/ella/notification` - Notification callback

**To Switch to Async**:
1. Backend: Change `requests.post()` to fire-and-forget (remove blocking)
2. Backend: Add `status="processing"` field to conversations
3. n8n: Add callback HTTP nodes at end of workflows
4. iOS: Implement polling until `status="completed"`

**Current Recommendation**:
- **Stay synchronous** for MVP (simpler, works today)
- **Switch to async** when scaling up (better performance)

---

**Last Updated**: November 20, 2025
**Status**: Backend ready for both flows, n8n/iOS need updates for async
