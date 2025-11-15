#!/bin/bash
#
# nginx Configuration Setup - OMI Backend
#
# Configures nginx reverse proxy for api.ella-ai-care.com
# Run with sudo: sudo ./setup_nginx.sh
#

set -e

if [ "$EUID" -ne 0 ]; then
    echo "❌ Please run with sudo: sudo ./setup_nginx.sh"
    exit 1
fi

DOMAIN="api.ella-ai-care.com"
BACKEND_PORT="8000"

echo "======================================"
echo "OMI Backend nginx Configuration"
echo "======================================"
echo ""
echo "Domain: $DOMAIN"
echo "Backend: localhost:$BACKEND_PORT"
echo ""

# Check if nginx is installed
if ! command -v nginx &> /dev/null; then
    echo "❌ nginx not found. Installing..."
    apt update
    apt install -y nginx
fi

# Create nginx configuration
echo "📝 Creating nginx configuration..."
cat > /etc/nginx/sites-available/omi-backend <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    # Increase timeouts for long-running WebSocket connections
    proxy_read_timeout 300s;
    proxy_connect_timeout 75s;

    # WebSocket upgrade headers
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";

    # Standard proxy headers
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # Proxy all requests to backend
    location / {
        proxy_pass http://localhost:$BACKEND_PORT;
    }

    # Health check endpoint (optional)
    location /health {
        proxy_pass http://localhost:$BACKEND_PORT/health;
    }
}
EOF

echo "✅ Configuration file created at /etc/nginx/sites-available/omi-backend"
echo ""

# Enable site
echo "🔗 Enabling nginx site..."
ln -sf /etc/nginx/sites-available/omi-backend /etc/nginx/sites-enabled/
echo "✅ Site enabled"
echo ""

# Test nginx configuration
echo "🧪 Testing nginx configuration..."
nginx -t
echo "✅ nginx configuration valid"
echo ""

# Reload nginx
echo "🔄 Reloading nginx..."
systemctl reload nginx
echo "✅ nginx reloaded"
echo ""

echo "======================================"
echo "✅ nginx Configuration Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Verify DNS is configured:"
echo "   ping $DOMAIN"
echo ""
echo "2. Get SSL certificate:"
echo "   sudo certbot --nginx -d $DOMAIN"
echo ""
echo "3. Test HTTP access (before SSL):"
echo "   curl http://$DOMAIN/health"
echo ""
