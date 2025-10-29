#!/bin/bash
#
# VPS Deployment Script - OMI Backend
#
# This script handles git-based deployment to VPS
# Run this ON THE VPS after cloning the repo
#
# Usage: ./vps_deploy.sh
#

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"

echo "======================================"
echo "OMI Backend VPS Deployment"
echo "======================================"
echo ""

# Check if running on VPS (not Mac)
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "❌ This script should run ON THE VPS, not on Mac"
    echo "   First push to git, then SSH to VPS and run this script"
    exit 1
fi

# Step 1: Git pull latest changes
echo "📥 Step 1: Pulling latest changes from git..."
cd "$BACKEND_DIR/.."
git pull origin feature/ios-backend-integration
echo "✅ Git pull complete"
echo ""

# Step 2: Check Python version
echo "🐍 Step 2: Checking Python version..."
if ! command -v python3.11 &> /dev/null; then
    echo "⚠️  Python 3.11 not found. Installing..."
    sudo apt update
    sudo apt install -y python3.11 python3.11-venv python3-pip
fi
python3.11 --version
echo "✅ Python 3.11 available"
echo ""

# Step 3: Create/update virtual environment
echo "📦 Step 3: Setting up virtual environment..."
cd "$BACKEND_DIR"
if [ ! -d "venv" ]; then
    echo "   Creating new virtual environment..."
    python3.11 -m venv venv
else
    echo "   Virtual environment exists, updating..."
fi
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "✅ Virtual environment ready"
echo ""

# Step 4: Install system dependencies
echo "🔧 Step 4: Installing system dependencies..."
sudo apt install -y libopus-dev opus-tools ffmpeg
echo "✅ System dependencies installed"
echo ""

# Step 5: Check for .env and google-credentials.json
echo "🔐 Step 5: Checking environment configuration..."
if [ ! -f ".env" ]; then
    echo "⚠️  .env file not found!"
    echo "   Please create .env with required variables"
    echo "   See docs/VPS_PRODUCTION_DEPLOYMENT.md for template"
    exit 1
fi

if [ ! -f "google-credentials.json" ]; then
    echo "⚠️  google-credentials.json not found!"
    echo "   Please copy Firebase credentials to backend/google-credentials.json"
    exit 1
fi
echo "✅ Environment configuration found"
echo ""

# Step 6: Download ML models (if not already cached)
echo "🤖 Step 6: Checking ML models..."
if [ ! -d "$HOME/.cache/huggingface/hub" ]; then
    echo "   ML models not found. Downloading (~17GB, may take 30-60 minutes)..."
    echo "   This is a one-time download."
    python download_models.py
else
    echo "   ML models already cached in ~/.cache/huggingface/"
fi
echo "✅ ML models ready"
echo ""

# Step 7: Test backend starts
echo "🧪 Step 7: Testing backend startup..."
timeout 10 python start_server.py || true
echo "✅ Backend startup test complete"
echo ""

echo "======================================"
echo "✅ Deployment Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Configure systemd service (one-time setup):"
echo "   sudo ./deploy/setup_systemd.sh"
echo ""
echo "2. Configure nginx (one-time setup):"
echo "   sudo ./deploy/setup_nginx.sh"
echo ""
echo "3. Get SSL certificate:"
echo "   sudo certbot --nginx -d api.ella-ai-care.com"
echo ""
echo "4. Start backend service:"
echo "   sudo systemctl start omi-backend"
echo ""
echo "5. Check status:"
echo "   sudo systemctl status omi-backend"
echo ""
