import os
import hashlib
import uuid
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Tuple, Optional
from pydantic import BaseModel, Field
from firebase_admin import messaging

from database.redis_db import get_enabled_apps, r as redis_client
from utils.apps import get_available_app_by_id, verify_api_key
from utils.app_integrations import send_app_notification
import database.notifications as notification_db
from models.other import SaveFcmTokenRequest
from utils.notifications import send_notification
from utils.other import endpoints as auth
from models.app import App


# logger = logging.getLogger('uvicorn.error')
# logger.setLevel(logging.DEBUG)
router = APIRouter()

# Rate limit settings - more conservative limits to prevent notification fatigue
RATE_LIMIT_PERIOD = 3600  # 1 hour in seconds
MAX_NOTIFICATIONS_PER_HOUR = 10  # Maximum notifications per hour per app per user


def check_rate_limit(app_id: str, user_id: str) -> Tuple[bool, int, int, int]:
    """
    Check if the app has exceeded its rate limit for a specific user
    Returns: (allowed, remaining, reset_time, retry_after)
    """
    now = datetime.utcnow()
    hour_key = f"notification_rate_limit:{app_id}:{user_id}:{now.strftime('%Y-%m-%d-%H')}"

    # Check hourly limit
    hour_count = redis_client.get(hour_key)
    if hour_count is None:
        redis_client.setex(hour_key, RATE_LIMIT_PERIOD, 1)
        hour_count = 1
    else:
        hour_count = int(hour_count)

    # Calculate reset time
    hour_reset = RATE_LIMIT_PERIOD - (int(now.timestamp()) % RATE_LIMIT_PERIOD)
    reset_time = hour_reset

    # Check if hourly limit is exceeded
    if hour_count >= MAX_NOTIFICATIONS_PER_HOUR:
        return False, MAX_NOTIFICATIONS_PER_HOUR - hour_count, hour_reset, hour_reset

    # Increment counter
    redis_client.incr(hour_key)

    remaining = MAX_NOTIFICATIONS_PER_HOUR - hour_count - 1

    return True, remaining, reset_time, 0


@router.post('/v1/users/fcm-token')
def save_token(data: SaveFcmTokenRequest, uid: str = Depends(auth.get_current_user_uid)):
    notification_db.save_token(uid, data.dict())
    return {'status': 'Ok'}


# ******************************************************
# ******************* TEAM ENDPOINTS *******************
# ******************************************************


@router.post('/v1/notification')
def send_notification_to_user(data: dict, secret_key: str = Header(...)):
    if secret_key != os.getenv('ADMIN_KEY'):
        raise HTTPException(status_code=403, detail='You are not authorized to perform this action')
    if not data.get('uid'):
        raise HTTPException(status_code=400, detail='uid is required')
    uid = data['uid']
    token = notification_db.get_token_only(uid)
    send_notification(token, data['title'], data['body'], data.get('data', {}))
    return {'status': 'Ok'}


@router.post('/v1/integrations/notification')
def send_app_notification_to_user(request: Request, data: dict, authorization: Optional[str] = Header(None)):
    # Check app-based auth
    if 'aid' not in data:
        raise HTTPException(status_code=400, detail='aid (app id) in request body is required')

    if not data.get('uid'):
        raise HTTPException(status_code=400, detail='uid is required')
    uid = data['uid']

    # Verify API key from Authorization header
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header. Must be 'Bearer API_KEY'")

    api_key = authorization.replace('Bearer ', '')
    if not verify_api_key(data['aid'], api_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    # Get app details and convert to App model
    app_data = get_available_app_by_id(data['aid'], uid)
    if not app_data:
        raise HTTPException(status_code=404, detail='App not found')
    app = App(**app_data)

    # Check if user has app installed
    user_enabled = set(get_enabled_apps(uid))
    if data['aid'] not in user_enabled:
        raise HTTPException(status_code=403, detail='User does not have this app installed')

    # Check rate limit
    allowed, remaining, reset_time, retry_after = check_rate_limit(app.id, uid)

    # Add rate limit headers to response
    headers = {
        'X-RateLimit-Limit': str(MAX_NOTIFICATIONS_PER_HOUR),
        'X-RateLimit-Remaining': str(remaining),
        'X-RateLimit-Reset': str(reset_time),
    }

    if not allowed:
        headers['Retry-After'] = str(retry_after)
        return JSONResponse(
            status_code=429,
            headers=headers,
            content={'detail': f'Rate limit exceeded. Maximum {MAX_NOTIFICATIONS_PER_HOUR} notifications per hour.'},
        )

    token = notification_db.get_token_only(uid)
    send_app_notification(token, app.name, app.id, data['message'])
    return JSONResponse(status_code=200, headers=headers, content={'status': 'Ok'})


# ******************************************************
# *************** TTS PUSH NOTIFICATIONS ***************
# ******************************************************

class TestTTSPushRequest(BaseModel):
    """Request model for test TTS push notification"""
    text: Optional[str] = Field(None, description="Text to speak (if not provided, uses default test message)")
    voice: Optional[str] = Field("nova", description="TTS voice to use")
    pregenerate: bool = Field(True, description="Pre-generate TTS audio before sending push")


@router.post('/v1/notifications/test-tts-push')
async def test_tts_push(
    request: TestTTSPushRequest,
    uid: str = Depends(auth.get_current_user_uid)
):
    """
    Send test TTS push notification to current user's device

    This endpoint:
    1. Gets user's FCM token
    2. Optionally pre-generates TTS audio
    3. Sends silent push notification
    4. iOS app receives push in background and plays audio

    Used by iOS developer settings "Request Test Push" button
    """

    text = request.text or "This is a test notification from the backend. Testing background audio playback."
    voice = request.voice or "nova"
    request_id = f"req-{uuid.uuid4().hex[:8]}"

    print(f"🔔 Test push requested by user {uid}")
    print(f"   Text: {text[:50]}...")
    print(f"   Voice: {voice}")
    print(f"   Pregenerate: {request.pregenerate}")

    # 1. Get user's FCM token
    fcm_token = notification_db.get_token_only(uid)
    if not fcm_token:
        raise HTTPException(
            status_code=404,
            detail=f"No FCM token found for user {uid}. Please open the app to register your device."
        )

    print(f"   FCM Token: {fcm_token[:20]}...")

    # 2. Pre-generate TTS audio (if requested)
    audio_url = ""

    if request.pregenerate:
        try:
            # Import here to avoid circular dependencies
            from routers.tts import get_tts_manager
            from utils.tts import TTSRequest

            print(f"   🎵 Pre-generating TTS audio...")

            manager = get_tts_manager()
            tts_request = TTSRequest(
                text=text,
                voice=voice,
                model="hd",
                cache_key=f"test_push_{uid}_{int(time.time())}",
                speed=1.0
            )

            tts_response = await manager.generate(
                request=tts_request,
                provider_name=None,
                uid=uid
            )

            audio_url = tts_response.audio_url
            print(f"   ✅ TTS audio pre-generated: {audio_url[:50]}...")

        except Exception as tts_error:
            print(f"   ❌ TTS generation error: {tts_error}")
            # Continue without pre-generated audio
            audio_url = ""

    # 3. Send silent push notification
    message = messaging.Message(
        token=fcm_token,
        data={
            "action": "speak_tts",
            "text": text,
            "voice": voice,
            "audio_url": audio_url,
            "request_id": request_id,
            "timestamp": str(int(time.time())),
        },
        apns=messaging.APNSConfig(
            headers={
                "apns-priority": "10",  # High priority
                "apns-push-type": "background",  # Silent push
            },
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    content_available=True,  # KEY: Silent push (no popup, no sound)
                    sound=None,  # No notification sound
                )
            )
        )
    )

    try:
        message_id = messaging.send(message)
        print(f"   ✅ Push notification sent: {message_id}")

        return {
            "status": "sent",
            "user_id": uid,
            "message_id": message_id,
            "text": text,
            "audio_url": audio_url or None,
            "request_id": request_id,
            "pregenerated": bool(audio_url),
        }

    except Exception as e:
        error_message = str(e)
        print(f"   ❌ Push notification failed: {error_message}")

        # Remove invalid token
        if "Requested entity was not found" in error_message:
            notification_db.remove_token(fcm_token)
            raise HTTPException(
                status_code=404,
                detail="FCM token is invalid or expired. Please restart the app to re-register."
            )

        raise HTTPException(
            status_code=500,
            detail=f"Failed to send push notification: {error_message}"
        )
