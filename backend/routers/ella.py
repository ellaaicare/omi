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
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

import database.memories as memories_db
import database.conversations as conversations_db
import database.notifications as notification_db
import database.redis_db as redis_db
from models.memories import MemoryDB, Memory, MemoryCategory
from models.conversation import Structured, ActionItem, Event, CategoryEnum
from models.transcript_segment import TranscriptSegment
from utils.other import endpoints as auth
from utils.tts import TTSManager, TTSRequest, TTSResponse, TTSVoice, TTSModel

router = APIRouter()

# Global TTS Manager instance (lazy-loaded)
_tts_manager = None


def safe_parse_datetime(date_string: Optional[str]) -> Optional[datetime]:
    """
    Safely parse datetime strings from various formats that n8n might send.

    Handles:
    - ISO 8601 with Z: "2025-11-25T10:00:00Z"
    - ISO 8601 with offset: "2025-11-25T10:00:00+00:00"
    - ISO 8601 without timezone: "2025-11-25T10:00:00"
    - Date only: "2025-11-25"
    - Truncated or malformed strings: gracefully returns None

    Returns:
        datetime object or None if parsing fails
    """
    if not date_string or not isinstance(date_string, str):
        return None

    date_string = date_string.strip()
    if not date_string:
        return None

    try:
        # Handle Z timezone suffix
        if date_string.endswith('Z'):
            date_string = date_string[:-1] + '+00:00'

        # Try standard ISO parsing
        return datetime.fromisoformat(date_string)
    except ValueError:
        pass

    # Try common formats
    formats_to_try = [
        "%Y-%m-%d",                    # Date only
        "%Y-%m-%dT%H:%M:%S",           # No timezone
        "%Y-%m-%dT%H:%M:%S.%f",        # With microseconds
        "%Y-%m-%d %H:%M:%S",           # Space separator
    ]

    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(date_string, fmt)
            # Add UTC timezone if none present
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # If all parsing fails, log and return None (don't crash)
    print(f"  ⚠️  Could not parse date: '{date_string}' - skipping")
    return None


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

from fastapi import Header

def verify_internal_key_callback(secret_key: str = Header(None, alias="secret-key")) -> bool:
    """Verify INTERNAL_API_KEY or ADMIN_KEY for callback endpoints.

    Note: Made optional (None default) for backwards compatibility with n8n.
    Will log warning if no key provided but still allow request.
    """
    if not secret_key:
        print("⚠️ Callback called without secret-key header (backwards compat mode)")
        return True

    admin_key = os.getenv('ADMIN_KEY')
    internal_key = os.getenv('INTERNAL_API_KEY')
    if secret_key != admin_key and secret_key != internal_key:
        raise HTTPException(status_code=403, detail='Invalid API key')
    return True


@router.post("/v1/ella/memory", tags=["ella"])
async def ella_memory_callback(
    request: EllaMemoryCallback,
    _: bool = Depends(verify_internal_key_callback)
):
    """
    **Ella Memory Agent Callback**

    Ella's memory agent sends extracted memories here after processing a conversation.

    **Auth**: Optional `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

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
                manually_added=False  # Fixed: was is_discarded (wrong parameter)
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
async def ella_conversation_callback(
    request: EllaConversationCallback,
    _: bool = Depends(verify_internal_key_callback)
):
    """
    **Ella Summary Agent Callback**

    Ella's summary agent sends conversation summary here after processing full transcript.

    **Auth**: Optional `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

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
        # Convert action items (with safe date parsing)
        action_items = []
        for item in request.structured.action_items:
            action_item = ActionItem(
                description=item.description,
                completed=False,
                due_at=safe_parse_datetime(item.due_at),  # Safe parser handles malformed dates
                conversation_id=request.conversation_id
            )
            action_items.append(action_item)

        # Convert events (with safe date parsing)
        events = []
        for event in request.structured.events:
            event_start = safe_parse_datetime(event.start)
            if event_start is None:
                # Skip events with invalid start dates
                print(f"  ⚠️  Skipping event '{event.title}' - invalid start date")
                continue
            event_obj = Event(
                title=event.title,
                description=event.description or "",
                start=event_start,
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

        # Check if conversation exists first
        existing_conv = conversations_db.get_conversation(request.uid, request.conversation_id)

        if existing_conv:
            # Update existing conversation
            conversations_db.update_conversation(
                request.uid,
                request.conversation_id,
                {
                    "structured": structured.dict(),
                    "status": "completed"
                }
            )
            print(f"  ✅ Updated existing conversation {request.conversation_id}")
        else:
            # Create new conversation with summary (for E2E testing support)
            # In production, conversations are created by audio processing first
            print(f"  ⚠️  Conversation {request.conversation_id} doesn't exist, creating it...")
            from datetime import timezone
            new_conv = {
                "id": request.conversation_id,
                "created_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
                "finished_at": datetime.now(timezone.utc),
                "status": "completed",
                "structured": structured.dict(),
                "transcript": request.structured.overview,  # Use overview as transcript
                "transcript_segments": [],
                "source": "external_integration",
                "language": "en",
                "discarded": False,
            }
            conversations_db.upsert_conversation(request.uid, new_conv)
            print(f"  ✅ Created new conversation {request.conversation_id}")

        return {
            "status": "success",
            "conversation_id": request.conversation_id,
            "message": f"Processed conversation summary for {request.uid}"
        }

    except Exception as e:
        print(f"❌ Error updating conversation: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to update conversation: {str(e)}")


@router.post("/v1/ella/notification", tags=["ella"])
async def ella_notification_callback(
    request: EllaNotificationCallback,
    _: bool = Depends(verify_internal_key_callback)
):
    """
    **Ella Scanner Notification Callback**

    Ella's scanner sends urgent notifications here when detecting important events.

    **Auth**: Optional `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

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
        # Import multi-device module
        from database import notifications_multi_device

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

        # Send push notification to ALL user devices
        from firebase_admin import messaging

        notification_data = {
            'data': {
                "action": "speak_tts",
                "audio_url": audio_url or "",
                "text": request.message,
                "urgency": request.urgency,
            },
            'apns': messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True,
                        sound="default" if request.urgency == "EMERGENCY" else None
                    )
                )
            )
        }

        # Send to all devices
        result = notifications_multi_device.send_to_all_devices(request.uid, notification_data)

        if result['status'] == 'no_tokens':
            print(f"  ⚠️  No FCM tokens found for user {request.uid}")
            return {
                "status": "no_token",
                "message": "User has no FCM token registered"
            }

        print(f"  ✅ Push notification sent to {result['sent']}/{result['total_devices']} devices")

        # ====== STORE ASSISTANT MESSAGE IN TRANSCRIPT ======
        # This enables proper context formatting for subsequent turns
        try:
            # Get in-progress conversation for this user
            conversation_id = redis_db.get_in_progress_conversation_id(request.uid)
            if conversation_id:
                # Get existing conversation
                existing_conv = conversations_db.get_conversation(request.uid, conversation_id)
                if existing_conv and existing_conv.get('status') == 'in_progress':
                    # Create assistant segment
                    current_time = time.time()
                    assistant_segment = TranscriptSegment(
                        text=request.message,
                        speaker="ELLA_ASSISTANT",
                        speaker_id=99,  # Special ID for assistant
                        is_user=False,
                        role="assistant",  # Key field for context formatting
                        start=current_time,
                        end=current_time,
                        source="ella_agent"
                    )

                    # Append to transcript_segments
                    segments = existing_conv.get('transcript_segments', [])
                    segments.append(assistant_segment.dict())

                    # Update conversation
                    conversations_db.update_conversation(
                        request.uid,
                        conversation_id,
                        {'transcript_segments': segments}
                    )
                    print(f"  📝 Stored assistant message in conversation {conversation_id}")
        except Exception as e:
            # Don't fail the notification if storage fails
            print(f"  ⚠️ Could not store assistant message: {e}")

        return {
            "status": result['status'],
            "total_devices": result['total_devices'],
            "sent": result['sent'],
            "failed": result['failed'],
            "audio_url": audio_url,
            "duration_seconds": duration_seconds,
            "urgency": request.urgency,
            "details": result['results']
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
            "POST /v1/ella/notification",
            "GET /v1/ella/conversations",
            "GET /v1/ella/conversations/{conversation_id}",
            "GET /v1/ella/memories",
            "GET /v1/ella/memories/{memory_id}"
        ]
    }


# ============================================================
# ADMIN ENDPOINTS - For Letta agent tools
# ============================================================

from fastapi import Header, Query

def verify_internal_key(secret_key: str = Header(..., alias="secret-key")) -> bool:
    """Verify INTERNAL_API_KEY or ADMIN_KEY for admin endpoints"""
    admin_key = os.getenv('ADMIN_KEY')
    internal_key = os.getenv('INTERNAL_API_KEY')
    if secret_key != admin_key and secret_key != internal_key:
        raise HTTPException(status_code=403, detail='Invalid API key')
    return True


@router.get("/v1/ella/conversations/{conversation_id}", tags=["ella"])
async def get_conversation_for_letta(
    conversation_id: str,
    uid: str = Query(..., description="User ID"),
    _: bool = Depends(verify_internal_key)
):
    """
    Get a single conversation by ID for Letta agent tools.

    **Auth**: Requires `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

    **Response includes**:
    - id, created_at, started_at, finished_at
    - transcript_segments: List of {text, speaker, start, end, is_user}
    - structured: {title, overview, emoji, category, action_items, events}
    - source, language, status

    **Example**:
    ```bash
    curl -X GET "https://api.ella-ai-care.com/v1/ella/conversations/conv-123?uid=user-456" \\
      -H "secret-key: YOUR_INTERNAL_API_KEY"
    ```
    """
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id} not found for user {uid}")

    print(f"📖 Letta fetched conversation {conversation_id} for uid={uid}")
    return conversation


@router.get("/v1/ella/conversations", tags=["ella"])
async def get_conversations_for_letta(
    uid: str = Query(..., description="User ID"),
    limit: int = Query(10, description="Max conversations to return"),
    offset: int = Query(0, description="Pagination offset"),
    include_transcript: bool = Query(False, description="Include full transcript_segments"),
    categories: str = Query(None, description="Comma-separated categories to filter (e.g., health,personal)"),
    start_date: str = Query(None, description="Filter conversations after this date (ISO format: 2025-12-01)"),
    end_date: str = Query(None, description="Filter conversations before this date (ISO format: 2025-12-31)"),
    _: bool = Depends(verify_internal_key)
):
    """
    Get recent conversations for a user for Letta agent tools.

    **Auth**: Requires `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

    **Filtering**:
    - categories: health,personal,work (comma-separated)
    - start_date: 2025-12-01 (ISO format)
    - end_date: 2025-12-31 (ISO format)

    **Response**: List of conversations with:
    - id, created_at, started_at, finished_at
    - structured: {title, overview, emoji, category}
    - transcript_segments (if include_transcript=true)

    **Example**:
    ```bash
    curl -X GET "https://api.ella-ai-care.com/v1/ella/conversations?uid=user-456&limit=5&categories=health&start_date=2025-12-01" \\
      -H "secret-key: YOUR_INTERNAL_API_KEY"
    ```
    """
    # Parse categories
    category_list = None
    if categories:
        category_list = [c.strip() for c in categories.split(",") if c.strip()]

    # Parse dates
    parsed_start = None
    parsed_end = None
    if start_date:
        try:
            parsed_start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid start_date format: {start_date}. Use ISO format (YYYY-MM-DD)")
    if end_date:
        try:
            parsed_end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid end_date format: {end_date}. Use ISO format (YYYY-MM-DD)")

    conversations = conversations_db.get_conversations(
        uid,
        limit=limit,
        offset=offset,
        include_discarded=False,
        statuses=["completed"],
        start_date=parsed_start,
        end_date=parsed_end,
        categories=category_list
    )

    # Remove transcript_segments if not requested (to reduce response size)
    if not include_transcript:
        for conv in conversations:
            conv.pop('transcript_segments', None)

    print(f"📖 Letta fetched {len(conversations)} conversations for uid={uid}")
    return conversations


@router.get("/v1/ella/memories", tags=["ella"])
async def get_memories_for_letta(
    uid: str = Query(..., description="User ID"),
    limit: int = Query(100, description="Max memories to return"),
    offset: int = Query(0, description="Pagination offset"),
    categories: str = Query(None, description="Comma-separated categories to filter (e.g., health,personal)"),
    start_date: str = Query(None, description="Filter memories after this date (ISO format: 2025-12-01)"),
    end_date: str = Query(None, description="Filter memories before this date (ISO format: 2025-12-31)"),
    _: bool = Depends(verify_internal_key)
):
    """
    Get memories for a user for Letta agent tools.

    **Auth**: Requires `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

    **Filtering**:
    - categories: health,personal,system (comma-separated)
    - start_date: 2025-12-01 (ISO format)
    - end_date: 2025-12-31 (ISO format)

    **Response**: List of memories with:
    - id, content, category, created_at
    - conversation_id (if linked to a conversation)

    **Example**:
    ```bash
    curl -X GET "https://api.ella-ai-care.com/v1/ella/memories?uid=user-456&limit=50&categories=health&start_date=2025-12-01" \\
      -H "secret-key: YOUR_INTERNAL_API_KEY"
    ```
    """
    # Parse categories
    category_list = []
    if categories:
        category_list = [c.strip() for c in categories.split(",") if c.strip()]

    # Parse dates
    parsed_start = None
    parsed_end = None
    if start_date:
        try:
            parsed_start = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid start_date format: {start_date}. Use ISO format (YYYY-MM-DD)")
    if end_date:
        try:
            parsed_end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid end_date format: {end_date}. Use ISO format (YYYY-MM-DD)")

    memories = memories_db.get_memories(uid, limit, offset, category_list, parsed_start, parsed_end)

    print(f"📖 Letta fetched {len(memories)} memories for uid={uid}")
    return memories


@router.get("/v1/ella/memories/{memory_id}", tags=["ella"])
async def get_memory_for_letta(
    memory_id: str,
    uid: str = Query(..., description="User ID"),
    _: bool = Depends(verify_internal_key)
):
    """
    Get a single memory by ID for Letta agent tools.

    **Auth**: Requires `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)

    **Example**:
    ```bash
    curl -X GET "https://api.ella-ai-care.com/v1/ella/memories/mem-123?uid=user-456" \\
      -H "secret-key: YOUR_INTERNAL_API_KEY"
    ```
    """
    memory = memories_db.get_memory(uid, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found for user {uid}")

    print(f"📖 Letta fetched memory {memory_id} for uid={uid}")
    return memory
