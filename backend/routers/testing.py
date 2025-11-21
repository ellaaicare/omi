"""
E2E Agent Testing Endpoints

Test endpoints calling REAL production n8n Letta agents:
- Scanner Agent: Urgency detection (emergencies, wake words)
- Memory Agent: Memory extraction from conversations
- Summary Agent: Daily conversation summaries
- Chat Agent: Conversational AI (sync and async)

These are test-only endpoints for validating the full pipeline:
Audio → STT → Agent → Response
"""

import asyncio
import base64
import os
import time
import uuid
from datetime import datetime
from typing import Optional, Dict

import requests
from fastapi import APIRouter, Body, HTTPException, Depends
from deepgram import DeepgramClient, PrerecordedOptions, FileSource

from utils.other import endpoints as auth

router = APIRouter()

# n8n webhook endpoints
N8N_BASE_URL = "https://n8n.ella-ai-care.com/webhook"
N8N_SCANNER_AGENT = f"{N8N_BASE_URL}/scanner-agent"
N8N_MEMORY_AGENT = f"{N8N_BASE_URL}/memory-agent"
N8N_SUMMARY_AGENT = f"{N8N_BASE_URL}/summary-agent"
N8N_CHAT_AGENT = f"{N8N_BASE_URL}/chat-agent"

# Global job storage for async chat (use Redis in production)
chat_jobs: Dict[str, dict] = {}


def create_segment(text: str, speaker: str = "SPEAKER_00", is_user: bool = True, stt_source: str = None) -> dict:
    """
    Create a conversation segment in n8n's required format

    Schema from HARDWARE_E2E_TEST_GUIDE.md (lines 36-42):
    - speaker: Speaker ID (e.g. "User", "SPEAKER_00")
    - text: Transcribed text
    - stt_source: STT provider (e.g. "edge_asr", "deepgram", "soniox")

    Args:
        text: Transcribed text
        speaker: Speaker ID (default: SPEAKER_00)
        is_user: DEPRECATED (not used in v5.12 schema)
        stt_source: STT provider source (e.g. "edge_asr", "deepgram", "soniox") - optional

    Returns:
        Segment dict matching n8n Scanner v5.12 format
    """
    return {
        "speaker": speaker,
        "text": text,
        "stt_source": stt_source  # STT provider: edge_asr, deepgram, soniox, etc.
    }


def format_agent_error(
    error: Exception,
    agent_name: str,
    endpoint: str,
    debug: bool = False
) -> dict:
    """
    Format agent call errors with debug details

    Args:
        error: Exception from requests
        agent_name: Name of agent (e.g., "scanner", "memory")
        endpoint: n8n endpoint URL
        debug: If True, include technical details

    Returns:
        Error dict with user/dev friendly messages
    """
    import requests

    error_response = {
        "error": f"{agent_name} agent failed",
        "agent": agent_name,
        "endpoint": endpoint if debug else None,
    }

    # Categorize error type
    if isinstance(error, requests.exceptions.Timeout):
        error_response["error_type"] = "timeout"
        error_response["message"] = f"Agent took too long to respond (>10s)"
        if debug:
            error_response["debug_detail"] = str(error)

    elif isinstance(error, requests.exceptions.ConnectionError):
        error_response["error_type"] = "connection_error"
        error_response["message"] = "Could not connect to agent service"
        if debug:
            error_response["debug_detail"] = str(error)

    elif isinstance(error, requests.exceptions.HTTPError):
        status_code = error.response.status_code if hasattr(error, 'response') else None
        error_response["error_type"] = "http_error"
        error_response["status_code"] = status_code

        if status_code == 401:
            error_response["message"] = "Agent authentication failed - n8n webhook requires auth"
            if debug:
                error_response["debug_detail"] = "n8n webhooks returning 401 Unauthorized. Ella team needs to either: A) Make webhooks public, or B) Provide API key"
                error_response["debug_response"] = error.response.text if hasattr(error, 'response') else None

        elif status_code == 404:
            error_response["message"] = "Agent endpoint not found"
            if debug:
                error_response["debug_detail"] = f"Endpoint {endpoint} does not exist. Check n8n workflow deployment."

        elif status_code == 500:
            error_response["message"] = "Agent internal error"
            if debug:
                error_response["debug_detail"] = "n8n workflow or Letta agent crashed"
                error_response["debug_response"] = error.response.text if hasattr(error, 'response') else None

        else:
            error_response["message"] = f"Agent returned error (HTTP {status_code})"
            if debug:
                error_response["debug_response"] = error.response.text if hasattr(error, 'response') else None
    else:
        error_response["error_type"] = "unknown"
        error_response["message"] = "Unexpected error calling agent"
        if debug:
            error_response["debug_detail"] = str(error)

    # Always include user-friendly suggestion
    if not debug:
        error_response["suggestion"] = "Enable debug mode to see technical details"

    return error_response


async def transcribe_audio(audio_base64: str) -> tuple[str, float]:
    """
    Transcribe audio using Deepgram
    Returns: (transcript, latency_ms)
    """
    try:
        start_time = time.time()

        # Decode base64 audio
        audio_data = base64.b64decode(audio_base64)

        # Initialize Deepgram client
        deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
        if not deepgram_api_key:
            raise ValueError("DEEPGRAM_API_KEY not configured")

        deepgram = DeepgramClient(deepgram_api_key)

        # Configure options
        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
            language="en",
        )

        # Create file source
        source: FileSource = {
            "buffer": audio_data,
        }

        # Transcribe
        response = deepgram.listen.rest.v("1").transcribe_file(source, options)

        # Extract transcript
        transcript = ""
        if response.results and response.results.channels:
            channel = response.results.channels[0]
            if channel.alternatives:
                transcript = channel.alternatives[0].transcript

        latency_ms = (time.time() - start_time) * 1000

        return transcript.strip(), latency_ms

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {str(e)}")


@router.post("/v1/test/scanner-agent")
async def test_scanner_agent(
    audio: Optional[str] = Body(None, description="Base64 encoded audio WAV"),
    text: Optional[str] = Body(None, description="Text to test (if no audio)"),
    device_type: str = Body("omi", description="Hardware device type: omi, friend, openglass, etc."),
    source: str = Body("phone_mic", description="STT provider source: edge_asr, deepgram, soniox, etc."),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    debug: bool = Body(False, description="Enable debug mode for detailed error messages"),
    uid: str = Body("test_user_123", description="User ID for testing - use test_user_123 for E2E tests"),
):
    """
    Test Scanner Agent (urgency detection)
    Calls REAL production agent at n8n.ella-ai-care.com

    Detects emergencies, wake words, and urgency levels from conversations.

    Set debug=true to see detailed error messages for troubleshooting.
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()
    stt_latency_ms = 0
    transcript = ""

    # Step 1: Convert audio to text if needed
    if audio:
        transcript, stt_latency_ms = await transcribe_audio(audio)
    else:
        transcript = text

    # Step 2: Call REAL Scanner Agent via n8n (using n8n's required format)
    agent_start = time.time()

    # Log what we're sending to n8n
    n8n_payload = {
        "uid": uid,
        "device_type": device_type,  # Hardware device: "omi", "friend", "openglass"
        "segments": [create_segment(transcript, stt_source=source)]  # STT provider source
    }
    print(f"🔍 [Scanner] iOS UID: {uid}")
    print(f"🔍 [Scanner] Device Type: {device_type}")
    print(f"🔍 [Scanner] STT Source: {source}")
    print(f"🔍 [Scanner] Sending to n8n: {N8N_SCANNER_AGENT}")
    print(f"🔍 [Scanner] Payload: {n8n_payload}")

    try:
        response = requests.post(
            N8N_SCANNER_AGENT,
            json=n8n_payload,
            timeout=10  # 10 second timeout
        )

        print(f"🔍 [Scanner] n8n status: {response.status_code}")
        print(f"🔍 [Scanner] n8n response body: '{response.text[:500]}'")

        response.raise_for_status()

        # Handle empty n8n responses (Ella team still working on endpoints)
        try:
            agent_result = response.json()
            print(f"✅ [Scanner] n8n returned valid JSON for uid={uid}")
        except ValueError:
            # n8n returned empty/invalid JSON - use placeholder for iOS testing
            print(f"⚠️  [Scanner] n8n returned empty/invalid response for uid={uid}, using placeholder")
            agent_result = {
                "urgency_level": "low",
                "detected_event": "none",
                "explanation": "n8n webhook returned empty response (placeholder used for testing)",
                "recommended_action": "None",
                "confidence": 0.0,
                "_placeholder": True
            }
            if debug:
                agent_result["_debug"] = {
                    "backend_status": "✅ Backend received request and authenticated successfully",
                    "ios_uid_sent": uid,
                    "n8n_endpoint": N8N_SCANNER_AGENT,
                    "n8n_payload_sent": n8n_payload,
                    "n8n_status_code": response.status_code,
                    "n8n_response_body": response.text[:200] if response.text else "(empty)",
                    "issue": "n8n webhook returned empty/invalid JSON - Ella team still working on it",
                    "using_placeholder": True,
                    "note": "Check backend logs (journalctl -u omi-backend -f) for detailed request/response"
                }
    except requests.exceptions.RequestException as e:
        # Return detailed error for debugging
        error_details = format_agent_error(e, "scanner", N8N_SCANNER_AGENT, debug)
        raise HTTPException(status_code=500, detail=error_details)

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


@router.post("/v1/test/memory-agent")
async def test_memory_agent(
    audio: Optional[str] = Body(None, description="Base64 encoded audio WAV"),
    text: Optional[str] = Body(None, description="Text to test (if no audio)"),
    source: str = Body("phone_mic", description="Audio source"),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    debug: bool = Body(False, description="Enable debug mode for detailed error messages"),
    uid: str = Body("test_user_123", description="User ID for testing - use test_user_123 for E2E tests"),
):
    """
    Test Memory Agent (memory extraction)
    Calls REAL production agent at n8n.ella-ai-care.com

    Extracts memories from conversations (social events, activities, people, etc.)

    Set debug=true to see detailed error messages for troubleshooting.
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()
    stt_latency_ms = 0
    transcript = ""

    # Step 1: STT if needed
    if audio:
        transcript, stt_latency_ms = await transcribe_audio(audio)
    else:
        transcript = text

    # Step 2: Call REAL Memory Agent (using n8n's required format)
    agent_start = time.time()
    try:
        response = requests.post(
            N8N_MEMORY_AGENT,
            json={
                "uid": uid,
                "segments": [create_segment(transcript, stt_source=source)],
                "structured": {
                    "title": f"Test conversation {conversation_id}",
                    "overview": transcript[:100]  # First 100 chars as overview
                }
            },
            timeout=10
        )
        response.raise_for_status()

        # Handle empty n8n responses
        try:
            agent_result = response.json()
        except ValueError:
            print(f"⚠️  n8n memory-agent returned empty response for uid={uid}, using placeholder")
            agent_result = {
                "memories": [
                    {
                        "content": f"Test memory from: {transcript[:50]}...",
                        "category": "interesting",
                        "visibility": "private",
                        "tags": ["test"]
                    }
                ],
                "memory_count": 1,
                "_placeholder": True
            }
            if debug:
                agent_result["_debug"] = {
                    "backend_status": "✅ Backend received request and authenticated successfully",
                    "n8n_endpoint": N8N_MEMORY_AGENT,
                    "n8n_status_code": response.status_code,
                    "n8n_response_body": response.text[:200] if response.text else "(empty)",
                    "issue": "n8n webhook returned empty/invalid JSON - Ella team still working on it",
                    "using_placeholder": True
                }
    except requests.exceptions.RequestException as e:
        error_details = format_agent_error(e, "memory", N8N_MEMORY_AGENT, debug)
        raise HTTPException(status_code=500, detail=error_details)

    agent_latency_ms = (time.time() - agent_start) * 1000
    total_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "memory_agent",
        "source": source,
        "transcript": transcript,
        "agent_response": agent_result,
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2) if audio else 0,
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "stt_provider": "deepgram" if audio else None,
            "agent_endpoint": "memory-agent",
        }
    }


@router.post("/v1/test/summary-agent")
async def test_summary_agent(
    text: str = Body(..., description="Conversation transcript to summarize"),
    date: Optional[str] = Body(None, description="YYYY-MM-DD format"),
    debug: bool = Body(False, description="Enable debug mode for detailed error messages"),
    uid: str = Body("test_user_123", description="User ID for testing - use test_user_123 for E2E tests"),
):
    """
    Test Summary Agent (daily summaries)
    Calls REAL production agent at n8n.ella-ai-care.com

    Generates daily conversation summaries with key points and sentiment.

    Set debug=true to see detailed error messages for troubleshooting.
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # Convert date to ISO 8601 timestamp (n8n format requirement)
    started_at = f"{date}T00:00:00Z"

    start_time = time.time()

    # Call REAL Summary Agent (using n8n's required format)
    try:
        response = requests.post(
            N8N_SUMMARY_AGENT,
            json={
                "uid": uid,
                "transcript": text,
                "started_at": started_at
            },
            timeout=15  # Summary may take longer
        )
        response.raise_for_status()

        # Handle empty n8n responses
        try:
            agent_result = response.json()
        except ValueError:
            print(f"⚠️  n8n summary-agent returned empty response for uid={uid}, using placeholder")
            agent_result = {
                "title": "Test Summary",
                "overview": f"Summary of conversation: {text[:100]}...",
                "emoji": "📝",
                "category": "general",
                "action_items": [],
                "events": [],
                "_placeholder": True
            }
            if debug:
                agent_result["_debug"] = {
                    "backend_status": "✅ Backend received request and authenticated successfully",
                    "n8n_endpoint": N8N_SUMMARY_AGENT,
                    "n8n_status_code": response.status_code,
                    "n8n_response_body": response.text[:200] if response.text else "(empty)",
                    "issue": "n8n webhook returned empty/invalid JSON - Ella team still working on it",
                    "using_placeholder": True
                }
    except requests.exceptions.RequestException as e:
        error_details = format_agent_error(e, "summary", N8N_SUMMARY_AGENT, debug)
        raise HTTPException(status_code=500, detail=error_details)

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


@router.post("/v1/test/chat-sync")
async def test_chat_sync(
    audio: Optional[str] = Body(None, description="Base64 encoded audio WAV"),
    text: Optional[str] = Body(None, description="Text to test (if no audio)"),
    source: str = Body("phone_mic", description="Audio source"),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    debug: bool = Body(False, description="Enable debug mode for detailed error messages"),
    uid: str = Body("test_user_123", description="User ID for testing - use test_user_123 for E2E tests"),
):
    """
    Test Chat Agent (synchronous mode)
    Calls REAL production agent at n8n.ella-ai-care.com

    Synchronous chat with 30s timeout. Use async endpoint for slower responses.

    Set debug=true to see detailed error messages for troubleshooting.
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()
    stt_latency_ms = 0
    transcript = ""

    # Step 1: STT if audio provided
    if audio:
        transcript, stt_latency_ms = await transcribe_audio(audio)
    else:
        transcript = text

    # Step 2: Call REAL Chat Agent (synchronous)
    agent_start = time.time()
    try:
        response = requests.post(
            N8N_CHAT_AGENT,
            json={
                "text": transcript,
                "uid": uid,
                "session_id": conversation_id
            },
            timeout=30  # 30s timeout for sync
        )
        response.raise_for_status()
        agent_result = response.json()
    except requests.exceptions.RequestException as e:
        error_details = format_agent_error(e, "chat", N8N_CHAT_AGENT, debug)
        raise HTTPException(status_code=500, detail=error_details)

    agent_latency_ms = (time.time() - agent_start) * 1000
    total_latency_ms = (time.time() - start_time) * 1000

    return {
        "test_type": "chat_sync",
        "source": source,
        "transcript": transcript,
        "agent_response": agent_result,
        "metrics": {
            "stt_latency_ms": round(stt_latency_ms, 2) if audio else 0,
            "agent_latency_ms": round(agent_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "stt_provider": "deepgram" if audio else None,
            "agent_endpoint": "chat-agent",
            "mode": "synchronous"
        }
    }


@router.post("/v1/test/chat-async")
async def test_chat_async(
    audio: Optional[str] = Body(None, description="Base64 encoded audio WAV"),
    text: Optional[str] = Body(None, description="Text to test (if no audio)"),
    source: str = Body("phone_mic", description="Audio source"),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    debug: bool = Body(False, description="Enable debug mode for detailed error messages"),
    uid: str = Body("test_user_123", description="User ID for testing - use test_user_123 for E2E tests"),
):
    """
    Test Chat Agent (asynchronous mode)
    Calls REAL production agent at n8n.ella-ai-care.com

    Asynchronous chat with 120s timeout. Returns job_id for polling.
    Use /v1/test/chat-response/{job_id} to poll for result.

    Set debug=true to see detailed error messages for troubleshooting.
    """
    if not audio and not text:
        raise HTTPException(status_code=400, detail="Either audio or text required")

    start_time = time.time()
    job_id = str(uuid.uuid4())
    transcript = ""

    # Step 1: STT if audio provided
    if audio:
        transcript, stt_latency_ms = await transcribe_audio(audio)
    else:
        transcript = text
        stt_latency_ms = 0

    # Step 2: Queue async processing
    chat_jobs[job_id] = {
        "uid": uid,
        "transcript": transcript,
        "source": source,
        "status": "processing",
        "started_at": time.time(),
        "stt_latency_ms": stt_latency_ms,
        "debug": debug,
    }

    # Step 3: Process in background
    asyncio.create_task(
        _call_chat_agent_async(
            job_id=job_id,
            text=transcript,
            uid=uid,
            conversation_id=conversation_id,
            debug=debug
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
            "queue_latency_ms": round(queue_latency_ms, 2),
            "stt_latency_ms": round(stt_latency_ms, 2) if audio else 0,
        }
    }


async def _call_chat_agent_async(job_id: str, text: str, uid: str, conversation_id: str, debug: bool = False):
    """Background task to call chat agent asynchronously"""
    try:
        agent_start = time.time()
        response = requests.post(
            N8N_CHAT_AGENT,
            json={
                "text": text,
                "uid": uid,
                "session_id": conversation_id
            },
            timeout=120  # 2 minute timeout for async
        )
        response.raise_for_status()
        agent_latency_ms = (time.time() - agent_start) * 1000

        # Update job with result
        chat_jobs[job_id]["status"] = "completed"
        chat_jobs[job_id]["response"] = response.json()
        chat_jobs[job_id]["agent_latency_ms"] = agent_latency_ms

    except requests.exceptions.RequestException as e:
        # Format detailed error for debugging
        error_details = format_agent_error(e, "chat", N8N_CHAT_AGENT, debug)
        chat_jobs[job_id]["status"] = "failed"
        chat_jobs[job_id]["error"] = error_details


@router.get("/v1/test/chat-response/{job_id}")
async def get_chat_response(
    job_id: str,
    uid: str = Depends(auth.get_current_user_uid)
):
    """
    Poll for async chat response

    Returns the status and result of an async chat job.
    Poll this endpoint every 500ms until status is "completed" or "failed".
    """
    if job_id not in chat_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = chat_jobs[job_id]

    if job["status"] == "processing":
        return {
            "status": "processing",
            "job_id": job_id
        }

    elif job["status"] == "completed":
        total_latency_ms = (time.time() - job["started_at"]) * 1000
        return {
            "status": "completed",
            "job_id": job_id,
            "source": job.get("source", "unknown"),
            "transcript": job["transcript"],
            "agent_response": job["response"],
            "metrics": {
                "stt_latency_ms": job.get("stt_latency_ms", 0),
                "agent_latency_ms": job["agent_latency_ms"],
                "total_latency_ms": round(total_latency_ms, 2),
                "stt_provider": "deepgram" if job.get("stt_latency_ms", 0) > 0 else None,
                "agent_endpoint": "chat-agent",
                "mode": "asynchronous"
            }
        }

    else:  # failed
        return {
            "status": "failed",
            "job_id": job_id,
            "error": job.get("error", "Unknown error")
        }
