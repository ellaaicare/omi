import logging
import asyncio
import io
import json
import os
import struct
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Callable

import av
import numpy as np
import opuslib  # type: ignore

import lc3  # lc3py

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from fastapi.websockets import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from firebase_admin.auth import InvalidIdTokenError

from utils.speaker_assignment import (
    process_speaker_assigned_segments,
    update_speaker_assignment_maps,
    should_update_speaker_to_person_map,
)
import database.conversations as conversations_db
import database.calendar_meetings as calendar_db
import database.users as user_db
from database.users import get_user_conversation_lifecycle_preferences, get_user_transcription_preferences
from database import redis_db
from database.redis_db import (
    get_cached_user_geolocation,
    try_acquire_listen_lock,
)
from models.conversation import (
    Conversation,
    ConversationPhoto,
    ConversationSource,
    ConversationStatus,
    Geolocation,
    Structured,
    TranscriptSegment,
)
from models.message_event import (
    ConversationEvent,
    FREEMIUM_ACTION_SETUP_ON_DEVICE_STT,
    FreemiumThresholdReachedEvent,
    LastConversationEvent,
    MessageEvent,
    MessageServiceStatusEvent,
    PhotoDescribedEvent,
    PhotoProcessingEvent,
    SegmentsDeletedEvent,
    SpeakerLabelSuggestionEvent,
    TranslationEvent,
)
from models.transcript_segment import Translation
from models.users import PlanType
from utils.analytics import record_usage
from utils.app_integrations import trigger_external_integrations, trigger_realtime_integrations
from utils.apps import is_audio_bytes_app_enabled
from utils.conversations.location import get_google_maps_location
from utils.conversations.process_conversation import (
    mark_unexpected_conversation_processing_failed,
    process_conversation_with_outcome,
    process_conversation_with_transcript_redelivery,
    retrieve_in_progress_conversation,
)
from utils.conversations.capture_protocol import (
    CAPTURE_PROTOCOL_VERSION,
    capture_drain_diagnostic_correlation_matches,
    claim_capture_authority_for_reconnect,
    complete_rotated_capture,
    flush_capture_before_drained,
    install_capture_authority,
    mark_capture_drained,
    renew_capture_authority,
    require_capture_protocol_before_creation,
    valid_capture_drain_body,
)
from utils.capture_buffer import (
    PusherTranscriptBatch,
    acknowledge_capture_persistence_batch,
    capture_buffer_contains_conversation,
    deliver_all_pusher_transcript_batches,
    prepare_conversation_bound_capture_batch,
    queue_pusher_transcript_batch,
)
from utils.ella.memory_artwork_storage import acquire_memory_artwork_publication_lock
from utils.ella.account_diagnostics import CaptureDiagnosticCorrelation
from utils.ella.scanner_keyterms import cache_status as scanner_keyterm_cache_status
from utils.ella.scanner_keyterms import combine_deepgram_keyterms, get_scanner_keyterms
from utils.notifications import send_credit_limit_notification, send_silent_user_notification
from utils.other import endpoints as auth
from utils.other.storage import get_profile_audio_if_exists, get_user_has_speech_profile
from utils.other.task import safe_create_task
from utils.pusher import connect_to_trigger_pusher
from utils.speaker_identification import detect_speaker_from_text
from utils.stt.streaming import (
    SPEECH_PROFILE_FIXED_DURATION,
    SPEECH_PROFILE_PADDING_DURATION,
    SPEECH_PROFILE_STABILIZE_DELAY,
    STTService,
    get_stt_service_for_language,
    process_audio_dg,
    process_audio_grok,
    process_audio_soniox,
    process_audio_speechmatics,
    send_initial_file_path,
)
from utils.subscription import has_transcription_credits, get_remaining_transcription_seconds
from utils.translation import TranslationService
from utils.translation_cache import TranscriptSegmentLanguageCache
from utils.webhooks import get_audio_bytes_webhook_seconds
from utils.onboarding import OnboardingHandler
from ella.routers.auto_provision import (
    auto_provision_user,
    ensure_firestore_user_document,
    get_agent_cluster,
    listen_runtime_gate,
)
from ella.services.ai_consent import assert_current_ai_consent, require_current_ai_consent, resolve_processor
from ella.services.account_diagnostics import (
    DiagnosticCorrelationAuthorityError,
    validate_capture_diagnostic_correlation,
)

from utils.aac import AACDecoder
from utils.audio import AudioRingBuffer
from utils.stt.speaker_embedding import (
    extract_embedding_from_bytes,
    compare_embeddings,
    SPEAKER_MATCH_THRESHOLD,
)
from utils.speaker_sample_migration import maybe_migrate_person_samples

router = APIRouter()

DIAGNOSTIC_CORRELATION_VALIDATION_TIMEOUT_SECONDS = 0.25
DIAGNOSTIC_CORRELATION_STATUS_TIMEOUT_SECONDS = 0.05
PUSHER_ENABLED = bool(os.getenv('HOSTED_PUSHER_API_URL'))
CAPTURE_CONVERSATION_ID_KEY = "_capture_conversation_id"


def drain_capture_persistence_batches(
    uid: str,
    conversation_id: str,
    owner_id: str,
    capture_generation: Optional[str] = None,
) -> int:
    batch_ids = conversations_db.list_capture_persistence_batches(uid, conversation_id)
    for batch_id in batch_ids:
        result = conversations_db.commit_capture_persistence_batch(
            uid,
            conversation_id,
            batch_id,
            owner_id,
            capture_generation,
        )
        if result.get("status") == "ownership_lost":
            raise RuntimeError("capture_persistence_ownership_lost")
    return len(batch_ids)


def poll_capture_persistence_batches(
    uid: str,
    conversation_id: str,
    owner_id: str,
    capture_generation: Optional[str] = None,
) -> Tuple[int, bool]:
    """Commit one recovery scan and report whether the socket still owns the conversation."""
    batch_ids = conversations_db.list_capture_persistence_batches(uid, conversation_id)
    for batch_id in batch_ids:
        result = conversations_db.commit_capture_persistence_batch(
            uid,
            conversation_id,
            batch_id,
            owner_id,
            capture_generation,
        )
        if result.get("status") == "ownership_lost":
            return len(batch_ids), False
    return len(batch_ids), True


def should_keep_capture_recovery_polling(
    recovery_conversation_id: str,
    current_conversation_id: Optional[str],
    websocket_active: bool,
) -> bool:
    """Keep polling the active owned conversation for batches from a superseded socket."""
    return websocket_active and recovery_conversation_id == str(current_conversation_id or "").strip()


STT_LATENCY_LOGS_ENABLED = os.getenv('ELLA_STT_LATENCY_LOGS_ENABLED', 'true').lower() == 'true'
STT_LATENCY_CLIENT_EVENT_LIMIT = int(os.getenv('ELLA_STT_LATENCY_CLIENT_EVENT_LIMIT', '25'))
# Server-side STT override: when set, all sessions use this provider regardless of client request.
# Useful when a provider hallucinates on ambient noise (e.g. soniox/grok for wearable use).
STT_FORCE_SERVICE = os.getenv('STT_FORCE_SERVICE', '').strip().lower() or None
PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS = float(os.getenv('PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS', '120'))

# Freemium: Send notification when credits threshold is reached
FREEMIUM_THRESHOLD_SECONDS = 180  # 3 minutes remaining - notify user


class CustomSttMode(str, Enum):
    disabled = "disabled"
    enabled = "enabled"


def _utc_iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _elapsed_ms(start: Optional[float], end: Optional[float] = None) -> Optional[int]:
    if start is None:
        return None
    end = end if end is not None else time.time()
    return int((end - start) * 1000)


def _client_ts_to_ms(value) -> Optional[int]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    # Accept either seconds or milliseconds from clients.
    if numeric < 10_000_000_000:
        numeric *= 1000
    return int(numeric)


def _stt_service_value(service: Optional[STTService]) -> Optional[str]:
    return service.value if isinstance(service, STTService) else service


async def _validate_socket_diagnostic_correlation(
    websocket: WebSocket,
    uid: str,
) -> Optional[CaptureDiagnosticCorrelation]:
    """Validate evidence after auth without allowing diagnostics to block capture."""
    rejection_code: Optional[str] = None
    try:
        return await asyncio.wait_for(
            validate_capture_diagnostic_correlation(uid, websocket.headers),
            timeout=DIAGNOSTIC_CORRELATION_VALIDATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        rejection_code = "diagnostic_store_unavailable"
    except DiagnosticCorrelationAuthorityError as exc:
        rejection_code = exc.code
    except Exception:
        rejection_code = "diagnostic_store_unavailable"

    if rejection_code is not None:
        try:
            await asyncio.wait_for(
                websocket.send_json(
                    {
                        "type": "service_status",
                        "status": "diagnostic_correlation_rejected",
                        "status_text": rejection_code,
                        "evidence_only": True,
                    }
                ),
                timeout=DIAGNOSTIC_CORRELATION_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
    return None


async def _stream_handler(
    websocket: WebSocket,
    uid: str,
    language: str = 'en',
    sample_rate: int = 8000,
    codec: str = 'pcm8',
    channels: int = 1,
    include_speech_profile: bool = True,
    stt_service: Optional[STTService] = None,
    conversation_timeout: int = 120,
    source: Optional[str] = None,
    custom_stt_mode: CustomSttMode = CustomSttMode.disabled,
    onboarding_mode: bool = False,
    speaker_auto_assign_enabled: bool = False,
    capture_protocol: int = 0,
    socket_accepted_at: Optional[float] = None,
    diagnostic_correlation: Optional[CaptureDiagnosticCorrelation] = None,
):
    """
    Core WebSocket streaming handler. Assumes websocket is already accepted and uid is validated.
    This function is called by both _listen (for app clients) and web_listen_handler (for web clients).
    """
    session_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    owner_token = session_id
    if not await require_capture_protocol_before_creation(websocket, capture_protocol):
        return
    session_started_at = time.time()
    socket_accepted_at = socket_accepted_at or session_started_at
    if STT_FORCE_SERVICE:
        try:
            requested_stt_service = STTService(STT_FORCE_SERVICE)
            print(f"[STT] Server override: forcing {STT_FORCE_SERVICE} (client requested {stt_service})")
        except ValueError:
            requested_stt_service = stt_service
    else:
        requested_stt_service = stt_service
    selected_stt_service: Optional[STTService] = None
    selected_stt_language: Optional[str] = None
    selected_stt_model: Optional[str] = None
    current_conversation_id = None
    capture_drained = False
    conversation_finalize_tasks: set[asyncio.Task] = set()
    first_audio_frame_at: Optional[float] = None
    first_interim_result_at: Optional[float] = None
    first_stt_result_at: Optional[float] = None
    first_transcript_buffered_at: Optional[float] = None
    first_transcript_dispatched_at: Optional[float] = None
    stt_connect_started_at: Optional[float] = None
    stt_connect_ready_at: Optional[float] = None
    speech_profile_state: dict = {}
    client_latency_events: List[dict] = []

    def _latency_metadata() -> dict:
        return {
            "session_id": session_id,
            "socket_accepted_at": _utc_iso_from_ts(socket_accepted_at),
            "first_audio_frame_at": _utc_iso_from_ts(first_audio_frame_at) if first_audio_frame_at else None,
            "selected_stt_provider": _stt_service_value(selected_stt_service),
            "selected_stt_language": selected_stt_language,
            "selected_stt_model": selected_stt_model,
            "stt_connect_ready_at": _utc_iso_from_ts(stt_connect_ready_at) if stt_connect_ready_at else None,
            "first_interim_result_at": _utc_iso_from_ts(first_interim_result_at) if first_interim_result_at else None,
            "first_stt_result_at": _utc_iso_from_ts(first_stt_result_at) if first_stt_result_at else None,
            "first_transcript_buffered_at": (
                _utc_iso_from_ts(first_transcript_buffered_at) if first_transcript_buffered_at else None
            ),
            "first_transcript_dispatched_at": (
                _utc_iso_from_ts(first_transcript_dispatched_at) if first_transcript_dispatched_at else None
            ),
            "speech_profile": speech_profile_state,
            "client_latency_events": client_latency_events[-STT_LATENCY_CLIENT_EVENT_LIMIT:],
        }

    def _latency_log(event: str, **metadata) -> None:
        if not STT_LATENCY_LOGS_ENABLED:
            return
        now = time.time()
        payload = {
            "event": event,
            "uid": uid,
            "session_id": session_id,
            "conversation_id": str(current_conversation_id) if current_conversation_id else None,
            "at": _utc_iso_from_ts(now),
            "since_socket_accept_ms": _elapsed_ms(socket_accepted_at, now),
            "since_first_audio_ms": _elapsed_ms(first_audio_frame_at, now),
            "requested_stt_provider": _stt_service_value(requested_stt_service),
            "selected_stt_provider": _stt_service_value(selected_stt_service),
            "selected_stt_language": selected_stt_language,
            "selected_stt_model": selected_stt_model,
            "codec": codec,
            "sample_rate": sample_rate,
            "include_speech_profile": include_speech_profile,
        }
        payload.update({k: v for k, v in metadata.items() if v is not None})
        print(f"[STT-LATENCY] {json.dumps(payload, default=str)[:4000]}", flush=True)

    def _remember_client_latency_event(event: dict) -> None:
        name = str(event.get("event") or event.get("name") or event.get("stage") or "client_event")
        client_ts_ms = _client_ts_to_ms(
            event.get("client_ts_ms")
            or event.get("client_timestamp_ms")
            or event.get("client_sent_at_ms")
            or event.get("timestamp_ms")
            or event.get("ts_ms")
        )
        received_at = time.time()
        item = {
            "event": name,
            "client_ts_ms": client_ts_ms,
            "received_at": _utc_iso_from_ts(received_at),
            "sequence": event.get("sequence") or event.get("seq"),
            "delta_client_to_backend_ms": int(time.time() * 1000) - client_ts_ms if client_ts_ms else None,
        }
        client_latency_events.append(item)
        del client_latency_events[:-STT_LATENCY_CLIENT_EVENT_LIMIT]
        _latency_log("client_timestamp_received", client_event=item)

    def _stt_event_callback(event: dict) -> None:
        nonlocal first_interim_result_at
        result_type = event.get("result_type")
        if result_type == "interim" and first_interim_result_at is None:
            first_interim_result_at = time.time()
            _latency_log(
                "first_interim_result",
                provider=event.get("provider"),
                text=event.get("text"),
                since_stt_ready_ms=_elapsed_ms(stt_connect_ready_at, first_interim_result_at),
                since_first_audio_ms=_elapsed_ms(first_audio_frame_at, first_interim_result_at),
            )

    _latency_log("socket_accepted", source=source, custom_stt_mode=custom_stt_mode.value, onboarding=onboarding_mode)
    print(
        '_stream_handler',
        uid,
        session_id,
        language,
        sample_rate,
        codec,
        include_speech_profile,
        stt_service,
        conversation_timeout,
        f'custom_stt={custom_stt_mode}',
        f'onboarding={onboarding_mode}',
    )

    use_custom_stt = custom_stt_mode == CustomSttMode.enabled

    # Set Ella context for LLM proxy
    from utils.llm.clients import set_ella_context

    set_ella_context(uid=uid, task='transcription')

    # Helper to gate person_id based on client capability (backward compatibility)
    # OLD apps don't send speaker_auto_assign param -> receive empty person_id
    # NEW apps send speaker_auto_assign=enabled -> receive populated person_id
    def _person_id_for_client(person_id: str) -> str:
        if speaker_auto_assign_enabled:
            return person_id
        return ""

    # Onboarding mode overrides: no speech profile (creating new one), single language
    if onboarding_mode:
        include_speech_profile = False

    if not uid or len(uid) <= 0:
        await websocket.close(code=1008, reason="Bad uid")
        return

    user_has_credits = True if use_custom_stt else has_transcription_credits(uid)
    if not user_has_credits:
        try:
            await send_credit_limit_notification(uid)
        except Exception as e:
            print(f"Error sending credit limit notification: {e}", uid, session_id)

    # Frame size, codec
    frame_size: int = 160
    lc3_chunk_size: Optional[int] = None
    lc3_frame_duration_us: Optional[int] = None

    if codec == "opus_fs320":
        codec = "opus"
        frame_size = 320
    elif codec == "lc3_fs1030":
        codec = "lc3"
        lc3_chunk_size = 30  # 30 bytes per frame
        lc3_frame_duration_us = 10000  # 10ms = 10000 microseconds

    # Fetch user transcription preferences
    transcription_prefs = get_user_transcription_preferences(uid)
    single_language_mode = transcription_prefs.get('single_language_mode', False)
    vocabulary = transcription_prefs.get('vocabulary', [])

    try:
        conversation_lifecycle_prefs = get_user_conversation_lifecycle_preferences(uid)
    except Exception as e:
        print(f"Error fetching conversation lifecycle preferences: {e}", uid, session_id)
        conversation_lifecycle_prefs = {}

    # Onboarding mode: force single language for better accuracy
    if onboarding_mode:
        single_language_mode = True

    # Always include "Omi" as predefined vocabulary
    vocabulary = list({"Omi"} | set(vocabulary))
    scanner_keyterms = await get_scanner_keyterms(uid)
    vocabulary = combine_deepgram_keyterms(vocabulary, scanner_keyterms)
    scanner_keyterm_status = scanner_keyterm_cache_status(uid)
    if scanner_keyterms:
        _latency_log(
            "scanner_keyterms_applied",
            scanner_keyterms_count=len(scanner_keyterms),
            scanner_keyterms_source=scanner_keyterm_status.get("source", "cache"),
            scanner_keyterms_age_seconds=scanner_keyterm_status.get("age_seconds"),
            deepgram_keyterms_count=len(vocabulary),
        )
    elif scanner_keyterm_status.get("error"):
        _latency_log(
            "scanner_keyterms_unavailable",
            scanner_keyterms_error=scanner_keyterm_status.get("error"),
            deepgram_keyterms_count=len(vocabulary),
        )

    # Convert 'auto' to 'multi' for consistency
    language = 'multi' if language == 'auto' else language

    # Determine the best STT service. If the client explicitly requests a
    # provider through /v4/listen?stt_service=..., honor that provider instead
    # of silently falling back to server STT_SERVICE_MODELS order.
    stt_service, stt_language, stt_model = get_stt_service_for_language(
        language, multi_lang_enabled=not single_language_mode, preferred_service=requested_stt_service
    )
    selected_stt_service = stt_service
    selected_stt_language = stt_language
    selected_stt_model = stt_model
    _latency_log(
        "stt_provider_selected",
        selection_source="client_query" if requested_stt_service else "server_config",
        single_language_mode=single_language_mode,
    )
    if not stt_service or not stt_language:
        _latency_log("stt_provider_unsupported", requested_language=language)
        await websocket.close(code=1008, reason=f"The language is not supported, {language}")
        return
    if resolve_processor(_stt_service_value(stt_service) or "") is None:
        _latency_log("stt_provider_not_disclosed", provider=_stt_service_value(stt_service))
        await websocket.close(code=1011, reason="STT processor disclosure is incomplete")
        return

    # Translation language (disabled in single language mode)
    translation_language = None
    if single_language_mode:
        translation_language = None
    elif stt_language == 'multi':
        if language == "multi":
            user_language_preference = user_db.get_user_language_preference(uid)
            if user_language_preference:
                translation_language = user_language_preference
        else:
            translation_language = language

    websocket_active = True
    accepting_capture = True
    websocket_close_code = 1001  # Going Away, don't close with good from backend

    # Initialize segment buffers early (before onboarding handler needs them)
    realtime_segment_buffers = []
    realtime_photo_buffers: list[dict] = []
    image_chunks: dict[str, dict] = {}
    photo_processing_tasks: dict[str, tuple[str, asyncio.Task]] = {}
    capture_recovery_conversation_ids: set[str] = set()
    capture_buffers_changed = asyncio.Event()

    def bind_capture_conversation(item: dict) -> dict:
        conversation_id = str(current_conversation_id or "").strip()
        if not conversation_id:
            raise RuntimeError("capture_conversation_not_ready")
        item[CAPTURE_CONVERSATION_ID_KEY] = conversation_id
        return item

    # === Speaker Identification State ===
    RING_BUFFER_DURATION = 60.0  # seconds
    SPEAKER_ID_MIN_AUDIO = 2.0
    SPEAKER_ID_TARGET_AUDIO = 4.0

    audio_ring_buffer: Optional[AudioRingBuffer] = None
    speaker_id_segment_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
    person_embeddings_cache: Dict[str, dict] = {}  # person_id -> {embedding, name}
    speaker_id_enabled = False  # Will be set after private_cloud_sync_enabled is known

    # Onboarding handler
    onboarding_handler: Optional[OnboardingHandler] = None
    if onboarding_mode:

        async def send_onboarding_event(event: dict):
            if websocket_active and websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_json(event)
                except Exception as e:
                    print(f"Error sending onboarding event: {e}", uid, session_id)

        def onboarding_stream_transcript(segments: List[dict]):
            """Inject onboarding question segments into the transcript stream."""
            nonlocal realtime_segment_buffers
            for segment in segments:
                segment.setdefault("id", str(uuid.uuid4()))
                bind_capture_conversation(segment)
            realtime_segment_buffers.extend(segments)

        onboarding_handler = OnboardingHandler(uid, send_onboarding_event, onboarding_stream_transcript)
        asyncio.create_task(onboarding_handler.send_current_question())

    locked_conversation_ids: Set[str] = set()
    speaker_to_person_map: Dict[int, Tuple[str, str]] = {}
    segment_person_assignment_map: Dict[str, str] = {}
    current_session_segments: Dict[str, bool] = {}  # Store only speech_profile_processed status
    suggested_segments: Set[str] = set()
    first_audio_byte_timestamp: Optional[float] = None
    last_usage_record_timestamp: Optional[float] = None
    words_transcribed_since_last_record: int = 0
    last_transcript_time: Optional[float] = None
    freemium_threshold_sent = False  # Track if we've sent the freemium threshold notification

    async def _record_usage_periodically():
        nonlocal websocket_active, last_usage_record_timestamp, words_transcribed_since_last_record
        nonlocal last_audio_received_time, last_transcript_time, user_has_credits
        nonlocal freemium_threshold_sent

        while websocket_active:
            await asyncio.sleep(60)
            if not websocket_active:
                break

            if use_custom_stt:
                continue

            if last_usage_record_timestamp:
                current_time = time.time()
                transcription_seconds = int(current_time - last_usage_record_timestamp)

                words_to_record = words_transcribed_since_last_record
                words_transcribed_since_last_record = 0  # reset

                if transcription_seconds > 0 or words_to_record > 0:
                    record_usage(uid, transcription_seconds=transcription_seconds, words_transcribed=words_to_record)
                last_usage_record_timestamp = current_time

            # Freemium: Check remaining credits and notify when threshold reached
            remaining_seconds = get_remaining_transcription_seconds(uid)

            # Notify user when approaching limit (3 minutes remaining)
            if (
                remaining_seconds is not None
                and remaining_seconds <= FREEMIUM_THRESHOLD_SECONDS
                and not freemium_threshold_sent
            ):
                # Determine required action
                # Currently: user must setup on-device STT
                # Future: backend may auto-fallback to lower-tier cloud STT (action = ACTION_NONE)
                await _asend_message_event(
                    FreemiumThresholdReachedEvent(
                        remaining_seconds=remaining_seconds,
                        action=FREEMIUM_ACTION_SETUP_ON_DEVICE_STT,
                    )
                )
                freemium_threshold_sent = True

                # Also send push notification
                try:
                    await send_credit_limit_notification(uid)
                except Exception as e:
                    print(f"Error sending credit limit notification: {e}", uid, session_id)

            # Update credits state
            if remaining_seconds is not None and remaining_seconds <= 0:
                user_has_credits = False
            elif remaining_seconds is None or remaining_seconds > 0:
                user_has_credits = True
                # Reset threshold flag if credits were restored (new month, upgrade, etc.)
                if remaining_seconds is None or remaining_seconds > FREEMIUM_THRESHOLD_SECONDS:
                    freemium_threshold_sent = False

            # Silence notification logic for basic plan users
            user_subscription = user_db.get_user_valid_subscription(uid)
            if not user_subscription or user_subscription.plan == PlanType.basic:
                time_of_last_words = last_transcript_time or first_audio_byte_timestamp
                if (
                    last_audio_received_time
                    and time_of_last_words
                    and (last_audio_received_time - time_of_last_words) > 15 * 60
                ):
                    print(f"User {uid} has been silent for over 15 minutes. Sending notification.", session_id)
                    try:
                        await send_silent_user_notification(uid)
                    except Exception as e:
                        print(f"Error sending silent user notification: {e}", uid, session_id)

    async def _asend_message_event(msg: MessageEvent):
        nonlocal websocket_active
        if not websocket_active:
            return False
        try:
            await websocket.send_json(msg.to_json())
            return True
        except WebSocketDisconnect:
            print("WebSocket disconnected", uid, session_id)
            websocket_active = False
        except Exception as e:
            print(f"Can not send message event, error: {e}", uid, session_id)

        return False

    def _send_message_event(msg: MessageEvent):
        nonlocal websocket_active
        if not websocket_active:
            return
        return asyncio.create_task(_asend_message_event(msg))

    # Heart beat
    started_at = time.time()
    inactivity_timeout_seconds = 90
    last_audio_received_time = None
    last_activity_time = None

    # Send pong every 10s then handle it in the app \
    # since Starlette is not support pong automatically
    async def send_heartbeat():
        print("send_heartbeat", uid, session_id)
        nonlocal websocket_active
        nonlocal websocket_close_code
        nonlocal started_at
        nonlocal last_audio_received_time

        try:
            while websocket_active:
                # ping fast
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text("ping")
                else:
                    break

                if current_conversation_id and not redis_db.refresh_in_progress_conversation_id(
                    uid,
                    current_conversation_id,
                    session_id,
                ):
                    _latency_log(
                        "capture_socket_ownership_lost",
                        conversation_id=current_conversation_id,
                    )
                    websocket_close_code = 1001
                    websocket_active = False
                    break
                if current_conversation_id and not renew_capture_authority(
                    uid,
                    current_conversation_id,
                    generation_id,
                    owner_token,
                ):
                    _latency_log(
                        "capture_protocol_authority_lost",
                        conversation_id=current_conversation_id,
                    )
                    websocket_close_code = 1008
                    websocket_active = False
                    break

                # Inactivity timeout
                if last_activity_time and time.time() - last_activity_time > inactivity_timeout_seconds:
                    print(f"Session timeout due to inactivity ({inactivity_timeout_seconds}s)", uid, session_id)
                    websocket_close_code = 1001
                    websocket_active = False
                    break

                # next
                await asyncio.sleep(10)
        except WebSocketDisconnect:
            print("WebSocket disconnected", uid, session_id)
        except Exception as e:
            print(f'Heartbeat error: {e}', uid, session_id)
            websocket_close_code = 1011
        finally:
            websocket_active = False

    # Start heart beat
    heartbeat_task = asyncio.create_task(send_heartbeat())

    _send_message_event(
        MessageServiceStatusEvent(event_type="service_status", status="initiating", status_text="Service Starting")
    )

    # Validate user
    if not user_db.is_exists_user(uid):
        websocket_active = False
        await websocket.close(code=1008, reason="Bad user")
        return

    # Create or get conversation ID early for audio chunk storage
    private_cloud_sync_enabled = user_db.get_user_private_cloud_sync_enabled(uid)

    # Enable speaker identification if not custom STT and private cloud sync is enabled
    speaker_id_enabled = not use_custom_stt and private_cloud_sync_enabled
    if speaker_id_enabled:
        audio_ring_buffer = AudioRingBuffer(RING_BUFFER_DURATION, sample_rate)

    # Conversation timeout (to process the conversation after x seconds of silence)
    # Binary audio sessions keep the legacy 2m minimum. Custom STT sessions can
    # finalize faster because the app has already provided transcript segments.
    conversation_creation_timeout = conversation_timeout
    if conversation_creation_timeout == -1:
        conversation_creation_timeout = 4 * 60 * 60
    min_conversation_timeout = 10 if use_custom_stt else 120
    if conversation_creation_timeout < min_conversation_timeout:
        conversation_creation_timeout = min_conversation_timeout
    inactivity_timeout_seconds = max(inactivity_timeout_seconds, conversation_creation_timeout + 30)

    # Stream transcript
    # Callback for when pusher finishes processing a conversation
    def on_conversation_processed(conversation_id: str):
        conversation_data = conversations_db.get_conversation(uid, conversation_id)
        if conversation_data:
            complete_rotated_capture(uid, conversation_id, generation_id, owner_token)
            conversation = Conversation(**conversation_data)
            _send_message_event(ConversationEvent(event_type="memory_created", memory=conversation, messages=[]))

    def on_conversation_processing_started(conversation_id: str):
        conversation_data = conversations_db.get_conversation(uid, conversation_id)
        if conversation_data:
            conversation = Conversation(**conversation_data)
            _send_message_event(ConversationEvent(event_type="memory_processing_started", memory=conversation))

    # Fallback for when pusher is not available
    async def _create_conversation_fallback(conversation_data: dict):
        conversation = Conversation(**conversation_data)
        if conversation.status not in {ConversationStatus.processing, ConversationStatus.completed}:
            _send_message_event(ConversationEvent(event_type="memory_processing_started", memory=conversation))

        try:
            # Geolocation
            geolocation = get_cached_user_geolocation(uid)
            if geolocation:
                geolocation = Geolocation(**geolocation)
                conversation.geolocation = get_google_maps_location(geolocation.latitude, geolocation.longitude)

            outcome = await asyncio.to_thread(
                process_conversation_with_transcript_redelivery,
                uid,
                language,
                conversation,
            )
            conversation = outcome.conversation
            if not outcome.dispatched and outcome.status in {
                'processing_in_progress',
                conversations_db.conversation_stock_summary_transcript_changed,
            }:
                return
        except Exception as e:
            print(f"Error processing conversation: {e}", uid, session_id)
            mark_unexpected_conversation_processing_failed(uid, conversation)
            messages = []
        else:
            messages = []
            if outcome.dispatched:
                try:
                    messages = await asyncio.to_thread(trigger_external_integrations, uid, conversation)
                except Exception as e:
                    print(f"External integrations failed after conversation processing: {e}", uid, session_id)

        _send_message_event(ConversationEvent(event_type="memory_created", memory=conversation, messages=messages))

    async def cleanup_processing_conversations():
        processing = conversations_db.get_processing_conversations(uid)
        print('finalize_processing_conversations len(processing):', len(processing), uid, session_id)
        if not processing or len(processing) == 0:
            return

        for conversation in processing:
            if PUSHER_ENABLED:
                processing_result = await request_conversation_processing(conversation['id'])
                if processing_result == 'unavailable':
                    await _create_conversation_fallback(conversation)
            else:
                await _create_conversation_fallback(conversation)

    async def process_pending_conversations(timed_out_id: Optional[str]):
        await asyncio.sleep(7.0)
        if timed_out_id:
            await _process_conversation(timed_out_id)
        await cleanup_processing_conversations()

    # Send last completed conversation to client
    def send_last_conversation():
        last_conversation = conversations_db.get_last_completed_conversation(uid)
        if last_conversation:
            _send_message_event(LastConversationEvent(memory_id=last_conversation['id']))

    send_last_conversation()

    async def _publish_capture_protocol_ready(
        conversation_id: str,
        *,
        expected_conversation_id: Optional[str] = None,
        adopt: bool = False,
    ) -> bool:
        nonlocal websocket_active, websocket_close_code
        installed = install_capture_authority(
            uid,
            conversation_id,
            generation_id,
            owner_token,
            expected_conversation_id=expected_conversation_id,
            adopt=adopt,
            **({"diagnostic_correlation": diagnostic_correlation} if diagnostic_correlation is not None else {}),
        )
        if not installed:
            _latency_log(
                "capture_protocol_authority_install_failed",
                conversation_id=conversation_id,
                expected_conversation_id=expected_conversation_id,
            )
            websocket_close_code = 1008
            websocket_active = False
            return False
        delivered = await _asend_message_event(
            MessageServiceStatusEvent(
                status="capture_protocol_ready",
                protocol_version=CAPTURE_PROTOCOL_VERSION,
                conversation_id=conversation_id,
                generation=generation_id,
                owner_token=owner_token,
                **(diagnostic_correlation.receipt_fields() if diagnostic_correlation else {}),
                evidence_only=True if diagnostic_correlation else None,
            )
        )
        if not delivered:
            websocket_close_code = 1008
            websocket_active = False
            return False
        return True

    # Create new stub conversation for next batch
    async def _create_new_in_progress_conversation(
        *,
        expected_conversation_id: Optional[str] = None,
        expected_owner_id: Optional[str] = None,
        replace_stale_conversation_id: Optional[str] = None,
        new_owner_id: Optional[str] = session_id,
        adopt: bool = True,
    ) -> bool:
        nonlocal current_conversation_id, websocket_active

        conversation_source = ConversationSource.omi
        if source:
            try:
                conversation_source = ConversationSource(source)
            except ValueError:
                print(f"Invalid conversation source '{source}', defaulting to 'omi'", uid, session_id)
                conversation_source = ConversationSource.omi

        new_conversation_id = str(uuid.uuid4())
        stub_conversation = Conversation(
            id=new_conversation_id,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            structured=Structured(),
            language=language,
            transcript_segments=[],
            photos=[],
            status=ConversationStatus.in_progress,
            source=conversation_source,
            private_cloud_sync_enabled=private_cloud_sync_enabled,
        )
        stub_conversation_data = stub_conversation.dict()
        stub_conversation_data['capture_owner_id'] = str(new_owner_id or '').strip() or None
        conversations_db.upsert_conversation(uid, conversation_data=stub_conversation_data)

        detected_meeting_id = None

        # Only check for meetings if source is desktop
        if conversation_source == ConversationSource.desktop:
            now = datetime.now(timezone.utc)
            # Check ±2 minute window
            time_window = timedelta(minutes=2)
            start_range = now - time_window
            end_range = now + time_window

            meetings = calendar_db.get_meetings_in_time_range(uid, start_range, end_range)

            if len(meetings) == 1:
                # Exactly one meeting found
                detected_meeting_id = meetings[0]['id']
            elif len(meetings) > 1:
                closest_meeting = None
                smallest_diff = None

                for meeting in meetings:
                    # Calculate absolute time difference between meeting start and now
                    time_diff = abs((meeting['start_time'] - now).total_seconds())

                    if smallest_diff is None or time_diff < smallest_diff:
                        smallest_diff = time_diff
                        closest_meeting = meeting

                if closest_meeting:
                    detected_meeting_id = closest_meeting['id']
                    print(
                        f"Selected closest meeting: {closest_meeting['title']} (diff: {smallest_diff}s)",
                        uid,
                        session_id,
                    )

        # Store meeting association if auto-detected
        if detected_meeting_id:
            redis_db.set_conversation_meeting_id(new_conversation_id, detected_meeting_id)

        if expected_conversation_id is not None:
            transferred = conversations_db.transfer_capture_conversation_owner(
                uid,
                expected_conversation_id,
                expected_owner_id or '',
                new_conversation_id,
                new_owner_id,
            )
            if not transferred:
                abandoned = conversations_db.abandon_capture_conversation_if_owned(
                    uid,
                    new_conversation_id,
                    new_owner_id or '',
                )
                if detected_meeting_id and abandoned:
                    redis_db.remove_conversation_meeting_id(new_conversation_id)
                return False
            published = redis_db.rotate_in_progress_conversation_id(
                uid,
                expected_conversation_id,
                expected_owner_id or '',
                new_conversation_id,
                new_owner_id,
            )
        elif replace_stale_conversation_id is not None:
            published = redis_db.replace_stale_in_progress_conversation_id(
                uid,
                replace_stale_conversation_id,
                new_conversation_id,
                new_owner_id or '',
            )
        else:
            published = redis_db.claim_in_progress_conversation_id(
                uid,
                new_conversation_id,
                new_owner_id or '',
            )

        if not published:
            rolled_back = True
            if expected_conversation_id is not None:
                rolled_back = conversations_db.rollback_capture_conversation_owner_transfer(
                    uid,
                    expected_conversation_id,
                    expected_owner_id or '',
                    new_conversation_id,
                    new_owner_id,
                )
            abandoned = False
            if expected_conversation_id is None:
                abandoned = conversations_db.abandon_capture_conversation_if_owned(
                    uid,
                    new_conversation_id,
                    new_owner_id or '',
                )
            cleanup_succeeded = rolled_back if expected_conversation_id is not None else abandoned
            if detected_meeting_id and cleanup_succeeded:
                redis_db.remove_conversation_meeting_id(new_conversation_id)
            return False

        if replace_stale_conversation_id is not None:
            conversations_db.bind_capture_conversation_owner(uid, replace_stale_conversation_id, None)

        if adopt:
            current_conversation_id = new_conversation_id

        predecessor_id = expected_conversation_id or replace_stale_conversation_id
        if adopt and not await _publish_capture_protocol_ready(
            new_conversation_id,
            expected_conversation_id=predecessor_id,
        ):
            return False

        print(f"Created new stub conversation: {new_conversation_id}", uid, session_id)
        return True

    def _capture_buffers_contain_conversation(conversation_id: str) -> bool:
        exact_conversation_id = str(conversation_id or "").strip()
        if capture_buffer_contains_conversation(
            realtime_segment_buffers,
            realtime_photo_buffers,
            conversation_key=CAPTURE_CONVERSATION_ID_KEY,
            conversation_id=exact_conversation_id,
        ):
            return True
        if any(
            str(upload.get("conversation_id") or "").strip() == exact_conversation_id
            for upload in image_chunks.values()
        ):
            return True
        return any(
            bound_conversation_id == exact_conversation_id and not task.done()
            for bound_conversation_id, task in photo_processing_tasks.values()
        )

    async def _wait_for_capture_buffers_to_drain(
        conversation_id: str,
        *,
        timeout_seconds: float = 2.0,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while _capture_buffers_contain_conversation(conversation_id):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            capture_buffers_changed.clear()
            if not _capture_buffers_contain_conversation(conversation_id):
                return True
            try:
                await asyncio.wait_for(capture_buffers_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return not _capture_buffers_contain_conversation(conversation_id)
        return True

    async def _process_conversation(conversation_id: str, *, wait_for_buffers: bool = True) -> bool:
        print("_process_conversation", uid, session_id)
        if wait_for_buffers and not await _wait_for_capture_buffers_to_drain(conversation_id):
            _latency_log(
                "capture_finalize_deferred",
                conversation_id=conversation_id,
            )
            return False
        conversation = conversations_db.get_conversation(uid, conversation_id)
        if conversation:
            has_content = conversation.get('transcript_segments') or conversation.get('photos')
            if has_content:
                if PUSHER_ENABLED:
                    on_conversation_processing_started(conversation_id)
                    processing_result = await request_conversation_processing(conversation_id)
                    if processing_result == 'unavailable':
                        await _create_conversation_fallback(conversation)
                else:
                    await _create_conversation_fallback(conversation)
            else:
                print(f'Clean up the conversation {conversation_id}, reason: no content', uid, session_id)
                async with acquire_memory_artwork_publication_lock(uid) as artwork_lock_proof:
                    await run_in_threadpool(
                        conversations_db.delete_conversation,
                        uid,
                        conversation_id,
                        artwork_lock_proof=artwork_lock_proof,
                    )
            complete_rotated_capture(uid, conversation_id, generation_id, owner_token)
        return True

    async def _process_conversation_after_rotation(conversation_id: str) -> None:
        while True:
            if await _process_conversation(conversation_id):
                return
            await asyncio.sleep(0.25)

    def _schedule_conversation_processing_after_rotation(conversation_id: str) -> None:
        task = asyncio.create_task(_process_conversation_after_rotation(conversation_id))
        conversation_finalize_tasks.add(task)

        def processing_done(completed: asyncio.Task) -> None:
            conversation_finalize_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                _latency_log(
                    "capture_finalize_error",
                    conversation_id=conversation_id,
                    error=str(error)[:300],
                )

        task.add_done_callback(processing_done)

    async def _await_conversation_finalize_tasks() -> None:
        while conversation_finalize_tasks:
            await asyncio.gather(
                *tuple(conversation_finalize_tasks),
                return_exceptions=True,
            )

    async def _finalize_current_conversation_on_disconnect() -> None:
        conversation_id = str(current_conversation_id or "").strip()
        if not conversation_id:
            return

        conversation = conversations_db.get_conversation(uid, conversation_id)
        if not conversation or conversation.get('status') != ConversationStatus.in_progress:
            return

        if not await _wait_for_capture_buffers_to_drain(conversation_id, timeout_seconds=5.0):
            _latency_log(
                "capture_disconnect_finalize_deferred",
                conversation_id=conversation_id,
            )
            return

        drain_capture_persistence_batches(uid, conversation_id, session_id, generation_id)

        rotated = await _create_new_in_progress_conversation(
            expected_conversation_id=conversation_id,
            expected_owner_id=session_id,
            new_owner_id=None,
            adopt=False,
        )
        if not rotated:
            _latency_log(
                "capture_disconnect_finalize_skipped",
                conversation_id=conversation_id,
                reason="socket_ownership_lost",
            )
            return

        await _process_conversation(conversation_id, wait_for_buffers=False)

        _latency_log(
            "capture_disconnect_finalized",
            conversation_id=conversation_id,
        )

    # Process existing conversations
    async def _prepare_in_progess_conversations():
        nonlocal current_conversation_id

        for _ in range(3):
            active_conversation_id = redis_db.get_in_progress_conversation_id(uid)
            candidate = retrieve_in_progress_conversation(uid)
            if candidate:
                candidate_id = str(candidate.get('id') or '').strip()
                candidate_owner_id = str(candidate.get('capture_owner_id') or '').strip() or None
                authority_claimed = bool(candidate_id) and claim_capture_authority_for_reconnect(
                    uid,
                    candidate_id,
                    generation_id,
                    candidate_owner_id,
                    session_id,
                    **(
                        {"diagnostic_correlation": diagnostic_correlation} if diagnostic_correlation is not None else {}
                    ),
                )
                if not authority_claimed:
                    await asyncio.sleep(0)
                    continue
                claimed = bool(candidate_id) and redis_db.claim_in_progress_conversation_id(
                    uid, candidate_id, session_id
                )
                if not claimed and candidate_id and active_conversation_id and active_conversation_id != candidate_id:
                    claimed = redis_db.replace_stale_in_progress_conversation_id(
                        uid,
                        active_conversation_id,
                        candidate_id,
                        session_id,
                    )
                if not claimed:
                    mark_capture_drained(uid, candidate_id, generation_id, session_id)
                    await asyncio.sleep(0)
                    continue

                drain_capture_persistence_batches(uid, candidate_id, session_id, generation_id)

                finished_at = datetime.fromisoformat(candidate['finished_at'].isoformat())
                seconds_since_last_segment = (datetime.now(timezone.utc) - finished_at).total_seconds()
                if seconds_since_last_segment >= conversation_creation_timeout:
                    print(
                        f'Processing existing conversation {candidate["id"]} (timed out: {seconds_since_last_segment:.1f}s)',
                        uid,
                        session_id,
                    )
                    if not await _create_new_in_progress_conversation(
                        expected_conversation_id=candidate_id,
                        expected_owner_id=session_id,
                    ):
                        await asyncio.sleep(0)
                        continue
                    return candidate_id

                current_conversation_id = candidate_id
                if not await _publish_capture_protocol_ready(candidate_id, adopt=True):
                    return None
                capture_recovery_conversation_ids.add(candidate_id)
                print(
                    f"Resuming conversation {current_conversation_id}. Will timeout in {conversation_creation_timeout - seconds_since_last_segment:.1f}s",
                    uid,
                    session_id,
                )
                return None

            if await _create_new_in_progress_conversation(
                replace_stale_conversation_id=active_conversation_id or None,
            ):
                return None
            await asyncio.sleep(0)

        raise RuntimeError("active conversation ownership changed during reconnect")

    _send_message_event(
        MessageServiceStatusEvent(status="in_progress_conversations_processing", status_text="Processing Conversations")
    )
    timed_out_conversation_id = await _prepare_in_progess_conversations()
    capture_recovery_conversation_ids.update(
        conversation_id for conversation_id in (current_conversation_id, timed_out_conversation_id) if conversation_id
    )

    # STT
    # Validate websocket_active before initiating STT
    if not websocket_active or websocket.client_state != WebSocketState.CONNECTED:
        print("websocket was closed", uid, session_id)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=websocket_close_code)
            except Exception as e:
                print(f"Error closing WebSocket: {e}", uid, session_id)
        return

    # Process STT
    soniox_socket = None
    grok_socket = None
    soniox_profile_socket = None  # Temporary socket for speech profile phase
    speechmatics_socket = None
    deepgram_socket = None
    deepgram_profile_socket = None  # Temporary socket for speech profile phase
    speech_profile_complete = asyncio.Event()  # Signals when speech profile send is done
    speech_profile_preseconds = 0  # Set by _process_stt(); used by flush_stt_buffer reconnect

    def stream_transcript(segments):
        nonlocal realtime_segment_buffers
        nonlocal first_stt_result_at
        if segments and first_stt_result_at is None:
            first_stt_result_at = time.time()
            first_segment = segments[0] if isinstance(segments[0], dict) else {}
            result_provider = (
                first_segment.get("stt_provider") if isinstance(first_segment, dict) else None
            ) or _stt_service_value(stt_service)
            _latency_log(
                "first_final_result",
                segment_count=len(segments),
                provider=result_provider,
                since_stt_ready_ms=_elapsed_ms(stt_connect_ready_at, first_stt_result_at),
                since_stt_connect_start_ms=_elapsed_ms(stt_connect_started_at, first_stt_result_at),
            )
        for segment in segments or []:
            if isinstance(segment, dict):
                segment.setdefault("id", str(uuid.uuid4()))
                segment.setdefault("stt_provider", _stt_service_value(stt_service))
                bind_capture_conversation(segment)
        realtime_segment_buffers.extend(segments)

    async def _process_stt():
        nonlocal websocket_close_code
        nonlocal soniox_socket
        nonlocal soniox_profile_socket
        nonlocal speechmatics_socket
        nonlocal deepgram_socket
        nonlocal deepgram_profile_socket
        nonlocal grok_socket
        nonlocal speech_profile_preseconds
        nonlocal stt_connect_started_at
        nonlocal stt_connect_ready_at
        try:
            if use_custom_stt:
                speech_profile_complete.set()  # No speech profile needed
                _latency_log("stt_custom_mode_ready")
                print(f"Custom STT mode enabled - using suggested transcripts from app", uid, session_id)
                return None

            speech_profile_preseconds = 0
            has_speech_profile = False
            if (
                (language == 'en' or language == 'auto')
                and (codec == 'opus' or codec == 'pcm16')
                and include_speech_profile
            ):
                has_speech_profile = get_user_has_speech_profile(uid)
                if has_speech_profile:
                    speech_profile_preseconds = SPEECH_PROFILE_FIXED_DURATION + SPEECH_PROFILE_PADDING_DURATION

            speech_profile_state.clear()
            speech_profile_state.update(
                {
                    "include_speech_profile": include_speech_profile,
                    "has_speech_profile": has_speech_profile,
                    "preseconds": speech_profile_preseconds,
                    "fixed_duration": SPEECH_PROFILE_FIXED_DURATION,
                    "padding_duration": SPEECH_PROFILE_PADDING_DURATION,
                    "stabilize_delay": SPEECH_PROFILE_STABILIZE_DELAY,
                    "speech_profile_processed_initial": speech_profile_complete.is_set(),
                    "native_soniox_speaker_identification_enabled": False,
                }
            )
            _latency_log("speech_profile_state", speech_profile=speech_profile_state)

            # If no speech profile, mark as complete immediately
            if not has_speech_profile:
                speech_profile_complete.set()

            stt_connect_started_at = time.time()
            _latency_log(
                "stt_connection_start",
                provider=_stt_service_value(stt_service),
                stt_language=stt_language,
                stt_model=stt_model,
                speech_profile_preseconds=speech_profile_preseconds,
                interim_results_enabled=False,
            )

            # DEEPGRAM
            if stt_service == STTService.deepgram:
                deepgram_socket = await process_audio_dg(
                    stream_transcript,
                    stt_language,
                    sample_rate,
                    1,
                    preseconds=speech_profile_preseconds,
                    model=stt_model,
                    keywords=vocabulary[:100] if vocabulary else None,
                    stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                )
                if has_speech_profile:
                    deepgram_profile_socket = await process_audio_dg(
                        stream_transcript,
                        stt_language,
                        sample_rate,
                        1,
                        model=stt_model,
                        keywords=vocabulary[:100] if vocabulary else None,
                        stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                    )

            # SONIOX
            elif stt_service == STTService.soniox:
                # For multi-language detection, provide language hints if available
                hints = None
                if stt_language == 'multi' and language != 'multi':
                    # Include the original language as a hint for multi-language detection
                    hints = [language]

                try:
                    soniox_socket = await process_audio_soniox(
                        stream_transcript,
                        sample_rate,
                        stt_language,
                        uid if include_speech_profile else None,
                        preseconds=speech_profile_preseconds,
                        language_hints=hints,
                        stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                    )

                    # Create a second socket for initial speech profile if needed
                    if has_speech_profile:
                        soniox_profile_socket = await process_audio_soniox(
                            stream_transcript,
                            sample_rate,
                            stt_language,
                            uid if include_speech_profile else None,
                            language_hints=hints,
                            stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                        )
                except ValueError as e:
                    print(f"Soniox unavailable ({e}), falling back to Deepgram nova-3")
                    deepgram_socket = await process_audio_dg(
                        stream_transcript,
                        stt_language,
                        sample_rate,
                        1,
                        preseconds=speech_profile_preseconds,
                        model='nova-3',
                        keywords=vocabulary[:100] if vocabulary else None,
                        stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                    )

            # GROK (disabled - Whisper-based, hallucinates on ambient background noise)
            elif stt_service == STTService.grok:
                print("Grok STT selected but disabled for ambient use; routing to Deepgram nova-2")
                deepgram_socket = await process_audio_dg(
                    stream_transcript,
                    stt_language if stt_language != 'multi' else 'multi',
                    sample_rate,
                    1,
                    preseconds=speech_profile_preseconds,
                    model='nova-2-general',
                    keywords=vocabulary[:100] if vocabulary else None,
                    stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                )

            # SPEECHMATICS
            elif stt_service == STTService.speechmatics:
                speechmatics_socket = await process_audio_speechmatics(
                    stream_transcript, sample_rate, stt_language, preseconds=speech_profile_preseconds
                )

            stt_connect_ready_at = time.time()
            _latency_log(
                "stt_connection_ready",
                connect_latency_ms=_elapsed_ms(stt_connect_started_at, stt_connect_ready_at),
                provider=_stt_service_value(stt_service),
            )

            # Return background task to load and send speech profile
            if has_speech_profile:
                return _create_speech_profile_loader_task(lambda: websocket_active, sample_rate)
            return None

        except Exception as e:
            _latency_log("stt_connection_error", error=str(e)[:300], provider=_stt_service_value(stt_service))
            print(f"Initial processing error: {e}", uid, session_id)
            websocket_close_code = 1011
            await websocket.close(code=websocket_close_code)
            return None

    def _create_speech_profile_loader_task(is_active: Callable, audio_sample_rate: int):
        """Create async task to load speech profile and send to STT in background."""

        async def _process_speech_profile():
            try:
                # Check if we should stop before doing any work
                if not is_active():
                    return

                # Download file in background thread (not blocking main flow)
                profile_load_started_at = time.time()
                _latency_log("speech_profile_load_start")
                file_path = await asyncio.to_thread(get_profile_audio_if_exists, uid)

                if not file_path:
                    _latency_log(
                        "speech_profile_file_missing",
                        load_latency_ms=_elapsed_ms(profile_load_started_at),
                    )
                    print(f"Speech profile file not found for {uid}", session_id)
                    return

                _latency_log(
                    "speech_profile_file_loaded",
                    load_latency_ms=_elapsed_ms(profile_load_started_at),
                    provider=_stt_service_value(stt_service),
                )

                # Send to appropriate STT socket with fixed duration padding
                profile_send_started_at = time.time()
                if stt_service == STTService.deepgram and deepgram_socket:

                    async def deepgram_socket_send(data):
                        return deepgram_socket.send(data)

                    await send_initial_file_path(
                        file_path,
                        deepgram_socket_send,
                        is_active,
                        sample_rate=audio_sample_rate,
                        target_duration=SPEECH_PROFILE_FIXED_DURATION,
                    )
                elif stt_service == STTService.soniox and soniox_socket:
                    await send_initial_file_path(
                        file_path,
                        soniox_socket.send,
                        is_active,
                        sample_rate=audio_sample_rate,
                        target_duration=SPEECH_PROFILE_FIXED_DURATION,
                    )
                elif stt_service == STTService.grok and grok_socket:
                    await send_initial_file_path(
                        file_path,
                        grok_socket.send,
                        is_active,
                        sample_rate=audio_sample_rate,
                        target_duration=SPEECH_PROFILE_FIXED_DURATION,
                    )
                elif stt_service == STTService.speechmatics and speechmatics_socket:
                    await send_initial_file_path(
                        file_path,
                        speechmatics_socket.send,
                        is_active,
                        sample_rate=audio_sample_rate,
                        target_duration=SPEECH_PROFILE_FIXED_DURATION,
                    )
                _latency_log(
                    "speech_profile_sent",
                    send_latency_ms=_elapsed_ms(profile_send_started_at),
                    provider=_stt_service_value(stt_service),
                )

                # Stabilization delay before switching to main socket
                if is_active():
                    print(
                        f"Speech profile sent, waiting {SPEECH_PROFILE_STABILIZE_DELAY}s for stabilization",
                        uid,
                        session_id,
                    )
                    await asyncio.sleep(SPEECH_PROFILE_STABILIZE_DELAY)

            except Exception as e:
                _latency_log("speech_profile_error", error=str(e)[:300])
                print(f"Error loading speech profile in background: {e}", uid, session_id)
            finally:
                # Always signal completion so main socket routing can proceed
                speech_profile_complete.set()
                speech_profile_state["speech_profile_processed"] = True
                speech_profile_state["completed_at"] = _utc_iso_from_ts(time.time())
                _latency_log("speech_profile_complete", speech_profile=speech_profile_state)
                print(f"Speech profile complete flag set", uid, session_id)

        return asyncio.create_task(_process_speech_profile())

    # Pusher
    #
    def create_pusher_task_handler():
        nonlocal websocket_active
        nonlocal current_conversation_id

        pusher_ws = None
        pusher_connect_lock = asyncio.Lock()
        pusher_connected = False

        # Transcript
        segment_buffers: list[PusherTranscriptBatch] = []

        last_synced_conversation_id = None

        # Conversation processing
        pending_conversation_requests: dict[str, asyncio.Future] = {}
        pending_request_event = asyncio.Event()

        def fail_pending_conversation_requests() -> None:
            for future in tuple(pending_conversation_requests.values()):
                if not future.done():
                    future.set_result('unavailable')

        def transcript_send(segments, conversation_id: str):
            nonlocal segment_buffers
            queue_pusher_transcript_batch(
                segment_buffers,
                segments,
                conversation_id,
            )

        async def request_conversation_processing(conversation_id: str):
            """Request pusher to process a conversation."""
            nonlocal pusher_ws, pusher_connected, pending_conversation_requests, pending_request_event
            if not pusher_connected or not pusher_ws:
                print(f"Pusher not connected, falling back to local processing for {conversation_id}", uid, session_id)
                return 'unavailable'
            response = None
            try:
                existing = pending_conversation_requests.get(conversation_id)
                if existing is not None and not existing.done():
                    return await asyncio.shield(existing)
                response = asyncio.get_running_loop().create_future()
                pending_conversation_requests[conversation_id] = response
                pending_request_event.set()  # Signal the receiver
                data = bytearray()
                data.extend(struct.pack("I", 104))
                data.extend(bytes(json.dumps({"conversation_id": conversation_id, "language": language}), "utf-8"))
                await pusher_ws.send(data)
                print(f"Sent process_conversation request to pusher: {conversation_id}", uid, session_id)
                deadline = asyncio.get_running_loop().time() + PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS
                while websocket_active and asyncio.get_running_loop().time() < deadline:
                    try:
                        return await asyncio.wait_for(asyncio.shield(response), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                return 'unavailable'
            except Exception as e:
                print(f"Failed to send process_conversation request: {e}", uid, session_id)
                return 'unavailable'
            finally:
                if response is not None and pending_conversation_requests.get(conversation_id) is response:
                    pending_conversation_requests.pop(conversation_id, None)

        async def _transcript_flush(auto_reconnect: bool = True):
            nonlocal segment_buffers
            nonlocal pusher_ws
            nonlocal pusher_connected
            if pusher_connected and pusher_ws and len(segment_buffers) > 0:
                try:

                    async def send_payload(payload: dict) -> None:
                        data = bytearray()
                        data.extend(struct.pack("I", 102))
                        data.extend(bytes(json.dumps(payload), "utf-8"))
                        await pusher_ws.send(data)

                    await deliver_all_pusher_transcript_batches(
                        segment_buffers,
                        send_payload,
                    )
                except ConnectionClosed as e:
                    print(f"Pusher transcripts Connection closed: {e}", uid, session_id)
                    pusher_connected = False
                except Exception as e:
                    print(f"Pusher transcripts failed: {e}", uid, session_id)
            if auto_reconnect and pusher_connected is False and websocket_active:
                await connect()

        async def transcript_consume():
            nonlocal websocket_active
            nonlocal segment_buffers
            while websocket_active:
                await asyncio.sleep(1)
                if len(segment_buffers) > 0:
                    await _transcript_flush(auto_reconnect=True)

        # Audio bytes
        audio_buffers = bytearray()
        audio_buffer_last_received: float = None  # Track when last audio was received
        audio_bytes_enabled = (
            bool(get_audio_bytes_webhook_seconds(uid)) or is_audio_bytes_app_enabled(uid) or private_cloud_sync_enabled
        )

        def audio_bytes_send(audio_bytes: bytes, received_at: float):
            nonlocal audio_buffers, audio_buffer_last_received
            audio_buffers.extend(audio_bytes)
            audio_buffer_last_received = received_at

        async def _audio_bytes_flush(auto_reconnect: bool = True):
            nonlocal audio_buffers
            nonlocal audio_buffer_last_received
            nonlocal pusher_ws
            nonlocal pusher_connected
            nonlocal last_synced_conversation_id

            # Send conversation ID
            if (
                pusher_ws
                and current_conversation_id
                and (last_synced_conversation_id is None or current_conversation_id != last_synced_conversation_id)
            ):
                try:
                    # 103|conversation_id
                    data = bytearray()
                    data.extend(struct.pack("I", 103))
                    data.extend(bytes(current_conversation_id, "utf-8"))
                    await pusher_ws.send(data)
                    last_synced_conversation_id = current_conversation_id
                except ConnectionClosed as e:
                    print(f"Pusher audio_bytes Connection closed: {e}", uid, session_id)
                    pusher_connected = False
                except Exception as e:
                    print(f"Failed to send conversation_id to pusher: {e}", uid, session_id)

            # Send audio bytes
            if pusher_connected and pusher_ws and len(audio_buffers) > 0:
                try:
                    # Calculate buffer start time:
                    # buffer_start = last_received_time - buffer_duration
                    # buffer_duration = buffer_length_bytes / (sample_rate * 2 bytes per sample)
                    buffer_duration_seconds = len(audio_buffers) / (sample_rate * 2)
                    buffer_start_time = (audio_buffer_last_received or time.time()) - buffer_duration_seconds

                    # 101|timestamp(8 bytes double)|audio_data
                    data = bytearray()
                    data.extend(struct.pack("I", 101))
                    data.extend(struct.pack("d", buffer_start_time))
                    data.extend(audio_buffers.copy())
                    audio_buffers = bytearray()  # reset
                    await pusher_ws.send(data)
                except ConnectionClosed as e:
                    print(f"Pusher audio_bytes Connection closed: {e}", uid, session_id)
                    pusher_connected = False
                except Exception as e:
                    print(f"Pusher audio_bytes failed: {e}", uid, session_id)
            if auto_reconnect and pusher_connected is False and websocket_active:
                await connect()

        async def audio_bytes_consume():
            nonlocal websocket_active
            nonlocal audio_buffers
            nonlocal pusher_ws
            nonlocal pusher_connected
            while websocket_active:
                await asyncio.sleep(1)
                if len(audio_buffers) > 0:
                    await _audio_bytes_flush(auto_reconnect=True)

        async def pusher_receive():
            """Receive and handle messages from pusher."""
            nonlocal websocket_active, pusher_ws, pusher_connected, pending_conversation_requests, pending_request_event
            while websocket_active:
                # Wait efficiently until there's work to do
                if not pending_conversation_requests:
                    pending_request_event.clear()
                    try:
                        await asyncio.wait_for(pending_request_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue  # Check websocket_active

                if not pusher_connected or not pusher_ws:
                    await asyncio.sleep(0.5)
                    continue

                try:
                    msg = await asyncio.wait_for(pusher_ws.recv(), timeout=5.0)
                    if not msg or len(msg) < 4:
                        continue
                    header_type = struct.unpack('<I', msg[:4])[0]

                    # Conversation processed response
                    if header_type == 201:
                        result = json.loads(msg[4:].decode("utf-8"))
                        conversation_id = result.get("conversation_id")
                        response = pending_conversation_requests.get(conversation_id)

                        if "error" in result:
                            print(f"Conversation processing failed: {result['error']}", uid, session_id)
                            if response is not None and not response.done():
                                response.set_result('terminal_error')
                            continue

                        if result.get("success"):
                            print(f"Conversation processed by pusher: {conversation_id}", uid, session_id)
                            if response is not None and not response.done():
                                response.set_result('processed')
                            on_conversation_processed(conversation_id)
                        elif response is not None and not response.done():
                            response.set_result('terminal_error')

                except asyncio.TimeoutError:
                    continue  # Check loop conditions again
                except asyncio.CancelledError:
                    break
                except ConnectionClosed as e:
                    print(f"Pusher receive connection closed: {e}", uid, session_id)
                    pusher_connected = False
                    fail_pending_conversation_requests()
                except Exception as e:
                    print(f"Pusher receive error: {e}", uid, session_id)
                    fail_pending_conversation_requests()
                    await asyncio.sleep(0.5)

                # Reconnect outside try/except (same pattern as flush functions)
                if pusher_connected is False and websocket_active:
                    await connect()

        async def _flush():
            await _audio_bytes_flush(auto_reconnect=False)
            await _transcript_flush(auto_reconnect=False)

        async def connect():
            nonlocal pusher_connected
            nonlocal pusher_connect_lock
            nonlocal pusher_ws
            async with pusher_connect_lock:
                if pusher_connected:
                    return
                # drain
                if pusher_ws:
                    try:
                        await pusher_ws.close()
                        pusher_ws = None
                    except Exception as e:
                        print(f"Pusher draining failed: {e}", uid, session_id)
                # connect
                await _connect()

        async def _connect():
            nonlocal pusher_ws
            nonlocal pusher_connected
            nonlocal current_conversation_id

            try:
                pusher_ws = await connect_to_trigger_pusher(
                    uid, sample_rate, retries=5, is_active=lambda: websocket_active
                )
                if pusher_ws is None:
                    # Session ended during connection attempt
                    return
                pusher_connected = True
            except Exception as e:
                print(f"Exception in connect: {e}")

        async def close(code: int = 1000):
            fail_pending_conversation_requests()
            await _flush()
            if pusher_ws:
                await pusher_ws.close(code)

        async def send_speaker_sample_request(
            person_id: str,
            conv_id: str,
            segment_ids: List[str],
        ):
            """Send speaker sample extraction request to pusher with segment IDs."""
            nonlocal pusher_ws, pusher_connected
            if not pusher_connected or not pusher_ws:
                return
            try:
                data = bytearray()
                data.extend(struct.pack("I", 105))
                data.extend(
                    bytes(
                        json.dumps(
                            {
                                "person_id": person_id,
                                "conversation_id": conv_id,
                                "segment_ids": segment_ids,
                            }
                        ),
                        "utf-8",
                    )
                )
                await pusher_ws.send(data)
                print(
                    f"Sent speaker sample request to pusher: person={person_id}, {len(segment_ids)} segments",
                    uid,
                    session_id,
                )
            except Exception as e:
                print(f"Failed to send speaker sample request: {e}", uid, session_id)

        def is_connected():
            return pusher_connected

        return (
            connect,
            close,
            transcript_send,
            transcript_consume,
            audio_bytes_send if audio_bytes_enabled else None,
            audio_bytes_consume if audio_bytes_enabled else None,
            request_conversation_processing,
            pusher_receive,
            is_connected,
            send_speaker_sample_request,
        )

    transcript_send = None
    transcript_consume = None
    audio_bytes_send = None
    audio_bytes_consume = None
    pusher_close = None
    pusher_connect = None
    request_conversation_processing = None
    pusher_receive = None
    pusher_is_connected = None
    send_speaker_sample_request = None

    # Transcripts
    #
    translation_enabled = translation_language is not None
    language_cache = TranscriptSegmentLanguageCache()
    translation_service = TranslationService()

    async def translate(segments: List[TranscriptSegment], conversation_id: str):
        if not translation_language:
            return

        try:
            translated_segments = []
            for segment in segments:
                if not segment or not segment.id:
                    continue

                segment_text = segment.text.strip()
                if not segment_text:
                    continue

                # Language Detection
                if language_cache.is_in_target_language(segment.id, segment_text, translation_language):
                    continue

                # Translation
                translated_text = translation_service.translate_text_by_sentence(translation_language, segment_text)

                if translated_text == segment_text:
                    # If translation is same as original, it's likely in the target language.
                    # Delete from cache to allow re-evaluation if more text is added.
                    language_cache.delete_cache(segment.id)
                    continue

                # Create/Update Translation object
                translation = Translation(lang=translation_language, text=translated_text)
                if segment.translations is not None:
                    existing_translation_index = next(
                        (i for i, t in enumerate(segment.translations) if t.lang == language), None
                    )
                    if existing_translation_index is not None:
                        segment.translations[existing_translation_index] = translation
                    else:
                        segment.translations.append(translation)

                translated_segments.append(segment)

            if not translated_segments:
                return

            # Persist and notify
            conversation = conversations_db.get_conversation(uid, conversation_id)
            if conversation:
                should_update = False
                for segment in translated_segments:
                    for i, existing_segment in enumerate(conversation['transcript_segments']):
                        if existing_segment['id'] == segment.id:
                            conversation['transcript_segments'][i]['translations'] = segment.dict()['translations']
                            should_update = True
                            break
                if should_update:
                    conversations_db.update_conversation_segments(
                        uid, conversation_id, conversation['transcript_segments']
                    )

            if websocket_active:
                _send_message_event(TranslationEvent(segments=[s.dict() for s in translated_segments]))

        except Exception as e:
            print(f"Translation error: {e}", uid, session_id)

    async def conversation_lifecycle_manager():
        """Background task that checks conversation timeout and triggers processing every 5 seconds."""
        nonlocal websocket_active, current_conversation_id, conversation_creation_timeout

        print(f"Starting conversation lifecycle manager (timeout: {conversation_creation_timeout}s)", uid, session_id)

        while websocket_active:
            await asyncio.sleep(5)

            if not current_conversation_id:
                print(f"WARN: the current conversation is not valid", uid, session_id)
                continue

            conversation = conversations_db.get_conversation(uid, current_conversation_id)
            if not conversation:
                print(f"WARN: the current conversation is not found (id: {current_conversation_id})", uid, session_id)
                if not await _create_new_in_progress_conversation(
                    expected_conversation_id=current_conversation_id,
                    expected_owner_id=session_id,
                ):
                    _latency_log(
                        "capture_rotation_skipped",
                        conversation_id=current_conversation_id,
                        reason="socket_ownership_lost",
                    )
                    websocket_active = False
                    return
                continue

            # Check if conversation status is not in_progress
            if conversation.get('status') != ConversationStatus.in_progress:
                print(
                    f"WARN: conversation {current_conversation_id} status is {conversation.get('status')}, not in_progress. Creating new conversation.",
                    uid,
                    session_id,
                )
                if not await _create_new_in_progress_conversation(
                    expected_conversation_id=current_conversation_id,
                    expected_owner_id=session_id,
                ):
                    _latency_log(
                        "capture_rotation_skipped",
                        conversation_id=current_conversation_id,
                        reason="socket_ownership_lost",
                    )
                    websocket_active = False
                    return
                continue

            # Check if conversation should be processed
            now = datetime.now(timezone.utc)
            finished_at = datetime.fromisoformat(conversation['finished_at'].isoformat())
            seconds_since_last_update = (now - finished_at).total_seconds()

            split_reason = None
            split_elapsed_seconds = seconds_since_last_update
            split_limit_seconds = conversation_creation_timeout
            if seconds_since_last_update >= conversation_creation_timeout:
                split_reason = "silence_timeout"
            else:
                try:
                    from ella.services.conversation_lifecycle import should_split_for_max_duration

                    decision = should_split_for_max_duration(conversation, now, conversation_lifecycle_prefs)
                    if decision.should_split:
                        split_reason = decision.reason or "max_duration"
                        split_elapsed_seconds = decision.elapsed_seconds
                        split_limit_seconds = decision.limit_seconds or 0
                except ImportError:
                    pass
                except Exception as e:
                    print(f"Error checking max conversation duration: {e}", uid, session_id)

            if split_reason:
                print(
                    f"Conversation {current_conversation_id} split triggered ({split_reason}: {split_elapsed_seconds:.1f}s/{split_limit_seconds}s). Processing...",
                    uid,
                    session_id,
                )
                conversation_id_to_process = current_conversation_id
                if not await _wait_for_capture_buffers_to_drain(
                    conversation_id_to_process,
                    timeout_seconds=5.0,
                ):
                    _latency_log(
                        "capture_rotation_deferred",
                        conversation_id=conversation_id_to_process,
                    )
                    continue
                drain_capture_persistence_batches(
                    uid,
                    conversation_id_to_process,
                    session_id,
                    generation_id,
                )
                if not await _create_new_in_progress_conversation(
                    expected_conversation_id=conversation_id_to_process,
                    expected_owner_id=session_id,
                ):
                    _latency_log(
                        "capture_rotation_skipped",
                        conversation_id=conversation_id_to_process,
                        reason="socket_ownership_lost",
                    )
                    websocket_active = False
                    return
                _schedule_conversation_processing_after_rotation(conversation_id_to_process)

    async def speaker_identification_task():
        """Consume segment queue, accumulate per speaker, trigger match when ready."""
        nonlocal websocket_active, speaker_to_person_map
        nonlocal person_embeddings_cache, audio_ring_buffer

        if not speaker_id_enabled:
            return

        # Load person embeddings (migrate if needed for v2 API compatibility)
        try:
            people = user_db.get_people(uid)
            for person in people:
                # Migrate if needed for v2 API compatibility
                if person.get('speech_samples'):
                    person = await maybe_migrate_person_samples(uid, person)

                # Skip cache if migration failed (version still <3) to avoid mixing embedding spaces
                if person.get('speech_samples_version', 1) < 3:
                    continue

                emb = person.get('speaker_embedding')
                if emb:
                    person_embeddings_cache[person['id']] = {
                        'embedding': np.array(emb, dtype=np.float32).reshape(1, -1),
                        'name': person['name'],
                    }
            print(f"Speaker ID: loaded {len(person_embeddings_cache)} person embeddings", uid, session_id)
        except Exception as e:
            print(f"Speaker ID: failed to load embeddings: {e}", uid, session_id)
            return

        if not person_embeddings_cache:
            print("Speaker ID: no stored embeddings, task disabled", uid, session_id)
            return

        # Consume loop
        while websocket_active:
            try:
                seg = await asyncio.wait_for(speaker_id_segment_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            speaker_id = seg['speaker_id']

            # Skip if already resolved
            if speaker_id in speaker_to_person_map:
                continue

            duration = seg['duration']
            if duration >= SPEAKER_ID_MIN_AUDIO:
                asyncio.create_task(_match_speaker_embedding(speaker_id, seg))

        print("Speaker ID task ended", uid, session_id)

    async def _match_speaker_embedding(speaker_id: int, segment: dict):
        """Extract audio from ring buffer and match against stored embeddings."""
        nonlocal speaker_to_person_map, segment_person_assignment_map, audio_ring_buffer

        try:
            seg_start = segment['abs_start']
            seg_end = segment['abs_end']
            duration = segment['duration']

            if duration < SPEAKER_ID_MIN_AUDIO:
                print(f"Speaker ID: segment too short ({duration:.1f}s)", uid, session_id)
                return

            # Get buffer time range
            time_range = audio_ring_buffer.get_time_range()
            if time_range is None:
                print(f"Speaker ID: buffer empty", uid, session_id)
                return

            buffer_start_ts, buffer_end_ts = time_range

            # Calculate extraction range - stay within segment bounds, max 10 seconds from center
            MAX_EXTRACT_DURATION = 10.0

            if duration <= MAX_EXTRACT_DURATION:
                # Segment fits within max duration, use full segment
                extract_start = seg_start
                extract_end = seg_end
            else:
                # Segment is longer than max, extract 10s from center
                center = (seg_start + seg_end) / 2
                half_duration = MAX_EXTRACT_DURATION / 2
                extract_start = center - half_duration
                extract_end = center + half_duration

            # Clamp to buffer availability
            extract_start = max(buffer_start_ts, extract_start)
            extract_end = min(buffer_end_ts, extract_end)

            if extract_end <= extract_start:
                print(f"Speaker ID: no audio to extract", uid, session_id)
                return

            # Extract only the needed bytes directly from ring buffer
            pcm_data = audio_ring_buffer.extract(extract_start, extract_end)
            if not pcm_data:
                print(f"Speaker ID: failed to extract audio", uid, session_id)
                return

            # Convert PCM to numpy for WAV encoding
            samples = np.frombuffer(pcm_data, dtype=np.int16)

            # Convert PCM to WAV using av
            output_buffer = io.BytesIO()
            output_container = av.open(output_buffer, mode='w', format='wav')
            output_stream = output_container.add_stream('pcm_s16le', rate=sample_rate)
            output_stream.layout = 'mono'

            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format='s16', layout='mono')
            frame.rate = sample_rate

            for packet in output_stream.encode(frame):
                output_container.mux(packet)
            for packet in output_stream.encode():
                output_container.mux(packet)

            output_container.close()
            wav_bytes = output_buffer.getvalue()

            # Extract embedding (API call)
            query_embedding = await asyncio.to_thread(extract_embedding_from_bytes, wav_bytes, "query.wav")

            # Find best match
            best_match = None
            best_distance = float('inf')

            # Print all candidates with scores for tuning
            print(
                f"Speaker ID: comparing speaker {speaker_id} against {len(person_embeddings_cache)} people:",
                uid,
                session_id,
            )
            for person_id, data in person_embeddings_cache.items():
                distance = compare_embeddings(query_embedding, data['embedding'])
                print(f"  - {data['name']}: {distance:.4f}", uid, session_id)
                if distance < best_distance:
                    best_distance = distance
                    best_match = (person_id, data['name'])

            if best_match and best_distance < SPEAKER_MATCH_THRESHOLD:
                person_id, person_name = best_match
                print(
                    f"Speaker ID: speaker {speaker_id} -> {person_name} (distance={best_distance:.3f})", uid, session_id
                )

                # Store for session consistency
                speaker_to_person_map[speaker_id] = (person_id, person_name)

                # Auto-assign processed segment
                segment_person_assignment_map[segment['id']] = person_id

                # Notify client (gated for backward compatibility)
                _send_message_event(
                    SpeakerLabelSuggestionEvent(
                        speaker_id=speaker_id,
                        person_id=_person_id_for_client(person_id),
                        person_name=person_name,
                        segment_id=segment['id'],
                    )
                )
            else:
                print(f"Speaker ID: speaker {speaker_id} no match (best={best_distance:.3f})", uid, session_id)

        except Exception as e:
            print(f"Speaker ID: match error for speaker {speaker_id}: {e}", uid, session_id)

    async def stream_transcript_process():
        nonlocal websocket_active, realtime_segment_buffers, realtime_photo_buffers, websocket
        nonlocal current_conversation_id, translation_enabled, speaker_to_person_map, suggested_segments, words_transcribed_since_last_record, last_transcript_time
        nonlocal first_transcript_buffered_at, first_transcript_dispatched_at

        while True:
            await asyncio.sleep(0.6)

            pending_batch_count = 0
            for recovery_conversation_id in tuple(capture_recovery_conversation_ids):
                try:
                    recovered_batch_count, recovery_owned = poll_capture_persistence_batches(
                        uid,
                        recovery_conversation_id,
                        session_id,
                        generation_id,
                    )
                    pending_batch_count += recovered_batch_count
                    if not recovery_owned:
                        capture_recovery_conversation_ids.discard(recovery_conversation_id)
                        websocket_active = False
                        _latency_log(
                            "capture_persistence_ownership_lost",
                            conversation_id=recovery_conversation_id,
                            phase="recovery",
                        )
                        return
                except Exception:
                    pending_batch_count += 1
                    _latency_log("capture_persistence_recovery_retry")
                    if not websocket_active and not realtime_segment_buffers and not realtime_photo_buffers:
                        continue
                else:
                    if not should_keep_capture_recovery_polling(
                        recovery_conversation_id,
                        current_conversation_id,
                        websocket_active,
                    ):
                        capture_recovery_conversation_ids.discard(recovery_conversation_id)

            if (
                not websocket_active
                and not realtime_segment_buffers
                and not realtime_photo_buffers
                and not image_chunks
                and not photo_processing_tasks
                and not capture_recovery_conversation_ids
                and pending_batch_count == 0
            ):
                break

            if not realtime_segment_buffers and not realtime_photo_buffers:
                continue

            persistence_batch = prepare_conversation_bound_capture_batch(
                realtime_segment_buffers,
                realtime_photo_buffers,
                conversation_key=CAPTURE_CONVERSATION_ID_KEY,
                timestamp_ready=first_audio_byte_timestamp is not None,
            )
            if persistence_batch is None:
                continue
            batch_conversation_id = persistence_batch.conversation_id
            if not batch_conversation_id:
                raise RuntimeError("capture_batch_conversation_missing")
            conversation_data = conversations_db.get_conversation(uid, batch_conversation_id)
            if not conversation_data:
                print(
                    f"Warning: conversation {batch_conversation_id} not found during segment processing",
                    uid,
                    session_id,
                )
                continue
            segments_to_process = []
            for segment in persistence_batch.segments:
                item = dict(segment)
                item.pop(CAPTURE_CONVERSATION_ID_KEY, None)
                segments_to_process.append(item)
            photos_to_process = [photo["photo"] for photo in persistence_batch.photos]

            if segments_to_process:
                if first_transcript_buffered_at is None:
                    first_transcript_buffered_at = time.time()
                    _latency_log(
                        "transcript_buffered",
                        buffered_segment_count=len(segments_to_process),
                        since_first_stt_result_ms=_elapsed_ms(first_stt_result_at, first_transcript_buffered_at),
                    )
                print(
                    f"[TRANSCRIPT-RECV] Processing {len(segments_to_process)} buffered segments for uid={uid} session={session_id} conv={batch_conversation_id}"
                )

            finished_at = datetime.now(timezone.utc)

            transcript_segments = []
            if segments_to_process:
                last_transcript_time = time.time()

                # If conversation has no segments yet, set started_at based on when first speech occurred
                if not conversation_data.get('transcript_segments'):
                    first_speech_timestamp = first_audio_byte_timestamp + segments_to_process[0]["start"]
                    new_started_at = datetime.fromtimestamp(first_speech_timestamp, tz=timezone.utc)
                    conversations_db.update_conversation(uid, batch_conversation_id, {'started_at': new_started_at})
                    conversation_data['started_at'] = new_started_at

                # Calculate unified time offset: audio stream start relative to conversation start
                conversation_started_at = conversation_data['started_at']
                if isinstance(conversation_started_at, str):
                    conversation_started_at = datetime.fromisoformat(conversation_started_at)
                time_offset = first_audio_byte_timestamp - conversation_started_at.timestamp()

                # Apply offset to all segments
                for i, segment in enumerate(segments_to_process):
                    segment["start"] += time_offset
                    segment["end"] += time_offset
                    segments_to_process[i] = segment

                newly_processed_segments = []
                for s in segments_to_process:
                    segment = TranscriptSegment(**s, speech_profile_processed=speech_profile_complete.is_set())
                    # In onboarding mode, force is_user=True for non-Omi segments (user's answers)
                    if onboarding_mode and s.get('speaker_id') != OnboardingHandler.OMI_SPEAKER_ID:
                        segment.is_user = True
                    newly_processed_segments.append(segment)
                words_transcribed = len(" ".join([seg.text for seg in newly_processed_segments]).split())
                if words_transcribed > 0:
                    words_transcribed_since_last_record += words_transcribed

                for seg in newly_processed_segments:
                    current_session_segments[seg.id] = seg.speech_profile_processed
                transcript_segments, _, _ = TranscriptSegment.combine_segments([], newly_processed_segments)
                process_speaker_assigned_segments(
                    transcript_segments,
                    segment_person_assignment_map,
                    speaker_to_person_map,
                )

            # Update transcript segments
            updated_segments = []
            removed_ids = []
            if transcript_segments or photos_to_process:
                try:
                    commit_result = conversations_db.persist_and_commit_capture_persistence_batch(
                        uid,
                        batch_conversation_id,
                        [segment.dict() for segment in transcript_segments],
                        finished_at,
                        session_id,
                        photos=photos_to_process,
                        capture_generation=generation_id,
                    )
                except Exception:
                    capture_recovery_conversation_ids.add(batch_conversation_id)
                    _latency_log(
                        "capture_persistence_retry",
                        segment_count=len(segments_to_process),
                        photo_count=len(photos_to_process),
                    )
                    continue
                if commit_result.get("status") == "ownership_lost":
                    acknowledge_capture_persistence_batch(
                        realtime_segment_buffers,
                        realtime_photo_buffers,
                        persistence_batch,
                        segments=bool(transcript_segments),
                        photos=bool(photos_to_process),
                    )
                    capture_buffers_changed.set()
                    capture_recovery_conversation_ids.discard(batch_conversation_id)
                    websocket_active = False
                    _latency_log(
                        "capture_persistence_ownership_lost",
                        conversation_id=batch_conversation_id,
                        phase="live",
                    )
                    return
                updated_segments = [TranscriptSegment(**segment) for segment in commit_result["updated_segments"]]
                removed_ids = commit_result["removed_ids"]
                acknowledge_capture_persistence_batch(
                    realtime_segment_buffers,
                    realtime_photo_buffers,
                    persistence_batch,
                    segments=bool(transcript_segments),
                    photos=bool(photos_to_process),
                )
                capture_buffers_changed.set()

            if removed_ids:
                _send_message_event(SegmentsDeletedEvent(segment_ids=removed_ids))

            if transcript_segments:
                # ====== ELLA INTEGRATION: Send chunks to scanner ======
                try:
                    from utils.ella import send_to_scanner

                    send_to_scanner(
                        uid=uid,
                        conversation_id=batch_conversation_id,
                        segments=[s.dict() for s in transcript_segments],
                        latency_metadata=_latency_metadata(),
                    )
                    if first_transcript_dispatched_at is None:
                        first_transcript_dispatched_at = time.time()
                        _latency_log(
                            "transcript_dispatched",
                            dispatch_target="scanner",
                            transcript_segment_count=len(transcript_segments),
                            since_first_audio_ms=_elapsed_ms(first_audio_frame_at, first_transcript_dispatched_at),
                            since_first_stt_result_ms=_elapsed_ms(first_stt_result_at, first_transcript_dispatched_at),
                        )
                except ImportError:
                    pass

                try:
                    await websocket.send_json([segment.dict() for segment in updated_segments])
                    _latency_log(
                        "transcript_sent_to_client",
                        transcript_segment_count=len(updated_segments),
                        since_first_audio_ms=_elapsed_ms(first_audio_frame_at),
                    )
                except Exception as e:
                    print(f"Error sending transcript segments to websocket: {e}", uid, session_id)

                if transcript_send is not None and user_has_credits:
                    transcript_send(
                        [segment.dict() for segment in transcript_segments],
                        batch_conversation_id,
                    )
                elif not PUSHER_ENABLED and user_has_credits:
                    # Fallback: trigger realtime integrations directly when pusher is disabled
                    try:
                        await trigger_realtime_integrations(
                            uid, [s.dict() for s in transcript_segments], batch_conversation_id
                        )
                    except Exception as e:
                        print(f"Error triggering realtime integrations: {e}", uid, session_id)

                # Onboarding: pass segments to handler for answer detection
                if onboarding_handler and not onboarding_handler.completed:
                    onboarding_handler.on_segments_received([s.dict() for s in transcript_segments])

                if translation_enabled:
                    await translate(updated_segments, batch_conversation_id)

                # Speaker detection
                for segment in updated_segments:
                    if segment.person_id or segment.is_user or segment.id in suggested_segments:
                        continue

                    # Session consistency speaker identification
                    if speech_profile_complete.is_set():
                        if segment.speaker_id in speaker_to_person_map:
                            person_id, person_name = speaker_to_person_map[segment.speaker_id]
                            _send_message_event(
                                SpeakerLabelSuggestionEvent(
                                    speaker_id=segment.speaker_id,
                                    person_id=_person_id_for_client(person_id),
                                    person_name=person_name,
                                    segment_id=segment.id,
                                )
                            )
                            suggested_segments.add(segment.id)
                            continue

                    # Embeding id speaker indentification
                    if speaker_id_enabled and person_embeddings_cache:
                        started_at_ts = conversation.started_at.timestamp()
                        if (
                            segment.speaker_id is not None
                            and not segment.person_id
                            and not segment.is_user
                            and segment.speaker_id not in speaker_to_person_map
                        ):
                            try:
                                speaker_id_segment_queue.put_nowait(
                                    {
                                        'id': segment.id,
                                        'speaker_id': segment.speaker_id,
                                        'abs_start': first_audio_byte_timestamp
                                        + segment.start
                                        - time_offset,  # raw start/end
                                        'abs_end': first_audio_byte_timestamp + segment.end - time_offset,
                                        'duration': segment.end - segment.start,
                                        'text': segment.text,  # TODO: remove
                                    }
                                )
                            except asyncio.QueueFull:
                                pass  # Drop if queue is full

                    # Text-based detection
                    detected_name = detect_speaker_from_text(segment.text)
                    if detected_name:
                        person = user_db.get_person_by_name(uid, detected_name)
                        if person:
                            person_id = person['id']
                        else:
                            # Backend creates person if missing
                            person_id = str(uuid.uuid4())
                            user_db.create_person(
                                uid,
                                {
                                    'id': person_id,
                                    'name': detected_name,
                                    'created_at': datetime.now(timezone.utc),
                                    'updated_at': datetime.now(timezone.utc),
                                },
                            )
                        _send_message_event(
                            SpeakerLabelSuggestionEvent(
                                speaker_id=segment.speaker_id,
                                person_id=_person_id_for_client(person_id),
                                person_name=detected_name,
                                segment_id=segment.id,
                            )
                        )
                        # Set maps for future segments, but only if diarization is active
                        # (speaker_id > 0 means diarization assigned a real speaker)
                        # Set maps for future segments using helper function
                        if should_update_speaker_to_person_map(segment.speaker_id):
                            speaker_to_person_map[segment.speaker_id] = (person_id, detected_name)
                        segment_person_assignment_map[segment.id] = person_id
                        suggested_segments.add(segment.id)

    async def process_photo(
        uid: str,
        image_b64: str,
        temp_id: str,
        send_event_func,
        photo_buffer: list[dict],
        conversation_id: str,
    ):
        from utils.llm.openglass import describe_image

        photo_id = str(uuid.uuid4())
        await send_event_func(PhotoProcessingEvent(temp_id=temp_id, photo_id=photo_id))

        try:
            description = await describe_image(image_b64)
            discarded = not description or not description.strip()
        except Exception as e:
            print(f"Error describing image: {e}", uid, session_id)
            description = "Could not generate description."
            discarded = True

        final_photo = ConversationPhoto(id=photo_id, base64=image_b64, description=description, discarded=discarded)
        photo_buffer.append(
            {
                CAPTURE_CONVERSATION_ID_KEY: conversation_id,
                "photo": final_photo,
            }
        )
        await send_event_func(PhotoDescribedEvent(photo_id=photo_id, description=description, discarded=discarded))

    async def handle_image_chunk(
        uid: str,
        chunk_data: dict,
        image_chunks_cache: dict,
        send_event_func,
        photo_buffer: list[dict],
        conversation_id: str,
    ):
        temp_id = chunk_data.get('id')
        index = chunk_data.get('index')
        total = chunk_data.get('total')
        data = chunk_data.get('data')

        if not temp_id or not isinstance(index, int) or not isinstance(total, int) or not data:
            print(f"Invalid image chunk received: {chunk_data}", uid, session_id)
            return

        if temp_id not in image_chunks_cache:
            if total <= 0:
                return
            image_chunks_cache[temp_id] = {
                "conversation_id": conversation_id,
                "chunks": [None] * total,
            }

        image_upload = image_chunks_cache[temp_id]
        chunks = image_upload["chunks"]

        if index < total and chunks[index] is None:
            chunks[index] = data

        if all(chunk is not None for chunk in chunks):
            b64_image_data = "".join(chunks)
            bound_conversation_id = str(image_upload["conversation_id"])
            del image_chunks_cache[temp_id]
            task = safe_create_task(
                process_photo(
                    uid,
                    b64_image_data,
                    temp_id,
                    send_event_func,
                    photo_buffer,
                    bound_conversation_id,
                )
            )
            photo_processing_tasks[temp_id] = (bound_conversation_id, task)

            def photo_processing_done(_task: asyncio.Task) -> None:
                photo_processing_tasks.pop(temp_id, None)
                capture_buffers_changed.set()

            task.add_done_callback(photo_processing_done)

    # Initialize decoders based on codec
    opus_decoder = None
    aac_decoder = None
    lc3_decoder = None

    if codec == 'opus':
        opus_decoder = opuslib.Decoder(sample_rate, 1)
    elif codec == 'aac':
        aac_decoder = AACDecoder(uid=uid, session_id=session_id, sample_rate=sample_rate, channels=channels)
    elif codec == 'lc3':
        lc3_decoder = lc3.Decoder(lc3_frame_duration_us, sample_rate)

    async def receive_data(
        dg_socket, dg_profile_socket, soniox_sock, soniox_profile_sock, speechmatics_sock, grok_sock
    ):
        nonlocal websocket_active, websocket_close_code, last_audio_received_time, last_activity_time, current_conversation_id
        nonlocal realtime_photo_buffers, speaker_to_person_map, first_audio_byte_timestamp, last_usage_record_timestamp
        nonlocal soniox_profile_socket, deepgram_profile_socket, audio_ring_buffer
        nonlocal first_audio_frame_at
        nonlocal accepting_capture, capture_drained

        timer_start = time.time()
        last_audio_received_time = timer_start
        last_activity_time = timer_start

        # STT audio buffer - accumulate 30ms before sending for better transcription quality
        stt_audio_buffer = bytearray()
        stt_buffer_flush_size = int(sample_rate * 2 * 0.03)  # 30ms at 16-bit mono (e.g., 6400 bytes at 16kHz)

        async def flush_stt_buffer(force: bool = False):
            nonlocal stt_audio_buffer, soniox_profile_socket, deepgram_profile_socket, grok_sock

            if not stt_audio_buffer:
                return
            if not force and len(stt_audio_buffer) < stt_buffer_flush_size:
                return

            chunk = bytes(stt_audio_buffer)
            stt_audio_buffer.clear()

            # Use event-based routing instead of time-based
            profile_complete = speech_profile_complete.is_set()

            if dg_socket is not None:
                if profile_complete or not deepgram_profile_socket:
                    dg_socket.send(chunk)
                    if deepgram_profile_socket:
                        print('Scheduling delayed close of deepgram_profile_socket', uid, session_id)
                        socket_to_close = deepgram_profile_socket
                        deepgram_profile_socket = None  # Stop sending immediately

                        async def close_dg_profile():
                            await asyncio.sleep(5)
                            socket_to_close.finish()
                            print('Closed deepgram_profile_socket after 5s delay', uid, session_id)

                        asyncio.create_task(close_dg_profile())
                else:
                    deepgram_profile_socket.send(chunk)

            if soniox_sock is not None:
                if profile_complete or not soniox_profile_socket:
                    await soniox_sock.send(chunk)
                    if soniox_profile_socket:
                        print('Scheduling delayed close of soniox_profile_socket', uid, session_id)
                        socket_to_close = soniox_profile_socket
                        soniox_profile_socket = None  # Stop sending immediately

                        async def close_soniox_profile():
                            await asyncio.sleep(5)
                            await socket_to_close.close()
                            print('Closed soniox_profile_socket after 5s delay', uid, session_id)

                        asyncio.create_task(close_soniox_profile())
                else:
                    await soniox_profile_socket.send(chunk)

            if grok_sock is not None:
                # Proactively reconnect if Grok closed the connection (internal error, timeout, etc.)
                try:
                    from websockets.connection import State as _WsState

                    _grok_dead = grok_sock.state != _WsState.OPEN
                except Exception:
                    _grok_dead = False
                if _grok_dead:
                    print("[GROK] connection lost (state not OPEN), reconnecting...")
                    try:
                        try:
                            await grok_sock.close()
                        except Exception:
                            pass
                        grok_sock = await process_audio_grok(
                            stream_transcript,
                            sample_rate,
                            stt_language or 'en',
                            preseconds=speech_profile_preseconds,
                            stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                        )
                        print("[GROK] reconnected successfully")
                    except Exception as _grok_reconnect_err:
                        print(f"[GROK] reconnect failed: {_grok_reconnect_err}, dropping Grok")
                        grok_sock = None
                if grok_sock is not None:
                    try:
                        await grok_sock.send(bytes(chunk))
                    except Exception as _grok_send_err:
                        print(f"[GROK] send error ({_grok_send_err}), reconnecting...")
                        try:
                            try:
                                await grok_sock.close()
                            except Exception:
                                pass
                            grok_sock = await process_audio_grok(
                                stream_transcript,
                                sample_rate,
                                stt_language or 'en',
                                preseconds=speech_profile_preseconds,
                                stt_event_callback=_stt_event_callback if STT_LATENCY_LOGS_ENABLED else None,
                            )
                            await grok_sock.send(bytes(chunk))
                            print("[GROK] reconnected and sent chunk")
                        except Exception as _grok_reconnect_err2:
                            print(f"[GROK] reconnect failed: {_grok_reconnect_err2}, dropping Grok")
                            grok_sock = None

            if speechmatics_sock is not None:
                await speechmatics_sock.send(chunk)

        async def finish_stt_inputs_for_drain() -> None:
            await flush_stt_buffer(force=True)
            if dg_socket is not None:
                dg_socket.finish()
            if dg_profile_socket is not None:
                dg_profile_socket.finish()
            if soniox_sock is not None:
                await soniox_sock.close()
            if soniox_profile_sock is not None:
                await soniox_profile_sock.close()
            if grok_sock is not None:
                await grok_sock.close()
            if speechmatics_sock is not None:
                await speechmatics_sock.close()
            await asyncio.sleep(0)

        async def wait_for_capture_drain_quiescence(conversation_id: str, timeout_seconds: float = 10.0) -> bool:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            stable_since = None
            while True:
                drain_capture_persistence_batches(
                    uid,
                    conversation_id,
                    session_id,
                    generation_id,
                )
                buffers_pending = _capture_buffers_contain_conversation(conversation_id)
                batches_pending = bool(conversations_db.list_capture_persistence_batches(uid, conversation_id))
                now = asyncio.get_running_loop().time()
                if not buffers_pending and not batches_pending:
                    stable_since = stable_since or now
                    if now - stable_since >= 1.0:
                        return True
                else:
                    stable_since = None
                if now >= deadline:
                    return False
                await asyncio.sleep(0.05)

        try:
            while websocket_active:
                message = await websocket.receive()
                last_activity_time = time.time()

                # Handle client disconnect
                if message.get("type") == "websocket.disconnect":
                    close_code = message.get("code", 1000)
                    close_reason = {
                        1000: "normal_closure",
                        1001: "going_away_os_or_background",
                        1006: "abnormal_closure",
                        1011: "server_error",
                    }.get(close_code, "unknown")
                    print(f"Client disconnected: code={close_code} reason={close_reason}", uid, session_id)
                    break

                if message.get("bytes") is not None:

                    if not accepting_capture:
                        continue

                    data = message.get("bytes")
                    if len(data) <= 2:  # Ping/keepalive, 0x8a 0x00
                        continue

                    last_audio_received_time = time.time()

                    if first_audio_byte_timestamp is None:
                        first_audio_byte_timestamp = last_audio_received_time
                        last_usage_record_timestamp = first_audio_byte_timestamp
                    if first_audio_frame_at is None:
                        first_audio_frame_at = last_audio_received_time
                        _latency_log(
                            "first_audio_frame_received",
                            encoded_bytes=len(data),
                            since_stt_ready_ms=_elapsed_ms(stt_connect_ready_at, first_audio_frame_at),
                        )

                    # Decode based on codec
                    if codec == 'opus' and sample_rate == 16000:
                        try:
                            data = opus_decoder.decode(bytes(data), frame_size=frame_size)
                            if not data:
                                continue
                        except Exception as e:
                            print(f"[OPUS] Decoding error: {e}", uid, session_id)
                            continue
                    elif codec == 'aac':
                        try:
                            data = aac_decoder.decode(bytes(data))
                            if not data:
                                continue
                        except Exception as e:
                            print(f"[AAC] Decoding error: {e}", uid, session_id)
                            continue
                    elif codec == 'lc3':
                        try:
                            # Decode LC3 frame to PCM
                            # lc3.decode returns PCM bytes directly with bit_depth=16
                            pcm_bytes = lc3_decoder.decode(bytes(data), bit_depth=16)
                            if not pcm_bytes:
                                continue
                            data = pcm_bytes
                        except Exception as e:
                            print(
                                f"[LC3] Decoding error: {e} | "
                                f"Data size: {len(data)} bytes (expected: {lc3_chunk_size}) | "
                                f"Frame duration: {lc3_frame_duration_us}μs | "
                                f"Sample rate: {sample_rate}Hz",
                                uid,
                                session_id,
                            )
                            continue

                    # Feed ring buffer for speaker identification
                    if audio_ring_buffer is not None:
                        audio_ring_buffer.write(data, last_audio_received_time)

                    if not use_custom_stt:
                        stt_audio_buffer.extend(data)
                        await flush_stt_buffer()

                    if audio_bytes_send is not None:
                        audio_bytes_send(data, last_audio_received_time)

                elif message.get("text") is not None:
                    try:
                        json_data = json.loads(message.get("text"))
                        if json_data.get('type') in {'client_latency_event', 'latency_event', 'timing_event'}:
                            _remember_client_latency_event(json_data)
                        elif json_data.get('type') == 'capture_drain':
                            exact_conversation_id = str(current_conversation_id or '').strip()
                            if not valid_capture_drain_body(
                                json_data,
                                exact_conversation_id,
                                generation_id,
                                owner_token,
                            ):
                                websocket_close_code = 1008
                                break
                            if not capture_drain_diagnostic_correlation_matches(
                                json_data,
                                diagnostic_correlation,
                            ):
                                try:
                                    await websocket.send_json(
                                        {
                                            "type": "service_status",
                                            "status": "diagnostic_correlation_rejected",
                                            "status_text": "capture_diagnostic_correlation_mismatch",
                                            "evidence_only": True,
                                        }
                                    )
                                except Exception:
                                    pass
                            accepting_capture = False
                            capture_drain_tasks: set[asyncio.Task] = set()
                            capture_drain_complete = asyncio.Event()

                            async def finish_and_schedule_durable_drain() -> None:
                                await finish_stt_inputs_for_drain()

                                async def await_durable_drain() -> None:
                                    if await wait_for_capture_drain_quiescence(exact_conversation_id):
                                        capture_drain_complete.set()

                                capture_drain_tasks.add(asyncio.create_task(await_durable_drain()))

                            if not await flush_capture_before_drained(
                                finish_and_schedule_durable_drain,
                                capture_drain_tasks,
                                capture_drain_complete,
                                timeout=10.0,
                            ):
                                websocket_close_code = 1011
                                break
                            if not mark_capture_drained(
                                uid,
                                exact_conversation_id,
                                generation_id,
                                owner_token,
                            ):
                                websocket_close_code = 1008
                                break
                            if not redis_db.release_owned_in_progress_conversation_id(
                                uid,
                                exact_conversation_id,
                                session_id,
                            ):
                                websocket_close_code = 1008
                                break
                            capture_drained = True
                            await _asend_message_event(
                                MessageServiceStatusEvent(
                                    status="capture_protocol_drained",
                                    protocol_version=CAPTURE_PROTOCOL_VERSION,
                                    conversation_id=exact_conversation_id,
                                    generation=generation_id,
                                    owner_token=owner_token,
                                    **(diagnostic_correlation.receipt_fields() if diagnostic_correlation else {}),
                                    evidence_only=True if diagnostic_correlation else None,
                                )
                            )
                            websocket_active = False
                            break
                        elif json_data.get('type') == 'image_chunk' and accepting_capture:
                            capture_conversation_id = str(current_conversation_id or "").strip()
                            if not capture_conversation_id:
                                raise RuntimeError("capture_conversation_not_ready")
                            await handle_image_chunk(
                                uid,
                                json_data,
                                image_chunks,
                                _asend_message_event,
                                realtime_photo_buffers,
                                capture_conversation_id,
                            )
                        elif json_data.get('type') == 'skip_question':
                            if onboarding_handler and not onboarding_handler.completed:
                                await onboarding_handler.skip_current_question()
                        elif json_data.get('type') == 'suggested_transcript' and accepting_capture:
                            if use_custom_stt:
                                suggested_segments = json_data.get('segments', [])
                                stt_provider = json_data.get('stt_provider')
                                if suggested_segments:
                                    if first_audio_byte_timestamp is None:
                                        first_audio_byte_timestamp = last_activity_time
                                        last_usage_record_timestamp = first_audio_byte_timestamp
                                    # Attach stt_provider to each segment
                                    if stt_provider:
                                        for seg in suggested_segments:
                                            seg['stt_provider'] = stt_provider
                                    _latency_log(
                                        "suggested_transcript_received",
                                        stt_provider=stt_provider,
                                        segment_count=len(suggested_segments),
                                    )
                                    stream_transcript(suggested_segments)
                        elif json_data.get('type') == 'speaker_assigned':
                            segment_ids = json_data.get('segment_ids', [])
                            can_assign = False
                            if segment_ids:
                                for sid in segment_ids:
                                    if sid in current_session_segments and current_session_segments[sid]:
                                        can_assign = True
                                        break

                            # Always set maps regardless of can_assign (fixes latest segments missed)
                            speaker_id = json_data.get('speaker_id')
                            person_id = json_data.get('person_id')
                            person_name = json_data.get('person_name')
                            maps_updated = update_speaker_assignment_maps(
                                speaker_id,
                                person_id,
                                person_name,
                                segment_ids,
                                speaker_to_person_map,
                                segment_person_assignment_map,
                            )
                            if maps_updated:
                                print(f"Speaker {speaker_id} assigned to {person_name} ({person_id})", uid, session_id)

                                # Forward to pusher for speech sample extraction (non-blocking)
                                # Only for real people (not 'user') and when private cloud sync is enabled
                                # Only when can_assign is true (has speech_profile_processed segment)
                                if (
                                    can_assign
                                    and person_id
                                    and person_id != 'user'
                                    and private_cloud_sync_enabled
                                    and send_speaker_sample_request is not None
                                    and current_conversation_id
                                ):
                                    asyncio.create_task(
                                        send_speaker_sample_request(
                                            person_id=person_id,
                                            conv_id=current_conversation_id,
                                            segment_ids=segment_ids,
                                        )
                                    )
                            else:
                                print(
                                    "Speaker assignment ignored: missing speaker_id/person_id/person_name.",
                                    uid,
                                    session_id,
                                )
                    except json.JSONDecodeError:
                        print(f"Received non-json text message: {message.get('text')}", uid, session_id)

        except WebSocketDisconnect:
            print("WebSocket disconnected (exception)", uid, session_id)
        except Exception as e:
            print(f'Could not process data: error {e}', uid, session_id)
            websocket_close_code = 1011
        finally:
            # Flush any remaining audio in buffer to STT
            if not use_custom_stt:
                await flush_stt_buffer(force=True)
            websocket_active = False
            image_chunks.clear()
            capture_buffers_changed.set()

    # Start
    #
    try:
        # Init STT (fast - profile file loads and sends in background)
        _send_message_event(MessageServiceStatusEvent(status="stt_initiating", status_text="STT Service Starting"))
        speech_profile_task = await _process_stt()

        # Init pusher
        pusher_tasks = []
        if PUSHER_ENABLED:
            (
                pusher_connect,
                pusher_close,
                transcript_send,
                transcript_consume,
                audio_bytes_send,
                audio_bytes_consume,
                request_conversation_processing,
                pusher_receive,
                pusher_is_connected,
                send_speaker_sample_request,
            ) = create_pusher_task_handler()

            # Pusher connection
            await pusher_connect()
            if not pusher_is_connected():
                print("Pusher connection failed after retries", uid, session_id)
                await websocket.close(code=1011, reason="Pusher connection failed")
                return

            # Pusher tasks
            if transcript_consume is not None:
                pusher_tasks.append(asyncio.create_task(transcript_consume()))
            if audio_bytes_consume is not None:
                pusher_tasks.append(asyncio.create_task(audio_bytes_consume()))
            if pusher_receive is not None:
                pusher_tasks.append(asyncio.create_task(pusher_receive()))

        # Tasks
        data_process_task = asyncio.create_task(
            receive_data(
                deepgram_socket,
                deepgram_profile_socket,
                soniox_socket,
                soniox_profile_socket,
                speechmatics_socket,
                grok_socket,
            )
        )
        stream_transcript_task = asyncio.create_task(stream_transcript_process())
        record_usage_task = asyncio.create_task(_record_usage_periodically())
        lifecycle_manager_task = asyncio.create_task(conversation_lifecycle_manager())
        pending_conversations_task = asyncio.create_task(process_pending_conversations(timed_out_conversation_id))
        speaker_id_task = asyncio.create_task(speaker_identification_task())

        _send_message_event(MessageServiceStatusEvent(status="ready"))

        tasks = [
            data_process_task,
            stream_transcript_task,
            heartbeat_task,
            record_usage_task,
            lifecycle_manager_task,
            pending_conversations_task,
            speaker_id_task,
        ] + pusher_tasks

        # Add speech profile task to run concurrently (sends profile audio in background)
        if speech_profile_task:
            tasks.append(speech_profile_task)

        await asyncio.gather(*tasks)

    except Exception as e:
        print(f"Error during WebSocket operation: {e}", uid, session_id)
    finally:
        if not use_custom_stt and last_usage_record_timestamp:
            transcription_seconds = int(time.time() - last_usage_record_timestamp)
            words_to_record = words_transcribed_since_last_record
            if transcription_seconds > 0 or words_to_record > 0:
                record_usage(uid, transcription_seconds=transcription_seconds, words_transcribed=words_to_record)
        websocket_active = False

        if capture_protocol != CAPTURE_PROTOCOL_VERSION:
            try:
                await _finalize_current_conversation_on_disconnect()
            except Exception as e:
                _latency_log(
                    "capture_disconnect_finalize_error",
                    conversation_id=str(current_conversation_id or "").strip() or None,
                    error=str(e)[:300],
                )
                print(f"Error finalizing conversation after disconnect: {e}", uid, session_id)
        elif not capture_drained:
            _latency_log(
                "capture_protocol_disconnect_before_drain",
                conversation_id=str(current_conversation_id or "").strip() or None,
            )

        await _await_conversation_finalize_tasks()

        # STT sockets
        try:
            if deepgram_socket:
                deepgram_socket.finish()
            if deepgram_profile_socket:
                deepgram_profile_socket.finish()
            if soniox_socket:
                await soniox_socket.close()
            if grok_socket:
                await grok_socket.close()
            if soniox_profile_socket:
                await soniox_profile_socket.close()
            if speechmatics_socket:
                await speechmatics_socket.close()
        except Exception as e:
            print(f"Error closing STT sockets: {e}", uid, session_id)

        # Client sockets
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=websocket_close_code)
            except Exception as e:
                print(f"Error closing Client WebSocket: {e}", uid, session_id)

        # Pusher sockets
        if pusher_close is not None:
            try:
                await pusher_close()
            except Exception as e:
                print(f"Error closing Pusher: {e}", uid, session_id)

        # Clean up onboarding handler
        if onboarding_handler:
            onboarding_handler.cleanup()

        # Clean up collections to aid garbage collection
        try:
            locked_conversation_ids.clear()
            speaker_to_person_map.clear()
            segment_person_assignment_map.clear()
            current_session_segments.clear()
            suggested_segments.clear()
            realtime_segment_buffers.clear()
            realtime_photo_buffers.clear()
            image_chunks.clear()
            person_embeddings_cache.clear()
        except NameError as e:
            # Variables might not be defined if an error occurred early
            print(f"Cleanup error (safe to ignore): {e}", uid, session_id)

    print("_stream_handler ended", uid, session_id)


async def _listen(
    websocket: WebSocket,
    uid: str,
    language: str = 'en',
    sample_rate: int = 8000,
    codec: str = 'pcm8',
    channels: int = 1,
    include_speech_profile: bool = True,
    stt_service: Optional[STTService] = None,
    conversation_timeout: int = 120,
    source: Optional[str] = None,
    custom_stt_mode: CustomSttMode = CustomSttMode.disabled,
    onboarding_mode: bool = False,
    speaker_auto_assign_enabled: bool = False,
    capture_protocol: int = 0,
):
    """
    WebSocket handler for app clients. Accepts the websocket connection and delegates to _stream_handler.
    """
    print("_listen", uid)
    try:
        await websocket.accept()
        socket_accepted_at = time.time()
        if STT_LATENCY_LOGS_ENABLED:
            print(
                f"[STT-LATENCY] {json.dumps({'event': 'listen_socket_accepted', 'uid': uid, 'at': _utc_iso_from_ts(socket_accepted_at), 'requested_stt_provider': _stt_service_value(stt_service), 'codec': codec, 'sample_rate': sample_rate, 'include_speech_profile': include_speech_profile}, default=str)}",
                flush=True,
            )
    except RuntimeError as e:
        print(f"_listen: accept error {e}", uid)
        return

    diagnostic_correlation = await _validate_socket_diagnostic_correlation(websocket, uid)

    await _stream_handler(
        websocket,
        uid,
        language,
        sample_rate,
        codec,
        channels,
        include_speech_profile,
        stt_service,
        conversation_timeout=conversation_timeout,
        source=source,
        custom_stt_mode=custom_stt_mode,
        onboarding_mode=onboarding_mode,
        speaker_auto_assign_enabled=speaker_auto_assign_enabled,
        capture_protocol=capture_protocol,
        socket_accepted_at=socket_accepted_at,
        diagnostic_correlation=diagnostic_correlation,
    )
    print("_listen ended", uid)


@router.websocket("/v4/listen")
async def listen_handler(
    websocket: WebSocket,
    uid: str = Depends(require_current_ai_consent),
    language: str = 'en',
    sample_rate: int = 8000,
    codec: str = 'pcm8',
    channels: int = 1,
    include_speech_profile: bool = True,
    stt_service: Optional[STTService] = None,
    conversation_timeout: int = 120,
    source: Optional[str] = None,
    custom_stt: str = 'disabled',
    onboarding: str = 'disabled',
    speaker_auto_assign: str = 'disabled',
    capture_protocol: int = 0,
):
    custom_stt_mode = CustomSttMode.enabled if custom_stt == 'enabled' else CustomSttMode.disabled
    onboarding_mode = onboarding == 'enabled'
    speaker_auto_assign_enabled = speaker_auto_assign == 'enabled'

    # Ella sidecar: require isolated Hermes when the runtime cutover flag is on.
    isolated_runtime_cutover = False
    try:
        runtime_gate = await listen_runtime_gate(uid, user_db.is_exists_user)
        isolated_runtime_cutover = runtime_gate.get("required", False)
        if isolated_runtime_cutover:
            if not runtime_gate.get("success"):
                logging.getLogger(__name__).warning(
                    "Isolated Ella listen setup incomplete for uid=%s code=%s",
                    uid,
                    runtime_gate.get("error"),
                )
                await websocket.close(code=1013, reason="Ella setup incomplete")
                return
        else:
            await ensure_firestore_user_document(uid, user_db.db)
            cluster = await get_agent_cluster(uid)
            if not cluster:
                logger = logging.getLogger(__name__)
                logger.info(f"No agent cluster for uid={uid}, auto-provisioning...")
                result = await auto_provision_user(uid)
                if not result.get("success"):
                    logger.warning(f"Auto-provision failed: {result.get('error')}")
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Auto-provision check error: {e}")
        if isolated_runtime_cutover:
            await websocket.close(code=1013, reason="Ella setup incomplete")
            return

    await _listen(
        websocket,
        uid,
        language,
        sample_rate,
        codec,
        channels,
        include_speech_profile,
        stt_service,
        conversation_timeout=conversation_timeout,
        source=source,
        custom_stt_mode=custom_stt_mode,
        onboarding_mode=onboarding_mode,
        speaker_auto_assign_enabled=speaker_auto_assign_enabled,
        capture_protocol=capture_protocol,
    )


@router.websocket("/v4/web/listen")
async def web_listen_handler(
    websocket: WebSocket,
    language: str = 'en',
    sample_rate: int = 8000,
    codec: str = 'pcm8',
    channels: int = 1,
    include_speech_profile: bool = True,
    conversation_timeout: int = 120,
    source: Optional[str] = None,
    custom_stt: str = 'disabled',
    onboarding: str = 'disabled',
    capture_protocol: int = 0,
):
    """
    WebSocket endpoint for web browser clients using first-message authentication.

    First message must be: {"type": "auth", "token": "<firebase_token>"}
    Response: {"type": "auth_response", "success": true/false}
    """
    print("web_listen_handler")
    try:
        await websocket.accept()
        socket_accepted_at = time.time()
    except RuntimeError as e:
        print(f"web_listen_handler: accept error {e}")
        return

    # Wait for auth message with timeout
    try:
        first_message = await asyncio.wait_for(websocket.receive(), timeout=5.0)
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Auth timeout")
        return
    except WebSocketDisconnect:
        return

    # Authenticate via first message
    try:
        uid = auth.get_current_user_uid_from_ws_message(first_message)
    except ValueError as e:
        await websocket.close(code=1008, reason=str(e))
        return
    except InvalidIdTokenError:
        await websocket.send_json({"type": "auth_response", "success": False})
        await websocket.close(code=1008, reason="Invalid token")
        return
    except Exception as e:
        print(f"web_listen_handler: auth error {e}")
        await websocket.send_json({"type": "auth_response", "success": False})
        await websocket.close(code=1008, reason="Auth error")
        return

    try:
        assert_current_ai_consent(uid)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        await websocket.send_json(
            {
                "type": "auth_response",
                "success": False,
                "error": detail.get("code", "ai_consent_required"),
            }
        )
        await websocket.close(code=1008, reason="AI consent required")
        return

    runtime_gate = await listen_runtime_gate(uid, user_db.is_exists_user)
    if runtime_gate.get("required") and not runtime_gate.get("success"):
        logging.getLogger(__name__).warning(
            "Isolated Ella web listen setup incomplete for uid=%s code=%s",
            uid,
            runtime_gate.get("error"),
        )
        await websocket.send_json(
            {
                "type": "auth_response",
                "success": False,
                "error": "ella_setup_incomplete",
            }
        )
        await websocket.close(code=1013, reason="Ella setup incomplete")
        return

    # Send success response
    await websocket.send_json({"type": "auth_response", "success": True})
    print("web_listen_handler authenticated", uid)

    diagnostic_correlation = await _validate_socket_diagnostic_correlation(websocket, uid)

    # Proceed with streaming (websocket already accepted, uid already validated)
    custom_stt_mode = CustomSttMode.enabled if custom_stt == 'enabled' else CustomSttMode.disabled
    onboarding_mode = onboarding == 'enabled'

    await _stream_handler(
        websocket,
        uid,
        language,
        sample_rate,
        codec,
        channels,
        include_speech_profile,
        None,
        conversation_timeout=conversation_timeout,
        source=source,
        custom_stt_mode=custom_stt_mode,
        onboarding_mode=onboarding_mode,
        capture_protocol=capture_protocol,
        socket_accepted_at=socket_accepted_at,
        diagnostic_correlation=diagnostic_correlation,
    )
    print("web_listen_handler ended", uid)
