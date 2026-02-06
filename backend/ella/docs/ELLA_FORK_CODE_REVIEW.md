# Ella Fork Code Review — Backend Strategy

**Date**: 2026-02-05
**Reviewer**: Claude Code
**Context**: iOS team forking OMI app for "Ella" elder care assistant

---

## Executive Summary

**Recommendation**: Keep stock OMI backend code. We've already done the hard work.

The current `feature/ella-v2-fresh` branch is a clean overlay on upstream OMI:
- 673 upstream commits absorbed
- Only 6 files patched (+123 lines)
- All Ella customizations isolated in `ella/` and `utils/ella/`

**Strategy**: iOS app just needs to point `apiBaseUrl` to `https://api.ella-ai-care.com`.

---

## Question 1: Minimum API Contract

### Critical Endpoints (iOS app will break without these)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v4/listen` | WebSocket | Audio capture + transcription |
| `/v1/conversations` | POST | Finalize conversation |
| `/v1/conversations` | GET | List conversations |
| `/v1/conversations/{id}` | GET | Get single conversation |
| `/v3/memories` | GET | List memories |
| `/v3/memories` | POST | Create manual memory |
| `/v3/memories/{id}` | DELETE | Delete memory |
| `/v2/messages` | POST | Send chat message |
| `/v2/messages` | GET | Get chat history |
| `/v1/users/profile` | GET | Get user profile |

### Secondary Endpoints (app functional without, but degraded)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/users/language` | PATCH | Set user language |
| `/v1/users/transcription-preferences` | GET/PATCH | Transcription settings |
| `/v1/apps` | GET | List available apps (not needed for Ella MVP) |
| `/v1/notification` | POST | Push notifications |
| `/v1/mcp/*` | Various | MCP protocol (for Claude Desktop integration) |

### Current Status: ALL IMPLEMENTED

The Ella backend at `api.ella-ai-care.com` implements the full OMI API.
No endpoints are missing.

---

## Question 2: Stock Backend Processing Flows

### Transcription Processing

```
iOS Device
    ↓ WebSocket /v4/listen (Opus audio frames)
Backend receives frames
    ↓ 600ms buffering window
Deepgram/Soniox API (cloud STT)
    ↓ Real-time transcript segments
Accumulate in memory buffer
    ↓ On WebSocket close OR 120s timeout
Store in Redis (in_progress_conversation)
    ↓ iOS calls POST /v1/conversations
process_conversation() {
    ├── [ELLA HOOK] call_summary_agent() → n8n webhook
    │   └── Fallback: local LLM (GPT-4/Grok)
    ├── [ELLA HOOK] call_memory_agent() → n8n webhook
    │   └── Fallback: local LLM extraction
    └── Trigger external integrations
}
    ↓
Save to Firestore: users/{uid}/conversations/{id}
```

### Memory Creation

**Automatic (from conversation)**:
```
process_conversation()
    ↓ Ella memory adapter
POST https://n8n.ella-ai-care.com/webhook/memory-agent
    ↓ Returns { memories: [...] }
Create MemoryDB objects
    ↓
Save to Firestore: users/{uid}/memories/{id}
```

**Manual**:
```
POST /v3/memories { content, category, tags }
    ↓
Create MemoryDB object
    ↓
Save to Firestore
```

### Chat Messages

```
POST /v2/messages { text, conversation_id }
    ↓ [ELLA HOOK] set_ella_context(uid, task='chat')
Route to LLM:
    ├── If ELLA_LLM_BASE_URL set → n8n proxy (Letta context)
    └── Else → OpenAI/Grok direct
    ↓
LLM generates response with:
    • Conversation context
    • Relevant memories (RAG)
    ↓
Save to Firestore
    ↓
Return Message object
```

### Webhook Triggers

The backend fires webhooks/integrations:
1. **Realtime** (during transcription): `send_to_scanner()` → n8n for urgency detection
2. **On completion**: `trigger_external_integrations()` → registered app webhooks
3. **Push notifications**: FCM via `send_notification()`

---

## Question 3: Backend Strategy Recommendation

### Option A: Full OMI Compatibility (RECOMMENDED)

**What we have now.**

Pros:
- iOS app works out of the box (just change `apiBaseUrl`)
- All OMI features available (future-proof)
- Can track upstream bug fixes
- Community plugins/apps compatible

Cons:
- Larger codebase (~2000 files)
- Some unused features

### Option B: Lightweight Ella-Specific Backend

**DON'T DO THIS.**

Would require:
- Reimplementing /v4/listen WebSocket (complex STT integration)
- Reimplementing conversation processing (LLM prompts, Pydantic models)
- Reimplementing Firestore schema (iOS app expects specific structure)
- Months of development

### Recommendation

**Stay on Option A.** We've already achieved the best of both worlds:

```
┌─────────────────────────────────────────────────────────┐
│  Upstream OMI Backend (stock code)                      │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Ella Extensions (+123 lines)                     │  │
│  │  • Summary → n8n webhook                          │  │
│  │  • Memory → n8n webhook                           │  │
│  │  • Scanner → realtime urgency                     │  │
│  │  • LLM Proxy → Letta context                      │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Question 4: Web Dashboard / Branding

### Current State

OMI has a Next.js web app at `/app` for:
- Conversation history viewing
- Memory management
- App store

### Recommendation: Rebrand Existing

**Quick wins:**
1. Change color palette in Tailwind config
2. Replace logo assets
3. Update text content

**Ella colors (per iOS team)**:
```css
/* Teal primary */
--ella-primary: #14B8A6;        /* Main teal */
--ella-primary-dark: #0D9488;   /* Hover state */
--ella-primary-light: #5EEAD4;  /* Accents */

/* Supporting palette */
--ella-bg: #F0FDFA;             /* Light teal background */
--ella-text: #134E4A;           /* Dark teal text */
```

**Files to modify** (in `/app` directory):
```
tailwind.config.js     → Add Ella color palette
public/logo.svg        → Replace with Ella logo
src/app/layout.tsx     → Update metadata, favicon
src/components/ui/*    → Uses Tailwind, auto-updates
```

### Alternative: Fresh Build

If the OMI web app is too complex, a minimal Ella dashboard could be:
- Next.js + shadcn/ui (same stack)
- 3-4 pages: Home, Conversations, Memories, Settings
- Connect to same Firestore via Firebase Admin SDK

**Recommendation**: Start with rebrand, build fresh only if needed.

---

## Question 5: Key Files & Data Flows

### Backend File Map

```
backend/
├── main.py                    # FastAPI app entry + Ella extension hook
├── routers/
│   ├── transcribe.py          # /v4/listen WebSocket (1800+ lines)
│   ├── conversations.py       # Conversation CRUD
│   ├── memories.py            # Memory CRUD
│   ├── chat.py                # /v2/messages
│   ├── users.py               # User profile
│   ├── mcp.py                 # MCP REST API
│   └── mcp_sse.py             # MCP SSE protocol
├── utils/
│   ├── llm/
│   │   ├── clients.py         # LLM initialization + Ella proxy
│   │   └── conversation_processing.py  # Summary/memory generation
│   ├── conversations/
│   │   └── process_conversation.py     # Main processing pipeline
│   └── ella/                  # Ella-specific adapters
│       ├── scanner.py         # Realtime urgency detection
│       ├── summary.py         # n8n summary webhook
│       └── memory.py          # n8n memory webhook
├── ella/                      # Ella extension package
│   ├── __init__.py            # register_ella_extensions()
│   ├── routers/               # Ella-specific endpoints
│   └── docs/                  # This documentation
├── database/
│   ├── conversations.py       # Firestore conversation ops
│   ├── memories.py            # Firestore memory ops
│   └── users.py               # Firestore user ops
└── models/
    ├── conversation.py        # Conversation Pydantic model
    ├── memory.py              # Memory Pydantic model
    └── chat.py                # Message Pydantic model
```

### Data Flow: Complete Elder Care Use Case

```
[Morning Check-In]

1. Elder opens Ella app
   └── App connects WebSocket /v4/listen

2. Elder speaks: "Good morning, I took my medication"
   └── Audio → Deepgram → Transcript segments
   └── [ELLA] send_to_scanner() → n8n urgency check

3. Elder stops recording
   └── WebSocket closes → Finalization triggered

4. Backend processes:
   └── [ELLA] call_summary_agent() → n8n
       └── Returns: { title: "Morning Medication Check-In", ... }
   └── [ELLA] call_memory_agent() → n8n
       └── Returns: { memories: ["Elder took medication at 8am"] }
   └── Store in Firestore

5. Caregiver dashboard shows:
   └── Conversation summary
   └── Memory: "Medication taken at 8am"
   └── n8n can trigger: SMS to caregiver, calendar event, etc.

[Emergency Detection]

1. Elder: "I fell and I can't get up"
   └── send_to_scanner() detects HIGH urgency

2. n8n scanner workflow:
   └── Evaluate urgency keywords
   └── If HIGH: trigger immediate alert
       └── SMS to caregiver
       └── Push notification
       └── Optional: call emergency contact

3. Conversation still processes normally
   └── Stored for later review
```

---

## Elder Care Feature Priorities

### Already Implemented

| Feature | Implementation |
|---------|---------------|
| Transcription | Stock OMI (/v4/listen) |
| Conversation storage | Stock OMI (Firestore) |
| Memory extraction | Ella adapter → n8n |
| Summary generation | Ella adapter → n8n |
| Real-time scanning | Ella send_to_scanner() |
| Chat with context | Stock OMI + Ella LLM proxy |
| MCP for external access | Stock OMI (/v1/mcp/*) |

### Need Implementation (n8n side)

| Feature | Where to Build |
|---------|----------------|
| Urgency detection | n8n /webhook/scanner-agent |
| Caregiver alerts | n8n workflow → SMS/push |
| Daily check-in reminders | n8n scheduled workflow |
| Emergency escalation | n8n workflow → multiple contacts |
| Medication tracking | Memory categorization in n8n |

### Need Implementation (iOS side)

| Feature | Where to Build |
|---------|----------------|
| Emergency button | New UI component |
| Caregiver pairing | New flow + Firestore schema |
| Daily check-in prompt | Push notification + UI |
| Large text / accessibility | Theme modifications |

---

## iOS App Changes Required

### Minimal (just point to Ella backend)

```dart
// lib/env/prod_env.dart
class ProdEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella-ai-care.com/';
  // ... rest unchanged
}
```

### Branding

```dart
// Update theme colors
// lib/theme/app_theme.dart
static const Color primaryColor = Color(0xFF14B8A6);  // Teal
static const Color primaryDark = Color(0xFF0D9488);
```

### Firebase Project

The iOS app needs to use the Ella Firebase project:
1. Create new `GoogleService-Info.plist` from Ella Firebase console
2. Update bundle ID to `com.ellaaicare.ella` (or similar)
3. Configure Firebase Auth (same methods as OMI)

---

## Testing Checklist

### Backend (already verified)

- [x] WebSocket /v4/listen accepts connections
- [x] Transcription works (Deepgram)
- [x] Conversation finalization works
- [x] Ella summary adapter fires
- [x] Ella memory adapter fires
- [x] Ella scanner fires (realtime)
- [x] MCP endpoints work
- [x] Chat messages work

### iOS App (to verify)

- [ ] App connects to api.ella-ai-care.com
- [ ] Firebase auth works with Ella project
- [ ] Transcription records and saves
- [ ] Conversations appear in history
- [ ] Memories appear in memories tab
- [ ] Chat works with context
- [ ] Push notifications work

### n8n Workflows (to build)

- [ ] Scanner urgency detection
- [ ] Caregiver alert workflow
- [ ] Daily check-in scheduler
- [ ] Emergency escalation path

---

## Summary

**Backend**: Ready. Use stock OMI with Ella overlay.
**iOS**: Change `apiBaseUrl` + Firebase project + rebrand.
**Web**: Rebrand existing OMI web app with teal theme.
**n8n**: Build urgency/alert workflows for elder care.

The heavy lifting is done. Focus on:
1. iOS app rebranding + Firebase setup
2. n8n workflows for caregiver alerts
3. Web dashboard rebrand (if needed)
