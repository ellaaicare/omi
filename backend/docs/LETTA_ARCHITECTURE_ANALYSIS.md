# Letta Integration Architecture Analysis

**Date:** November 16, 2025
**Purpose:** Analyze Letta's proposed architecture vs actual OMI conversation lifecycle
**Critical Issue:** Letta misunderstands how OMI creates and processes conversations

---

## 🚨 Critical Misunderstanding in Letta's Proposal

**Letta Assumes:** Conversations are created **turn-by-turn** (each message = new conversation)

**OMI Reality:** Conversations are **session-based** (full conversation = one object)

---

## 📊 How OMI Actually Works

### 1. Conversation Lifecycle (Actual Implementation)

```
Device Connects
     ↓
WebSocket Opens (/v4/listen)
     ↓
Audio/Transcript Streaming (continuous)
     ↓
Segments Accumulate in Memory
  - transcript_segments[] array grows
  - NO database writes yet
  - NO memory extraction yet
  - NO summary creation yet
     ↓
Session Ends (WebSocket close OR 120s timeout)
     ↓
Conversation Status = "processing"
     ↓
Backend Triggers Post-Processing (ONCE!)
  ├── get_transcript_structure() → Summary
  ├── new_memories_extractor() → Memories
  ├── save_structured_vector() → Pinecone
  └── extract_trends() → Trends
     ↓
Conversation Status = "completed"
     ↓
Saved to Firestore (single write)
     ↓
iOS App Polls → Sees New Conversation
```

**Key Points:**
- ✅ Conversation object created **once** per session
- ✅ Segments accumulate **in-memory** during session
- ✅ Post-processing triggers **once** when session ends
- ✅ Summary + memories extracted **once** from full transcript
- ❌ **NOT** turn-by-turn creation
- ❌ **NOT** incremental processing during session

---

## 🔍 Code Evidence

### WebSocket Handler (`routers/transcribe.py`)

**Session Start:**
```python
# Line 345: Create new stub conversation
stub_conversation = Conversation(
    id=new_conversation_id,
    created_at=datetime.now(timezone.utc),
    started_at=datetime.now(timezone.utc),
    status=ConversationStatus.in_progress,
    transcript_segments=[],  # ← Empty array
    source=ConversationSource.omi
)
conversations_db.upsert_conversation(uid, stub_conversation.dict())
```

**During Session (600ms loop):**
```python
# Line 858-869: Process buffered segments
async def process_segments_task():
    while True:
        await asyncio.sleep(0.6)  # ← 600ms processing interval
        if realtime_segment_buffers:
            segments = realtime_segment_buffers.copy()
            realtime_segment_buffers.clear()

            # Add segments to conversation object
            current_conversation.transcript_segments.extend(segments)

            # Update Firestore with accumulated segments
            conversations_db.update_conversation_transcript_segments(
                uid,
                current_conversation_id,
                current_conversation.transcript_segments
            )
```

**Session End:**
```python
# Line 314-326: Finalize processing
async def finalize_processing_conversations():
    processing = conversations_db.get_processing_conversations(uid)

    for conversation in processing:
        await _create_conversation(conversation)  # ← Post-processing

# Line 304: Post-processing function
def _create_conversation(conversation_data: dict):
    conversation = Conversation(**conversation_data)
    conversation.status = ConversationStatus.processing

    # THIS IS WHERE MAGIC HAPPENS (ONCE!)
    conversation = process_conversation(uid, language, conversation)
```

### Post-Processing (`utils/conversations/process_conversation.py`)

**Summary Generation (Line 133-142):**
```python
def _get_structured(...):
    # Get FULL transcript from ALL accumulated segments
    transcript_text = conversation.get_transcript(False, people=people)

    # Generate summary from FULL conversation (ONCE!)
    return get_transcript_structure(
        transcript_text,
        conversation.started_at,
        language_code,
        tz,
        photos=conversation.photos,
        uid=uid,
    )
```

**Memory Extraction (Line 279-312):**
```python
def _extract_memories(uid: str, conversation: Conversation):
    # Extract memories from FULL conversation (ONCE!)
    new_memories = new_memories_extractor(uid, conversation.transcript_segments)

    # Save all memories in batch
    memories_db.save_memories(uid, [fact.dict() for fact in parsed_memories])
```

**Process Conversation (Line 426-523):**
```python
def process_conversation(...):
    # 1. Generate summary (ONCE!)
    structured, discarded = _get_structured(...)

    # 2. Extract memories (background thread, ONCE!)
    threading.Thread(target=_extract_memories, args=(uid, conversation)).start()

    # 3. Save to Pinecone (background thread, ONCE!)
    threading.Thread(target=save_structured_vector, args=(uid, conversation)).start()

    # 4. Extract trends (background thread, ONCE!)
    threading.Thread(target=_extract_trends, args=(uid, conversation)).start()

    # 5. Save action items (background thread, ONCE!)
    threading.Thread(target=_save_action_items, args=(uid, conversation)).start()

    # 6. Mark as completed
    conversation.status = ConversationStatus.completed
    conversations_db.upsert_conversation(uid, conversation.dict())

    return conversation
```

---

## ❌ What Letta's Proposal Gets Wrong

### Letta's Assumption:
```
# n8n receives Discord message
{
  "text": "How do I take my medication?",
  "timestamp": "2025-11-16T10:00:00Z"
}
     ↓
# Post to Omi as conversation (WRONG!)
POST https://api.ella-ai-care.com/v1/conversations
{
  "transcript_segments": [{
    "text": "How do I take my medication?",
    "speaker": "USER",
    "is_user": true
  }]
}
```

**Problems:**
1. ❌ This creates a **completed** conversation with **one segment**
2. ❌ Post-processing triggers immediately (summary + memories for single message!)
3. ❌ No concept of multi-turn conversation
4. ❌ Each message = separate conversation object
5. ❌ Massive duplication and noise in database

---

## ✅ How Letta SHOULD Integrate

### Option A: Use WebSocket (Session-Based) - RECOMMENDED

**For Multi-Turn Conversations:**

```python
# Letta connects to OMI WebSocket (same as iOS app)
ws = await websocket.connect(
    "wss://api.ella-ai-care.com/v4/listen?uid=user123&language=en&codec=text"
)

# Send transcript segments as conversation progresses
# User message
await ws.send(json.dumps({
    "type": "transcript_segment",
    "text": "How do I take my medication?",
    "speaker": "USER",
    "is_user": true
}))

# Agent thinks... (Letta processes in background)

# Agent response
await ws.send(json.dumps({
    "type": "transcript_segment",
    "text": "Take your blood pressure medication at 9am daily with food.",
    "speaker": "ASSISTANT",
    "is_user": false
}))

# Continue multi-turn conversation...
# User: "What if I forget?"
# Assistant: "Set a reminder on your phone..."

# When conversation naturally ends:
await ws.close()

# OMI Backend automatically:
# 1. Finalizes conversation
# 2. Generates summary (from FULL conversation)
# 3. Extracts memories (from FULL conversation)
# 4. Saves to Firestore
# 5. iOS app shows complete conversation
```

**Advantages:**
- ✅ Natural conversation flow (multi-turn)
- ✅ Single summary for entire conversation
- ✅ Memories extracted from complete context
- ✅ Same as iOS app uses
- ✅ No duplication

**Use Cases:**
- Discord conversations
- Telegram chats
- Voice calls
- Any multi-turn interaction

---

### Option B: Use POST /v1/conversations (Single-Turn) - LIMITED USE

**For Standalone Messages:**

```python
# ONLY use this for complete, standalone messages
# Example: Daily journal entry, voice note, single question

POST /v1/conversations
{
  "started_at": "2025-11-16T10:00:00Z",
  "finished_at": "2025-11-16T10:05:00Z",
  "source": "discord",  # or "telegram", "chat", etc.
  "transcript_segments": [
    {
      "text": "Journal entry: Today I had a great day at work. Met with Sarah about the project.",
      "speaker": "USER",
      "is_user": true,
      "start": 0.0,
      "end": 300.0
    }
  ]
}
```

**When to Use:**
- ✅ Daily journal entries (standalone)
- ✅ Voice memos (complete thought)
- ✅ Single-turn Q&A
- ❌ **NOT** for chat conversations (use WebSocket!)

**Trigger:** OMI immediately runs post-processing on this completed conversation.

---

## 🤔 Your Question: Can Letta Send Small Chunks Like iOS?

**Answer: YES, exactly like iOS Edge ASR!**

### How iOS App Uses Edge ASR (Same Pattern Letta Should Use)

```dart
// iOS opens WebSocket
final ws = WebSocket.connect('wss://api.ella-ai-care.com/v4/listen?uid=...');

// As user speaks, iOS sends transcript segments
ws.send(jsonEncode({
  'type': 'transcript_segment',
  'text': 'Hello how are you',
  'speaker': 'USER',
  'is_user': true,
  'asr_provider': 'apple_speech'
}));

// Backend accumulates segments in memory
// NO database write yet
// NO summary generation yet

// Conversation continues...
ws.send(jsonEncode({
  'type': 'transcript_segment',
  'text': 'I am having chest pain',
  'speaker': 'USER',
  'is_user': true
}));

// When conversation ends:
ws.close();

// Backend triggers post-processing (ONCE!)
// - Builds full transcript from all segments
// - Generates summary
// - Extracts memories
// - Saves to Firestore
```

**Letta Should Do Same Thing:**
```python
# Letta multi-turn conversation via WebSocket
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123&codec=text")

# User message
await ws.send(json.dumps({
    "type": "transcript_segment",
    "text": "How do I take my medication?",
    "speaker": "USER",
    "is_user": true
}))

# Letta processes, generates response

# Agent message
await ws.send(json.dumps({
    "type": "transcript_segment",
    "text": "Take your blood pressure medication at 9am with food",
    "speaker": "ASSISTANT",
    "is_user": false
}))

# Continue conversation...
# When done:
await ws.close()

# Backend auto-processes FULL conversation
```

---

## 🔧 Is Memory/Summary Creation an iOS Function?

**NO! It's 100% backend!**

### What iOS Does:
- ✅ Send audio OR text segments via WebSocket
- ✅ Close WebSocket when done
- ✅ Poll `GET /v1/conversations` to see completed conversations
- ❌ **NOT** create summaries
- ❌ **NOT** extract memories
- ❌ **NOT** trigger post-processing

### What Backend Does (Automatically):
- ✅ Accumulate segments during session
- ✅ Detect session end (WebSocket close or timeout)
- ✅ Generate summary from full transcript
- ✅ Extract memories from full transcript
- ✅ Save to Firestore
- ✅ Index in Pinecone
- ✅ Notify iOS via polling/webhooks

**Evidence:**
```python
# Line 484: Backend triggers memory extraction (NOT iOS!)
threading.Thread(target=_extract_memories, args=(uid, conversation)).start()

# Line 500-507: Backend marks conversation as completed
conversation.status = ConversationStatus.completed
conversations_db.upsert_conversation(uid, conversation_dict)
```

**iOS just polls:**
```dart
// iOS polls every 30 seconds
final response = await http.get('$baseUrl/v1/conversations?limit=10');
final conversations = jsonDecode(response.body);
// Display in UI
```

---

## 🏗️ Correct Letta Architecture

### For Discord/Telegram/Chat (Multi-Turn)

```
Discord Message → n8n Webhook
     ↓
n8n Opens WebSocket to OMI
     ↓
Send User Message as Segment
     ↓
n8n Calls Letta Agent
     ↓
Get Agent Response
     ↓
Send Agent Response as Segment
     ↓
Continue Multi-Turn Conversation
     ↓
Conversation Ends → Close WebSocket
     ↓
OMI Backend Auto-Processes:
  - Generate summary (ONCE, from full conversation)
  - Extract memories (ONCE, from full conversation)
  - Save to Firestore
     ↓
User Sees in iOS App
```

### Data Flow Diagram

```
┌─────────────┐
│   Discord   │
│   Message   │
└──────┬──────┘
       │
       ▼
┌─────────────────────┐
│   n8n Webhook       │
│   (Orchestrator)    │
└──────┬──────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  OMI WebSocket /v4/listen            │
│  (Session-based, accumulates)        │
│                                      │
│  Segments Buffer:                    │
│  [                                   │
│    {text: "User msg 1", ...},        │
│    {text: "Agent reply 1", ...},     │
│    {text: "User msg 2", ...},        │
│    {text: "Agent reply 2", ...}      │
│  ]                                   │
└──────┬───────────────────────────────┘
       │
       │ (WebSocket closes)
       ▼
┌──────────────────────────────────────┐
│  Post-Processing (ONCE!)             │
│                                      │
│  get_transcript_structure()          │
│    → Summary from FULL conversation  │
│                                      │
│  new_memories_extractor()            │
│    → Memories from FULL conversation │
│                                      │
│  save_structured_vector()            │
│    → Pinecone embedding              │
└──────┬───────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  Firestore Save (ONCE!)              │
│                                      │
│  Conversation:                       │
│    - Full transcript                 │
│    - Summary (title, overview)       │
│    - Status: "completed"             │
│                                      │
│  Memories (separate collection):     │
│    - Extracted facts                 │
│    - Tags, categories                │
└──────┬───────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────┐
│  iOS App Polls GET /v1/conversations │
│  Sees new completed conversation     │
└──────────────────────────────────────┘
```

---

## 🎯 Recommendations for Letta Team

### 1. Use WebSocket for Multi-Turn Conversations

**Why:**
- Matches iOS app behavior
- Natural conversation flow
- Single summary per conversation
- Context-aware memory extraction

**Implementation:**
```python
class OMIWebSocketClient:
    async def start_conversation(self, uid: str):
        self.ws = await websocket.connect(
            f"wss://api.ella-ai-care.com/v4/listen?uid={uid}&language=en&codec=text"
        )

    async def send_message(self, text: str, is_user: bool):
        await self.ws.send(json.dumps({
            "type": "transcript_segment",
            "text": text,
            "speaker": "USER" if is_user else "ASSISTANT",
            "is_user": is_user
        }))

    async def end_conversation(self):
        await self.ws.close()
        # OMI auto-processes now!
```

### 2. Keep Letta Stateless (Good Idea!)

**Store in Letta:**
- ✅ Agent configs
- ✅ System prompts
- ✅ Memory block templates
- ❌ **NOT** conversations
- ❌ **NOT** memories
- ❌ **NOT** transcripts

**Query from OMI:**
```python
# Get recent memories
memories = await omi_api.get(f"/v1/letta/memories/{uid}?limit=50&days=7")

# Get recent conversations
conversations = await omi_api.get(f"/v1/conversations?limit=10")

# Search conversations
results = await omi_api.post(f"/v1/conversations/search", {
    "query": "medication schedule",
    "per_page": 5
})
```

### 3. n8n Orchestrates (Good Idea!)

**Flow:**
```
1. Discord message arrives
2. n8n opens OMI WebSocket
3. n8n sends user message to OMI
4. n8n queries Letta for agent config
5. n8n queries OMI for user context (memories)
6. n8n constructs prompt with context
7. n8n calls LLM (OpenAI/Groq)
8. n8n sends agent response to OMI
9. Conversation continues...
10. When done, n8n closes OMI WebSocket
11. OMI auto-processes conversation
12. User sees complete conversation in iOS app
```

### 4. One Conversation = One Session

**Examples:**

**Good (Session-based):**
```
Conversation 1: Discord thread about medication (10 messages)
  - Summary: "Discussed medication schedule and reminders"
  - Memories: ["Takes BP med at 9am", "Prefers morning routine"]

Conversation 2: Voice call with doctor (30 turns)
  - Summary: "Doctor consultation about chest pain"
  - Memories: ["Prescribed new medication", "Follow-up in 2 weeks"]
```

**Bad (Turn-by-turn, what Letta proposed):**
```
Conversation 1: "How do I take my medication?"
  - Summary: "User asked about medication" (not useful!)
  - Memories: [] (no context!)

Conversation 2: "Take your BP med at 9am"
  - Summary: "Medication instructions provided" (fragment!)
  - Memories: [] (no user context!)

Conversation 3: "What if I forget?"
  - Summary: "User asked about forgetting" (disconnected!)
```

---

## 📋 Implementation Checklist for Letta

**Phase 1: WebSocket Integration**
- [ ] Implement OMI WebSocket client
- [ ] Test sending transcript segments
- [ ] Verify conversation accumulation
- [ ] Test session finalization (WebSocket close)
- [ ] Confirm auto-processing triggers

**Phase 2: Multi-Turn Support**
- [ ] Build conversation state management in n8n
- [ ] Track user/agent messages during session
- [ ] Implement session end detection
- [ ] Test with Discord multi-message thread

**Phase 3: Context Retrieval**
- [ ] Implement OMI API client for memories
- [ ] Implement conversation search
- [ ] Build context construction for LLM prompts
- [ ] Test with real user data

**Phase 4: Testing**
- [ ] Test: Discord multi-turn → OMI WebSocket → Single conversation
- [ ] Verify: Summary generated from full conversation
- [ ] Verify: Memories extracted from full conversation
- [ ] Verify: iOS app shows complete conversation
- [ ] Verify: No duplication in database

---

## 🚀 Summary

**Letta's Core Insight is CORRECT:**
✅ OMI should be single source of truth
✅ Letta should be stateless
✅ n8n should orchestrate

**But Implementation Needs Fixing:**
❌ **DON'T** create conversation per message
❌ **DON'T** POST /v1/conversations for each turn
✅ **DO** use WebSocket for multi-turn conversations
✅ **DO** let OMI auto-process when session ends
✅ **DO** match iOS app behavior

**Critical Understanding:**
> OMI conversations are **session-based**, not **message-based**.
> Post-processing happens **once per session**, not **once per message**.
> Summary and memory extraction use **full conversation context**, not **individual messages**.

---

**Created:** November 16, 2025
**For:** Letta Development Team
**Priority:** Critical - Fundamental architecture decision
