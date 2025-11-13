"""
Transcription session management.

This module handles the lifecycle and state of a transcription WebSocket session,
including connection management, heartbeat, and session metrics.
"""

import asyncio
import time
import uuid
from datetime import datetime
from typing import Dict, Optional, Set, Tuple

from fastapi.websockets import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from models.message_event import MessageEvent, MessageServiceStatusEvent
from utils.analytics import record_usage
from utils.logging_config import get_logger
from utils.notifications import send_credit_limit_notification, send_silent_user_notification
from utils.subscription import has_transcription_credits

logger = get_logger(__name__)


class TranscriptionSession:
    """
    Manages a single transcription WebSocket session.

    Handles session lifecycle, heartbeat, usage tracking, and credit management.
    """

    def __init__(
        self,
        websocket: WebSocket,
        uid: str,
        language: str = 'en',
        conversation_timeout: int = 120,
    ):
        """
        Initialize a transcription session.

        Args:
            websocket: The WebSocket connection
            uid: User ID
            language: Language code for transcription
            conversation_timeout: Timeout in seconds for conversation creation
        """
        self.websocket = websocket
        self.uid = uid
        self.language = language
        self.conversation_timeout = conversation_timeout

        # Session identification
        self.session_id = str(uuid.uuid4())

        # Session state
        self.active = True
        self.close_code = 1001  # Going Away
        self.started_at = time.time()

        # Credit management
        self.user_has_credits = has_transcription_credits(uid)

        # Heartbeat and timing
        self.last_audio_received_time: Optional[float] = None
        self.inactivity_timeout_seconds = 30

        # Usage tracking
        self.first_audio_byte_timestamp: Optional[float] = None
        self.last_usage_record_timestamp: Optional[float] = None
        self.last_transcript_time: Optional[float] = None
        self.words_transcribed_since_last_record = 0

        # Conversation state
        self.current_conversation_id: Optional[str] = None
        self.locked_conversation_ids: Set[str] = set()

        # Speaker identification
        self.speaker_to_person_map: Dict[int, Tuple[str, str]] = {}
        self.segment_person_assignment_map: Dict[str, str] = {}
        self.current_session_segments: Dict[str, bool] = {}  # segment_id -> speech_profile_processed
        self.suggested_segments: Set[str] = set()

        logger.info(
            "transcription_session_created",
            uid=uid,
            session_id=self.session_id,
            language=language,
            conversation_timeout=conversation_timeout,
        )

    async def accept(self) -> bool:
        """
        Accept the WebSocket connection.

        Returns:
            True if connection was accepted, False otherwise
        """
        try:
            await self.websocket.accept()
            logger.info("websocket_accepted", uid=self.uid, session_id=self.session_id)
            return True
        except RuntimeError as e:
            logger.error(
                "websocket_accept_failed",
                uid=self.uid,
                session_id=self.session_id,
                error=str(e),
            )
            return False

    async def send_message_event(self, msg: MessageEvent) -> bool:
        """
        Send a message event to the client.

        Args:
            msg: The message event to send

        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.active:
            return False

        logger.debug(
            "sending_message_event",
            uid=self.uid,
            session_id=self.session_id,
            event_type=msg.event_type,
        )

        try:
            await self.websocket.send_json(msg.to_json())
            return True
        except WebSocketDisconnect:
            logger.warning(
                "websocket_disconnected_during_send",
                uid=self.uid,
                session_id=self.session_id,
            )
            self.active = False
        except Exception as e:
            logger.error(
                "message_event_send_failed",
                uid=self.uid,
                session_id=self.session_id,
                error=str(e),
            )

        return False

    def create_send_task(self, msg: MessageEvent):
        """
        Create an async task to send a message event.

        Args:
            msg: The message event to send

        Returns:
            The created asyncio task
        """
        if not self.active:
            return None
        return asyncio.create_task(self.send_message_event(msg))

    async def send_heartbeat(self):
        """
        Send periodic heartbeat pings and monitor inactivity.

        This task runs continuously while the session is active.
        """
        logger.info("heartbeat_started", uid=self.uid, session_id=self.session_id)

        try:
            while self.active:
                # Send ping
                if self.websocket.client_state == WebSocketState.CONNECTED:
                    await self.websocket.send_text("ping")
                else:
                    logger.warning(
                        "websocket_not_connected_during_heartbeat",
                        uid=self.uid,
                        session_id=self.session_id,
                    )
                    break

                # Check for inactivity timeout
                if (
                    self.last_audio_received_time
                    and time.time() - self.last_audio_received_time > self.inactivity_timeout_seconds
                ):
                    logger.info(
                        "session_timeout_inactivity",
                        uid=self.uid,
                        session_id=self.session_id,
                        timeout_seconds=self.inactivity_timeout_seconds,
                    )
                    self.close_code = 1001
                    self.active = False
                    break

                # Wait before next heartbeat
                await asyncio.sleep(10)

        except WebSocketDisconnect:
            logger.info("websocket_disconnected_during_heartbeat", uid=self.uid, session_id=self.session_id)
        except Exception as e:
            logger.error(
                "heartbeat_error",
                uid=self.uid,
                session_id=self.session_id,
                error=str(e),
                exc_info=True,
            )
            self.close_code = 1011
        finally:
            self.active = False

    async def record_usage_periodically(self):
        """
        Periodically record usage metrics and check credit limits.

        This task runs every 60 seconds while the session is active.
        """
        logger.info("usage_recording_started", uid=self.uid, session_id=self.session_id)

        while self.active:
            await asyncio.sleep(60)

            if not self.active:
                break

            # Record usage
            if self.last_usage_record_timestamp:
                current_time = time.time()
                transcription_seconds = int(current_time - self.last_usage_record_timestamp)

                words_to_record = self.words_transcribed_since_last_record
                self.words_transcribed_since_last_record = 0  # reset

                if transcription_seconds > 0 or words_to_record > 0:
                    record_usage(
                        self.uid,
                        transcription_seconds=transcription_seconds,
                        words_transcribed=words_to_record,
                    )
                    logger.debug(
                        "usage_recorded",
                        uid=self.uid,
                        session_id=self.session_id,
                        transcription_seconds=transcription_seconds,
                        words_transcribed=words_to_record,
                    )

                self.last_usage_record_timestamp = current_time

            # Check credit limit
            await self._check_credit_limit()

            # Check for silence (basic plan users only)
            await self._check_user_silence()

        logger.info("usage_recording_stopped", uid=self.uid, session_id=self.session_id)

    async def _check_credit_limit(self):
        """Check if user has reached transcription credit limit."""
        if not has_transcription_credits(self.uid):
            self.user_has_credits = False

            try:
                await send_credit_limit_notification(self.uid)
            except Exception as e:
                logger.error(
                    "credit_limit_notification_failed",
                    uid=self.uid,
                    session_id=self.session_id,
                    error=str(e),
                )

            # Lock the in-progress conversation if needed
            if self.current_conversation_id and self.current_conversation_id not in self.locked_conversation_ids:
                # Import here to avoid circular dependency
                import database.conversations as conversations_db
                from models.conversation import ConversationStatus

                conversation = conversations_db.get_conversation(self.uid, self.current_conversation_id)
                if conversation and conversation['status'] == ConversationStatus.in_progress:
                    logger.info(
                        "locking_conversation_credit_limit",
                        uid=self.uid,
                        session_id=self.session_id,
                        conversation_id=self.current_conversation_id,
                    )
                    conversations_db.update_conversation(
                        self.uid,
                        self.current_conversation_id,
                        {'is_locked': True},
                    )
                    self.locked_conversation_ids.add(self.current_conversation_id)
        else:
            self.user_has_credits = True

    async def _check_user_silence(self):
        """Check for user silence and send notification if needed (basic plan users)."""
        # Import here to avoid circular dependency
        import database.users as user_db
        from models.users import PlanType

        user_subscription = user_db.get_user_valid_subscription(self.uid)
        if not user_subscription or user_subscription.plan == PlanType.basic:
            time_of_last_words = self.last_transcript_time or self.first_audio_byte_timestamp
            if (
                self.last_audio_received_time
                and time_of_last_words
                and (self.last_audio_received_time - time_of_last_words) > 15 * 60
            ):
                logger.info(
                    "user_silent_notification_triggered",
                    uid=self.uid,
                    session_id=self.session_id,
                    silence_duration_seconds=self.last_audio_received_time - time_of_last_words,
                )
                try:
                    await send_silent_user_notification(self.uid)
                except Exception as e:
                    logger.error(
                        "silent_user_notification_failed",
                        uid=self.uid,
                        session_id=self.session_id,
                        error=str(e),
                    )

    def record_audio_received(self):
        """Record that audio was received, updating timing metrics."""
        current_time = time.time()
        self.last_audio_received_time = current_time

        if self.first_audio_byte_timestamp is None:
            self.first_audio_byte_timestamp = current_time
            self.last_usage_record_timestamp = current_time

    def record_transcript_received(self, word_count: int):
        """
        Record that transcript was received.

        Args:
            word_count: Number of words in the transcript
        """
        self.last_transcript_time = time.time()
        self.words_transcribed_since_last_record += word_count

    async def close(self):
        """Close the WebSocket connection gracefully."""
        # Record final usage
        if self.last_usage_record_timestamp:
            transcription_seconds = int(time.time() - self.last_usage_record_timestamp)
            words_to_record = self.words_transcribed_since_last_record

            if transcription_seconds > 0 or words_to_record > 0:
                record_usage(
                    self.uid,
                    transcription_seconds=transcription_seconds,
                    words_transcribed=words_to_record,
                )
                logger.debug(
                    "final_usage_recorded",
                    uid=self.uid,
                    session_id=self.session_id,
                    transcription_seconds=transcription_seconds,
                    words_transcribed=words_to_record,
                )

        # Close WebSocket
        if self.websocket.client_state == WebSocketState.CONNECTED:
            try:
                await self.websocket.close(code=self.close_code)
                logger.info(
                    "websocket_closed",
                    uid=self.uid,
                    session_id=self.session_id,
                    close_code=self.close_code,
                )
            except Exception as e:
                logger.error(
                    "websocket_close_failed",
                    uid=self.uid,
                    session_id=self.session_id,
                    error=str(e),
                )

        # Cleanup
        self.locked_conversation_ids.clear()
        self.speaker_to_person_map.clear()
        self.segment_person_assignment_map.clear()
        self.current_session_segments.clear()
        self.suggested_segments.clear()

        logger.info(
            "transcription_session_ended",
            uid=self.uid,
            session_id=self.session_id,
            duration_seconds=time.time() - self.started_at,
        )
