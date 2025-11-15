# Backend Developer Instructions: E2E Agent Testing (REVISED)

**Date:** November 15, 2025
**Task:** Implement test endpoints that call REAL production n8n Letta agents
**Feature Branch:** `feature/backend-e2e-agent-testing`
**Base Branch:** `main` (TTS push notifications merged)
**Estimated Time:** 4-6 hours (Option B - Audio+Text, recommended)

---

## 🔄 **REVISION NOTES**

**What Changed from Original Instructions:**
- ❌ **REMOVED**: Fake emergency scanner with hardcoded keywords
- ❌ **REMOVED**: `backend/utils/emergency_scanner.py` (not needed)
- ✅ **ADDED**: Real n8n agent webhook calls
- ✅ **ADDED**: Audio→STT handling (reuse Edge ASR code)
- ✅ **ADDED**: Real performance metrics (latency, cache stats)
- ✅ **ADDED**: Four agent endpoints instead of two

**Why This is Better:**
- Tests ACTUAL production code (Scanner, Memory, Summary, Chat agents)
- Provides real latency metrics and cache hit rates
- Validates full audio→STT→Agent pipeline
- More valuable for stakeholders
- Backend Dev recommended this approach

---

## 🎯 What You're Building

Four test endpoints that are **synchronous wrappers** around real production n8n Letta agents:

1. **POST /v1/test/scanner-agent** - Tests urgency detection (wake words, emergencies)
2. **POST /v1/test/memory-agent** - Tests memory extraction
3. **POST /v1/test/summary-agent** - Tests daily summaries
4. **POST /v1/test/chat-agent** - Tests conversational AI

These are TEST-ONLY endpoints that allow iOS to validate the full pipeline: Audio → STT → Agent → Response

---

## 🚀 Getting Started

### 1. Pull Latest Main
```bash
cd /root/omi/backend  # Or your backend directory
git checkout main
git pull origin main
```

### 2. Create Feature Branch
```bash
git checkout -b feature/backend-e2e-agent-testing
```

### 3. Verify n8n Agents are Running
```bash
# Test Scanner Agent
curl -X POST "https://n8n.ella-ai-care.com/webhook/scanner-agent" \
  -H "Content-Type: application/json" \
  -d '{"text":"I am having chest pain","conversation_id":"test_123","user_id":"test_user"}'

# Expected: {"urgency_level":"critical","detected_event":"cardiac_emergency",...}

# Test Memory Agent
curl -X POST "https://n8n.ella-ai-care.com/webhook/memory-agent" \
  -H "Content-Type: application/json" \
  -d '{"text":"I had lunch with Sarah at noon","conversation_id":"test_123","user_id":"test_user"}'

# Expected: {"memories":[{...}],...}

# Test Summary Agent
curl -X POST "https://n8n.ella-ai-care.com/webhook/summary-agent" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"test_123","user_id":"test_user","date":"2025-11-15"}'

# Expected: {"summary":"...","key_points":[...],...}

# Test Chat Agent (verify endpoint exists)
curl -X POST "https://n8n.ella-ai-care.com/webhook/chat-agent" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the weather today?","conversation_id":"test_123","user_id":"test_user"}'

# Expected: {"response":"..."}
# If 404: Chat Agent endpoint doesn't exist yet, skip for now
```

---

## 📝 Endpoints to Implement

### **Endpoint 1: Test Scanner Agent**

**URL:** `POST /v1/test/scanner-agent`

**Purpose:** Test urgency detection (emergencies, wake words)

**Request:**
```json
{
  "audio": "base64_encoded_audio_wav",  // Optional if text provided
  "text": "I'm having chest pain",      // Optional if audio provided
  "source": "phone_mic",
  "conversation_id": "test_conv_123"    // Optional, defaults to "test_conv"
}
```

**Response:**
```json
{
  "test_type": "scanner_agent",
  "source": "phone_mic",
  "transcript": "I'm having chest pain",
  "agent_response": {
    "urgency_level": "critical",
    "detected_event": "cardiac_emergency",
    "explanation": "User reported chest pain...",
    "recommended_action": "Call 911 immediately",
    "confidence": 0.95
  },
  "metrics": {
    "stt_latency_ms": 450.23,
    "agent_latency_ms": 850.12,
    "total_latency_ms": 2500.45,
    "stt_provider": "deepgram",
    "agent_endpoint": "scanner-agent"
  }
}
```

**Implementation:**
```python
# backend/routers/testing.py

import time
import base64
import requests
from typing import Optional
from fastapi import APIRouter, Body, HTTPException, Depends

from utils.other import endpoints as auth
from utils.stt import deepgram_stt  # Your existing STT service

router = APIRouter()


@router.post("/v1/test/scanner-agent")
async def test_scanner_agent(
    audio: Optional[str] = Body(None, description="Base64 encoded audio WAV"),
    text: Optional[str] = Body(None, description="Text to test (if no audio)"),
    source: str = Body("phone_mic", description="Audio source"),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Scanner Agent (urgency detection)
    Calls REAL production agent at n8n.ella-ai-care.com
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()
    stt_latency_ms = 0

    # Step 1: Convert audio to text if needed
    if audio:
        try:
            audio_data = base64.b64decode(audio)
            stt_start = time.time()
            transcript = await deepgram_stt.transcribe(audio_data)
            stt_latency_ms = (time.time() - stt_start) * 1000
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"STT failed: {e}")
    else:
        transcript = text

    # Step 2: Call REAL Scanner Agent via n8n
    agent_start = time.time()
    try:
        response = requests.post(
            "https://n8n.ella-ai-care.com/webhook/scanner-agent",
            json={
                "text": transcript,
                "conversation_id": conversation_id,
                "user_id": uid
            },
            timeout=10  # 10 second timeout
        )
        response.raise_for_status()
        agent_result = response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Agent call failed: {e}")

    agent_latency_ms = (time.time() - agent_start) * 1000
    total_latency_ms = (time.time() - start_time) * 1000

    # Step 3: Return results with metrics
    return {
        "test_type": "scanner_agent",
        "source": source,
        "transcript": transcript,
        "agent_response": agent_result,  # Real agent response
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2) if audio else 0,
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "stt_provider": "deepgram" if audio else None,
            "agent_endpoint": "scanner-agent",
        }
    }
```

---

### **Endpoint 2: Test Memory Agent**

**URL:** `POST /v1/test/memory-agent`

**Purpose:** Test memory extraction from conversations

**Request:** Same structure as Scanner Agent

**Response:**
```json
{
  "test_type": "memory_agent",
  "source": "phone_mic",
  "transcript": "I had lunch with Sarah at noon",
  "agent_response": {
    "memories": [
      {
        "content": "Had lunch with Sarah",
        "category": "social",
        "timestamp": "2025-11-15T12:00:00Z",
        "participants": ["Sarah"]
      }
    ],
    "total_memories": 1
  },
  "metrics": {
    "stt_latency_ms": 420.15,
    "agent_latency_ms": 1200.45,
    "total_latency_ms": 3100.78
  }
}
```

**Implementation:**
```python
@router.post("/v1/test/memory-agent")
async def test_memory_agent(
    audio: Optional[str] = Body(None),
    text: Optional[str] = Body(None),
    source: str = Body("phone_mic"),
    conversation_id: str = Body("test_conv"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Memory Agent (memory extraction)
    Calls REAL production agent at n8n.ella-ai-care.com
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()

    # Step 1: STT if needed
    if audio:
        audio_data = base64.b64decode(audio)
        stt_start = time.time()
        transcript = await deepgram_stt.transcribe(audio_data)
        stt_latency_ms = (time.time() - stt_start) * 1000
    else:
        transcript = text
        stt_latency_ms = 0

    # Step 2: Call REAL Memory Agent
    agent_start = time.time()
    try:
        response = requests.post(
            "https://n8n.ella-ai-care.com/webhook/memory-agent",
            json={
                "text": transcript,
                "conversation_id": conversation_id,
                "user_id": uid
            },
            timeout=10
        )
        response.raise_for_status()
        agent_result = response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Memory agent failed: {e}")

    agent_latency_ms = (time.time() - agent_start) * 1000
    total_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "memory_agent",
        "source": source,
        "transcript": transcript,
        "agent_response": agent_result,
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2),
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "stt_provider": "deepgram" if audio else None,
            "agent_endpoint": "memory-agent",
        }
    }
```

---

### **Endpoint 3: Test Summary Agent**

**URL:** `POST /v1/test/summary-agent`

**Purpose:** Test daily conversation summarization

**Request:**
```json
{
  "conversation_id": "test_conv_123",
  "date": "2025-11-15"  // Optional, defaults to today
}
```

**Response:**
```json
{
  "test_type": "summary_agent",
  "agent_response": {
    "summary": "User discussed various topics including...",
    "key_points": [
      "Had lunch with Sarah",
      "Discussed new project",
      "Scheduled doctor appointment"
    ],
    "sentiment": "positive",
    "word_count": 150
  },
  "metrics": {
    "agent_latency_ms": 1500.23,
    "total_latency_ms": 1500.23
  }
}
```

**Implementation:**
```python
@router.post("/v1/test/summary-agent")
async def test_summary_agent(
    conversation_id: str = Body("test_conv"),
    date: Optional[str] = Body(None, description="YYYY-MM-DD format"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Summary Agent (daily summaries)
    Calls REAL production agent at n8n.ella-ai-care.com
    """
    from datetime import datetime

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    start_time = time.time()

    # Call REAL Summary Agent
    try:
        response = requests.post(
            "https://n8n.ella-ai-care.com/webhook/summary-agent",
            json={
                "conversation_id": conversation_id,
                "user_id": uid,
                "date": date
            },
            timeout=15  # Summary may take longer
        )
        response.raise_for_status()
        agent_result = response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Summary agent failed: {e}")

    agent_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "summary_agent",
        "agent_response": agent_result,
        "metrics": {
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(agent_latency_ms, 2),
            "agent_endpoint": "summary-agent",
        }
    }
```

---

### **Endpoint 4: Test Chat Agent**

**URL:** `POST /v1/test/chat-agent`

**Purpose:** Test conversational AI

**Note:** First verify `https://n8n.ella-ai-care.com/webhook/chat-agent` exists

**Request:** Same structure as Scanner Agent

**Response:**
```json
{
  "test_type": "chat_agent",
  "transcript": "What's the weather today?",
  "agent_response": {
    "response": "I don't have access to real-time weather data...",
    "context_used": ["user_location", "previous_conversations"],
    "confidence": 0.85
  },
  "metrics": {
    "stt_latency_ms": 430.12,
    "agent_latency_ms": 950.34,
    "total_latency_ms": 2200.56
  }
}
```

**Implementation:**
```python
@router.post("/v1/test/chat-agent")
async def test_chat_agent(
    audio: Optional[str] = Body(None),
    text: Optional[str] = Body(None),
    source: str = Body("phone_mic"),
    conversation_id: str = Body("test_conv"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Chat Agent (conversational AI)
    Calls REAL production agent at n8n.ella-ai-care.com

    NOTE: Verify chat-agent endpoint exists first!
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()

    # Step 1: STT if needed
    if audio:
        audio_data = base64.b64decode(audio)
        stt_start = time.time()
        transcript = await deepgram_stt.transcribe(audio_data)
        stt_latency_ms = (time.time() - stt_start) * 1000
    else:
        transcript = text
        stt_latency_ms = 0

    # Step 2: Call REAL Chat Agent
    agent_start = time.time()
    try:
        response = requests.post(
            "https://n8n.ella-ai-care.com/webhook/chat-agent",
            json={
                "message": transcript,
                "conversation_id": conversation_id,
                "user_id": uid
            },
            timeout=10
        )
        response.raise_for_status()
        agent_result = response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Chat agent failed: {e}")

    agent_latency_ms = (time.time() - agent_start) * 1000
    total_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "chat_agent",
        "source": source,
        "transcript": transcript,
        "agent_response": agent_result,
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2),
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "stt_provider": "deepgram" if audio else None,
            "agent_endpoint": "chat-agent",
        }
    }
```

---

## 📂 File Structure

```
backend/
├── routers/
│   └── testing.py          # NEW - All 4 test endpoints
└── tests/
    └── test_agents.py      # NEW - Automated tests
```

**NOTE:** No `utils/emergency_scanner.py` needed - we're calling real agents!

---

## 🔗 Register Router in Main

**File:** `backend/main.py`

Add after other router imports:

```python
from routers import testing

# ... existing routers ...

# E2E Agent Testing endpoints
app.include_router(testing.router, tags=['testing'])
```

---

## 🧪 Testing Your Implementation

### **Manual Testing with curl**

**Test Scanner Agent (Text):**
```bash
curl -X POST "http://localhost:8000/v1/test/scanner-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I am having chest pain",
    "source": "phone_mic",
    "conversation_id": "test_123"
  }'

# Expected: urgency_level: "critical", detected_event: "cardiac_emergency"
```

**Test Memory Agent (Text):**
```bash
curl -X POST "http://localhost:8000/v1/test/memory-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon and we discussed the new project",
    "source": "phone_mic"
  }'

# Expected: memories array with social and work memories
```

**Test Summary Agent:**
```bash
curl -X POST "http://localhost:8000/v1/test/summary-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "real_conv_id_here",
    "date": "2025-11-15"
  }'

# Expected: Daily summary with key points
```

**Test Chat Agent (Text):**
```bash
curl -X POST "http://localhost:8000/v1/test/chat-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "What is the weather today?",
    "source": "phone_mic"
  }'

# Expected: Conversational response
```

### **Test with Audio (Base64)**

```bash
# Record audio file (WAV format)
# Convert to base64
AUDIO_BASE64=$(base64 -i test_audio.wav)

curl -X POST "http://localhost:8000/v1/test/scanner-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"audio\": \"$AUDIO_BASE64\",
    \"source\": \"phone_mic\"
  }"

# Expected: transcript + agent response
```

---

## 📦 Commit and Push

```bash
git add backend/routers/testing.py
git add backend/main.py
git add backend/tests/test_agents.py  # If you added tests

git commit -m "feat(testing): add E2E agent testing endpoints calling real n8n agents

Implemented test endpoints that call REAL production Letta agents:
- POST /v1/test/scanner-agent: Tests urgency detection via n8n
- POST /v1/test/memory-agent: Tests memory extraction via n8n
- POST /v1/test/summary-agent: Tests daily summaries via n8n
- POST /v1/test/chat-agent: Tests conversational AI via n8n

Features:
- Audio→STT→Agent pipeline (reuses Edge ASR code)
- Real latency metrics (STT, agent, total)
- Synchronous wrappers around n8n webhooks
- Comprehensive error handling

Endpoints Called:
- https://n8n.ella-ai-care.com/webhook/scanner-agent
- https://n8n.ella-ai-care.com/webhook/memory-agent
- https://n8n.ella-ai-care.com/webhook/summary-agent
- https://n8n.ella-ai-care.com/webhook/chat-agent

Files Added:
- backend/routers/testing.py (test endpoints)

Files Modified:
- backend/main.py (router registration)

Testing:
- All endpoints tested with curl
- Audio and text modes verified
- Ready for iOS integration

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"

git push -u origin feature/backend-e2e-agent-testing
```

---

## 🚦 Ready for iOS Integration

Once pushed, iOS developer can:
1. Call test endpoints from Developer Settings
2. Test all 4 agent types
3. Use both audio and text inputs
4. View real agent responses and latency metrics

**Coordination:** iOS will work on UI while you implement this. Test together once both sides are done.

---

## ❓ Questions to Resolve

1. **Chat Agent Endpoint** - Does `https://n8n.ella-ai-care.com/webhook/chat-agent` exist?
   - If YES: Implement `/v1/test/chat-agent`
   - If NO: Skip Chat Agent for now, document in commit

2. **n8n Authentication** - Do webhooks require auth headers?
   - If YES: Add auth to requests.post() calls
   - If NO: Current implementation is correct

3. **Error Response Format** - What does n8n return on errors?
   - Need to handle timeouts, 500 errors, etc.
   - May need to adjust error handling

---

## 🔄 Differences from Original Instructions

| Aspect | Original | Revised |
|--------|----------|---------|
| **Endpoints** | `/v1/test/wake-word`, `/v1/test/emergency` | `/v1/test/scanner-agent`, `/v1/test/memory-agent`, etc. |
| **Backend Logic** | `if "chest pain" in text:` | `requests.post("n8n.ella-ai-care.com/webhook/...")` |
| **Files Created** | `utils/emergency_scanner.py` | None (call real agents) |
| **Response** | Fake confidence scores | Real agent responses |
| **Value** | Tests fake heuristics | Tests PRODUCTION code |
| **Audio Handling** | Optional | Recommended (tests full pipeline) |

---

**Estimated Time:** 4-6 hours
**Priority:** High (enables critical AI safety testing)
**Status:** Ready to implement

**Backend Dev Feedback:** "REBUILD from scratch, test REAL agents, don't waste time with fake heuristics"

Good luck! 🚀
