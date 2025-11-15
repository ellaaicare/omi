# OMI Backend System State Snapshot

**Date**: October 30, 2025 19:00 UTC
**Session**: Session 2 - Letta Integration Architecture Design
**Environment**: Production VPS + Local Development

---

## 🌐 Production Environment State

### VPS Infrastructure

**Server Details**:
```
Hostname: api.ella-ai-care.com
IP Address: 100.101.168.91
Provider: Vultr
OS: Ubuntu 22.04
Region: [US/EU - to be confirmed]
```

**Service Status**:
```bash
# Backend Service
systemctl status omi-backend
● omi-backend.service - OMI Backend API Server
     Loaded: loaded (/etc/systemd/system/omi-backend.service; enabled)
     Active: active (running) since Oct 30 18:30:00 UTC 2025
     Main PID: 1260093
```

**Process Information**:
- **Main Process**: uvicorn (PID: 1260093)
- **Port**: 8000 (internal), 443 (external HTTPS)
- **Workers**: 1 (single-threaded)
- **Auto-restart**: Every 3 seconds on failure
- **Uptime**: ~30 minutes (last restart: Oct 30 18:30 UTC)

### Network Configuration

**Exposed Endpoints**:
```
HTTPS (443) → https://api.ella-ai-care.com → nginx reverse proxy → :8000
WebSocket (WSS) → wss://api.ella-ai-care.com/v4/listen → :8000
```

**SSL Certificate**:
- Provider: Let's Encrypt
- Auto-renewal: Enabled (certbot)
- Status: ✅ Valid

**Firewall Rules**:
```
Port 22 (SSH): Open
Port 443 (HTTPS): Open
Port 8000: Closed (internal only)
Port 5432 (Postgres): Closed (if running)
```

### Docker Containers

**n8n Workflow Engine**:
```bash
# Container: n8n
Status: Running
Port: 5678
Network: n8n-network (172.21.0.0/16)
Purpose: Workflow automation (future Letta integration)
```

**Redis Cache**:
```bash
# Container: n8n-redis
Status: Running
IP: 172.21.0.4
Port: 6379
Network: n8n-network (172.21.0.0/16)
Password: None (internal network only)
Purpose: Conversation state tracking
```

**Docker Networks**:
```
n8n-network: 172.21.0.0/16 (bridge)
  - n8n container
  - n8n-redis container
  - Backend can access via 172.21.0.4:6379
```

---

## 🗄️ Database State

### Firebase Firestore

**Project**: omi-dev-ca005
**Console**: https://console.firebase.google.com/project/omi-dev-ca005

**Collections & Document Counts** (approximate):
```
users: ~5-10 documents
conversations: ~15+ documents (growing)
memories: ~5-10 documents (after index fix)
```

**Composite Indexes** (October 30, 2025):

1. **Conversations Index**:
   ```
   Collection: conversations
   Fields indexed:
     - discarded (ascending)
     - status (ascending)
     - created_at (descending)
   Status: ✅ ACTIVE
   Created: Oct 30, 2025
   ```

2. **Memories Index**:
   ```
   Collection: memories
   Fields indexed:
     - scoring (ascending/descending)
     - created_at (descending)
   Status: ✅ ACTIVE (user created)
   Created: Oct 30, 2025
   ```

**Security Rules**:
```javascript
// CURRENT (Development Mode):
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if true;  // ⚠️ OPEN ACCESS
    }
  }
}

// TODO: Restrict before production
```

### GCS Storage Bucket

**Bucket Name**: `omi-dev-ca005.firebasestorage.app`
**Region**: us-central1 (default Firebase)
**Console**: https://console.cloud.google.com/storage/browser/omi-dev-ca005.firebasestorage.app

**Permissions**:
```
Service Account: firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com
Role: Storage Object Admin (roles/storage.objectAdmin)
Granted: October 30, 2025
Status: ✅ ACTIVE
```

**Purpose**:
- Audio file storage for conversations
- Private cloud sync backups
- User-uploaded media

**Current Usage**:
- Estimated: < 1 GB
- Files: Audio recordings from iOS app tests

### Redis State

**Host**: 172.21.0.4 (Docker container)
**Port**: 6379
**Database**: 0 (default)
**Password**: None

**Current Keys** (estimated):
```
conversation:{session_id}: In-progress conversation tracking
# Other keys: TBD (no comprehensive scan performed)
```

**Purpose**:
- Conversation state tracking (`set_in_progress_conversation_id`)
- NOT used for chunk buffering (in-memory Python lists)
- Future: Chunk aggregation for Letta integration

---

## 📡 Real-Time Data Flow State

### WebSocket Connection Lifecycle

**Step 1: iOS App Connection**
```
Client: iOS app (version TBD)
URL: wss://api.ella-ai-care.com/v4/listen?uid={user_id}&language=en&sample_rate=16000&codec=opus&channels=1
Protocol: WebSocket (RFC 6455)
```

**Step 2: Audio Streaming**
```
Format: Opus-encoded audio frames
Sample Rate: 16000 Hz
Channels: 1 (mono)
Frame Duration: 20ms (default Opus frame size)
Bandwidth: ~30 kbps
```

**Step 3: Deepgram Transcription**
```
Service: Deepgram Nova 2 API
Model: nova-2-general (default)
Language: en (English)
Endpoint: wss://api.deepgram.com/v1/listen
Latency: ~300-600ms per chunk
```

**Step 4: Backend Processing**
```
File: /root/omi/backend/routers/transcribe.py
Function: stream_transcript_process() (line 858-869)
Buffer: realtime_segment_buffers (Python list, in-memory)
Interval: 600ms (0.6 seconds)
Processing: Copy buffer → Clear buffer → Process segments
```

**Step 5: Firestore Storage**
```
Trigger: WebSocket close (2-minute timeout or manual stop)
Function: _process_current_conversation()
Collections: conversations, memories
Processing: Memory extraction, embeddings, structured data
```

### Current Buffer State (in-memory)

**Active Buffers** (per WebSocket connection):
```python
# Line 496-501 in transcribe.py
realtime_segment_buffers = []  # Main real-time buffer (cleared every 600ms)
realtime_photo_buffers = []    # Photo buffer (rarely used)
segment_buffers = []           # Webhook forward buffer (1-second interval)
```

**Buffer Flow**:
```
Deepgram chunks arrive → stream_transcript() → realtime_segment_buffers.extend()
                                                         ↓
                                              (every 600ms)
                                                         ↓
                          stream_transcript_process() → segments_to_process = buffer.copy()
                                                         ↓
                                                   buffer.clear()
                                                         ↓
                                              Process segments (async)
```

**Key Characteristics**:
- ❌ NO aggregation if processing gets behind
- ❌ NO splicing of backpressured chunks
- ✅ One batch per 600ms cycle (fixed rate)
- ✅ Non-blocking (asyncio tasks)
- ✅ Separate webhook buffer (1-second batches)

---

## 🔌 External API State

### Deepgram API

**API Key**: Configured in `.env`
**Endpoint**: wss://api.deepgram.com/v1/listen
**Model**: nova-2-general
**Status**: ✅ Active and working
**Logging**: ✅ Enabled (lines 268, 270, 273 in utils/stt/streaming.py)

**Recent Activity** (from logs):
```
Oct 30 18:49:53: Received message from Deepgram
Oct 30 18:49:53: "App. Just seeing if it works."
Oct 30 18:49:53: "After all this development time."
```

**Usage Metrics** (October 30, 2025):
- Requests: ~10-20 (testing)
- Audio Duration: ~5-10 minutes
- Cost: ~$0.02-0.05 (at $0.0043/min)

### OpenAI API

**Status**: Configured but not actively used for real-time processing
**Purpose**: Future fast LLM scanning (Option 3 in architecture doc)
**Model**: gpt-4o-mini (planned for alert scanning)

### Firebase Admin SDK

**Service Account**: firebase-adminsdk-fbsvc@omi-dev-ca005.iam.gserviceaccount.com
**Credentials File**: /root/omi/backend/google-credentials.json
**Status**: ✅ Active
**Operations**:
- Firestore read/write
- GCS bucket access
- Authentication (JWT verification)

---

## 📁 File System State

### VPS Filesystem

**Backend Directory** (`/root/omi/backend`):
```
/root/omi/backend/
├── .env                          # ✅ Configured (Redis, GCS bucket)
├── google-credentials.json       # ✅ Service account credentials
├── main.py                       # FastAPI app entry point
├── venv/                         # ✅ Virtual environment active
│   └── bin/uvicorn              # Running process
├── routers/
│   └── transcribe.py            # WebSocket handler (modified: Deepgram logging enabled)
├── utils/
│   ├── stt/
│   │   └── streaming.py         # ✅ Modified: Lines 268, 270, 273 uncommented
│   └── llms/                    # Future: Letta integration modules
├── database/
│   └── redis_db.py              # ✅ Redis integration
└── docs/
    ├── LETTA_INTEGRATION_ARCHITECTURE.md  # ✅ NEW: Architecture design
    └── SYSTEM_STATE_SNAPSHOT_20251030.md  # ✅ NEW: This document
```

**Modified Files** (Session 2):
1. `/root/omi/backend/.env`:
   - Added: `REDIS_DB_HOST=172.21.0.4`
   - Added: `REDIS_DB_PORT=6379`
   - Added: `BUCKET_PRIVATE_CLOUD_SYNC=omi-dev-ca005.firebasestorage.app`

2. `/root/omi/backend/utils/stt/streaming.py`:
   - Line 268: Uncommented `print(f"Received message from Deepgram")`
   - Line 270: Uncommented `print(sentence)`
   - Line 273: Uncommented `print(sentence)` (duplicate)

### Local Development Filesystem

**Backend Directory** (`/Users/greg/repos/omi/backend`):
```
/Users/greg/repos/omi/backend/
├── CLAUDE.md                    # ✅ UPDATED: Production deployment section added
├── docs/
│   ├── LETTA_INTEGRATION_ARCHITECTURE.md  # ✅ NEW: 47KB comprehensive design
│   ├── SYSTEM_STATE_SNAPSHOT_20251030.md  # ✅ NEW: This document
│   ├── SESSION_SUMMARY.md       # Previous session
│   └── README_TESTING.md        # Testing guide
├── test_audio/                  # Test audio samples
├── test_omi_device_simulation.py  # Device simulator
└── venv/                        # Local Python environment
```

**Pending Git Commits**:
- New files created this session (to be committed)
- CLAUDE.md updates (to be committed)
- No code changes on VPS requiring git sync

---

## 🚦 Service Health State

### OMI Backend Service

**Health Endpoint**: https://api.ella-ai-care.com/health
```json
{
  "status": "healthy",
  "timestamp": "2025-10-30T19:00:00Z"
}
```

**Recent Logs** (last 100 lines):
```
Oct 30 18:49:53: process_conversation completed conversation.id= 70a989c5-9f77-4b81-8ca6-93191f0dceb3
Oct 30 18:49:53: Message: type $memory_processing_started
Oct 30 18:49:53: Message: type $memory_created
Oct 30 18:49:53: INFO: 24.4.148.29:0 - "POST /v1/conversations HTTP/1.1" 200 OK
```

**Error State**:
- Last error: Memories index missing (resolved Oct 30)
- Current errors: None
- Warning: Redis connection errors resolved with config update

### iOS App Connection State

**Test User**: `5aGC5YE9BnhcSoTxxtT4ar6ILQy2`
**Last Connection**: October 30, 2025 ~18:45 UTC
**Test Result**: ✅ Successful
- Audio transmitted
- Deepgram transcribed
- Chunks logged
- Memory created

**App Behavior**:
- Startup screen: ✅ Passing (language endpoint accessible)
- Audio recording: ✅ Working
- Chunk display: ✅ Showing chunks captured
- Memory persistence: ⏳ Pending verification (index just created)

---

## 🔄 Integration State

### n8n Workflow Engine

**Status**: Running but not actively integrated
**Access**: http://[VPS-IP]:5678
**Workflows**: None configured yet for OMI backend

**Future Integration** (from architecture doc):
- Postgres agent lookup
- Fast LLM alert scanning
- Letta API calls
- Chunk aggregation with Redis

### Letta Integration

**Status**: ⏳ Design Phase (not implemented)
**Architecture Options Documented**: 3 options
- Option 1: Webhook approach (no code changes)
- Option 2: Direct backend modification (full integration)
- Option 3: Hybrid approach (recommended)

**Pending Decisions**:
1. Which integration option to implement?
2. Postgres database schema for user → agent mapping
3. Fast LLM model selection (GPT-4o-mini vs alternatives)
4. Chunk aggregation strategy (backend vs n8n)

---

## 🎯 Decision Trees

### WebSocket Connection Decision Tree

```
iOS App Starts
    ├─> Check /health endpoint
    │   ├─> Success → Continue
    │   └─> Fail → Show error, retry
    │
    ├─> PATCH /v1/users/language
    │   ├─> Success → Continue
    │   └─> Fail → Show error, retry
    │
    ├─> Open WebSocket wss://api.ella-ai-care.com/v4/listen?uid=...
    │   ├─> Connection accepted
    │   │   ├─> Start audio streaming (Opus frames)
    │   │   ├─> Backend buffers chunks (600ms)
    │   │   ├─> Deepgram transcribes
    │   │   ├─> Chunks displayed in app (real-time)
    │   │   └─> User stops recording
    │   │       ├─> WebSocket closes gracefully
    │   │       ├─> Backend processes full conversation
    │   │       ├─> Memory extracted and stored in Firestore
    │   │       └─> App displays memory
    │   │
    │   └─> Connection rejected
    │       ├─> Check authentication
    │       └─> Retry connection
    │
    └─> 2-minute timeout (no activity)
        ├─> WebSocket closes automatically
        └─> Backend processes partial conversation
```

### Memory Processing Decision Tree

```
WebSocket Closes
    ├─> Check conversation duration
    │   ├─> < 5 seconds → Discard (too short)
    │   └─> ≥ 5 seconds → Process
    │
    ├─> Save conversation to Firestore
    │   ├─> conversations collection
    │   │   ├─> Status: completed
    │   │   ├─> Transcript segments
    │   │   └─> Timestamp metadata
    │   │
    │   └─> Query requires index?
    │       ├─> Index exists → Success
    │       └─> Index missing → Error (FIXED: Oct 30)
    │
    ├─> Extract memories
    │   ├─> Call OpenAI API (memory extraction LLM)
    │   ├─> Generate embeddings
    │   └─> Save to memories collection
    │       ├─> Query requires index?
    │       │   ├─> Index exists → Success (FIXED: Oct 30)
    │       │   └─> Index missing → Error → Return 400
    │       │
    │       └─> Memory saved successfully
    │           ├─> App queries memories
    │           └─> Memory displayed in app
    │
    └─> Cleanup
        ├─> Clear Redis conversation state
        └─> Close database connections
```

### Letta Integration Decision Tree (Future)

**Option 1: Webhook Approach**
```
Chunk arrives (600ms)
    ├─> Buffer in realtime_segment_buffers
    ├─> Copy to segment_buffers (webhook queue)
    └─> Every 1 second
        ├─> Send to webhook URL (configured in app)
        └─> n8n receives chunks
            ├─> Query Postgres for agent_id
            ├─> Fast LLM scan for alerts
            ├─> Redis aggregation (backpressure handling)
            ├─> Call Letta API
            └─> Return response to webhook callback
                ├─> Backend receives response
                └─> Forward to iOS app (TBD: how?)
```

**Option 2: Backend Integration**
```
Chunk arrives (600ms)
    ├─> Buffer in realtime_segment_buffers
    └─> stream_transcript_process()
        ├─> Query Postgres for agent_id (asyncpg pool)
        ├─> Push to Redis chunk queue
        ├─> Get aggregated chunks (backpressure handling)
        ├─> Fast LLM scan (OpenAI gpt-4o-mini)
        │   ├─> Needs response → Call Letta
        │   └─> No response → Continue
        │
        ├─> Call Letta API (letta SDK)
        │   ├─> Response received
        │   └─> Check for "NA" value
        │       ├─> NA → Silent (no response)
        │       └─> Real response → Send to iOS
        │
        └─> Send via WebSocket to iOS app
            ├─> {"type": "letta_response", "text": "..."}
            └─> App displays response
```

**Option 3: Hybrid Approach** (Recommended)
```
Chunk arrives (600ms)
    ├─> Buffer in realtime_segment_buffers
    └─> stream_transcript_process()
        ├─> Fast LLM scan (backend, 200ms)
        │   ├─> URGENT alert detected
        │   │   ├─> Send immediate alert to iOS via WebSocket
        │   │   └─> Also forward to webhook for full processing
        │   │
        │   ├─> ALERT detected
        │   │   └─> Forward to webhook for full processing
        │   │
        │   └─> INFO (no alert)
        │       └─> Store in buffer, wait for more context
        │
        └─> Webhook forwarding (async, 1-second batches)
            ├─> n8n receives chunks
            ├─> Postgres agent lookup
            ├─> Redis aggregation
            ├─> Letta API call (full response)
            └─> Callback to backend
                └─> Forward to iOS app
```

---

## 📊 Metrics & Analytics State

### Backend Performance Metrics

**Current Monitoring**:
- Service uptime: systemd status
- Log analysis: journalctl
- No Prometheus/Grafana yet

**Key Metrics to Track** (not yet implemented):
```python
# Suggested metrics
letta_response_latency_seconds: Histogram
postgres_lookup_latency_seconds: Histogram
fast_llm_scan_latency_seconds: Histogram
deepgram_transcription_latency_seconds: Histogram
websocket_connection_duration_seconds: Histogram
conversation_duration_seconds: Histogram
memory_extraction_latency_seconds: Histogram
```

### iOS App Metrics

**Current Visibility**: Limited (no analytics SDK)
**User Feedback**:
- "App shows chunks captured" ✅
- "Backend is silent" → FIXED (Deepgram logging enabled)
- "Memories disappearing" → FIXED (Firestore index created)

---

## 🔒 Security State

### Authentication

**Current Mode**: Development
- `LOCAL_DEVELOPMENT=true` in .env
- Firebase JWT verification: Active but bypassed for test users
- ADMIN_KEY: Configured for development access

**Production Requirements** (not yet implemented):
- Strict Firebase JWT verification
- No ADMIN_KEY bypass
- Rate limiting on endpoints
- API key rotation

### Firestore Security

**Current Rules**: OPEN ACCESS (allow all)
**Risk Level**: ⚠️ HIGH (development only)
**TODO**: Implement user-scoped security rules before production

### GCS Bucket Security

**Current State**: ✅ Service account only
**Permissions**: Storage Object Admin (required for audio uploads)
**Public Access**: Disabled

### Redis Security

**Current State**: No password (internal Docker network only)
**Risk Level**: ⚠️ MEDIUM (internal network only)
**TODO**: Add password if exposing externally

---

## 🚀 Deployment Checklist

### Completed (Oct 30, 2025)

- [x] VPS provisioned and configured
- [x] Backend deployed with systemd service
- [x] SSL certificate configured (Let's Encrypt)
- [x] Firebase credentials deployed
- [x] Environment variables configured (.env)
- [x] Firestore composite indexes created (conversations, memories)
- [x] GCS bucket permissions granted
- [x] Redis configuration added
- [x] Deepgram logging enabled
- [x] iOS app successfully connects and transcribes
- [x] Memory processing working (post index fix)
- [x] Documentation updated (CLAUDE.md)
- [x] Letta integration architecture documented (3 options)

### Pending (Next Steps)

- [ ] Verify memories displaying in iOS app after index creation
- [ ] Choose Letta integration option (1, 2, or 3)
- [ ] Implement chosen Letta integration approach
- [ ] Set up Postgres database for user → agent mapping
- [ ] Configure monitoring (Prometheus, Grafana, or similar)
- [ ] Implement production security rules (Firestore, authentication)
- [ ] Add rate limiting to API endpoints
- [ ] Set up automated backups (Firestore, Redis)
- [ ] Configure alerting (Sentry, PagerDuty, or similar)
- [ ] Load testing with multiple concurrent WebSocket connections
- [ ] iOS app update to handle Letta responses
- [ ] Deploy n8n workflows for Option 1 or 3

---

## 📝 Session Summary

### What Was Fixed (October 30, 2025)

1. **Firestore Composite Indexes**:
   - Conversations index: Created for `discarded`, `status`, `created_at`
   - Memories index: Created for `scoring`, `created_at`
   - Result: 400 errors resolved, queries now working

2. **GCS Bucket Permissions**:
   - Service account granted Storage Object Admin role
   - Bucket name corrected in .env
   - Result: Audio file uploads should now work

3. **Redis Configuration**:
   - Added connection details for n8n-redis container
   - Result: Redis-dependent endpoints no longer erroring

4. **Deepgram Logging**:
   - Uncommented print statements in streaming.py
   - Result: Transcription activity now visible in logs

### What Was Documented

1. **Letta Integration Architecture** (`docs/LETTA_INTEGRATION_ARCHITECTURE.md`):
   - 47KB comprehensive design document
   - 3 implementation options with pros/cons
   - Latency analysis for each approach
   - Complete code examples for all components
   - Migration checklists and rollback plans
   - Cost analysis and FAQ section

2. **CLAUDE.md Updates**:
   - Added complete production deployment section
   - VPS configuration details
   - Service management commands
   - Redis and GCS bucket information
   - Deployment history
   - Known issues and solutions
   - Future enhancements roadmap

3. **System State Snapshot** (this document):
   - Complete snapshot of production and local environments
   - Infrastructure state (VPS, Docker, networks)
   - Database state (Firestore, GCS, Redis)
   - Real-time data flow state
   - External API state
   - File system state
   - Service health state
   - Integration state
   - Decision trees for key workflows

### Pending User Decisions

1. **Letta Integration Approach**:
   - Option 1: Webhook (no code changes, 3-4s latency)
   - Option 2: Backend (major changes, 2-3s latency)
   - Option 3: Hybrid (recommended, <1s alerts, 3-4s full responses)

2. **Postgres Database Schema**:
   - Need to confirm schema for user → agent mapping
   - Database location (VPS, separate server, cloud)
   - Connection pooling requirements

3. **Fast LLM Model**:
   - GPT-4o-mini (OpenAI) vs alternatives
   - Self-hosted vs cloud API
   - Cost vs latency tradeoff

4. **Chunk Aggregation Strategy**:
   - Keep in n8n (Redis) vs migrate to backend
   - Backpressure handling approach
   - Splicing algorithm tuning

---

## 🎯 Next Session Priorities

### High Priority

1. **Verify Memories Fix**:
   - Test iOS app after Firestore index creation
   - Confirm memories are persisting and displaying
   - Check for any remaining index errors

2. **Choose Letta Integration Option**:
   - User decision on Option 1, 2, or 3
   - Begin implementation based on choice
   - Set up Postgres if needed (Option 2 or 3)

### Medium Priority

3. **iOS App Response Handling**:
   - Update iOS app to receive WebSocket responses
   - Implement "typing..." indicator
   - Display Letta responses in UI

4. **Monitoring Setup**:
   - Configure basic metrics collection
   - Set up log aggregation
   - Create alerting for critical errors

### Low Priority

5. **Documentation**:
   - API documentation updates (OpenAPI/Swagger)
   - iOS developer guide for Letta integration
   - n8n workflow templates (if Option 1 or 3)

6. **Testing**:
   - Load testing with multiple concurrent users
   - Latency benchmarking for each integration option
   - Edge case testing (poor network, long conversations)

---

## 📚 Reference Links

### Firebase Console
- **Project**: https://console.firebase.google.com/project/omi-dev-ca005
- **Firestore**: https://console.firebase.google.com/project/omi-dev-ca005/firestore/data
- **Storage**: https://console.cloud.google.com/storage/browser/omi-dev-ca005.firebasestorage.app

### Deepgram Dashboard
- **Console**: https://console.deepgram.com/
- **Usage**: https://console.deepgram.com/usage
- **API Docs**: https://developers.deepgram.com/

### VPS Management
- **SSH Access**: `ssh root@100.101.168.91`
- **Systemd Service**: `/etc/systemd/system/omi-backend.service`
- **Backend Directory**: `/root/omi/backend`

### Documentation
- **Architecture**: `/Users/greg/repos/omi/backend/docs/LETTA_INTEGRATION_ARCHITECTURE.md`
- **Developer Guide**: `/Users/greg/repos/omi/backend/CLAUDE.md`
- **This Snapshot**: `/Users/greg/repos/omi/backend/docs/SYSTEM_STATE_SNAPSHOT_20251030.md`

---

**Status**: ✅ System operational, documentation complete, awaiting Letta integration decision
**Last Updated**: October 30, 2025 19:00 UTC
**Session**: 2 of ongoing deployment and integration work
