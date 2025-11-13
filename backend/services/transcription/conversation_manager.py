"""
Conversation lifecycle management.

This module handles conversation creation, updates, lifecycle management,
and integration with transcript processing and photo handling.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import database.conversations as conversations_db
from database.redis_db import get_cached_user_geolocation
from models.conversation import (
    Conversation,
    ConversationPhoto,
    ConversationSource,
    ConversationStatus,
    Geolocation,
    Structured,
    TranscriptSegment,
)
from models.message_event import ConversationEvent, LastConversationEvent, MessageEvent
from utils.app_integrations import trigger_external_integrations
from utils.conversations.location import get_google_maps_location
from utils.conversations.process_conversation import process_conversation, retrieve_in_progress_conversation
from utils.logging_config import get_logger
from utils.locking import conversation_lock

logger = get_logger(__name__)


class ConversationManager:
    """
    Manages conversation lifecycle and updates.

    Handles conversation creation, updates, timeout monitoring, and finalization.
    """

    def __init__(
        self,
        uid: str,
        session_id: str,
        language: str,
        conversation_timeout: int,
        private_cloud_sync_enabled: bool,
        send_message_event_callback: callable,
    ):
        """
        Initialize the conversation manager.

        Args:
            uid: User ID
            session_id: Session ID for logging
            language: Language code
            conversation_timeout: Timeout for conversation creation (seconds)
            private_cloud_sync_enabled: Whether private cloud sync is enabled
            send_message_event_callback: Callback to send message events to client
        """
        self.uid = uid
        self.session_id = session_id
        self.language = language
        self.conversation_timeout = conversation_timeout
        self.private_cloud_sync_enabled = private_cloud_sync_enabled
        self.send_message_event = send_message_event_callback

        # Conversation state
        self.current_conversation_id: Optional[str] = None
        self.seconds_to_trim: Optional[float] = None
        self.seconds_to_add: Optional[float] = None

        # Active flag
        self.active = True

        logger.info(
            "conversation_manager_created",
            uid=uid,
            session_id=session_id,
            conversation_timeout=conversation_timeout,
        )

    async def initialize(self):
        """Initialize conversation management, including processing existing conversations."""
        # Finalize any processing conversations
        await self._finalize_processing_conversations()

        # Send last completed conversation
        self._send_last_conversation()

        # Prepare or create in-progress conversation
        await self._prepare_in_progress_conversations()

    async def _finalize_processing_conversations(self):
        """Finalize any conversations that are in processing state."""
        processing = conversations_db.get_processing_conversations(self.uid)

        logger.info(
            "finalizing_processing_conversations",
            uid=self.uid,
            session_id=self.session_id,
            count=len(processing) if processing else 0,
        )

        if not processing or len(processing) == 0:
            return

        # Sleep to yield for WebSocket acceptance
        await asyncio.sleep(1)

        for conversation in processing:
            await self._create_conversation(conversation)

    def _send_last_conversation(self):
        """Send the last completed conversation to the client."""
        last_conversation = conversations_db.get_last_completed_conversation(self.uid)
        if last_conversation:
            self.send_message_event(LastConversationEvent(memory_id=last_conversation['id']))
            logger.debug(
                "sent_last_conversation",
                uid=self.uid,
                session_id=self.session_id,
                conversation_id=last_conversation['id'],
            )

    async def _prepare_in_progress_conversations(self):
        """Prepare or create in-progress conversation."""
        # Check for existing in-progress conversation
        existing_conversation = retrieve_in_progress_conversation(self.uid)

        if existing_conversation:
            finished_at = datetime.fromisoformat(existing_conversation['finished_at'].isoformat())
            seconds_since_last_segment = (datetime.now(timezone.utc) - finished_at).total_seconds()

            # Check if conversation has timed out
            if seconds_since_last_segment >= self.conversation_timeout:
                logger.info(
                    "existing_conversation_timed_out",
                    uid=self.uid,
                    session_id=self.session_id,
                    conversation_id=existing_conversation['id'],
                    seconds_since_last_segment=seconds_since_last_segment,
                )
                await self._process_current_conversation(existing_conversation['id'])
                return

            # Resume existing conversation
            self.current_conversation_id = existing_conversation['id']
            started_at = datetime.fromisoformat(existing_conversation['started_at'].isoformat())
            self.seconds_to_add = (
                (datetime.now(timezone.utc) - started_at).total_seconds()
                if existing_conversation['transcript_segments']
                else None
            )

            logger.info(
                "resuming_conversation",
                uid=self.uid,
                session_id=self.session_id,
                conversation_id=self.current_conversation_id,
                offset_seconds=self.seconds_to_add if self.seconds_to_add else 0,
                timeout_in=self.conversation_timeout - seconds_since_last_segment,
            )
            return

        # Create new conversation
        await self._create_new_in_progress_conversation()

    async def _create_new_in_progress_conversation(self) -> str:
        """
        Create a new in-progress conversation.

        Returns:
            The new conversation ID
        """
        new_conversation_id = str(uuid.uuid4())

        stub_conversation = Conversation(
            id=new_conversation_id,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            structured=Structured(),
            language=self.language,
            transcript_segments=[],
            photos=[],
            status=ConversationStatus.in_progress,
            source=ConversationSource.omi,
            private_cloud_sync_enabled=self.private_cloud_sync_enabled,
        )

        conversations_db.upsert_conversation(self.uid, conversation_data=stub_conversation.dict())

        # Import here to avoid circular dependency
        from database import redis_db

        redis_db.set_in_progress_conversation_id(self.uid, new_conversation_id)

        self.current_conversation_id = new_conversation_id
        self.seconds_to_trim = None
        self.seconds_to_add = None

        logger.info(
            "created_new_conversation",
            uid=self.uid,
            session_id=self.session_id,
            conversation_id=new_conversation_id,
        )

        return new_conversation_id

    async def _create_conversation(self, conversation_data: dict):
        """
        Finalize and process a conversation.

        Args:
            conversation_data: The conversation data dictionary
        """
        conversation = Conversation(**conversation_data)

        if conversation.status != ConversationStatus.processing:
            self.send_message_event(
                ConversationEvent(event_type="memory_processing_started", memory=conversation)
            )
            conversations_db.update_conversation_status(self.uid, conversation.id, ConversationStatus.processing)
            conversation.status = ConversationStatus.processing

        try:
            # Add geolocation
            geolocation = get_cached_user_geolocation(self.uid)
            if geolocation:
                geolocation = Geolocation(**geolocation)
                conversation.geolocation = get_google_maps_location(geolocation.latitude, geolocation.longitude)

            # Process conversation
            conversation = process_conversation(self.uid, self.language, conversation)
            messages = trigger_external_integrations(self.uid, conversation)

            logger.info(
                "conversation_processed",
                uid=self.uid,
                session_id=self.session_id,
                conversation_id=conversation.id,
            )

        except Exception as e:
            logger.error(
                "conversation_processing_failed",
                uid=self.uid,
                session_id=self.session_id,
                conversation_id=conversation.id,
                error=str(e),
                exc_info=True,
            )
            conversations_db.set_conversation_as_discarded(self.uid, conversation.id)
            conversation.discarded = True
            messages = []

        self.send_message_event(
            ConversationEvent(event_type="memory_created", memory=conversation, messages=messages)
        )

    async def _process_current_conversation(self, conversation_id: str):
        """
        Process the current conversation and create a new one.

        Args:
            conversation_id: The conversation ID to process
        """
        logger.info(
            "processing_current_conversation",
            uid=self.uid,
            session_id=self.session_id,
            conversation_id=conversation_id,
        )

        conversation = conversations_db.get_conversation(self.uid, conversation_id)

        if conversation:
            has_content = conversation.get('transcript_segments') or conversation.get('photos')

            if has_content:
                await self._create_conversation(conversation)
            else:
                logger.info(
                    "deleting_empty_conversation",
                    uid=self.uid,
                    session_id=self.session_id,
                    conversation_id=conversation_id,
                )
                conversations_db.delete_conversation(self.uid, conversation_id)

        await self._create_new_in_progress_conversation()

    def update_in_progress_conversation(
        self,
        segments: List[TranscriptSegment],
        photos: List[ConversationPhoto],
        finished_at: datetime,
        segment_person_assignment_map: dict,
    ) -> Optional[Tuple[Conversation, Tuple[int, int]]]:
        """
        Update the current in-progress conversation with new segments/photos.

        Args:
            segments: New transcript segments
            photos: New photos
            finished_at: Updated finished_at timestamp
            segment_person_assignment_map: Map of segment IDs to person IDs

        Returns:
            Tuple of (conversation, (start_index, end_index)) or None
        """
        if not self.current_conversation_id:
            logger.warning(
                "no_current_conversation_for_update",
                uid=self.uid,
                session_id=self.session_id,
            )
            return None

        # Use distributed locking to prevent race conditions
        try:
            with conversation_lock(self.uid, self.current_conversation_id, timeout=60):
                conversation_data = conversations_db.get_conversation(self.uid, self.current_conversation_id)

                if not conversation_data:
                    logger.warning(
                        "conversation_not_found_for_update",
                        uid=self.uid,
                        session_id=self.session_id,
                        conversation_id=self.current_conversation_id,
                    )
                    return None

                conversation = Conversation(**conversation_data)
                starts, ends = (0, 0)

                if segments:
                    # Update started_at if this is the first segment
                    if not conversation.transcript_segments:
                        started_at = finished_at - timedelta(seconds=max(0, segments[-1].end))
                        conversations_db.update_conversation(self.uid, conversation.id, {'started_at': started_at})
                        conversation.started_at = started_at

                    # Combine segments
                    conversation.transcript_segments, (starts, ends) = TranscriptSegment.combine_segments(
                        conversation.transcript_segments, segments
                    )

                    # Process speaker assignments
                    self._process_speaker_assigned_segments(
                        conversation.transcript_segments[starts:ends],
                        segment_person_assignment_map,
                    )

                    # Update database
                    conversations_db.update_conversation_segments(
                        self.uid,
                        conversation.id,
                        [segment.dict() for segment in conversation.transcript_segments],
                    )

                    logger.debug(
                        "conversation_segments_updated",
                        uid=self.uid,
                        session_id=self.session_id,
                        conversation_id=conversation.id,
                        new_segments=ends - starts,
                    )

                if photos:
                    conversations_db.store_conversation_photos(self.uid, conversation.id, photos)

                    # Update source if we now have photos
                    if conversation.source != ConversationSource.openglass:
                        conversations_db.update_conversation(
                            self.uid,
                            conversation.id,
                            {'source': ConversationSource.openglass},
                        )
                        conversation.source = ConversationSource.openglass

                    logger.debug(
                        "conversation_photos_added",
                        uid=self.uid,
                        session_id=self.session_id,
                        conversation_id=conversation.id,
                        photos_count=len(photos),
                    )

                # Update finished_at
                conversations_db.update_conversation_finished_at(self.uid, conversation.id, finished_at)

                return conversation, (starts, ends)

        except Exception as e:
            logger.error(
                "conversation_update_failed",
                uid=self.uid,
                session_id=self.session_id,
                conversation_id=self.current_conversation_id,
                error=str(e),
                exc_info=True,
            )
            return None

    def _process_speaker_assigned_segments(
        self,
        transcript_segments: List[TranscriptSegment],
        segment_person_assignment_map: dict,
    ):
        """
        Apply speaker assignments to transcript segments.

        Args:
            transcript_segments: List of transcript segments to process
            segment_person_assignment_map: Map of segment IDs to person IDs
        """
        for segment in transcript_segments:
            if segment.id in segment_person_assignment_map and not segment.is_user and not segment.person_id:
                person_id = segment_person_assignment_map[segment.id]
                if person_id == 'user':
                    segment.is_user = True
                    segment.person_id = None
                else:
                    segment.is_user = False
                    segment.person_id = person_id

    async def conversation_lifecycle_monitor(self):
        """
        Background task that monitors conversation timeout.

        Checks every 5 seconds if the current conversation should be processed.
        """
        logger.info(
            "conversation_lifecycle_monitor_started",
            uid=self.uid,
            session_id=self.session_id,
            timeout=self.conversation_timeout,
        )

        while self.active:
            await asyncio.sleep(5)

            if not self.current_conversation_id:
                logger.warning(
                    "no_current_conversation_in_monitor",
                    uid=self.uid,
                    session_id=self.session_id,
                )
                continue

            conversation = conversations_db.get_conversation(self.uid, self.current_conversation_id)

            if not conversation:
                logger.warning(
                    "current_conversation_not_found_in_monitor",
                    uid=self.uid,
                    session_id=self.session_id,
                    conversation_id=self.current_conversation_id,
                )
                await self._create_new_in_progress_conversation()
                continue

            # Check timeout
            finished_at = datetime.fromisoformat(conversation['finished_at'].isoformat())
            seconds_since_last_update = (datetime.now(timezone.utc) - finished_at).total_seconds()

            if seconds_since_last_update >= self.conversation_timeout:
                logger.info(
                    "conversation_timeout_triggered",
                    uid=self.uid,
                    session_id=self.session_id,
                    conversation_id=self.current_conversation_id,
                    seconds_since_last_update=seconds_since_last_update,
                )
                await self._process_current_conversation(self.current_conversation_id)

        logger.info(
            "conversation_lifecycle_monitor_stopped",
            uid=self.uid,
            session_id=self.session_id,
        )

    def stop(self):
        """Stop the conversation manager."""
        self.active = False
        logger.info("conversation_manager_stopped", uid=self.uid, session_id=self.session_id)
