# Chat Endpoint: Callback Requirements & Implementation Guide

**Status**: Analysis complete, based on Ella Dev review [11/15 22:00 UTC]
**Objective**: Implement proper callback handling for `/webhook/omi-realtime` chat endpoint

---

## Overview

The chat endpoint (`POST /webhook/omi-realtime`) requires sophisticated callback handling to support:
- Real-time responses to iOS app
- Asynchronous agent processing
- Multiple response patterns (sync, async, streaming)
- Error handling and timeouts

---

## Architecture: Three Response Patterns

### Pattern 1: Synchronous Request-Response (Simplest)

```
iOS App
  ↓ POST /webhook/omi-realtime with text
n8n Chat Agent
  ↓ Process (fast agent, <5s)
Response JSON
  ↓ Back to iOS immediately
```

**Use Case**: Quick chat responses, no long processing
**Latency**: 1-5 seconds
**Implementation**: Simple HTTP POST + await

**Code Example**:
```python
@router.post('/v1/chat/sync')
def chat_sync(
    request: ChatRequest,
    uid: str = Depends(auth.get_current_user_uid)
):
    """Synchronous chat: wait for response before returning"""

    response = requests.post(
        'https://n8n.ella-ai-care.com/webhook/omi-realtime',
        json={
            'text': request.text,
            'uid': uid,
            'session_id': request.session_id
        },
        timeout=30
    )

    return {
        'response': response.json().get('text'),
        'urgency': response.json().get('urgency_level'),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
```

---

### Pattern 2: Asynchronous with Callback (Recommended)

```
iOS App
  ↓ POST /v1/chat/async with text + callback_url
Backend
  ↓ Returns immediately with job_id
  ↓ Sends text to chat agent (background)
n8n Chat Agent
  ↓ Process (can be slower, up to 120s)
  ↓ POST to callback_url with response
Backend /v1/chat-callback
  ↓ Store result + notify iOS (push notification or WebSocket)
iOS App
  ↓ Receives real-time update
```

**Use Case**: Long-running agent processes, streaming responses
**Latency**: 100ms (initial response) + processing time
**Implementation**: Background task with callback

**Code Example**:
```python
from typing import Optional
import asyncio
import uuid

# Global storage for pending requests
chat_jobs = {}

@router.post('/v1/chat/async')
async def chat_async(
    request: ChatRequest,
    uid: str = Depends(auth.get_current_user_uid)
):
    """
    Async chat: return immediately, process in background

    The chat agent will POST to /v1/chat-callback/{job_id}
    when the response is ready
    """

    job_id = str(uuid.uuid4())

    # Store request metadata
    chat_jobs[job_id] = {
        'uid': uid,
        'text': request.text,
        'session_id': request.session_id,
        'status': 'processing',
        'created_at': datetime.now(timezone.utc),
        'response': None,
        'error': None
    }

    # Send to chat agent in background
    asyncio.create_task(
        process_chat_async(
            job_id=job_id,
            text=request.text,
            uid=uid,
            session_id=request.session_id
        )
    )

    return {
        'job_id': job_id,
        'status': 'processing',
        'message': 'Chat request queued. Response will be sent via callback.',
        'poll_url': f'/v1/chat/response/{job_id}',
        'poll_interval_ms': 500
    }


async def process_chat_async(
    job_id: str,
    text: str,
    uid: str,
    session_id: str
):
    """Background task: send to chat agent and handle response"""

    try:
        response = await asyncio.to_thread(
            requests.post,
            'https://n8n.ella-ai-care.com/webhook/omi-realtime',
            json={
                'text': text,
                'uid': uid,
                'session_id': session_id,
                'callback_url': f'{BACKEND_URL}/v1/chat-callback/{job_id}',
                'callback_timeout': 120  # seconds
            },
            timeout=130
        )

        # Update job status
        chat_jobs[job_id]['response'] = response.json()
        chat_jobs[job_id]['status'] = 'completed'

        # Notify iOS (via push notification or WebSocket)
        await notify_ios(uid, 'chat_response_ready', {
            'job_id': job_id,
            'response': response.json()
        })

    except Exception as e:
        chat_jobs[job_id]['error'] = str(e)
        chat_jobs[job_id]['status'] = 'failed'
        await notify_ios(uid, 'chat_response_error', {
            'job_id': job_id,
            'error': str(e)
        })


@router.post('/v1/chat-callback/{job_id}')
async def chat_callback(
    job_id: str,
    callback_data: ChatCallbackData
):
    """
    Webhook: Chat agent sends response here

    Called by n8n when chat agent finishes processing
    Can happen milliseconds or seconds after initial request

    Request body (from n8n):
    {
        "text": "Your response...",
        "urgency_level": "high",
        "action_items": [...],
        "metadata": {...}
    }
    """

    if job_id not in chat_jobs:
        return {'error': 'Job not found'}, 404

    job = chat_jobs[job_id]
    uid = job['uid']

    # Store response
    job['response'] = callback_data.dict()
    job['status'] = 'completed'
    job['completed_at'] = datetime.now(timezone.utc)

    # Save to Firestore
    conversations_db.add_chat_message(uid, {
        'job_id': job_id,
        'session_id': job['session_id'],
        'user_text': job['text'],
        'agent_response': callback_data.text,
        'urgency': callback_data.urgency_level,
        'action_items': callback_data.action_items,
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

    # Notify iOS immediately (push notification)
    await send_push_notification(uid, {
        'type': 'chat_response_received',
        'job_id': job_id,
        'preview': callback_data.text[:100],
        'urgency': callback_data.urgency_level
    })

    # Keep response in memory for 24 hours (or delete after iOS retrieves)
    # Schedule cleanup
    asyncio.create_task(cleanup_job_after(job_id, hours=24))

    return {'status': 'received', 'job_id': job_id}


@router.get('/v1/chat/response/{job_id}')
def get_chat_response(
    job_id: str,
    uid: str = Depends(auth.get_current_user_uid)
):
    """
    Poll endpoint: iOS can poll this to get response

    Response:
    {
        "status": "processing" | "completed" | "failed",
        "response": {...},  // Only if status == "completed"
        "error": "..."      // Only if status == "failed"
    }
    """

    if job_id not in chat_jobs:
        return {'error': 'Job not found'}, 404

    job = chat_jobs[job_id]

    # Verify uid owns this job
    if job['uid'] != uid:
        return {'error': 'Unauthorized'}, 403

    return {
        'status': job['status'],
        'response': job['response'] if job['status'] == 'completed' else None,
        'error': job['error'] if job['status'] == 'failed' else None,
        'created_at': job['created_at'].isoformat(),
        'completed_at': job.get('completed_at', '').isoformat() if job.get('completed_at') else None
    }
```

---

### Pattern 3: Streaming with WebSocket (Most Complex)

```
iOS App
  ↓ WebSocket /v1/chat/stream with text
Backend
  ↓ Send to chat agent
n8n Chat Agent
  ↓ Stream chunks back to callback_url
Backend
  ↓ Stream chunks to iOS WebSocket client (real-time)
iOS App
  ↓ Display chunks as they arrive (typewriter effect)
```

**Use Case**: Interactive chat with streaming responses
**Latency**: Real-time (sub-second updates)
**Implementation**: WebSocket + chunked streaming

**Code Example**:
```python
from fastapi import WebSocket
import asyncio

# WebSocket connection tracking
active_chat_connections: dict[str, WebSocket] = {}

@router.websocket('/v1/chat/stream/{uid}/{session_id}')
async def websocket_chat_stream(
    websocket: WebSocket,
    uid: str,
    session_id: str
):
    """
    WebSocket endpoint for streaming chat responses

    Client sends initial text, then server streams chunks
    """

    await websocket.accept()
    connection_id = f"{uid}:{session_id}"
    active_chat_connections[connection_id] = websocket

    try:
        # Receive initial request
        data = await websocket.receive_json()
        text = data.get('text', '')

        if not text:
            await websocket.send_json({'error': 'Text required'})
            return

        # Send to chat agent with streaming callback
        response = requests.post(
            'https://n8n.ella-ai-care.com/webhook/omi-realtime',
            json={
                'text': text,
                'uid': uid,
                'session_id': session_id,
                'stream': True,
                'callback_url': f'{BACKEND_URL}/v1/chat-stream-callback/{connection_id}',
                'stream_chunks': True
            },
            timeout=130,
            stream=True  # Enable streaming responses
        )

        # Stream chunks to client
        for chunk in response.iter_lines():
            if chunk:
                await websocket.send_json({
                    'type': 'chunk',
                    'data': chunk.decode('utf-8')
                })

        await websocket.send_json({
            'type': 'done',
            'status': 'completed'
        })

    except Exception as e:
        await websocket.send_json({
            'type': 'error',
            'error': str(e)
        })
    finally:
        active_chat_connections.pop(connection_id, None)
        await websocket.close()


@router.post('/v1/chat-stream-callback/{connection_id}')
async def chat_stream_callback(
    connection_id: str,
    chunk_data: dict
):
    """
    Webhook: Chat agent sends stream chunks here

    Chunks are forwarded to WebSocket client immediately
    """

    if connection_id not in active_chat_connections:
        return {'error': 'Connection not found'}, 404

    websocket = active_chat_connections[connection_id]

    try:
        await websocket.send_json({
            'type': 'chunk',
            'data': chunk_data.get('chunk', '')
        })
        return {'status': 'received'}
    except Exception as e:
        return {'error': str(e)}, 500
```

---

## Callback Protocol Specification

### Request from Backend to Chat Agent

```json
POST https://n8n.ella-ai-care.com/webhook/omi-realtime

{
    "uid": "user_123",
    "session_id": "conv_456",
    "text": "I'm experiencing chest pain",
    "source": "edge_asr" | "deepgram",
    "asr_provider": "apple_speech" | "parakeet" | "whisper",

    // Callback configuration
    "callback_url": "https://api.ella-ai-care.com/v1/chat-callback/job_789",
    "callback_timeout": 30,  // seconds
    "stream": false,  // Enable streaming chunks

    // Metadata
    "timestamp": "2025-11-15T22:00:00Z",
    "conversation_history": [
        {"role": "user", "text": "..."},
        {"role": "assistant", "text": "..."}
    ]
}
```

### Response from Chat Agent to Callback

```json
POST https://api.ella-ai-care.com/v1/chat-callback/job_789

{
    "text": "I'm concerned about your symptoms. Please seek immediate medical attention...",
    "urgency_level": "critical" | "high" | "medium" | "low",
    "action_items": [
        {
            "description": "Call emergency services (911)",
            "priority": "critical",
            "immediate": true
        },
        {
            "description": "Document symptoms for healthcare provider",
            "priority": "high",
            "due_at": "2025-11-15T23:00:00Z"
        }
    ],
    "metadata": {
        "agent_id": "chat_agent_v2",
        "model": "gpt-4-turbo",
        "confidence": 0.95,
        "processing_time_ms": 1250
    },
    "timestamp": "2025-11-15T22:00:01Z"
}
```

---

## Error Handling & Timeouts

### Timeout Scenarios

| Scenario | Timeout | Action |
|----------|---------|--------|
| Agent slow (>30s) | 30 seconds | Timeout + fallback |
| Network error | 10 seconds | Retry 3x then fallback |
| Agent down | Immediate | Fallback to local LLM |
| Callback never sent | 120 seconds | Mark as failed, retry |

### Fallback Strategy

```python
async def call_chat_agent_with_fallback(
    text: str,
    uid: str,
    session_id: str
):
    """
    Call chat agent with automatic fallback to local LLM
    """

    try:
        # Try real agent first
        response = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                'https://n8n.ella-ai-care.com/webhook/omi-realtime',
                json={'text': text, 'uid': uid, 'session_id': session_id},
                timeout=30
            ),
            timeout=35  # Give some buffer
        )

        return response.json()

    except (asyncio.TimeoutError, requests.Timeout, requests.ConnectionError) as e:
        # Fallback to local LLM
        print(f"⚠️  Chat agent unavailable, using fallback LLM: {e}")

        response = await generate_chat_response_local(text, uid)
        response['fallback'] = True
        response['fallback_reason'] = str(e)

        return response


async def generate_chat_response_local(text: str, uid: str):
    """Fallback: Use local LLM for chat response"""

    from utils.llm.chat import generate_response

    # Get recent conversation history
    messages = get_recent_messages(uid, limit=5)

    response_text = await generate_response(
        text=text,
        uid=uid,
        messages=messages
    )

    # Get urgency from text analysis
    urgency = analyze_urgency_local(text)

    return {
        'text': response_text,
        'urgency_level': urgency,
        'action_items': [],
        'fallback': True
    }
```

---

## Implementation Checklist

- [ ] Choose pattern (sync/async/streaming)
- [ ] Implement test endpoint: `/v1/test/chat`
- [ ] Implement callback endpoint: `/v1/chat-callback/{job_id}`
- [ ] Implement response retrieval: `/v1/chat/response/{job_id}`
- [ ] Add timeout handling (30-120 seconds)
- [ ] Add fallback to local LLM
- [ ] Add logging/monitoring
- [ ] Test with mock agent
- [ ] Test with real Ella chat agent
- [ ] Verify iOS receives responses
- [ ] Document for iOS team
- [ ] Deploy to production VPS

---

## Testing Strategy

### Unit Tests

```python
def test_chat_sync():
    """Test synchronous chat endpoint"""
    response = client.post('/v1/chat/sync', json={
        'text': 'I have a question',
        'session_id': 'test_123'
    })
    assert response.status_code == 200
    assert 'response' in response.json()

def test_chat_async():
    """Test asynchronous chat endpoint"""
    response = client.post('/v1/chat/async', json={
        'text': 'Process this',
        'session_id': 'test_456'
    })
    assert response.status_code == 200
    assert 'job_id' in response.json()

def test_chat_callback():
    """Test callback endpoint"""
    callback_response = client.post(
        '/v1/chat-callback/job_789',
        json={
            'text': 'Response from agent',
            'urgency_level': 'high',
            'action_items': []
        }
    )
    assert callback_response.status_code == 200

def test_timeout_fallback():
    """Test fallback when agent times out"""
    # Mock agent timeout
    with patch('requests.post', side_effect=TimeoutError):
        response = client.post('/v1/chat/async', json={'text': 'test'})
        # Should use fallback LLM
        assert response.status_code == 200
```

### Integration Tests

```python
def test_chat_e2e():
    """End-to-end: request → agent → callback → response"""
    # Send chat request
    response = client.post('/v1/chat/async', json={
        'text': 'I need help',
        'session_id': 'e2e_test'
    })
    job_id = response.json()['job_id']

    # Simulate agent sending callback
    callback_data = {
        'text': 'How can I help?',
        'urgency_level': 'low',
        'action_items': []
    }
    callback_response = client.post(
        f'/v1/chat-callback/{job_id}',
        json=callback_data
    )
    assert callback_response.status_code == 200

    # Verify response available
    result = client.get(f'/v1/chat/response/{job_id}')
    assert result.status_code == 200
    assert result.json()['status'] == 'completed'
```

---

## Related Documentation

- **Ella Integration**: `docs/ELLA_INTEGRATION.md`
- **E2E Testing PRD**: Review for chat endpoint implementation
- **Chat Router**: `routers/chat.py`
- **Ella Router**: `routers/ella.py`

---

**Status**: Implementation guidelines complete
**Next Steps**: Choose pattern and begin implementation
**Target Completion**: E2E Testing PRD revision with proper chat endpoint
