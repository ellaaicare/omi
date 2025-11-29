# Voice Mode Session Management
#
# State machine for voice conversations.

import asyncio
import time
import uuid
from enum import Enum
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field

from .config import VOICE_CONFIG
from .llm import get_voice_config, stream_llm_response
from .tts import stream_tts_chunked


class VoiceState(Enum):
    """Voice session states."""
    INACTIVE = "inactive"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ENDING = "ending"


@dataclass
class VoiceTurn:
    """A single turn in the voice conversation."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class VoiceSession:
    """
    Voice conversation session.

    Manages state, conversation history, and coordinates
    LLM and TTS streaming.
    """
    uid: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: VoiceState = VoiceState.INACTIVE
    conversation_history: List[VoiceTurn] = field(default_factory=list)
    config: Optional[Dict[str, Any]] = None

    # Timing
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    # Callbacks for WebSocket events
    on_state_change: Optional[Callable] = None
    on_transcription: Optional[Callable] = None
    on_audio_chunk: Optional[Callable] = None
    on_response_complete: Optional[Callable] = None
    on_error: Optional[Callable] = None

    async def start(self) -> bool:
        """
        Start the voice session.

        Fetches config from n8n and transitions to LISTENING state.

        Returns:
            True if started successfully
        """
        try:
            print(f"🎤 Starting voice session: {self.session_id[:8]}...", flush=True)

            # Fetch config from n8n
            self.config = await get_voice_config(self.uid, self.session_id)

            # Transition to listening
            self.state = VoiceState.LISTENING
            self.last_activity = time.time()

            if self.on_state_change:
                await self.on_state_change(self.state)

            print(f"✅ Voice session started, state=LISTENING", flush=True)
            return True

        except Exception as e:
            print(f"❌ Failed to start voice session: {e}", flush=True)
            if self.on_error:
                await self.on_error("start_failed", str(e))
            return False

    async def handle_user_utterance(self, transcript: str) -> None:
        """
        Handle a complete user utterance.

        Processes transcript through LLM and streams TTS response.

        Args:
            transcript: Transcribed user speech
        """
        if self.state == VoiceState.INACTIVE:
            print("⚠️ Received utterance but session inactive", flush=True)
            return

        if not transcript or not transcript.strip():
            return

        self.last_activity = time.time()

        try:
            # Store user turn
            self.conversation_history.append(VoiceTurn(
                role="user",
                content=transcript
            ))

            # Notify transcription
            if self.on_transcription:
                await self.on_transcription(transcript, is_final=True)

            # Transition to thinking
            self.state = VoiceState.THINKING
            if self.on_state_change:
                await self.on_state_change(self.state)

            # Get conversation history for context
            history = [
                {"role": t.role, "content": t.content}
                for t in self.conversation_history[:-1]  # Exclude current
            ]

            # Stream LLM response
            self.state = VoiceState.SPEAKING
            if self.on_state_change:
                await self.on_state_change(self.state)

            # Collect full response while streaming TTS
            full_response = ""

            async def text_stream():
                nonlocal full_response
                async for chunk in stream_llm_response(
                    self.config, transcript, history
                ):
                    full_response += chunk
                    yield chunk

            # Stream TTS audio chunks
            async for audio_chunk in stream_tts_chunked(text_stream()):
                if self.on_audio_chunk:
                    await self.on_audio_chunk(audio_chunk)

            # Store assistant turn
            self.conversation_history.append(VoiceTurn(
                role="assistant",
                content=full_response
            ))

            # Notify response complete
            if self.on_response_complete:
                await self.on_response_complete(full_response)

            # Back to listening
            self.state = VoiceState.LISTENING
            self.last_activity = time.time()
            if self.on_state_change:
                await self.on_state_change(self.state)

        except Exception as e:
            print(f"❌ Error handling utterance: {e}", flush=True)
            if self.on_error:
                await self.on_error("utterance_failed", str(e))

            # Try to recover to listening
            self.state = VoiceState.LISTENING
            if self.on_state_change:
                await self.on_state_change(self.state)

    async def end(self, reason: str = "user_request") -> Dict[str, Any]:
        """
        End the voice session.

        Returns session summary for storage.

        Args:
            reason: Why session ended (user_request, silence_timeout, error)

        Returns:
            Session summary dict
        """
        self.state = VoiceState.ENDING
        if self.on_state_change:
            await self.on_state_change(self.state)

        duration = time.time() - self.started_at
        turn_count = len(self.conversation_history)

        print(f"🎤 Ending voice session: {self.session_id[:8]}, "
              f"reason={reason}, duration={duration:.1f}s, turns={turn_count}", flush=True)

        # Build transcript for summary/memory agents
        transcript_lines = []
        for turn in self.conversation_history:
            role = "User" if turn.role == "user" else "Ella"
            transcript_lines.append(f"{role}: {turn.content}")

        summary = {
            "session_id": self.session_id,
            "uid": self.uid,
            "reason": reason,
            "duration_seconds": duration,
            "turn_count": turn_count,
            "transcript": "\n".join(transcript_lines),
            "conversation_history": [
                {"role": t.role, "content": t.content, "timestamp": t.timestamp}
                for t in self.conversation_history
            ]
        }

        self.state = VoiceState.INACTIVE
        if self.on_state_change:
            await self.on_state_change(self.state)

        return summary

    def check_timeout(self) -> bool:
        """
        Check if session has timed out due to silence.

        Returns:
            True if timed out
        """
        if self.state == VoiceState.INACTIVE:
            return False

        silence_duration = time.time() - self.last_activity
        return silence_duration > VOICE_CONFIG.silence_timeout_seconds

    def check_max_duration(self) -> bool:
        """
        Check if session has exceeded max duration.

        Returns:
            True if exceeded
        """
        duration = time.time() - self.started_at
        return duration > VOICE_CONFIG.max_session_duration_seconds

    @property
    def is_active(self) -> bool:
        """Check if session is active."""
        return self.state not in (VoiceState.INACTIVE, VoiceState.ENDING)
