#!/bin/bash
# Official test script for Voice Mode v2 (Pipecat)
#
# Usage:
#   ./scripts/test_voice_v2.sh           # Test local server
#   ./scripts/test_voice_v2.sh prod      # Test production
#   ./scripts/test_voice_v2.sh health    # Run specific test
#
# Requirements:
#   - Backend server running (local or production)
#   - Virtual environment activated
#   - websockets and httpx packages installed

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BACKEND_DIR"

# Activate virtual environment if not already active
if [[ -z "$VIRTUAL_ENV" ]]; then
    if [[ -f "venv/bin/activate" ]]; then
        source venv/bin/activate
    else
        echo "❌ Virtual environment not found. Create with: python -m venv venv"
        exit 1
    fi
fi

# Install test dependencies if needed
pip install -q websockets httpx 2>/dev/null || true

# Parse arguments
case "${1:-local}" in
    prod|production)
        echo "🌐 Testing production server..."
        python -m integrations.pipecat.tests.test_voice_v2 \
            --host api.ella-ai-care.com \
            --port 443 \
            --ssl \
            "${@:2}"
        ;;
    local)
        echo "🏠 Testing local server..."
        python -m integrations.pipecat.tests.test_voice_v2 \
            --host localhost \
            --port 8000 \
            "${@:2}"
        ;;
    health|websocket|audio|n8n|pipeline)
        echo "🧪 Running single test: $1"
        python -m integrations.pipecat.tests.test_voice_v2 \
            --test "$1" \
            "${@:2}"
        ;;
    -h|--help|help)
        echo "Voice Mode v2 Test Script"
        echo ""
        echo "Usage:"
        echo "  ./scripts/test_voice_v2.sh [target] [options]"
        echo ""
        echo "Targets:"
        echo "  local       Test local server (default)"
        echo "  prod        Test production server"
        echo "  health      Run health check test only"
        echo "  websocket   Run WebSocket test only"
        echo "  audio       Run audio streaming test only"
        echo "  n8n         Run n8n integration test only"
        echo "  pipeline    Run pipeline creation test only"
        echo ""
        echo "Options:"
        echo "  -v, --verbose    Verbose output"
        echo "  --uid USER_ID    Custom test user ID"
        echo ""
        echo "Examples:"
        echo "  ./scripts/test_voice_v2.sh              # Full local test"
        echo "  ./scripts/test_voice_v2.sh prod         # Full production test"
        echo "  ./scripts/test_voice_v2.sh health       # Health check only"
        echo "  ./scripts/test_voice_v2.sh local -v     # Verbose local test"
        ;;
    *)
        # Pass through to Python script
        python -m integrations.pipecat.tests.test_voice_v2 "$@"
        ;;
esac
