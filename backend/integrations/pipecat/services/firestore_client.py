"""
Firestore client for Pipecat integration.

Handles storing voice conversations and session analytics.
Uses the standard upsert_conversation() for consistency with iOS app.
"""

from datetime import datetime, timezone
from typing import Optional
from google.cloud import firestore

# Import the standard conversation storage function
from database.conversations import upsert_conversation


class FirestoreClient:
    """
    Client for Firestore database operations.

    Stores voice conversations and session analytics.
    Uses the standard upsert_conversation() function for consistency
    with the main OMI backend and iOS app.
    """

    def __init__(self, db: Optional[firestore.Client] = None):
        """
        Initialize Firestore client.

        Args:
            db: Firestore client instance (creates one if not provided)
        """
        self._db = db

    @property
    def db(self) -> firestore.Client:
        """Lazy initialization of Firestore client."""
        if self._db is None:
            self._db = firestore.Client()
        return self._db

    async def store_voice_conversation(
        self,
        uid: str,
        session_id: str,
        transcript: str,
        segments: list[dict],
        duration_seconds: float,
        source: str = "voice_mode_v2",
        started_at: Optional[datetime] = None,
        language: str = "en",
    ) -> str:
        """
        Store a voice conversation in Firestore.

        Uses the standard upsert_conversation() function to ensure
        consistent field structure, encryption, and indexing with
        the iOS app and other conversation sources.

        Args:
            uid: Firebase user ID
            session_id: Unique session identifier
            transcript: Full conversation transcript
            segments: List of conversation turns with role, text, timestamp
            duration_seconds: Total session duration
            source: Source identifier for analytics
            started_at: When the conversation started (defaults to now - duration)
            language: Conversation language code (default: "en")

        Returns:
            Document ID of stored conversation
        """
        now = datetime.now(timezone.utc)

        # Calculate started_at if not provided
        if started_at is None:
            from datetime import timedelta
            started_at = now - timedelta(seconds=duration_seconds)

        # Build conversation data with all required fields
        # This matches the Conversation model structure expected by iOS
        conversation_data = {
            "id": session_id,
            "created_at": now,
            "started_at": started_at,
            "finished_at": now,

            # Required fields for iOS app
            "source": source,
            "language": language,
            "status": "completed",
            "discarded": False,

            # Transcript data
            "transcript": transcript,
            "transcript_segments": segments,

            # Structured summary - empty placeholder, will be populated by Ella callback
            "structured": {
                "title": "",
                "overview": "",
                "emoji": "🎤",
                "category": "other",
                "action_items": [],
                "events": [],
            },

            # Voice-specific metadata
            "is_voice_conversation": True,
            "turn_count": len(segments),

            # Empty defaults for other expected fields
            "plugins_results": [],
            "apps_results": [],
            "geolocation": None,
            "photos": [],
            "audio_files": [],
            "external_data": None,
            "app_id": None,
            "visibility": "private",
        }

        # Use the standard upsert_conversation function
        # This handles encryption, data protection, and proper indexing
        upsert_conversation(uid, conversation_data)
        print(f"💾 Stored conversation {session_id[:8]} for uid={uid[:8]}")

        return session_id

    async def store_session_analytics(
        self,
        uid: str,
        session_id: str,
        duration_seconds: float,
        turn_count: int,
        interruption_count: int = 0,
        avg_response_latency_ms: Optional[float] = None,
    ) -> str:
        """
        Store voice session analytics.

        Args:
            uid: Firebase user ID
            session_id: Unique session identifier
            duration_seconds: Total session duration
            turn_count: Number of conversation turns
            interruption_count: Number of interruptions
            avg_response_latency_ms: Average response latency

        Returns:
            Document ID of stored analytics
        """
        now = datetime.now(timezone.utc)

        analytics_data = {
            "session_id": session_id,
            "uid": uid,
            "created_at": now,
            "duration_seconds": duration_seconds,
            "turn_count": turn_count,
            "interruption_count": interruption_count,
            "avg_response_latency_ms": avg_response_latency_ms,
            "source": "voice_mode_v2",
        }

        # Store in user's voice_sessions subcollection
        doc_ref = (
            self.db.collection("users")
            .document(uid)
            .collection("voice_sessions")
            .document(session_id)
        )

        doc_ref.set(analytics_data)
        print(f"📊 Stored analytics {session_id[:8]} for uid={uid[:8]}")

        return session_id

    async def get_conversation(self, uid: str, session_id: str) -> Optional[dict]:
        """
        Retrieve a voice conversation.

        Args:
            uid: Firebase user ID
            session_id: Session identifier

        Returns:
            Conversation data or None if not found
        """
        doc_ref = (
            self.db.collection("users")
            .document(uid)
            .collection("conversations")
            .document(session_id)
        )

        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else None

    async def get_session_analytics(self, uid: str, session_id: str) -> Optional[dict]:
        """
        Retrieve session analytics.

        Args:
            uid: Firebase user ID
            session_id: Session identifier

        Returns:
            Analytics data or None if not found
        """
        doc_ref = (
            self.db.collection("users")
            .document(uid)
            .collection("voice_sessions")
            .document(session_id)
        )

        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else None
