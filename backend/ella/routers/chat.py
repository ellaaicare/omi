"""
Ella Chat Router

Provides a streaming chat endpoint that connects to xAI's Grok API
for Ella's conversational AI.

Endpoints:
- POST /v1/ella/chat/stream - Stream a chat response from Grok as Ella
"""

import logging
import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella-chat"])

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"

ELLA_SYSTEM_PROMPT = (
    "You are Ella, a warm and caring AI companion for elderly users. "
    "You speak clearly and simply, with patience and warmth. "
    "You help with daily life questions, provide companionship, and gently encourage healthy habits. "
    "Keep responses concise and easy to understand. "
    "If someone seems confused or distressed, respond with extra gentleness and reassurance."
)


class EllaChatRequest(BaseModel):
    uid: str
    message: str
    conversation_id: str = ""


@router.post("/chat/stream")
async def ella_chat_stream(request: EllaChatRequest):
    """Stream a chat response from Grok (xAI) as Ella."""
    if not XAI_API_KEY:
        raise HTTPException(status_code=500, detail="XAI_API_KEY not configured")

    async def generate():
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{XAI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-3-mini",
                    "messages": [
                        {"role": "system", "content": ELLA_SYSTEM_PROMPT},
                        {"role": "user", "content": request.message},
                    ],
                    "stream": True,
                    "temperature": 0.7,
                },
                timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    yield f'data: {{"error": "Grok API error: {response.status_code}"}}\n\n'
                    return
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield f"{line}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
