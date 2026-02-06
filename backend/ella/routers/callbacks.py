"""
Ella Callback Router

Provides Ella-specific callback endpoints that n8n workflows use to trigger
backend actions (push notifications with TTS audio, etc).

Endpoints:
- POST /v1/ella/notification    - Push notification with optional TTS audio
- POST /v1/ella/emergency       - Emergency alert to all configured contacts
- POST /v1/ella/daily-summary   - Trigger daily summary for a user's caregivers
- GET  /v1/ella/health          - Health check
"""

import io
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ella.config import ELLA_CONFIG
from utils.notifications import send_notification
from utils.other.storage import storage_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ella", tags=["ella"])

# GCS bucket for Ella TTS audio files (defaults to project's Firebase Storage bucket)
ELLA_AUDIO_BUCKET = os.getenv("ELLA_AUDIO_BUCKET", "omi-dev-ca005.firebasestorage.app")

# OpenAI TTS configuration
OPENAI_TTS_MODEL = os.getenv("ELLA_TTS_MODEL", "tts-1")
OPENAI_TTS_VOICE = os.getenv("ELLA_TTS_VOICE", "nova")


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
            "/v1/ella/daily-summary",
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
