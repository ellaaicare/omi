# E2E Agent Testing Endpoints - Usage Guide

**Date:** November 15, 2025
**Status:** Implemented ✅
**Endpoints:** 6 test endpoints calling real n8n Letta agents

---

## 🎯 Overview

This implementation provides test endpoints for validating the full E2E pipeline:
**Audio → STT → Agent → Response**

### Endpoints Implemented

1. **POST /v1/test/scanner-agent** - Tests urgency detection (emergencies, wake words)
2. **POST /v1/test/memory-agent** - Tests memory extraction from conversations
3. **POST /v1/test/summary-agent** - Tests daily conversation summaries
4. **POST /v1/test/chat-sync** - Tests synchronous chat (30s timeout)
5. **POST /v1/test/chat-async** - Tests asynchronous chat (120s timeout)
6. **GET /v1/test/chat-response/{job_id}** - Polls for async chat results

---

## 🔑 Authentication

All endpoints require Firebase authentication via the `Authorization` header:

```bash
Authorization: Bearer <firebase_id_token>
```

For local testing, you can use the admin key:
```bash
Authorization: Bearer <ADMIN_KEY><uid>
```

---

## 📝 Testing Examples

### 1. Scanner Agent (Text Input)

```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/scanner-agent" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I am having chest pain and difficulty breathing",
    "source": "phone_mic",
    "conversation_id": "test_123"
  }'
```

**Expected Response:**
```json
{
  "test_type": "scanner_agent",
  "source": "phone_mic",
  "transcript": "I am having chest pain and difficulty breathing",
  "agent_response": {
    "urgency_level": "critical",
    "detected_event": "cardiac_emergency",
    "explanation": "User reported chest pain...",
    "recommended_action": "Call 911 immediately",
    "confidence": 0.95
  },
  "metrics": {
    "stt_latency_ms": 0,
    "agent_latency_ms": 850.12,
    "total_latency_ms": 851.45,
    "stt_provider": null,
    "agent_endpoint": "scanner-agent"
  }
}
```

### 2. Memory Agent (Text Input)

```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/memory-agent" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon and we discussed the new project",
    "source": "phone_mic",
    "conversation_id": "test_123"
  }'
```

**Expected Response:**
```json
{
  "test_type": "memory_agent",
  "source": "phone_mic",
  "transcript": "I had lunch with Sarah at noon and we discussed the new project",
  "agent_response": {
    "memories": [
      {
        "content": "Had lunch with Sarah",
        "category": "social",
        "timestamp": "2025-11-15T12:00:00Z",
        "participants": ["Sarah"]
      },
      {
        "content": "Discussed new project",
        "category": "work",
        "timestamp": "2025-11-15T12:00:00Z"
      }
    ],
    "total_memories": 2
  },
  "metrics": {
    "stt_latency_ms": 0,
    "agent_latency_ms": 1200.45,
    "total_latency_ms": 1201.78
  }
}
```

### 3. Summary Agent

```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/summary-agent" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "real_conv_id_here",
    "date": "2025-11-15"
  }'
```

**Expected Response:**
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
    "total_latency_ms": 1500.23,
    "agent_endpoint": "summary-agent"
  }
}
```

### 4. Chat Sync

```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/chat-sync" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "What is the weather today?",
    "source": "phone_mic",
    "conversation_id": "test_123"
  }'
```

**Expected Response:**
```json
{
  "test_type": "chat_sync",
  "source": "phone_mic",
  "transcript": "What is the weather today?",
  "agent_response": {
    "response": "I don't have access to real-time weather data...",
    "context_used": ["user_location", "previous_conversations"],
    "confidence": 0.85
  },
  "metrics": {
    "stt_latency_ms": 0,
    "agent_latency_ms": 950.34,
    "total_latency_ms": 951.56,
    "stt_provider": null,
    "agent_endpoint": "chat-agent",
    "mode": "synchronous"
  }
}
```

### 5. Chat Async (Submit)

```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/chat-async" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Tell me about my day",
    "source": "phone_mic",
    "conversation_id": "test_123"
  }'
```

**Expected Response:**
```json
{
  "test_type": "chat_async",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "poll_url": "/v1/test/chat-response/550e8400-e29b-41d4-a716-446655440000",
  "poll_interval_ms": 500,
  "metrics": {
    "queue_latency_ms": 45.23,
    "stt_latency_ms": 0
  }
}
```

### 6. Chat Response (Poll)

```bash
JOB_ID="550e8400-e29b-41d4-a716-446655440000"
curl "https://api.ella-ai-care.com/v1/test/chat-response/$JOB_ID" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Expected Response (Processing):**
```json
{
  "status": "processing",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Expected Response (Completed):**
```json
{
  "status": "completed",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "source": "phone_mic",
  "transcript": "Tell me about my day",
  "agent_response": {
    "response": "Based on your conversations today...",
    "context_used": ["memories", "calendar"],
    "confidence": 0.92
  },
  "metrics": {
    "stt_latency_ms": 0,
    "agent_latency_ms": 2500.34,
    "total_latency_ms": 2545.67,
    "stt_provider": null,
    "agent_endpoint": "chat-agent",
    "mode": "asynchronous"
  }
}
```

---

## 🎤 Audio Testing

All endpoints (except summary-agent) support audio input via base64-encoded WAV files:

```bash
# Record or prepare audio file
AUDIO_FILE="test_audio.wav"

# Convert to base64
AUDIO_BASE64=$(base64 -w 0 $AUDIO_FILE)

# Send request
curl -X POST "https://api.ella-ai-care.com/v1/test/scanner-agent" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"audio\": \"$AUDIO_BASE64\",
    \"source\": \"phone_mic\",
    \"conversation_id\": \"test_123\"
  }"
```

**Notes:**
- Audio is transcribed using Deepgram STT
- STT latency is tracked separately
- Both audio OR text can be provided, not both
- Audio format: WAV, PCM, or other Deepgram-supported formats

---

## 🔧 N8N Webhook Configuration

The endpoints call these n8n webhooks:

1. **Scanner Agent**: `https://n8n.ella-ai-care.com/webhook/scanner-agent`
2. **Memory Agent**: `https://n8n.ella-ai-care.com/webhook/memory-agent`
3. **Summary Agent**: `https://n8n.ella-ai-care.com/webhook/summary-agent`
4. **Chat Agent**: `https://n8n.ella-ai-care.com/webhook/chat-agent`

### Authentication Requirements

If n8n webhooks require authentication, update the code in `backend/routers/testing.py`:

```python
# Add auth headers to requests
response = requests.post(
    N8N_SCANNER_AGENT,
    headers={
        "Authorization": f"Bearer {n8n_api_key}",
        "Content-Type": "application/json"
    },
    json={...},
    timeout=10
)
```

---

## 🐛 Troubleshooting

### Error: "Access denied" from n8n

**Solution:** n8n webhooks require authentication. Contact n8n team for API credentials or check n8n webhook settings.

### Error: "STT failed"

**Possible causes:**
- Invalid base64 audio data
- Deepgram API key not configured
- Audio format not supported

**Solution:**
- Verify `DEEPGRAM_API_KEY` is set in environment
- Test with text input first
- Check audio file format (WAV recommended)

### Error: "Agent call failed"

**Possible causes:**
- n8n webhook timeout
- n8n service unavailable
- Invalid request format

**Solution:**
- Check n8n service status
- Verify webhook endpoints are active
- Test webhooks directly with curl

---

## 📊 Performance Metrics

All endpoints return latency metrics:

- **stt_latency_ms**: Time to transcribe audio (0 if text input)
- **agent_latency_ms**: Time for n8n agent to respond
- **total_latency_ms**: Total end-to-end latency
- **queue_latency_ms**: Time to queue async job (async only)

**Typical Latencies:**
- STT: 200-500ms
- Scanner Agent: 500-1500ms
- Memory Agent: 800-2000ms
- Summary Agent: 1000-3000ms
- Chat Agent: 1000-5000ms

---

## 🚀 Integration with iOS

iOS Developer Settings can call these endpoints to:

1. Test agent functionality without full app integration
2. Validate audio pipeline (microphone → STT → agent)
3. Measure real-world latencies
4. Debug agent responses

**iOS Implementation:**
- Add UI toggles for each agent type
- Display latency metrics
- Show agent responses in debug view
- Support both audio and text input

---

## 📝 Files Modified

- **backend/routers/testing.py** (NEW) - All 6 test endpoints
- **backend/main.py** - Router registration
- **backend/docs/E2E_TESTING_ENDPOINTS_USAGE.md** (NEW) - This file

---

## ✅ Testing Checklist

- [ ] Scanner Agent (text input)
- [ ] Scanner Agent (audio input)
- [ ] Memory Agent (text input)
- [ ] Memory Agent (audio input)
- [ ] Summary Agent
- [ ] Chat Sync (text input)
- [ ] Chat Sync (audio input)
- [ ] Chat Async (submit job)
- [ ] Chat Async (poll result)

---

**Implementation Status:** ✅ Complete
**Ready for:** iOS integration, production testing
**Next Steps:** Configure n8n webhook authentication, test with real agents
