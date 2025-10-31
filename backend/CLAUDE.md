# OMI Backend - Developer Guide

**Last Updated**: October 30, 2025
**Branch**: `feature/backend-infrastructure`
**Status**: ✅ Production deployment on VPS + Local development environment

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

**Last Updated**: October 28, 2025
**Maintained By**: Development Team
**Status**: ✅ Production-ready backend infrastructure, ready for Letta integration and deployment

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
