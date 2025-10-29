# Phase 1: Cloud-Based VPS Deployment

**Goal**: iOS testing with fully cloud-based backend
**Timeline**: 10-20 minutes
**Infrastructure**: VPS only (no Mac needed)

---

## ✅ What Runs Where

### VPS (api.ella-ai-care.com):
- FastAPI backend (lightweight)
- WebSocket server for audio streaming
- Request routing and CORS handling

### Cloud Services (via APIs):
- Deepgram - Speech-to-text transcription
- OpenAI - GPT processing
- Firestore - Database and storage
- Pinecone - Vector database (optional)

### NOT NEEDED for Phase 1:
- ❌ Local ML models (PyAnnote, WhisperX)
- ❌ Mac M4 Pro or M1 iMac
- ❌ Heavy GPU processing

---

## 📊 Resource Requirements

**VPS (2GB RAM, 1 vCPU):**
- FastAPI backend: ~300-500MB RAM
- Python process: ~50-100MB
- nginx: ~10MB
- **Total: ~500MB** (plenty of headroom)

**Existing services safe:**
- n8n, Letta, Redis, Postgres still have 1.5GB available

---

## 🚀 Deployment Steps

### Step 1: Check VPS Repo
```bash
ssh user@your-vps-ip
cd ~/omi  # If repo already exists
git status
git pull origin feature/ios-backend-integration

# If repo doesn't exist:
# git clone https://github.com/ellaaicare/omi.git
# cd omi && git checkout feature/ios-backend-integration
```

### Step 2: Setup Backend on VPS
```bash
cd ~/omi/backend

# Install Python 3.11 (if not present)
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install system dependencies (lightweight)
sudo apt install -y libopus-dev opus-tools ffmpeg
```

### Step 3: Create .env (Use Cloud APIs)
```bash
cd ~/omi/backend
nano .env
```

**Paste** (use your actual API keys from Mac's .env):
```bash
# Cloud API Keys (required)
OPENAI_API_KEY=your-actual-openai-key
DEEPGRAM_API_KEY=your-actual-deepgram-key
PINECONE_API_KEY=your-actual-pinecone-key

# Optional (for model downloads - NOT NEEDED for Phase 1)
# HUGGINGFACE_TOKEN=your-token
# GITHUB_TOKEN=your-token

# Firebase (cloud database)
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=/home/user/omi/backend/google-credentials.json
BUCKET_SPEECH_PROFILES=omi-dev-ca005.appspot.com

# Development mode
LOCAL_DEVELOPMENT=true
ADMIN_KEY=dev_testing_key_12345

# Optional services
TYPESENSE_HOST=localhost
TYPESENSE_HOST_PORT=8108
TYPESENSE_API_KEY=dummy_key_for_dev
```

**Note**: Update `/home/user/` with your VPS username

### Step 4: Transfer Firebase Credentials
```bash
# On Mac (new terminal):
scp /Users/greg/repos/omi/backend/google-credentials.json user@your-vps-ip:~/omi/backend/
```

### Step 5: Test Backend Locally on VPS
```bash
# On VPS, in ~/omi/backend with venv activated:
python start_server.py

# Should see: "Application startup complete"
# Press Ctrl+C to stop
```

### Step 6: Create Systemd Service
```bash
# On VPS:
sudo nano /etc/systemd/system/omi-backend.service
```

**Paste** (update username if needed):
```ini
[Unit]
Description=OMI Backend FastAPI Service (Cloud-Based)
After=network.target

[Service]
Type=simple
User=user
WorkingDirectory=/home/user/omi/backend
Environment="PATH=/home/user/omi/backend/venv/bin"
ExecStart=/home/user/omi/backend/venv/bin/python start_server.py
Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=omi-backend

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable omi-backend
sudo systemctl start omi-backend
sudo systemctl status omi-backend
# Should show: "active (running)"
```

### Step 7: Configure nginx
```bash
# On VPS:
sudo nano /etc/nginx/sites-available/omi-backend
```

**Paste**:
```nginx
server {
    listen 80;
    server_name api.ella-ai-care.com;

    # Increase timeouts for WebSocket
    proxy_read_timeout 300s;
    proxy_connect_timeout 75s;

    # WebSocket upgrade headers
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    # Standard headers
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Proxy to local backend
    location / {
        proxy_pass http://localhost:8000;
    }
}
```

```bash
# Enable and test
sudo ln -s /etc/nginx/sites-available/omi-backend /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Step 8: Get SSL Certificate
```bash
# On VPS:
sudo certbot --nginx -d api.ella-ai-care.com

# Follow prompts (same email as n8n cert)
```

### Step 9: Test Everything
```bash
# From anywhere:
curl https://api.ella-ai-care.com/health
# Should return: {"status":"ok"}

# Open in browser:
# https://api.ella-ai-care.com/docs
# Should see FastAPI Swagger UI
```

---

## 📱 iOS App Configuration

**Backend URL:**
```
https://api.ella-ai-care.com
```

**WebSocket URL:**
```
wss://api.ella-ai-care.com/v4/listen
```

---

## 🧪 Testing Audio Pipeline

**From Mac (or any device):**
```bash
# Install websocket client
pip install websockets

# Test WebSocket connection
python test_omi_device_simulation.py --audio-file test_audio/pyannote_sample.wav
```

**Expected flow:**
1. Audio sent to VPS backend
2. Backend forwards to Deepgram API (cloud transcription)
3. Transcription saved to Firestore (cloud database)
4. Results returned to iOS app

---

## 💰 Cost Analysis (Phase 1)

**Free Tiers:**
- Firebase/Firestore: Free tier (plenty for testing)
- OpenAI API: Pay-per-use (minimal cost for testing)
- Deepgram API: Pay-per-use (~$0.0125/min)

**Testing costs (10 hours of audio):**
- Deepgram: ~$7.50
- OpenAI: ~$1-5 (depends on usage)
- **Total**: ~$10-15 for extensive testing

**VPS costs:**
- No change (already paying for VPS)
- No RAM upgrade needed

---

## 🔍 Monitoring

**Backend logs:**
```bash
sudo journalctl -u omi-backend -f
```

**nginx logs:**
```bash
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log
```

**Resource usage:**
```bash
htop  # Should show backend using ~500MB RAM
```

---

## 🚨 What This Does NOT Include

**Phase 1 limitations:**
- ❌ **Not HIPAA compliant** - Audio/transcripts go to Deepgram/OpenAI
- ❌ **No speaker diarization** - Can't identify multiple speakers
- ❌ **Ongoing API costs** - Per-minute charges for Deepgram
- ❌ **No offline capability** - Requires internet for transcription

**When you need these features**: Move to Phase 2 (hybrid with local ML)

---

## ✅ Phase 1 Success Criteria

- [ ] Backend running on VPS
- [ ] HTTPS working (api.ella-ai-care.com)
- [ ] WebSocket accepting connections
- [ ] Audio transcription via Deepgram working
- [ ] Firestore saving conversations
- [ ] iOS app can connect from any device
- [ ] No VPN/special software needed
- [ ] Existing VPS services still working

---

## 🔄 Future Phases

### Phase 2: Local ML Processing
**When**: Need HIPAA compliance or cost reduction
**How**:
- Keep VPS as reverse proxy
- Add Mac M4 Pro or M1 iMac via Tailscale
- Run WhisperX locally (replace Deepgram)
- Run PyAnnote for speaker diarization

### Phase 3: Full Production
**When**: Ready for real users
**How**:
- Set LOCAL_DEVELOPMENT=false
- Restrict Firestore rules
- Add rate limiting
- Set up monitoring/alerting
- Configure backups

---

## 💡 Key Insight

**You don't need heavy ML models for Phase 1!**

The backend is just a lightweight FastAPI app that:
- Receives audio via WebSocket
- Forwards to cloud APIs (Deepgram, OpenAI)
- Saves results to Firestore

All the "heavy lifting" happens in cloud services you already pay for.

---

**Total setup time**: 10-20 minutes
**VPS RAM usage**: ~500MB (plenty of headroom)
**Cost**: Free (using existing VPS + API free tiers)
**Result**: iOS app can test from any device

---

**Last Updated**: October 29, 2025
**Phase**: 1 - Cloud-Based VPS Deployment
**Next Phase**: Local ML processing (when HIPAA needed)
