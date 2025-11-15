# VPS Deployment Quickstart - Git-Based

**Domain**: api.ella-ai-care.com
**Branch**: feature/ios-backend-integration
**Method**: Git-based deployment (industry standard)

---

## 🚀 One-Time VPS Setup (30 minutes)

### Prerequisites
- ✅ DNS A record created: `api.ella-ai-care.com` → VPS IP
- ✅ SSH access to VPS
- ✅ Git repo pushed to ellaaicare/omi

---

## Step 1: SSH to VPS & Clone Repo

```bash
# SSH to VPS
ssh user@your-vps-ip

# Clone repo (if not already cloned)
git clone https://github.com/ellaaicare/omi.git
cd omi

# Checkout the correct branch
git checkout feature/ios-backend-integration

# Pull latest changes
git pull
```

---

## Step 2: Create Environment Files

### A. Copy Firebase Credentials

From Mac, transfer google-credentials.json to VPS:
```bash
# On Mac M4 Pro
scp /Users/greg/repos/omi/backend/google-credentials.json user@your-vps-ip:~/omi/backend/
```

### B. Create .env File

```bash
# On VPS
cd omi/backend
nano .env
```

Paste this content (**use your actual API keys** - get them from your .env on Mac):
```bash
# API Keys (replace with your actual keys from Mac .env file)
OPENAI_API_KEY=sk-proj-YOUR-ACTUAL-OPENAI-KEY-HERE
DEEPGRAM_API_KEY=your-deepgram-api-key-here
PINECONE_API_KEY=pcsk_your-pinecone-api-key-here
HUGGINGFACE_TOKEN=hf_your-huggingface-token-here
GITHUB_TOKEN=ghp_your-github-personal-access-token-here

# Firebase
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=/home/user/omi/backend/google-credentials.json
BUCKET_SPEECH_PROFILES=omi-dev-ca005.appspot.com

# Development Settings
LOCAL_DEVELOPMENT=true
ADMIN_KEY=dev_testing_key_12345

# Optional Services
TYPESENSE_HOST=localhost
TYPESENSE_HOST_PORT=8108
TYPESENSE_API_KEY=dummy_key_for_dev
```

**Important**: Update `/home/user/` with your actual VPS username

Save and exit (Ctrl+X, Y, Enter)

---

## Step 3: Run Deployment Script

```bash
# On VPS, in omi/backend directory
chmod +x deploy/vps_deploy.sh
./deploy/vps_deploy.sh
```

This script will:
- ✅ Pull latest git changes
- ✅ Install Python 3.11 and dependencies
- ✅ Create virtual environment
- ✅ Install system libraries (Opus, ffmpeg)
- ✅ Download ML models (~17GB, 30-60 minutes)

**Wait for completion** - ML model download takes time on first run.

---

## Step 4: Setup Systemd Service (One-Time)

```bash
# On VPS, in omi/backend directory
chmod +x deploy/setup_systemd.sh
sudo ./deploy/setup_systemd.sh
```

This creates:
- ✅ Systemd service for auto-restart
- ✅ Automatic startup on boot
- ✅ Logging to journalctl

---

## Step 5: Setup nginx Reverse Proxy (One-Time)

```bash
# On VPS, in omi/backend directory
chmod +x deploy/setup_nginx.sh
sudo ./deploy/setup_nginx.sh
```

This configures:
- ✅ nginx reverse proxy
- ✅ WebSocket support
- ✅ Routes api.ella-ai-care.com → localhost:8000

---

## Step 6: Get SSL Certificate

```bash
# On VPS
sudo certbot --nginx -d api.ella-ai-care.com
```

Follow prompts:
- Use same email as n8n certificate
- Agree to terms
- Choose redirect HTTP → HTTPS

**Certbot auto-configures nginx for HTTPS**

---

## Step 7: Start Backend Service

```bash
# On VPS
sudo systemctl start omi-backend

# Check status
sudo systemctl status omi-backend
# Should show: "active (running)"

# View logs
sudo journalctl -u omi-backend -f
```

---

## Step 8: Test Deployment

### A. Test from Browser

Visit: https://api.ella-ai-care.com/docs

Should see FastAPI Swagger documentation

### B. Test Health Endpoint

```bash
curl https://api.ella-ai-care.com/health
# Returns: {"status":"ok"}
```

### C. Test from iOS App

Configure iOS app:
```
Backend URL: https://api.ella-ai-care.com
WebSocket URL: wss://api.ella-ai-care.com/v4/listen
```

Start recording, verify transcription appears.

---

## 🔄 Future Deployments (Git-Based)

When you make backend changes:

```bash
# On Mac: commit and push changes
cd /Users/greg/repos/omi
git add .
git commit -m "feat: your changes"
git push fork feature/ios-backend-integration

# On VPS: pull and restart
ssh user@your-vps-ip
cd omi/backend
git pull
./deploy/vps_deploy.sh
sudo systemctl restart omi-backend
```

**No more manual file copying!** Just git push/pull.

---

## 📊 Useful Commands

```bash
# Service management
sudo systemctl status omi-backend    # Check status
sudo systemctl start omi-backend     # Start service
sudo systemctl stop omi-backend      # Stop service
sudo systemctl restart omi-backend   # Restart service

# Logs
sudo journalctl -u omi-backend -f    # Real-time logs
sudo journalctl -u omi-backend -n 100  # Last 100 lines

# nginx
sudo systemctl status nginx          # Check nginx
sudo nginx -t                        # Test config
sudo systemctl reload nginx          # Reload config

# Resource monitoring
htop                                 # CPU/RAM usage
df -h                                # Disk space
free -h                              # Memory usage

# Check port usage
sudo netstat -tlnp | grep 8000       # Backend port
sudo netstat -tlnp | grep 443        # HTTPS port
```

---

## 🐛 Troubleshooting

### Backend Won't Start

```bash
# Check logs for errors
sudo journalctl -u omi-backend -n 50

# Common issues:
# - Missing .env file
# - Wrong path in GOOGLE_APPLICATION_CREDENTIALS
# - Port 8000 already in use
# - Opus library not installed
```

### SSL Certificate Issues

```bash
# Check certificate status
sudo certbot certificates

# Renew certificate
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

### WebSocket Connection Fails

```bash
# Check nginx config
sudo nginx -t

# View nginx error logs
sudo tail -f /var/log/nginx/error.log

# Verify backend is running
curl http://localhost:8000/health
```

### Out of Memory (VPS has 2GB RAM)

```bash
# Check memory usage
free -h

# Restart backend to free memory
sudo systemctl restart omi-backend

# Consider upgrading VPS to 4GB RAM if issues persist
```

---

## 🔐 Security Notes

**Current Setup (Development)**:
- ✅ HTTPS/SSL enabled (Let's Encrypt)
- ✅ Firewall configured (only 22, 80, 443 open)
- ✅ Backend not directly exposed (nginx proxy)
- ⚠️ LOCAL_DEVELOPMENT=true (auth bypass)
- ⚠️ Firestore rules OPEN (7-day deadline)

**Before Production**:
- [ ] Set LOCAL_DEVELOPMENT=false
- [ ] Restrict Firestore security rules
- [ ] Remove/secure ADMIN_KEY
- [ ] Rotate all API keys
- [ ] Configure CORS whitelist
- [ ] Enable rate limiting
- [ ] Set up monitoring/alerting

See `docs/SECURITY_HIPAA_CHECKLIST.md` for complete list.

---

## 📈 Scaling Path

**Current**: Single VPS (2GB RAM, 1 vCPU)
- Good for: Development, testing, low traffic

**Next**: Upgrade to 4GB RAM
- When: Backend using >1GB consistently
- Cost: +$5-10/month

**Future**: Multi-instance deployment
- Load balancer + 2-3 VPS instances
- When: 100+ concurrent users
- Consider: Managed cloud (Railway, Fly.io)

---

**Last Updated**: October 29, 2025
**Status**: Production-ready for iOS testing
**Deployment Method**: Git-based (industry standard)
