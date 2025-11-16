# Letta Team Questions - Answered

**Date:** November 16, 2025
**Purpose:** Answer Letta team's questions about OMI conversation processing
**Source:** OMI backend codebase analysis

---

## Question 1: Agent Responses in Transcripts?

**Q:** When we send `speaker_id=1, is_user=false`, does OMI include these in summaries? Do memories get extracted from agent responses too? Or only user segments?

**Answer:** ✅ **YES - Agent responses ARE included in everything!**

### Code Evidence:

**Transcript Generation (models/transcript_segment.py, lines 41-58):**
```python
@staticmethod
def segments_as_string(segments, include_timestamps=False, user_name: str = None, people: List[Person] = None):
    transcript = ''
    for segment in segments:
        segment_text = segment.text.strip()
        speaker_name = user_name
        if not segment.is_user:  # ← Agent responses
            speaker_name = f'Speaker {segment.speaker_id}'
        transcript += f'{speaker_name}: {segment_text}\n\n'

    return transcript.strip()
```

**Key Points:**
- ✅ ALL segments are included (no filtering by `is_user`)
- ✅ `is_user=true` → Labeled as "User"
- ✅ `is_user=false` → Labeled as "Speaker {speaker_id}"
- ✅ Full transcript sent to summary generation
- ✅ Full transcript sent to memory extraction

**Summary Generation (utils/llm/conversation_processing.py, line 358):**
```python
def get_transcript_structure(transcript: str, ...):
    # transcript parameter is the FULL conversation
    # Including both user and agent messages
    context_parts.append(f"Transcript: ```{transcript.strip()}```")

    # Send full context to LLM for summarization
    full_context = "\n\n".join(context_parts)
```

**Memory Extraction (utils/llm/memories.py, line 54):**
```python
def new_memories_extractor(uid: str, segments: List[TranscriptSegment], ...):
    # Convert ALL segments to string (user + agent)
    content = TranscriptSegment.segments_as_string(segments, user_name=user_name, people=people)

    # Extract memories from full conversation
    # Includes agent responses!
```

### Example:

```python
# Letta sends this via WebSocket:
[
    {"text": "How do I take my medication?", "is_user": true, "speaker_id": 0},
    {"text": "Take your BP medication at 9am with food", "is_user": false, "speaker_id": 1},
    {"text": "What if I forget?", "is_user": true, "speaker_id": 0},
    {"text": "Set a reminder on your phone", "is_user": false, "speaker_id": 1}
]

# OMI generates transcript:
"""
User: How do I take my medication?

Speaker 1: Take your BP medication at 9am with food

User: What if I forget?

Speaker 1: Set a reminder on your phone
"""

# Summary generation sees: ✅ FULL transcript (all 4 messages)
# Memory extraction sees: ✅ FULL transcript (all 4 messages)
# Result:
# - Summary: "Discussed medication schedule and reminders"
# - Memories: ["Takes BP med at 9am with food", "Uses phone reminders"]
```

---

## Question 2: Selective Memory Creation?

**Q:** What triggers OMI to create a memory vs skip? Is it based on importance/novelty detection? Can we learn from this logic?

**Answer:** ✅ **LLM-based importance/novelty detection**

### How It Works:

**Memory Extraction Logic (utils/llm/memories.py, lines 45-60):**

```python
def new_memories_extractor(uid: str, segments: List[TranscriptSegment], ...):
    content = TranscriptSegment.segments_as_string(segments)

    # Skip if conversation too short (less than 5 words)
    if not content or len(content) < 25:
        return []

    # LLM determines what's worth remembering
    # (importance, novelty, user-specific facts)
```

**Two Paths (as of Nov 2025):**

### Path 1: Ella Memory Agent (n8n/Letta)
```python
# Try calling Ella's memory agent first
response = requests.post(
    "https://n8n.ella-ai-care.com/webhook/memory-agent",
    json={"uid": uid, "segments": segments_data},
    timeout=120
)

# Ella/Letta agent decides what's worth remembering
# Returns: List of memories with categories and tags
```

**Ella's Logic (in Letta agent):**
- Uses LLM with specific prompt for memory extraction
- Filters for importance, novelty, user-specific facts
- Returns 0-4 memories per conversation
- Categories: "interesting" (user-relevant), "system" (app metadata)

### Path 2: Fallback to Local LLM
```python
# If Ella unavailable, use local LLM (OpenAI/Anthropic)
messages = [
    {
        "role": "system",
        "content": extract_memories_prompt  # ← Prompt defines extraction logic
    },
    {
        "role": "user",
        "content": f"Transcript: {content}\n\nPrevious memories: {memories_str}"
    }
]

response = llm_mini.invoke(messages)
```

**Local LLM Prompt Logic:**
- Extract facts user wouldn't want to repeat
- Filter out generic/obvious information
- Focus on preferences, habits, specific details
- Avoid duplication with existing memories
- Max 4 memories per conversation

### What Gets Remembered:

**YES - Create Memory:**
- ✅ User preferences ("Prefers morning workouts")
- ✅ Specific schedules ("Takes BP med at 9am")
- ✅ Personal relationships ("Friend Sarah works at Google")
- ✅ Important events ("Doctor appointment Nov 20")
- ✅ Health conditions ("Has high blood pressure")
- ✅ Habits ("Drinks coffee before exercise")

**NO - Skip:**
- ❌ Generic statements ("Exercise is good for you")
- ❌ Already known facts (duplicates existing memories)
- ❌ Temporary information ("It's raining today")
- ❌ Questions without answers
- ❌ Small talk ("Hi, how are you?")

### Can You Learn From This?

**YES! You can reuse this logic:**

**Option A: Use Ella's Memory Agent (Recommended)**
- Already implements smart filtering
- Query via: `POST https://n8n.ella-ai-care.com/webhook/memory-agent`
- Returns: Filtered, categorized memories

**Option B: Query OMI's Existing Memories**
- Get what OMI already extracted: `GET /v3/memories?uid={uid}&limit=50`
- These are pre-filtered by importance

**Option C: Implement Your Own**
- Study OMI's prompt: `utils/prompts.py` → `extract_memories_prompt`
- Copy the filtering logic
- Adjust for your use case

---

## Question 3: Summary Generation Triggers?

**Q:** You mentioned summaries happen on WS closure (silence detection). What's the timeout? 120s? Can we customize this for Letta source?

**Answer:** ✅ **YES - Customizable timeout (default 120s, min 120s, max 4 hours)**

### Code Evidence:

**WebSocket Handler (routers/transcribe.py, lines 78-86):**
```python
async def _listen(
    websocket: WebSocket,
    uid: str,
    language: str = 'en',
    sample_rate: int = 8000,
    codec: str = 'pcm8',
    channels: int = 1,
    include_speech_profile: bool = True,
    stt_service: Optional[STTService] = None,
    conversation_timeout: int = 120,  # ← Customizable parameter!
):
```

**Timeout Validation (routers/transcribe.py, lines 379-386):**
```python
# Conversation timeout (to process the conversation after x seconds of silence)
# Max: 4h, min 2m
conversation_creation_timeout = conversation_timeout
if conversation_creation_timeout == -1:
    conversation_creation_timeout = 4 * 60 * 60  # 4 hours
if conversation_creation_timeout < 120:
    conversation_creation_timeout = 120  # Minimum 120 seconds
```

### How to Customize:

**Method 1: WebSocket Query Parameter**
```python
# Default (120 seconds)
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123")

# Custom timeout (5 minutes = 300 seconds)
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123&conversation_timeout=300")

# Long conversations (1 hour = 3600 seconds)
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123&conversation_timeout=3600")

# Max timeout (4 hours)
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123&conversation_timeout=-1")
```

**Method 2: Source-Specific Configuration (Not Yet Implemented)**

Currently, timeout is NOT customizable per `source` field. But we could add this:

```python
# Proposed enhancement (not yet implemented):
SOURCE_TIMEOUTS = {
    "omi": 120,        # Default for OMI device
    "discord": 300,    # 5 minutes for Discord threads
    "telegram": 180,   # 3 minutes for Telegram
    "letta": 600,      # 10 minutes for Letta multi-turn conversations
}

timeout = SOURCE_TIMEOUTS.get(conversation.source, 120)
```

**Would you like this feature?** We can add source-based timeout configuration.

### Summary Trigger Points:

**1. WebSocket Close (Primary)**
```python
# When client closes WebSocket:
await websocket.close()

# Backend immediately triggers:
# - Conversation status = "processing"
# - Generate summary
# - Extract memories
# - Save to Firestore
```

**2. Timeout (Secondary)**
```python
# If WebSocket stays open but idle for {conversation_timeout} seconds:
# - Backend auto-closes conversation
# - Triggers post-processing
```

**3. Manual Stop (Tertiary)**
```python
# Client can send special message to force finalization:
await websocket.send(json.dumps({"type": "stop_conversation"}))
# (Not yet implemented, but could add)
```

### Recommendation for Letta:

**Short Conversations (Discord/Telegram):**
```python
# Use default 120s timeout
ws = await websocket.connect("wss://api.ella-ai-care.com/v4/listen?uid=user123")
# Conversation auto-finalizes after 2 minutes of silence
```

**Long Conversations (Voice Calls, Therapy Sessions):**
```python
# Use extended timeout (10-30 minutes)
ws = await websocket.connect(
    "wss://api.ella-ai-care.com/v4/listen?uid=user123&conversation_timeout=1800"
)
# Conversation auto-finalizes after 30 minutes of silence
```

**Active Management (Recommended):**
```python
# Close WebSocket when conversation naturally ends
# Don't rely on timeout for finalization

# Example: Discord thread marked as resolved
if thread.is_resolved:
    await ws.close()  # ← Immediate finalization
```

---

## Question 4: Metadata Passthrough?

**Q:** If we send `letta_summary` and `letta_memories` in metadata, does OMI use these, or always regenerate its own? Can we skip OMI's processing if we already have good summary/memories?

**Answer:** ❌ **NO metadata passthrough currently - OMI always regenerates**

### Current Behavior:

**CreateConversation Model (models/conversation.py, lines 404-415):**
```python
class CreateConversation(BaseModel):
    started_at: datetime
    finished_at: datetime
    transcript_segments: List[TranscriptSegment]
    geolocation: Optional[Geolocation] = None
    photos: List[ConversationPhoto] = []
    source: ConversationSource = ConversationSource.omi
    language: Optional[str] = None
    processing_conversation_id: Optional[str] = None

    # NO fields for custom summary or memories!
```

**Processing Pipeline (utils/conversations/process_conversation.py):**
```python
def process_conversation(...):
    # ALWAYS generates new summary
    structured = get_transcript_structure(transcript, ...)

    # ALWAYS extracts new memories
    threading.Thread(target=_extract_memories, args=(uid, conversation)).start()

    # NO way to skip or provide custom summary/memories
```

### Why No Passthrough?

**Design Decision:**
- OMI owns the summarization/memory extraction quality
- Ensures consistent format across all sources
- LLM prompts tuned for OMI's specific use case
- Prevents malformed data from breaking iOS app

### Workaround Options:

### Option 1: Skip Ella Integration for Letta (Recommended)

**Current Flow:**
```
Conversation → Ella Memory Agent → Return Memories → OMI Saves
             ↓
        (If Ella fails)
             ↓
          Local LLM → Return Memories → OMI Saves
```

**Proposed for Letta:**
```
Letta Conversation → Skip Ella Call → Use Letta's Own Memories
                      ↓
                  Save Directly to OMI
```

**Implementation:**

**Step 1: Letta Generates Memories During Conversation**
```python
# While conversation is happening, Letta tracks memories
letta_memories = [
    {"content": "Takes BP medication at 9am", "category": "health", "tags": ["medication"]},
    {"content": "Prefers morning routine", "category": "lifestyle", "tags": ["routine"]}
]
```

**Step 2: After WebSocket Close, Write Memories Directly**
```python
# After conversation ends and OMI processes it:
conversation_id = await get_latest_conversation_id(uid)

# Write Letta's memories directly to OMI
for mem in letta_memories:
    await omi_api.post("/v3/memories", {
        "content": mem["content"],
        "category": mem["category"],
        "tags": mem["tags"],
        "conversation_id": conversation_id  # Link to conversation
    })
```

**Advantages:**
- ✅ Letta controls memory quality
- ✅ No duplicate processing
- ✅ Faster (no Ella roundtrip)
- ❌ Two sources of memories (OMI's + Letta's)

### Option 2: Enhance OMI to Accept Custom Summaries/Memories

**Proposed Enhancement (not yet implemented):**

```python
# New field in CreateConversation
class CreateConversation(BaseModel):
    # ... existing fields ...

    custom_structured: Optional[Structured] = None  # ← NEW
    custom_memories: Optional[List[Memory]] = None  # ← NEW
    skip_processing: bool = False  # ← NEW

# Updated processing logic
def process_conversation(...):
    if conversation.skip_processing and conversation.custom_structured:
        # Use provided summary/memories
        structured = conversation.custom_structured
        memories = conversation.custom_memories
    else:
        # Generate as usual
        structured = get_transcript_structure(...)
        memories = new_memories_extractor(...)
```

**Usage:**
```python
# Letta sends conversation with custom summary/memories
await omi_api.post("/v1/conversations", {
    "transcript_segments": [...],
    "custom_structured": {
        "title": "Medication Consultation",
        "overview": "Discussed BP medication schedule",
        "emoji": "💊",
        "category": "health"
    },
    "custom_memories": [
        {"content": "Takes BP med at 9am", "category": "health"}
    ],
    "skip_processing": true  # ← Skip OMI's LLM calls
})
```

**Advantages:**
- ✅ Single source of truth
- ✅ Letta controls quality
- ✅ Skips unnecessary processing
- ❌ Requires backend changes

**Would you like this feature?** We can implement it!

### Option 3: Hybrid Approach (Best of Both)

**Let OMI and Letta BOTH create memories, then merge:**

```python
# 1. Letta sends conversation via WebSocket
# 2. OMI auto-processes (summary + memories)
# 3. Letta writes additional memories
# 4. User sees combined memories in iOS app

# Example result in iOS app:
Memories from conversation XYZ:
  - "Takes BP medication at 9am" (extracted by OMI)
  - "Prefers morning routine" (extracted by OMI)
  - "Discussed setting phone reminders" (added by Letta)
  - "User wants daily check-ins" (added by Letta)
```

**Implementation:**
```python
# After WebSocket closes:
conversation_id = await get_latest_conversation_id(uid)

# Wait for OMI processing to complete
await asyncio.sleep(5)

# Add Letta's supplementary memories
for mem in letta_additional_memories:
    await omi_api.post("/v3/memories", {
        "content": mem["content"],
        "category": mem["category"],
        "tags": mem["tags"] + ["letta"],  # Tag as Letta-generated
        "conversation_id": conversation_id
    })
```

**Advantages:**
- ✅ Best of both worlds
- ✅ No backend changes needed
- ✅ OMI provides baseline quality
- ✅ Letta adds specialized insights
- ❌ Some duplication possible

---

## Summary Table

| Question | Answer | Customizable? |
|----------|--------|---------------|
| **Agent responses in summaries?** | ✅ YES - Always included | N/A |
| **Agent responses in memories?** | ✅ YES - Always included | N/A |
| **Memory filtering logic?** | ✅ LLM-based importance/novelty | ✅ Can use Ella agent |
| **Timeout for summary?** | ✅ Default 120s | ✅ YES (120s - 4h) |
| **Timeout per source?** | ❌ Not yet implemented | ⚠️ Can be added |
| **Metadata passthrough?** | ❌ Not supported | ⚠️ Can be added |
| **Skip OMI processing?** | ❌ Not supported | ⚠️ Can be added |

---

## Recommendations for Letta Integration

### 1. Include Agent Responses
✅ **DO** send both user and agent messages in transcript segments
✅ **DO** set `is_user=false` for agent responses
✅ **DO** use `speaker_id=1` (or higher) for agents
✅ Summaries and memories will include agent context

### 2. Memory Extraction
✅ **OPTION A:** Let OMI/Ella handle memory extraction automatically
✅ **OPTION B:** Write Letta's own memories directly via `POST /v3/memories`
✅ **OPTION C:** Hybrid - let both extract, merge results

### 3. Timeout Configuration
✅ **Short conversations:** Use default 120s timeout
✅ **Long conversations:** Increase timeout via query parameter
✅ **Active management:** Close WebSocket when conversation ends (don't rely on timeout)

### 4. Custom Summaries/Memories
⚠️ **CURRENT:** Not supported, OMI always regenerates
✅ **WORKAROUND:** Write memories after processing via `POST /v3/memories`
⚠️ **FUTURE:** Request metadata passthrough feature (we can add it!)

---

## Feature Requests from Letta Team

If you'd like us to implement:

1. **Source-based timeout configuration** - Custom timeout per source (Discord, Telegram, Letta)
2. **Metadata passthrough** - Accept custom summaries/memories, skip processing
3. **Manual stop trigger** - WebSocket message to force finalization without closing

Please let us know! We can add these features.

---

**Created:** November 16, 2025
**For:** Letta Development Team
**Contact:** OMI Backend Team
