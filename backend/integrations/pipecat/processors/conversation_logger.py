"""
Conversation logger processor for Pipecat pipeline.

Logs conversation turns and stores them in Firestore.
Calls n8n agents on session completion.
"""

import asyncio
import time
from typing import Optional
from datetime import datetime

from pipecat.frames.frames import (
    Frame,
    StartFrame,
    EndFrame,
    TranscriptionFrame,
    TextFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

from ..services.n8n_client import N8NClient
from ..services.firestore_client import FirestoreClient


class ConversationLogger(FrameProcessor):
    """
    Logs conversation turns and handles session lifecycle.

    Tracks:
    - User utterances (from STT)
    - Assistant responses (from LLM/TTS)
    - Session timing and metrics

    On session end:
    - Stores conversation in Firestore
    - Calls n8n memory and summary agents
    - Records analytics
    """

    def __init__(
        self,
        uid: str,
        session_id: str,
        n8n_client: N8NClient,
        firestore_client: Optional[FirestoreClient] = None,
    ):
        """
        Initialize conversation logger.

        Args:
            uid: Firebase user ID
            session_id: Unique session identifier
            n8n_client: Client for n8n webhook calls
            firestore_client: Client for Firestore (created if not provided)
        """
        super().__init__()
        self.uid = uid
        self.session_id = session_id
        self.n8n_client = n8n_client
        self.firestore_client = firestore_client or FirestoreClient()

        # Session state
        self.start_time: Optional[float] = None
        self.turns: list[dict] = []
        self.current_assistant_response: str = ""
        self.is_assistant_speaking: bool = False

        # Metrics
        self.interruption_count: int = 0
        self.response_latencies: list[float] = []
        self.last_user_end_time: Optional[float] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and log conversation turns."""
        # Call parent first to handle internal state (StartFrame tracking, etc.)
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self.start_time = time.time()
            print(f"📝 Conversation logging started: {self.session_id[:8]}")

        elif isinstance(frame, TranscriptionFrame):
            # User speech detected
            self._log_user_turn(frame.text)

        elif isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
            # LLM response text (accumulate for assistant turn)
            self.current_assistant_response += frame.text

        elif isinstance(frame, TTSStartedFrame):
            # Track when TTS starts for latency measurement
            self.is_assistant_speaking = True
            if self.last_user_end_time:
                latency = (time.time() - self.last_user_end_time) * 1000
                self.response_latencies.append(latency)
                print(f"⚡ Response latency: {latency:.0f}ms")

        elif isinstance(frame, TTSStoppedFrame):
            # TTS finished - log assistant turn
            self.is_assistant_speaking = False
            if self.current_assistant_response:
                self._log_assistant_turn(self.current_assistant_response)
                self.current_assistant_response = ""

        elif isinstance(frame, EndFrame):
            # Session ended
            print(f"📝 Conversation logging ended: {self.session_id[:8]}")

    def _log_user_turn(self, text: str):
        """Log a user utterance."""
        if not text.strip():
            return

        self.turns.append({
            "role": "user",
            "text": text.strip(),
            "timestamp": time.time(),
        })
        self.last_user_end_time = time.time()
        print(f"👤 User: {text[:50]}...")

    def _log_assistant_turn(self, text: str):
        """Log an assistant response."""
        if not text.strip():
            return

        self.turns.append({
            "role": "assistant",
            "text": text.strip(),
            "timestamp": time.time(),
        })
        print(f"🤖 Assistant: {text[:50]}...")

    async def finalize_session(self):
        """
        Finalize the session - store data and call agents.

        Called when the pipeline task completes.
        """
        if not self.turns:
            print(f"⚠️ No conversation turns to save for {self.session_id[:8]}")
            return

        duration = time.time() - (self.start_time or time.time())
        transcript = self._build_transcript()

        print(f"💾 Saving conversation: {len(self.turns)} turns, {duration:.1f}s")

        # 1. Store conversation in Firestore
        await self._store_conversation(transcript, duration)

        # 2. Store analytics
        await self._store_analytics(duration)

        # 3. Call n8n agents (async, don't block)
        asyncio.create_task(self._call_agents(transcript))

    def _build_transcript(self) -> str:
        """Build transcript string from turns."""
        lines = []
        for turn in self.turns:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['text']}")
        return "\n".join(lines)

    async def _store_conversation(self, transcript: str, duration: float):
        """Store conversation in Firestore."""
        try:
            await self.firestore_client.store_voice_conversation(
                uid=self.uid,
                session_id=self.session_id,
                transcript=transcript,
                segments=self.turns,
                duration_seconds=duration,
                source="voice_mode_v2",
            )
        except Exception as e:
            print(f"❌ Failed to store conversation: {e}")

    async def _store_analytics(self, duration: float):
        """Store session analytics."""
        try:
            avg_latency = (
                sum(self.response_latencies) / len(self.response_latencies)
                if self.response_latencies
                else None
            )

            await self.firestore_client.store_session_analytics(
                uid=self.uid,
                session_id=self.session_id,
                duration_seconds=duration,
                turn_count=len(self.turns),
                interruption_count=self.interruption_count,
                avg_response_latency_ms=avg_latency,
            )
        except Exception as e:
            print(f"❌ Failed to store analytics: {e}")

    async def _call_agents(self, transcript: str):
        """Call n8n memory and summary agents."""
        try:
            await self.n8n_client.call_memory_agent(
                uid=self.uid,
                conversation_id=self.session_id,
                transcript=transcript,
                segments=self.turns,
            )

            await self.n8n_client.call_summary_agent(
                uid=self.uid,
                conversation_id=self.session_id,
                transcript=transcript,
                segments=self.turns,
            )

            print(f"✅ n8n agents called for {self.session_id[:8]}")

        except Exception as e:
            print(f"⚠️ n8n agent call failed (non-blocking): {e}")
