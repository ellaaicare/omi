#!/bin/bash
#
# Systemd Service Setup - OMI Backend
#
# Creates systemd service for automatic startup and restart
# Run with sudo: sudo ./setup_systemd.sh
#

set -e

if [ "$EUID" -ne 0 ]; then
    echo "❌ Please run with sudo: sudo ./setup_systemd.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
USER=$(logname)  # Get the actual user who ran sudo

echo "======================================"
echo "OMI Backend Systemd Service Setup"
echo "======================================"
echo ""
echo "User: $USER"
echo "Backend directory: $BACKEND_DIR"
echo ""

# Create systemd service file
echo "📝 Creating systemd service file..."
cat > /etc/systemd/system/omi-backend.service <<EOF
[Unit]
Description=OMI Backend FastAPI Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BACKEND_DIR
Environment="PATH=$BACKEND_DIR/venv/bin"
ExecStart=$BACKEND_DIR/venv/bin/python start_server.py
Restart=always
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=omi-backend

[Install]
WantedBy=multi-user.target
EOF

echo "✅ Service file created at /etc/systemd/system/omi-backend.service"
echo ""

# Reload systemd
echo "🔄 Reloading systemd daemon..."
systemctl daemon-reload
echo "✅ Systemd reloaded"
echo ""

# Enable service (start on boot)
echo "🚀 Enabling service for automatic startup..."
systemctl enable omi-backend
echo "✅ Service enabled"
echo ""

echo "======================================"
echo "✅ Systemd Service Setup Complete!"
echo "======================================"
echo ""
echo "Service commands:"
echo "  Start:   sudo systemctl start omi-backend"
echo "  Stop:    sudo systemctl stop omi-backend"
echo "  Restart: sudo systemctl restart omi-backend"
echo "  Status:  sudo systemctl status omi-backend"
echo "  Logs:    sudo journalctl -u omi-backend -f"
echo ""
