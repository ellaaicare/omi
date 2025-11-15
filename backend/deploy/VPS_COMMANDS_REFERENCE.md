# VPS Deployment - Quick Command Reference

**Domain**: api.ella-ai-care.com
**Git Branch**: feature/ios-backend-integration

---

## 🚀 First Time Setup (Complete Workflow)

### 1. SSH to VPS
```bash
ssh user@your-vps-ip
```

### 2. Clone/Update Repo
```bash
# If not already cloned:
git clone https://github.com/ellaaicare/omi.git
cd omi
git checkout feature/ios-backend-integration

# If already cloned:
cd omi
git pull origin feature/ios-backend-integration
```

### 3. Transfer Credentials from Mac
```bash
# On Mac (new terminal):
scp /Users/greg/repos/omi/backend/google-credentials.json user@your-vps-ip:~/omi/backend/

# Copy .env values manually (don't transfer .env file itself for security)
```

### 4. Create .env on VPS
```bash
# On VPS:
cd omi/backend
cp deploy/.env.vps.example .env
nano .env

# Paste API keys from Mac's backend/.env
# Update GOOGLE_APPLICATION_CREDENTIALS path with your VPS username
# Save and exit: Ctrl+X, Y, Enter
```

### 5. Run Deployment Script
```bash
# On VPS, in omi/backend:
chmod +x deploy/vps_deploy.sh
./deploy/vps_deploy.sh

# This will:
# - Install Python 3.11
# - Create venv and install dependencies
# - Install system libraries (Opus, ffmpeg)
# - Download ML models (~17GB, 30-60 min)
```

### 6. Setup Systemd Service
```bash
# On VPS, in omi/backend:
chmod +x deploy/setup_systemd.sh
sudo ./deploy/setup_systemd.sh
```

### 7. Setup nginx Reverse Proxy
```bash
# On VPS, in omi/backend:
chmod +x deploy/setup_nginx.sh
sudo ./deploy/setup_nginx.sh
```

### 8. Get SSL Certificate
```bash
# On VPS:
sudo certbot --nginx -d api.ella-ai-care.com

# Follow prompts:
# - Use same email as n8n
# - Agree to terms
# - Choose redirect HTTP → HTTPS
```

### 9. Start Backend
```bash
# On VPS:
sudo systemctl start omi-backend
sudo systemctl status omi-backend
# Should show: "active (running)"
```

### 10. Test
```bash
# From anywhere:
curl https://api.ella-ai-care.com/health
# Returns: {"status":"ok"}

# Or open in browser:
# https://api.ella-ai-care.com/docs
```

---

## 🔄 Future Updates (Git-Based)

### When Making Backend Changes

**On Mac:**
```bash
cd /Users/greg/repos/omi
# Make changes...
git add .
git commit -m "your changes"
git push fork feature/ios-backend-integration
```

**On VPS:**
```bash
ssh user@your-vps-ip
cd omi/backend
git pull
./deploy/vps_deploy.sh
sudo systemctl restart omi-backend
```

**Done!** No manual file copying.

---

## 🛠️ Common Operations

### Check Backend Status
```bash
sudo systemctl status omi-backend
```

### View Backend Logs
```bash
# Real-time
sudo journalctl -u omi-backend -f

# Last 100 lines
sudo journalctl -u omi-backend -n 100

# Since specific time
sudo journalctl -u omi-backend --since "10 minutes ago"
```

### Restart Backend
```bash
sudo systemctl restart omi-backend
```

### Stop Backend
```bash
sudo systemctl stop omi-backend
```

### Check nginx
```bash
sudo systemctl status nginx
sudo nginx -t
sudo systemctl reload nginx
```

### Check Resource Usage
```bash
htop          # Interactive CPU/RAM monitor
free -h       # Memory usage
df -h         # Disk space
```

### Check Port Usage
```bash
sudo netstat -tlnp | grep 8000    # Backend port
sudo netstat -tlnp | grep 443     # HTTPS port
```

---

## 🐛 Troubleshooting

### Backend Won't Start
```bash
# Check logs for error
sudo journalctl -u omi-backend -n 50

# Common fixes:
cat /home/user/omi/backend/.env  # Verify .env exists
ls -la /home/user/omi/backend/google-credentials.json  # Verify creds exist

# Kill conflicting process
sudo lsof -ti:8000 | xargs kill -9

# Restart
sudo systemctl restart omi-backend
```

### SSL Certificate Issues
```bash
# Check certificate status
sudo certbot certificates

# Renew manually
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

### nginx Config Issues
```bash
# Test config
sudo nginx -t

# View error logs
sudo tail -f /var/log/nginx/error.log
```

### Out of Memory
```bash
# Check memory
free -h

# Restart backend to free memory
sudo systemctl restart omi-backend

# If persistent: upgrade VPS to 4GB RAM
```

---

## 📦 What Each Script Does

### `vps_deploy.sh`
- Pulls latest git changes
- Installs Python 3.11 and dependencies
- Creates/updates virtual environment
- Installs system libraries (Opus, ffmpeg)
- Downloads ML models (one-time, ~17GB)
- Tests backend startup

### `setup_systemd.sh` (requires sudo)
- Creates systemd service file
- Enables auto-start on boot
- Configures auto-restart on crashes
- Sets up logging to journalctl

### `setup_nginx.sh` (requires sudo)
- Creates nginx reverse proxy config
- Enables WebSocket support
- Routes api.ella-ai-care.com → localhost:8000
- Reloads nginx

---

## 📊 Expected Behavior

### After First Setup:
- Backend auto-starts on VPS boot
- Crashes automatically trigger restart
- Logs accessible via journalctl
- HTTPS working with Let's Encrypt SSL
- iOS app connects from anywhere

### Resource Usage:
- RAM: 500MB-1GB (with ML models)
- CPU: 10-30% idle, up to 100% during transcription
- Disk: ~20GB (code + ML models)

### SSL Certificate:
- Auto-renews every 90 days (certbot cron job)
- Check renewal: `sudo certbot renew --dry-run`

---

## 🔒 Security Checklist

**Current (Development):**
- [x] HTTPS/SSL enabled
- [x] Backend behind reverse proxy
- [x] Firewall configured
- [ ] LOCAL_DEVELOPMENT=true (needs false for production)
- [ ] Firestore rules OPEN (needs restriction)

**Before Production:**
See `docs/SECURITY_HIPAA_CHECKLIST.md`

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

No special software needed on iOS device!

---

**Last Updated**: October 29, 2025
**Branch**: feature/ios-backend-integration
**Deployment Method**: Git-based (industry standard)
