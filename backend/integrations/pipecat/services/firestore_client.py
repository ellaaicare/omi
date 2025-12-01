"""
Firestore client for Pipecat integration.

Handles storing voice conversations and session analytics.
"""

from datetime import datetime
from typing import Optional
from google.cloud import firestore


class FirestoreClient:
    """
    Client for Firestore database operations.

    Stores voice conversations and session analytics.
    Uses the same Firestore instance as the main OMI backend.
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
    ) -> str:
        """
        Store a voice conversation in Firestore.

        Stores in the same structure as regular conversations for
        consistency with the iOS app.

        Args:
            uid: Firebase user ID
            session_id: Unique session identifier
            transcript: Full conversation transcript
            segments: List of conversation turns with role, text, timestamp
            duration_seconds: Total session duration
            source: Source identifier for analytics

        Returns:
            Document ID of stored conversation
        """
        conversation_data = {
            "id": session_id,
            "uid": uid,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "finished_at": datetime.utcnow(),
            "transcript": transcript,
            "transcript_segments": segments,
            "source": source,
            "status": "completed",
            "duration_seconds": duration_seconds,
            "discarded": False,
            # Voice-specific metadata
            "is_voice_conversation": True,
            "turn_count": len(segments),
        }

        # Store in user's conversations collection
        doc_ref = (
            self.db.collection("users")
            .document(uid)
            .collection("conversations")
            .document(session_id)
        )

        doc_ref.set(conversation_data)
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
        analytics_data = {
            "session_id": session_id,
            "uid": uid,
            "created_at": datetime.utcnow(),
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
