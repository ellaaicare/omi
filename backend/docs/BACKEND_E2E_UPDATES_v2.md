# Backend E2E Testing - Key Updates (v2)

**Date:** November 15, 2025
**Changes:** Chat endpoint updates + generic naming
**Time Estimate:** 6-8 hours (updated from 4-6h)

---

## ⚡ Quick Summary of Changes

### 1. **Generic Chat Endpoint** ✅
- Use: `https://n8n.ella-ai-care.com/webhook/chat-agent`
- NOT: `omi-realtime` or any "omi-" prefix
- Reason: Keep naming consistent with n8n team

### 2. **Chat has TWO Patterns** ✅
- **Sync Chat** (`/v1/test/chat-sync`) - 1 hour to implement
- **Async Chat** (`/v1/test/chat-async` + polling) - 3-4 hours to implement
- Reason: Handle slow chat responses gracefully

### 3. **Total Endpoints: 6** ✅
1. `/v1/test/scanner-agent` (sync)
2. `/v1/test/memory-agent` (sync)
3. `/v1/test/summary-agent` (sync)
4. `/v1/test/chat-sync` (sync, 30s timeout)
5. `/v1/test/chat-async` (async submit, returns job_id)
6. `/v1/test/chat-response/{job_id}` (async poll)

### 4. **Updated Time Estimate** ⏱️
- **6-8 hours total** (was 4-6h)
- Breakdown:
  - Scanner: 1h
  - Memory: 1h
  - Summary: 1h
  - Chat Sync: 1h
  - Chat Async + Polling: 3-4h

---

## 📝 Chat Sync Implementation (Simple)

**Endpoint:** `POST /v1/test/chat-sync`

**Code:**
```python
@router.post("/v1/test/chat-sync")
async def test_chat_sync(
    audio: Optional[str] = Body(None),
    text: Optional[str] = Body(None),
    source: str = Body("phone_mic"),
    conversation_id: str = Body("test_conv"),
    uid: str = Depends(auth.get_current_user_uid),
):
    start_time = time.time()

    # Step 1: STT if audio provided
    if audio:
        audio_data = base64.b64decode(audio)
        transcript = await deepgram_stt.transcribe(audio_data)
        stt_latency_ms = (time.time() - start_time) * 1000
    else:
        transcript = text
        stt_latency_ms = 0

    # Step 2: Call REAL Chat Agent (synchronous)
    agent_start = time.time()
    response = requests.post(
        "https://n8n.ella-ai-care.com/webhook/chat-agent",  # ← GENERIC NAME
        json={
            "text": transcript,
            "uid": uid,
            "session_id": conversation_id
        },
        timeout=30  # 30s timeout for sync
    )
    agent_latency_ms = (time.time() - agent_start) * 1000

    return {
        "test_type": "chat_sync",
        "transcript": transcript,
        "agent_response": response.json(),
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2),
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round((time.time() - start_time) * 1000, 2),
            "mode": "synchronous"
        }
    }
```

**Test:**
```bash
curl -X POST "http://localhost:8000/v1/test/chat-sync" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the weather?", "source": "phone_mic"}'

# Expected: Immediate response with chat text
```

---

## 📝 Chat Async Implementation (Advanced)

**Endpoints:**
- `POST /v1/test/chat-async` - Submit job
- `GET /v1/test/chat-response/{job_id}` - Poll for result

**Code:**
```python
# Global job storage (use Redis in production)
chat_jobs = {}

@router.post("/v1/test/chat-async")
async def test_chat_async(
    audio: Optional[str] = Body(None),
    text: Optional[str] = Body(None),
    source: str = Body("phone_mic"),
    conversation_id: str = Body("test_conv"),
    uid: str = Depends(auth.get_current_user_uid),
):
    start_time = time.time()
    job_id = str(uuid.uuid4())

    # Step 1: STT if audio provided
    if audio:
        audio_data = base64.b64decode(audio)
        transcript = await deepgram_stt.transcribe(audio_data)
    else:
        transcript = text

    # Step 2: Queue async processing
    chat_jobs[job_id] = {
        "uid": uid,
        "transcript": transcript,
        "status": "processing",
        "started_at": time.time()
    }

    # Step 3: Process in background
    asyncio.create_task(
        _call_chat_agent_async(
            job_id=job_id,
            text=transcript,
            uid=uid,
            conversation_id=conversation_id
        )
    )

    queue_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "chat_async",
        "job_id": job_id,
        "status": "processing",
        "poll_url": f"/v1/test/chat-response/{job_id}",
        "poll_interval_ms": 500,
        "metrics": {
            "queue_latency_ms": round(queue_latency_ms, 2)
        }
    }


async def _call_chat_agent_async(job_id: str, text: str, uid: str, conversation_id: str):
    """Background task to call chat agent"""
    try:
        agent_start = time.time()
        response = requests.post(
            "https://n8n.ella-ai-care.com/webhook/chat-agent",  # ← GENERIC NAME
            json={
                "text": text,
                "uid": uid,
                "session_id": conversation_id
            },
            timeout=120  # 2 minute timeout for async
        )
        agent_latency_ms = (time.time() - agent_start) * 1000

        # Update job with result
        chat_jobs[job_id]["status"] = "completed"
        chat_jobs[job_id]["response"] = response.json()
        chat_jobs[job_id]["agent_latency_ms"] = agent_latency_ms

    except Exception as e:
        chat_jobs[job_id]["status"] = "failed"
        chat_jobs[job_id]["error"] = str(e)


@router.get("/v1/test/chat-response/{job_id}")
def get_chat_response(
    job_id: str,
    uid: str = Depends(auth.get_current_user_uid)
):
    """iOS polls this to get async result"""
    if job_id not in chat_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = chat_jobs[job_id]

    if job["status"] == "processing":
        return {"status": "processing", "job_id": job_id}

    elif job["status"] == "completed":
        total_latency_ms = (time.time() - job["started_at"]) * 1000
        return {
            "status": "completed",
            "job_id": job_id,
            "transcript": job["transcript"],
            "agent_response": job["response"],
            "metrics": {
                "agent_latency_ms": job["agent_latency_ms"],
                "total_latency_ms": round(total_latency_ms, 2)
            }
        }

    else:  # failed
        return {
            "status": "failed",
            "job_id": job_id,
            "error": job.get("error")
        }
```

**Test:**
```bash
# Submit async job
RESPONSE=$(curl -X POST "http://localhost:8000/v1/test/chat-async" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the weather?", "source": "phone_mic"}')

# Extract job_id
JOB_ID=$(echo $RESPONSE | jq -r '.job_id')

# Poll for result
while true; do
  RESULT=$(curl "http://localhost:8000/v1/test/chat-response/$JOB_ID" \
    -H "Authorization: Bearer ADMIN_KEY")

  STATUS=$(echo $RESULT | jq -r '.status')

  if [ "$STATUS" = "completed" ]; then
    echo "✅ Completed!"
    echo $RESULT | jq '.'
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "❌ Failed!"
    echo $RESULT | jq '.'
    break
  else
    echo "⏳ Processing..."
    sleep 0.5
  fi
done
```

---

## 🔄 Updates to Existing Instructions

### File Structure (Updated)
```
backend/
├── routers/
│   └── testing.py          # 6 endpoints (was 4)
└── tests/
    └── test_agents.py      # Optional automated tests
```

**NO** `utils/emergency_scanner.py` - we call real agents!

### Router Registration (Same)
```python
# backend/main.py
from routers import testing

app.include_router(testing.router, tags=['testing'])
```

### Import Additions
```python
# backend/routers/testing.py
import uuid          # For job_id generation
import asyncio       # For background tasks
```

---

## ✅ Testing Checklist

**Scanner, Memory, Summary** - Same as before:
```bash
curl -X POST "http://localhost:8000/v1/test/scanner-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "I am having chest pain", "source": "phone_mic"}'
```

**Chat Sync** - New:
```bash
curl -X POST "http://localhost:8000/v1/test/chat-sync" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the weather?", "source": "phone_mic"}'

# Expected: Immediate response with chat_response.text
```

**Chat Async** - New:
```bash
# 1. Submit job
curl -X POST "http://localhost:8000/v1/test/chat-async" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the weather?", "source": "phone_mic"}'

# Expected: {"job_id": "550e8400-...", "status": "processing", ...}

# 2. Poll for result
curl "http://localhost:8000/v1/test/chat-response/550e8400-..." \
  -H "Authorization: Bearer ADMIN_KEY"

# Expected (processing): {"status": "processing", "job_id": "..."}
# Expected (completed): {"status": "completed", "agent_response": {...}, ...}
```

---

## 📦 Commit Message (Updated)

```bash
git commit -m "feat(testing): add E2E agent testing endpoints with sync/async chat patterns

Implemented test endpoints calling REAL production n8n Letta agents:
- POST /v1/test/scanner-agent: Urgency detection via n8n
- POST /v1/test/memory-agent: Memory extraction via n8n
- POST /v1/test/summary-agent: Daily summaries via n8n
- POST /v1/test/chat-sync: Synchronous chat (30s timeout)
- POST /v1/test/chat-async: Asynchronous chat (120s timeout)
- GET /v1/test/chat-response/{job_id}: Async polling endpoint

Features:
- Audio→STT→Agent pipeline (reuses Edge ASR code)
- Real latency metrics (STT, agent, total)
- Synchronous wrappers around n8n webhooks
- Async pattern with job queue for slow chat responses
- Generic naming (chat-agent, not omi-specific)

Endpoints Called:
- https://n8n.ella-ai-care.com/webhook/scanner-agent
- https://n8n.ella-ai-care.com/webhook/memory-agent
- https://n8n.ella-ai-care.com/webhook/summary-agent
- https://n8n.ella-ai-care.com/webhook/chat-agent (sync + async)

Files Added:
- backend/routers/testing.py (6 test endpoints)

Files Modified:
- backend/main.py (router registration)

Testing:
- All endpoints tested with curl
- Sync chat: immediate response
- Async chat: polling pattern verified
- Ready for iOS integration

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 🎯 Summary

**Key Changes:**
1. ✅ Generic endpoint: `webhook/chat-agent` (NOT `omi-realtime`)
2. ✅ Two chat patterns: sync (simple) + async (production-ready)
3. ✅ 6 total endpoints (was 4)
4. ✅ Updated time: 6-8 hours (was 4-6h)

**Implementation Priority:**
1. Scanner, Memory, Summary (3 hours) - straightforward
2. Chat Sync (1 hour) - simple, same as others
3. Chat Async + Polling (3-4 hours) - more complex, async infrastructure

**Reference Full PRD:** `/tmp/E2E_TESTING_PRD_REVISED.md` (all 713 lines)

Ready to implement! 🚀
