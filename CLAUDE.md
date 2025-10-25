# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Omi is an open-source AI wearable ecosystem consisting of multiple interconnected components: hardware devices (Omi device and Omi Glass), mobile applications, web interfaces, backend services, and an extensible plugin system. The system captures conversations, provides transcriptions, generates summaries, and executes actions through AI integration.

## Project Architecture

### Core Components
- **app/**: Flutter mobile app (iOS/Android) - primary user interface
- **backend/**: FastAPI Python backend deployed on Modal.com
- **omi/firmware/**: Nordic-based device firmware (Zephyr RTOS)
- **omiGlass/firmware/**: ESP32-S3 glass firmware (Arduino Framework)
- **web/**: Next.js web applications (frontend dashboard, AI personas)
- **plugins/**: Docker-based extensible plugin system
- **sdks/**: SDKs for third-party integration (React Native)
- **mcp/**: Model Context Protocol server for LLM integration

### Data Flow
```
[Hardware] → [Bluetooth] → [Mobile App] → [Backend API] → [AI Services]
    ↓              ↓             ↓            ↓             ↓
[Audio Data] → [Processing] → [Cloud Sync] → [Memory] → [Actions]
```

## Development Commands

### Flutter Mobile App (app/)
```bash
# Setup (first time)
cd app/
bash setup.sh ios                 # iOS setup with certificates
bash setup.sh android             # Android setup
cd ~/.ssh && ssh-add              # Setup SSH for certificate repos

# Development
flutter pub get                   # Install dependencies
flutter run --flavor dev          # Run development build
flutter run --flavor prod --release  # Run production build

# Platform-specific
flutter run --flavor dev -d ios   # Run on iOS
flutter run --flavor dev -d android  # Run on Android
flutter run --flavor dev -d macos    # Run on macOS

# Building
flutter build ios --flavor dev --release  # iOS release
flutter build apk --flavor dev --release  # Android APK
ios-deploy --bundle build/ios/iphoneos/Runner.app --debug  # Install to iPhone

# Testing and maintenance
flutter test                      # Run unit tests
flutter clean                     # Clean build artifacts
flutter packages pub run build_runner build  # Code generation
```

### Backend (backend/)
```bash
# Setup
cd backend/
cp .env.template .env             # Create environment file
python -m venv venv               # Create virtual environment
source venv/bin/activate          # Activate venv (Linux/macOS)
# venv\Scripts\activate           # Activate venv (Windows)
pip install -r requirements.txt   # Install dependencies

# Google Cloud authentication (required for Firebase)
gcloud auth login
gcloud config set project <project-id>
gcloud auth application-default login --project <project-id>

# Local development with ngrok
ngrok http --domain=example.ngrok-free.app 8000  # Setup ngrok tunnel
uvicorn main:app --reload --env-file .env        # Run local server

# Modal.com deployment
modal serve main.py               # Development with hot reload
modal deploy main.py              # Deploy to production
modal logs backend                # View logs
```

### Web Applications (web/)
```bash
# Frontend dashboard
cd web/frontend/
npm install && npm run dev        # Development server (Turbo)
npm run build                     # Production build

# AI Personas
cd web/personas-open-source/
npm install && npm run dev        # Next.js development
npm run build                     # Production build
```

### Firmware Development

#### Omi Device (omi/firmware/)
```bash
# Setup nRF Connect SDK v2.9.0
nrfutil toolchain-manager install --ncs-version v2.9.0
cd v2.9.0/

# Initialize workspace (first time)
west init -m https://github.com/nrfconnect/sdk-nrf --mr v2.9.0
west update  # Downloads ~1.5GB dependencies

# Build firmware
west build -b omi/nrf5340/cpuapp ../omi --sysbuild -- -DBOARD_ROOT=/path/to/firmware
# Clean build
west build -b omi/nrf5340/cpuapp ../omi --sysbuild --pristine -- -DBOARD_ROOT=/path/to/firmware

# Docker build (alternative)
cd scripts/
./build-docker.sh

# Outputs:
# - dfu_application.zip (OTA update package)
# - merged.hex (complete firmware for direct flash)
```

#### Omi Glass (omiGlass/firmware/)
```bash
# UF2 flashing (easiest method)
cd omiGlass/firmware/
./scripts/build_uf2.sh -e uf2_release  # Build optimized release
# 1. Hold BOOT button, press RESET, release BOOT
# 2. Copy omi_glass_firmware.uf2 to ESP32S3 USB drive
# 3. Device auto-flashes and reboots

# PlatformIO method
pio run -e seeed_xiao_esp32s3          # Build
pio run -e seeed_xiao_esp32s3 --target upload  # Upload
pio device monitor --baud 115200       # Serial monitor

# Arduino-CLI method
arduino-cli compile --build-path build --output-dir dist -e -u -p COM5 \
  -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi
```

### Plugins (plugins/)
```bash
cd plugins/example/
pip install -r requirements.txt
python -m uvicorn main:app --reload  # Development server
docker build -t omi-plugin .         # Containerize
```

### Model Context Protocol (MCP) Server (mcp/)
```bash
# Using Docker (recommended)
docker run --rm -i -e OMI_API_KEY=your_api_key omiai/mcp-server

# Debug with MCP inspector
npx @modelcontextprotocol/inspector uvx mcp-server-omi

# Configure for Claude Desktop (claude_desktop_config.json):
{
  "mcpServers": {
    "omi": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "OMI_API_KEY=your_api_key", "omiai/mcp-server"]
    }
  }
}
```

## Key Technologies

### Mobile App (Flutter 3.0+)
- **State Management**: Provider pattern with context
- **Device Communication**:
  - Bluetooth Low Energy (BLE) via flutter_blue_plus
  - Apple Watch integration via WatchConnectivity framework
  - Frame device support with custom transport layer
- **Audio**: Real-time recording, OPUS codec, background processing
- **Firebase**: Authentication, messaging, crashlytics, Firestore
- **Platform Support**: iOS, Android, macOS, Web, Apple Watch (watchOS)

### Backend (Python FastAPI)
- **Framework**: FastAPI with async/await patterns
- **Database**:
  - Google Cloud Firestore (primary NoSQL database)
  - Redis (caching and real-time features)
  - Pinecone (vector database for semantic search)
- **AI Integration**: OpenAI, LangChain, Groq, AssemblyAI, Deepgram
- **Audio Processing**: Librosa, PyTorch, SpeechBrain, WebSocket streaming
- **Deployment**: Modal.com serverless platform with auto-scaling
- **Security**: Transparent encryption layer, Firebase Auth

### Firmware
- **Omi Device**: Zephyr RTOS 3.4+, Nordic nRF5340 SoC, OPUS audio encoding
- **Omi Glass**: Arduino Framework, ESP32-S3, NimBLE Bluetooth stack
- **OTA Updates**: MCUboot bootloader with signed firmware packages

### Web (Next.js 14/15)
- **Framework**: Next.js with App Router, TypeScript
- **Styling**: Tailwind CSS, Radix UI components
- **AI Integration**: OpenAI, Anthropic Claude APIs
- **Authentication**: NextAuth.js

## Testing Strategy

### Mobile App Testing
```bash
flutter test                      # Unit tests
flutter drive --target=test_driver/integration_test.dart  # Integration tests
```

### Backend Testing
- Use FastAPI's test client with async pytest
- Mock external services (Firebase, AI APIs)
- Integration tests for database operations

### Firmware Testing
- Hardware-in-the-loop testing with real devices
- Zephyr test framework for unit tests
- Audio pipeline validation

## Plugin Development

Plugins extend Omi's functionality through Docker containers:

1. **Create Plugin**: Copy from `plugins/example/`
2. **Define Capabilities**: Modify manifest with required permissions
3. **Implement Logic**: Handle webhooks and real-time events
4. **Test Locally**: Use uvicorn for development
5. **Deploy**: Build Docker container and register

### Plugin Types
- **Real-time**: Process audio streams during recording
- **Memory**: Analyze completed conversations
- **Trigger**: Execute actions based on conversation content
- **Integration**: Connect to external services (Zapier, databases)

## API and Developer Tools

### Developer API Keys
Generate API keys in the Omi app:
- Navigate to `Settings > Developer > API Keys`
- Create new API key for programmatic access
- Use for MCP server, custom integrations, or third-party apps
- Keys can be revoked at any time

### MCP (Model Context Protocol) Tools
Available tools in the MCP server:
- `get_memories` - Retrieve user memories with filtering
- `create_memory` - Create new memory manually
- `delete_memory` - Delete memory by ID
- `edit_memory` - Edit memory content
- `get_conversations` - Retrieve conversations with transcripts

### API Documentation
- Full API docs available at `/docs` endpoint (Swagger UI)
- Alternative docs at `/redoc` (ReDoc)
- See `docs/doc/developer/api.mdx` for detailed API reference

## Development Environment Setup

### Prerequisites
- **Flutter**: 3.0+ with iOS/Android development setup
- **Python**: 3.8+ with pip
- **Node.js**: 18+ with npm
- **nRF Connect SDK**: v2.9.0 (for Nordic firmware)
- **PlatformIO**: For ESP32 development (Omi Glass)
- **Docker**: For plugin development
- **Xcode**: For iOS/macOS/watchOS development

### Environment Variables
Backend requires various API keys and configuration:
```bash
# AI Services
export OPENAI_API_KEY=your_key
export DEEPGRAM_API_KEY=your_key
export GROQ_API_KEY=your_key

# Google Cloud / Firebase
export FIREBASE_SERVICE_ACCOUNT_JSON=your_json
export GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json

# Database & Storage
export REDIS_URL=your_redis_url
export PINECONE_API_KEY=your_key
export PINECONE_INDEX_NAME=your_index

# Stripe (for payments)
export STRIPE_SECRET_KEY=your_key

# See backend/.env.template for complete list
```

## Device Architecture

### Supported Devices
1. **Omi Device** (Nordic nRF5340)
   - Dual-core ARM Cortex-M33
   - BLE 5.3 with high-throughput audio streaming
   - OPUS codec for real-time compression
   - Secure OTA updates via MCUboot

2. **Omi Glass** (ESP32-S3)
   - Camera-enabled smart glasses
   - UF2 bootloader for easy flashing
   - Battery monitoring and charging
   - Serial debugging support

3. **Apple Watch** (watchOS)
   - Native WatchKit app
   - WatchConnectivity framework for iOS sync
   - Independent audio recording
   - Background audio processing

### Device Communication Layers
```
┌─────────────────────────────────────┐
│   Flutter App (iOS/Android/macOS)  │
├─────────────────────────────────────┤
│      Device Transport Layer         │
│  ┌──────────┬──────────┬──────────┐ │
│  │ BLE      │ Watch    │ Frame    │ │
│  │ Transport│ Transport│ Transport│ │
│  └──────────┴──────────┴──────────┘ │
├─────────────────────────────────────┤
│      Device Connection Layer        │
│  ┌──────────┬──────────┬──────────┐ │
│  │ Omi      │ Apple    │ Frame    │ │
│  │ Device   │ Watch    │ Device   │ │
│  └──────────┴──────────┴──────────┘ │
└─────────────────────────────────────┘
```

## Common Development Workflows

### Adding New Features
1. **Mobile**: Implement UI in Flutter, integrate with backend APIs
2. **Backend**: Add FastAPI routes, update database schemas
3. **Firmware**: Modify device behavior, update OTA packages
4. **Plugin**: Create extensible functionality via plugin system
5. **Apple Watch**: Develop WatchKit UI, sync via WatchConnectivity

### Debugging Audio Issues
1. Check Bluetooth connection status in mobile app
2. Verify audio encoding (OPUS) on firmware
3. Monitor transcription service responses
4. Test with different audio input sources

### Deployment Process
1. **Mobile**: Build and distribute through app stores
2. **Backend**: Deploy to Modal.com with `modal deploy`
3. **Web**: Deploy to Vercel/Netlify
4. **Firmware**: Generate OTA packages, distribute via mobile app

## Architecture Patterns

- **Event-Driven**: Real-time audio processing and transcription
- **Plugin Architecture**: Docker-based extensible functionality
- **Cross-Platform**: Unified codebase across mobile/web/firmware
- **AI-First**: Deep integration with multiple LLM providers
- **Microservices**: Modular backend with clear separation of concerns

## Important Notes

### Architecture
- Firmware uses Nordic nRF5340 dual-core architecture with secure MCUboot bootloader
- Mobile app supports background audio recording with iOS/Android/watchOS permissions
- Backend is stateless and deployed on Modal.com for auto-scaling
- Device communication supports BLE, Apple Watch (WatchConnectivity), and Frame devices
- Plugin system supports real-time audio processing with webhook integration

### Key Files and Patterns
- **Device Discovery**: `app/lib/services/devices/discovery/` - Multi-device discovery system
- **Transport Layer**: `app/lib/services/devices/transports/` - Protocol abstraction
- **Provider Architecture**: `app/lib/providers/` - 20+ interconnected providers
- **Firmware Core**: `omi/firmware/omi/src/lib/core/` - Renamed from dk2
- **Backend Routers**: `backend/routers/` - Modular API endpoints
- **MCP Integration**: `mcp/` - LLM tool integration via Model Context Protocol

### Recent Major Changes
- Apple Watch support added with independent recording capability
- Firmware library reorganized from `dk2/` to `core/` structure
- Enhanced developer API with key management system
- Stripe integration for payments and subscriptions
- Multi-device architecture with pluggable transport layer
- Action items with notification support
- Enhanced conversation processing with app-specific summaries