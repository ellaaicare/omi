# Letta Integration Architecture

**Last Updated**: October 30, 2025
**Status**: Design Phase - Awaiting Implementation Decision

---

## Executive Summary

This document outlines three architectural approaches for integrating the OMI backend with the Letta AI agent system, based on the requirements for real-time 2-way conversation capabilities with intelligent response filtering.

### System Requirements

**User's Current n8n/Letta Architecture**:
1. **Postgres Database**: Maps user_id → letta_agent_id
2. **Fast LLM Scanner**: Filters audio chunks - not all input needs response
3. **Redis Buffer**: Aggregates chunks if system gets behind, splices backpressured chunks
4. **Letta Agent**: Main conversation agent - can send "NA" when no response needed
5. **Response Latency**: Must be under 2 seconds for real-time feel (not 2-minute timeout)

---

## Current OMI Backend Architecture

### Real-Time Data Streams

**Three Independent Data Access Points**:

1. **WebSocket Stream** (600ms latency - FASTEST)
   - File: `/root/omi/backend/routers/transcribe.py`
   - Chunks processed every 600ms (line 858-869)
   - In-memory buffer: `realtime_segment_buffers` (Python list)
   - Direct access to transcription segments as they arrive
   - **Current behavior**: Sends one batch per 600ms cycle, does NOT aggregate backpressure

2. **Webhook/Pusher Stream** (1-2s latency)
   - Optional real-time forwarding to external webhook
   - Separate buffer: `segment_buffers` (line 602-641)
   - Sends batches every 1 second
   - User configurable via device settings

3. **Firestore Storage** (end of session - SLOWEST)
   - Conversation saved after 2-minute timeout or manual stop
   - Full transcript with speaker diarization
   - Structured memory extraction and embeddings
   - Not suitable for real-time 2-way conversation

### Buffering System Details

**In-Memory Buffering** (NOT Redis):
```python
# Line 496-501: Buffer initialization
realtime_segment_buffers = []  # Main real-time buffer
segment_buffers = []            # Webhook forward buffer

# Line 499-501: Chunk accumulation
def stream_transcript(segments):
    nonlocal realtime_segment_buffers
    realtime_segment_buffers.extend(segments)  # Append new chunks

# Line 858-869: Processing loop (every 600ms)
async def stream_transcript_process():
    nonlocal websocket_active, realtime_segment_buffers
    while websocket_active or len(realtime_segment_buffers) > 0:
        await asyncio.sleep(0.6)  # 600ms wait

        if not realtime_segment_buffers:
            continue

        segments_to_process = realtime_segment_buffers.copy()
        realtime_segment_buffers = []  # Clear buffer after copy

        # Process segments...
```

**Current Limitations**:
- ❌ Does NOT aggregate multiple batches if processing gets behind
- ❌ Does NOT splice backpressured chunks together
- ❌ Sends exactly one batch per 600ms cycle regardless of queue depth
- ✅ Uses in-memory lists, not Redis (Redis only used for conversation state tracking)

---

## Integration Option 1: Webhook Approach (Keep Existing n8n Flow)

### Architecture
```
OMI Device → Backend WebSocket → Deepgram → Webhook → n8n → Letta
                                              ↓
                                         Firestore
```

### Implementation

**Backend Configuration** (Already Exists):
- User configures webhook URL in iOS app settings
- Backend sends transcript batches to webhook every 1 second
- No backend code changes required

**n8n Flow** (User's Existing System):
1. Receive webhook POST with transcript segments
2. Extract user_id from request
3. Query Postgres: `SELECT agent_id FROM user_agents WHERE user_id = ?`
4. Fast LLM scan: Check if input requires response
5. Redis buffer: Aggregate chunks if system is behind
6. Letta API call: Send aggregated chunks
7. Letta response: Return response or "NA" value
8. Backend webhook receiver: Forward response to iOS app

### Pros
- ✅ Zero backend code changes required
- ✅ Preserves all existing n8n logic (Postgres, fast LLM, Redis aggregation)
- ✅ User retains full control over filtering and aggregation logic
- ✅ Easy to debug and modify n8n flow independently
- ✅ Backend remains stateless and focused on transcription

### Cons
- ❌ Additional latency: ~1 second overhead from webhook hop
- ❌ Total latency: 3-4 seconds (Backend 1s + n8n 1s + Letta 1-2s)
- ❌ Requires n8n to be running 24/7
- ❌ Extra network hop introduces failure point
- ❌ Webhook response handling not currently implemented in backend

### Estimated Total Latency
```
Deepgram transcription:     600ms
Backend webhook send:       1000ms
n8n processing:             500ms
Postgres lookup:            50ms
Fast LLM scan:              200ms
Redis aggregation:          50ms
Letta API call:             1000-2000ms
-----------------------------------
TOTAL:                      3.4-4.4 seconds
```

**Verdict**: ⚠️ May exceed 2-second real-time requirement

---

## Integration Option 2: Direct Backend Modification (Lowest Latency)

### Architecture
```
OMI Device → Backend WebSocket → Deepgram → [Postgres → Fast LLM → Letta] → Response
                                              ↓
                                         Firestore
```

### Implementation

**New Backend Module**: `/root/omi/backend/utils/llms/letta_integration.py`

```python
import asyncpg
import redis
from letta import create_client

class LettaIntegration:
    def __init__(self):
        self.pg_pool = None  # Postgres connection pool
        self.redis = redis.Redis(host='172.21.0.4', port=6379)
        self.letta_client = create_client(...)

    async def init_postgres(self):
        """Initialize Postgres connection pool for agent lookup"""
        self.pg_pool = await asyncpg.create_pool(
            user='postgres',
            password='...',
            database='letta_agents',
            host='localhost',
            port=5432,
            min_size=5,
            max_size=20
        )

    async def get_agent_id(self, user_id: str) -> str:
        """Lookup Letta agent ID from Postgres"""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT agent_id FROM user_agents WHERE user_id = $1',
                user_id
            )
            return row['agent_id'] if row else None

    async def scan_for_alerts(self, text: str) -> bool:
        """Fast LLM scan to check if input needs response"""
        # Use fast GPT-4o-mini or similar
        prompt = f"Does this require a response? Answer YES or NO only.\n\n{text}"
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3,
            temperature=0
        )
        return "YES" in response.choices[0].message.content.upper()

    def aggregate_chunks(self, user_id: str, segments: list) -> str:
        """
        Redis-based chunk aggregation with backpressure handling

        Strategy:
        - Store chunks in Redis list per user
        - If processing is behind, splice multiple chunks together
        - Return aggregated text when ready to send to Letta
        """
        redis_key = f"chunks:{user_id}"

        # Add new chunks to Redis list
        for seg in segments:
            self.redis.rpush(redis_key, seg['text'])

        # Check queue depth
        queue_depth = self.redis.llen(redis_key)

        if queue_depth <= 3:
            # Normal flow: send latest chunk only
            chunk = self.redis.lpop(redis_key)
            return chunk.decode('utf-8') if chunk else ""
        else:
            # Backpressure: splice multiple chunks together
            chunks = []
            for _ in range(min(queue_depth, 10)):  # Max 10 chunks at once
                chunk = self.redis.lpop(redis_key)
                if chunk:
                    chunks.append(chunk.decode('utf-8'))
            return " ".join(chunks)

    async def process_chunk(self, user_id: str, segments: list) -> dict:
        """
        Main processing pipeline for real-time chunks

        Returns:
        - {"type": "response", "text": "..."} - Letta response
        - {"type": "no_response"} - No response needed
        - {"type": "error", "message": "..."} - Error occurred
        """
        # 1. Get agent ID from Postgres
        agent_id = await self.get_agent_id(user_id)
        if not agent_id:
            return {"type": "error", "message": "No agent configured"}

        # 2. Aggregate chunks with backpressure handling
        aggregated_text = self.aggregate_chunks(user_id, segments)
        if not aggregated_text:
            return {"type": "no_response"}

        # 3. Fast LLM scan for alert detection
        needs_response = await self.scan_for_alerts(aggregated_text)
        if not needs_response:
            return {"type": "no_response"}

        # 4. Call Letta agent
        try:
            response = await self.letta_client.send_message(
                agent_id=agent_id,
                message=aggregated_text,
                stream=False
            )

            # Check if Letta returned "NA" value
            if response.text.strip().upper() == "NA":
                return {"type": "no_response"}

            return {
                "type": "response",
                "text": response.text,
                "agent_id": agent_id
            }
        except Exception as e:
            return {"type": "error", "message": str(e)}

# Global instance
letta_integration = LettaIntegration()
```

**Modified WebSocket Handler**: `/root/omi/backend/routers/transcribe.py`

```python
# Add import at top
from utils.llms.letta_integration import letta_integration

# Modify stream_transcript_process function (line 858-869)
async def stream_transcript_process():
    nonlocal websocket_active, realtime_segment_buffers, realtime_photo_buffers

    # Initialize Letta integration
    await letta_integration.init_postgres()

    while websocket_active or len(realtime_segment_buffers) > 0:
        await asyncio.sleep(0.6)  # 600ms buffer

        if not realtime_segment_buffers and not realtime_photo_buffers:
            continue

        segments_to_process = realtime_segment_buffers.copy()
        realtime_segment_buffers = []

        # === NEW: Process chunks with Letta ===
        letta_result = await letta_integration.process_chunk(uid, segments_to_process)

        if letta_result['type'] == 'response':
            # Send Letta response back to iOS app via WebSocket
            await websocket.send_json({
                "type": "letta_response",
                "text": letta_result['text'],
                "agent_id": letta_result['agent_id']
            })
        elif letta_result['type'] == 'error':
            print(f"Letta error: {letta_result['message']}")
        # If no_response, do nothing (silent)

        # Continue with existing Firestore processing...
        # (existing code remains unchanged)
```

### Pros
- ✅ Lowest possible latency: 2-3 seconds total
- ✅ Single integrated system (no external dependencies)
- ✅ All logic in one codebase (easier debugging)
- ✅ WebSocket bidirectional communication (instant response to iOS)
- ✅ Implements all required features:
  - Postgres agent lookup
  - Fast LLM scanning
  - Redis chunk aggregation with backpressure handling
  - NA response handling

### Cons
- ❌ Requires significant backend code changes
- ❌ Adds complexity to transcribe.py (already 1300+ lines)
- ❌ Postgres dependency introduced to backend
- ❌ Harder to modify filtering logic (requires backend deployment)
- ❌ Increases backend memory usage (Redis connections, Postgres pool)
- ❌ Requires Letta Python SDK integration

### Estimated Total Latency
```
Deepgram transcription:     600ms
Postgres lookup:            50ms
Fast LLM scan:              200ms
Redis aggregation:          20ms
Letta API call:             1000-2000ms
WebSocket send:             50ms
-----------------------------------
TOTAL:                      1.92-2.92 seconds
```

**Verdict**: ✅ Meets 2-3 second real-time requirement

---

## Integration Option 3: Hybrid Approach (Recommended)

### Architecture
```
OMI Device → Backend WebSocket → Deepgram → [Fast Processing Path] → WebSocket Response
                                              ↓
                                         [Webhook → n8n → Letta] (Async, Optional)
                                              ↓
                                         Firestore
```

### Implementation Strategy

**Phase 1: Lightweight Backend Integration**
- Add ONLY fast LLM scanning to backend
- Keep Postgres lookup and Letta in n8n
- Use webhook for full processing, WebSocket for urgent alerts

**Phase 2: Gradual Migration**
- Move Postgres lookup to backend
- Keep Letta in n8n initially
- Monitor latency improvements

**Phase 3: Full Integration** (Optional)
- Move Letta calls to backend if latency requirements demand it
- Keep n8n as fallback/monitoring layer

### Code Changes (Phase 1)

**New Module**: `/root/omi/backend/utils/llms/fast_alert_scanner.py`

```python
from openai import AsyncOpenAI

class FastAlertScanner:
    def __init__(self):
        self.client = AsyncOpenAI()

    async def needs_immediate_response(self, text: str) -> tuple[bool, str]:
        """
        Ultra-fast scan to detect if immediate response needed

        Returns:
            (needs_response: bool, urgency_level: str)
        """
        prompt = f"""Classify this audio transcription. Answer with ONE word only:
        - URGENT: Immediate response needed (question, greeting, direct request)
        - ALERT: Important but not urgent
        - INFO: Just information, no response needed

        Text: {text}

        Answer (one word):"""

        response = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0
        )

        classification = response.choices[0].message.content.strip().upper()

        return (
            classification in ["URGENT", "ALERT"],
            classification
        )

scanner = FastAlertScanner()
```

**Modified WebSocket Handler**: `/root/omi/backend/routers/transcribe.py`

```python
from utils.llms.fast_alert_scanner import scanner

async def stream_transcript_process():
    # ... existing code ...

    while websocket_active or len(realtime_segment_buffers) > 0:
        await asyncio.sleep(0.6)

        segments_to_process = realtime_segment_buffers.copy()
        realtime_segment_buffers = []

        # === NEW: Fast alert detection ===
        if segments_to_process:
            combined_text = " ".join(seg['text'] for seg in segments_to_process)
            needs_response, urgency = await scanner.needs_immediate_response(combined_text)

            if needs_response:
                # Send alert to iOS app immediately
                await websocket.send_json({
                    "type": "alert",
                    "urgency": urgency,
                    "text": combined_text
                })

            # Also send to webhook for full n8n processing (async)
            if webhook_url:
                asyncio.create_task(send_to_webhook(webhook_url, segments_to_process))

        # Continue with existing processing...
```

### Pros
- ✅ Incremental migration (low risk)
- ✅ Fast alerts handled in backend (1-1.5s latency)
- ✅ Full processing still in n8n (preserves existing logic)
- ✅ Minimal code changes initially
- ✅ Can scale up to Option 2 if needed
- ✅ Best of both worlds: speed + flexibility

### Cons
- ❌ More complex architecture (two processing paths)
- ❌ Requires coordination between backend and n8n
- ❌ Potential for duplicate processing
- ❌ Still has webhook latency for full Letta responses

### Estimated Latency

**Alert Path** (Backend Only):
```
Deepgram:       600ms
Fast LLM:       200ms
WebSocket:      50ms
-------------------
TOTAL:          850ms ✅ VERY FAST
```

**Full Response Path** (n8n + Letta):
```
Deepgram:       600ms
Webhook:        1000ms
n8n:            500ms
Postgres:       50ms
Letta:          1500ms
-------------------
TOTAL:          3.65s ⚠️ Acceptable for non-urgent
```

**Verdict**: ✅ Best balance of speed, flexibility, and risk

---

## Decision Matrix

| Feature | Option 1 (Webhook) | Option 2 (Backend) | Option 3 (Hybrid) |
|---------|-------------------|-------------------|-------------------|
| **Latency (Urgent)** | 3-4s ❌ | 2-3s ✅ | <1s ✅✅ |
| **Latency (Full Response)** | 3-4s | 2-3s ✅ | 3-4s |
| **Code Changes** | None ✅✅ | Major ❌ | Minimal ✅ |
| **Complexity** | Low ✅ | High ❌ | Medium |
| **Flexibility** | High ✅ | Medium | High ✅ |
| **Risk** | Low ✅ | High ❌ | Low ✅ |
| **Postgres Required** | No ✅ | Yes ❌ | Phase 2 only |
| **Redis Required** | n8n only ✅ | Backend + n8n ❌ | n8n only ✅ |
| **Chunk Aggregation** | n8n ✅ | Backend | n8n ✅ |
| **Scalability** | Good | Excellent | Excellent |

---

## Recommended Implementation: Option 3 (Hybrid)

### Rationale

1. **Meets Latency Requirements**:
   - Urgent alerts: <1 second (backend fast LLM)
   - Full responses: 3-4 seconds (n8n + Letta)
   - User gets immediate feedback for critical interactions

2. **Low Risk Migration**:
   - Phase 1: Add fast scanner only (50 lines of code)
   - Phase 2: Move Postgres lookup if needed
   - Phase 3: Full backend integration only if required
   - Can rollback easily at each phase

3. **Preserves Existing Logic**:
   - All n8n flows remain operational
   - Chunk aggregation stays in Redis (tested system)
   - Letta integration unchanged
   - No disruption to current development

4. **Best User Experience**:
   - Instant alerts for questions/greetings (<1s)
   - Thoughtful responses for complex queries (2-4s)
   - "Typing..." indicator can bridge the gap

### Implementation Timeline

**Week 1: Fast Alert Scanner** (Phase 1)
- Add `fast_alert_scanner.py` module
- Modify `transcribe.py` to call scanner
- Send alerts via WebSocket to iOS app
- Test with real iOS device

**Week 2: iOS Response Handler**
- Update iOS app to receive WebSocket alerts
- Show "typing..." indicator while n8n processes
- Display Letta response when webhook completes

**Week 3: Monitoring & Optimization**
- Track latencies for both paths
- Tune fast LLM prompts for accuracy
- Optimize webhook response handling

**Week 4+: Gradual Migration** (Phase 2, Optional)
- Move Postgres lookup to backend if needed
- Consider moving Redis aggregation if latency critical
- Keep n8n as monitoring/fallback layer

---

## Redis Chunk Aggregation Implementation

### Current n8n Redis Logic

**User's Existing System** (to be preserved):
```javascript
// n8n Redis Node Configuration
const chunks = await redis.lrange(`chunks:${userId}`, 0, -1);

if (chunks.length <= 3) {
  // Normal flow: process one chunk
  const chunk = chunks.shift();
  await redis.lpop(`chunks:${userId}`);
  return chunk;
} else {
  // Backpressure: splice multiple chunks
  const aggregated = chunks.slice(0, 10).join(" ");
  await redis.ltrim(`chunks:${userId}`, 10, -1);
  return aggregated;
}
```

### Backend Implementation (if migrating to Option 2)

**Redis Manager**: `/root/omi/backend/utils/redis/chunk_aggregator.py`

```python
import redis.asyncio as aioredis

class ChunkAggregator:
    def __init__(self, redis_host='172.21.0.4', redis_port=6379):
        self.redis = aioredis.from_url(f"redis://{redis_host}:{redis_port}")

    async def push_chunk(self, user_id: str, text: str):
        """Add new chunk to user's queue"""
        key = f"chunks:{user_id}"
        await self.redis.rpush(key, text)
        await self.redis.expire(key, 300)  # 5-minute TTL

    async def get_aggregated_chunk(self, user_id: str) -> str:
        """
        Get next chunk(s) with backpressure handling

        Logic:
        - Queue depth <= 3: Return single chunk (normal flow)
        - Queue depth > 3: Splice up to 10 chunks (catch-up mode)
        """
        key = f"chunks:{user_id}"
        queue_depth = await self.redis.llen(key)

        if queue_depth == 0:
            return ""
        elif queue_depth <= 3:
            # Normal flow: one chunk at a time
            chunk = await self.redis.lpop(key)
            return chunk.decode('utf-8') if chunk else ""
        else:
            # Backpressure: aggregate multiple chunks
            num_to_aggregate = min(queue_depth, 10)
            chunks = []
            for _ in range(num_to_aggregate):
                chunk = await self.redis.lpop(key)
                if chunk:
                    chunks.append(chunk.decode('utf-8'))
            return " ".join(chunks)

    async def get_queue_depth(self, user_id: str) -> int:
        """Check how many chunks are queued"""
        key = f"chunks:{user_id}"
        return await self.redis.llen(key)

aggregator = ChunkAggregator()
```

---

## Postgres Agent Lookup Implementation

### Database Schema (User's Existing Setup)

```sql
-- Assumed schema (user to confirm)
CREATE TABLE user_agents (
    user_id VARCHAR(255) PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_id)
);

CREATE INDEX idx_user_agents_lookup ON user_agents(user_id);

-- Example data
INSERT INTO user_agents (user_id, agent_id) VALUES
('5aGC5YE9BnhcSoTxxtT4ar6ILQy2', 'letta-agent-12345');
```

### Backend Implementation

**Agent Lookup Manager**: `/root/omi/backend/utils/letta/agent_lookup.py`

```python
import asyncpg
from typing import Optional
from fastapi import HTTPException

class AgentLookup:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def init_pool(
        self,
        host='localhost',
        port=5432,
        database='letta_agents',
        user='postgres',
        password='...'
    ):
        """Initialize Postgres connection pool"""
        self.pool = await asyncpg.create_pool(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            min_size=5,
            max_size=20,
            command_timeout=2.0  # 2-second timeout for fast lookups
        )

    async def get_agent_id(self, user_id: str) -> Optional[str]:
        """
        Lookup Letta agent ID for user

        Returns:
            agent_id if found, None if not configured

        Raises:
            HTTPException if database error
        """
        if not self.pool:
            raise HTTPException(
                status_code=500,
                detail="Agent lookup not initialized"
            )

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT agent_id FROM user_agents WHERE user_id = $1',
                    user_id
                )
                return row['agent_id'] if row else None
        except asyncpg.PostgresError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Database error: {str(e)}"
            )

    async def create_user_agent(self, user_id: str, agent_id: str):
        """Create new user-agent mapping"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''INSERT INTO user_agents (user_id, agent_id, updated_at)
                   VALUES ($1, $2, NOW())
                   ON CONFLICT (user_id) DO UPDATE
                   SET agent_id = $2, updated_at = NOW()''',
                user_id, agent_id
            )

    async def close_pool(self):
        """Cleanup connection pool"""
        if self.pool:
            await self.pool.close()

# Global instance
agent_lookup = AgentLookup()

# FastAPI startup/shutdown
async def startup():
    await agent_lookup.init_pool(
        host=os.getenv('POSTGRES_HOST', 'localhost'),
        database=os.getenv('POSTGRES_DB', 'letta_agents'),
        user=os.getenv('POSTGRES_USER', 'postgres'),
        password=os.getenv('POSTGRES_PASSWORD')
    )

async def shutdown():
    await agent_lookup.close_pool()
```

---

## Letta API Integration

### Letta Client Setup

**Letta Interface**: `/root/omi/backend/utils/letta/client.py`

```python
from letta import create_client, LettaClient
from typing import Optional

class LettaInterface:
    def __init__(self):
        self.client: Optional[LettaClient] = None

    def init_client(self, base_url: str, api_key: str):
        """Initialize Letta client"""
        self.client = create_client(
            base_url=base_url,
            token=api_key
        )

    async def send_message(
        self,
        agent_id: str,
        message: str,
        stream: bool = False
    ) -> dict:
        """
        Send message to Letta agent

        Returns:
            {
                "text": str,          # Agent response text
                "needs_response": bool,  # True if real response, False if "NA"
                "metadata": dict      # Additional context
            }
        """
        if not self.client:
            raise ValueError("Letta client not initialized")

        try:
            response = self.client.send_message(
                agent_id=agent_id,
                message=message,
                stream=stream
            )

            # Check if Letta returned "NA" (no response needed)
            response_text = response.messages[-1].content.strip()
            is_na_response = response_text.upper() in ["NA", "N/A", "NO RESPONSE"]

            return {
                "text": response_text,
                "needs_response": not is_na_response,
                "metadata": {
                    "agent_id": agent_id,
                    "message_count": len(response.messages)
                }
            }
        except Exception as e:
            return {
                "text": "",
                "needs_response": False,
                "metadata": {
                    "error": str(e)
                }
            }

letta_interface = LettaInterface()
```

---

## Environment Configuration

### Backend .env Updates (Option 2 or 3)

```bash
# === Letta Integration ===

# Postgres (Agent Lookup)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=letta_agents
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_postgres_password

# Letta API
LETTA_BASE_URL=https://your-letta-api.com
LETTA_API_KEY=your_letta_api_key

# Redis (Chunk Aggregation) - Already configured
REDIS_DB_HOST=172.21.0.4
REDIS_DB_PORT=6379
REDIS_DB_PASSWORD=

# Fast LLM (Alert Scanning)
OPENAI_API_KEY=your_openai_key  # Already exists
```

---

## Testing Strategy

### Phase 1: Fast Alert Scanner Test

```bash
# Test script: test_fast_alerts.py
import asyncio
from utils.llms.fast_alert_scanner import scanner

async def test_alerts():
    test_cases = [
        ("Hello, how are you?", True, "URGENT"),  # Greeting
        ("What time is it?", True, "URGENT"),     # Question
        ("I'm just talking about my day", False, "INFO"),  # Info
        ("HELP! Emergency!", True, "URGENT"),     # Emergency
        ("Reminder to buy milk", True, "ALERT"),  # Reminder
    ]

    for text, expected_response, expected_urgency in test_cases:
        needs_response, urgency = await scanner.needs_immediate_response(text)
        assert needs_response == expected_response, f"Failed: {text}"
        assert urgency == expected_urgency, f"Wrong urgency: {text}"
        print(f"✅ {text[:30]}... -> {urgency}")

asyncio.run(test_alerts())
```

### Phase 2: Integration Test (Option 2)

```bash
# Test full pipeline with mock iOS app
python test_letta_integration.py
```

**Test Script**:
```python
import asyncio
import websockets
import json

async def test_letta_integration():
    uri = "ws://localhost:8000/v4/listen?uid=test_user_123&language=en"

    async with websockets.connect(uri) as websocket:
        # Send audio frames (mock)
        print("Sending audio frames...")
        await websocket.send(b"mock_audio_data")

        # Wait for Letta response
        response = await websocket.recv()
        data = json.loads(response)

        assert data['type'] == 'letta_response'
        assert 'text' in data
        print(f"✅ Received Letta response: {data['text']}")

asyncio.run(test_letta_integration())
```

---

## Monitoring & Metrics

### Key Metrics to Track

```python
# Add to transcribe.py
import time
from prometheus_client import Histogram

# Latency metrics
letta_latency = Histogram('letta_response_latency_seconds', 'Letta response time')
postgres_latency = Histogram('postgres_lookup_latency_seconds', 'Postgres lookup time')
fast_llm_latency = Histogram('fast_llm_scan_latency_seconds', 'Fast LLM scan time')

async def process_with_metrics():
    # Postgres lookup
    start = time.time()
    agent_id = await agent_lookup.get_agent_id(user_id)
    postgres_latency.observe(time.time() - start)

    # Fast LLM scan
    start = time.time()
    needs_response, urgency = await scanner.needs_immediate_response(text)
    fast_llm_latency.observe(time.time() - start)

    # Letta call
    start = time.time()
    response = await letta_interface.send_message(agent_id, text)
    letta_latency.observe(time.time() - start)
```

### Alerts to Configure

```yaml
# alerts.yaml
- alert: LettaHighLatency
  expr: letta_response_latency_seconds > 3.0
  for: 5m
  annotations:
    summary: "Letta responses taking >3 seconds"

- alert: PostgresLookupFailed
  expr: rate(postgres_lookup_errors[5m]) > 0.1
  annotations:
    summary: "High rate of Postgres lookup failures"
```

---

## Migration Checklist

### Option 1 (Webhook) - Zero Migration
- [ ] Configure webhook URL in iOS app settings
- [ ] Verify n8n receives transcript chunks
- [ ] Test Postgres lookup in n8n
- [ ] Verify Letta responses returned to app
- [ ] Monitor latency (should be 3-4s)

### Option 2 (Backend) - Full Migration
- [ ] Add Postgres connection pool to backend
- [ ] Implement `agent_lookup.py` module
- [ ] Implement `chunk_aggregator.py` module
- [ ] Integrate Letta SDK (`letta_interface.py`)
- [ ] Modify `transcribe.py` WebSocket handler
- [ ] Add environment variables to .env
- [ ] Test Postgres connectivity
- [ ] Test Redis chunk aggregation
- [ ] Test Letta API calls
- [ ] Deploy to VPS (api.ella-ai-care.com)
- [ ] Monitor latency (should be 2-3s)
- [ ] Verify iOS app receives responses
- [ ] Deprecate n8n webhook (keep as backup)

### Option 3 (Hybrid) - Recommended
**Phase 1**:
- [ ] Add `fast_alert_scanner.py` module
- [ ] Modify `transcribe.py` to call scanner
- [ ] Send alerts via WebSocket
- [ ] Test with iOS app
- [ ] Monitor alert latency (should be <1s)

**Phase 2** (optional):
- [ ] Add Postgres connection pool
- [ ] Migrate agent lookup to backend
- [ ] Monitor latency improvements

**Phase 3** (optional):
- [ ] Migrate chunk aggregation to backend
- [ ] Integrate Letta SDK
- [ ] Full backend processing (Option 2)

---

## Security Considerations

### Postgres Connection
- Use connection pooling (max 20 connections)
- 2-second query timeout to prevent blocking
- Store credentials in .env (not in code)
- Use read-only user if possible

### Redis Access
- Already secured (n8n-redis container on private Docker network)
- No authentication currently (add if exposed publicly)
- Use short TTLs (5 minutes) to prevent memory leaks

### Letta API
- Store API key in .env
- Use HTTPS for all API calls
- Implement rate limiting (max 100 requests/minute)
- Add retry logic with exponential backoff

---

## Rollback Plan

### Option 2 → Option 1 (Backend to Webhook)
1. Disable Letta integration in `transcribe.py`:
   ```python
   # Comment out Letta processing
   # letta_result = await letta_integration.process_chunk(uid, segments)
   ```
2. Re-enable webhook forwarding in n8n
3. Monitor webhook receives chunks
4. Verify Letta responses work via n8n

### Option 3 → Option 1 (Hybrid to Webhook)
1. Disable fast alert scanner:
   ```python
   # Comment out alert detection
   # needs_response, urgency = await scanner.needs_immediate_response(text)
   ```
2. Keep webhook forwarding enabled
3. iOS app falls back to webhook responses only

---

## Cost Analysis

### Option 1 (Webhook)
- **Server Costs**: n8n (existing), no backend changes
- **API Costs**: Deepgram ($0.0043/min) + Letta (varies)
- **Total**: ~$50-100/month (current baseline)

### Option 2 (Backend)
- **Server Costs**: Backend + Postgres + Redis + Letta
- **API Costs**: Deepgram + OpenAI (fast LLM ~$0.002/req) + Letta
- **Additional**: Postgres hosting (~$20/month), increased backend memory
- **Total**: ~$80-150/month (+50% increase)

### Option 3 (Hybrid)
- **Server Costs**: Backend (minimal increase) + n8n (existing)
- **API Costs**: Deepgram + OpenAI (alerts only) + Letta
- **Additional**: ~$10/month for extra OpenAI calls
- **Total**: ~$60-110/month (+20% increase)

**Verdict**: Option 3 has best cost-to-performance ratio

---

## FAQ

### Q: Can we use the existing Redis for chunk aggregation?
**A**: Yes, the n8n-redis container (172.21.0.4:6379) can be shared. Backend would need to use different key prefixes to avoid conflicts.

### Q: How do we handle "NA" responses from Letta?
**A**: Backend checks for "NA", "N/A", or "NO RESPONSE" in response text and doesn't send WebSocket message to iOS app (silent).

### Q: What if Letta API is down?
**A**: Backend catches exceptions and sends error type to iOS app. User sees "Agent unavailable" message. Conversation still saved to Firestore.

### Q: Can we switch between options later?
**A**: Yes, all three options can coexist:
- Option 1: Always available (webhook fallback)
- Option 3: Can be enabled/disabled via feature flag
- Option 2: Can replace Option 3 incrementally

### Q: How do we test without affecting production?
**A**: Use separate test user IDs and test agent IDs in Postgres. Backend supports multiple environments via .env configuration.

---

## Next Steps

1. **User Decision**: Choose integration option (1, 2, or 3)
2. **Confirm Architecture**:
   - Verify Postgres schema for agent lookup
   - Confirm Redis chunk aggregation logic
   - Verify Letta API endpoints and authentication
3. **Implementation**: Follow checklist for chosen option
4. **Testing**: Use test scripts to verify each component
5. **Deployment**: Roll out to VPS with monitoring
6. **Iteration**: Monitor latency and optimize as needed

---

**Status**: ✅ Architecture documented, awaiting implementation decision
**Last Updated**: October 30, 2025
**Contact**: Backend team for questions

