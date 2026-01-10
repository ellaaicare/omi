# OMI/Ella AI Backend Architecture Overview

**Last Updated**: December 23, 2025
**Version**: 2.1
**Author**: Backend Integration Developer
**Status**: Production

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Core Data Flows](#2-core-data-flows)
3. [External Integrations](#3-external-integrations)
4. [n8n Webhook Endpoints](#4-n8n-webhook-endpoints)
5. [Backend API Endpoints](#5-backend-api-endpoints)
6. [Infrastructure](#6-infrastructure)
7. [Database Schema](#7-database-schema)
8. [Critical Dependencies](#8-critical-dependencies)
9. [Current Architecture Challenges](#9-current-architecture-challenges)
10. [File Structure](#10-file-structure)

---

## 1. System Overview

### What is OMI?

**OMI** (Open Memory Interface) is a **wearable AI device** - a necklace with a microphone that continuously records ambient conversations and processes them into structured memories. Think of it as a "memory backup" for your brain.

**Hardware**: Bluetooth Low Energy (BLE) device with:
- Built-in microphone
- Opus audio codec (16kHz, mono)
- Battery-powered
- Speaker (for voice interactions)
- Haptic feedback

**Purpose**: Capture every conversation, meeting, idea, and moment - then intelligently extract what matters.

### What is Ella AI?

**Ella AI** is the **AI companion layer** built on top of OMI infrastructure. It transforms raw transcripts into:
- **Memories**: Long-term facts about the user
- **Summaries**: Structured conversation recaps
- **Action Items**: Extracted tasks and TODOs
- **Emotional Support**: Caring responses to user needs
- **Voice Interactions**: 2-way voice calls with AI

**Powered by**: Letta (formerly MemGPT) - an agent framework for long-term memory and context management.

### How They Work Together

```
OMI Device (Hardware)
    ↓ [Audio via Bluetooth]
iOS/Android App
    ↓ [Audio frames via WebSocket]
OMI Backend (This Repository)
    ↓ [Transcripts via webhooks]
Ella AI / Letta Agents (n8n workflows)
    ↓ [Processed results via callbacks]
OMI Backend (Storage)
    ↓ [Push notifications]
iOS/Android App (Display)
```

**Key Insight**: The backend acts as a **thin orchestration layer** - it handles real-time audio processing but delegates AI decision-making to Ella's agent cluster.

---

## 2. Core Data Flows

### A. Ambient Listening Flow (OMI Device → Memories)

**Purpose**: Background audio capture without user interaction
**Latency**: 2-10 seconds for real-time transcription
**Endpoint**: `wss://api.ella-ai-care.com/v4/listen`

```
┌─────────────────────────────────────────────────────────────────┐
│                     AMBIENT LISTENING FLOW                       │
└─────────────────────────────────────────────────────────────────┘

1. OMI DEVICE
   ├─ Microphone captures audio
   ├─ Opus encoder (16kHz mono)
   └─ BLE → iOS app
         ↓
2. iOS APP
   ├─ Receives Opus frames
   ├─ Opens WebSocket: wss://api.ella-ai-care.com/v4/listen
   ├─ Sends: { uid, language, codec, sample_rate }
   └─ Streams audio bytes
         ↓
3. BACKEND (routers/transcribe.py)
   ├─ Accepts WebSocket connection
   ├─ Validates user credits
   ├─ Decodes Opus → PCM16
   ├─ Sends to Deepgram STT API (or Soniox/Speechmatics)
   └─ Receives streaming transcription
         ↓
4. TRANSCRIPTION PROCESSING (600ms buffer)
   ├─ Buffers transcript segments in-memory
   ├─ Every 600ms: processes buffered segments
   ├─ Creates TranscriptSegment objects
   │   ├─ text: "Hello, how are you?"
   │   ├─ speaker: "SPEAKER_00"
   │   ├─ start: 0.0 (seconds)
   │   ├─ end: 2.5
   │   └─ source: "deepgram"
   ├─ Combines segments (handles overlaps)
   └─ Stores in Redis (temporary conversation buffer)
         ↓
5. CONVERSATION FINALIZATION (2-minute timeout or WebSocket close)
   ├─ Triggers conversation processing
   ├─ Builds full transcript from segments
   ├─ Calls n8n scanner webhook (fire-and-forget, 1s timeout)
   │   POST https://n8n.ella-ai-care.com/webhook/scanner-agent
   │   { uid, conversation_id, segments[] }
   ├─ Calls n8n summary webhook (30s timeout)
   │   POST https://n8n.ella-ai-care.com/webhook/summary-agent
   │   Returns: { title, overview, emoji, category, action_items, events }
   ├─ Calls n8n memory webhook (30s timeout)
   │   POST https://n8n.ella-ai-care.com/webhook/memory-agent
   │   Returns: { memories: [{ content, category, tags }] }
   └─ Stores results in Firestore
         ↓
6. FIRESTORE STORAGE
   ├─ Collection: users/{uid}/conversations/{conversation_id}
   │   ├─ transcript: "Full conversation text..."
   │   ├─ transcript_segments: [{ text, speaker, start, end }]
   │   ├─ structured: { title, overview, emoji, category }
   │   ├─ status: "completed"
   │   ├─ created_at, finished_at
   │   └─ source: "omi"
   ├─ Collection: users/{uid}/memories/{memory_id}
   │   ├─ content: "User takes medication daily"
   │   ├─ category: "health"
   │   ├─ conversation_id: (link back)
   │   └─ created_at
   └─ Vector DB: Pinecone (for semantic search)
         ↓
7. iOS APP POLLING
   ├─ GET /v1/conversations?uid={uid}&limit=10
   ├─ GET /v3/memories?uid={uid}&limit=100
   └─ Displays new conversations and memories
```

**Key Features**:
- **Speaker Diarization**: PyAnnote identifies multiple speakers (SPEAKER_00, SPEAKER_01)
- **Speech Profiles**: User's voice is recognized and tagged as `is_user=true`
- **Real-time Streaming**: iOS app receives transcript segments as they're transcribed
- **Automatic Finalization**: Conversation ends after 2 minutes of silence
- **Encryption**: Transcript segments encrypted at rest (optional "enhanced" protection level)

---

### B. Voice Mode Flow (2-way Voice Calls)

**Purpose**: Synchronous voice conversations with Ella AI
**Latency**: <2 seconds (STT → LLM → TTS)
**Endpoint**: `wss://api.ella-ai-care.com/v2/voice`

```
┌─────────────────────────────────────────────────────────────────┐
│                     VOICE MODE FLOW (v2)                         │
└─────────────────────────────────────────────────────────────────┘

1. iOS APP
   ├─ User taps "Talk to Ella"
   ├─ Opens WebSocket: wss://api.ella-ai-care.com/v2/voice?uid={uid}
   └─ Sends: Raw PCM16 audio (16kHz, mono)
         ↓
2. BACKEND (routers/voice_v2.py → integrations/pipecat/)
   ├─ Accepts WebSocket
   ├─ Initializes Pipecat Manual Pipeline
   ├─ Fetches Ella config from n8n:
   │   POST https://n8n.ella-ai-care.com/webhook/voice-init
   │   { uid, message_limit: 10 }
   │   Returns:
   │   ├─ agent_config: { model, temperature, system_prompt }
   │   ├─ blocks: { user_profile, rolling_memories, rolling_summaries }
   │   ├─ recent_messages: [{ role, content }]  # Last 10 messages for continuity
   │   └─ persona: "You are Ella, a warm AI companion..."
   └─ Notifies call start:
       POST https://n8n.ella-ai-care.com/webhook/call-state
       { action: "start", uid, params: { call_type, call_sid } }
         ↓
3. VOICE ACTIVITY DETECTION (Silero VAD)
   ├─ Buffers incoming PCM16 audio
   ├─ VAD analyzes every 1024 bytes (512 samples @ 16kHz)
   ├─ Detects speech start: 200ms confirmation
   ├─ Detects speech end: 1.5s silence
   └─ Triggers STT when speech confirmed
         ↓
4. SPEECH-TO-TEXT (Deepgram Nova-2)
   ├─ Streams audio to Deepgram WebSocket API
   ├─ Receives streaming transcription:
   │   { is_final: true, text: "What's my schedule today?" }
   ├─ Accumulates final transcripts
   └─ Returns complete user utterance
         ↓
5. LLM PROCESSING (Groq / xAI)
   ├─ Builds context from:
   │   ├─ System prompt (from n8n config)
   │   ├─ Memory blocks (user_profile, rolling_memories)
   │   ├─ Recent messages (last 10 turns)
   │   └─ Current user message
   ├─ Calls LLM API:
   │   Model: llama-3.3-70b-versatile (Groq) or grok-2-1212 (xAI)
   │   Temperature: 0.7
   │   Max tokens: 150 (for low latency)
   ├─ Receives streaming response
   └─ Returns: "You have a meeting at 2pm with Sarah."
         ↓
6. TEXT-TO-SPEECH (OpenAI TTS or ElevenLabs)
   ├─ OpenAI TTS (default):
   │   Model: tts-1-hd
   │   Voice: nova
   │   Output: PCM16 @ 24kHz
   ├─ ElevenLabs (optional, for ultra-low latency):
   │   Model: eleven_turbo_v2_5
   │   Voice: configurable
   │   Output: PCM16 @ 24kHz (streaming)
   └─ Streams audio chunks back to iOS
         ↓
7. iOS APP
   ├─ Receives PCM16 audio chunks
   ├─ Plays audio via AVAudioEngine
   ├─ Supports barge-in:
   │   └─ User speaks → iOS sends audio → Backend cancels TTS
   └─ Continues conversation loop
         ↓
8. POST-CALL PROCESSING (when WebSocket closes)
   ├─ Builds full conversation transcript
   ├─ Calls n8n memory agent (background)
   │   POST https://n8n.ella-ai-care.com/webhook/memory-agent
   ├─ Calls n8n summary agent (background)
   │   POST https://n8n.ella-ai-care.com/webhook/summary-agent
   ├─ Stores conversation in Firestore
   └─ Notifies call end:
       POST https://n8n.ella-ai-care.com/webhook/call-state
       { action: "end", uid, params: { call_sid, status } }
```

**Key Features**:
- **Server-side VAD**: Backend detects when user stops speaking
- **Barge-in Support**: User can interrupt AI mid-sentence
- **Streaming TTS**: Audio chunks sent as generated (no buffering)
- **Context Continuity**: Last 10 messages loaded from Letta for seamless conversation
- **Call State Tracking**: n8n knows when user is on a call (prevents duplicate alerts)

---

### C. Edge ASR Flow (On-Device Transcription)

**Purpose**: Privacy-first transcription (PHI never leaves device)
**Latency**: <1 second (no network calls for STT)
**Endpoint**: Same `wss://api.ella-ai-care.com/v4/listen` but sends text instead of audio

```
┌─────────────────────────────────────────────────────────────────┐
│                     EDGE ASR FLOW                                │
└─────────────────────────────────────────────────────────────────┘

1. iOS APP (On-Device ASR)
   ├─ Microphone captures audio
   ├─ Apple Speech Framework (or Parakeet/Whisper.cpp)
   ├─ Performs STT locally on device
   └─ Generates transcript segments:
       { text: "Hello, how are you?", isFinal: true }
         ↓
2. iOS APP (WebSocket)
   ├─ Opens WebSocket: wss://api.ella-ai-care.com/v4/listen
   ├─ Sends JSON messages instead of audio bytes:
   │   {
   │     "type": "transcript_segment",
   │     "text": "Hello, how are you?",
   │     "speaker": "SPEAKER_00",
   │     "start": 0.0,
   │     "end": 2.5,
   │     "asr_provider": "apple_speech"  // or "parakeet", "whisper"
   │   }
   └─ Backend receives pre-transcribed text
         ↓
3. BACKEND (routers/transcribe.py, lines 1094-1113)
   ├─ Detects JSON message with type="transcript_segment"
   ├─ Creates TranscriptSegment object:
   │   ├─ text: from iOS
   │   ├─ source: "edge_asr" (for analytics)
   │   ├─ asr_provider: "apple_speech" (for A/B testing)
   │   └─ speech_profile_processed: excluded (added later)
   ├─ Buffers segment (same 600ms processing loop)
   └─ No Deepgram API call (cost savings!)
         ↓
4. DOWNSTREAM PROCESSING (identical to cloud flow)
   ├─ Conversation finalization (2-minute timeout)
   ├─ n8n summary agent
   ├─ n8n memory agent
   ├─ Firestore storage
   └─ iOS polling
```

**Key Features**:
- **Zero Cloud STT Costs**: No Deepgram charges
- **HIPAA Compliance**: PHI (protected health info) never sent to cloud for transcription
- **ASR Provider Tracking**: Backend logs which framework was used (analytics)
- **A/B Testing**: Compare Apple Speech vs Parakeet accuracy
- **Same Infrastructure**: Downstream processing identical to cloud flow

**Critical Bug Fixes (Nov 2025)**:
1. ❌ Bug #1: "TranscriptSegment not subscriptable" → ✅ Fixed: Convert to dict before buffering
2. ❌ Bug #2: Duplicate `speech_profile_processed` keyword → ✅ Fixed: Exclude from dict
3. ❌ Bug #3: Empty transcript field in Firestore → ✅ Fixed: Populate from segments

---

## 3. External Integrations

### Deepgram (STT)

**Purpose**: Cloud speech-to-text transcription
**API Endpoint**: `wss://api.deepgram.com/v1/listen`
**Model**: Nova-2 (multilingual, high accuracy)
**Sample Rate**: 16kHz
**Features**: Streaming transcription, speaker diarization, interim results

**Used In**:
- Ambient listening (v4/listen)
- Voice mode v2 (v2/voice)

**Cost**: ~$0.0048/min for Nova-2
**Alternative**: Soniox (for languages Deepgram doesn't support)

---

### OpenAI (TTS, Embeddings, Fallback LLM)

**TTS API**: `https://api.openai.com/v1/audio/speech`
**Model**: `tts-1-hd`
**Voice**: `nova` (natural, female voice)
**Output**: PCM16 @ 24kHz
**Latency**: ~1.5 seconds for 50 tokens

**Embeddings API**: `https://api.openai.com/v1/embeddings`
**Model**: `text-embedding-3-small` (1536 dimensions)
**Used For**: Pinecone vector search, semantic similarity

**Chat API**: `https://api.openai.com/v1/chat/completions`
**Model**: `gpt-4o-mini`
**Used For**: Fallback when Groq/xAI unavailable, local summary generation

---

### Groq (Primary Voice LLM)

**Purpose**: Ultra-fast LLM inference for voice mode
**API Endpoint**: `https://api.groq.com/openai/v1/chat/completions`
**Model**: `llama-3.3-70b-versatile`
**Speed**: ~500 tokens/sec (fastest in industry)
**Temperature**: 0.7
**Max Tokens**: 150 (for low latency)

**Why Groq?**: 10x faster than OpenAI GPT-4 → critical for sub-2s voice latency

**Rate Limits**: 30 requests/min → can cause 429 errors during high usage

---

### xAI (Alternative Voice LLM)

**Purpose**: Alternative to Groq when rate-limited
**API Endpoint**: `https://api.x.ai/v1/chat/completions`
**Model**: `grok-2-1212`
**Speed**: ~200 tokens/sec
**Temperature**: 0.7
**Max Tokens**: 150

**Fallback Order**: Groq → xAI → OpenAI GPT-4o-mini

---

### ElevenLabs (Alternative TTS)

**Purpose**: Ultra-low latency streaming TTS
**API Endpoint**: `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`
**Model**: `eleven_turbo_v2_5`
**Voice**: Configurable per user
**Output**: PCM16 @ 24kHz (streaming)
**Latency**: ~800ms for 50 tokens (vs 1.5s for OpenAI)

**Status**: Optional (requires ElevenLabs SDK installed)

---

### Firebase / Firestore (Primary Database)

**Purpose**: User data, conversations, memories, authentication
**Project ID**: `omi-dev-ca005`
**Region**: `us-central1`

**Collections**:
- `users/` - User profiles, settings, FCM tokens
- `users/{uid}/conversations/` - Conversation documents
- `users/{uid}/memories/` - Memory documents
- `users/{uid}/action_items/` - Task documents
- `users/{uid}/people/` - Contact/person records

**Security**: Enhanced encryption for PHI (transcript_segments compressed + encrypted)

---

### Pinecone (Vector Search)

**Purpose**: Semantic conversation search
**Index Name**: `conversations` (configurable)
**Dimensions**: 1536 (matches OpenAI embeddings)
**Metric**: Cosine similarity

**Workflow**:
1. Conversation finalized → generate embedding (OpenAI)
2. Store vector in Pinecone with metadata (uid, conversation_id, timestamp)
3. Query: user searches "health discussions" → backend generates embedding → Pinecone returns similar conversation IDs → backend fetches full data from Firestore

**Used By**: `/v1/ella/search/conversations` endpoint

---

### n8n (Workflow Orchestration)

**Purpose**: Bridge between OMI backend and Letta agents
**Host**: `https://n8n.ella-ai-care.com`
**Architecture**: Docker container on same VPS as backend
**Role**: Workflow engine that calls Letta agents and formats responses

**See next section for webhook details**

---

### Letta (AI Agent Framework)

**Purpose**: Long-term memory, context management, persona
**Former Name**: MemGPT
**Host**: Managed by Ella team (separate infrastructure)

**Agents**:
- **Scanner Agent**: Real-time urgency detection (medical emergencies, questions)
- **Memory Agent**: Extract facts/memories from conversations
- **Summary Agent**: Generate structured conversation summaries
- **Main Agent**: Voice conversation handler (persona + memory blocks)

**Integration**: Backend → n8n webhooks → Letta API → Agent execution

**Memory Silo Problem**: Letta has its own memory store, separate from OMI's Firestore/Pinecone (see Challenges section)

---

## 4. n8n Webhook Endpoints

All n8n endpoints are hosted at `https://n8n.ella-ai-care.com/webhook/`

### A. `/webhook/voice-init` (Voice Configuration)

**Called By**: `integrations/pipecat/services/n8n_client.py`
**When**: Voice session starts (before first audio exchange)
**Method**: POST
**Timeout**: 30 seconds

**Request**:
```json
{
  "uid": "user-123",
  "message_limit": 10
}
```

**Response**:
```json
{
  "agent_config": {
    "model": "llama-3.3-70b-versatile",
    "provider": "groq",
    "temperature": 0.7,
    "max_tokens": 150,
    "system_prompt": "You are Ella, a caring AI companion..."
  },
  "blocks": {
    "user_profile": "User is Sarah, 65, lives in Portland...",
    "rolling_memories": "Recent facts: Takes blood pressure med daily...",
    "rolling_summaries": "Recent conversations: Discussed family on 12/20..."
  },
  "recent_messages": [
    { "role": "user", "content": "How's the weather?" },
    { "role": "assistant", "content": "It's sunny in Portland today!" }
  ],
  "user": {
    "name": "Sarah",
    "timezone": "America/Los_Angeles"
  },
  "audio_preferences": {
    "tts_sample_rate": 24000
  }
}
```

**Purpose**: Provide AI persona, memory context, and conversation history for seamless voice interactions

---

### B. `/webhook/memory-agent` (Memory Extraction)

**Called By**: `utils/conversations/process_conversation.py` (ambient), `integrations/pipecat/` (voice mode)
**When**: Conversation finalized
**Method**: POST
**Timeout**: 30 seconds

**Request**:
```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "transcript": "Full conversation text...",
  "segments": [
    { "text": "Hello", "speaker": "SPEAKER_00", "start": 0.0, "end": 1.0 }
  ],
  "source": "omi" // or "voice_mode_v2", "edge_asr"
}
```

**Response** (n8n calls backend callback):
```json
POST /v1/ella/memory
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "memories": [
    {
      "content": "User takes blood pressure medication daily at 8am",
      "category": "system",
      "visibility": "private",
      "tags": ["medication", "health"]
    }
  ]
}
```

**Flow**: Backend → n8n → Letta memory agent → n8n → Backend callback → Firestore

---

### C. `/webhook/summary-agent` (Conversation Summary)

**Called By**: `utils/conversations/process_conversation.py` (ambient), `integrations/pipecat/` (voice mode)
**When**: Conversation finalized
**Method**: POST
**Timeout**: 30 seconds

**Request**: Same as memory-agent

**Response** (n8n calls backend callback):
```json
POST /v1/ella/conversation
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "structured": {
    "title": "Morning Health Check-In",
    "overview": "User discussed morning routine and medication schedule...",
    "emoji": "💊",
    "category": "health",
    "action_items": [
      {
        "description": "Schedule doctor appointment",
        "due_at": "2025-12-30T10:00:00Z"
      }
    ],
    "events": [
      {
        "title": "Doctor Appointment",
        "start": "2025-12-30T14:00:00Z",
        "duration": 60,
        "description": "Follow-up checkup"
      }
    ]
  }
}
```

**Flow**: Backend → n8n → Letta summary agent → n8n → Backend callback → Firestore

---

### D. `/webhook/scanner-agent` (Real-time Urgency Detection)

**Called By**: `routers/transcribe.py` (line 925-933)
**When**: Every 600ms during ambient listening
**Method**: POST
**Timeout**: 1 second (fire-and-forget)

**Request**:
```json
{
  "uid": "user-123",
  "conversation_id": "conv-456",
  "segments": [
    { "text": "I'm having chest pain", "speaker": "SPEAKER_00" }
  ],
  "device_type": "omi"
}
```

**Response** (n8n calls backend notification callback if urgent):
```json
POST /v1/ella/notification
{
  "uid": "user-123",
  "message": "I noticed you mentioned chest pain. Are you okay?",
  "urgency": "EMERGENCY",
  "generate_audio": true,
  "metadata": { "trigger": "chest_pain_keyword" }
}
```

**Urgency Levels**:
- `EMERGENCY`: Medical crisis → push notification with sound
- `QUESTION`: User asked Ella something → silent notification
- `WAKE_WORD`: "Hey Ella" detected → wake up voice mode
- `INTERESTING`: Worth noting → no notification
- `NORMAL`: Low priority → ignored

**Flow**: Backend → n8n → Letta scanner → n8n → Backend notification → FCM push → iOS

---

### E. `/webhook/call-state` (Voice Call Tracking)

**Called By**: `integrations/pipecat/services/n8n_client.py`
**When**: Voice session starts/ends
**Method**: POST
**Timeout**: 5 seconds (fire-and-forget)

**Start Request**:
```json
{
  "action": "start",
  "uid": "user-123",
  "params": {
    "call_type": "voice_mode",
    "call_sid": "session-789",
    "initiated_by": "user"
  }
}
```

**End Request**:
```json
{
  "action": "end",
  "uid": "user-123",
  "params": {
    "call_sid": "session-789",
    "ended_by": "user",
    "status": "completed" // or "failed", "cancelled"
  }
}
```

**Purpose**: Prevent scanner from sending notifications during voice calls (avoids duplicate alerts)

---

## 5. Backend API Endpoints

### WebSocket Endpoints

#### `wss://api.ella-ai-care.com/v4/listen`

**Purpose**: Ambient audio transcription
**Auth**: Firebase JWT or `ADMIN_KEY` header
**Query Params**:
- `uid`: User ID
- `language`: Language code (e.g., "en", "es", "multi")
- `sample_rate`: Audio sample rate (8000 or 16000)
- `codec`: Audio codec ("pcm8", "pcm16", "opus", "opus_fs320")
- `channels`: Audio channels (1 or 2)
- `include_speech_profile`: Enable speaker recognition (true/false)
- `conversation_timeout`: Seconds before auto-finalizing (default 120)

**Protocol**:
- Client sends: Binary audio frames (Opus or PCM)
- Server sends: JSON transcript segments
- Server sends: JSON events (status, photos, translations)

**Lifecycle**:
1. Client connects → server validates user
2. Client streams audio → server transcribes in real-time
3. Server sends segments every 600ms
4. Client closes → server finalizes conversation
5. OR 2 minutes of silence → server auto-finalizes

---

#### `wss://api.ella-ai-care.com/v2/voice`

**Purpose**: 2-way voice conversations
**Auth**: None (uid in query param)
**Query Params**:
- `uid`: User ID (required)
- `session_id`: Optional session ID

**Protocol**:
- Client sends: Raw PCM16 audio (16kHz, mono)
- Server sends: PCM16 audio chunks (TTS)
- Barge-in supported (client can interrupt AI)

**Lifecycle**:
1. Client connects → server fetches Ella config
2. Server notifies call start (n8n)
3. Client speaks → VAD detects → STT → LLM → TTS → audio response
4. Loop continues until client closes
5. Server saves conversation → notifies call end

---

### REST Endpoints (Ella Integration)

#### `POST /v1/ella/memory` (Callback)

**Purpose**: Ella's memory agent sends extracted memories
**Auth**: `secret-key` header (INTERNAL_API_KEY or ADMIN_KEY)
**Request**: See n8n webhooks section above

---

#### `POST /v1/ella/conversation` (Callback)

**Purpose**: Ella's summary agent sends conversation summary
**Auth**: `secret-key` header
**Request**: See n8n webhooks section above

---

#### `POST /v1/ella/notification` (Callback)

**Purpose**: Ella's scanner sends urgent notifications
**Auth**: `secret-key` header
**Request**: See n8n webhooks section above

**Features**:
- Generates TTS audio (via OpenAI)
- Sends multi-device push notification
- Stores assistant message in conversation transcript

---

#### `GET /v1/ella/conversations` (Letta Tools)

**Purpose**: Letta agents query user conversations
**Auth**: `secret-key` header
**Query Params**:
- `uid`: User ID
- `limit`: Max results (default 10)
- `offset`: Pagination offset
- `include_transcript`: Include full segments (default false)
- `categories`: Filter by category (comma-separated)
- `start_date`: ISO date (e.g., "2025-12-01")
- `end_date`: ISO date

**Response**:
```json
[
  {
    "id": "conv-123",
    "created_at": "2025-12-23T10:00:00Z",
    "structured": {
      "title": "Morning Health Check-In",
      "overview": "...",
      "emoji": "💊",
      "category": "health"
    }
  }
]
```

---

#### `GET /v1/ella/memories` (Letta Tools)

**Purpose**: Letta agents query user memories
**Auth**: `secret-key` header
**Query Params**: Similar to conversations

**Response**:
```json
[
  {
    "id": "mem-456",
    "content": "User takes blood pressure medication daily",
    "category": "system",
    "created_at": "2025-12-23T10:00:00Z",
    "conversation_id": "conv-123"
  }
]
```

---

#### `GET /v1/ella/search/conversations` (Semantic Search)

**Purpose**: Vector similarity search across conversations
**Auth**: `secret-key` header
**Query Params**:
- `uid`: User ID
- `query`: Natural language search ("health concerns", "family discussions")
- `limit`: Max results (default 10, max 20)
- `start_date`, `end_date`: Date filters
- `include_transcript`: Include segments

**Flow**:
1. Backend generates embedding (OpenAI)
2. Queries Pinecone vector DB
3. Fetches full conversations from Firestore
4. Returns ranked results

---

### REST Endpoints (iOS App)

#### `GET /v1/conversations`

**Purpose**: iOS app fetches user conversations
**Auth**: Firebase JWT
**Query Params**: `uid`, `limit`, `offset`

---

#### `GET /v3/memories`

**Purpose**: iOS app fetches user memories
**Auth**: Firebase JWT
**Query Params**: `uid`, `limit`, `offset`

---

#### `PATCH /v1/users/language`

**Purpose**: Update user language preference
**Auth**: Firebase JWT
**Body**: `{ "language": "en" }`

---

### Health Check Endpoints

#### `GET /health`

**Response**: `{ "status": "ok" }`

---

#### `GET /v2/voice/health`

**Response**:
```json
{
  "status": "ok",
  "version": "2.1.1",
  "config": {
    "vad_provider": "silero",
    "stt_provider": "deepgram",
    "tts_provider": "openai",
    "llm_provider": "groq"
  }
}
```

---

#### `GET /v1/ella/health`

**Response**:
```json
{
  "status": "healthy",
  "service": "ella-integration",
  "version": "1.2.0",
  "endpoints": { ... }
}
```

---

## 6. Infrastructure

### VPS Deployment

**Provider**: Vultr
**IP Address**: `100.101.168.91` (Tailscale)
**Public URL**: `https://api.ella-ai-care.com`
**OS**: Ubuntu 22.04
**SSL**: Let's Encrypt (auto-renewal)

---

### Systemd Service

**Service Name**: `omi-backend.service`
**Location**: `/etc/systemd/system/omi-backend.service`
**User**: `root`
**Working Directory**: `/root/omi/backend`
**Command**: `uvicorn main:app --host 0.0.0.0 --port 8000`
**Auto-restart**: Yes (3 second delay)

**Management**:
```bash
sudo systemctl status omi-backend
sudo systemctl restart omi-backend
sudo journalctl -u omi-backend -f  # Live logs
```

---

### Environment Variables

**Location**: `/root/omi/backend/.env`

**Critical Variables**:
```bash
# Firebase
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=./google-credentials.json

# APIs
DEEPGRAM_API_KEY=xxx
OPENAI_API_KEY=xxx
GROQ_API_KEY=xxx
XAI_API_KEY=xxx
ELEVENLABS_API_KEY=xxx
PINECONE_API_KEY=xxx
HUGGINGFACE_TOKEN=xxx

# Redis (n8n Docker container)
REDIS_DB_HOST=172.21.0.4
REDIS_DB_PORT=6379

# Storage
BUCKET_PRIVATE_CLOUD_SYNC=omi-dev-ca005.firebasestorage.app

# Security
ADMIN_KEY=dev_testing_key_12345
INTERNAL_API_KEY=xxx
LOCAL_DEVELOPMENT=false  # Set to true for local testing
```

---

### Firebase Credentials

**Location**: `/root/omi/backend/google-credentials.json`
**Service Account**: `firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com`
**Roles**:
- Firestore Admin
- Storage Object Admin
- Firebase Authentication Admin

---

### Redis Integration

**Container**: `n8n-redis` (Docker)
**Network**: 172.21.0.0/16 (Docker bridge)
**IP**: 172.21.0.4
**Port**: 6379
**Password**: None (internal network only)

**Usage**:
- Conversation state tracking (`in_progress_conversation_id`)
- User geolocation cache
- Notification rate limiting

**NOT used for**: Chunk buffering (handled in-memory in Python)

---

### GCS Bucket Permissions

**Bucket**: `gs://omi-dev-ca005.firebasestorage.app`
**Permissions**:
```bash
gsutil iam ch serviceAccount:firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com:roles/storage.objectAdmin \
  gs://omi-dev-ca005.firebasestorage.app
```

---

### Firestore Composite Indexes

**Index 1: Conversations**
- Collection: `conversations`
- Fields: `discarded` (ASC), `status` (ASC), `created_at` (DESC)
- Status: ✅ Active

**Index 2: Memories**
- Collection: `memories`
- Fields: `scoring` (DESC), `created_at` (DESC)
- Status: ✅ Active (user added)

---

## 7. Database Schema

### Firestore Collections

#### `users/{uid}`

```typescript
{
  id: string,
  email: string,
  name: string,
  created_at: Timestamp,
  language: string,  // "en", "es", etc.
  fcm_tokens: [
    { token: string, device_name: string, last_updated: Timestamp }
  ],
  plan: "basic" | "premium" | "enterprise",
  transcription_credits: number,
  // ... other fields
}
```

---

#### `users/{uid}/conversations/{conversation_id}`

```typescript
{
  id: string,
  created_at: Timestamp,
  started_at: Timestamp,
  finished_at: Timestamp,
  status: "in_progress" | "processing" | "completed",
  source: "omi" | "openglass" | "external_integration" | "voice_mode_v2",
  language: string,

  // Transcript (encrypted if data_protection_level="enhanced")
  transcript: string,  // Full conversation text
  transcript_segments: [
    {
      id: string,
      text: string,
      speaker: string,  // "SPEAKER_00", "SPEAKER_01"
      speaker_id: number,
      is_user: boolean,
      role: "user" | "assistant" | null,
      person_id?: string,  // Link to people collection
      start: number,  // Seconds
      end: number,
      source: "deepgram" | "edge_asr" | "soniox",
      asr_provider?: "apple_speech" | "parakeet" | "whisper"
    }
  ],
  transcript_segments_compressed: boolean,

  // Structured summary (from Ella summary agent)
  structured: {
    title: string,
    overview: string,
    emoji: string,
    category: CategoryEnum,  // "health", "personal", "work", etc.
    action_items: [
      {
        description: string,
        completed: boolean,
        due_at?: Timestamp,
        completed_at?: Timestamp
      }
    ],
    events: [
      {
        title: string,
        description: string,
        start: Timestamp,
        duration: number,  // minutes
        created: boolean
      }
    ]
  },

  // Metadata
  discarded: boolean,
  is_locked: boolean,  // Credit limit reached
  geolocation?: { latitude: number, longitude: number, address: string },
  photos: [
    {
      id: string,
      base64: string,
      description: string,
      created_at: Timestamp,
      discarded: boolean
    }
  ],

  // Privacy
  data_protection_level: "standard" | "enhanced"
}
```

---

#### `users/{uid}/memories/{memory_id}`

```typescript
{
  id: string,
  content: string,  // "User takes blood pressure medication daily"
  category: "interesting" | "system",
  visibility: "private" | "shared",
  tags: string[],

  created_at: Timestamp,
  updated_at: Timestamp,
  conversation_id?: string,  // Link back to source conversation

  // Search scoring
  scoring: number,
  manually_added: boolean,
  discarded: boolean,

  // Privacy
  data_protection_level: "standard" | "enhanced"
}
```

---

#### `users/{uid}/action_items/{action_item_id}`

```typescript
{
  id: string,
  description: string,
  completed: boolean,
  created_at: Timestamp,
  updated_at: Timestamp,
  due_at?: Timestamp,
  completed_at?: Timestamp,
  conversation_id?: string
}
```

---

#### `users/{uid}/people/{person_id}`

```typescript
{
  id: string,
  name: string,
  created_at: Timestamp,
  updated_at: Timestamp
}
```

---

### Pinecone Index

**Index Name**: `conversations` (configurable)
**Dimensions**: 1536
**Metric**: Cosine similarity

**Vector Metadata**:
```typescript
{
  uid: string,
  conversation_id: string,
  created_at: number,  // Unix timestamp
  category?: string
}
```

**Query Flow**:
1. Generate embedding from query text (OpenAI)
2. Query Pinecone: `index.query(vector=embedding, filter={"uid": uid}, top_k=10)`
3. Returns: `[{ id: conversation_id, score: 0.95 }]`
4. Fetch full conversations from Firestore

---

### Redis Keys

**Pattern**: `{prefix}:{uid}:{key}`

**Keys**:
- `in_progress_conversation_id:{uid}` → conversation_id (string)
- `user_geolocation:{uid}` → JSON { latitude, longitude, address }
- `notification_sent:{uid}:{type}` → timestamp (rate limiting)

**TTL**: Varies by key (geolocation: 1 hour, notifications: 24 hours)

---

## 8. Critical Dependencies

### Python Packages

**Core Framework**:
- `fastapi==0.104.0` - Web framework
- `uvicorn==0.24.0` - ASGI server
- `websockets==12.0` - WebSocket support
- `pydantic==2.5.0` - Data validation

**Audio Processing**:
- `opuslib==3.0.1` - Opus codec
- `pydub==0.25.1` - Audio manipulation
- `webrtcvad==2.0.10` - Voice activity detection (deprecated, use Silero)

**STT/TTS**:
- `deepgram-sdk==3.0.0` - Deepgram client
- `openai==1.0.0` - OpenAI client
- `elevenlabs==1.0.0` - ElevenLabs client (optional)

**LLM**:
- `anthropic==0.7.0` - Claude API (fallback)
- `groq==0.4.0` - Groq API
- (xAI uses OpenAI-compatible endpoint)

**Database**:
- `firebase-admin==6.2.0` - Firestore, Auth, FCM
- `redis==5.0.0` - Redis client
- `pinecone-client==3.0.0` - Vector DB

**Voice Mode (Pipecat)**:
- `pipecat-ai==0.0.49` - Pipecat framework
- `daily-python==0.9.0` - Daily.co integration (unused)

**ML Models**:
- `torch==2.1.0` - PyTorch (for Silero VAD)
- `silero-vad==4.0.0` - Voice activity detection
- `pyannote.audio==3.0.0` - Speaker diarization (17GB models)

**Utilities**:
- `httpx==0.25.0` - Async HTTP client
- `requests==2.31.0` - Sync HTTP client
- `python-dotenv==1.0.0` - Environment variables
- `cryptography==41.0.0` - Encryption

---

### System Dependencies

**Required**:
- `ffmpeg` - Audio format conversion
- `opus` - Opus codec library (via Homebrew on macOS)
- `git` - Version control
- `unzip` - Model extraction

**Installation (Ubuntu)**:
```bash
sudo apt-get install -y ffmpeg libopus-dev git unzip
```

**Installation (macOS)**:
```bash
brew install opus ffmpeg
```

---

### ML Models (Cached)

**Silero VAD** (~17MB):
- Location: `~/.cache/torch/hub/snakers4_silero-vad_master/`
- Auto-downloaded on first use

**PyAnnote Speaker Diarization** (~17GB):
- Location: `~/.cache/huggingface/hub/models--pyannote--speaker-diarization-3.1/`
- Requires: `python download_models.py`
- Requires: Hugging Face token with model access

**WhisperX** (~2GB):
- Location: `~/.cache/huggingface/`
- Status: Downloaded but not yet integrated
- Requires: `python download_whisper_models.py`

---

## 9. Current Architecture Challenges

### A. Dual Memory Silo Problem

**Issue**: OMI and Letta maintain separate memory databases with no sync

**OMI Memory Storage**:
- Firestore: `users/{uid}/memories/`
- Pinecone: Vector embeddings for semantic search
- Updated by: Backend after conversation finalization

**Letta Memory Storage**:
- Letta's internal database (Postgres + vector store)
- Updated by: Letta agents during processing
- Contains: Agent's working memory, conversation history, user profile

**Consequence**:
- ❌ Letta can't see memories created via iOS manual entry
- ❌ OMI can't see Letta's internal reasoning/context
- ❌ Risk of duplicate memories (same fact stored twice)
- ❌ Inconsistent memory retrieval (depends on which API you query)

**Potential Solutions**:
1. **Option A**: Backend syncs Firestore → Letta after every memory write
2. **Option B**: Letta writes directly to Firestore (bypass n8n callbacks)
3. **Option C**: Read-through cache: Letta queries OMI API for missing memories
4. **Option D**: Single source of truth: Deprecate OMI's memory storage, use Letta only

**Status**: ⚠️ No solution implemented yet

---

### B. Rate Limiting (Groq 429 Errors)

**Issue**: Groq API has 30 requests/min limit → voice mode fails during high usage

**Current Behavior**:
- User initiates voice call
- Backend calls Groq for LLM response
- Groq returns 429 (rate limit exceeded)
- Backend crashes (no fallback)

**Mitigation**:
- ✅ Implemented: xAI fallback (grok-2-1212)
- ⚠️ Missing: OpenAI GPT-4o-mini as final fallback
- ⚠️ Missing: Rate limit backoff/retry logic

**Ideal Flow**:
```
Try Groq (llama-3.3-70b-versatile)
  → 429 error → Try xAI (grok-2-1212)
    → 429 error → Try OpenAI (gpt-4o-mini)
      → All fail → Return error to user
```

---

### C. Upstream Sync (393 Commits Behind)

**Issue**: OMI backend is a downstream fork of `BasedHardware/omi`

**Upstream**: `github.com/BasedHardware/omi`
**Downstream**: `github.com/ellaaicare/omi`
**Divergence**: 393 commits behind upstream (as of Dec 2025)

**Risks**:
- ❌ Missing upstream bug fixes
- ❌ Missing performance improvements
- ❌ Merge conflicts accumulating
- ❌ Security vulnerabilities not patched

**Manual Merge Required**:
- Cannot auto-merge due to custom Ella integration code
- Must review each upstream commit for conflicts
- Estimated effort: 2-3 days

**Critical**: Always use `--repo ellaaicare/omi` when posting GitHub issues/PRs!

---

### D. Firebase Security Rules (Open for Development)

**Issue**: Firestore security rules set to OPEN (30-day temporary window)

**Current Rules**:
```javascript
allow read, write: if true;  // ⚠️ DANGEROUS
```

**Timeline**: Must lock down within 7 days of database creation

**Required Rules** (before production):
```javascript
match /users/{userId}/conversations/{conversationId} {
  allow read, write: if request.auth.uid == userId;
}
match /users/{userId}/memories/{memoryId} {
  allow read, write: if request.auth.uid == userId;
}
```

**See**: `docs/SECURITY_HIPAA_CHECKLIST.md` for full production requirements

---

### E. No Automated Backups

**Issue**: Firestore data has no automated backup policy

**Risks**:
- ❌ Accidental deletion (no point-in-time recovery)
- ❌ Data corruption (no rollback)
- ❌ Compliance violation (HIPAA requires backups)

**Solution**: Enable Firestore automated exports to GCS bucket

---

### F. Voice Mode Latency Variability

**Issue**: Voice response time fluctuates 1-5 seconds

**Contributors**:
1. **Groq rate limits** → fallback to slower xAI
2. **Deepgram streaming delays** → varies by utterance length
3. **OpenAI TTS latency** → 1.5s for 50 tokens
4. **n8n webhook timeouts** → 30s max (blocks voice pipeline)

**Optimization Opportunities**:
- ✅ Use ElevenLabs TTS (800ms vs 1.5s)
- ⚠️ Cache common responses (pre-generate TTS audio)
- ⚠️ Move n8n calls to background (don't block voice)
- ⚠️ Increase Groq rate limits (paid tier)

---

## 10. File Structure

```
/Users/greg/repos/omi/backend/
│
├── main.py                          # FastAPI app entry point, router registration
├── start_server.py                  # Helper script with SSL/Opus path setup
├── requirements.txt                 # Python dependencies
├── .env                             # Environment variables (gitignored)
├── google-credentials.json          # Firebase credentials (gitignored)
│
├── routers/                         # API endpoint handlers
│   ├── transcribe.py                # ⭐ WebSocket /v4/listen (ambient listening)
│   ├── voice_v2.py                  # ⭐ WebSocket /v2/voice (voice mode)
│   ├── ella.py                      # ⭐ Ella callback endpoints (/v1/ella/*)
│   ├── conversations.py             # REST: Conversation CRUD
│   ├── memories.py                  # REST: Memory CRUD
│   ├── chat.py                      # REST: Chat endpoints
│   ├── notifications.py             # REST: Push notifications
│   ├── users.py                     # REST: User management
│   ├── testing.py                   # REST: E2E testing endpoints
│   └── ...                          # Other routers
│
├── integrations/                    # External service integrations
│   └── pipecat/                     # ⭐ Voice mode v2 (Pipecat framework)
│       ├── __init__.py
│       ├── pipeline/
│       │   ├── config.py            # Pipeline configuration (VAD, STT, TTS, LLM)
│       │   └── manual_pipeline.py   # ⭐ Manual voice pipeline (Option B)
│       └── services/
│           ├── n8n_client.py        # ⭐ n8n webhook client
│           └── firestore_client.py  # Conversation storage
│
├── database/                        # Database abstraction layer
│   ├── _client.py                   # Firestore client singleton
│   ├── conversations.py             # ⭐ Conversation CRUD operations
│   ├── memories.py                  # Memory CRUD operations
│   ├── users.py                     # User CRUD operations
│   ├── redis_db.py                  # Redis operations
│   ├── vector_db.py                 # Pinecone operations
│   └── notifications_multi_device.py # FCM multi-device push
│
├── models/                          # Pydantic data models
│   ├── conversation.py              # ⭐ Conversation, Structured, ActionItem, Event
│   ├── transcript_segment.py        # ⭐ TranscriptSegment (audio → text)
│   ├── memories.py                  # Memory, MemoryDB
│   ├── users.py                     # User, PlanType
│   └── ...                          # Other models
│
├── utils/                           # Utility functions
│   ├── conversations/
│   │   ├── process_conversation.py  # ⭐ Conversation finalization (n8n calls)
│   │   └── ...
│   ├── stt/
│   │   └── streaming.py             # ⭐ Deepgram/Soniox/Speechmatics STT
│   ├── tts/
│   │   └── manager.py               # TTS provider abstraction (OpenAI/ElevenLabs)
│   ├── ella/
│   │   ├── __init__.py
│   │   └── scanner.py               # ⭐ send_to_scanner() - realtime chunks
│   ├── analytics.py                 # Usage tracking
│   ├── notifications.py             # Push notification helpers
│   └── ...
│
├── docs/                            # Documentation
│   ├── BACKEND_ARCHITECTURE_OVERVIEW.md  # ⭐ This file
│   ├── ELLA_INTEGRATION.md          # Ella integration spec
│   ├── EDGE_ASR_INTEGRATION_GUIDE.md # On-device ASR guide
│   ├── PRD_VOICE_MODE_V2_PIPECAT.md # Voice mode v2 PRD
│   ├── SECURITY_HIPAA_CHECKLIST.md  # Production security requirements
│   └── ...                          # 60+ other docs
│
├── scripts/                         # One-off scripts
│   ├── quick_dump_transcripts.py    # Debug: View recent conversations
│   └── ...
│
├── test_audio/                      # Test audio files (gitignored)
│   ├── pyannote_sample.wav
│   └── silero_test.wav
│
└── testing/                         # Load testing
    └── locustfile.py                # Locust load test definitions
```

---

## Key Files Reference

### ⭐ Most Critical Files (Read These First)

1. **`main.py`** (146 lines)
   → Entry point, router registration, CORS config

2. **`routers/transcribe.py`** (1,282 lines)
   → Ambient listening WebSocket handler
   → Contains Edge ASR integration (lines 1094-1113)
   → Contains Ella scanner integration (lines 925-933)

3. **`routers/voice_v2.py`** (116 lines)
   → Voice mode WebSocket handler
   → Delegates to Pipecat manual pipeline

4. **`integrations/pipecat/pipeline/manual_pipeline.py`** (~800 lines)
   → Voice mode core logic
   → VAD, STT, LLM, TTS orchestration

5. **`routers/ella.py`** (1,035 lines)
   → Ella callback endpoints
   → Memory, summary, notification handling
   → Letta tool endpoints (read/write conversations/memories)

6. **`utils/conversations/process_conversation.py`** (~500 lines)
   → Conversation finalization logic
   → Calls n8n memory/summary agents
   → Fallback to local LLM if n8n fails

7. **`database/conversations.py`** (~800 lines)
   → Firestore conversation CRUD
   → Encryption/compression logic

8. **`models/conversation.py`** (~400 lines)
   → Conversation, Structured, ActionItem, Event
   → Pydantic models with validation

9. **`integrations/pipecat/services/n8n_client.py`** (270 lines)
   → n8n webhook client
   → Voice config, memory agent, summary agent, call state

10. **`utils/stt/streaming.py`** (~600 lines)
    → Deepgram/Soniox/Speechmatics STT
    → WebSocket streaming transcription

---

## Architecture Diagrams (ASCII)

### Overall System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         OMI/ELLA AI ECOSYSTEM                        │
└─────────────────────────────────────────────────────────────────────┘

┌────────────┐
│ OMI Device │ (Hardware: Bluetooth necklace with mic)
└─────┬──────┘
      │ Audio (Opus, 16kHz)
      ↓ (BLE)
┌────────────────┐
│  iOS/Android   │ (Mobile app)
│      App       │
└───────┬────────┘
        │
        ├─ Ambient Listening (WebSocket /v4/listen)
        │  ↓
        │  ┌──────────────────────────────────────┐
        │  │   OMI Backend (FastAPI)              │
        │  │   - Deepgram STT                     │
        │  │   - Conversation buffering           │
        │  │   - Real-time transcription          │
        │  └────────────┬─────────────────────────┘
        │               │
        │               ├─ Fire-and-forget scanner webhook
        │               │  ↓
        │               ├─ POST /webhook/summary-agent (30s timeout)
        │               │  ↓
        │               └─ POST /webhook/memory-agent (30s timeout)
        │                  ↓
        │               ┌──────────────────────────────────────┐
        │               │  n8n Workflow Engine                 │
        │               │  - Routes to Letta agents            │
        │               │  - Formats responses                 │
        │               └────────────┬─────────────────────────┘
        │                            │
        │                            ↓
        │                         ┌──────────────────────────────┐
        │                         │  Letta Agent Cluster         │
        │                         │  - Scanner Agent             │
        │                         │  - Memory Agent              │
        │                         │  - Summary Agent             │
        │                         └────────────┬─────────────────┘
        │                                      │
        │                                      ↓
        │                               POST /v1/ella/memory
        │                               POST /v1/ella/conversation
        │                               POST /v1/ella/notification
        │                                      ↓
        │               ┌──────────────────────────────────────┐
        │               │   OMI Backend (Storage)              │
        │               │   - Firestore (conversations, memories)│
        │               │   - Pinecone (vector search)         │
        │               │   - FCM (push notifications)         │
        │               └────────────┬─────────────────────────┘
        │                            │
        │←───────────────────────────┘ (Polling)
        │  GET /v1/conversations
        │  GET /v3/memories
        │
        └─ Voice Mode (WebSocket /v2/voice)
           ↓
           ┌──────────────────────────────────────┐
           │   OMI Backend (Pipecat)              │
           │   - Silero VAD                       │
           │   - Deepgram STT                     │
           │   - Groq LLM (or xAI fallback)       │
           │   - OpenAI/ElevenLabs TTS            │
           └────────────┬─────────────────────────┘
                        │
                        ├─ POST /webhook/voice-init (config)
                        │  ↓
                        └─ POST /webhook/call-state (start/end)
                           ↓
                        ┌──────────────────────────────────────┐
                        │  n8n → Letta Main Agent              │
                        │  - Returns persona + memory blocks   │
                        │  - Recent chat history               │
                        └──────────────────────────────────────┘
```

---

## Deployment Checklist

Before deploying to production:

- [ ] Set `LOCAL_DEVELOPMENT=false`
- [ ] Lock down Firestore security rules (user-scoped access only)
- [ ] Rotate all API keys to production versions
- [ ] Enable Firebase audit logging
- [ ] Configure automated Firestore backups to GCS
- [ ] Set up monitoring/alerting (e.g., Sentry, Datadog)
- [ ] Sign Business Associate Agreements (HIPAA) with:
  - [ ] Deepgram
  - [ ] OpenAI
  - [ ] Groq
  - [ ] xAI
  - [ ] ElevenLabs
- [ ] Enable SSL/TLS pinning (iOS app)
- [ ] Configure CORS allowlist (remove `*`)
- [ ] Set up rate limiting (prevent abuse)
- [ ] Test failover scenarios (Groq down, n8n down, etc.)
- [ ] Load test voice mode (10+ concurrent calls)
- [ ] Verify encryption at rest (enhanced protection level)

---

## Troubleshooting Reference

### WebSocket Connection Failed

**Symptom**: iOS app can't connect to `/v4/listen` or `/v2/voice`

**Checks**:
1. Backend running? `systemctl status omi-backend`
2. Firewall open? `sudo ufw status` (port 8000 must be open)
3. SSL cert valid? `curl https://api.ella-ai-care.com/health`
4. User exists? Check Firestore `users/{uid}`
5. Credits available? Check `transcription_credits` field

---

### No Transcription Output

**Symptom**: Audio sent but no transcript received

**Checks**:
1. Deepgram API key valid? `echo $DEEPGRAM_API_KEY`
2. Deepgram API usage? Visit https://console.deepgram.com/
3. STT service configured? Check `get_stt_service_for_language()` return value
4. WebSocket open? Check `journalctl -u omi-backend -f` for "WebSocket accepted"

---

### Ella Callbacks Not Working

**Symptom**: Conversations finalized but no summary/memories

**Checks**:
1. n8n reachable? `curl https://n8n.ella-ai-care.com/webhook/summary-agent`
2. Callback endpoints working? `curl https://api.ella-ai-care.com/v1/ella/health`
3. Check backend logs: `journalctl -u omi-backend -f | grep -E "(Ella|📤|✅|⚠️)"`
4. Verify fallback triggered? Look for "🔄 Using local LLM for summary generation"

---

### Voice Mode High Latency

**Symptom**: >3 seconds between user speech and AI response

**Diagnose**:
1. Check Groq rate limits: Look for "429" errors in logs
2. Measure TTS latency: Add timing logs in `manual_pipeline.py`
3. Check n8n webhook timeouts: Look for "⚠️ Voice config timeout"
4. Network latency? Test from different location

**Optimize**:
- Use ElevenLabs TTS (faster than OpenAI)
- Increase `max_tokens` from 150 to 100 (shorter responses)
- Cache common responses (pre-generate TTS audio)

---

## Additional Resources

- **Production Deployment Guide**: `docs/VPS_PRODUCTION_DEPLOYMENT.md`
- **HIPAA Compliance Checklist**: `docs/SECURITY_HIPAA_CHECKLIST.md`
- **Ella Integration Spec**: `docs/ELLA_INTEGRATION.md`
- **Edge ASR Guide**: `docs/EDGE_ASR_INTEGRATION_GUIDE.md`
- **Voice Mode PRD**: `docs/PRD_VOICE_MODE_V2_PIPECAT.md`
- **Testing Guide**: `docs/README_TESTING.md`
- **Letta Architecture**: `docs/LETTA_INTEGRATION_ARCHITECTURE.md`

---

**Document Version**: 2.1
**Last Updated**: December 23, 2025
**Maintained By**: Backend Integration Developer
**Next Review**: January 15, 2026

---

*This document is the authoritative source for understanding OMI/Ella AI backend architecture. Keep it updated as the system evolves.*
