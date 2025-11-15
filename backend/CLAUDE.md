# OMI Backend - Developer Guide

**Last Updated**: November 10, 2025
**Branch**: `feature/ios-backend-integration`
**Status**: ✅ Ella AI integration + Edge ASR integration + Production deployment on VPS

---

## 🎭 **YOUR ROLE & IDENTITY**

**You are**: Claude-Backend-Developer
**Role**: backend_dev
**Project**: Ella AI Care / OMI Backend (FastAPI/Python)
**Working Directory**: `/Users/greg/repos/omi/backend`

**Your Specialty**:
- Backend APIs (FastAPI, Python)
- TTS/STT integration (OpenAI, Deepgram)
- VAD (Voice Activity Detection)
- Speaker diarization
- Cloud deployment (VPS)
- Performance optimization
- Database design (Firebase/Firestore)

**IMPORTANT**: When starting a new session, ALWAYS introduce yourself to the PM agent first to get context on active tasks and coordinate with other developers.

---

## 📊 **CURRENT STATUS & LATEST WORK**

**Last Session**: November 15, 2025
**Current Task**: E2E Agent Testing Endpoints (6-8 hours)
**Branch**: `feature/backend-e2e-agent-testing`
**Latest Commit**: `0578dc00a` - fix(main): restore ai router import now that ai.py is committed

### **E2E Agent Testing - IN PROGRESS 🔄**

**Task**: Implement test endpoints that call REAL production n8n Letta agents
**Status**: Ready to implement
**Documentation**: `/tmp/BACKEND_E2E_TESTING_INSTRUCTIONS_REVISED.md` (iOS-provided)

**Endpoints to Implement (6 total)**:
1. `POST /v1/test/scanner-agent` - Urgency detection (1h)
2. `POST /v1/test/memory-agent` - Memory extraction (1h)
3. `POST /v1/test/summary-agent` - Daily summaries (1h)
4. `POST /v1/test/chat-sync` - Synchronous chat (1h)
5. `POST /v1/test/chat-async` - Asynchronous chat (3-4h)
6. `GET /v1/test/chat-response/{job_id}` - Async polling (included in #5)

**Key Requirements**:
- ✅ Call REAL n8n agents (not fake keyword matching)
- ✅ Use generic endpoint: `https://n8n.ella-ai-care.com/webhook/chat-agent`
- ✅ Support both audio→STT→agent and text→agent flows
- ✅ Dual chat pattern: sync (simple) + async (production-ready)
- ✅ Real performance metrics (STT latency, agent latency, total)
- ✅ Reuse existing Edge ASR/Deepgram code

**n8n Webhooks to Call**:
- `https://n8n.ella-ai-care.com/webhook/scanner-agent` ✅
- `https://n8n.ella-ai-care.com/webhook/memory-agent` ✅
- `https://n8n.ella-ai-care.com/webhook/summary-agent` ✅
- `https://n8n.ella-ai-care.com/webhook/chat-agent` ✅ (generic, NOT omi-realtime)

**Time Estimate**: 6-8 hours total
**Priority**: High (enables critical AI agent testing for iOS)

**Next Steps**:
1. Create feature branch: `git checkout -b feature/backend-e2e-agent-testing`
2. Create `backend/routers/testing.py` with all 6 endpoints
3. Register router in `main.py`
4. Test with curl (scanner, memory, summary, chat-sync, chat-async)
5. Commit and push for iOS integration

---

**Previous Session**: November 10, 2025
**Previous Commit**: `714968cfc` - docs(edge-asr): add comprehensive Edge ASR integration documentation

### **Edge ASR Integration - COMPLETE ✅**

Successfully implemented and debugged iOS on-device transcription support with three critical bug fixes:

1. **Bug #1** (Fixed: `e62c323bb`): "TranscriptSegment not subscriptable"
   - Root cause: Pydantic object passed where dict expected
   - Impact: WebSocket closing with 1001 code
   - Fix: Convert to dict before buffering

2. **Bug #2** (Fixed: `c2a845665`): "Multiple values for keyword argument"
   - Root cause: Duplicate `speech_profile_processed` field
   - Impact: Continued crashes after first fix
   - Fix: Exclude field from dict conversion

3. **Bug #3** (Fixed: `e8c197368`): Empty transcript field in Firestore
   - Root cause: Transcript built for LLM but not saved
   - Impact: iOS app showing empty conversations
   - Fix: Populate transcript field before saving

**Documentation**: Added 396-line Edge ASR section to CLAUDE.md with protocol specs, bug analysis, testing procedures, and iOS team guidance.

### **Latest Production Test (Nov 10, 01:48 UTC)**

**Device**: iOS (uid: 5aGC5YE9BnhcSoTxxtT4ar6ILQy2)
**Duration**: 71.85 seconds
**Codec**: PCM16 @ 16kHz
**STT**: Deepgram Nova-3

**Results**:
- ✅ WebSocket connection stable
- ✅ 3 conversations processed and finalized
- ✅ Transcripts populated correctly
- ✅ Memories extracted (8 total)
- ✅ Structured summaries generated

### **Ella AI Integration Status**

**Configuration**: ✅ Implemented (Option B - Synchronous)
**Current Status**: ⚠️ Failing with JSON parse errors
**Fallback**: ✅ Working (Local LLM generating summaries)

**Issue**: n8n webhooks returning empty/non-JSON response
- Error: `Expecting value: line 1 column 1 (char 0)`
- Endpoints: `https://n8n.ella-ai-care.com/webhook/summary-agent` and `/memory-agent`
- Impact: Minimal (fallback to local LLM working correctly)

**Summary Generation Currently Using**: Local LLM (OpenAI/Anthropic via LangChain)

### **Architecture Clarification (Nov 11)**

**Intent Classification**: Handled by Ella/Letta agents, NOT backend
- Backend sends transcripts to n8n scanner webhook
- Letta agent cluster performs classification, routing, memory extraction
- Backend receives processed results via callbacks
- **Do NOT implement separate intent classification endpoint**

iOS handoff docs (`app/docs/BACKEND_INTENT_INTEGRATION.md`) are **outdated** - they describe on-device approach that was abandoned.

### **Logging Issue Fixed (Nov 14)**

**Problem**: Summary generation logs not appearing in journalctl
- **Cause**: Python `print()` buffering stdout in production
- **Impact**: Couldn't determine if Ella or local LLM was generating summaries
- **Solution**: Added `flush=True` to all summary generation print statements
- **Commit**: `8248fea7e` - fix(logging): add flush=True to summary generation print statements
- **Status**: ✅ Deployed to production, logs now visible

**Confirmed**: Backend IS generating summaries correctly (titles, emojis, categories) for Edge ASR conversations.

### **Next Actions**

1. ~~Investigate Ella webhook configuration~~ → Posted to Ella team on Discord
2. ~~Fix summary logging~~ → ✅ FIXED (deployed Nov 14)
3. Test next conversation to confirm Ella vs local LLM logging
4. Continue supporting iOS team with Edge ASR testing
5. Ready for next feature work (Ella team handles intent/routing)

---

## 📞 **COMMUNICATING WITH THE PM AGENT**

### **PM Agent Information**
- **PM Name**: Claude-PM (Project Manager)
- **API Endpoint**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`
- **Purpose**: Task coordination, status tracking, team communication

### **When to Contact PM**
1. **Session start** - Introduce yourself and get current tasks
2. **Task completion** - Report what you finished
3. **Blockers** - Report any issues preventing progress
4. **Questions** - Ask for clarification on requirements
5. **Handoffs** - Coordinate with iOS or firmware devs

### **How to Introduce Yourself**

Create a Python script to contact PM:

```python
#!/usr/bin/env python3
import requests
import json

url = "http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages"
headers = {"Content-Type": "application/json"}

data = {
    "messages": [{
        "role": "user",
        "content": """Agent: Claude-Backend-Developer
Role: backend_dev

Project: Ella AI Care / OMI Backend (FastAPI/Python)
Folder: /Users/greg/repos/omi/backend
Specialty: Backend APIs, TTS/STT integration, VAD, speaker diarization, cloud deployment

Status: Just spawned, ready for tasks. What backend work needs attention?

Recent context (if resuming):
- [List any recent work or context you have]

Questions for PM:
- What are the current priorities for backend?
- Any blockers or issues reported by iOS/firmware teams?
- Any pending integrations or API changes needed?"""
    }]
}

try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
```

Save as `/tmp/contact_pm_backend.py` and run: `python3 /tmp/contact_pm_backend.py`

### **What to Report to PM**

**Completed Tasks**:
```python
"Just completed:
1. ✅ [Task name] - [Brief description]
2. ✅ [Task name] - [Files changed: path/to/file.py]

Current status: [Ready for next task / Testing / Deploying]
Ready for: [Next backend tasks / iOS team integration / etc.]"
```

**Blockers**:
```python
"Blocker encountered:
- Task: [What you were working on]
- Issue: [What's blocking you]
- Need: [What you need to unblock - API access, credentials, clarification, etc.]
- Impact: [Who/what is blocked - iOS team, firmware, deployment]"
```

**Questions**:
```python
"Questions for PM:
1. [Specific question about requirements/architecture]
2. [Coordination question - who is responsible for X?]
3. [Priority question - should I work on A or B first?]"
```

---

## 🤖 **ELLA AI INTEGRATION**

**Date**: November 6, 2025
**Status**: ✅ Implementation Complete
**Architecture**: Option B (Synchronous, Pluggable LLM)

### **Overview**

The Ella integration replaces hard-coded OpenAI LLM calls with calls to Ella's Letta-powered agents. This provides:
- ✅ Centralized AI brain (all agent configs in Letta)
- ✅ Pluggable LLM architecture (swap between Groq/OpenAI/Anthropic)
- ✅ Zero backend changes (same Pydantic models, same Firestore structure)
- ✅ Safe fallback (local LLM if Ella unavailable)

### **Key Files**

1. **`routers/ella.py`** (NEW - 407 lines)
   - External integration endpoints for web apps/chatbots
   - `POST /v1/ella/memory` - Submit memories from external sources
   - `POST /v1/ella/conversation` - Submit conversations from external sources
   - `POST /v1/ella/notification` - Trigger push notifications externally

2. **`utils/llm/conversation_processing.py`** (Modified - +66 lines)
   - `get_transcript_structure()` now calls Ella summary agent
   - Waits for response (30s timeout), converts to `Structured` object
   - Falls back to local LLM if Ella fails

3. **`utils/llm/memories.py`** (Modified - +48 lines)
   - `new_memories_extractor()` now calls Ella memory agent
   - Waits for response (30s timeout), converts to `Memory` objects
   - Falls back to local LLM if Ella fails

4. **`routers/transcribe.py`** (Modified - +21 lines)
   - Sends realtime chunks to Ella scanner (fire-and-forget, 1s timeout)
   - Silent failure if Ella unavailable

### **Data Flow (Option B - Synchronous)**

```
OMI Device → Backend WebSocket
            ↓
    [Conversation ends]
            ↓
Backend → POST https://n8n.ella-ai-care.com/webhook/summary-agent (30s wait)
       ← Ella returns JSON summary
       → Backend stores in Firestore
            ↓
Backend → POST https://n8n.ella-ai-care.com/webhook/memory-agent (30s wait)
       ← Ella returns JSON memories
       → Backend stores in Firestore
            ↓
        iOS app polls GET /v1/conversations and GET /v3/memories
```

### **Ella's Response Formats**

**Summary Agent** (`/webhook/summary-agent`):
```json
{
  "title": "Morning Health Check-In",
  "overview": "User discussed...",
  "emoji": "💊",
  "category": "health",
  "action_items": [{"description": "...", "due_at": "..."}],
  "events": [{"title": "...", "start": "...", "duration": 30}]
}
```

**Memory Agent** (`/webhook/memory-agent`):
```json
{
  "memories": [
    {
      "content": "User takes medication daily",
      "category": "interesting",
      "visibility": "private",
      "tags": ["health"]
    }
  ]
}
```

### **Testing Ella Integration**

```bash
# Health check
curl https://api.ella-ai-care.com/v1/ella/health

# Test memory callback endpoint
curl -X POST https://api.ella-ai-care.com/v1/ella/memory \
  -H "Content-Type: application/json" \
  -d '{"uid":"123","conversation_id":"test","memories":[{"content":"Test","category":"interesting"}]}'

# Test notification endpoint
curl -X POST https://api.ella-ai-care.com/v1/ella/notification \
  -H "Content-Type: application/json" \
  -d '{"uid":"123","message":"Test","urgency":"QUESTION","generate_audio":true}'
```

### **Documentation**

- **Complete Spec**: `docs/ELLA_INTEGRATION.md` - Architecture diagrams, implementation details
- **Response Formats**: `docs/ELLA_RESPONSE_FORMATS.md` - Exact JSON formats for Ella Dev
- **Deployment Checklist**: See ELLA_INTEGRATION.md for VPS deployment steps

### **Monitoring Ella Integration**

```bash
# Watch backend logs for Ella calls
journalctl -u omi-backend -f | grep -E "(Ella|📤|✅|⚠️)"

# Look for these messages:
# 📤 Calling Ella summary agent for uid=123
# ✅ Ella summary agent returned: Morning Health Check-In
# ⚠️  Ella summary agent returned status 500, falling back to local LLM
# 🔄 Using local LLM for summary generation
```

### **Rollback Plan**

If Ella integration causes issues:

1. **Automatic fallback**: Already built-in, local LLM takes over if Ella fails
2. **Disable Ella temporarily**: Set env var `ELLA_ENABLED=false` (not yet implemented)
3. **Comment out Ella calls**: Search for `# ====== ELLA INTEGRATION ======` markers
4. **Zero downtime**: iOS app sees no difference, all infrastructure unchanged

### **Git Commits**

```
c64d2d3 - feat(ella): add Ella integration callback endpoints
145df32 - feat(ella): replace summary generation with Ella agent call
acf4505 - feat(ella): replace memory extraction with Ella agent call
5043461 - feat(ella): add realtime chunk sending to Ella scanner
1e53a09 - docs(ella): add comprehensive Ella integration documentation
```

---

## 📱 **EDGE ASR INTEGRATION (iOS On-Device Transcription)**

**Date**: November 10, 2025
**Status**: ✅ Implementation Complete + 3 Critical Bugs Fixed
**Architecture**: Dual-mode WebSocket (Audio + Pre-transcribed Text)

### **Overview**

Edge ASR enables iOS devices to perform speech-to-text locally using Apple Speech Framework, Parakeet, or other on-device ASR engines, then send pre-transcribed text to the backend instead of raw audio. This provides:

- ✅ **Reduced Latency**: No audio encoding/decoding overhead
- ✅ **Lower Bandwidth**: Send text instead of Opus frames
- ✅ **Cost Reduction**: Avoid Deepgram API charges for on-device transcription
- ✅ **Privacy**: PHI never leaves device for speech-to-text
- ✅ **ASR Provider Tracking**: Test multiple ASR frameworks (Apple Speech, Parakeet, Whisper.cpp)
- ✅ **Source Tracking**: Analytics distinguish edge_asr from cloud (Deepgram/Soniox)

### **Data Flow**

**Traditional Audio Flow** (Still Supported):
```
iOS Device → Opus Encoding → WebSocket → Backend → Deepgram API → Transcription
                                            ↓
                                      Firestore DB
```

**Edge ASR Flow** (New):
```
iOS Device → Apple Speech Framework → Pre-transcribed Text
                                            ↓
                                      JSON over WebSocket
                                            ↓
                                      Backend (source='edge_asr')
                                            ↓
                                      600ms processing loop
                                            ↓
                                      Firestore DB
```

### **WebSocket Message Protocol**

iOS sends JSON messages instead of binary audio:

```json
{
  "type": "transcript_segment",
  "text": "Hello, how are you today?",
  "speaker": "SPEAKER_00",
  "start": 0.0,
  "end": 2.5,
  "asr_provider": "apple_speech"
}
```

**Fields**:
- `type` (required): Must be `"transcript_segment"`
- `text` (required): Transcribed text from on-device ASR
- `speaker` (optional): Speaker ID (default: "SPEAKER_00")
- `start` (optional): Segment start time in seconds
- `end` (optional): Segment end time in seconds
- `asr_provider` (optional): ASR framework identifier
  - `"apple_speech"` - Apple Speech Framework
  - `"parakeet"` - NEXA Parakeet
  - `"whisper"` - Whisper.cpp
  - Or any other identifier for testing

### **Backend Implementation**

#### **Key Files Modified**

**1. `routers/transcribe.py` (Lines 1100-1119)** - Edge ASR Handler

```python
elif json_data.get('type') == 'transcript_segment':
    # ====== EDGE ASR INTEGRATION ======
    # Handle pre-transcribed text from iOS on-device ASR
    text = json_data.get('text', '').strip()
    if text:
        asr_provider = json_data.get('asr_provider')  # Optional: apple_speech, parakeet, etc.
        segment = TranscriptSegment(
            text=text,
            speaker=json_data.get('speaker', 'SPEAKER_00'),
            speaker_id=0,
            is_user=False,
            start=json_data.get('start', 0),
            end=json_data.get('end', 0),
            person_id=None,
            source='edge_asr',  # Mark as edge ASR for analytics
            asr_provider=asr_provider  # Track which ASR framework
        )
        stream_transcript([segment.dict(exclude={'speech_profile_processed'})])
        provider_info = f" (provider: {asr_provider})" if asr_provider else ""
        print(f"📱 Edge ASR segment{provider_info}: {text[:50]}...", uid, session_id)
```

**2. `utils/conversations/process_conversation.py` (Lines 503-505)** - Transcript Field Fix

```python
# Build transcript text from segments for iOS app display
transcript_text = conversation.get_transcript(False, people=people)
conversation_dict['transcript'] = transcript_text
```

**3. `models/transcript_segment.py` (Lines 12-27)** - Source/Provider Tracking

```python
class TranscriptSegment(BaseModel):
    text: str
    speaker: Optional[str] = 'SPEAKER_00'
    speaker_id: Optional[int] = None
    is_user: bool
    start: float
    end: float
    source: Optional[str] = None  # "deepgram", "edge_asr", "soniox"
    asr_provider: Optional[str] = None  # "apple_speech", "parakeet", "whisper"
    # ... other fields ...
```

### **Three Critical Bugs Fixed**

#### **Bug #1: "TranscriptSegment object is not subscriptable"**

**Symptom**: WebSocket closing with 1001 code immediately after receiving edge ASR segments

**Error Log**:
```
📱 Edge ASR segment (provider: apple_speech): To attend the...
Error during WebSocket operation: 'TranscriptSegment' object is not subscriptable
Connection Closed
```

**Root Cause**:
- Created Pydantic `TranscriptSegment` object (line 1106)
- Passed directly to `stream_transcript([segment])`
- Downstream code at lines 880, 884-891 expected dict format
- Tried to access `segment["start"]` on Pydantic object → TypeError

**Fix**: Convert to dict before buffering
```python
# Before (BROKEN):
stream_transcript([segment])

# After (FIXED):
stream_transcript([segment.dict()])
```

**Git Commit**: `e62c323bb` - "fix(edge-asr): convert TranscriptSegment to dict to prevent 'not subscriptable' error"

---

#### **Bug #2: "Multiple values for keyword argument 'speech_profile_processed'"**

**Symptom**: After fixing Bug #1, new error appeared

**Error Log**:
```
📱 Edge ASR segment (provider: apple_speech): Great national...
Error during WebSocket operation: TranscriptSegment() got multiple values for keyword argument 'speech_profile_processed'
```

**Root Cause**:
- Used `segment.dict()` which included ALL fields including `speech_profile_processed=True`
- Line 894: `TranscriptSegment(**dict, speech_profile_processed=speech_profile_processed)`
- Passed `speech_profile_processed` twice (once from dict, once explicitly)

**Fix**: Exclude field from dict conversion
```python
# Before (BROKEN):
stream_transcript([segment.dict()])

# After (FIXED):
stream_transcript([segment.dict(exclude={'speech_profile_processed'})])
```

**Git Commit**: `c2a845665` - "fix(edge-asr): exclude speech_profile_processed from dict to prevent duplicate keyword error"

---

#### **Bug #3: Empty Transcript Field in Firestore**

**Symptom**: Conversations completing successfully but transcript field empty in iOS app

**Firestore Data**:
```
Conversation ID: 2001081e-eb71-442b-81f3-734adc8eadfd
Status: completed
Transcript: None  ❌
Segments: [{"text": "...", ...}] ✅
```

**Root Cause**:
- Line 108 in `process_conversation.py` builds `transcript_text` for LLM
- Used for generating structured summary
- Never assigned back to conversation object before saving
- iOS app displays `transcript` field, not raw segments → empty conversations

**Fix**: Populate transcript field before saving
```python
# Before (BROKEN):
conversation.status = ConversationStatus.completed
conversations_db.upsert_conversation(uid, conversation.dict())

# After (FIXED):
conversation.status = ConversationStatus.completed
conversation_dict = conversation.dict()

# Build transcript text from segments for iOS app display
transcript_text = conversation.get_transcript(False, people=people)
conversation_dict['transcript'] = transcript_text

conversations_db.upsert_conversation(uid, conversation_dict)
```

**Git Commit**: `e8c197368` - "fix(edge-asr): populate transcript field from segments during conversation processing"

---

### **Backend Protocol & Conversation Lifecycle**

**Questions from iOS Team (Answered)**:

**Q1: How does finalization work?**
- Backend uses **WebSocket close** as primary finalization trigger
- Also has **120-second timeout** if WebSocket stays open but idle
- iOS should send `isFinal` segments only, then close WebSocket when done
- No need for explicit "finalize" message

**Q2: When does segment accumulation happen?**
- **600ms processing loop** (see `routers/transcribe.py` line 858-869)
- Backend buffers incoming segments in `realtime_segment_buffers`
- Every 600ms, processes buffered segments and adds to conversation
- Segments accumulate until finalization (WebSocket close or timeout)

**Q3: Should iOS send partial or final segments?**
- **Send only `isFinal` segments** (recommended)
- Deepgram sends complete utterances, not partial updates
- Sending all partial updates causes duplicate text in UI
- Example issue: "Do you" → "Do you have" → "Do you have a" creates 3 segments

**Q4: How does Deepgram behave?**
- Deepgram sends streaming partial results during processing
- Backend accumulates these but only keeps final results
- Final utterances are complete, non-overlapping segments
- iOS should mimic this: send complete utterances only

### **Source & Provider Tracking**

**Source Field** (`source`):
- `"deepgram"` - Cloud transcription via Deepgram API
- `"edge_asr"` - iOS on-device transcription
- `"soniox"` - Alternative cloud provider
- Used for backend analytics and debugging

**ASR Provider Field** (`asr_provider`):
- `"apple_speech"` - Apple Speech Framework
- `"parakeet"` - NEXA Parakeet model
- `"whisper"` - Whisper.cpp
- Used for iOS A/B testing different ASR frameworks
- Backend logs: `📱 Edge ASR segment (provider: apple_speech): text...`

### **Testing Edge ASR Integration**

**Test Script** (`scripts/quick_dump_transcripts.py`):
```bash
# Quick check recent conversations with source tracking
python scripts/quick_dump_transcripts.py <uid> [limit]

# Example:
python scripts/quick_dump_transcripts.py 5aGC5YE9BnhcSoTxxtT4ar6ILQy2 5
```

**Output Shows**:
```
CONVERSATION 1
ID: 2001081e-eb71-442b-81f3-734adc8eadfd
Created: 2025-11-10 07:14:32
Status: completed
Source: edge_asr
Segments: 1
Segment sources: {'edge_asr'}
ASR providers: {'apple_speech'}

First segment:
  Text: Hello, this is a test from Apple Speech Framework...
  Speaker: SPEAKER_00
  Source: edge_asr
  ASR Provider: apple_speech
```

**Verification Checklist**:
1. ✅ Conversation created with status='completed'
2. ✅ Transcript field populated (not empty)
3. ✅ Structured summary generated (title, overview, emoji)
4. ✅ Source field = 'edge_asr'
5. ✅ ASR provider field = 'apple_speech' (or other provider)
6. ✅ Segments have text content
7. ✅ iOS app displays conversation correctly

### **Backend Logs to Monitor**

```bash
# Watch edge ASR activity
journalctl -u omi-backend -f | grep "📱 Edge ASR"

# Example output:
📱 Edge ASR segment (provider: apple_speech): Hello, this is a test...
📱 Edge ASR segment (provider: parakeet): User mentioned medication...
📱 Edge ASR segment: Generic segment without provider tracking...
```

### **iOS Team Guidance**

**Best Practices**:
1. **Send only `isFinal` segments** to avoid duplicate text
2. **Close WebSocket** when conversation ends (don't rely on timeout)
3. **Include `asr_provider`** for A/B testing tracking
4. **Test thoroughly** with different ASR frameworks
5. **Handle WebSocket errors** gracefully (1001 close codes were bugs, now fixed)

**Testing Multiple ASR Frameworks**:
```swift
// Example: Switch between providers
let provider = userSelectedASR // "apple_speech", "parakeet", "whisper"

let message = [
    "type": "transcript_segment",
    "text": finalTranscript,
    "asr_provider": provider
]

webSocket.send(JSONEncoder().encode(message))
```

### **Known Issues & Solutions**

**Issue: Duplicate Text in UI**
- **Cause**: iOS sending all partial updates (49+ segments in 1 second)
- **Backend Behavior**: Working as designed, accumulates all segments
- **Solution**: iOS sends only `isFinal` segments

**Issue: Empty Conversations in App**
- **Cause**: Transcript field not populated (Bug #3)
- **Status**: ✅ Fixed in commit `e8c197368`

**Issue: WebSocket Closing with 1001**
- **Cause**: Pydantic object vs dict mismatch (Bug #1 and #2)
- **Status**: ✅ Fixed in commits `e62c323bb` and `c2a845665`

### **Analytics & Monitoring**

**Firestore Queries**:
```python
# Get all edge ASR conversations
conversations = db.collection('users').document(uid) \
    .collection('conversations') \
    .where('source', '==', 'edge_asr') \
    .order_by('created_at', direction=firestore.Query.DESCENDING) \
    .limit(10).stream()

# Compare providers (requires client-side filtering of segments)
# Backend stores asr_provider in segment-level, not conversation-level
```

**Backend Metrics**:
- Track edge_asr vs deepgram usage per user
- Monitor ASR provider distribution (apple_speech, parakeet, etc.)
- Compare conversation completion rates
- Analyze transcript length by source

### **Git Commits (Edge ASR)**

```
e8c197368 - fix(edge-asr): populate transcript field from segments during conversation processing
c2a845665 - fix(edge-asr): exclude speech_profile_processed from dict to prevent duplicate keyword error
e62c323bb - fix(edge-asr): convert TranscriptSegment to dict to prevent 'not subscriptable' error
015bd2f0a - feat(edge-asr): add ASR provider tracking and transcript dump tools
e88e7ea88 - docs(edge-asr): add iOS quick start guide
404519ba6 - feat(edge-asr): add source tracking and testing guide
4a38129f0 - test(edge-asr): add comprehensive test suite for edge ASR integration
f410475d6 - feat(transcribe): add edge ASR support for iOS on-device transcription
```

### **Future Enhancements**

1. **Multi-Speaker Support**: Parse speaker IDs from iOS
2. **Timestamp Validation**: Verify start/end times are sequential
3. **Quality Metrics**: Track accuracy by ASR provider
4. **Fallback Logic**: Auto-switch to Deepgram if edge ASR fails
5. **Compression**: Compress transcript segments before Firestore storage

---

## 🌐 Production Deployment (VPS)

### Server Information
- **URL**: https://api.ella-ai-care.com
- **Server**: Vultr VPS (100.101.168.91)
- **OS**: Ubuntu 22.04
- **Service**: systemd (`omi-backend.service`)
- **Auto-start**: Enabled on boot
- **Monitoring**: journalctl logs

### Quick Access
```bash
# SSH into VPS
ssh root@100.101.168.91

# Check service status
systemctl status omi-backend

# View live logs
journalctl -u omi-backend -f

# Restart service
systemctl restart omi-backend

# View recent errors
journalctl -u omi-backend -n 100 --no-pager | grep ERROR
```

### Production Environment
- **Working Directory**: `/root/omi/backend`
- **Virtual Environment**: `/root/omi/backend/venv`
- **Google Credentials**: `/root/omi/backend/google-credentials.json`
- **Environment File**: `/root/omi/backend/.env`

### VPS Configuration Files

**Systemd Service** (`/etc/systemd/system/omi-backend.service`):
```ini
[Unit]
Description=OMI Backend API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/omi/backend
Environment="PATH=/root/omi/backend/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="GOOGLE_APPLICATION_CREDENTIALS=/root/omi/backend/google-credentials.json"
ExecStart=/root/omi/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**Environment Variables** (`.env`):
```bash
# Redis Configuration (n8n Docker container)
REDIS_DB_HOST=172.21.0.4
REDIS_DB_PORT=6379
REDIS_DB_PASSWORD=

# GCS Bucket Configuration
BUCKET_PRIVATE_CLOUD_SYNC=omi-dev-ca005.firebasestorage.app

# Firebase Configuration
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=./google-credentials.json

# API Keys (same as local development)
DEEPGRAM_API_KEY=...
OPENAI_API_KEY=...
# ... other keys ...
```

### Firestore Configuration

**Composite Indexes Created**:
1. **Conversations Index** (October 30, 2025):
   - Collection: `conversations`
   - Fields: `discarded`, `status`, `created_at`
   - Status: ✅ Active

2. **Memories Index** (October 30, 2025):
   - Collection: `memories`
   - Fields: `scoring`, `created_at`
   - Status: ✅ Active (user added)

### GCS Bucket Permissions

**Service Account**: `firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com`
**Bucket**: `gs://omi-dev-ca005.firebasestorage.app`
**Role**: Storage Object Admin

```bash
# Grant permissions (already done)
gsutil iam ch serviceAccount:firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com:roles/storage.objectAdmin \
  gs://omi-dev-ca005.firebasestorage.app
```

### Redis Integration

**Docker Container**: `n8n-redis`
- **Network**: Docker bridge (172.21.0.0/16)
- **IP Address**: 172.21.0.4
- **Port**: 6379
- **Password**: None (internal network only)
- **Purpose**: Conversation state tracking only (NOT chunk buffering)

### Testing Production Deployment

```bash
# Health check
curl https://api.ella-ai-care.com/health

# Test language endpoint (iOS app requirement)
curl -X PATCH https://api.ella-ai-care.com/v1/users/language \
  -H "Content-Type: application/json" \
  -d '{"language": "en"}'

# Check WebSocket endpoint (requires device)
# iOS app connects to: wss://api.ella-ai-care.com/v4/listen
```

### Real-Time Data Flow

**Current Architecture** (October 30, 2025):
```
iOS Device → wss://api.ella-ai-care.com/v4/listen → Deepgram API → Transcription
                                                       ↓
                                    [600ms chunk processing in-memory buffer]
                                                       ↓
                                                  Firestore DB
                                                   (on session end)
```

**Buffering System**:
- **Type**: In-memory Python lists (NOT Redis)
- **Chunk Interval**: 600ms (see `routers/transcribe.py` line 858-869)
- **Buffer**: `realtime_segment_buffers` list
- **Webhook**: Optional 1-second batching to external webhook
- **Firestore**: Final storage after 2-minute timeout or manual stop

**See**: `docs/LETTA_INTEGRATION_ARCHITECTURE.md` for 2-way conversation design

### Deployment History

**October 30, 2025 - Session 2**:
- ✅ Fixed missing Firestore composite indexes (conversations, memories)
- ✅ Configured GCS bucket permissions for audio storage
- ✅ Added Redis configuration for n8n integration
- ✅ Enabled Deepgram logging for debugging
- ✅ Verified real-time chunk processing working
- ✅ Documented Letta integration architecture (3 options)
- ✅ iOS app successfully connecting and transcribing

**October 28, 2025 - Initial Deployment**:
- ✅ VPS provisioned and configured
- ✅ Backend deployed with systemd service
- ✅ SSL certificate configured (Let's Encrypt)
- ✅ Firebase credentials deployed
- ✅ Environment variables configured

### Known Issues & Solutions

1. **Memories Disappearing from App**:
   - **Cause**: Missing Firestore composite index for memories collection
   - **Solution**: Index created on October 30, 2025
   - **Status**: ✅ Resolved (pending verification)

2. **GCS Bucket Permission Errors**:
   - **Cause**: Service account missing Storage Object Admin role
   - **Solution**: Granted via gsutil iam command
   - **Status**: ✅ Resolved

3. **Redis Connection Errors**:
   - **Cause**: Missing Redis configuration in .env
   - **Solution**: Added Redis config for n8n-redis container
   - **Status**: ✅ Resolved

4. **Deepgram Logging Silent**:
   - **Cause**: Print statements commented out in streaming.py
   - **Solution**: Uncommented lines 268, 270, 273
   - **Status**: ✅ Resolved

### Future Enhancements

See `docs/LETTA_INTEGRATION_ARCHITECTURE.md` for:
- 2-way conversation capabilities
- Real-time response system (under 2-second latency)
- Postgres agent lookup integration
- Fast LLM alert scanning
- Redis chunk aggregation with backpressure handling

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+ with virtual environment
- Homebrew (macOS) with `opus` and `ffmpeg` installed
- Firebase project credentials (`google-credentials.json`)
- API keys for: Deepgram, OpenAI, Pinecone, Hugging Face

### Start Backend Server
```bash
cd backend
source venv/bin/activate
python start_server.py

# Server runs on: http://localhost:8000
# API docs available at: http://localhost:8000/docs
```

### Run Test Simulation
```bash
cd backend
source venv/bin/activate
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav
```

---

## 🏗️ Architecture Overview

### Audio Processing Pipeline

```
OMI Device → Opus Encoding → WebSocket → Backend → Deepgram API
                                            ↓
                                      Transcription
                                            ↓
                                      Firestore DB
```

### Key Components

1. **WebSocket Endpoint**: `/v4/listen`
   - Accepts real-time audio streams from OMI devices
   - Parameters: `uid`, `language`, `sample_rate`, `codec`, `channels`
   - Authentication: Firebase JWT or ADMIN_KEY (LOCAL_DEVELOPMENT mode)

2. **Audio Processing**:
   - Silero VAD: Voice activity detection (17MB model)
   - Deepgram: Speech-to-text transcription (cloud API)
   - PyAnnote: Speaker diarization for multi-speaker identification (17GB models)

3. **Database**: Firebase Firestore
   - Collections: `users`, `conversations`, `memories`
   - Real-time queries with composite indexes
   - Security rules (currently OPEN for development)

4. **Authentication**:
   - Production: Firebase Authentication with JWT tokens
   - Development: `LOCAL_DEVELOPMENT=true` bypasses auth (uses UID '123')

---

## 🔧 Environment Configuration

### Required Environment Variables

Create `.env` file in `backend/` directory:

```bash
# API Keys
DEEPGRAM_API_KEY=your_deepgram_key
OPENAI_API_KEY=your_openai_key
PINECONE_API_KEY=your_pinecone_key
HUGGINGFACE_TOKEN=your_huggingface_token
GITHUB_TOKEN=your_github_token (for model downloads)

# Firebase
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=./google-credentials.json

# Development Settings
LOCAL_DEVELOPMENT=true
ADMIN_KEY=dev_testing_key_12345

# Optional Services
TYPESENSE_HOST=localhost
TYPESENSE_HOST_PORT=8108
TYPESENSE_API_KEY=dummy_key_for_dev
```

### Firebase Setup

1. **Enable Firestore API**: https://console.developers.google.com/apis/api/firestore.googleapis.com/overview?project=omi-dev-ca005
2. **Create Firestore Database**: Choose "Firestore Native Mode"
3. **Create Test User**: `python create_test_user.py`
4. **Create Composite Index** (if needed): Backend will provide link in error message

---

## 🧪 Testing & Verification

### Test Scripts Available

1. **Device Simulator**: `test_omi_device_simulation.py`
   - Simulates OMI device sending audio via WebSocket
   - Supports loading WAV files or generating synthetic audio
   - Real-time frame streaming with Opus encoding

2. **Model Downloaders**:
   - `download_models.py` - PyAnnote speaker diarization (~17GB)
   - `download_whisper_models.py` - WhisperX models (~2GB)

3. **User Setup**: `create_test_user.py`
   - Creates test user in Firestore with UID '123'
   - Gives 10,000 transcription credits for testing

### Verifying Test Output

#### 1. **Console Output** (Immediate Feedback)
When running `test_omi_device_simulation.py`, you'll see:

```
🎧 OMI Device Simulator
📂 Loading audio from test_audio/pyannote_sample.wav...
✅ Encoded 1500 Opus frames
✅ Connected!

🎵 Sending audio frames...
   Sent 50/1500 frames (1.0s elapsed)
   Sent 100/1500 frames (2.1s elapsed)

🗣️  [0.0s - 4.8s] Speaker 0: Hello? Hello? Oh, hello...
🗣️  [0.0s - 12.5s] Speaker 0: This is Diane in New Jersey...

✅ Test completed!
```

**What This Shows**:
- Audio loading and encoding working
- WebSocket connection established
- Real-time transcription from Deepgram
- Progressive transcript updates

#### 2. **Backend Server Logs**
Check the terminal where `start_server.py` is running:

```bash
INFO:     ('127.0.0.1', 63801) - "WebSocket /v4/listen?uid=123..." [accepted]
INFO:     connection open
INFO:     connection closed
```

**What to Look For**:
- `[accepted]` = WebSocket handshake successful
- No SSL errors (should see clean connection open/close)
- No Python exceptions or tracebacks

#### 3. **Firestore Database** (Verify Memories Created)

**Via Firebase Console**:
1. Visit: https://console.firebase.google.com/project/omi-dev-ca005/firestore/data
2. Navigate to `conversations` collection
3. Look for documents with `uid: 123` (test user)
4. Check fields:
   - `transcript`: Full conversation text
   - `created_at`: Timestamp
   - `status`: Should be 'completed' or 'processing'
   - `language`: 'en'

**Via Python Script** (Quick Check):
```python
from google.cloud import firestore
import os

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = './google-credentials.json'
db = firestore.Client()

# Get recent conversations for test user
conversations = db.collection('conversations') \
    .where('uid', '==', '123') \
    .order_by('created_at', direction=firestore.Query.DESCENDING) \
    .limit(5) \
    .stream()

for conv in conversations:
    data = conv.to_dict()
    print(f"ID: {conv.id}")
    print(f"Created: {data.get('created_at')}")
    print(f"Transcript: {data.get('transcript', '')[:100]}...")
    print("---")
```

**Via curl** (API Endpoint):
```bash
# If backend implements GET endpoint for conversations
curl "http://localhost:8000/api/v1/conversations?uid=123&limit=5"
```

#### 4. **Deepgram Dashboard** (API Usage Verification)
- Visit: https://console.deepgram.com/
- Check "Usage" tab to see API requests
- Verify requests correspond to test times
- Should see ~30 seconds of audio processed

#### 5. **Model Cache Verification**
```bash
# Check ML models are cached (should be ~17GB)
du -sh ~/.cache/huggingface/
du -sh ~/.cache/torch/

# List cached models
ls -lh ~/.cache/huggingface/hub/
```

---

## 📁 Project Structure

```
backend/
├── CLAUDE.md                          # This file
├── .env                               # Environment variables (gitignored)
├── google-credentials.json            # Firebase credentials (gitignored)
├── main.py                            # FastAPI application entry point
├── start_server.py                    # Helper script with env setup
├── requirements.txt                   # Python dependencies
├── venv/                              # Virtual environment (gitignored)
│
├── routers/                           # API endpoints
│   ├── transcribe.py                  # WebSocket /v4/listen endpoint
│   └── ...
│
├── database/                          # Database utilities
│   ├── users.py                       # User validation functions
│   └── ...
│
├── utils/                             # Utilities
│   └── other/
│       └── endpoints.py               # Authentication helpers
│
├── test_audio/                        # Test audio files (gitignored)
│   ├── pyannote_sample.wav            # 30s, 16kHz mono
│   ├── silero_test.wav                # 60s, 16kHz mono
│   └── ...
│
├── docs/                              # Documentation
│   ├── SESSION_SUMMARY.md             # Complete session overview
│   ├── SECURITY_HIPAA_CHECKLIST.md    # Production security requirements
│   ├── README_TESTING.md              # Comprehensive testing guide
│   ├── QUICK_TEST.md                  # Quick start testing
│   └── TESTING_SETUP_NOTE.md          # Troubleshooting notes
│
└── [Test Scripts]
    ├── test_omi_device_simulation.py  # Device simulator
    ├── create_test_user.py            # Firestore user setup
    ├── download_models.py             # PyAnnote downloader
    └── download_whisper_models.py     # WhisperX downloader
```

---

## 🔍 Common Operations

### Check Backend Health
```bash
curl http://localhost:8000/health
# Should return 200 OK
```

### List API Endpoints
```bash
# Visit Swagger UI
open http://localhost:8000/docs

# Or ReDoc
open http://localhost:8000/redoc
```

### Test Different Audio Files
```bash
# Use PyAnnote sample (30s, best for diarization)
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav

# Use Silero test (60s, full pipeline)
python test_omi_device_simulation.py --audio-file test_audio/silero_test.wav

# Generate synthetic audio (10s default)
python test_omi_device_simulation.py --duration 10
```

### Override Test User
```bash
# Use different UID
python test_omi_device_simulation.py --uid your-firebase-uid --audio-file test_audio/pyannote_sample.wav
```

---

## 🐛 Troubleshooting

### Opus Library Not Found
```bash
# macOS: Install via Homebrew
brew install opus

# Verify installation
ls -la /opt/homebrew/opt/opus/lib/

# The start_server.py script automatically sets DYLD_LIBRARY_PATH
```

### SSL Certificate Errors
```bash
# Verify certifi is installed
pip install --upgrade certifi

# Check SSL_CERT_FILE is set (done by start_server.py)
echo $SSL_CERT_FILE
```

### Firestore Index Missing
```bash
# Error message will include a direct link like:
# https://console.firebase.google.com/v1/r/project/omi-dev-ca005/firestore/indexes?create_composite=...

# Click the link, Firebase will auto-populate the index configuration
# Click "Create Index" and wait 1-5 minutes
```

### WebSocket Connection Failed
```bash
# Check backend is running
curl http://localhost:8000/health

# Check test user exists in Firestore
python create_test_user.py

# Verify LOCAL_DEVELOPMENT=true in .env
grep LOCAL_DEVELOPMENT .env
```

### No Transcription Output
```bash
# Verify Deepgram API key is valid
grep DEEPGRAM_API_KEY .env

# Check Deepgram API usage dashboard
# https://console.deepgram.com/

# Try with synthetic audio to isolate issues
python test_omi_device_simulation.py --duration 5
```

---

## 📊 ML Models & Storage

### Models Cached Locally (~17GB Total)

1. **Silero VAD** (~17MB)
   - Location: `~/.cache/torch/hub/snakers4_silero-vad_master/`
   - Purpose: Voice activity detection
   - Download: Automatic on first backend start

2. **PyAnnote Speaker Diarization** (~17GB)
   - Location: `~/.cache/huggingface/hub/models--pyannote--speaker-diarization-3.1/`
   - Purpose: Multi-speaker identification
   - Download: `python download_models.py`
   - Requires: Hugging Face token with model access granted

3. **WhisperX** (~2GB)
   - Location: `~/.cache/huggingface/` (various models)
   - Purpose: Local speech-to-text (HIPAA compliance alternative)
   - Download: `python download_whisper_models.py`
   - Status: Downloaded but not yet integrated (future work)

### Why Local Models Matter

- **Offline Development**: Work on transcription without internet
- **HIPAA Compliance**: Process PHI locally without cloud providers
- **Cost Reduction**: Avoid per-minute Deepgram charges
- **Latency**: Faster processing without API round-trips

---

## 🔐 Security Notes

### Development vs Production

**Current Setup** (Development):
- ✅ `LOCAL_DEVELOPMENT=true` - Bypasses Firebase auth
- ✅ Firestore rules: OPEN (30-day temporary setting)
- ✅ CORS: `*` (accepts all origins)
- ✅ ADMIN_KEY authentication bypass

**Before Production** (See `docs/SECURITY_HIPAA_CHECKLIST.md`):
- ❌ Set `LOCAL_DEVELOPMENT=false`
- ❌ Restrict Firestore security rules (user-scoped access only)
- ❌ Configure CORS allowlist (specific domains)
- ❌ Rotate all API keys to production versions
- ❌ Enable audit logging
- ❌ Sign Business Associate Agreements (HIPAA)
- ❌ Configure HTTPS/TLS (minimum TLS 1.3)
- ❌ Set up automated backups

**Critical Timeline**: Firestore security rules must be changed within **7 days** of database creation (before 30-day open period expires).

---

## 🚀 Future Development

### Planned Improvements

1. **Letta Integration**:
   - Direct integration to replace n8n workflow hop
   - Conversation context management
   - Long-term memory storage

2. **Local WhisperX**:
   - Replace Deepgram with local processing
   - Full HIPAA compliance (no PHI leaves device)
   - Cost savings on transcription

3. **Speaker Diarization**:
   - Enable PyAnnote for multi-speaker detection
   - Assign speaker IDs to transcript segments
   - Voice profile learning

4. **M1 iMac Deployment**:
   - 24/7 backend operation
   - Automatic startup on boot
   - Monitoring and alerting

5. **Redis Integration**:
   - Conversation buffer/cache
   - Real-time state management
   - Multi-device synchronization

---

## 📚 Additional Documentation

- **Session Summary**: `docs/SESSION_SUMMARY.md` - Complete setup history
- **Testing Guide**: `docs/README_TESTING.md` - Comprehensive testing instructions
- **Quick Start**: `docs/QUICK_TEST.md` - Minimal testing steps
- **Security**: `docs/SECURITY_HIPAA_CHECKLIST.md` - Production requirements
- **Troubleshooting**: `docs/TESTING_SETUP_NOTE.md` - Common issues and solutions

---

## 🤝 Contributing

### Development Workflow

1. **Create Feature Branch**: `git checkout -b feature/your-feature-name`
2. **Make Changes**: Edit code, add tests
3. **Test Locally**: Run `test_omi_device_simulation.py`
4. **Verify Backend**: Check server logs for errors
5. **Commit**: `git commit -m "feat: description"`
6. **Push**: `git push origin feature/your-feature-name`

### Code Quality

```bash
# Format code
black backend/

# Sort imports
isort backend/

# Type checking (if configured)
mypy backend/
```

---

## 💡 Tips for New Developers

### 1. Start Simple
Run the basic test first to verify everything works:
```bash
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav
```

### 2. Watch Backend Logs
Keep the `start_server.py` terminal visible to see real-time processing

### 3. Use API Documentation
FastAPI auto-generates docs: http://localhost:8000/docs
- Test endpoints interactively
- See request/response schemas
- Try WebSocket connections

### 4. Check Firestore Console
Real-time view of database changes as tests run:
https://console.firebase.google.com/project/omi-dev-ca005/firestore/data

### 5. Test Incrementally
- First: Verify server starts (`curl http://localhost:8000/health`)
- Second: Test with synthetic audio (`python test_omi_device_simulation.py --duration 5`)
- Third: Test with real audio files
- Fourth: Test with real OMI device

### 6. Debug with Print Statements
The test script shows detailed progress - modify it to add more debugging output if needed

### 7. Read the Session Summary
`docs/SESSION_SUMMARY.md` has the complete history of what was built and why

---

## 📞 Support & Resources

- **Firebase Console**: https://console.firebase.google.com/project/omi-dev-ca005
- **Deepgram Dashboard**: https://console.deepgram.com/
- **Hugging Face Models**: https://huggingface.co/pyannote
- **FastAPI Docs**: https://fastapi.tiangolo.com/

---

**Last Updated**: November 10, 2025
**Maintained By**: Development Team
**Status**: ✅ Production-ready backend infrastructure with Edge ASR + Ella AI integration

---

## 📝 **Git Commit Guidelines (Backend)**

### **Commit Message Examples**
```bash
# Features
git commit -m "feat(tts): implement OpenAI TTS provider with caching"
git commit -m "feat(api): add /admin/lookup-agent endpoint"
git commit -m "feat(vad): enable Silero VAD for cost reduction"

# Fixes
git commit -m "fix(tts): resolve lazy initialization for Firestore client"
git commit -m "fix(api): correct import order in main.py router registration"

# Documentation
git commit -m "docs(deployment): add TTS API deployment guide"
git commit -m "docs(setup): update VPS configuration instructions"

# Infrastructure
git commit -m "chore(deploy): update systemd service configuration"
git commit -m "chore(deps): update FastAPI to v0.104.0"
```

### **Files You Own**
Backend developers commit:
- `backend/**/*.py` - All Python backend code
- `backend/docs/**` - Backend documentation
- `backend/.env.example` - Example env file (never commit actual .env)
- `backend/requirements.txt` - Python dependencies

### **Before Committing Backend Code**
```bash
# Run tests
cd /Users/greg/repos/omi/backend
pytest

# Check code quality (if configured)
black . --check
flake8

# Review changes
git status
git diff

# Commit
git add backend/path/to/files
git commit -m "feat(scope): description"
```

### **Current Branch**: `feature/backend-infrastructure`

See root CLAUDE.md for general git guidelines.
