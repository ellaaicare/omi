"""
Audio processing and STT service management.

This module handles audio data processing, codec conversion, and integration
with various Speech-to-Text (STT) services.
"""

import asyncio
import time
from typing import Callable, List, Optional

import opuslib  # type: ignore
from pydub import AudioSegment  # type: ignore

from utils.logging_config import get_logger
from utils.other.storage import get_profile_audio_if_exists
from utils.other.task import safe_create_task
from utils.stt.streaming import (
    STTService,
    get_stt_service_for_language,
    process_audio_dg,
    process_audio_soniox,
    process_audio_speechmatics,
    send_initial_file_path,
)

logger = get_logger(__name__)


class AudioProcessor:
    """
    Handles audio data processing and STT integration.

    Manages audio codec conversion, STT service connections, and speech profile processing.
    """

    def __init__(
        self,
        uid: str,
        session_id: str,
        language: str,
        sample_rate: int,
        codec: str,
        channels: int,
        include_speech_profile: bool,
        stream_transcript_callback: Callable[[List], None],
    ):
        """
        Initialize the audio processor.

        Args:
            uid: User ID
            session_id: Session ID for logging
            language: Language code for transcription
            sample_rate: Audio sample rate
            codec: Audio codec (pcm8, pcm16, opus, etc.)
            channels: Number of audio channels
            include_speech_profile: Whether to include speech profile for personalization
            stream_transcript_callback: Callback for transcript segments
        """
        self.uid = uid
        self.session_id = session_id
        self.language = language
        self.sample_rate = sample_rate
        self.codec = codec
        self.channels = channels
        self.include_speech_profile = include_speech_profile
        self.stream_transcript_callback = stream_transcript_callback

        # Frame size for opus codec
        self.frame_size = 160
        if codec == "opus_fs320":
            self.codec = "opus"
            self.frame_size = 320

        # STT service configuration
        self.stt_service: Optional[STTService] = None
        self.stt_language: Optional[str] = None
        self.stt_model: Optional[str] = None

        # STT sockets
        self.deepgram_socket = None
        self.deepgram_socket2 = None
        self.soniox_socket = None
        self.soniox_socket2 = None
        self.speechmatics_socket = None

        # Speech profile
        self.speech_profile_duration = 0
        self.speech_profile_processed = False
        self.timer_start: Optional[float] = None

        # Opus decoder
        self.decoder = opuslib.Decoder(sample_rate, 1) if codec == "opus" else None

        logger.info(
            "audio_processor_created",
            uid=uid,
            session_id=session_id,
            language=language,
            sample_rate=sample_rate,
            codec=codec,
        )

    async def initialize_stt(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Initialize the STT service.

        Returns:
            Tuple of (success, stt_language, translation_language)
        """
        # Convert 'auto' to 'multi' for consistency
        language = 'multi' if self.language == 'auto' else self.language

        # Determine the best STT service
        self.stt_service, self.stt_language, self.stt_model = get_stt_service_for_language(language)

        if not self.stt_service or not self.stt_language:
            logger.error(
                "stt_service_not_supported",
                uid=self.uid,
                session_id=self.session_id,
                language=language,
            )
            return False, None, None

        logger.info(
            "stt_service_selected",
            uid=self.uid,
            session_id=self.session_id,
            stt_service=self.stt_service.value,
            stt_language=self.stt_language,
            stt_model=self.stt_model,
        )

        # Determine translation language
        translation_language = None
        if self.stt_language == 'multi':
            if language == "multi":
                # Import here to avoid circular dependency
                import database.users as user_db

                user_language_preference = user_db.get_user_language_preference(self.uid)
                if user_language_preference:
                    translation_language = user_language_preference
            else:
                translation_language = language

        # Load speech profile
        file_path = None
        if (
            (language == 'en' or language == 'auto')
            and (self.codec == 'opus' or self.codec == 'pcm16')
            and self.include_speech_profile
        ):
            file_path = get_profile_audio_if_exists(self.uid)
            if file_path:
                self.speech_profile_duration = AudioSegment.from_wav(file_path).duration_seconds + 5
                logger.info(
                    "speech_profile_loaded",
                    uid=self.uid,
                    session_id=self.session_id,
                    duration=self.speech_profile_duration,
                )

        self.speech_profile_processed = not (self.speech_profile_duration > 0)

        # Initialize STT service connections
        try:
            await self._initialize_stt_connections(file_path)
            self.timer_start = time.time()
            return True, self.stt_language, translation_language

        except Exception as e:
            logger.error(
                "stt_initialization_failed",
                uid=self.uid,
                session_id=self.session_id,
                error=str(e),
                exc_info=True,
            )
            return False, None, None

    async def _initialize_stt_connections(self, speech_profile_file: Optional[str]):
        """
        Initialize connections to STT services.

        Args:
            speech_profile_file: Path to speech profile audio file
        """
        # DEEPGRAM
        if self.stt_service == STTService.deepgram:
            self.deepgram_socket = await process_audio_dg(
                self.stream_transcript_callback,
                self.stt_language,
                self.sample_rate,
                1,
                preseconds=self.speech_profile_duration,
                model=self.stt_model,
            )

            if self.speech_profile_duration and speech_profile_file:
                self.deepgram_socket2 = await process_audio_dg(
                    self.stream_transcript_callback,
                    self.stt_language,
                    self.sample_rate,
                    1,
                    model=self.stt_model,
                )

                async def deepgram_socket_send(data):
                    return self.deepgram_socket.send(data)

                safe_create_task(send_initial_file_path(speech_profile_file, deepgram_socket_send))

            logger.info("deepgram_initialized", uid=self.uid, session_id=self.session_id)

        # SONIOX
        elif self.stt_service == STTService.soniox:
            # Language hints for multi-language detection
            hints = None
            if self.stt_language == 'multi' and self.language != 'multi':
                hints = [self.language]

            self.soniox_socket = await process_audio_soniox(
                self.stream_transcript_callback,
                self.sample_rate,
                self.stt_language,
                self.uid if self.include_speech_profile else None,
                preseconds=self.speech_profile_duration,
                language_hints=hints,
            )

            if self.speech_profile_duration and speech_profile_file:
                self.soniox_socket2 = await process_audio_soniox(
                    self.stream_transcript_callback,
                    self.sample_rate,
                    self.stt_language,
                    self.uid if self.include_speech_profile else None,
                    language_hints=hints,
                )

                safe_create_task(send_initial_file_path(speech_profile_file, self.soniox_socket.send))

            logger.info(
                "soniox_initialized",
                uid=self.uid,
                session_id=self.session_id,
                speech_profile_duration=self.speech_profile_duration,
            )

        # SPEECHMATICS
        elif self.stt_service == STTService.speechmatics:
            self.speechmatics_socket = await process_audio_speechmatics(
                self.stream_transcript_callback,
                self.sample_rate,
                self.stt_language,
                preseconds=self.speech_profile_duration,
            )

            if self.speech_profile_duration and speech_profile_file:
                safe_create_task(send_initial_file_path(speech_profile_file, self.speechmatics_socket.send))

            logger.info(
                "speechmatics_initialized",
                uid=self.uid,
                session_id=self.session_id,
                speech_profile_duration=self.speech_profile_duration,
            )

    async def process_audio_data(self, audio_data: bytes) -> bytes:
        """
        Process incoming audio data and send to STT service.

        Args:
            audio_data: Raw audio data bytes

        Returns:
            Processed audio data (decoded if needed)
        """
        # Decode opus if needed
        if self.codec == 'opus' and self.sample_rate == 16000:
            audio_data = self.decoder.decode(bytes(audio_data), frame_size=self.frame_size)

        # Send to STT services
        elapsed_seconds = time.time() - self.timer_start if self.timer_start else 0

        # Soniox
        if self.soniox_socket is not None:
            if elapsed_seconds > self.speech_profile_duration or not self.soniox_socket2:
                await self.soniox_socket.send(audio_data)
                if self.soniox_socket2:
                    logger.debug(
                        "closing_soniox_socket2_profile_complete",
                        uid=self.uid,
                        session_id=self.session_id,
                    )
                    await self.soniox_socket2.close()
                    self.soniox_socket2 = None
                    self.speech_profile_processed = True
            else:
                await self.soniox_socket2.send(audio_data)

        # Speechmatics
        if self.speechmatics_socket is not None:
            await self.speechmatics_socket.send(audio_data)

        # Deepgram
        if self.deepgram_socket is not None:
            if elapsed_seconds > self.speech_profile_duration or not self.deepgram_socket2:
                self.deepgram_socket.send(audio_data)
                if self.deepgram_socket2:
                    logger.debug(
                        "closing_deepgram_socket2_profile_complete",
                        uid=self.uid,
                        session_id=self.session_id,
                    )
                    self.deepgram_socket2.finish()
                    self.deepgram_socket2 = None
                    self.speech_profile_processed = True
            else:
                self.deepgram_socket2.send(audio_data)

        return audio_data

    async def close(self):
        """Close all STT connections."""
        logger.info("closing_audio_processor", uid=self.uid, session_id=self.session_id)

        try:
            if self.deepgram_socket:
                self.deepgram_socket.finish()
            if self.deepgram_socket2:
                self.deepgram_socket2.finish()
            if self.soniox_socket:
                await self.soniox_socket.close()
            if self.soniox_socket2:
                await self.soniox_socket2.close()
            if self.speechmatics_socket:
                await self.speechmatics_socket.close()

            logger.info("audio_processor_closed", uid=self.uid, session_id=self.session_id)

        except Exception as e:
            logger.error(
                "audio_processor_close_error",
                uid=self.uid,
                session_id=self.session_id,
                error=str(e),
                exc_info=True,
            )


# Fix missing import
from typing import Tuple
