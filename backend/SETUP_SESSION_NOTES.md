# OMI Backend Setup - Session Notes
## Date: October 27, 2025

### ✅ Completed Tonight (Session 1)

#### Infrastructure Setup
- ✅ Created new branch: `feature/backend-infrastructure`
- ✅ Python 3.11.6 virtual environment created
- ✅ All dependencies installed (~200+ packages, ~500 MB)
- ✅ Homebrew prerequisites: ffmpeg, opus
- ✅ Opus library path configured for macOS

#### Firebase Configuration
- ✅ Firebase project created: `omi-dev-ca005`
- ✅ Service account credentials downloaded: `google-credentials.json`
- ✅ Firestore database initialized (test mode, us-central1)
- ✅ Storage buckets configured

#### API Keys Obtained
- ✅ OpenAI API key
- ✅ Deepgram API key (free $200 credit)
- ✅ Pinecone API key + index created (`omi-dev`)
- ✅ Hugging Face token

#### Configuration Files
- ✅ `.env` file created with all credentials
- ✅ `start_server.py` helper script created
- ✅ Typesense dummy config (optional service)

---

### 🔴 Blocked - Waiting for WiFi

#### Issue Encountered
Backend requires downloading ML models on first startup:
- **Silero VAD**: ~50-100 MB (voice activity detection)
- **PyAnnote.Audio**: ~500 MB - 1 GB (speaker diarization)
- **Total**: ~1-2 GB download

**Error**: GitHub API rate limiting (401 Unauthorized) when downloading torch.hub models

#### Why We Stopped
- On cellular network with limited bandwidth
- Full end-to-end testing requires app modifications (separate session)
- Better to do complete setup on WiFi

---

### 📋 Next Session - When You Have WiFi

#### Step 1: Get Backend Running on Mac M4 (30 min)

```bash
# 1. Navigate to backend directory
cd ~/repos/omi/backend
source venv/bin/activate

# 2. Set library paths and start server
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/opus/lib:$DYLD_LIBRARY_PATH"
export SSL_CERT_FILE=$(python3 -m certifi)
python start_server.py

# This will download ML models (~1-2 GB one-time download)
# Models cached in: ~/.cache/torch/hub/
```

**What to expect:**
- First startup: 5-10 min (downloading models)
- Subsequent startups: ~10-30 seconds
- Server runs on: http://localhost:8000

#### Step 2: Test Backend APIs (15 min)

```bash
# Health check
curl http://localhost:8000/health

# Check Swagger docs
open http://localhost:8000/docs

# Test user endpoint (requires auth token)
curl http://localhost:8000/v1/users/me \
  -H "Authorization: Bearer YOUR_TOKEN"
```

#### Step 3: Deploy to VPS (45 min)

**VPS Specs:**
- Label: Letta
- OS: Ubuntu 22.04 x64
- RAM: 2048 MB
- CPU: 1 vCPU
- Storage: 55 GB SSD
- **Perfect for OMI backend with 2GB RAM!**

```bash
# On VPS:
1. Install Python 3.11, ffmpeg, opus
2. Clone omi repo
3. Copy .env file (update IPs/URLs)
4. Run setup script
5. Configure systemd service for auto-start
6. Set up nginx reverse proxy (optional)
7. Configure firewall
```

---

### 🏗️ Architecture Decisions Made

#### Deployment Strategy: Mac + VPS Hybrid

**Mac M4 Pro (48GB RAM) - Development:**
- Full backend with all ML features
- Fast iteration and testing
- Speaker diarization with Metal GPU acceleration

**VPS (2GB RAM) - Production:**
- Lightweight backend or full backend
- 24/7 availability
- Connects to cloud APIs (Deepgram, OpenAI)
- Can run Letta integration
- Handles OMI device connections

#### Key Insights

**What Doesn't Need Modal:**
- FastAPI backend runs standalone
- Modal code is optional (only for their cloud deployment)
- Can deploy anywhere: Mac, VPS, Docker, cloud

**What Cloud APIs Handle (No Local Compute):**
- Deepgram: Speech-to-text (~$0.0043/min)
- OpenAI: LLM processing
- Firebase: Database/auth
- Pinecone: Vector search

**What Runs Locally (If Enabled):**
- Silero VAD: Voice activity detection
- PyAnnote: Speaker diarization
- Audio processing: ffmpeg conversions

**Optional Services (Can Disable):**
- Typesense: Search engine (we set dummy config)
- Redis: Caching (left empty)

---

### 🎯 Future Sessions

#### Session 2: Backend Complete Setup (When on WiFi)
- Download ML models
- Test all API endpoints
- Deploy to VPS
- Configure production settings

#### Session 3: iOS App Integration
- Modify Flutter app to use custom backend URL
- Rebuild app
- Deploy to iPhone
- Test full device → backend → Letta flow

#### Session 4: WhisperKit On-Device STT (Optional)
- Integrate WhisperKit into iOS app
- Move transcription to device (100% private)
- Eliminate Deepgram costs
- Perfect for HIPAA compliance

---

### 📁 Important Files

**Created This Session:**
- `backend/.env` - All API keys and configuration
- `backend/google-credentials.json` - Firebase service account
- `backend/start_server.py` - Helper script with proper env loading
- `backend/venv/` - Python virtual environment

**DO NOT COMMIT (already in .gitignore):**
- `.env` - Contains secrets
- `google-credentials.json` - Contains private key
- `venv/` - Virtual environment

**Safe to Commit:**
- `start_server.py`
- This notes file
- Updated documentation

---

### 🔑 Credentials Reference

**Firebase Project:**
- Project ID: `omi-dev-ca005`
- Region: us-central1
- Credentials: `./google-credentials.json`

**Pinecone:**
- Index: `omi-dev`
- Dimensions: 3072
- Metric: cosine
- Cloud: AWS us-east-1

**API Services:**
- OpenAI: Configured
- Deepgram: Configured ($200 free credit)
- Hugging Face: Configured

---

### 💡 Quick Start Commands (Next Session)

```bash
# Start backend on Mac
cd ~/repos/omi/backend
source venv/bin/activate
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/opus/lib:$DYLD_LIBRARY_PATH"
python start_server.py

# Test health
curl http://localhost:8000/health

# View API docs
open http://localhost:8000/docs
```

---

### 📊 Estimated Times (Next Session)

- ML model download: 10-15 min (one-time, WiFi dependent)
- Backend startup test: 5 min
- API endpoint testing: 10 min
- VPS deployment: 45-60 min
- **Total**: ~90 min to fully operational

---

### 🎓 What We Learned

1. **OMI backend is ML-heavy** - Designed for production with speaker diarization
2. **Modal is optional** - FastAPI runs standalone anywhere
3. **M4 Mac is perfect** - Can run everything locally with 48GB RAM
4. **VPS (2GB) will work** - With lightweight config or cloud STT
5. **First startup downloads models** - Need WiFi, ~1-2 GB one-time
6. **Architecture is flexible** - Can deploy Mac-only, VPS-only, or hybrid

---

### ❓ Questions for Next Session

- [ ] Is Letta already running on the VPS? What port?
- [ ] Prefer Mac-only or VPS deployment first?
- [ ] Want to enable speaker diarization? (Uses more RAM/CPU)
- [ ] Need to set up Tailscale/ngrok for remote access?

---

**Status**: Ready to continue when on WiFi! 🚀
