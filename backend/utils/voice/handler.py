# Voice Mode WebSocket Handler
#
# Handles voice mode events over WebSocket.
# Designed for minimal integration with transcribe.py.

import asyncio
import base64
import json
import time
from typing import Optional, Callable, Any
from fastapi import WebSocket

from .config import VOICE_CONFIG
from .session import VoiceSession, VoiceState


class VoiceModeHandler:
    """
    Handles voice mode WebSocket communication.

    Usage in transcribe.py:
        voice_handler = VoiceModeHandler(websocket, uid)

        # In message loop:
        if voice_handler.is_voice_event(message):
            await voice_handler.handle_event(message)
            continue  # Skip normal processing
    """

    def __init__(
        self,
        websocket: WebSocket,
        uid: str,
        on_session_end: Optional[Callable] = None
    ):
        self.websocket = websocket
        self.uid = uid
        self.session: Optional[VoiceSession] = None
        self.on_session_end = on_session_end
        self._audio_sequence = 0

    @property
    def is_active(self) -> bool:
        """Check if voice mode is active."""
        return self.session is not None and self.session.is_active

    def is_voice_event(self, message: dict) -> bool:
        """Check if message is a voice mode event."""
        event = message.get("event", "")
        return event.startswith("voice_")

    async def handle_event(self, message: dict) -> bool:
        """
        Handle a voice mode event.

        Args:
            message: WebSocket message dict

        Returns:
            True if event was handled
        """
        event = message.get("event", "")

        if event == "voice_mode_start":
            await self._handle_start(message)
            return True

        elif event == "voice_mode_stop":
            await self._handle_stop(message)
            return True

        elif event == "voice_audio":
            await self._handle_audio(message)
            return True

        elif event == "voice_utterance":
            # Pre-transcribed text from iOS (using on-device ASR)
            await self._handle_utterance(message)
            return True

        return False

    async def _handle_start(self, message: dict) -> None:
        """Handle voice_mode_start event."""
        trigger = message.get("trigger", "button")
        print(f"🎤 Voice mode start requested: trigger={trigger}", flush=True)

        if self.session and self.session.is_active:
            print("⚠️ Voice session already active", flush=True)
            return

        # Create new session with callbacks
        self.session = VoiceSession(uid=self.uid)
        self.session.on_state_change = self._on_state_change
        self.session.on_transcription = self._on_transcription
        self.session.on_audio_chunk = self._on_audio_chunk
        self.session.on_response_complete = self._on_response_complete
        self.session.on_error = self._on_error

        # Start session (fetches config from n8n)
        success = await self.session.start()

        if success:
            await self._send_event("voice_mode_active", {
                "session_id": self.session.session_id,
                "timeout_seconds": VOICE_CONFIG.silence_timeout_seconds
            })
        else:
            await self._send_event("voice_error", {
                "code": "start_failed",
                "message": "Failed to start voice session"
            })

    async def _handle_stop(self, message: dict) -> None:
        """Handle voice_mode_stop event."""
        reason = message.get("reason", "user_request")
        print(f"🎤 Voice mode stop requested: reason={reason}", flush=True)

        if not self.session:
            return

        # End session and get summary
        summary = await self.session.end(reason)

        await self._send_event("voice_mode_ended", {
            "reason": reason,
            "session_duration_seconds": summary.get("duration_seconds", 0),
            "turn_count": summary.get("turn_count", 0)
        })

        # Callback for transcript storage
        if self.on_session_end:
            await self.on_session_end(summary)

        self.session = None
        self._audio_sequence = 0

    async def _handle_audio(self, message: dict) -> None:
        """Handle voice_audio event (raw audio from iOS)."""
        if not self.session or not self.session.is_active:
            return

        # TODO: Implement audio buffering and transcription
        # For now, we expect iOS to send pre-transcribed text via voice_utterance
        pass

    async def _handle_utterance(self, message: dict) -> None:
        """Handle voice_utterance event (pre-transcribed text from iOS)."""
        if not self.session or not self.session.is_active:
            return

        text = message.get("text", "").strip()
        if not text:
            return

        # Process utterance through session
        await self.session.handle_user_utterance(text)

    # === Callbacks from VoiceSession ===

    async def _on_state_change(self, state: VoiceState) -> None:
        """Called when session state changes."""
        await self._send_event("voice_status", {
            "status": state.value
        })

    async def _on_transcription(self, text: str, is_final: bool) -> None:
        """Called with transcription updates."""
        await self._send_event("voice_transcription", {
            "text": text,
            "is_final": is_final
        })

    async def _on_audio_chunk(self, audio_bytes: bytes) -> None:
        """Called with TTS audio chunks."""
        self._audio_sequence += 1
        await self._send_event("voice_response_audio", {
            "data": base64.b64encode(audio_bytes).decode('utf-8'),
            "sequence": self._audio_sequence,
            "format": VOICE_CONFIG.audio_format,
            "sample_rate": VOICE_CONFIG.audio_sample_rate
        })

    async def _on_response_complete(self, text: str) -> None:
        """Called when assistant response is complete."""
        await self._send_event("voice_response_complete", {
            "text": text
        })
        self._audio_sequence = 0

    async def _on_error(self, code: str, message: str) -> None:
        """Called on error."""
        await self._send_event("voice_error", {
            "code": code,
            "message": message
        })

    # === Helpers ===

    async def _send_event(self, event: str, data: dict) -> None:
        """Send event to iOS via WebSocket."""
        try:
            await self.websocket.send_json({
                "event": event,
                **data
            })
        except Exception as e:
            print(f"⚠️ Failed to send voice event: {e}", flush=True)

    async def check_timeout(self) -> None:
        """Check for session timeout and end if needed."""
        if self.session and self.session.check_timeout():
            print("⏰ Voice session timeout", flush=True)
            await self._handle_stop({"reason": "silence_timeout"})

        if self.session and self.session.check_max_duration():
            print("⏰ Voice session max duration", flush=True)
            await self._handle_stop({"reason": "max_duration"})
