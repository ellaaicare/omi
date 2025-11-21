# Feature Requirements Analysis - OMI Backend

**Date**: October 30, 2025
**Context**: Evaluating need for advanced features for necklace audio processing and 2-way conversation

---

## ✅ **CURRENT STATE - WORKING FEATURES**

### What's Confirmed Working:

1. **Real-Time Transcription** ✅
   - Deepgram Nova 3 transcribing successfully
   - 91-second conversation captured with high accuracy
   - WebSocket streaming functioning correctly
   - Transcript quality excellent (see logs - Apple/Starlink discussion)

2. **Memory System** ✅ (Partially)
   - Logs show: `get_memories 1` - App received a memory
   - Background processing: `Saving 2 memories for conversation`
   - **User confirmed**: "I do now see an earlier memory!"
   - Async processing working (3-4 minute delay for background tasks)

3. **Conversation Filtering** ✅
   - Intelligent discard logic filtering test/noise
   - LLM-based content evaluation
   - Saves storage and focuses on meaningful content

### What's NOT Working:

1. **Firestore Persistence** ❌
   - Conversations not visible in Firestore queries
   - Memories not visible in Firestore queries
   - **BUT** app IS receiving memories somehow (cache? API layer?)
   - Needs investigation of write path

2. **App Loading** ❌
   - "App timing out" when querying conversations/memories
   - Likely due to missing Firestore indexes
   - Need to create: `uid` + `created_at` index

---

## 🎯 **FEATURE REQUIREMENTS FOR YOUR USE CASES**

### Use Case 1: Necklace with Chaotic/Noisy Audio

**Your Question**: "Will these other apps help with decoding chaotic audio from necklace?"

**Answer**: The "apps" in OMI are **post-processing plugins**, NOT audio enhancement tools. They won't help with audio quality.

**What WILL Help with Chaotic/Noisy Audio**:

#### **1. Voice Activity Detection (VAD) - Silero**
**Status**: Not currently enabled
**Purpose**: Filters out silence and non-speech audio
**Benefit for Necklace**:
- ✅ Reduces noise from clothing rustling, wind, ambient sounds
- ✅ Only processes segments with actual speech
- ✅ Saves transcription costs (Deepgram charges per audio second)
- ✅ Improves transcript quality by ignoring non-speech

**Recommendation**: ⚠️ **ENABLE THIS** - Critical for necklace use

**How to Enable**:
```python
# In routers/transcribe.py - Already has VAD code, just needs to be activated
# Check if VAD model downloaded: ~/.cache/torch/hub/snakers4_silero-vad_master/
```

#### **2. Speaker Diarization - PyAnnote**
**Status**: Downloaded but not enabled
**Purpose**: Identifies different speakers in conversation
**Benefit for Necklace**:
- ✅ Distinguishes between YOU and other people
- ✅ Multi-person conversations properly attributed
- ✅ Better context for memory extraction

**Requirements**:
- 17GB models (already downloaded from earlier session)
- Significant compute (may slow processing)
- Best for multi-person conversations

**Recommendation**: ⚠️ **OPTIONAL** - Enable only if you have multi-person conversations

#### **3. Audio Codec Optimization**
**Current**: Opus codec (good compression, decent quality)
**Alternatives**:
- PCM16: Higher quality, larger bandwidth
- Opus with different bitrate settings

**For Noisy Necklace Environment**:
- Use higher Opus bitrate (24kbps vs current 16kbps)
- Or switch to PCM16 if bandwidth allows
- Configure in iOS app before sending to backend

#### **4. Deepgram Model Selection**
**Current**: `nova-3-general`
**Alternatives**:
- `nova-2-enhanced` - Better noise handling
- `nova-3-meeting` - Optimized for multi-speaker
- `nova-3-phonecall` - Optimized for telephony (coming soon for your use case!)

**Recommendation**: Test `nova-2-enhanced` or `nova-3-meeting` for better noisy audio handling

### What OMI "Apps" Actually Do (Post-Processing):

**Apps are NOT for audio quality** - they process the TRANSCRIPT after it's created:

1. **Summarization Apps**: Create structured summaries
2. **Action Item Apps**: Extract tasks from conversations
3. **Trend Analysis Apps**: Identify patterns over time
4. **Integration Apps**: Send data to external services (n8n, Letta, etc.)

**For Chaotic Audio**: These apps won't help - you need **pre-processing** (VAD, better codec, better STT model)

---

### Use Case 2: 2-Way Phone Conversation System

**Your Question**: "Will they help for regular 2-way conversation if we get a phone call system implemented?"

**Answer**: This is a **DIFFERENT ARCHITECTURE** than the current necklace setup!

#### Current Architecture (Necklace):
```
Necklace → Opus Audio → WebSocket → Deepgram → Transcript → Firestore
(One-way: Device sends, backend processes, app displays)
```

#### Required Architecture (2-Way Phone):
```
Phone Call ↔ WebSocket ↔ Backend ↔ Letta Agent
    ↑                                    ↓
    |                               Response Generation
    |                                    ↓
    └─────────── Audio Response ─────────┘

(Two-way: Real-time bidirectional audio + LLM responses)
```

#### Key Differences for 2-Way Phone System:

**1. Real-Time Response Generation** (NOT batch processing)
- Current: Processes conversation AFTER it ends
- Needed: Responds DURING the conversation
- Latency budget: <2 seconds total (Deepgram + Letta + TTS)

**2. Text-to-Speech (TTS)** - NEW REQUIREMENT
- Need to convert Letta's text response to audio
- Options:
  - ElevenLabs (realistic, 300-500ms latency)
  - OpenAI TTS (fast, 200-300ms latency)
  - Deepgram Aura (very fast, 100-200ms latency)

**3. Bidirectional Audio Streaming**
- Current: One-way (device → backend)
- Needed: Two-way (device ↔ backend)
- WebSocket must support BOTH send and receive simultaneously

**4. Conversation State Management**
- Current: Stateless processing
- Needed: Maintain context across turns
- Letta handles this (session-based conversation)

#### What You WILL Need for 2-Way Phone:

**Backend Changes** (from Letta Integration Architecture doc):

```python
# NEW: Real-time response pipeline (Option 3 - Hybrid from architecture doc)

async def stream_transcript_process():
    while websocket_active:
        await asyncio.sleep(0.6)  # 600ms chunk processing

        # 1. Get transcript chunk
        chunk_text = " ".join(seg['text'] for seg in realtime_segment_buffers)

        # 2. Fast LLM alert scanner (<200ms)
        needs_response, urgency = await scanner.needs_immediate_response(chunk_text)

        if needs_response:
            # 3. Query Letta for response (1-2s)
            letta_response = await letta_client.send_message(
                agent_id=agent_id,
                message=chunk_text
            )

            if letta_response['text'] != "NA":
                # 4. Convert to speech (300ms)
                audio_response = await tts_client.generate_audio(letta_response['text'])

                # 5. Send audio back to phone via WebSocket
                await websocket.send_bytes(audio_response)
```

**Total Latency Budget**:
- Deepgram transcription: 600ms
- Fast LLM scan: 200ms
- Letta response: 1500ms
- TTS generation: 300ms
- **TOTAL: ~2.6 seconds** ✅ Acceptable for phone call

**What OMI Apps CAN Help With (Phone Use Case)**:

1. **Action Item Extraction**: During call, extract tasks
   - Example: "Call Mom at 3pm" → Auto-create reminder

2. **Meeting Summarization**: After call ends
   - Generate call summary, key points, decisions

3. **CRM Integration**: Send call data to external systems
   - Automatically log calls in Salesforce, HubSpot, etc.

4. **Trend Analysis**: Track call patterns over time
   - Who you talk to most, common topics, etc.

**What Apps WON'T Help With**:
- ❌ Real-time audio quality
- ❌ Response generation speed
- ❌ Bidirectional streaming
- ❌ TTS quality

---

## 📋 **RECOMMENDED ARCHITECTURE FOR YOUR GOALS**

### Phase 1: Necklace Audio Quality (NOW)

**Priority 1: Enable VAD** ⚠️
- Filters non-speech audio
- Critical for noisy necklace environment
- Already in codebase, just needs activation

**Priority 2: Test Better Deepgram Models**
- Switch to `nova-2-enhanced` for noise handling
- Or `nova-3-meeting` for multi-speaker

**Priority 3: Fix Firestore Persistence** 🔧
- Create missing indexes
- Verify write path
- Enable app to load conversations/memories

### Phase 2: Letta Integration (NEXT - See Architecture Doc)

**Option 3 (Hybrid)** - Recommended from `LETTA_INTEGRATION_ARCHITECTURE.md`:
- Fast alert scanner in backend (<1s alerts)
- Full Letta processing via n8n (3-4s thoughtful responses)
- Best balance for 2-way conversation

**Components Needed**:
1. ✅ Postgres: user_id → agent_id mapping (in your n8n)
2. ✅ Fast LLM scanner: GPT-4o-mini for quick alerts (NEW - add to backend)
3. ✅ Redis aggregation: Chunk buffering (in your n8n)
4. ✅ Letta agent: Main conversation partner (existing)
5. ❌ TTS engine: Convert Letta text → audio (NEW - add to backend)

### Phase 3: 2-Way Phone System (FUTURE)

**New Requirements** (PRD Coming Soon):
1. **Bidirectional WebSocket** - Send AND receive audio
2. **TTS Integration** - Deepgram Aura or ElevenLabs
3. **Call State Management** - Track active calls, participants
4. **Phone Number Integration** - Twilio, Plivo, or VoIP provider
5. **Real-Time Latency Optimization** - Target <2s total response time

**Reusable from Current System**:
- ✅ Deepgram STT (already working)
- ✅ WebSocket infrastructure (needs bidirectional upgrade)
- ✅ Letta agent integration (from Phase 2)
- ✅ Conversation storage (Firestore, once fixed)

---

## 🔧 **IMMEDIATE ACTION ITEMS**

### 1. Enable VAD (Voice Activity Detection)

**Why**: Critical for noisy necklace audio
**How**:
```bash
# Check if VAD model downloaded
ssh root@100.101.168.91 "ls -lh ~/.cache/torch/hub/ | grep silero"

# If not downloaded, backend will auto-download on first use
# Enable in transcribe.py (already has code, just needs config flag)
```

### 2. Fix Firestore Composite Index

**Create Index** (Click this link):
```
https://console.firebase.google.com/v1/r/project/omi-dev-ca005/firestore/indexes?create_composite=ClNwcm9qZWN0cy9vbWktZGV2LWNhMDA1L2RhdGFiYXNlcy8oZGVmYXVsdCkvY29sbGVjdGlvbkdyb3Vwcy9jb252ZXJzYXRpb25zL2luZGV4ZXMvXhABGgcKA3VpZBABGg4KCmNyZWF0ZWRfYXQQAhoMCghfX25hbWVfXxAC
```

**Wait**: 5-10 minutes for index to build
**Then**: App should load conversations without timeout

### 3. Test Different Deepgram Models

**Edit**: `/root/omi/backend/utils/stt/streaming.py`
**Change**:
```python
# Current
model="nova-3-general"

# Test for noisy audio
model="nova-2-enhanced"  # OR
model="nova-3-meeting"   # For multi-speaker
```

**Restart backend and test with necklace**

### 4. Investigate Firestore Write Path

**Add Debugging** to `database/conversations.py`:
```python
def upsert_conversation(uid: str, conversation_data: dict):
    try:
        print(f"🔍 ATTEMPTING TO WRITE CONVERSATION: {conversation_data.get('id')}")
        # ... existing code ...
        print(f"✅ CONVERSATION WRITTEN SUCCESSFULLY")
    except Exception as e:
        print(f"❌ CONVERSATION WRITE FAILED: {e}")
        raise
```

**Monitor logs** on next test

---

## 📊 **FEATURE MATRIX: What Helps What**

| Feature | Noisy Necklace Audio | 2-Way Phone Call | Memory/Storage |
|---------|---------------------|------------------|----------------|
| **VAD (Silero)** | ✅ Critical | ✅ Helpful | ✅ Reduces noise |
| **Speaker Diarization** | ⚠️ Optional | ✅ Very helpful | ✅ Better context |
| **Better Deepgram Model** | ✅ Very helpful | ✅ Helpful | ✅ Better transcripts |
| **Higher Audio Bitrate** | ✅ Helpful | ✅ Critical | - |
| **OMI Apps (Plugins)** | ❌ No help | ⚠️ Post-call only | ✅ Helpful |
| **Letta Integration** | ❌ No help | ✅ Critical | ✅ Helpful |
| **TTS Engine** | ❌ No help | ✅ Critical | - |
| **Fast LLM Scanner** | ❌ No help | ✅ Critical | - |
| **Firestore Indexes** | - | - | ✅ Critical |

---

## 🎯 **ANSWERS TO YOUR QUESTIONS**

### Q1: "Will these other apps help with decoding chaotic audio from necklace?"

**A**: **NO** - OMI "apps" are post-processing plugins that work on the TRANSCRIPT, not the audio.

**What WILL help**:
- ✅ **VAD (Silero)** - Filters non-speech
- ✅ **Better Deepgram model** - Handles noise better
- ✅ **Higher audio bitrate** - More data for STT
- ❌ Not OMI apps/plugins

### Q2: "Will they help for regular 2-way conversation if we get a phone call system implemented?"

**A**: **PARTIALLY** - Apps help with POST-CALL processing (summaries, action items), but NOT real-time response generation.

**What you ACTUALLY need for 2-way phone**:
- ✅ **Letta integration** (from architecture doc)
- ✅ **TTS engine** (Deepgram Aura, ElevenLabs, OpenAI)
- ✅ **Bidirectional WebSocket** (upgrade current implementation)
- ✅ **Fast LLM scanner** (quick alert detection)
- ⚠️ **OMI apps** (only for post-call processing)

### Q3: "Is the higher level flow needed?"

**A**: **DEPENDS ON USE CASE**:

**For Necklace (One-Way Recording)**:
- Current simple flow is FINE
- Just need: VAD + Better STT + Firestore fixes

**For Phone (2-Way Conversation)**:
- Need NEW architecture (see Letta Integration doc)
- Requires: Real-time response pipeline
- Letta + Fast LLM + TTS + Bidirectional streaming

---

## 📞 **PREPARING FOR YOUR 2-WAY AUDIO PRD**

When you send the PRD for 2-way audio API, please include:

### Required Information:

1. **Use Case Details**:
   - Phone calls? Video calls? Live conversations?
   - Expected call duration (5 min? 30 min? hours?)
   - Number of concurrent calls (1? 10? 100?)

2. **Response Requirements**:
   - Max acceptable latency (2s? 5s?)
   - Response style (short? conversational? detailed?)
   - Can agent say "I don't know" or must always respond?

3. **Audio Quality**:
   - Phone quality (8kHz)? HD audio (16kHz+)?
   - Background noise expected?
   - Multi-speaker or 1-on-1?

4. **Integration Points**:
   - Twilio? Plivo? Custom VoIP?
   - SIP trunking? WebRTC?
   - Mobile app? Web browser? Phone number?

5. **Letta Agent Behavior**:
   - Interrupt detection needed?
   - Context window size?
   - Memory persistence across calls?

### We're Ready to Build:

Based on the Letta Integration Architecture doc, we have 3 options ready:
- **Option 1**: Webhook → n8n → Letta (easiest, 3-4s latency)
- **Option 2**: Direct backend integration (fastest, 2-3s latency)
- **Option 3**: Hybrid (recommended, <1s alerts + 3-4s full responses)

---

## 🎬 **NEXT STEPS**

1. **YOU**: Create Firestore index (click link above)
2. **ME**: Enable VAD in backend
3. **ME**: Test different Deepgram models
4. **ME**: Debug Firestore write path
5. **YOU**: Test with necklace - longer, meaningful conversation
6. **YOU**: Send 2-way audio PRD when ready

**Status**: Backend foundation solid, need VAD + Firestore fixes, then ready for Letta integration

---

**Document Version**: 1.0
**Last Updated**: October 30, 2025
**Next Update**: After Firestore index creation and VAD enablement
