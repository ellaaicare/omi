# Data Storage & Search Architecture - OMI Backend

**Date:** November 16, 2025
**Purpose:** Document how memories, transcripts, and summaries are stored and searched
**For:** Letta/Ella AI integration planning

---

## Executive Summary

**Can Letta use existing OMI data instead of storing duplicates?**
✅ **YES** - OMI backend has comprehensive APIs and search infrastructure that Letta can query directly.

**Key Points:**
- All data stored in **Firestore** (primary database)
- **Pinecone** for vector/semantic search
- **Typesense** for full-text search
- **Multiple search methods** available (tags, dates, categories, vector similarity)
- **RESTful APIs** with authentication for external access
- **iOS app already uses this** - same APIs can be used by Letta agents

---

## Storage Architecture

### 1. Firestore (Primary Database)

**Collection Structure:**
```
users/
  └── {uid}/
      ├── memories/
      │   └── {memory_id}
      │       ├── id: string
      │       ├── content: string (encrypted if enhanced protection)
      │       ├── category: "interesting" | "system"
      │       ├── tags: string[]
      │       ├── created_at: timestamp
      │       ├── updated_at: timestamp
      │       ├── conversation_id: string
      │       ├── scoring: string (for ranking)
      │       ├── visibility: "private" | "public" | "shared"
      │       ├── reviewed: boolean
      │       ├── manually_added: boolean
      │       └── data_protection_level: "standard" | "enhanced"
      │
      └── conversations/
          └── {conversation_id}
              ├── id: string
              ├── created_at: timestamp
              ├── started_at: timestamp
              ├── finished_at: timestamp
              ├── source: "omi" | "friend" | "openglass" | "screenpipe" | ...
              ├── language: string
              ├── status: "completed" | "in_progress" | "processing"
              ├── transcript_segments: []
              │   └── {
              │       text: string,
              │       speaker: string,
              │       speaker_id: int,
              │       is_user: boolean,
              │       start: float,
              │       end: float,
              │       person_id: string,
              │       source: "deepgram" | "edge_asr" | "soniox"
              │       asr_provider: "apple_speech" | "parakeet" | ...
              │     }
              ├── structured: {
              │   title: string,
              │   overview: string,
              │   emoji: string,
              │   category: CategoryEnum (30+ categories),
              │   action_items: [],
              │   events: []
              │ }
              ├── geolocation: { latitude, longitude, address }
              ├── photos: []
              ├── audio_files: []
              ├── discarded: boolean
              ├── visibility: "private" | "shared" | "public"
              └── data_protection_level: "standard" | "enhanced"
```

**Categories (30+ available):**
- personal, education, health, finance, legal, philosophy, spiritual, science
- entrepreneurship, parenting, romance, travel, inspiration, technology
- business, social, work, sports, politics, literature, history
- architecture, music, weather, news, entertainment, psychology
- real, design, family, economics, environment, other

### 2. Pinecone (Vector Database)

**Purpose:** Semantic/vector search for conversations

**Index Structure:**
```python
{
  "id": "{uid}-{conversation_id}",
  "values": [float] * 1536,  # Embedding vector
  "metadata": {
    "uid": uid,
    "memory_id": conversation_id,
    "created_at": unix_timestamp,

    # Enhanced metadata for filtering:
    "people": ["John", "Sarah"],  # People mentioned
    "topics": ["medication", "exercise"],  # Topics discussed
    "entities": ["Kaiser Hospital", "Blue Shield"],  # Entities
    "dates": ["2025-11-16"]  # Dates mentioned
  }
}
```

**Vector Search Capabilities:**
- Semantic similarity search
- Filter by date range
- Filter by people, topics, entities
- Top-k results (configurable)
- Metadata-based re-ranking

**Example Query:**
```python
query_vectors(
    query="Tell me about my doctor appointments",
    uid="user123",
    starts_at=1700000000,
    ends_at=1702592000,
    k=5
)
# Returns: Top 5 semantically similar conversation IDs
```

### 3. Typesense (Full-Text Search)

**Purpose:** Fast text search for conversations

**Index Schema:**
```typescript
{
  userId: string,
  created_at: timestamp,
  started_at: timestamp,
  finished_at: timestamp,
  discarded: boolean,
  structured: {
    title: string,
    overview: string,
    category: string
  }
}
```

**Search Fields:**
- `structured.overview` - Conversation summary
- `structured.title` - Conversation title

**Search Features:**
- Full-text keyword search
- Date range filtering
- Category filtering
- Pagination support
- Sorting by relevance or date

**Example Query:**
```json
{
  "q": "chest pain medication",
  "query_by": "structured.overview, structured.title",
  "filter_by": "userId:=user123 && created_at:>=1700000000",
  "sort_by": "created_at:desc",
  "per_page": 10
}
```

---

## Search Methods Available

### 1. Memory Search (GET /v3/memories)

**Endpoint:** `GET /v3/memories`

**Query Parameters:**
- `limit`: int (default: 100)
- `offset`: int (default: 0)
- `categories`: string[] (filter by category)
- `start_date`: ISO datetime (optional)
- `end_date`: ISO datetime (optional)

**Returns:**
```json
[
  {
    "id": "mem_123",
    "content": "User takes blood pressure medication daily",
    "category": "interesting",
    "tags": ["health", "medication"],
    "created_at": "2025-11-16T10:30:00Z",
    "conversation_id": "conv_456",
    "scoring": "00_999_1731753000",
    "visibility": "private"
  }
]
```

**Sorting:** By `scoring` (manual > category boost > created_at)

### 2. Conversation Search (POST /v1/conversations/search)

**Endpoint:** `POST /v1/conversations/search`

**Request Body:**
```json
{
  "query": "doctor appointment",
  "page": 1,
  "per_page": 10,
  "include_discarded": false,
  "start_date": "2025-11-01T00:00:00Z",
  "end_date": "2025-11-30T23:59:59Z"
}
```

**Returns:**
```json
{
  "items": [
    {
      "id": "conv_789",
      "created_at": "2025-11-16T14:00:00Z",
      "structured": {
        "title": "Doctor Appointment Discussion",
        "overview": "Discussed upcoming appointment with Dr. Smith",
        "emoji": "🏥",
        "category": "health"
      },
      "transcript_segments": [...],
      "status": "completed"
    }
  ],
  "total_pages": 5,
  "current_page": 1,
  "per_page": 10
}
```

**Backend:** Uses Typesense for fast full-text search

### 3. Vector/Semantic Search (Backend API)

**Function:** `query_vectors()` in `database/vector_db.py`

**Parameters:**
```python
query_vectors(
    query: str,              # Natural language query
    uid: str,                # User ID
    starts_at: int = None,   # Unix timestamp
    ends_at: int = None,     # Unix timestamp
    k: int = 5               # Number of results
)
```

**Returns:** List of conversation IDs ranked by semantic similarity

**Example Use Case:**
```python
# Find conversations about health topics
results = query_vectors(
    query="health problems and symptoms",
    uid="user123",
    k=10
)
# Returns: ["conv_1", "conv_2", "conv_3", ...]
```

### 4. Enhanced Metadata Search (Backend API)

**Function:** `query_vectors_by_metadata()` in `database/vector_db.py`

**Parameters:**
```python
query_vectors_by_metadata(
    uid: str,
    vector: List[float],      # Query embedding
    dates_filter: List[datetime],  # [start, end]
    people: List[str],        # ["John", "Sarah"]
    topics: List[str],        # ["health", "medication"]
    entities: List[str],      # ["Kaiser", "Blue Shield"]
    dates: List[str],         # ["2025-11-16"]
    limit: int = 5
)
```

**Returns:** Conversation IDs ranked by metadata match + vector similarity

**Features:**
- Filters by people mentioned
- Filters by topics discussed
- Filters by entities (organizations, places)
- Filters by date range
- Re-ranks by metadata relevance

---

## API Endpoints for External Access

### Memory Endpoints

**1. Get Memories**
```
GET /v3/memories
Authorization: Bearer {firebase_token}

Query Params:
  - limit: int
  - offset: int
  - categories: string[]
  - start_date: ISO datetime
  - end_date: ISO datetime
```

**2. Create Memory**
```
POST /v3/memories
Authorization: Bearer {firebase_token}

Body:
{
  "content": "string",
  "category": "interesting" | "system",
  "tags": ["tag1", "tag2"],
  "visibility": "private" | "public"
}
```

**3. Get Single Memory**
```
GET /v3/memories/{memory_id}
Authorization: Bearer {firebase_token}
```

**4. Review Memory**
```
POST /v3/memories/{memory_id}/review
Authorization: Bearer {firebase_token}

Body: { "value": true/false }
```

### Conversation Endpoints

**1. Get Conversations**
```
GET /v1/conversations
Authorization: Bearer {firebase_token}

Query Params:
  - limit: int (max 100)
  - offset: int
  - include_discarded: bool
```

**2. Get Single Conversation**
```
GET /v1/conversations/{conversation_id}
Authorization: Bearer {firebase_token}
```

**3. Search Conversations (Full-Text)**
```
POST /v1/conversations/search
Authorization: Bearer {firebase_token}

Body:
{
  "query": "search terms",
  "page": 1,
  "per_page": 10,
  "include_discarded": false,
  "start_date": "2025-11-01T00:00:00Z",
  "end_date": "2025-11-30T23:59:59Z"
}
```

**4. Create Conversation (External Integration)**
```
POST /v1/conversations
Authorization: Bearer {firebase_token}

Body:
{
  "started_at": "2025-11-16T10:00:00Z",
  "finished_at": "2025-11-16T10:30:00Z",
  "transcript_segments": [
    {
      "text": "Hello, how are you?",
      "speaker": "SPEAKER_00",
      "speaker_id": 0,
      "is_user": true,
      "start": 0.0,
      "end": 2.5
    }
  ],
  "source": "workflow" | "external_integration"
}
```

---

## Authentication for External Systems

### Option 1: Firebase Authentication (Recommended)

**For Letta Agents:**
1. Create a service account for Letta in Firebase
2. Generate a custom token for the user being queried
3. Use token in Authorization header

```python
# Letta backend generates custom token
import firebase_admin
from firebase_admin import auth

custom_token = auth.create_custom_token(uid="user123")

# Send to OMI API
headers = {"Authorization": f"Bearer {custom_token}"}
response = requests.get(
    "https://api.ella-ai-care.com/v3/memories",
    headers=headers
)
```

### Option 2: Admin API Key (Development Only)

**Current E2E Testing Approach:**
```python
# Dev API key format
dev_key = f"dev_testing_key_12345{uid}"

headers = {"Authorization": f"Bearer {dev_key}"}
```

**Note:** Not recommended for production Letta integration (dev keys are for testing only)

### Option 3: Create Letta-Specific Endpoint

**Backend can add:**
```python
@router.get("/v1/letta/memories/{uid}")
async def get_memories_for_letta(
    uid: str,
    letta_api_key: str = Header(...),
):
    # Validate Letta API key
    if letta_api_key != os.getenv("LETTA_API_KEY"):
        raise HTTPException(401)

    # Return memories without Firebase auth
    return memories_db.get_memories(uid)
```

---

## iOS App Memory Search Feature

**Implementation:** iOS app uses **POST /v1/conversations/search**

**User Workflow:**
1. User types search query in iOS app
2. App sends POST request to `/v1/conversations/search`
3. Backend queries **Typesense** full-text search
4. Returns matching conversations
5. iOS displays results with title, overview, emoji, date

**Search happens on backend, not iOS app!**

**iOS Code Example:**
```dart
// iOS app calls backend search API
final response = await http.post(
  Uri.parse('$baseUrl/v1/conversations/search'),
  headers: {'Authorization': 'Bearer $firebaseToken'},
  body: jsonEncode({
    'query': userSearchQuery,
    'page': 1,
    'per_page': 20,
  }),
);

final results = jsonDecode(response.body)['items'];
// Display in UI
```

**Note:** iOS does NOT do local search - it's all server-side via Typesense!

---

## Letta Integration Options

### Option A: Direct API Access (Recommended)

**Architecture:**
```
Letta Agent → OMI Backend API → Firestore/Pinecone/Typesense
                ↓
          Returns memories/conversations
```

**Advantages:**
- ✅ No duplicate storage
- ✅ Single source of truth
- ✅ Real-time access to user data
- ✅ Leverages existing search infrastructure
- ✅ Consistent with iOS app approach

**Implementation:**
```python
# Letta agent queries OMI API
class LettaOMIConnector:
    def get_user_memories(self, uid: str, query: str = None):
        """Get memories from OMI backend"""
        url = "https://api.ella-ai-care.com/v3/memories"
        token = self._get_firebase_token(uid)

        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 50}
        )
        return response.json()

    def search_conversations(self, uid: str, query: str):
        """Search conversations via Typesense"""
        url = "https://api.ella-ai-care.com/v1/conversations/search"
        token = self._get_firebase_token(uid)

        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "per_page": 10}
        )
        return response.json()["items"]

    def vector_search(self, uid: str, query: str):
        """Semantic search via Pinecone (if backend exposes endpoint)"""
        # Option 1: Backend adds endpoint
        url = "https://api.ella-ai-care.com/v1/conversations/vector-search"

        # Option 2: Letta queries Pinecone directly
        # (needs Pinecone API key and index name)
        pass
```

### Option B: PostgreSQL Cache + Sync (Not Recommended)

**Architecture:**
```
OMI Backend → Webhook → Letta → PostgreSQL
                         ↓
                  (stores duplicate)
```

**Disadvantages:**
- ❌ Duplicate storage
- ❌ Sync complexity
- ❌ Data consistency issues
- ❌ Increased latency (webhook delays)

**Only use if:**
- Letta needs offline access
- Letta performs complex SQL queries OMI doesn't support
- Response time requirements < 100ms (API too slow)

### Option C: Hybrid Approach

**Architecture:**
```
Letta PostgreSQL:
  - User preferences
  - Agent state
  - Conversation history with Letta

OMI Backend API:
  - User memories (query on-demand)
  - Transcripts (query on-demand)
  - Conversation summaries (query on-demand)
```

**Best of both worlds:**
- ✅ Letta stores what it needs
- ✅ OMI remains source of truth for user data
- ✅ No duplication of memories/transcripts

---

## Backend Modifications Needed

### 1. Add Vector Search Endpoint (Optional)

**Current:** Vector search is backend-internal only

**Needed for Letta:**
```python
@router.post("/v1/conversations/vector-search")
async def vector_search_conversations(
    query: str,
    limit: int = 5,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    uid: str = Depends(auth.get_current_user_uid)
):
    """Semantic/vector search using Pinecone"""
    from database.vector_db import query_vectors

    start_ts = parse_iso_to_timestamp(start_date) if start_date else None
    end_ts = parse_iso_to_timestamp(end_date) if end_date else None

    conversation_ids = query_vectors(
        query=query,
        uid=uid,
        starts_at=start_ts,
        ends_at=end_ts,
        k=limit
    )

    # Fetch full conversation objects
    conversations = [
        conversations_db.get_conversation(uid, cid)
        for cid in conversation_ids
    ]

    return {"items": conversations, "count": len(conversations)}
```

### 2. Letta Authentication Endpoint

**Create dedicated Letta API key auth:**
```python
# In routers/auth.py
async def get_letta_authenticated_uid(
    uid: str,
    letta_api_key: str = Header(None, alias="X-Letta-API-Key")
):
    """
    Authenticate Letta agent requests
    UID provided in request, verified via Letta API key
    """
    if not letta_api_key:
        raise HTTPException(401, "Missing Letta API key")

    # Validate against environment variable
    if letta_api_key != os.getenv("LETTA_API_KEY"):
        raise HTTPException(401, "Invalid Letta API key")

    # Validate UID exists
    if not users_db.get_user(uid):
        raise HTTPException(404, "User not found")

    return uid

# Use in Letta endpoints
@router.get("/v1/letta/memories/{uid}")
async def get_memories_for_letta(
    uid: str = Depends(get_letta_authenticated_uid)
):
    return memories_db.get_memories(uid, limit=100)
```

### 3. Batch Retrieval Endpoint

**Letta may need multiple conversations at once:**
```python
@router.post("/v1/conversations/batch")
async def get_conversations_batch(
    conversation_ids: List[str],
    uid: str = Depends(auth.get_current_user_uid)
):
    """Get multiple conversations in single request"""
    conversations = []
    for cid in conversation_ids:
        conv = conversations_db.get_conversation(uid, cid)
        if conv:
            conversations.append(conv)

    return {"items": conversations}
```

---

## Performance Considerations

### Current Performance

**Firestore:**
- Read latency: 10-50ms (single document)
- Batch read: 50-200ms (100 documents)
- Composite indexes required for complex queries

**Pinecone:**
- Query latency: 100-300ms (semantic search)
- Top-k=5: ~150ms average
- Metadata filtering adds ~50ms

**Typesense:**
- Search latency: 10-50ms (full-text)
- Very fast for keyword search
- Excellent for pagination

### Optimization for Letta

**1. Caching Strategy:**
```python
# Cache recent memories in Redis
@cache(ttl=300)  # 5 minute cache
def get_recent_memories(uid: str, limit: int = 50):
    return memories_db.get_memories(uid, limit=limit)
```

**2. Pagination:**
```python
# Don't fetch all memories at once
# Use pagination: limit=50, offset=0
```

**3. Field Filtering:**
```python
# Only fetch fields Letta needs
# Firestore supports field selection:
memories_ref.select(['content', 'tags', 'created_at'])
```

---

## Recommendation for Letta Integration

### ✅ **Recommended Approach: Direct API Access**

**Why:**
1. **Single source of truth** - OMI backend owns user data
2. **No duplication** - Memories/transcripts stored once
3. **Consistent with iOS** - Same APIs iOS app uses
4. **Lower complexity** - No sync logic needed
5. **Real-time access** - Always latest data

**Implementation Plan:**

**Phase 1: Authentication**
- [ ] Create Letta service account in Firebase
- [ ] Add Letta API key authentication endpoint
- [ ] Test with sample UID

**Phase 2: Core Endpoints**
- [ ] Expose `/v1/letta/memories/{uid}` endpoint
- [ ] Expose `/v1/letta/conversations/{uid}` endpoint
- [ ] Add batch retrieval support

**Phase 3: Search**
- [ ] Add vector search endpoint `/v1/conversations/vector-search`
- [ ] Verify Typesense search works for Letta use cases
- [ ] Document search API for Letta team

**Phase 4: Optimization**
- [ ] Add Redis caching for frequent queries
- [ ] Implement field filtering
- [ ] Monitor performance, optimize as needed

---

## Summary

**Storage:**
- ✅ Firestore (primary) - All memories, conversations, summaries
- ✅ Pinecone (vector DB) - Semantic search on conversations
- ✅ Typesense (full-text) - Keyword search on conversations

**Search Methods:**
- ✅ By tags (Firestore query)
- ✅ By date range (Firestore query)
- ✅ By category (Firestore query)
- ✅ Full-text keywords (Typesense)
- ✅ Semantic/vector (Pinecone)
- ✅ Metadata filters (Pinecone + Firestore)

**External Access:**
- ✅ RESTful APIs with Firebase auth
- ✅ iOS app already uses these APIs
- ✅ Letta can use same infrastructure

**Recommendation:**
**Letta should query OMI backend APIs directly instead of duplicating storage.**

---

**Created:** November 16, 2025
**Last Updated:** November 16, 2025
**Maintained By:** Backend Development Team
