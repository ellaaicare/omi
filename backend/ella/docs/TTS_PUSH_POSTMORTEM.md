# TTS Push Notification Post-Mortem

**Date:** January 18, 2026
**Status:** RESOLVED
**Duration:** ~6 hours of debugging

## Summary

TTS (Text-to-Speech) push notifications were not being delivered to iOS devices. FCM reported successful sends, but APNs was not delivering to devices. The root cause was a **bundle ID mismatch** between the backend code and the Firebase Console configuration.

## What Was Broken

iOS devices were not receiving push notifications with TTS audio. The flow was:
1. Backend sends notification via FCM ✅
2. FCM accepts message (1/1 successful) ✅
3. FCM forwards to APNs ❌ **FAILED SILENTLY**
4. iOS device receives nothing ❌

## Root Cause

**Bundle ID Mismatch**

The backend was configured with:
```python
IOS_BUNDLE_ID = 'com.friend-app-with-wearable.ios12'
```

But the Firebase Console had the iOS app registered as:
```
com.greg.friendapp
```

When FCM forwards to APNs, it uses the bundle ID to route the notification. Since `com.friend-app-with-wearable.ios12` wasn't registered in Firebase, APNs couldn't deliver.

## The Fix

Changed `backend/utils/notifications.py` line 20:
```python
# Before (BROKEN)
IOS_BUNDLE_ID = 'com.friend-app-with-wearable.ios12'

# After (WORKING)
IOS_BUNDLE_ID = 'com.greg.friendapp'
```

## Timeline of Investigation

### Initial Symptoms
- FCM reported 1/1 successful sends
- iOS team saw no notifications arriving
- Backend logs showed no errors

### Red Herrings (Things That Weren't The Problem)
1. **APNs keys not configured** - Actually were configured correctly in Firebase
2. **mutable_content vs content_available** - Tried both, neither was the issue
3. **Notification payload structure** - Tried many variations (alert, background, silent)
4. **APNS headers** - Tried various priority and push-type combinations

### What Finally Worked
1. Checked Firebase Console → Cloud Messaging → Apple app configuration
2. Found APNs keys WERE configured correctly
3. Noticed the registered Bundle ID: `com.greg.friendapp`
4. Checked backend code, found: `com.friend-app-with-wearable.ios12`
5. **Mismatch found!**
6. Updated backend to use `com.greg.friendapp`
7. Deployed → Success!

## Notification Payload That Works

```python
# Backend: utils/notifications.py - send_tts_audio_notification()

apns_config = messaging.APNSConfig(
    headers={
        'apns-priority': '10',
        'apns-push-type': 'alert',
    },
    payload=messaging.APNSPayload(
        aps=messaging.Aps(
            content_available=True,  # Wakes the app
            sound='default',
        )
    )
)

msg = messaging.Message(
    token=token,
    notification=messaging.Notification(title=title, body=body),
    data={
        'action': 'speak_tts',
        'audio_url': audio_url,
    },
    apns=apns_config,
)
```

## Prevention Checklist

### Before Deploying Push Notifications

1. **Verify Bundle ID alignment:**
   ```bash
   # Check backend
   grep IOS_BUNDLE_ID backend/utils/notifications.py

   # Check iOS app (Custom.xcconfig takes precedence)
   cat app/ios/Flutter/Custom.xcconfig | grep APP_BUNDLE_IDENTIFIER

   # Check Firebase Console
   # https://console.firebase.google.com/project/YOUR_PROJECT/settings/cloudmessaging
   ```

2. **All three must match:**
   - `IOS_BUNDLE_ID` in backend code
   - `APP_BUNDLE_IDENTIFIER` in iOS xcconfig
   - Bundle ID registered in Firebase Console

3. **Development vs Production:**
   - Debug builds use: `$(APP_BUNDLE_IDENTIFIER).development`
   - Release builds use: `$(APP_BUNDLE_IDENTIFIER)`
   - Both need to be registered in Firebase if testing both

### Firebase Console Checklist

- [ ] iOS app registered with correct Bundle ID
- [ ] APNs Authentication Key uploaded (Key ID + Team ID)
- [ ] OR APNs Certificates uploaded (Dev + Prod)
- [ ] If using `.development` suffix, register that app too

## Configuration Reference

### Current Working Configuration

| Component | Value |
|-----------|-------|
| Firebase Project | `omi-dev-ca005` |
| iOS Bundle ID (Release) | `com.greg.friendapp` |
| iOS Bundle ID (Debug) | `com.greg.friendapp.development` |
| Backend IOS_BUNDLE_ID | `com.greg.friendapp` |
| APNs Key ID | `SCQKB4Y2A4` |
| Team ID | `H6S4582TRM` |

### Files That Matter

| File | Purpose |
|------|---------|
| `backend/utils/notifications.py:20` | `IOS_BUNDLE_ID` constant |
| `app/ios/Flutter/Custom.xcconfig` | iOS bundle ID (overrides Base) |
| `app/ios/Flutter/Base.xcconfig` | iOS bundle ID (default) |
| Firebase Console | APNs key + registered apps |

## Lessons Learned

1. **FCM "success" doesn't mean APNs delivery** - FCM accepts the message, but APNs can still reject it silently if bundle ID doesn't match.

2. **Check the simplest things first** - Bundle ID mismatch is a common issue but easy to overlook when debugging complex notification payloads.

3. **Firebase Console is the source of truth** - Always verify what's actually registered there, not what you think should be there.

4. **Silent failures are the worst** - APNs doesn't return errors for unregistered bundle IDs; it just drops the notification.

5. **Document your configuration** - Having a single reference for bundle IDs, keys, and team IDs prevents future confusion.

## Test Command

```bash
# Quick test (replace UID with your Firebase UID from .env.test)
source .env.test && curl -s -X POST "http://100.101.168.91:8000/v1/test/tts-notification" \
  -H "Content-Type: application/json" \
  -d "{\"uid\": \"$TEST_USER_UID\", \"audio_url\": \"https://storage.googleapis.com/omi-dev-ca005.firebasestorage.app/tts-test/test-tts-3b52a351.mp3\"}"
```

---

**Author:** Claude Code
**Reviewed by:** Greg
**Last Updated:** January 18, 2026
