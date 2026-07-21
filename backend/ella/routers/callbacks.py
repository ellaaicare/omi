"""
Ella Callback Router

Provides Ella-specific callback endpoints that n8n workflows use to trigger
backend actions (push notifications with TTS audio, etc).

Endpoints:
- POST   /v1/ella/notification                         - Push notification with optional TTS audio
- POST   /v1/ella/emergency                            - Emergency alert to all configured contacts
- POST   /v1/ella/daily-summary                        - Trigger daily summary for a user's caregivers
- GET    /v1/ella/caregiver-dashboard-data             - Dashboard data for caregivers (token auth)
- POST   /v1/ella/generate-dashboard-token             - Generate 24h dashboard token (internal)
- POST   /v1/ella/emergency-contact                    - Add an emergency contact
- GET    /v1/ella/emergency-contacts/{uid}             - List all contacts for a user
- PUT    /v1/ella/emergency-contact/{contact_id}       - Update a contact
- DELETE /v1/ella/emergency-contact/{contact_id}       - Remove a contact
- POST   /v1/ella/chat/stream                           - Stream chat response from Grok (xAI)
NOTE: Caregiver CRUD endpoints moved to n8n (ella-ai-care repo). iOS calls n8n webhooks directly.
- GET    /v1/ella/health                               - Health check
- GET    /v1/ella/conversation/{id}/data               - Fetch conversation data with transcript (internal)
"""

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import string
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database.conversations as conversations_db
import database.memories as memories_db
import database.users as users_db
from database._client import db
from models.conversation import CategoryEnum
from database.ella_contacts import create_contact, delete_contact, get_contact, get_contacts, update_contact
from ella.config import ELLA_CONFIG
from ella.services.runtime_resolver import resolve_isolated_runtime, runtime_bindings_enabled
from ella.services.summary_sanitizer import SummarySanitizationError
from ella.services.summary_writeback import (
    ConversationSummaryNotFoundError,
    InvalidConversationSummaryCategoryError,
    write_conversation_summary,
)
from utils.notifications import send_notification
from utils.ella.canonical_omi import write_omi_canonical_event
from utils.other.storage import storage_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella"])
_resolve_pool: Optional[asyncpg.Pool] = None

# GCS bucket for Ella TTS audio files (defaults to project's Firebase Storage bucket)
ELLA_AUDIO_BUCKET = os.getenv("ELLA_AUDIO_BUCKET", "omi-dev-ca005.firebasestorage.app")

# OpenAI TTS configuration
OPENAI_TTS_MODEL = os.getenv("ELLA_TTS_MODEL", "tts-1")
OPENAI_TTS_VOICE = os.getenv("ELLA_TTS_VOICE", "nova")
PROVISION_API_KEY = os.getenv("ELLA_PROVISION_API_KEY", os.getenv("ELLA_PROVISION_API_TOKEN", ""))
PROVISION_API_URL = os.getenv("ELLA_PROVISION_URL", "http://100.76.138.56:8200")


# ============================================================================
# Request/Response Models
# ============================================================================


class NotificationMetadata(BaseModel):
    source: str = "omi"
    letta_agent: str = "main-agent"
    confidence: Optional[float] = None
    trigger: Optional[str] = None
    conversation_id: Optional[str] = None


class NotificationRequest(BaseModel):
    uid: str
    message: str = Field(..., min_length=1, max_length=500)
    urgency: str = "NORMAL"
    generate_audio: bool = True
    audio_url: Optional[str] = None
    metadata: Optional[NotificationMetadata] = None


class NotificationResponse(BaseModel):
    status: str
    message_id: Optional[str] = None
    audio_url: Optional[str] = None
    urgency: str = "NORMAL"
    error: Optional[str] = None


# ============================================================================
# TTS + Upload helpers
# ============================================================================


def _generate_tts_audio(text: str) -> Optional[bytes]:
    """Generate TTS audio via OpenAI API. Returns MP3 bytes or None."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("[Ella] OPENAI_API_KEY not set, skipping TTS")
        return None

    try:
        response = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_TTS_MODEL,
                "input": text,
                "voice": OPENAI_TTS_VOICE,
                "response_format": "mp3",
            },
            timeout=15.0,
        )
        response.raise_for_status()
        audio_bytes = response.content
        logger.info(f"[Ella] TTS generated: {len(audio_bytes)} bytes")
        return audio_bytes

    except Exception as e:
        logger.error(f"[Ella] TTS generation failed: {e}")
        return None


def _upload_audio_to_gcs(audio_bytes: bytes, uid: str) -> Optional[str]:
    """Upload MP3 audio to GCS and return a signed URL (1 hour expiry)."""
    try:
        bucket = storage_client.bucket(ELLA_AUDIO_BUCKET)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"ella-tts/{uid}/{timestamp}-{uuid.uuid4().hex[:8]}.mp3"
        blob = bucket.blob(filename)
        blob.upload_from_file(io.BytesIO(audio_bytes), content_type="audio/mpeg")
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=1), method="GET")
        logger.info(f"[Ella] Audio uploaded: {filename}")
        return url

    except Exception as e:
        logger.error(f"[Ella] GCS upload failed: {e}")
        return None


# ============================================================================
# Conversation Summary Update (for n8n pipeline write-back)
# ============================================================================


class ConversationSummaryUpdate(BaseModel):
    """Schema for updating conversation structured summary fields."""

    title: Optional[str] = None
    overview: Optional[str] = None
    emoji: Optional[str] = None
    category: Optional[str] = None
    summary_source: str = "observer"
    summary_kind: str = "observer_enriched"
    correction_id: Optional[str] = None
    based_on_version_id: Optional[str] = None
    set_active: bool = True
    trace_id: Optional[str] = None
    require_canonical: bool = False
    ella_tags: List[str] = Field(default_factory=list)
    ella_signal: Optional[Dict[str, Any]] = None


def _active_summary_version(conversation: dict) -> Optional[dict]:
    active_id = conversation.get("active_summary_version_id")
    versions = conversation.get("summary_versions") or []
    if active_id:
        for version in versions:
            if version.get("id") == active_id:
                return version
    for version in versions:
        if version.get("is_active"):
            return version
    return None


def _enrichment_candidate_reason(conversation: dict) -> Optional[str]:
    enrichment_state = conversation.get("enrichment_state") or {}
    status = enrichment_state.get("status")
    if status == "writeback_applied":
        return None
    if status == "failed":
        return "enrichment_failed"
    if enrichment_state.get("pending"):
        return "enrichment_pending"

    active_version = _active_summary_version(conversation)
    if not active_version:
        return "missing_active_summary_version"

    source = str(active_version.get("source") or "")
    kind = str(active_version.get("kind") or "")
    if source == "observer" and kind in {"observer_enriched", "corrected_enriched"}:
        return None
    return "active_summary_not_enriched"


def _update_correction_audit(
    uid: str,
    conversation_id: str,
    correction_id: str,
    payload: dict,
) -> None:
    (
        db.collection("users")
        .document(uid)
        .collection("conversations")
        .document(conversation_id)
        .collection("corrections")
        .document(correction_id)
        .set(payload, merge=True)
    )


async def _get_resolve_pool() -> asyncpg.Pool:
    global _resolve_pool
    if _resolve_pool is None:
        _resolve_pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=5433,
            user="postgres",
            password=os.getenv("ELLA_POSTGRES_PASSWORD", "postgres"),
            database="ella_ai",
            min_size=1,
            max_size=4,
        )
    return _resolve_pool


async def _resolve_agent_id_for_uid(uid: str) -> Optional[str]:
    pool = await _get_resolve_pool()
    row = await pool.fetchrow(
        """
        SELECT ac.agents
        FROM users u
        LEFT JOIN agent_clusters ac ON ac.user_id = u.id
        WHERE u.omi_uid = $1
        """,
        uid,
    )
    if not row or not row["agents"]:
        return None

    agents = row["agents"]
    if isinstance(agents, str):
        try:
            agents = json.loads(agents)
        except json.JSONDecodeError:
            return None
    if not isinstance(agents, dict):
        return None
    return agents.get("userAgentId")


async def _resolve_workspace_target_for_uid(uid: str) -> Optional[tuple[str, str, str]]:
    if runtime_bindings_enabled(uid):
        runtime = await resolve_isolated_runtime(uid)
        if runtime is None:
            return None
        return (
            runtime.agent_id,
            os.getenv("ELLA_HERMES_PROVISION_API_URL", "http://100.76.138.56:8210").rstrip("/"),
            os.getenv("ELLA_HERMES_PROVISION_API_TOKEN", ""),
        )

    agent_id = await _resolve_agent_id_for_uid(uid)
    if not agent_id:
        return None
    return agent_id, PROVISION_API_URL.rstrip("/"), PROVISION_API_KEY


async def _fetch_internal_assessment(uid: str, conversation_id: str) -> Optional[dict]:
    """Best-effort fetch of the user's Observer internal_assessment.

    Isolated users fail closed on their active Hermes runtime. Legacy users retain
    the existing 8200 lookup until the canary rollout is complete.
    """
    try:
        target = await _resolve_workspace_target_for_uid(uid)
        if not target:
            return None
        agent_id, provision_api_url, provision_api_key = target

        headers = {}
        if provision_api_key:
            headers["Authorization"] = f"Bearer {provision_api_key}"

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{provision_api_url}/workspace/{agent_id}/metadata/conversations/{conversation_id}",
                headers=headers,
            )

        if resp.status_code >= 400:
            return None

        data = resp.json()
        assessment = data.get("internal_assessment")
        return assessment if isinstance(assessment, dict) else None
    except Exception as e:
        logger.warning(
            "Failed to fetch internal_assessment from Provision API",
            extra={"uid": uid, "conversation_id": conversation_id},
        )
        logger.warning(str(e))
        return None


@router.patch("/conversation/{conversation_id}/summary")
async def update_conversation_summary(
    conversation_id: str,
    update: ConversationSummaryUpdate,
    uid: str = None,
):
    """
    Update the structured summary of an OMI conversation.
    Called by n8n pipeline after agent enhances the summary.
    Requires uid as query parameter (internal endpoint, not user-facing).
    """
    if not uid:
        raise HTTPException(status_code=400, detail="uid query parameter required")

    try:
        return await write_conversation_summary(
            uid=uid,
            conversation_id=conversation_id,
            title=update.title,
            overview=update.overview,
            emoji=update.emoji,
            category=update.category,
            summary_source=update.summary_source,
            summary_kind=update.summary_kind,
            correction_id=update.correction_id,
            based_on_version_id=update.based_on_version_id,
            set_active=update.set_active,
            trace_id=update.trace_id,
            ella_tags=update.ella_tags,
            ella_signal=update.ella_signal,
            internal_assessment_fetcher=_fetch_internal_assessment,
            correction_audit_updater=_update_correction_audit,
            canonical_writer=write_omi_canonical_event,
            require_canonical=update.require_canonical,
        )
    except SummarySanitizationError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "Unsafe conversation summary", "violations": e.violations},
        )
    except ConversationSummaryNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    except InvalidConversationSummaryCategoryError as e:
        raise HTTPException(status_code=400, detail=f"Invalid category: '{e.args[0]}'")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(f"Failed to update conversation summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/conversations/enrichment/reconcile-candidates")
async def list_enrichment_reconcile_candidates(
    uid: Optional[str] = None,
    lookback_minutes: int = 180,
    limit: int = 25,
):
    """
    Internal helper for n8n reconciliation.
    Returns recent completed conversations whose active summary is not yet
    marked as observer-enriched in OMI.
    """
    if not uid:
        raise HTTPException(status_code=400, detail="uid query parameter required")
    if lookback_minutes <= 0:
        raise HTTPException(status_code=400, detail="lookback_minutes must be positive")
    if limit <= 0 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")

    start_date = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    get_recent = getattr(conversations_db, "get_conversations_without_photos", conversations_db.get_conversations)
    conversations = get_recent(
        uid,
        limit=limit,
        offset=0,
        include_discarded=False,
        statuses=["completed"],
        start_date=start_date,
    )

    candidates = []
    for conversation in conversations:
        reason = _enrichment_candidate_reason(conversation)
        if not reason:
            continue

        active_version = _active_summary_version(conversation) or {}
        structured = conversation.get("structured") or {}
        candidates.append(
            {
                "conversation_id": conversation.get("id"),
                "created_at": conversation.get("created_at"),
                "title": structured.get("title") or conversation.get("title") or "",
                "reason": reason,
                "active_summary_version_id": conversation.get("active_summary_version_id"),
                "active_summary_source": active_version.get("source"),
                "active_summary_kind": active_version.get("kind"),
                "enrichment_state": conversation.get("enrichment_state"),
            }
        )

    return {
        "uid": uid,
        "lookback_minutes": lookback_minutes,
        "limit": limit,
        "total_scanned": len(conversations),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# ============================================================================
# Conversation Data Fetch (for reprocessing pipeline)
# ============================================================================


def _conversation_field(conversation, key: str, default=None):
    if isinstance(conversation, dict):
        return conversation.get(key, default)
    return getattr(conversation, key, default)


def _structured_field(structured, key: str):
    if isinstance(structured, dict):
        return structured.get(key)
    return getattr(structured, key, None)


@router.get("/conversation/{conversation_id}/data")
async def get_conversation_data(
    conversation_id: str,
    uid: Optional[str] = None,
):
    """
    Fetch conversation data used by the reprocessing pipeline when re-firing the
    conversation-ready webhook. Internal endpoint; requires uid as query param.
    """
    if not uid:
        raise HTTPException(status_code=400, detail="uid query parameter required")

    try:
        conversation = conversations_db.get_conversation(uid, conversation_id)
    except Exception as e:
        logging.error(f"Failed to fetch conversation {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    segments = _conversation_field(conversation, "transcript_segments", []) or []
    transcript_parts = []
    for segment in segments:
        if isinstance(segment, dict):
            speaker = "User" if segment.get("is_user") else (segment.get("speaker") or "Other")
            text = segment.get("text", "")
        else:
            speaker = "User" if getattr(segment, "is_user", False) else (getattr(segment, "speaker", None) or "Other")
            text = getattr(segment, "text", "")
        transcript_parts.append(f"{speaker}: {text}")

    structured_src = _conversation_field(conversation, "structured", {}) or {}
    structured = {}
    if structured_src:
        structured = {
            "title": _structured_field(structured_src, "title"),
            "overview": _structured_field(structured_src, "overview"),
            "emoji": _structured_field(structured_src, "emoji"),
            "category": _structured_field(structured_src, "category"),
        }
        if structured.get("category") and hasattr(structured["category"], "value"):
            structured["category"] = structured["category"].value

    return {
        "conversation_id": conversation_id,
        "uid": uid,
        "transcript": "\n\n".join(transcript_parts),
        "segment_count": len(segments),
        "structured": structured,
        "started_at": str(_conversation_field(conversation, "started_at", "")),
        "finished_at": str(_conversation_field(conversation, "finished_at", "")),
    }


# ============================================================================
# Conversation Data Fetch (for reprocessing pipeline)
# ============================================================================


def _conversation_field(conversation, key: str, default=None):
    if isinstance(conversation, dict):
        return conversation.get(key, default)
    return getattr(conversation, key, default)


def _structured_field(structured, key: str):
    if isinstance(structured, dict):
        return structured.get(key)
    return getattr(structured, key, None)


@router.get("/conversation/{conversation_id}/data")
async def get_conversation_data(
    conversation_id: str,
    uid: Optional[str] = None,
):
    """
    Fetch conversation data used by the reprocessing pipeline when re-firing the
    conversation-ready webhook. Internal endpoint; requires uid as query param.
    """
    if not uid:
        raise HTTPException(status_code=400, detail="uid query parameter required")

    try:
        conversation = conversations_db.get_conversation(uid, conversation_id)
    except Exception as e:
        logging.error(f"Failed to fetch conversation {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    segments = _conversation_field(conversation, "transcript_segments", []) or []
    transcript_parts = []
    for segment in segments:
        if isinstance(segment, dict):
            speaker = "User" if segment.get("is_user") else (segment.get("speaker") or "Other")
            text = segment.get("text", "")
        else:
            speaker = "User" if getattr(segment, "is_user", False) else (getattr(segment, "speaker", None) or "Other")
            text = getattr(segment, "text", "")
        transcript_parts.append(f"{speaker}: {text}")

    structured_src = _conversation_field(conversation, "structured", {}) or {}
    structured = {}
    if structured_src:
        structured = {
            "title": _structured_field(structured_src, "title"),
            "overview": _structured_field(structured_src, "overview"),
            "emoji": _structured_field(structured_src, "emoji"),
            "category": _structured_field(structured_src, "category"),
        }
        if structured.get("category") and hasattr(structured["category"], "value"):
            structured["category"] = structured["category"].value

    return {
        "conversation_id": conversation_id,
        "uid": uid,
        "transcript": "\n\n".join(transcript_parts),
        "segment_count": len(segments),
        "structured": structured,
        "started_at": str(_conversation_field(conversation, "started_at", "")),
        "finished_at": str(_conversation_field(conversation, "finished_at", "")),
    }


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/health")
async def ella_health():
    """Health check for Ella callback endpoints."""
    return {
        "status": "ok",
        "service": "ella-callbacks",
        "endpoints": [
            "/v1/ella/notification",
            "/v1/ella/emergency",
            "/v1/ella/emergency-contact",
            "/v1/ella/emergency-contacts/{uid}",
            "/v1/ella/daily-summary",
            "/v1/ella/caregiver-dashboard-data",
            "/v1/ella/generate-dashboard-token",
            "# Caregiver CRUD: n8n.ella-ai-care.com/webhook/caregiver-*",
            "/v1/ella/chat/stream",
            "/v1/ella/conversation/{id}/data",
            "/v1/ella/health",
        ],
    }


@router.post("/notification", response_model=NotificationResponse)
async def ella_notification(request: NotificationRequest):
    """
    Send push notification to user with optional TTS audio.

    Called by n8n push-notification workflow when Letta agents need to
    proactively alert the user (medication reminders, urgency detection, etc).

    Flow:
        Letta agent tool call -> n8n webhook -> this endpoint -> TTS -> GCS -> FCM -> iOS
    """
    logger.info(f"[Ella] Notification: uid={request.uid}, urgency={request.urgency}, audio={request.generate_audio}")

    audio_url = request.audio_url

    # Step 1: Generate TTS audio if requested and no pre-existing URL
    if request.generate_audio and not audio_url:
        audio_bytes = _generate_tts_audio(request.message)
        if audio_bytes:
            audio_url = _upload_audio_to_gcs(audio_bytes, request.uid)
            del audio_bytes

    # Step 2: Build FCM data payload
    data = {
        "type": "ella_notification",
        "urgency": request.urgency,
    }
    if audio_url:
        data["audio_url"] = audio_url
        data["action"] = "play_audio"
    if request.metadata:
        data["source"] = request.metadata.source
        if request.metadata.conversation_id:
            data["conversation_id"] = request.metadata.conversation_id

    # Step 3: Send FCM push notification
    try:
        send_notification(
            user_id=request.uid,
            title="Ella",
            body=request.message,
            data=data,
        )
    except Exception as e:
        logger.error(f"[Ella] FCM send failed: {e}")
        return NotificationResponse(
            status="error",
            urgency=request.urgency,
            error=f"FCM delivery failed: {e}",
        )

    logger.info(f"[Ella] Notification sent: uid={request.uid}, audio={'yes' if audio_url else 'no'}")

    return NotificationResponse(
        status="success",
        audio_url=audio_url,
        urgency=request.urgency,
    )


# ============================================================================
# Emergency Alert Models
# ============================================================================


class EmergencyLocation(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None


class EmergencyRequest(BaseModel):
    uid: str
    message: str = Field(default="Emergency alert triggered", max_length=500)
    location: Optional[EmergencyLocation] = None
    audio_context_url: Optional[str] = None
    contacts: Optional[List[dict]] = None


class ContactResult(BaseModel):
    name: str
    method: str
    status: str
    error: Optional[str] = None


class EmergencyResponse(BaseModel):
    status: str
    alert_id: str
    contacts_notified: List[ContactResult] = []
    push_sent: bool = False
    sms_available: bool = False
    error: Optional[str] = None


# ============================================================================
# Emergency Alert Endpoint
# ============================================================================


@router.post("/emergency", response_model=EmergencyResponse)
async def ella_emergency(request: EmergencyRequest):
    """
    Emergency alert endpoint. Called by the iOS app when the elder taps
    the emergency button.

    Flow:
        1. Send immediate FCM push to elder's device (confirmation)
        2. Dispatch SMS/call alerts to emergency contacts via n8n
        3. Return alert status

    The endpoint is designed to work without Twilio credentials:
    - Without Twilio: push notification only (still useful)
    - With Twilio: push + SMS + voice call via n8n workflow
    """
    alert_id = f"emg-{uuid.uuid4().hex[:12]}"
    timestamp = datetime.now(timezone.utc).isoformat()

    logger.info(f"[Ella] EMERGENCY: uid={request.uid}, alert_id={alert_id}")

    contacts_notified: List[ContactResult] = []
    push_sent = False
    sms_available = False

    # Step 1: Send immediate push notification to elder's device
    try:
        data = {
            "type": "ella_emergency_confirmation",
            "urgency": "EMERGENCY",
            "alert_id": alert_id,
        }
        send_notification(
            user_id=request.uid,
            title="Ella - Emergency Alert Sent",
            body="Your emergency contacts are being notified.",
            data=data,
        )
        push_sent = True
        logger.info(f"[Ella] Emergency push sent to elder: uid={request.uid}")
    except Exception as e:
        logger.error(f"[Ella] Emergency push to elder failed: {e}")

    # Step 2: Dispatch to n8n for SMS/call alerts (non-blocking)
    n8n_payload = {
        "alert_id": alert_id,
        "uid": request.uid,
        "message": request.message,
        "timestamp": timestamp,
        "location": request.location.model_dump() if request.location else None,
        "audio_context_url": request.audio_context_url,
        "contacts": request.contacts,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                ELLA_CONFIG.emergency_url,
                json=n8n_payload,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code == 200:
                result = response.json()
                sms_available = result.get("sms_available", False)

                for contact in result.get("contacts_notified", []):
                    contacts_notified.append(
                        ContactResult(
                            name=contact.get("name", "Unknown"),
                            method=contact.get("method", "unknown"),
                            status=contact.get("status", "unknown"),
                            error=contact.get("error"),
                        )
                    )

                logger.info(
                    f"[Ella] Emergency n8n dispatch success: " f"{len(contacts_notified)} contacts, sms={sms_available}"
                )
            else:
                logger.warning(
                    f"[Ella] Emergency n8n dispatch returned {response.status_code}: " f"{response.text[:200]}"
                )

    except httpx.TimeoutException:
        logger.warning("[Ella] Emergency n8n dispatch timed out (10s) - SMS may still be processing")
    except Exception as e:
        logger.error(f"[Ella] Emergency n8n dispatch failed: {e}")

    status = "success" if push_sent or contacts_notified else "partial"

    logger.info(
        f"[Ella] Emergency alert complete: alert_id={alert_id}, "
        f"push={push_sent}, contacts={len(contacts_notified)}, sms={sms_available}"
    )

    return EmergencyResponse(
        status=status,
        alert_id=alert_id,
        contacts_notified=contacts_notified,
        push_sent=push_sent,
        sms_available=sms_available,
    )


# ============================================================================
# Daily Summary Models
# ============================================================================


class DailySummaryHighlight(BaseModel):
    title: str
    overview: str


class DailySummaryMemory(BaseModel):
    content: str
    category: str = "general"


class DailySummaryRequest(BaseModel):
    uid: str
    date: Optional[str] = None


class DailySummaryResponse(BaseModel):
    status: str
    dispatched: bool = False
    error: Optional[str] = None


# ============================================================================
# Daily Summary Endpoint
# ============================================================================


@router.post("/daily-summary", response_model=DailySummaryResponse)
async def ella_daily_summary(request: DailySummaryRequest):
    """
    Trigger daily summary generation for a user's caregivers.

    Called by n8n cron workflow (hourly, at 8pm user-local) or manually
    from the caregiver dashboard. Dispatches to the n8n daily-summary
    webhook which handles data aggregation, email formatting, and delivery.

    Flow:
        n8n cron (hourly) -> checks timezone -> this endpoint -> n8n daily-summary webhook
        OR
        Caregiver dashboard "Send Summary Now" -> this endpoint -> n8n daily-summary webhook
    """
    logger.info(f"[Ella] Daily summary requested: uid={request.uid}, date={request.date}")

    summary_date = request.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    n8n_payload = {
        "uid": request.uid,
        "date": summary_date,
        "trigger": "api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                ELLA_CONFIG.daily_summary_url,
                json=n8n_payload,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code == 200:
                logger.info(f"[Ella] Daily summary dispatched to n8n: uid={request.uid}")
                return DailySummaryResponse(status="success", dispatched=True)
            else:
                logger.warning(f"[Ella] Daily summary n8n returned {response.status_code}: {response.text[:200]}")
                return DailySummaryResponse(
                    status="error",
                    dispatched=False,
                    error=f"n8n returned {response.status_code}",
                )

    except httpx.TimeoutException:
        logger.warning("[Ella] Daily summary n8n dispatch timed out (30s)")
        return DailySummaryResponse(
            status="timeout",
            dispatched=False,
            error="n8n webhook timed out",
        )
    except Exception as e:
        logger.error(f"[Ella] Daily summary dispatch failed: {e}")
        return DailySummaryResponse(
            status="error",
            dispatched=False,
            error=str(e),
        )


# ============================================================================
# Emergency Contact CRUD Models
# ============================================================================


class ContactPermissions(BaseModel):
    emergency_contact: bool = True
    daily_summary: bool = False
    view_conversations: bool = False


class EmergencyContactCreate(BaseModel):
    uid: str
    name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    relationship: str = Field(default="other", max_length=100)
    permissions: ContactPermissions = ContactPermissions()


class EmergencyContactUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    relationship: Optional[str] = Field(default=None, max_length=100)
    permissions: Optional[ContactPermissions] = None


class EmergencyContactOut(BaseModel):
    id: str
    uid: str
    name: str
    phone: str = ""
    email: Optional[str] = None
    relationship: str = "other"
    permissions: ContactPermissions = ContactPermissions()
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============================================================================
# Emergency Contact CRUD Endpoints
# ============================================================================


@router.post("/emergency-contact", response_model=EmergencyContactOut, status_code=201)
async def create_emergency_contact(request: EmergencyContactCreate):
    """Add an emergency contact for a user."""
    data = request.model_dump()
    data['permissions'] = request.permissions.model_dump()
    result = create_contact(request.uid, data)
    logger.info(f"[Ella] Contact created: uid={request.uid}, contact_id={result['id']}, name={request.name}")
    return EmergencyContactOut(**result)


@router.get("/emergency-contacts/{uid}", response_model=List[EmergencyContactOut])
async def list_emergency_contacts(uid: str):
    """List all emergency contacts for a user."""
    contacts = get_contacts(uid)
    logger.info(f"[Ella] Contacts listed: uid={uid}, count={len(contacts)}")
    return [EmergencyContactOut(**c) for c in contacts]


@router.put("/emergency-contact/{contact_id}", response_model=EmergencyContactOut)
async def update_emergency_contact(contact_id: str, request: EmergencyContactUpdate, uid: str = ""):
    """
    Update an emergency contact. Pass uid as a query parameter.

    Example: PUT /v1/ella/emergency-contact/{contact_id}?uid=abc123
    """
    if not uid:
        raise HTTPException(status_code=422, detail="uid query parameter is required")

    existing = get_contact(uid, contact_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Contact not found")

    update_data = request.model_dump(exclude_none=True)
    if 'permissions' in update_data and request.permissions is not None:
        update_data['permissions'] = request.permissions.model_dump()

    result = update_contact(uid, contact_id, update_data)
    logger.info(f"[Ella] Contact updated: uid={uid}, contact_id={contact_id}")
    return EmergencyContactOut(**result)


@router.delete("/emergency-contact/{contact_id}", status_code=204)
async def delete_emergency_contact(contact_id: str, uid: str = ""):
    """
    Remove an emergency contact. Pass uid as a query parameter.

    Example: DELETE /v1/ella/emergency-contact/{contact_id}?uid=abc123
    """
    if not uid:
        raise HTTPException(status_code=422, detail="uid query parameter is required")

    deleted = delete_contact(uid, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Contact not found")

    logger.info(f"[Ella] Contact deleted: uid={uid}, contact_id={contact_id}")


# ============================================================================
# Caregiver Dashboard Token Helpers
# ============================================================================

DASHBOARD_SECRET = os.getenv("ELLA_DASHBOARD_SECRET", "ella-dashboard-dev-secret")


def generate_dashboard_token(caregiver_id: str, user_id: str, omi_uid: str, hours_valid: int = 24) -> str:
    """Generate an HMAC-signed dashboard token.

    Token format: {caregiver_id}.{omi_uid}.{expiry_ts}.{signature}
    """
    expiry = int((datetime.now(timezone.utc) + timedelta(hours=hours_valid)).timestamp())
    payload = f"{caregiver_id}.{omi_uid}.{expiry}"
    signature = hmac.new(DASHBOARD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def validate_dashboard_token(token: str) -> Optional[dict]:
    """Validate and decode a dashboard token. Returns dict with caregiver_id, omi_uid, or None."""
    parts = token.split(".")
    if len(parts) != 4:
        return None

    caregiver_id, omi_uid, expiry_str, signature = parts

    try:
        expiry = int(expiry_str)
    except ValueError:
        return None

    if datetime.now(timezone.utc).timestamp() > expiry:
        return None

    payload = f"{caregiver_id}.{omi_uid}.{expiry_str}"
    expected_sig = hmac.new(DASHBOARD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]

    if not hmac.compare_digest(signature, expected_sig):
        return None

    return {"caregiver_id": caregiver_id, "omi_uid": omi_uid, "expiry": expiry}


# ============================================================================
# Caregiver Dashboard Data Models
# ============================================================================


class DashboardConversation(BaseModel):
    id: str
    title: str
    overview: str
    category: str = "general"
    started_at: Optional[str] = None


class DashboardMemory(BaseModel):
    id: str
    content: str
    category: str = "general"


class DashboardData(BaseModel):
    elder_name: str
    date: str
    date_formatted: str
    conversations: List[DashboardConversation] = []
    memories: List[DashboardMemory] = []
    conversation_count: int = 0
    memory_count: int = 0


# ============================================================================
# Caregiver Dashboard Data Endpoint
# ============================================================================


@router.get("/caregiver-dashboard-data")
async def caregiver_dashboard_data(token: str, date: Optional[str] = None):
    """
    Returns dashboard data for a caregiver, validated by signed token.

    Token is generated by the daily summary n8n workflow and embedded in
    the email link. Valid for 24 hours.

    Query params:
        token: HMAC-signed token from daily summary email
        date: optional date override (YYYY-MM-DD), defaults to today
    """
    token_data = validate_dashboard_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired dashboard token")

    omi_uid = token_data["omi_uid"]
    logger.info(f"[Ella] Dashboard data requested: uid={omi_uid}, caregiver={token_data['caregiver_id']}")

    # Get elder profile
    user_profile = users_db.get_user_profile(omi_uid)
    elder_name = user_profile.get("name", "Your loved one") if user_profile else "Your loved one"

    # Determine date range
    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        start_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    end_dt = start_dt + timedelta(days=1)

    # Format date for display
    date_formatted = start_dt.strftime("%A, %B %d, %Y")

    # Fetch conversations for the day
    raw_conversations = conversations_db.get_conversations(
        omi_uid,
        limit=50,
        offset=0,
        include_discarded=False,
        statuses=["completed"],
        start_date=start_dt,
        end_date=end_dt,
    )

    conversations = []
    for conv in raw_conversations:
        structured = conv.get("structured", {})
        started = conv.get("started_at")
        conversations.append(
            DashboardConversation(
                id=conv.get("id", ""),
                title=structured.get("title", "Conversation"),
                overview=structured.get("overview", ""),
                category=structured.get("category", "general"),
                started_at=started.isoformat() if hasattr(started, "isoformat") else str(started) if started else None,
            )
        )

    # Fetch recent memories
    raw_memories = memories_db.get_memories(omi_uid, limit=20, offset=0)
    memories = []
    for mem in raw_memories:
        if mem.get("is_locked", False):
            continue
        memories.append(
            DashboardMemory(
                id=mem.get("id", ""),
                content=mem.get("content", ""),
                category=mem.get("category", "general"),
            )
        )

    dashboard = DashboardData(
        elder_name=elder_name,
        date=target_date,
        date_formatted=date_formatted,
        conversations=conversations,
        memories=memories,
        conversation_count=len(conversations),
        memory_count=len(memories),
    )

    logger.info(
        f"[Ella] Dashboard data served: uid={omi_uid}, " f"conversations={len(conversations)}, memories={len(memories)}"
    )

    return dashboard


@router.post("/generate-dashboard-token")
async def generate_dashboard_token_endpoint(uid: str, caregiver_id: str):
    """
    Generate a dashboard token for a caregiver. Called by n8n workflows
    when composing daily summary emails.

    This is an internal endpoint — should be called from n8n on the same VPS,
    not exposed to end users.
    """
    token = generate_dashboard_token(caregiver_id=caregiver_id, user_id=uid, omi_uid=uid, hours_valid=24)
    dashboard_url = f"https://ella-ai-care.com/dashboard/?token={token}"

    logger.info(f"[Ella] Dashboard token generated: uid={uid}, caregiver={caregiver_id}")

    return {"token": token, "dashboard_url": dashboard_url, "expires_in_hours": 24}


# ============================================================================
# Caregiver CRUD — MOVED to n8n (ella-ai-care repo)
# iOS app calls n8n.ella-ai-care.com webhooks directly for all caregiver ops.
# All caregiver data in ella-ai-care Postgres, not OMI Firestore.
# See: https://github.com/ellaaicare/ella-ai/issues/55
# ============================================================================
