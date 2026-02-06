# Ella MVP Feature Specs

**Version**: 1.0
**Date**: 2026-02-06
**Author**: Product Manager
**Status**: Ready for Engineering Review

---

## Overview

Four feature specs for Ella v1.0 MVP ("Safe Companion"):

1. **Emergency Button** -- UX + backend contract
2. **Caregiver Invite Flow** -- onboarding + daily summaries
3. **Push Notification Content & Scheduling** -- restore + extend
4. **Cognitive Recall Prompts in Chat** -- Alzheimer's-specific companion feature

Each spec includes: user story, UX behavior, API contract, data model, and acceptance criteria.

---

## Spec 1: Emergency Button

### User Story

> As an Alzheimer's patient, I need a single-tap emergency button so that my caregivers are immediately alerted when I need help.

### UX Behavior

**Location**: Home screen, bottom-right quadrant. Always visible. Never scrolls off screen.

**Appearance**:
- Red circle, 72dp diameter (larger than WCAG minimum)
- White SOS icon centered inside
- Text label "Emergency" below the button (16sp, high contrast)
- Subtle pulse animation when idle (draws attention without being distracting)
- No confirmation dialog on first tap (speed matters in emergencies)

**Tap Flow**:
1. User taps emergency button
2. Button immediately changes to "Alerting..." state (gray, non-tappable, 5-second cooldown)
3. Haptic feedback (medium impact)
4. Full-screen confirmation: "Help is on the way. Your contacts have been notified."
5. Audio plays (TTS): "Help is on the way. Your emergency contacts have been notified."
6. Screen shows: list of contacts being notified with status (sending... / sent)
7. "Cancel" button available for 10 seconds (in case of accidental tap)
8. After 10 seconds, cancel disappears. Emergency is committed.

**Cancel Flow**:
1. User taps "Cancel" within 10-second window
2. Cancel request sent to backend
3. Backend sends follow-up "false alarm" message to any contacts already notified
4. Screen returns to Home

**Accidental Tap Protection**:
- 5-second cooldown between taps (prevents double-trigger)
- Cancel window gives 10 seconds to undo
- No confirmation dialog (deliberate: speed > accidental tap protection for this population)

### API Contract

#### Trigger Emergency

```
POST /v1/ella/emergency
Authorization: Bearer {firebase_token}
Content-Type: application/json

Request:
{
  "uid": "string",
  "location": {                    // optional, from device GPS
    "latitude": 37.7749,
    "longitude": -122.4194,
    "accuracy_meters": 15.0
  },
  "audio_context_seconds": 30,     // last N seconds of audio buffer to attach
  "trigger_source": "manual_button" // or "voice_command", "inactivity_timeout"
}

Response (200):
{
  "emergency_id": "em_abc123",
  "status": "alerting",
  "contacts_notified": [
    {
      "contact_id": "cg_jane",
      "name": "Jane (Daughter)",
      "method": "sms",
      "status": "sent"
    },
    {
      "contact_id": "cg_john",
      "name": "John (Son)",
      "method": "phone_call",
      "status": "queued"
    }
  ],
  "cancel_window_seconds": 10,
  "created_at": "2026-02-06T12:00:00Z"
}
```

#### Cancel Emergency

```
POST /v1/ella/emergency/{emergency_id}/cancel
Authorization: Bearer {firebase_token}

Response (200):
{
  "emergency_id": "em_abc123",
  "status": "cancelled",
  "contacts_notified_of_cancel": ["cg_jane"]
}

Response (409):  // cancel window expired
{
  "error": "cancel_window_expired",
  "message": "Emergency cannot be cancelled after 10 seconds"
}
```

#### Get Emergency Status

```
GET /v1/ella/emergency/{emergency_id}
Authorization: Bearer {firebase_token}

Response (200):
{
  "emergency_id": "em_abc123",
  "status": "active",           // alerting | active | resolved | cancelled
  "contacts": [...],
  "audio_context_url": "https://storage.../emergency/em_abc123.mp3",
  "created_at": "2026-02-06T12:00:00Z",
  "resolved_at": null
}
```

### Data Model

**Firestore**: `users/{uid}/emergencies/{emergency_id}`

```json
{
  "id": "em_abc123",
  "uid": "user123",
  "status": "active",
  "trigger_source": "manual_button",
  "location": { "latitude": 37.7749, "longitude": -122.4194 },
  "audio_context_url": "https://storage.../em_abc123.mp3",
  "contacts_notified": [
    {
      "contact_id": "cg_jane",
      "name": "Jane",
      "phone": "+15551234567",
      "email": "jane@example.com",
      "method": "sms",
      "status": "delivered",
      "notified_at": "2026-02-06T12:00:01Z"
    }
  ],
  "cancel_window_expires_at": "2026-02-06T12:00:10Z",
  "created_at": "2026-02-06T12:00:00Z",
  "resolved_at": null,
  "resolved_by": null
}
```

### Backend Implementation Notes

- Emergency endpoint goes in `ella/routers/emergency.py`
- Uses existing `send_notification()` from `utils/notifications.py` for FCM push
- SMS/phone calls via Twilio (uses credentials from `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`)
- Audio context: grab last 30s from Redis buffer (same buffer used by `/v4/listen` transcription) and upload to GCS
- The cancel window is enforced server-side (check `cancel_window_expires_at` timestamp)
- Log all emergencies to an audit collection: `ella_emergency_audit/{emergency_id}`

### n8n Workflow (Phase 1)

For MVP, the emergency endpoint triggers an n8n webhook that handles the notification cascade:

```
POST /v1/ella/emergency
    -> Store in Firestore
    -> POST https://n8n.ella-ai-care.com/webhook/emergency-alert
       Body: { uid, emergency_id, contacts, audio_url, location }
    -> n8n workflow:
       1. Send SMS to all contacts (parallel)
       2. Send email to all contacts (parallel)
       3. Make phone call to primary contact (Twilio)
       4. If phone not answered in 30s, call secondary contact
       5. Log delivery status back to Firestore
```

### Acceptance Criteria

- [ ] Emergency button visible on Home screen at all times
- [ ] Single tap triggers alert (no confirmation dialog)
- [ ] Contacts receive SMS within 5 seconds of tap
- [ ] Cancel works within 10-second window
- [ ] Cancel sends "false alarm" follow-up to already-notified contacts
- [ ] Audio context (last 30s) attached to emergency record
- [ ] Emergency logged to Firestore with full audit trail
- [ ] Cooldown prevents double-trigger within 5 seconds
- [ ] Works offline-ish: queues emergency if network is temporarily down, sends when reconnected

---

## Spec 2: Caregiver Invite Flow

### User Story

> As an elder using Ella, I want to add my family members so they can see how I'm doing and be alerted if something is wrong.

> As a caregiver, I want to receive daily updates about my loved one without needing to install an app.

### UX Behavior (Elder Side -- In-App)

**Location**: Settings > Care Team

**Add Caregiver Flow**:
1. Elder taps "Add Family Member" button (large, teal, 48dp height)
2. Form appears:
   - Name (required, text field, 24sp font)
   - Phone number (required, phone keyboard)
   - Email (optional, email keyboard)
   - Relationship (dropdown: Daughter, Son, Spouse, Caregiver, Doctor, Other)
3. Elder taps "Send Invite"
4. System generates a 6-digit invite code
5. SMS sent to caregiver: "Hi, [Elder Name] has invited you to their Ella care team. Your code: 123456. Visit ella-ai-care.com/join to get started."
6. Success screen: "[Name] has been invited!"

**Care Team List**:
- Shows all added caregivers with name, relationship, status (invited / active)
- Swipe-to-remove or tap to edit
- Maximum 5 caregivers per elder (MVP limit)

### UX Behavior (Caregiver Side -- Web)

**URL**: `https://ella-ai-care.com/join`

**Join Flow**:
1. Caregiver opens link from SMS
2. Enters invite code (6 digits)
3. Creates account (email + password, or Sign in with Google/Apple)
4. Sees welcome screen: "You're now connected to [Elder Name]'s care team"
5. Redirected to caregiver dashboard

**Caregiver Dashboard** (web, MVP):
- **Today**: Activity status (last conversation time, device connected/disconnected), today's conversation summaries
- **Memories**: Recent memories extracted from conversations (read-only)
- **Alerts**: Emergency history, notification log
- **Settings**: Notification preferences (email frequency, SMS alerts on/off)

### API Contract

#### Create Caregiver Invite

```
POST /v1/ella/caregivers/invite
Authorization: Bearer {firebase_token}  // elder's token
Content-Type: application/json

Request:
{
  "name": "Jane",
  "phone": "+15551234567",
  "email": "jane@example.com",
  "relationship": "daughter"
}

Response (201):
{
  "invite_id": "inv_xyz789",
  "invite_code": "482917",
  "status": "sent",
  "expires_at": "2026-02-13T12:00:00Z"  // 7-day expiry
}
```

#### Accept Caregiver Invite

```
POST /v1/ella/caregivers/accept
Content-Type: application/json

Request:
{
  "invite_code": "482917",
  "caregiver_uid": "firebase_uid_of_caregiver",
  "email": "jane@example.com"
}

Response (200):
{
  "caregiver_id": "cg_jane",
  "patient_uid": "user123",
  "patient_name": "Mom",
  "relationship": "daughter",
  "permissions": {
    "view_summaries": true,
    "view_memories": true,
    "view_transcripts": false,
    "receive_daily_summary": true,
    "receive_emergency_alerts": true
  }
}
```

#### List Caregivers (Elder View)

```
GET /v1/ella/caregivers
Authorization: Bearer {firebase_token}  // elder's token

Response (200):
{
  "caregivers": [
    {
      "id": "cg_jane",
      "name": "Jane",
      "relationship": "daughter",
      "status": "active",
      "joined_at": "2026-02-07T10:00:00Z"
    },
    {
      "id": "inv_xyz789",
      "name": "John",
      "relationship": "son",
      "status": "invited",
      "invited_at": "2026-02-06T12:00:00Z"
    }
  ]
}
```

#### Remove Caregiver

```
DELETE /v1/ella/caregivers/{caregiver_id}
Authorization: Bearer {firebase_token}  // elder's token

Response (204)
```

#### Get Patient Summary (Caregiver View)

```
GET /v1/ella/caregiver/patient
Authorization: Bearer {firebase_token}  // caregiver's token

Response (200):
{
  "patient": {
    "name": "Mom",
    "last_active": "2026-02-06T11:30:00Z",
    "device_connected": true,
    "today_conversations": 3,
    "today_summaries": [
      {
        "id": "conv_abc",
        "title": "Morning medication check-in",
        "overview": "Discussed taking morning medication. Mentioned feeling well.",
        "created_at": "2026-02-06T08:15:00Z"
      }
    ],
    "recent_memories": [
      {
        "id": "mem_123",
        "content": "Takes donepezil 10mg at 8am daily",
        "category": "medical",
        "created_at": "2026-02-06T08:20:00Z"
      }
    ]
  }
}
```

### Data Model

**Firestore**: `users/{uid}/caregivers/{caregiver_id}`

```json
{
  "id": "cg_jane",
  "name": "Jane",
  "phone": "+15551234567",
  "email": "jane@example.com",
  "relationship": "daughter",
  "caregiver_uid": "firebase_uid_of_jane",
  "status": "active",
  "permissions": {
    "view_summaries": true,
    "view_memories": true,
    "view_transcripts": false,
    "receive_daily_summary": true,
    "receive_emergency_alerts": true
  },
  "notification_preferences": {
    "daily_summary_email": true,
    "daily_summary_sms": false,
    "emergency_sms": true,
    "emergency_phone_call": true
  },
  "invited_at": "2026-02-06T12:00:00Z",
  "joined_at": "2026-02-07T10:00:00Z"
}
```

**Firestore**: `ella_invites/{invite_code}`

```json
{
  "code": "482917",
  "patient_uid": "user123",
  "caregiver_name": "Jane",
  "caregiver_phone": "+15551234567",
  "status": "pending",
  "created_at": "2026-02-06T12:00:00Z",
  "expires_at": "2026-02-13T12:00:00Z",
  "accepted_at": null
}
```

### Privacy Rules (Hard-Coded for MVP)

All caregivers in MVP get the same permissions:
- **Can see**: Conversation summaries, memories, activity status, emergency alerts
- **Cannot see**: Raw transcripts, raw audio, full conversation text
- **Cannot do**: Modify memories, send messages as the elder, access device settings

Per-caregiver granular permissions are Phase 2.

### Daily Summary Email

Sent once daily at 8pm in the elder's timezone via n8n workflow:

**Subject**: "Ella Daily Update: [Elder Name] - [Date]"

**Body**:
```
Hi [Caregiver Name],

Here's today's update for [Elder Name]:

STATUS: [Active / No activity today]
Device: [Connected since X / Last seen at X]
Conversations today: [N]

HIGHLIGHTS:
- [Summary 1 title]: [Summary 1 overview, max 100 chars]
- [Summary 2 title]: [Summary 2 overview, max 100 chars]

NEW MEMORIES:
- [Memory 1 content, max 100 chars]

No concerns detected today. / [Alert: concern description]

View full details: https://ella-ai-care.com/dashboard

-- Ella AI Care
```

### Acceptance Criteria

- [ ] Elder can add caregiver with name + phone (email optional)
- [ ] SMS invite sent with 6-digit code
- [ ] Caregiver can join via web with invite code
- [ ] Caregiver dashboard shows today's summaries and memories
- [ ] Caregiver receives daily summary email at 8pm elder's timezone
- [ ] Caregiver receives emergency SMS immediately on emergency trigger
- [ ] Elder can remove caregiver (access revoked immediately)
- [ ] Maximum 5 caregivers per elder enforced
- [ ] Caregiver cannot see raw transcripts or audio
- [ ] Invite codes expire after 7 days

---

## Spec 3: Push Notification Content & Scheduling

### User Story

> As an Alzheimer's patient, I need gentle audio reminders for medication and daily check-ins, because I may forget to take my medicine or may not notice text-only notifications.

### Context

The notification infrastructure already exists:
- `ella/routers/callbacks.py` has `POST /v1/ella/notification` endpoint
- TTS generation via OpenAI API (`tts-1` model, `nova` voice)
- Audio upload to GCS (`ella-tts-audio` bucket)
- FCM push delivery via `utils/notifications.py`

What's missing: scheduling, content templates, and the iOS app playing audio on receipt.

### Notification Types

| Type | Trigger | Frequency | Audio? | Priority |
|------|---------|-----------|--------|----------|
| Medication Reminder | Schedule (cron) | Per medication schedule | Yes | High |
| Morning Check-in | Schedule (cron) | Daily, 9am local | Yes | Normal |
| Evening Check-in | Schedule (cron) | Daily, 7pm local | Yes | Normal |
| Inactivity Alert | Backend logic | If no activity by noon | Yes | Normal |
| Emergency Confirmation | Emergency trigger | On-demand | Yes | Critical |
| Caregiver Connected | Invite accepted | Once | No | Low |

### Content Templates

#### Medication Reminder
```
Title: "Ella Reminder"
Body: "Hi [Name], it's time for your [medication_name]. Have you taken it yet?"
Audio: TTS of body text
FCM Data: { "type": "medication_reminder", "medication": "[name]", "action": "play_audio" }
```

#### Morning Check-in
```
Title: "Good Morning from Ella"
Body: "Good morning, [Name]! How are you feeling today? I'm here if you'd like to chat."
Audio: TTS of body text
FCM Data: { "type": "morning_checkin", "action": "play_audio" }
```

#### Evening Check-in
```
Title: "Evening Check-in"
Body: "Hi [Name], I hope you had a good day. Would you like to tell me about it?"
Audio: TTS of body text
FCM Data: { "type": "evening_checkin", "action": "play_audio" }
```

#### Inactivity Alert (to patient)
```
Title: "Ella is thinking of you"
Body: "Hi [Name], I haven't heard from you today. Just checking in -- are you doing okay?"
Audio: TTS of body text
FCM Data: { "type": "inactivity_checkin", "action": "play_audio" }
```

#### Inactivity Alert (to caregiver, if no response within 2 hours)
```
Title: "Ella Activity Alert"
Body: "[Elder Name] has not been active today and did not respond to check-in. Last activity: [time]."
Delivery: SMS to caregiver phone
FCM Data: { "type": "caregiver_inactivity_alert" }
```

### Scheduling Architecture

#### Phase 1 (MVP): n8n Cron Workflows

n8n handles all scheduling. Three workflows:

**Workflow 1: Morning Check-in** (runs hourly, checks user timezone)
```
Cron: 0 * * * * (every hour)
Logic:
  1. Query Firestore for users where local time = 9am
  2. For each user:
     POST /v1/ella/notification
     { uid, message: morning_template, urgency: "NORMAL", generate_audio: true }
```

**Workflow 2: Evening Check-in** (same pattern, 7pm local)

**Workflow 3: Medication Reminder**
```
Cron: */15 * * * * (every 15 minutes)
Logic:
  1. Query Firestore for users with medication schedules
  2. For each medication where current time matches schedule (+/- 7 min window):
     POST /v1/ella/notification
     { uid, message: medication_template(med_name), urgency: "MEDICATION" }
```

**Workflow 4: Inactivity Monitor**
```
Cron: 0 12 * * * (noon daily)
Logic:
  1. Query Firestore for users with no conversations today
  2. For each inactive user:
     POST /v1/ella/notification (inactivity check-in to patient)
  3. Set 2-hour timer
  4. If still no activity after 2 hours:
     Send SMS to caregivers (inactivity alert)
```

#### Medication Schedule Data Model

**Firestore**: `users/{uid}/ella_settings/medications`

```json
{
  "medications": [
    {
      "id": "med_001",
      "name": "Donepezil",
      "dose": "10mg",
      "schedule": [
        { "time": "08:00", "days": ["mon","tue","wed","thu","fri","sat","sun"] },
        { "time": "20:00", "days": ["mon","tue","wed","thu","fri","sat","sun"] }
      ],
      "active": true,
      "added_by": "caregiver_jane",
      "added_at": "2026-02-06T10:00:00Z"
    }
  ]
}
```

**Who adds medications**: Caregivers, via the web dashboard Settings page. Not the elder (too complex for Alzheimer's patients to manage medication schedules).

### iOS App Audio Playback (Critical)

The app must handle the `play_audio` action in FCM data payload:

1. App receives FCM push with `data.action = "play_audio"` and `data.audio_url`
2. If app is in foreground: show notification banner + auto-play audio
3. If app is in background: show system notification. On tap, open app and play audio.
4. Audio plays through device speaker at current volume (not earpiece)
5. If audio URL fails to load, fall back to system TTS of the notification body text

### API Extensions

#### Set Medication Schedule (Caregiver Endpoint)

```
PUT /v1/ella/caregiver/medications
Authorization: Bearer {firebase_token}  // caregiver's token
Content-Type: application/json

Request:
{
  "medications": [
    {
      "name": "Donepezil",
      "dose": "10mg",
      "schedule": [
        { "time": "08:00", "days": ["mon","tue","wed","thu","fri","sat","sun"] }
      ]
    }
  ]
}

Response (200):
{
  "medications": [
    { "id": "med_001", "name": "Donepezil", "dose": "10mg", "schedule": [...] }
  ]
}
```

#### Get Notification History

```
GET /v1/ella/notifications?limit=20
Authorization: Bearer {firebase_token}

Response (200):
{
  "notifications": [
    {
      "id": "notif_123",
      "type": "medication_reminder",
      "message": "Hi Mom, it's time for your Donepezil.",
      "audio_url": "https://storage.../notif_123.mp3",
      "urgency": "MEDICATION",
      "delivered_at": "2026-02-06T08:00:05Z",
      "status": "delivered"
    }
  ]
}
```

### Acceptance Criteria

- [ ] Morning check-in push arrives at 9am local time with audio
- [ ] Evening check-in push arrives at 7pm local time with audio
- [ ] Medication reminder arrives within 7 minutes of scheduled time
- [ ] iOS app plays audio automatically when notification tapped
- [ ] Inactivity check-in sent to patient if no activity by noon
- [ ] Caregiver alerted via SMS if patient inactive 2 hours after check-in
- [ ] Caregiver can set medication schedules via web dashboard
- [ ] Notification history accessible via API
- [ ] Existing `/v1/ella/notification` endpoint works (verified: it exists in callbacks.py)
- [ ] TTS generates warm, clear audio (OpenAI nova voice, already configured)

---

## Spec 4: Cognitive Recall Prompts in Chat

### User Story

> As an Alzheimer's patient chatting with Ella, I want Ella to gently prompt me to recall things from my day, because cognitive exercises help slow memory decline.

### Design Philosophy

This is NOT a separate "exercises" feature or screen. It's woven into natural conversation with Ella. The patient doesn't know they're doing exercises -- they're just having a friendly chat.

This is primarily a **prompt engineering** deliverable, not a feature engineering deliverable. The chat infrastructure already works (`POST /v2/messages`). We're changing what Ella says, not how she says it.

### Recall Prompt Types

#### Type 1: Same-Day Recall
Ella references something from earlier today and asks the patient to tell her more.

```
Trigger: Patient has had 2+ conversations today
Ella: "Earlier today you mentioned [topic from conversation summary].
       Can you tell me more about that? I'd love to hear the details."
```

**Example**:
- Conversation summary at 10am: "Discussed visit from granddaughter Sarah"
- At 3pm during chat, Ella says: "You mentioned Sarah visited today -- that sounds lovely! What did you two do together?"

#### Type 2: Recent Memory Recall
Ella references a memory from the past few days.

```
Trigger: Patient has memories from past 7 days
Ella: "I remember you told me about [memory content] a few days ago.
       Do you remember that? What else comes to mind about it?"
```

#### Type 3: Preference Confirmation
Ella confirms known preferences, reinforcing the patient's sense of identity.

```
Trigger: Patient has stored preferences/interests in memories
Ella: "I know you enjoy [interest/hobby]. Have you done anything
       related to that recently?"
```

### System Prompt Addition

Add to Ella's system prompt (configured via `ELLA_LLM_BASE_URL` proxy or directly in the LLM client):

```
You are Ella, a warm and caring AI companion for [Patient Name], who has
Alzheimer's disease.

COGNITIVE SUPPORT GUIDELINES:
- Naturally weave memory recall into conversation. Do not announce
  "time for an exercise" -- keep it conversational.
- Reference specific details from the patient's recent conversations and
  memories. Use their name and the names of people they've mentioned.
- If the patient cannot recall something, never express disappointment.
  Say something like "That's okay! Let me remind you..." and share the
  detail warmly.
- Ask open-ended follow-up questions that encourage elaboration:
  "What was that like?", "How did that make you feel?", "Tell me more
  about that."
- Mix recall prompts with new conversation. Aim for roughly 1 recall
  prompt per 3-4 conversational turns. Do not make every message a test.
- Celebrate when the patient remembers something: "That's wonderful that
  you remember that!", "Your memory of that is so vivid!"
- Focus on positive, emotionally rich memories (family, hobbies,
  achievements). Avoid prompting recall of stressful or confusing events.
- Never correct the patient if they misremember details. Gently redirect
  or accept their version. Memory accuracy is less important than the
  cognitive engagement.
```

### Context Injection

The LLM needs access to recent data to make recall prompts specific. The existing Ella LLM proxy already injects context. Ensure these are included:

**Injected into every chat request** (via `set_ella_context` in `routers/chat.py`):

1. **Today's conversation summaries** (titles + overviews)
2. **Recent memories** (last 7 days, max 20)
3. **Patient profile** (name, interests from memories tagged as `interests` or `hobbies`)

This data is already available via the existing OMI API:
- `GET /v3/memories?limit=20` -- recent memories
- `GET /v1/conversations?limit=10` -- recent conversations

The LLM proxy at `ELLA_LLM_BASE_URL` should fetch and inject this context before forwarding to the LLM. If using Letta, the Main Agent already has access to memories in its context window.

### Response Quality Signals

Track these metrics to evaluate whether recall prompts are working:

1. **Engagement length**: Does the patient respond with longer messages after recall prompts?
2. **Conversation duration**: Do conversations with recall prompts last longer?
3. **Recall success rate**: Does the patient add details beyond what Ella prompted? (signals genuine recall)

These are tracked passively -- no additional UI needed. Backend logs the conversation turns; analysis done in Phase 2 reporting dashboard.

### API Changes

No new endpoints needed. The chat endpoint (`POST /v2/messages`) works as-is. Changes are:

1. **System prompt update**: Add cognitive support guidelines to Ella's persona
2. **Context injection**: Ensure recent conversations + memories are in the LLM context window
3. **Prompt tuning**: Iterative refinement of the system prompt based on real conversations

### Implementation Path

**Step 1** (Backend/Integrations): Update the LLM proxy system prompt to include cognitive support guidelines. This is a config change at `ELLA_LLM_BASE_URL` or in the Letta Main Agent's system prompt.

**Step 2** (Backend): Verify that `set_ella_context(uid, task='chat')` in `routers/chat.py` is injecting recent conversations and memories into the LLM context. If not, add this data to the context injection.

**Step 3** (Testing): Have test conversations and evaluate:
- Does Ella reference specific past events?
- Does Ella avoid making every message a recall prompt?
- Does Ella handle "I don't remember" gracefully?
- Are recall prompts emotionally warm, not clinical?

**Step 4** (Iteration): Refine system prompt based on test results. This is ongoing prompt engineering, not a one-time deployment.

### Acceptance Criteria

- [ ] Ella references specific details from today's conversations during chat
- [ ] Ella references memories from the past 7 days
- [ ] Recall prompts appear in roughly 1 out of every 3-4 conversational turns (not every message)
- [ ] If patient says "I don't remember", Ella responds warmly and shares the detail
- [ ] Ella never announces "time for a cognitive exercise"
- [ ] Ella never expresses disappointment at failed recall
- [ ] Ella celebrates successful recall
- [ ] Recent conversations and memories are in the LLM context window during chat
- [ ] No new endpoints or UI needed (works through existing chat)

---

## Cross-Cutting Concerns

### Authentication

All `/v1/ella/*` endpoints use Firebase Auth tokens (same as stock OMI endpoints). The `uid` is extracted from the token via the existing `auth.get_uid()` dependency.

Caregiver endpoints verify that the requesting `uid` has a valid caregiver relationship to the patient.

### Error Handling

All new endpoints return standard error responses:

```json
{
  "error": "error_code",
  "message": "Human-readable description",
  "details": {}  // optional
}
```

HTTP status codes: 400 (bad request), 401 (unauthorized), 403 (forbidden -- caregiver accessing wrong patient), 404 (not found), 409 (conflict -- e.g., cancel window expired), 429 (rate limited), 500 (server error).

### Rate Limiting

- Emergency button: 1 per 5 seconds per user (cooldown)
- Caregiver invite: 5 per hour per user
- Notifications: 20 per hour per user (prevents notification spam)

### Feature Flags

All new features gated by environment variables (consistent with existing Ella pattern):

```bash
ELLA_EMERGENCY_ENABLED=true
ELLA_CAREGIVER_ENABLED=true
ELLA_SCHEDULED_NOTIFICATIONS_ENABLED=true
ELLA_COGNITIVE_PROMPTS_ENABLED=true
```

### File Locations

New files to create:
- `backend/ella/routers/emergency.py` -- Emergency button endpoints
- `backend/ella/routers/caregivers.py` -- Caregiver invite/management endpoints
- `backend/ella/routers/caregiver_view.py` -- Caregiver dashboard API
- `backend/ella/models/emergency.py` -- Emergency Pydantic models
- `backend/ella/models/caregiver.py` -- Caregiver Pydantic models

Modified files:
- `backend/ella/__init__.py` -- Register new routers
- LLM proxy system prompt (in ella-ai repo or Letta agent config)

### Testing

Run `backend/test.sh` after implementation. Add tests for:
- Emergency create/cancel flow
- Caregiver invite/accept/remove flow
- Notification scheduling logic
- Permission checks (caregiver can't access wrong patient)

---

## Implementation Priority

| Spec | Priority | Dependencies | Team |
|------|----------|-------------|------|
| Push Notifications (restore) | P0 | None (endpoint exists) | Backend |
| Emergency Button | P1 | Twilio credentials, n8n workflow | Backend + Integrations |
| Caregiver Invite | P1 | Emergency (shares contact model) | Backend + iOS |
| Cognitive Recall | P2 | None (prompt engineering) | Integrations |

**Critical path**: Push notifications (P0) unblocks the daily companion experience. Emergency button (P1) unblocks caregiver alerts. These two together make the product viable for real use.

---

## Phase 2 Priority Note (Greg Directive)

**Caregiver native app is EARLY Phase 2, not late Phase 2.** Greg wants caregivers to have a dedicated app to stay in touch with the entire care team as soon as possible after MVP.

### Phase 2 Ordering

1. **Caregiver Native App (or PWA)** -- FIRST in Phase 2. Builds on MVP web dashboard APIs. Adds push notifications for caregivers, care team messaging, shared care calendar.
2. **Voice-to-voice with Ella** -- OpenClaw channels integration
3. **Proactive Ella check-ins** -- Pattern-based ("You haven't recorded today")
4. **Medication tracking + adherence reporting** -- Structured extraction from conversations
5. **Doctor portal** -- Basic web, leverages caregiver dashboard infrastructure
6. **Multi-language** -- Spanish first

The caregiver app can start development in the final week of Phase 1, since the caregiver API endpoints (`/v1/ella/caregiver/*`) are built during MVP. The app is a consumer of those APIs.
