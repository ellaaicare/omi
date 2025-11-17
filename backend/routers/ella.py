"""
Ella Integration API Endpoints

Callback endpoints for Ella's n8n agents to send processed results back to OMI backend.
Backend stores results in Firestore and routes notifications to iOS app.

Architecture:
- Ella processes transcripts using Letta agents (scanner, summary, memory)
- Ella calls these endpoints to store results
- Backend acts as thin wrapper between Ella and OMI infrastructure
- iOS app polls existing endpoints (GET /v1/conversations, GET /v3/memories)
"""

import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

import database.memories as memories_db
import database.conversations as conversations_db
import database.notifications as notification_db
from models.memories import MemoryDB, Memory, MemoryCategory
from models.conversation import Structured, ActionItem, Event, CategoryEnum
from utils.other import endpoints as auth
from utils.tts import TTSManager, TTSRequest, TTSResponse, TTSVoice, TTSModel

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


# ================================
# REQUEST/RESPONSE MODELS
# ================================

class MemoryPayload(BaseModel):
    """Single memory from Ella's memory agent"""
    content: str = Field(..., description="The memory/fact text")
    category: str = Field(..., description="Memory category: 'interesting' or 'system'")
    visibility: Optional[str] = Field(default="private", description="Visibility level")
    tags: Optional[List[str]] = Field(default_factory=list, description="Tags for search")


class EllaMemoryCallback(BaseModel):
    """Ella memory agent sends extracted memories here"""
    uid: str = Field(..., description="User ID")
    conversation_id: Optional[str] = Field(None, description="Associated conversation ID")
    memories: List[MemoryPayload] = Field(..., description="List of extracted memories")


class ActionItemPayload(BaseModel):
    """Action item from Ella's summary agent"""
    description: str = Field(..., description="Task description")
    due_at: Optional[str] = Field(None, description="ISO 8601 due date")


class EventPayload(BaseModel):
    """Calendar event from Ella's summary agent"""
    title: str = Field(..., description="Event title")
    description: Optional[str] = Field(default="", description="Event description")
    start: str = Field(..., description="ISO 8601 start time")
    duration: Optional[int] = Field(default=60, description="Duration in minutes")


class StructuredPayload(BaseModel):
    """Structured summary from Ella's summary agent"""
    title: str = Field(..., description="Conversation title")
    overview: str = Field(..., description="2-3 sentence summary")
    emoji: str = Field(..., description="Single emoji representing conversation")
    category: str = Field(..., description="Conversation category")
    action_items: Optional[List[ActionItemPayload]] = Field(default_factory=list)
    events: Optional[List[EventPayload]] = Field(default_factory=list)


class EllaConversationCallback(BaseModel):
    """Ella summary agent sends conversation summary here"""
    uid: str = Field(..., description="User ID")
    conversation_id: str = Field(..., description="Conversation ID to update")
    structured: StructuredPayload = Field(..., description="Structured summary")


class EllaNotificationCallback(BaseModel):
    """Ella scanner sends urgent notifications here"""
    uid: str = Field(..., description="User ID")
    message: str = Field(..., description="Message to send to user")
    urgency: str = Field(..., description="EMERGENCY, QUESTION, WAKE_WORD, INTERESTING, NORMAL")
    generate_audio: bool = Field(default=True, description="Should backend generate TTS audio?")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Debug metadata")


# ================================
# CALLBACK ENDPOINTS
# ================================

@router.post("/v1/ella/memory", tags=["ella"])
async def ella_memory_callback(request: EllaMemoryCallback):
    """
    **Ella Memory Agent Callback**

    Ella's memory agent sends extracted memories here after processing a conversation.

    **Flow:**
    1. Ella memory agent processes transcript segments
    2. Extracts facts/memories using Letta agent config
    3. POSTs memories to this endpoint
    4. Backend stores in Firestore
    5. iOS app polls `GET /v3/memories` and sees new data

    **Example:**
    ```json
    {
      "uid": "user-123",
      "conversation_id": "conv-456",
      "memories": [
        {
          "content": "User takes blood pressure medication daily at 8am",
          "category": "system",
          "visibility": "private",
          "tags": ["medication", "health"]
        }
      ]
    }
    ```
    """
    print(f"💾 Ella Memory Callback - UID: {request.uid}, Count: {len(request.memories)}")

    try:
        saved_count = 0
        for memory_data in request.memories:
            # Convert to Memory model
            memory = Memory(
                content=memory_data.content,
                category=MemoryCategory(memory_data.category),
                visibility=memory_data.visibility or "private",
                tags=memory_data.tags or []
            )

            # Convert to MemoryDB (adds ID, timestamps)
            memory_db = MemoryDB.from_memory(
                memory,
                uid=request.uid,
                conversation_id=request.conversation_id,
                is_discarded=False
            )

            # Store in Firestore
            memories_db.create_memory(request.uid, memory_db.dict())
            saved_count += 1

            print(f"  ✅ Saved: {memory_data.content[:50]}...")

        return {
            "status": "success",
            "count": saved_count,
            "message": f"Stored {saved_count} memories for user {request.uid}"
        }

    except Exception as e:
        print(f"❌ Error storing memories: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to store memories: {str(e)}")


@router.post("/v1/ella/conversation", tags=["ella"])
async def ella_conversation_callback(request: EllaConversationCallback):
    """
    **Ella Summary Agent Callback**

    Ella's summary agent sends conversation summary here after processing full transcript.

    **Flow:**
    1. Ella summary agent processes complete conversation transcript
    2. Generates structured summary using Letta agent config
    3. POSTs summary to this endpoint
    4. Backend updates Firestore conversation
    5. iOS app polls `GET /v1/conversations` and sees updated summary

    **Example:**
    ```json
    {
      "uid": "user-123",
      "conversation_id": "conv-456",
      "structured": {
        "title": "Morning Health Check-In",
        "overview": "User discussed morning routine...",
        "emoji": "💊",
        "category": "health",
        "action_items": [{"description": "Schedule appointment", "due_at": "2025-11-15T10:00:00Z"}],
        "events": []
      }
    }
    ```
    """
    print(f"📝 Ella Conversation Callback - UID: {request.uid}, ID: {request.conversation_id}")
    print(f"  Title: {request.structured.title}")
    print(f"  Category: {request.structured.category}")

    try:
        # Convert action items
        action_items = []
        for item in request.structured.action_items:
            action_item = ActionItem(
                description=item.description,
                completed=False,
                due_at=datetime.fromisoformat(item.due_at.replace('Z', '+00:00')) if item.due_at else None,
                conversation_id=request.conversation_id
            )
            action_items.append(action_item)

        # Convert events
        events = []
        for event in request.structured.events:
            event_obj = Event(
                title=event.title,
                description=event.description or "",
                start=datetime.fromisoformat(event.start.replace('Z', '+00:00')),
                duration=event.duration or 60
            )
            events.append(event_obj)

        # Create Structured object
        structured = Structured(
            title=request.structured.title,
            overview=request.structured.overview,
            emoji=request.structured.emoji,
            category=CategoryEnum(request.structured.category),
            action_items=action_items,
            events=events
        )

        # Update conversation in Firestore
        conversations_db.update_conversation(
            request.uid,
            request.conversation_id,
            {
                "structured": structured.dict(),
                "status": "completed"
            }
        )

        print(f"  ✅ Updated conversation {request.conversation_id}")

        return {
            "status": "success",
            "conversation_id": request.conversation_id,
            "message": f"Updated conversation summary for {request.uid}"
        }

    except Exception as e:
        print(f"❌ Error updating conversation: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to update conversation: {str(e)}")


@router.post("/v1/ella/notification", tags=["ella"])
async def ella_notification_callback(request: EllaNotificationCallback):
    """
    **Ella Scanner Notification Callback**

    Ella's scanner sends urgent notifications here when detecting important events.

    **Flow:**
    1. Ella scanner processes realtime chunks
    2. Detects emergency/question/wake word
    3. Main agent generates caring response
    4. POSTs notification to this endpoint
    5. Backend generates TTS audio
    6. Backend sends push notification to iOS
    7. iOS app plays audio and shows message

    **Example:**
    ```json
    {
      "uid": "user-123",
      "message": "I noticed you mentioned chest pain. Are you okay?",
      "urgency": "EMERGENCY",
      "generate_audio": true,
      "metadata": {"trigger": "chest_pain_keyword", "confidence": 0.95}
    }
    ```

    **Urgency Levels:**
    - EMERGENCY: Medical crisis, immediate alert
    - QUESTION: User asked Ella something
    - WAKE_WORD: "Hey Ella" detected
    - INTERESTING: Worth noting, not urgent
    - NORMAL: Low priority
    """
    print(f"🚨 Ella Notification Callback - UID: {request.uid}")
    print(f"  Message: {request.message[:50]}...")
    print(f"  Urgency: {request.urgency}")

    try:
        # Get FCM token for user
        fcm_token = notification_db.get_token_only(request.uid)

        if not fcm_token:
            print(f"  ⚠️  No FCM token found for user {request.uid}")
            return {
                "status": "no_token",
                "message": "User has no FCM token registered"
            }

        # Generate TTS audio (if requested)
        audio_url = None
        duration_seconds = None

        if request.generate_audio:
            print(f"  🎵 Generating TTS audio...")
            manager = get_tts_manager()
            tts_request = TTSRequest(
                text=request.message,
                voice=TTSVoice.NOVA,
                model=TTSModel.HD,
                speed=1.0
            )

            tts_response = await manager.generate(
                request=tts_request,
                provider_name="openai",
                uid=request.uid
            )

            audio_url = tts_response.audio_url
            # Convert duration from ms to seconds (TTSResponse has duration_ms, not duration_seconds)
            duration_seconds = tts_response.duration_ms / 1000.0 if tts_response.duration_ms else None
            print(f"  ✅ TTS audio: {audio_url} (duration: {duration_seconds:.1f}s)" if duration_seconds else f"  ✅ TTS audio: {audio_url}")

        # Send push notification (reuse existing FCM code)
        from firebase_admin import messaging

        message = messaging.Message(
            token=fcm_token,
            data={
                "action": "speak_tts",
                "audio_url": audio_url or "",
                "text": request.message,
                "urgency": request.urgency,
            },
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True,
                        sound="default" if request.urgency == "EMERGENCY" else None
                    )
                )
            )
        )

        message_id = messaging.send(message)
        print(f"  ✅ Push notification sent: {message_id}")

        return {
            "status": "success",
            "message_id": message_id,
            "audio_url": audio_url,
            "duration_seconds": duration_seconds,
            "urgency": request.urgency
        }

    except Exception as e:
        print(f"❌ Error sending notification: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to send notification: {str(e)}")


@router.get("/v1/ella/health", tags=["ella"])
async def ella_health_check():
    """Health check for Ella integration endpoints"""
    return {
        "status": "healthy",
        "service": "ella-integration",
        "version": "1.0.0",
        "endpoints": [
            "POST /v1/ella/memory",
            "POST /v1/ella/conversation",
            "POST /v1/ella/notification"
        ]
    }
