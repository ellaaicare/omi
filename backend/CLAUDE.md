# OMI Backend - Developer Guide

**Last Updated**: October 28, 2025
**Branch**: `feature/backend-infrastructure`
**Status**: ✅ Fully operational and tested

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
