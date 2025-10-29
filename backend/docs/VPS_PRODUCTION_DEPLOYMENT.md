# VPS Production Deployment - Public Testing Setup

**Created**: October 29, 2025
**Goal**: Deploy backend to VPS for realistic iOS testing (no VPN required)
**Target**: End-to-end testing with public test devices

---

## 🎯 Deployment Strategy: Reverse Proxy on VPS

### Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│                    INTERNET (Public)                       │
└────────────────────────────────────────────────────────────┘
                              │
                              │ HTTPS (443)
                              ▼
┌────────────────────────────────────────────────────────────┐
│                         VPS                                │
├────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────┐           │
│  │  nginx Reverse Proxy (Port 80/443)          │           │
│  │                                              │           │
│  │  api-dev.yourdomain.com → localhost:8000    │           │
│  │  otherdomain.com → localhost:5678 (n8n)     │           │
│  └─────────────────────────────────────────────┘           │
│                     │                                       │
│  ┌──────────────────┴────────────────────┐                │
│  │                                        │                │
│  ▼                                        ▼                │
│  ┌─────────────────┐         ┌──────────────────────┐     │
│  │ OMI Backend     │         │ Existing Services    │     │
│  │ :8000           │         │ - n8n                │     │
│  │                 │         │ - Letta              │     │
│  │ FastAPI         │         │ - Redis              │     │
│  │ WebSocket       │         │ - Postgres           │     │
│  │ Deepgram        │         │ - Tailscale          │     │
│  └─────────────────┘         └──────────────────────┘     │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

---

## 📦 Step 1: Transfer Backend to VPS

### A. Package Backend for VPS

On Mac M4 Pro:
```bash
cd /Users/greg/repos/omi/backend

# Create deployment package (exclude ML models - download on VPS)
tar -czf omi-backend-deploy.tar.gz \
  --exclude='venv' \
  --exclude='test_audio' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  .
```

### B. Transfer to VPS

```bash
# Copy to VPS (replace with your VPS details)
scp omi-backend-deploy.tar.gz user@your-vps-ip:/home/user/

# SSH into VPS
ssh user@your-vps-ip
```

### C. Extract and Setup on VPS

```bash
# On VPS
cd /home/user
mkdir -p omi-backend
tar -xzf omi-backend-deploy.tar.gz -C omi-backend/
cd omi-backend

# Install Python 3.11 (if not present)
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip -y

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install system dependencies
sudo apt install ffmpeg opus-tools libopus-dev -y
```

### D. Setup Environment Variables

```bash
# Create .env file on VPS
nano .env
```

Copy these variables (use your actual keys):
```bash
# API Keys
OPENAI_API_KEY=sk-proj-...
DEEPGRAM_API_KEY=6ba1c4953e79c5f8b66438769c6cd2deba33a8cd
PINECONE_API_KEY=pcsk_...
HUGGINGFACE_TOKEN=hf_...
GITHUB_TOKEN=ghp_...

# Firebase
FIREBASE_PROJECT_ID=omi-dev-ca005
GOOGLE_APPLICATION_CREDENTIALS=/home/user/omi-backend/google-credentials.json
BUCKET_SPEECH_PROFILES=omi-dev-ca005.appspot.com

# Development Settings (for testing)
LOCAL_DEVELOPMENT=true
ADMIN_KEY=dev_testing_key_12345

# Optional Services
TYPESENSE_HOST=localhost
TYPESENSE_HOST_PORT=8108
TYPESENSE_API_KEY=dummy_key_for_dev
```

### E. Transfer Firebase Credentials

```bash
# On Mac M4 Pro
scp /Users/greg/repos/omi/backend/google-credentials.json user@your-vps-ip:/home/user/omi-backend/
```

### F. Download ML Models on VPS

```bash
# On VPS, in omi-backend directory
source venv/bin/activate

# Download PyAnnote models (17GB - will take time)
python download_models.py

# Optional: Download WhisperX models
python download_whisper_models.py
```

**Note**: This will take 30-60 minutes on VPS depending on bandwidth.

---

## 🌐 Step 2: Configure Reverse Proxy (nginx)

### A. Install nginx

```bash
sudo apt update
sudo apt install nginx -y
```

### B. Create Backend Configuration

```bash
sudo nano /etc/nginx/sites-available/omi-backend
```

Add this configuration:
```nginx
# OMI Backend - HTTP (will upgrade to HTTPS with certbot)
server {
    listen 80;
    server_name api-dev.yourdomain.com;  # Replace with your subdomain

    # Increase timeouts for long-running WebSocket connections
    proxy_read_timeout 300s;
    proxy_connect_timeout 75s;

    # WebSocket upgrade headers
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    # Standard proxy headers
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Proxy all requests to backend
    location / {
        proxy_pass http://localhost:8000;
    }

    # Health check endpoint (optional)
    location /health {
        proxy_pass http://localhost:8000/health;
    }
}
```

### C. Enable Configuration

```bash
# Create symbolic link to enable site
sudo ln -s /etc/nginx/sites-available/omi-backend /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx
```

---

## 🔐 Step 3: Setup SSL Certificates (Let's Encrypt)

### A. Install Certbot

```bash
sudo apt install certbot python3-certbot-nginx -y
```

### B. Configure DNS First

**IMPORTANT**: Before running certbot, configure DNS A record:

1. Log into your domain registrar (GoDaddy, Namecheap, Cloudflare, etc.)
2. Add A record:
   ```
   Type: A
   Name: api-dev
   Value: [Your VPS IP Address]
   TTL: 3600 (or lowest available)
   ```
3. Wait 5-10 minutes for DNS propagation
4. Test: `ping api-dev.yourdomain.com` (should resolve to VPS IP)

### C. Obtain SSL Certificate

```bash
# Run certbot (will auto-configure nginx)
sudo certbot --nginx -d api-dev.yourdomain.com

# Follow prompts:
# - Enter email for renewal notifications
# - Agree to terms
# - Choose redirect HTTP to HTTPS (recommended)
```

### D. Test Auto-Renewal

```bash
# Dry run to verify auto-renewal works
sudo certbot renew --dry-run
```

**Certbot will automatically renew certificates every 90 days.**

---

## 🚀 Step 4: Run Backend as System Service

### A. Create Systemd Service

```bash
sudo nano /etc/systemd/system/omi-backend.service
```

Add this configuration:
```ini
[Unit]
Description=OMI Backend FastAPI Service
After=network.target

[Service]
Type=simple
User=user  # Replace with your VPS username
WorkingDirectory=/home/user/omi-backend
Environment="PATH=/home/user/omi-backend/venv/bin"
Environment="DYLD_LIBRARY_PATH=/usr/local/lib"
ExecStart=/home/user/omi-backend/venv/bin/python start_server.py
Restart=always
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=omi-backend

[Install]
WantedBy=multi-user.target
```

### B. Enable and Start Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service (start on boot)
sudo systemctl enable omi-backend

# Start service now
sudo systemctl start omi-backend

# Check status
sudo systemctl status omi-backend
```

### C. View Logs

```bash
# Real-time logs
sudo journalctl -u omi-backend -f

# Recent logs
sudo journalctl -u omi-backend -n 100

# Logs with timestamps
sudo journalctl -u omi-backend --since "10 minutes ago"
```

---

## 📱 Step 5: Configure iOS App

### Update Backend URL

In your iOS app configuration:
```
Backend URL: https://api-dev.yourdomain.com
WebSocket URL: wss://api-dev.yourdomain.com/v4/listen
```

**Note**: Use `wss://` (WebSocket Secure) instead of `ws://` with HTTPS.

---

## ✅ Step 6: Test End-to-End

### A. Test from Browser

```bash
# From any device, visit:
https://api-dev.yourdomain.com/docs

# Should see FastAPI Swagger documentation
```

### B. Test Health Endpoint

```bash
curl https://api-dev.yourdomain.com/health

# Should return:
# {"status":"ok"}
```

### C. Test WebSocket Connection

```bash
# Install websocat for testing (optional)
sudo apt install websocat -y

# Test WebSocket
websocat wss://api-dev.yourdomain.com/v4/listen?uid=123&language=en&sample_rate=16000&codec=opus&channels=1
```

### D. Test from iOS Device

1. Open OMI app on iPhone
2. Configure backend URL: `https://api-dev.yourdomain.com`
3. Start recording
4. Speak test phrase: "Testing OMI backend deployment on VPS"
5. Verify transcription appears

**Monitor VPS logs**:
```bash
sudo journalctl -u omi-backend -f
```

Should see:
```
INFO:     WebSocket /v4/listen?uid=... [accepted]
INFO:     connection open
```

---

## 🔍 Monitoring & Troubleshooting

### Check Service Status

```bash
# Backend service
sudo systemctl status omi-backend

# nginx
sudo systemctl status nginx

# View ports in use
sudo netstat -tlnp | grep -E '(8000|80|443)'
```

### Resource Monitoring

```bash
# Real-time monitoring
htop

# Memory usage
free -h

# Disk usage
df -h

# Check backend process
ps aux | grep python
```

### Common Issues

#### 1. Backend Won't Start

**Check logs**:
```bash
sudo journalctl -u omi-backend -n 50
```

**Common causes**:
- Missing environment variables
- Google credentials not found
- Port 8000 already in use
- Opus library not installed

**Fix**:
```bash
# Verify .env exists
cat /home/user/omi-backend/.env

# Check Google credentials
ls -la /home/user/omi-backend/google-credentials.json

# Kill conflicting process
sudo lsof -ti:8000 | xargs kill -9
```

#### 2. SSL Certificate Errors

**Check certificate**:
```bash
sudo certbot certificates
```

**Renew manually**:
```bash
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

#### 3. WebSocket Connection Fails

**Check nginx WebSocket config**:
```bash
sudo nginx -t
sudo tail -f /var/log/nginx/error.log
```

**Verify headers**:
```bash
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: test" \
  https://api-dev.yourdomain.com/v4/listen
```

#### 4. Out of Memory

**VPS has 2GB RAM - may need to limit resources**:

```bash
# Edit service to limit memory
sudo nano /etc/systemd/system/omi-backend.service

# Add under [Service]:
MemoryLimit=1G
MemoryMax=1.5G

# Reload
sudo systemctl daemon-reload
sudo systemctl restart omi-backend
```

---

## 🔒 Security Hardening

### A. Firewall Configuration

```bash
# Install ufw
sudo apt install ufw -y

# Allow SSH (IMPORTANT - don't lock yourself out!)
sudo ufw allow 22/tcp

# Allow HTTP/HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status
```

### B. Restrict Backend Port

Backend runs on localhost:8000 - **not directly accessible from internet**.
Only nginx can access it (proxied requests).

Verify:
```bash
# Should only show 127.0.0.1:8000
sudo netstat -tlnp | grep 8000
```

### C. Rate Limiting (nginx)

```bash
sudo nano /etc/nginx/sites-available/omi-backend
```

Add before server block:
```nginx
# Rate limiting zone
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {
    # ... existing config ...

    # Apply rate limit
    location / {
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://localhost:8000;
    }
}
```

Reload:
```bash
sudo systemctl reload nginx
```

---

## 📊 Resource Management

### Expected Resource Usage

**Backend Process**:
- RAM: 500MB-1GB (with ML models loaded)
- CPU: 10-30% (idle), up to 100% (during transcription)
- Disk: 20GB (code + ML models)

**VPS Capacity**:
- Total RAM: 2GB
- Available for backend: ~1GB (after OS and other services)
- **Recommendation**: Monitor closely, may need RAM upgrade

### Optimization Tips

1. **Lazy Load Models**: Don't load PyAnnote until needed
2. **Use Deepgram**: Cloud transcription reduces local CPU/RAM
3. **Limit Connections**: Set max WebSocket connections
4. **Restart Periodically**: Systemd auto-restart on crashes

---

## 🎯 Testing Checklist

- [ ] Backend deploys to VPS successfully
- [ ] Systemd service starts on boot
- [ ] nginx reverse proxy configured
- [ ] SSL certificate obtained (Let's Encrypt)
- [ ] DNS A record points to VPS
- [ ] Health endpoint returns 200 OK
- [ ] Swagger docs accessible via HTTPS
- [ ] WebSocket connection accepts from iOS
- [ ] Audio streaming works end-to-end
- [ ] Transcription appears in real-time
- [ ] Firestore persistence working
- [ ] Existing VPS services (n8n, Letta) still functional
- [ ] Logs accessible via journalctl
- [ ] Auto-restart on crash verified
- [ ] SSL auto-renewal tested

---

## 🔄 Rollback Plan

If deployment causes issues:

### Quick Rollback

```bash
# Stop backend service
sudo systemctl stop omi-backend
sudo systemctl disable omi-backend

# Remove nginx config
sudo rm /etc/nginx/sites-enabled/omi-backend
sudo systemctl reload nginx

# VPS returns to original state
```

### Cleanup

```bash
# Remove backend directory
rm -rf /home/user/omi-backend

# Remove systemd service
sudo rm /etc/systemd/system/omi-backend.service
sudo systemctl daemon-reload
```

---

## 📈 Migration to Production

Once testing is complete and ready for production:

### Changes Needed:

1. **Environment Variables**:
   ```bash
   # In .env
   LOCAL_DEVELOPMENT=false  # Enable real Firebase auth
   ADMIN_KEY=  # Remove or use secure production key
   ```

2. **Firestore Security Rules**:
   - Change from OPEN to user-scoped access
   - See `SECURITY_HIPAA_CHECKLIST.md`

3. **Domain**:
   - Change from `api-dev.yourdomain.com` to `api.yourdomain.com`
   - Update DNS A record
   - Obtain new SSL certificate

4. **Monitoring**:
   - Set up uptime monitoring (UptimeRobot, Pingdom)
   - Configure error alerting (email/SMS)
   - Add performance monitoring (New Relic, DataDog)

5. **Backups**:
   - Automated Firestore backups
   - VPS snapshot schedule
   - Environment variable backup (secure storage)

---

## 💰 Cost Analysis

### VPS Resources (Current):
- 2GB RAM, 1 vCPU, 55GB SSD
- Cost: ~$5-10/month (already have)
- **May need upgrade to 4GB RAM**: +$5-10/month

### Additional Costs:
- Domain name: $12/year (if needed)
- SSL certificate: $0 (Let's Encrypt free)
- Bandwidth: Included in VPS plan
- **Total**: $5-20/month

### Scaling Path:
- **Now**: Single VPS (2-4GB RAM)
- **100 users**: Add load balancer, 2-3 VPS instances
- **1000 users**: Migrate to managed cloud (Railway, Fly.io)
- **10,000 users**: Full cloud architecture (AWS, GCP)

---

## 📞 Support Commands

```bash
# Restart everything
sudo systemctl restart omi-backend nginx

# Check all services
sudo systemctl status omi-backend nginx

# View all logs
sudo journalctl -u omi-backend -u nginx -f

# Check disk space
df -h

# Check memory
free -h

# Check CPU
top

# Check network
sudo netstat -tulpn
```

---

**Last Updated**: October 29, 2025
**Status**: Ready for implementation
**Estimated Setup Time**: 2-3 hours (including ML model downloads)
**Risk Level**: 🟡 **Medium** - Requires careful VPS configuration
