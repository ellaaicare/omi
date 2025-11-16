"""
AI Processing API Endpoints

Webhook endpoint for processing transcripts with AI agents (Letta integration).
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field
import openai

from utils.tts import TTSManager, TTSRequest, TTSResponse, TTSVoice, TTSModel
from utils.other import endpoints as auth

# Initialize OpenAI client
openai.api_key = os.getenv("OPENAI_API_KEY")

router = APIRouter()

# Global TTS Manager instance (lazy-loaded)
_tts_manager = None


def get_tts_manager() -> TTSManager:
    """Get or create TTS manager singleton (lazy initialization)"""
    global _tts_manager

    if _tts_manager is None:
        import redis
        from google.cloud import firestore

        redis_host = os.getenv("REDIS_DB_HOST", "172.21.0.4")
        redis_port = int(os.getenv("REDIS_DB_PORT", "6379"))
        redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=False)

        firestore_client = firestore.Client()
        storage_bucket = os.getenv("BUCKET_PRIVATE_CLOUD_SYNC", "omi-dev-ca005.firebasestorage.app")

        _tts_manager = TTSManager(
            storage_bucket=storage_bucket,
            redis_client=redis_client,
            firestore_client=firestore_client
        )

    return _tts_manager


class TranscriptSegment(BaseModel):
    """Transcript segment from Deepgram"""
    text: str
    speaker: Optional[str] = None
    speaker_id: Optional[int] = None
    is_user: Optional[bool] = None
    person_id: Optional[str] = None
    start: float
    end: float


class UserProfile(BaseModel):
    """User profile information"""
    name: Optional[str] = None
    timezone: Optional[str] = None


class ConversationHistoryItem(BaseModel):
    """Past conversation for context"""
    id: str
    created_at: Optional[str] = None
    transcript: str
    structured: Optional[Dict[str, Any]] = {}


class UserMemory(BaseModel):
    """User memory/fact for personalization"""
    id: str
    content: str
    structured: Optional[Dict[str, Any]] = {}
    created_at: Optional[str] = None


class ProcessTranscriptRequest(BaseModel):
    """Enhanced webhook payload with conversation context"""
    session_id: str = Field(..., description="User ID")
    segments: List[TranscriptSegment] = Field(..., description="Current transcript segments")
    conversation_id: Optional[str] = Field(None, description="Current conversation ID")
    request_id: str = Field(..., description="Unique request ID for tracing")
    timestamp: float = Field(..., description="Request timestamp")

    # Enhanced context fields
    user_profile: Optional[UserProfile] = Field(None, description="User profile information")
    conversation_history: Optional[List[ConversationHistoryItem]] = Field(None, description="Recent conversations")
    user_memories: Optional[List[UserMemory]] = Field(None, description="User memories/facts")


class ProcessTranscriptResponse(BaseModel):
    """Response with AI-generated message and optional audio"""
    message: str = Field(..., description="AI-generated response text")
    notification: Optional[Dict[str, Any]] = Field(None, description="Push notification payload")


@router.post("/v1/ai/process-transcript", response_model=ProcessTranscriptResponse, tags=["ai"])
async def process_transcript(request: ProcessTranscriptRequest, background_tasks: BackgroundTasks):
    """
    Process transcript with AI agent (Letta) and generate TTS response.

    **Context-Aware Processing**:
    - User profile (name, timezone)
    - Conversation history (last 10 conversations)
    - User memories (last 20 facts)
    - Current transcript segments

    **Response Format**:
    ```json
    {
      "message": "AI response text",
      "notification": {
        "text": "Push notification message",
        "audio_url": "https://storage.../audio.mp3"
      }
    }
    ```

    **Integration Flow**:
    1. Receive enhanced webhook payload
    2. Route to Letta agent with user context
    3. Generate AI response
    4. Pre-generate TTS audio
    5. Return message + audio URL for push notification

    **Future**: Replace simple OpenAI call with Letta agent lookup
    """

    print(f"🤖 AI Processing - Request ID: {request.request_id}")
    print(f"   User: {request.session_id}")
    print(f"   Segments: {len(request.segments)}")
    print(f"   Conversation ID: {request.conversation_id}")

    try:
        # Extract transcript text from segments
        transcript_text = " ".join([seg.text for seg in request.segments])

        print(f"   Transcript: {transcript_text[:100]}...")

        # Build context-aware prompt
        context_parts = []

        if request.user_profile and request.user_profile.name:
            context_parts.append(f"User: {request.user_profile.name}")

        if request.user_memories:
            memory_texts = [mem.content for mem in request.user_memories[:5]]  # Top 5 memories
            if memory_texts:
                context_parts.append(f"Key facts: {'; '.join(memory_texts)}")

        if request.conversation_history:
            recent_conv = request.conversation_history[0] if request.conversation_history else None
            if recent_conv and recent_conv.transcript:
                context_parts.append(f"Recent context: {recent_conv.transcript[:200]}")

        context_prompt = "\n".join(context_parts) if context_parts else "No additional context available."

        # Generate AI response (placeholder - replace with Letta agent)
        # TODO: Replace with Letta agent lookup by user ID
        completion = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are Ella AI, a caring health assistant. Respond warmly and concisely.

Context about the user:
{context_prompt}

Guidelines:
- Keep responses under 50 words
- Be caring and supportive
- Reference user context naturally
- 1-2 sentences maximum
"""
                },
                {
                    "role": "user",
                    "content": transcript_text
                }
            ],
            max_tokens=100,
            temperature=0.7
        )

        ai_response = completion.choices[0].message.content
        print(f"✅ AI Response: {ai_response[:100]}...")

        # Pre-generate TTS audio for faster push notification
        manager = get_tts_manager()
        tts_request = TTSRequest(
            text=ai_response,
            voice=TTSVoice.NOVA,
            model=TTSModel.HD,
            speed=1.0
        )

        tts_response = await manager.generate(
            request=tts_request,
            provider_name="openai",
            uid=request.session_id
        )

        print(f"✅ TTS Audio: {tts_response.audio_url}")

        # Build response with notification payload
        return ProcessTranscriptResponse(
            message=ai_response,
            notification={
                "text": ai_response,
                "audio_url": tts_response.audio_url,
                "duration_seconds": tts_response.duration_seconds,
            }
        )

    except Exception as e:
        print(f"❌ AI processing error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"AI processing failed: {str(e)}")


@router.get("/v1/ai/health", tags=["ai"])
async def ai_health_check():
    """Health check for AI processing service"""
    return {
        "status": "healthy",
        "service": "ella-ai-agent",
        "version": "1.0.0",
        "letta_integrated": False,  # TODO: Update when Letta is integrated
    }
