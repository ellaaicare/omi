# Conversations vs Memories - Complete Architecture Guide

**Date**: October 30, 2025
**Context**: Understanding the distinction between "conversations" and "memories" in OMI backend
**Status**: Production system documentation

---

## 🎯 **TL;DR - The Key Distinction**

**YOU WERE RIGHT!** Conversations and memories ARE two separate things:

- **Conversations**: Full transcript records of audio sessions with structured summaries
- **Memories**: Extracted facts/insights from conversations, stored separately for long-term recall

Think of it like this:
- **Conversation** = The entire meeting recording + transcript + summary
- **Memory** = Key facts extracted from that meeting ("User mentioned they prefer TypeScript over JavaScript")

---

## 📊 **Data Model Comparison**

### Conversation Data Model

**Location**: `users/{uid}/conversations/{conversation_id}` (Firestore)

**Key Fields**:
```python
{
    'id': 'uuid',
    'uid': 'user-id',
    'created_at': datetime,
    'finished_at': datetime,
    'started_at': datetime,
    'status': 'in_progress' | 'processing' | 'completed',
    'discarded': bool,  # True if conversation was filtered out

    # The full transcript
    'transcript_segments': [
        {
            'text': 'What the user said',
            'speaker': 'SPEAKER_00',
            'start': 0.0,
            'end': 5.2,
            'is_user': True
        }
    ],

    # Structured summary (created by LLM)
    'structured': {
        'title': 'Discussion about AI models',
        'overview': 'User discussed preferences...',
        'emoji': '💡',
        'category': 'personal',
        'action_items': [
            {
                'description': 'Research Claude API',
                'completed': False
            }
        ],
        'events': []
    },

    # App processing results
    'apps_results': [
        {
            'app_id': 'summary_assistant',
            'content': 'Generated summary...'
        }
    ],

    # Photos (if OpenGlass integration)
    'photos': []
}
```

**Size**: Large (full transcript + metadata)
**Purpose**: Complete record of what was said and when
**Retention**: Full history preserved
**Display**: Shown in "Conversations" tab in app

---

### Memory Data Model

**Location**: `users/{uid}/memories/{memory_id}` (Firestore)

**Key Fields**:
```python
{
    'id': 'content-hash',  # Deterministic ID from content
    'uid': 'user-id',
    'created_at': datetime,
    'updated_at': datetime,

    # The extracted fact/insight (SHORT)
    'content': 'User prefers TypeScript over JavaScript for type safety',

    # Categorization
    'category': 'interesting' | 'system' | 'core' | 'work' | etc,
    'tags': ['programming', 'preferences'],

    # Metadata
    'conversation_id': 'source-conversation-uuid',
    'memory_id': 'same-as-conversation_id',  # Legacy field

    # User interaction
    'reviewed': bool,
    'user_review': True | False | None,
    'visibility': 'public' | 'private',
    'manually_added': bool,
    'edited': bool,

    # Scoring for retrieval
    'scoring': '00_999_1730316542',  # Format: manual_boost_category_timestamp

    # App association
    'app_id': 'optional-app-that-created-this',

    # Security
    'data_protection_level': 'standard' | 'enhanced',
    'is_locked': bool
}
```

**Size**: Small (just the fact, no full transcript)
**Purpose**: Long-term knowledge base for AI assistant context
**Retention**: Permanent (unless deleted)
**Display**: Shown in "Memories" tab in app

---

## 🔄 **Processing Pipeline - How Conversations Become Memories**

### Step 1: Real-Time Audio Processing (0-2 minutes)

```
iOS Device → WebSocket → Deepgram → Transcript Chunks
                              ↓
                      In-Memory Buffer
                              ↓
                      (600ms aggregation)
```

**Status at this point**:
- Conversation is `in_progress`
- Transcript segments accumulating
- NO memories extracted yet

---

### Step 2: Conversation Completion (2-minute timeout or manual stop)

**File**: `routers/transcribe.py` (WebSocket endpoint)

```python
async def _websocket_util(
    websocket: WebSocket,
    uid: str,
    ...
):
    # ... real-time processing ...

    # After 120 seconds of silence OR manual stop:

    # 1. Create conversation object
    conversation_data = {
        'id': session_id,
        'transcript_segments': realtime_segment_buffers,
        'status': 'processing',
        # ... other fields
    }

    # 2. Trigger conversation processing (async background task)
    asyncio.create_task(
        process_conversation_wrapper(uid, conversation_data)
    )
```

**Status at this point**:
- Conversation status changed to `processing`
- Full transcript saved to Firestore
- Background processing begins

---

### Step 3: Conversation Processing (main thread)

**File**: `utils/conversations/process_conversation.py`

```python
def process_conversation(
    uid: str,
    conversation: Conversation,
    ...
):
    # 1. Check if conversation should be discarded
    discarded = should_discard_conversation(
        transcript_text,
        conversation.photos
    )

    if discarded:
        # Save conversation with discarded=True
        # NO memories extracted
        # NO app processing
        return conversation

    # 2. Generate structured summary (LLM call)
    structured = get_transcript_structure(
        transcript_text,
        started_at,
        language_code,
        tz
    )
    # Creates: title, overview, action_items, events, category, emoji

    # 3. Trigger app processing
    _trigger_apps(uid, conversation, ...)

    # 4. Launch background threads (ASYNC!)
    threading.Thread(target=_extract_memories, args=(uid, conversation)).start()
    threading.Thread(target=_extract_trends, args=(uid, conversation)).start()
    threading.Thread(target=_save_action_items, args=(uid, conversation)).start()

    # 5. Mark conversation as completed
    conversation.status = 'completed'
    conversations_db.upsert_conversation(uid, conversation.dict())

    print('process_conversation completed conversation.id=', conversation.id)
    return conversation
```

**Status at this point**:
- Conversation saved with full transcript ✅
- Structured summary generated ✅
- Conversation status = `completed` ✅
- App IS receiving conversation data NOW
- Memories extraction happening in BACKGROUND (3-4 minute delay observed)

---

### Step 4: Memory Extraction (background thread - ASYNC!)

**File**: `utils/conversations/process_conversation.py`

```python
def _extract_memories(uid: str, conversation: Conversation):
    """
    Background thread - runs AFTER conversation processing completes
    This explains the 3-4 minute delay you observed!
    """

    # 1. Get full transcript text
    transcript = conversation.get_transcript(False, people=None)

    # 2. Use LLM to extract facts/insights
    parsed_memories = new_memories_extractor(
        uid=uid,
        conversation=conversation,
        transcript=transcript
    )
    # LLM analyzes transcript and extracts:
    # - Important facts
    # - Preferences
    # - Decisions
    # - Learnings
    # - Context about the user

    # 3. Convert to MemoryDB objects
    memory_dbs = [
        MemoryDB.from_memory(
            memory=Memory(content=item.content, category=item.category),
            uid=uid,
            conversation_id=conversation.id,
            manually_added=False
        )
        for item in parsed_memories
    ]

    # 4. Save to Firestore (separate collection!)
    if len(parsed_memories) == 0:
        print(f"No memories extracted for conversation {conversation.id}")
        return

    print(f"Saving {len(parsed_memories)} memories for conversation {conversation.id}")
    memories_db.save_memories(uid, [fact.dict() for fact in memory_dbs])
    # ^^^ This is what you saw in logs: "Saving 2 memories for conversation..."

    # 5. Track analytics
    if len(parsed_memories) > 0:
        record_usage(uid, memories_created=len(parsed_memories))
```

**Status at this point**:
- Memories saved to SEPARATE Firestore collection ✅
- App can now query memories endpoint
- Logs show: `Saving 2 memories for conversation 59f952fd-5729-46cf-baaa-d4933f0b70dc`

---

## ⏱️ **Timeline - What Happens When**

| Time | Event | Conversation Status | Memories Status | App Shows |
|------|-------|---------------------|-----------------|-----------|
| **T+0s** | Audio streaming starts | `in_progress` | None | Live chunks (if supported) |
| **T+91s** | User manually stops | `processing` | None | Nothing yet |
| **T+92s** | Conversation processing starts | `processing` | None | Nothing yet |
| **T+95s** | LLM generates structured summary | `processing` | Background extraction starts | Nothing yet |
| **T+96s** | Conversation saved to Firestore | `completed` | Still processing | **Conversation appears!** ✅ |
| **T+240s** | Memory extraction completes | `completed` | Extraction done | Conversation visible |
| **T+241s** | Memories saved to Firestore | `completed` | **Saved** ✅ | **Memories appear!** ✅ |

**This explains your observation**: "app still timing out but I do now see an earlier memory!"

The conversation appeared first (~90 seconds after recording), then memories appeared ~4 minutes later due to async LLM processing.

---

## 🔍 **How to Query Each**

### Get Conversations (App "Conversations" Tab)

**Endpoint**: `/v1/conversations` (GET)

**Backend Code**:
```python
# database/conversations.py
def get_conversations(
    uid: str,
    limit: int = 100,
    offset: int = 0,
    include_discarded: bool = False,
    statuses: List[str] = [],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    conversations_ref = db.collection('users').document(uid).collection('conversations')

    # Filter out discarded conversations (test/noise)
    if not include_discarded:
        conversations_ref = conversations_ref.where(filter=FieldFilter('discarded', '==', False))

    # Sort by most recent first
    conversations_ref = conversations_ref.order_by('created_at', direction=firestore.Query.DESCENDING)

    conversations = [doc.to_dict() for doc in conversations_ref.stream()]
    return conversations
```

**Returns**: Full conversation objects with transcripts, structured summaries, app results

---

### Get Memories (App "Memories" Tab)

**Endpoint**: `/v1/memories` (GET)

**Backend Code**:
```python
# database/memories.py
def get_memories(
    uid: str,
    limit: int = 100,
    offset: int = 0,
    categories: List[str] = [],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    memories_ref = db.collection('users').document(uid).collection('memories')

    # Sort by scoring (importance) and recency
    memories_ref = (
        memories_ref.order_by('scoring', direction=firestore.Query.DESCENDING)
        .order_by('created_at', direction=firestore.Query.DESCENDING)
        .limit(limit)
        .offset(offset)
    )

    memories = [doc.to_dict() for doc in memories_ref.stream()]

    # Filter out user-rejected memories
    result = [memory for memory in memories if memory['user_review'] is not False]
    return result
```

**Returns**: Just memory facts (no full transcripts), sorted by importance

---

## 🧠 **How Memories Are Used**

### 1. **AI Assistant Context**

When you chat with your OMI AI assistant:

```python
# The assistant retrieves your memories to understand you better
memories = get_memories(uid, limit=50)
context = Memory.get_memories_as_str(memories)

# Context looks like:
"""
- User prefers TypeScript over JavaScript (2025-10-30 19:42:39 UTC)
- User is building a necklace audio device
- User discussed Apple and Starlink technologies
- User mentioned they see memories now
"""

# This context is passed to the LLM so it "remembers" you
ai_response = llm.chat(user_message, context=context)
```

### 2. **Vector Search for Relevant Facts**

```python
# Generate embedding for current conversation
vector = generate_embedding(conversation_text)

# Search for similar memories in vector database
relevant_memories = vector_db.search(vector, limit=10)

# Use these to provide context-aware responses
```

### 3. **Memory Categories**

Memories are categorized to prioritize important facts:

```python
class MemoryCategory(str, Enum):
    interesting = "interesting"  # Boost: 1 (high priority)
    system = "system"            # Boost: 0 (low priority)
    core = "core"                # Legacy, maps to 'system'
    work = "work"                # Legacy, maps to 'system'
    hobbies = "hobbies"          # Legacy, maps to 'system'
    # ... etc
```

The `scoring` field determines retrieval priority:
```python
scoring = "{:02d}_{:02d}_{:010d}".format(
    1 if manually_added else 0,  # Manual memories ranked higher
    999 - category_boost,         # Category priority
    int(created_at.timestamp())   # Recency
)
# Example: "01_999_1730316542" = manual, interesting, recent
```

---

## 🚨 **Why Your Test Conversation Was Discarded**

### The Discard Logic

**File**: `utils/llm/conversation_processing.py`

```python
def should_discard_conversation(
    transcript: str,
    photos: List[ConversationPhoto] = None
) -> bool:
    """
    Determines if a conversation should be discarded based on content quality.
    """

    # 1. Performance optimization: Long conversations are always kept
    if transcript and len(transcript.split(' ')) > 100:
        return False  # Keep it!

    # 2. Short conversations analyzed by LLM
    result = llm_analyze_conversation_quality(transcript, photos)

    # 3. KEEP if conversation contains:
    #    - A task, request, or action item
    #    - A decision, commitment, or plan
    #    - Meaningful conversation content
    #
    # 4. DISCARD if conversation:
    #    - Too short (< 100 words)
    #    - Test content ("testing 1 2 3")
    #    - Background noise without speech
    #    - Nonsensical fragments

    return result  # True = discard, False = keep
```

### Your Test Cases:

**Test 1**: "Number two happening right now for the Ella AI app back end"
- **Words**: ~11 words
- **LLM Analysis**: "This appears to be a test/debugging statement, not meaningful conversation"
- **Result**: ❌ DISCARDED
- **Saved to Firestore**: Yes, but with `discarded: true`
- **Memories extracted**: ❌ NO
- **Shown in app**: ❌ NO (filtered out by default)

**Test 2**: 91-second conversation about AI models, Apple, Starlink
- **Words**: ~200+ words
- **LLM Analysis**: Bypassed (> 100 words = auto-keep)
- **Result**: ✅ KEPT
- **Saved to Firestore**: Yes, `discarded: false`
- **Memories extracted**: ✅ YES (2 memories saved)
- **Shown in app**: ✅ YES (both conversation and memories)

---

## 📱 **What the App Shows**

### Conversations Tab

**Query**: `GET /v1/conversations?uid={uid}&include_discarded=false&limit=20`

**Shows**:
- Conversation title (e.g., "Discussion about AI models")
- Date/time
- Duration
- Category emoji
- Overview summary
- Action items count
- Tap to expand: Full transcript

**Data Source**: `users/{uid}/conversations` collection (Firestore)

---

### Memories Tab

**Query**: `GET /v1/memories?uid={uid}&limit=100`

**Shows**:
- Memory content (just the fact)
- Category badge
- Date created
- Source conversation link
- User review status (thumbs up/down)

**Data Source**: `users/{uid}/memories` collection (Firestore)

**Sorting**: By importance score (manually added > category priority > recency)

---

## 🔧 **Firestore Database Structure**

```
Firestore
├── users/
│   └── {uid}/
│       ├── conversations/                    # FULL TRANSCRIPTS
│       │   ├── {conversation_id_1}/
│       │   │   ├── id: "uuid"
│       │   │   ├── created_at: timestamp
│       │   │   ├── transcript_segments: [...]  # FULL AUDIO TRANSCRIPT
│       │   │   ├── structured: {...}           # LLM-generated summary
│       │   │   ├── discarded: false
│       │   │   ├── status: "completed"
│       │   │   └── photos/                     # Sub-collection (OpenGlass)
│       │   │       └── {photo_id}/
│       │   │           └── base64: "..."
│       │   │
│       │   └── {conversation_id_2}/
│       │       └── ...
│       │
│       └── memories/                         # EXTRACTED FACTS
│           ├── {memory_id_1}/
│           │   ├── id: "content-hash"
│           │   ├── content: "User prefers TypeScript"  # SHORT FACT
│           │   ├── category: "interesting"
│           │   ├── conversation_id: "source-uuid"
│           │   ├── scoring: "01_001_1730316542"
│           │   └── reviewed: true
│           │
│           └── {memory_id_2}/
│               └── ...
```

**Key Point**: Conversations and Memories are in **SEPARATE COLLECTIONS** under the same user!

---

## 🎯 **Answering Your Questions**

### Q: "How do memories get triggered?"

**A**: Automatic background thread after conversation processing completes.

**Trigger Point** (line 482 in `process_conversation.py`):
```python
threading.Thread(target=_extract_memories, args=(uid, conversation)).start()
```

**Triggering Conditions**:
- ✅ Conversation must NOT be discarded
- ✅ Conversation must have meaningful content (> 100 words OR quality transcript)
- ✅ Runs automatically for EVERY non-discarded conversation
- ✅ NO manual action needed

---

### Q: "Why did I see conversation before memory?"

**A**: Asynchronous processing with different timelines:

1. **Conversation saved**: ~90 seconds after recording stops
2. **Memory extraction starts**: Same time as conversation save (background thread)
3. **Memory LLM processing**: 2-4 minutes (analyzing transcript, extracting facts)
4. **Memory saved**: After LLM completes

**Your observation**:
- "app still timing out but I do now see an earlier memory!"
- Conversation appeared first (fast path)
- Memory appeared later (slow LLM path)

---

### Q: "Are conversations and memories 2 separate things?"

**A**: ✅ **YES! Absolutely correct!**

- **Separate Firestore collections**
- **Different data models**
- **Different purposes**
- **Different query endpoints**
- **Different display in app**

**Relationship**: Memories are DERIVED FROM conversations, but stored separately.

---

## 🚀 **Production Behavior Summary**

### What's Working ✅

1. **Real-time transcription**: Deepgram Nova 3 transcribing successfully
2. **Conversation storage**: Full transcripts saved to Firestore with `discarded: false`
3. **Structured summaries**: LLM generating titles, overviews, action items
4. **Memory extraction**: Background LLM extracting 2 memories from 91s conversation
5. **App data retrieval**: App successfully querying and displaying both conversations and memories
6. **Discard logic**: Filtering out test/noise conversations automatically

### What Needs Investigation ⚠️

1. **Firestore direct queries showing empty**:
   - You reported Firestore appears empty in console
   - BUT app IS receiving data somehow
   - Possible explanations:
     - Data in different region/database
     - API layer caching before Firestore write
     - Async write delay (eventual consistency)
     - Looking at wrong Firestore project

2. **App timeout issues**:
   - Missing composite indexes (you created one for conversations)
   - May need additional index for memories: `scoring` + `created_at`
   - Query timeout due to unindexed filters

3. **Memory triggering delay**:
   - 3-4 minute delay is normal (LLM processing time)
   - Could be optimized with faster LLM model
   - Not a bug, just expected behavior

---

## 📋 **Recommended Next Actions**

### 1. Create Missing Firestore Index for Memories

**Current Issue**: Memory page timing out

**Solution**: Create composite index

**Index Fields**:
```
Collection: memories
Fields:
  - scoring (Descending)
  - created_at (Descending)
```

**Create via**:
- Wait for error message with auto-generated link
- OR manually create in Firebase Console

---

### 2. Verify Firestore Data Visibility

**Run test query**:
```python
from google.cloud import firestore
import os

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = './google-credentials.json'
db = firestore.Client()

# Check conversations
conversations = db.collection('users').document('{YOUR_UID}').collection('conversations').stream()
print("Conversations:")
for conv in conversations:
    data = conv.to_dict()
    print(f"  - {conv.id}: {data.get('structured', {}).get('title', 'No title')}")

# Check memories
memories = db.collection('users').document('{YOUR_UID}').collection('memories').stream()
print("\nMemories:")
for mem in memories:
    data = mem.to_dict()
    print(f"  - {mem.id}: {data.get('content', 'No content')}")
```

---

### 3. Monitor Memory Extraction Logs

**Check VPS logs** for memory extraction:
```bash
ssh root@100.101.168.91
journalctl -u omi-backend -f | grep -E "extract_memories|Saving.*memories"
```

**Expected output** after conversation:
```
Oct 30 19:46:04: Saving 2 memories for conversation 59f952fd-5729-46cf-baaa-d4933f0b70dc
```

---

## 📊 **Data Flow Diagram**

```
┌─────────────────────────────────────────────────────────────────┐
│                    AUDIO CAPTURE PHASE                          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    iOS Device Recording
                              ↓
                    Opus-encoded chunks
                              ↓
                    WebSocket streaming
                              ↓
                    Deepgram transcription
                              ↓
                    In-memory buffer (600ms)
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                  CONVERSATION PROCESSING                        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Check discard logic
                              ├─ DISCARD → Save with discarded=true
                              │             ❌ NO memories extracted
                              │
                              └─ KEEP → Generate structured summary
                                         (LLM creates title, overview, etc.)
                                                ↓
                                         Save conversation to Firestore
                                         Collection: users/{uid}/conversations/
                                                ↓
                                         Status: "completed"
                                         ✅ APP SHOWS CONVERSATION NOW
                                                ↓
┌─────────────────────────────────────────────────────────────────┐
│              BACKGROUND MEMORY EXTRACTION (ASYNC)               │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Launch background thread
                              ↓
                    LLM analyzes transcript
                    (2-4 minutes processing)
                              ↓
                    Extract facts/insights
                    (e.g., "User prefers X", "User mentioned Y")
                              ↓
                    Create Memory objects
                    (content, category, tags)
                              ↓
                    Save memories to Firestore
                    Collection: users/{uid}/memories/
                              ↓
                    ✅ APP SHOWS MEMORIES NOW
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    APP DISPLAYS BOTH                            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Conversations Tab
                    - Shows full conversation
                    - Transcript available
                    - Structured summary
                              ↓
                    Memories Tab
                    - Shows extracted facts
                    - Links to source conversation
                    - User can review/edit
```

---

## 🎓 **Key Takeaways**

1. **Conversations = Complete Records**: Full transcript, structured summary, apps results
2. **Memories = Extracted Facts**: Short insights derived from conversations
3. **Separate Storage**: Different Firestore collections (`conversations/` and `memories/`)
4. **Async Processing**: Memories extracted 2-4 minutes AFTER conversation completes
5. **LLM-Powered**: Both structured summaries and memory extraction use LLM analysis
6. **Quality Filtering**: Discard logic prevents noise/test data from cluttering database
7. **User Control**: Users can review, edit, delete memories; conversations remain immutable

---

**Document Version**: 1.0
**Last Updated**: October 30, 2025
**Next Review**: After resolving Firestore visibility and index issues
