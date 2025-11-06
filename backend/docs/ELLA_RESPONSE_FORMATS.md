# Ella Agent Response Formats

**Date**: November 6, 2025
**Purpose**: Exact JSON formats Ella agents must return to match existing hard-coded LLM responses
**Status**: Specification for n8n agent implementation

---

## Overview

Ella's agents replace hard-coded OpenAI LLM calls. To make this a **plug-and-play** swap with **zero backend modifications**, Ella must return responses in the EXACT same format as the old Pydantic models.

**Architecture**: Option B (Synchronous)
- Backend POSTs to Ella webhook → Ella processes → Ella returns JSON synchronously
- Backend converts JSON to Pydantic models (same code path as old LLM)
- No callbacks needed (backend stores directly in Firestore)

---

## 1. Memory Agent Response Format

### Webhook Endpoint
```
POST https://n8n.ella-ai-care.com/webhook/memory-agent
```

### Request from Backend
```json
{
  "uid": "user-123",
  "segments": [
    {
      "text": "I take blood pressure medication every morning",
      "speaker": "SPEAKER_00"
    },
    {
      "text": "Usually around 8am with breakfast",
      "speaker": "SPEAKER_00"
    }
  ]
}
```

### Response to Backend (EXACT FORMAT)
```json
{
  "memories": [
    {
      "content": "User takes blood pressure medication every morning around 8am with breakfast",
      "category": "interesting",
      "visibility": "private",
      "tags": ["health", "medication", "routine"]
    },
    {
      "content": "User has breakfast around 8am",
      "category": "system",
      "visibility": "private",
      "tags": ["routine", "habits"]
    }
  ]
}
```

### Field Specifications

| Field | Type | Required | Default | Options | Description |
|-------|------|----------|---------|---------|-------------|
| `content` | string | ✅ Yes | - | - | The factual memory content. Be specific and actionable. |
| `category` | string | ✅ Yes | "interesting" | "interesting", "system" | Use "interesting" for user facts, "system" for metadata/habits |
| `visibility` | string | ❌ No | "private" | "private", "public" | Currently only "private" is used |
| `tags` | array[string] | ❌ No | [] | - | Lowercase keywords for categorization |

### Category Guidelines

- **"interesting"**: User facts, preferences, relationships, goals, experiences
- **"system"**: Habits, routines, technical preferences, metadata

### Memory Extraction Rules

1. **Be specific**: "User takes lisinopril 10mg daily" > "User takes medication"
2. **Include context**: Add time/frequency/conditions when mentioned
3. **One fact per memory**: Don't combine multiple facts
4. **Max 4 memories**: Most important/novel facts only
5. **Skip obvious**: Don't extract universal facts ("humans need food")

---

## 2. Summary Agent Response Format

### Webhook Endpoint
```
POST https://n8n.ella-ai-care.com/webhook/summary-agent
```

### Request from Backend
```json
{
  "uid": "user-123",
  "transcript": "Hey doc, I wanted to follow up on my blood pressure. It's been around 140 over 90 lately. I'm taking my medication but maybe we should adjust it. Can we schedule a follow-up next Tuesday at 2pm?",
  "started_at": "2025-11-06T14:30:00Z",
  "language_code": "en",
  "timezone": "America/New_York"
}
```

### Response to Backend (EXACT FORMAT)
```json
{
  "title": "Blood Pressure Follow-Up Discussion",
  "overview": "User discussed elevated blood pressure readings (140/90) despite medication compliance. Requested dosage adjustment consultation and scheduled follow-up appointment.",
  "emoji": "🩺",
  "category": "health",
  "action_items": [
    {
      "description": "Schedule follow-up appointment with doctor",
      "due_at": "2025-11-12T14:00:00Z"
    }
  ],
  "events": [
    {
      "title": "Doctor Follow-Up Appointment",
      "description": "Blood pressure medication review",
      "start": "2025-11-12T19:00:00Z",
      "duration": 30
    }
  ]
}
```

### Field Specifications

#### Root Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `title` | string | ✅ Yes | "" | Clear, compelling headline (≤10 words). Use Title Case. Include key noun + verb. |
| `overview` | string | ✅ Yes | "" | 1-3 sentence summary of main topics and key details. |
| `emoji` | string | ✅ Yes | "🧠" | Single emoji representing core subject/mood. Be specific, not generic. |
| `category` | string | ✅ Yes | "other" | One of 40+ categories (see below) |
| `action_items` | array | ❌ No | [] | List of tasks/todos extracted from conversation |
| `events` | array | ❌ No | [] | List of calendar events with confirmed date/time |

#### Action Item Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `description` | string | ✅ Yes | - | Clear action item description |
| `due_at` | string (ISO 8601) | ❌ No | null | When the task is due (UTC timezone) |

**Note**: Backend will auto-populate `completed=false`, `created_at`, `updated_at`, `completed_at=null`

#### Event Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `title` | string | ✅ Yes | - | Event title/name |
| `description` | string | ❌ No | "" | Brief event description |
| `start` | string (ISO 8601) | ✅ Yes | - | Event start time in UTC |
| `duration` | integer | ❌ No | 30 | Duration in minutes (max 180) |

**Important**: All timestamps must be in UTC with format: `YYYY-MM-DDTHH:MM:SSZ`

---

## 3. Available Categories

Must use one of these exact strings (case-sensitive):

```
personal
education
health
finance
legal
philosophy
spiritual
science
entrepreneurship
parenting
romantic
travel
inspiration
technology
business
social
work
sports
politics
literature
history
architecture
music
weather
news
entertainment
psychology
real
design
family
economics
environment
other
```

### Category Selection Guidelines

- **health**: Medical, fitness, mental health, nutrition
- **work**: Job, career, workplace topics
- **finance**: Money, investments, budgets, expenses
- **personal**: Personal life, self-improvement, habits
- **education**: Learning, courses, reading, skills
- **family**: Family relationships, parenting (non-romantic)
- **romantic**: Dating, relationships, romance
- **social**: Friends, social events, networking
- **technology**: Tech, software, gadgets, AI
- **business**: Entrepreneurship, business strategy
- **travel**: Trips, vacations, places
- **other**: Use when no category clearly fits

---

## 4. Title Guidelines

### Good Titles ✅
- "Blood Pressure Follow-Up Discussion" (health context, clear outcome)
- "Q2 Budget Finalized with Team" (business, specific)
- "Weekend Road Trip to Portland" (travel, destination)
- "Python Course Enrollment Decision" (education, action)

### Bad Titles ❌
- "Conversation about stuff" (too vague)
- "The user and I talked about things" (not title case, verbose)
- "Important discussion regarding medical matters" (too formal, wordy)
- "Chat" (no context)

**Formula**: `[Key Noun/Topic] + [Action/Outcome]` in ≤10 words

---

## 5. Overview Guidelines

### Good Overviews ✅
```
"User discussed elevated blood pressure readings (140/90) despite medication
compliance. Requested dosage adjustment consultation and scheduled follow-up
appointment for next Tuesday."
```
(Specific numbers, context, outcome)

```
"Team finalized Q2 marketing budget at $150K with 40% allocated to digital ads.
Discussed hiring two contractors for campaign execution."
```
(Specific amounts, percentages, clear decisions)

### Bad Overviews ❌
```
"The user talked about their health."
```
(Too vague, no details)

```
"We discussed various topics including medication, appointments, and other
health-related matters that the user was concerned about."
```
(Too wordy, no specifics)

**Formula**: Main topic + Key details/numbers + Outcome/decision (1-3 sentences max)

---

## 6. Emoji Guidelines

### Be Specific ✅
- 🩺 Doctor appointment (not 🏥 generic hospital)
- 💡 New idea (not 🧠 generic thinking)
- 🎉 Celebration (not 👍 generic approval)
- 💰 Money/finance (not 💵 generic cash)
- 🎓 Education/graduation (not 📚 generic books)

### Avoid Generic ❌
- 🧠 Generic "thinking"
- 👍 Generic "good"
- 😊 Generic "happy"
- 📝 Generic "notes"

**Goal**: User should understand the topic from emoji alone

---

## 7. Action Items Filtering Rules

### INCLUDE These ✅
- Explicit tasks: "I need to call the dentist"
- Commitments: "I'll send you the report tomorrow"
- Requests: "Can you schedule a meeting?"
- Deadlines: "The proposal is due Friday"

### EXCLUDE These ❌
- Vague intentions: "I should probably exercise more"
- Historical: "I called the dentist yesterday"
- Hypothetical: "Maybe I'll look into it"
- Other people's tasks: "She said she'll handle it"

---

## 8. Calendar Events Filtering Rules

### INCLUDE These ✅
- **Confirmed commitments**: "Let's meet Tuesday at 2pm" (both agreed)
- **User involvement**: User must attend/participate
- **Specific timing**: Concrete date/time (not "sometime" or "soon")
- **Important/actionable**: Missing it has consequences

**Event Types**:
- Meetings & appointments
- Hard deadlines
- Personal commitments
- Travel & transportation
- Recurring obligations

### EXCLUDE These ❌
- Casual mentions: "we should meet sometime"
- Historical references: "we met last week"
- Other people's events: "he has a dentist appointment"
- Vague suggestions: "let's grab coffee soon"
- Hypothetical: "if we meet Tuesday..."

---

## 9. Timezone Handling

**CRITICAL**: All timestamps must be in UTC

Backend provides:
- `started_at`: When conversation started (UTC)
- `timezone`: User's local timezone (e.g., "America/New_York")

Ella must:
1. Parse relative time references ("next Tuesday at 2pm")
2. Convert to UTC using user's timezone
3. Return in ISO 8601 format: `2025-11-12T19:00:00Z`

**Example**:
- User says: "Tuesday at 2pm"
- User timezone: "America/New_York" (EST, UTC-5)
- Ella returns: `"start": "2025-11-12T19:00:00Z"` (2pm EST = 7pm UTC)

---

## 10. Testing Examples

### Example 1: Health Conversation

**Input**:
```json
{
  "uid": "user-123",
  "transcript": "I've been having headaches every afternoon around 3pm for the past week. They last about 2 hours. I'm drinking plenty of water and getting enough sleep. Should I see a doctor?",
  "started_at": "2025-11-06T16:00:00Z",
  "language_code": "en",
  "timezone": "America/Los_Angeles"
}
```

**Expected Response**:
```json
{
  "title": "Recurring Afternoon Headaches Discussion",
  "overview": "User experiencing daily headaches at 3pm for past week, lasting 2 hours each. Adequate hydration and sleep maintained. Considering medical consultation.",
  "emoji": "🤕",
  "category": "health",
  "action_items": [
    {
      "description": "Schedule doctor appointment for recurring headaches",
      "due_at": null
    }
  ],
  "events": []
}
```

### Example 2: Work Meeting

**Input**:
```json
{
  "uid": "user-456",
  "transcript": "Team sync call. Sarah presented Q4 roadmap. We're launching the new dashboard on December 15th. I need to finish the API integration by December 10th. Mike will handle the frontend. Let's meet again next Monday at 10am to review progress.",
  "started_at": "2025-11-06T18:00:00Z",
  "language_code": "en",
  "timezone": "America/New_York"
}
```

**Expected Response**:
```json
{
  "title": "Q4 Dashboard Launch Planning",
  "overview": "Team reviewed Q4 roadmap with dashboard launch scheduled for December 15th. User responsible for API integration completion by December 10th, Mike handling frontend development. Follow-up meeting scheduled.",
  "emoji": "🚀",
  "category": "work",
  "action_items": [
    {
      "description": "Complete API integration for new dashboard",
      "due_at": "2025-12-10T05:00:00Z"
    }
  ],
  "events": [
    {
      "title": "Dashboard Progress Review Meeting",
      "description": "Review Q4 dashboard development progress",
      "start": "2025-11-10T15:00:00Z",
      "duration": 60
    }
  ]
}
```

---

## 11. Error Handling

If Ella encounters an error or cannot process the request:

**Option 1**: Return minimal valid response
```json
{
  "title": "Conversation",
  "overview": "Unable to generate summary",
  "emoji": "🧠",
  "category": "other",
  "action_items": [],
  "events": []
}
```

**Option 2**: Return HTTP 500 status
- Backend will automatically fall back to local LLM
- No partial data needed

---

## 12. Language Support

Backend sends `language_code` in request (e.g., "en", "es", "fr")

Ella should:
- Generate `title` and `overview` in the same language as the transcript
- Use appropriate emojis (universal)
- Use English for field names (JSON structure)

**Example (Spanish)**:
```json
{
  "title": "Discusión sobre Presión Arterial",
  "overview": "El usuario discutió lecturas elevadas de presión arterial...",
  "emoji": "🩺",
  "category": "health",
  ...
}
```

---

## 13. Performance Requirements

- **Response time**: < 30 seconds (backend timeout)
- **Recommended**: 2-5 seconds for good UX
- **Scanner agent**: ~200ms (separate endpoint)

---

## 14. Integration Checklist

- [ ] Memory agent returns exactly 4 fields: `content`, `category`, `visibility`, `tags`
- [ ] Summary agent returns exactly 6 fields: `title`, `overview`, `emoji`, `category`, `action_items`, `events`
- [ ] All categories are from the approved list (40+ options)
- [ ] All timestamps are in UTC with `Z` suffix
- [ ] Action items have `description` (required) and optional `due_at`
- [ ] Events have `title`, `start` (required), optional `description`, `duration`
- [ ] Duration is capped at 180 minutes max
- [ ] Titles are ≤10 words, Title Case
- [ ] Overviews are 1-3 sentences
- [ ] Tags are lowercase
- [ ] No hallucinated information (only extract what's explicitly stated)

---

## 15. Testing Endpoints

Once Ella agents are deployed, test with:

```bash
# Memory Agent
curl -X POST https://n8n.ella-ai-care.com/webhook/memory-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test-user",
    "segments": [
      {"text": "I love hiking on weekends", "speaker": "SPEAKER_00"}
    ]
  }'

# Summary Agent
curl -X POST https://n8n.ella-ai-care.com/webhook/summary-agent \
  -H "Content-Type: application/json" \
  -d '{
    "uid": "test-user",
    "transcript": "Quick test conversation",
    "started_at": "2025-11-06T12:00:00Z",
    "language_code": "en",
    "timezone": "UTC"
  }'
```

Expected: JSON responses matching formats above within 5 seconds

---

**Contact**: @OMI Backend Dev on Discord for questions
**Status**: Ready for Ella Dev n8n implementation
**Last Updated**: November 6, 2025
