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
    source: str = Body("phone_mic", description="Audio source"),
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Scanner Agent (urgency detection)
    Calls REAL production agent at n8n.ella-ai-care.com

    Detects emergencies, wake words, and urgency levels from conversations.
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

    # Step 2: Call REAL Scanner Agent via n8n
    agent_start = time.time()
    try:
        response = requests.post(
            N8N_SCANNER_AGENT,
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
        raise HTTPException(status_code=500, detail=f"Scanner agent failed: {str(e)}")

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
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Memory Agent (memory extraction)
    Calls REAL production agent at n8n.ella-ai-care.com

    Extracts memories from conversations (social events, activities, people, etc.)
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

    # Step 2: Call REAL Memory Agent
    agent_start = time.time()
    try:
        response = requests.post(
            N8N_MEMORY_AGENT,
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
        raise HTTPException(status_code=500, detail=f"Memory agent failed: {str(e)}")

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
    conversation_id: str = Body("test_conv", description="Conversation ID"),
    date: Optional[str] = Body(None, description="YYYY-MM-DD format"),
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Summary Agent (daily summaries)
    Calls REAL production agent at n8n.ella-ai-care.com

    Generates daily conversation summaries with key points and sentiment.
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    start_time = time.time()

    # Call REAL Summary Agent
    try:
        response = requests.post(
            N8N_SUMMARY_AGENT,
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
        raise HTTPException(status_code=500, detail=f"Summary agent failed: {str(e)}")

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
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Chat Agent (synchronous mode)
    Calls REAL production agent at n8n.ella-ai-care.com

    Synchronous chat with 30s timeout. Use async endpoint for slower responses.
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
        raise HTTPException(status_code=500, detail=f"Chat agent failed: {str(e)}")

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
    uid: str = Depends(auth.get_current_user_uid),
):
    """
    Test Chat Agent (asynchronous mode)
    Calls REAL production agent at n8n.ella-ai-care.com

    Asynchronous chat with 120s timeout. Returns job_id for polling.
    Use /v1/test/chat-response/{job_id} to poll for result.
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
            "queue_latency_ms": round(queue_latency_ms, 2),
            "stt_latency_ms": round(stt_latency_ms, 2) if audio else 0,
        }
    }


async def _call_chat_agent_async(job_id: str, text: str, uid: str, conversation_id: str):
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

    except Exception as e:
        chat_jobs[job_id]["status"] = "failed"
        chat_jobs[job_id]["error"] = str(e)


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
