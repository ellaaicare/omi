# Inbound Calls PRD - Ella Calls User

**Feature**: Agent-Initiated Inbound Calls
**Branch**: `feature/inbound-calls`
**Issue**: ellaaicare/ella-ai#16
**Status**: In Development
**Last Updated**: 2025-12-04

---

## Executive Summary

Enable Ella to initiate voice calls to users ("Ella is calling you") using existing push notification and V2 voice infrastructure. Users can answer by voice command ("answer") or decline to voicemail.

---

## Problem Statement

Currently, voice interaction is user-initiated only:
- User says "Hey Ella" (wake word)
- User taps phone button (V2 mode)

Ella cannot proactively reach out to users for:
- Medication reminders
- Urgent health alerts
- Scheduled check-ins
- Follow-up conversations

---

## Solution

### High-Level Flow

```
Backend/n8n triggers call
        │
        ▼
Push Notification: "Ella is calling"
        │
        ▼
iOS shows incoming call UI + starts ASR
        │
        ├── User says "Answer" → Start V2 voice call
        ├── User says "Decline" → Play voicemail
        ├── Auto-answer enabled → Start V2 voice call
        └── Timeout (30s) → Play voicemail
```

### Key Features

1. **Push-triggered incoming calls** - No CallKit required
2. **Voice answer detection** - "Answer", "Yes", "Hello" to accept
3. **Voicemail fallback** - Pre-generated TTS plays on decline/timeout
4. **Auto-answer option** - For urgent calls or user preference
5. **Priority levels** - Normal, High, Urgent

---

## Technical Design

### Push Notification Payload

```json
{
  "notification": {
    "title": "Ella is calling",
    "body": "Medication reminder - Answer or say 'voicemail'"
  },
  "data": {
    "action": "incoming_call",
    "call_id": "uuid-123-456",
    "reason": "medication_reminder",
    "reason_display": "Medication reminder",
    "priority": "normal",
    "auto_answer": false,
    "timeout_seconds": 30,
    "voicemail_text": "Hi! I wanted to remind you about your evening medication.",
    "voicemail_audio_url": "https://storage.googleapis.com/.../voicemail.mp3",
    "context": {
      "medication": "Aspirin",
      "scheduled_time": "18:00"
    }
  }
}
```

### iOS Components

#### 1. Push Handler (`notification_service_fcm.dart`)
```dart
if (data['action'] == 'incoming_call') {
  IncomingCallService().handleIncomingCall(data);
}
```

#### 2. Incoming Call Service (NEW)
- Show overlay UI
- Start voice detection
- Handle answer/decline
- Timeout management

#### 3. Voice Answer Detection
```dart
// Answer phrases
final answerPhrases = ['answer', 'pick up', 'yes', 'hello', 'accept'];

// Decline phrases
final declinePhrases = ['decline', 'voicemail', 'no', 'busy', 'later'];
```

#### 4. Incoming Call UI (NEW)
- Full-screen overlay
- "Ella is calling" with reason
- Pulsing answer/decline buttons
- Voice activation indicator
- Timeout progress bar

### Backend Components (ella-ai repo)

#### 1. Initiate Call Endpoint
```
POST /v1/calls/initiate
{
  "uid": "user-123",
  "reason": "medication_reminder",
  "priority": "normal",
  "voicemail_text": "Hi! I wanted to remind you..."
}
```

#### 2. Call Response Endpoint
```
POST /v1/calls/{call_id}/response
{
  "status": "answered|declined|timeout",
  "response_time_ms": 5000
}
```

---

## Implementation Plan

### Phase 1: iOS Foundation (This PR)
- [ ] Incoming call service skeleton
- [ ] Push handler for `incoming_call` action
- [ ] Basic incoming call UI
- [ ] Voice answer detection
- [ ] Voicemail playback (reuse TTS)

### Phase 2: Backend Integration
- [ ] `/v1/calls/initiate` endpoint
- [ ] `/v1/calls/{call_id}/response` endpoint
- [ ] Voicemail TTS generation
- [ ] Call record storage

### Phase 3: n8n Integration
- [ ] Call trigger node
- [ ] Response webhook
- [ ] Scheduling workflows

### Phase 4: Polish
- [ ] Settings UI for auto-answer
- [ ] Custom answer/decline phrases
- [ ] Call history (optional)
- [ ] Analytics

---

## File Structure

```
lib/
├── services/
│   └── incoming_call/
│       ├── incoming_call_service.dart    # Main service
│       ├── voice_answer_detector.dart    # ASR-based detection
│       └── incoming_call_state.dart      # State management
├── widgets/
│   └── incoming_call/
│       ├── incoming_call_overlay.dart    # Full-screen UI
│       ├── call_action_button.dart       # Answer/decline buttons
│       └── voice_indicator.dart          # Listening indicator
└── pages/
    └── settings/
        └── incoming_calls_settings.dart  # Settings page
```

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Call answer rate | > 70% |
| Voice detection accuracy | > 95% |
| Time to answer | < 10s average |
| Voicemail completion | > 90% |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Push not received (app killed) | Clear user expectation, CallKit future |
| Voice detection false positives | Require clear phrase, confirmation sound |
| User ignores all calls | Escalation to emergency contact |
| Battery drain from ASR | 30s timeout, stop on answer/decline |

---

## Dependencies

- Push notification infrastructure (existing)
- V2 voice mode `/v2/voice` (existing)
- On-device ASR (existing)
- TTS playback (existing)

---

## Future Enhancements

- CallKit integration (#18) for background calls
- Video calls
- Group calls
- In-app call scheduling
- Call transcripts/summaries

---

## References

- GitHub Issue: ellaaicare/ella-ai#16
- CallKit Future: ellaaicare/omi#18
- V2 Voice Mode: `lib/services/voice_mode_v2/`
- Push Handler: `lib/services/notifications/notification_service_fcm.dart`
