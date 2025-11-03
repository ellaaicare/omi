# Backend Push Notification PRD (Product Requirements Document)

**Project**: Background TTS Push Notifications
**Date**: November 2, 2025
**Requester**: iOS Developer
**Audience**: Backend Developer
**Priority**: HIGH

---

## 🎯 Objective

Enable backend to send silent push notifications to iOS devices that automatically play TTS audio in the background, without requiring user interaction.

**Use Cases**:
- Medication reminders (time-critical)
- Appointment reminders (30 min before)
- Health alerts from AI agent
- Healthcare provider messages
- Proactive AI companion features

---

## ✅ What iOS Has Already Implemented

### **1. Device Token Registration** ✅
- **Already working!** iOS sends FCM token to backend automatically
- **Endpoint**: `POST /v1/users/fcm-token`
- **Payload**:
  ```json
  {
    "fcm_token": "f5R3tG8kL2mN9pQ1wS4xY7zA0bC3dE6fH9iJ2kL5mN8pQ1rT4uV7wX0yZ3",
    "time_zone": "America/Los_Angeles"
  }
  ```
- **Headers**: `Authorization: Bearer <Firebase JWT>`
- **Backend stores**: `user_id` (from JWT) → `fcm_token` mapping

### **2. Silent Push Handler** ✅
- **Already implemented in main.dart** (lines 86-123)
- Wakes app in background when push arrives
- Supports two delivery modes:
  - **Direct audio URL** (fastest)
  - **Text generation** (generates TTS on device)
- Automatically plays audio without user interaction
- Works when app backgrounded (NOT when terminated)

### **3. Test Button in Developer Settings** ✅
- **Just added!** User can tap "Request Test Push from Backend"
- Calls: `POST /v1/notifications/test-tts-push`
- Provides instant user feedback

---

## 📋 What Backend Needs to Implement

### **Endpoint 1: Send Test TTS Push Notification** 🔴 HIGH PRIORITY

**Endpoint**: `POST /v1/notifications/test-tts-push`

**Purpose**: Test endpoint for iOS developers to trigger a test push notification

**Request**:
```json
POST /v1/notifications/test-tts-push
Authorization: Bearer <Firebase JWT>
Content-Type: application/json

{
  "text": "Test push notification from backend. This is your medication reminder.",
  "voice": "nova",
  "pregenerate": true
}
```

**Parameters**:
- `text` (required): Text to speak
- `voice` (optional, default: "nova"): TTS voice (nova, shimmer, alloy, echo, fable, onyx)
- `pregenerate` (optional, default: true): If true, backend pre-generates TTS audio before sending push

**Response**:
```json
{
  "status": "sent",
  "user_id": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "message_id": "projects/ella-ai-care/messages/0:1234567890",
  "text": "Test push notification...",
  "audio_url": "https://storage.googleapis.com/.../abc123.mp3",
  "request_id": "req-abc123"
}
```

**Implementation**:
```python
@app.post("/v1/notifications/test-tts-push")
async def test_tts_push(
    request: TestTTSPushRequest,
    uid: str = Depends(get_user_id),  # Extract from JWT
):
    """Send test TTS push notification to current user's device"""

    text = request.text or "This is a test notification from the backend."
    voice = request.voice or "nova"

    # 1. Get user's FCM token from database
    user = await db.users.find_one({"uid": uid})
    if not user or not user.get("fcm_token"):
        raise HTTPException(
            status_code=404,
            detail=f"No FCM token found for user {uid}. User must open app to register."
        )

    fcm_token = user["fcm_token"]

    # 2. Pre-generate TTS audio (if requested)
    audio_url = None
    request_id = str(uuid.uuid4())

    if request.pregenerate:
        # Call your TTS API
        tts_response = await generate_tts(
            text=text,
            voice=voice,
            cache_key=f"test_push_{uid}_{int(time.time())}",
        )
        audio_url = tts_response["audio_url"]

    # 3. Send silent push notification
    message = messaging.Message(
        token=fcm_token,
        data={
            "action": "speak_tts",
            "text": text,
            "voice": voice,
            "audio_url": audio_url or "",  # Empty string if not pre-generated
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
                    content_available=True,  # KEY: Silent push (no popup)
                    sound=None,  # No notification sound
                )
            )
        )
    )

    # 4. Send via Firebase Admin SDK
    message_id = messaging.send(message)

    # 5. Log for debugging
    logger.info(f"✅ Test push sent to user {uid}: {message_id}")

    return {
        "status": "sent",
        "user_id": uid,
        "message_id": message_id,
        "text": text,
        "audio_url": audio_url,
        "request_id": request_id,
    }
```

---

### **Endpoint 2: Production Push Notification Sender** 🟡 NEXT PRIORITY

**Endpoint**: `POST /v1/notifications/send-tts-push`

**Purpose**: Production endpoint for AI agents/backend to send proactive TTS notifications

**Request**:
```json
POST /v1/notifications/send-tts-push
Authorization: Bearer <ADMIN_KEY or SERVICE_ACCOUNT_TOKEN>
Content-Type: application/json

{
  "user_id": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "text": "Time to take your blood pressure medication.",
  "voice": "nova",
  "cache_key": "med_reminder_bp",
  "metadata": {
    "type": "medication_reminder",
    "medication_id": "med_123",
    "priority": "high"
  }
}
```

**Parameters**:
- `user_id` (required): Target user's Firebase UID
- `text` (required): Text to speak
- `voice` (optional): TTS voice
- `cache_key` (optional): Cache key for TTS (enables 25x speed improvement)
- `metadata` (optional): Custom metadata for logging/analytics

**Implementation**: Similar to test endpoint, but:
- Accepts `user_id` parameter (not extracted from JWT)
- Requires admin/service account auth
- Includes analytics/logging for production use

---

### **Endpoint 3: Batch Push Notifications** 🔵 FUTURE

**Endpoint**: `POST /v1/notifications/batch-tts-push`

**Purpose**: Send TTS push to multiple users at once (e.g., daily health tips)

**Request**:
```json
POST /v1/notifications/batch-tts-push
Authorization: Bearer <ADMIN_KEY>
Content-Type: application/json

{
  "user_ids": ["user1", "user2", "user3"],
  "text": "Good morning! Time for your daily health check-in.",
  "voice": "nova",
  "cache_key": "daily_checkin_morning"
}
```

---

## 🔧 Firebase Admin SDK Setup

### **Prerequisites**
- Firebase Admin SDK installed: `pip install firebase-admin`
- Service account JSON key file

### **Initialization** (one-time setup)
```python
# backend/config/firebase_admin.py

import firebase_admin
from firebase_admin import credentials, messaging

# Initialize Firebase Admin SDK
cred = credentials.Certificate("path/to/firebase-service-account.json")
firebase_admin.initialize_app(cred)

# Now you can use messaging.send()
```

### **Service Account Key**
1. Go to Firebase Console → Project Settings → Service Accounts
2. Click "Generate new private key"
3. Save as `firebase-service-account.json` (DO NOT commit to git!)
4. Add to environment variable or secrets manager

---

## 📱 iOS Push Payload Format

### **What iOS Expects**

Silent push notification with `content-available: 1` and data payload:

```json
{
  "data": {
    "action": "speak_tts",
    "text": "Time to take your medication",
    "voice": "nova",
    "audio_url": "https://storage.googleapis.com/.../abc123.mp3",
    "request_id": "req-abc123"
  },
  "apns": {
    "headers": {
      "apns-priority": "10",
      "apns-push-type": "background"
    },
    "payload": {
      "aps": {
        "content-available": 1
      }
    }
  }
}
```

### **Fields Explained**
- `action`: Must be `"speak_tts"` or `"tts_available"`
- `text`: Text to speak (used if audio_url is empty)
- `voice`: TTS voice to use
- `audio_url`: (Optional) Pre-generated audio URL (fastest path)
- `request_id`: (Optional) Tracking ID for logs
- `content-available: 1`: KEY - Makes push silent (no popup, no sound)

---

## 🧪 Testing Flow

### **Step 1: iOS Developer Tests**
1. User opens app → device token auto-registers to backend
2. User goes to Settings → Developer Settings
3. User taps "🔔 Request Test Push from Backend"
4. iOS calls `POST /v1/notifications/test-tts-push`
5. Backend sends silent push
6. User backgrounds app (home button)
7. **Expected**: Audio plays automatically in ~3 seconds

### **Step 2: Backend Developer Tests**
```bash
# Get a test user's Firebase JWT token (from iOS logs or Firebase Auth)
USER_JWT="eyJhbGciOiJSUzI1NiIsImtpZCI6IjFlOWdkazcifQ..."

# Send test push
curl -X POST "https://api.ella-ai-care.com/v1/notifications/test-tts-push" \
  -H "Authorization: Bearer $USER_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello from the backend! This is a test push notification.",
    "voice": "nova",
    "pregenerate": true
  }'

# Expected response:
# {
#   "status": "sent",
#   "user_id": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
#   "message_id": "projects/.../messages/0:1234567890",
#   "audio_url": "https://storage.googleapis.com/.../audio.mp3"
# }
```

### **Step 3: Verify iOS Receives Push**
- Check Xcode console logs (connect iPhone to Mac):
  ```
  🔔 Background push received: speak_tts
  🌩️ Cloud TTS: Generating audio with voice: nova
  ✅ Cloud TTS: Got audio URL (cached: true)
  🔊 Playing audio from: https://storage.googleapis.com/.../audio.mp3
  ✅ Cloud TTS: Audio playback started
  ```

---

## 📊 Success Metrics

After implementation, measure:
- **Push delivery success rate**: Target 95%+
- **Average time from push to audio playback**: Target <3s
- **Cache hit rate for TTS**: Target 90%+
- **Background execution success rate**: Target 90%+ (when app backgrounded)
- **User engagement**: % of notifications that play successfully

---

## ⚠️ iOS Limitations (Not Backend's Fault!)

### **1. App Terminated**
- **Limitation**: If user force-quits app, silent push is IGNORED by iOS
- **Workaround**: Send visible notification instead (user must tap)
- **Detection**: Backend can track app state (last_active timestamp)

### **2. 30 Second Execution Window**
- **Limitation**: iOS kills background tasks after ~30 seconds
- **Mitigation**: Use cached TTS (130ms response instead of 3s)
- **Backend**: Pre-cache common phrases

### **3. Do Not Disturb Mode**
- **Limitation**: iOS may silence/throttle notifications
- **Workaround**: Request Critical Alert entitlement (for emergencies only)

---

## 🚀 Implementation Checklist

### **Phase 1: Test Endpoint** (HIGH PRIORITY)
- [ ] Install Firebase Admin SDK
- [ ] Add Firebase service account JSON to backend
- [ ] Implement `POST /v1/notifications/test-tts-push`
- [ ] Test with iOS developer (send test push)
- [ ] Verify audio plays on iOS device
- [ ] Monitor VPS logs during testing

### **Phase 2: Production Endpoint**
- [ ] Implement `POST /v1/notifications/send-tts-push`
- [ ] Add admin authentication
- [ ] Add logging/analytics
- [ ] Test with Letta agent integration

### **Phase 3: Advanced Features**
- [ ] Implement hybrid push strategy (silent vs visible)
- [ ] Pre-cache common TTS phrases
- [ ] Add batch push endpoint
- [ ] Implement app state tracking (`/v1/app-state`)

---

## 📞 Coordination

### **iOS Developer (me) will:**
- Test push notifications on iPhone
- Run Tests 1-4:
  1. Background app + push
  2. Cached TTS (speed test)
  3. Phone locked
  4. Bluetooth routing
- Report results with timings and logs
- Debug any iOS-side issues

### **Backend Developer needs to:**
- Implement test endpoint (`/v1/notifications/test-tts-push`)
- Provide ETA for endpoint availability
- Monitor VPS logs during testing
- Debug push delivery issues

### **PM will:**
- Track implementation progress
- Coordinate testing between iOS and Backend devs
- Collect test results
- Create follow-up tasks

---

## 🎯 Definition of Done

- [ ] Backend endpoint deployed to production
- [ ] iOS developer can trigger test push from app
- [ ] Audio plays automatically when app backgrounded
- [ ] Tests 1-4 pass with timings documented
- [ ] Backend logs confirm push delivery
- [ ] Documentation complete

---

## 📚 Reference Links

- **Firebase Cloud Messaging**: https://firebase.google.com/docs/cloud-messaging
- **Firebase Admin SDK (Python)**: https://firebase.google.com/docs/admin/setup
- **iOS Background Modes**: https://developer.apple.com/documentation/usernotifications/setting_up_a_remote_notification_server/pushing_background_updates_to_your_app
- **iOS Implementation**: `lib/main.dart` lines 86-123
- **Device Token Registration**: `lib/services/notifications/notification_service_fcm.dart`

---

**Questions?** Contact iOS Developer or PM

**Status**: ⏳ Waiting for backend implementation
**ETA**: TBD by backend dev
**Ready to test**: As soon as endpoint is live!
