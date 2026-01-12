# OMI iOS/Flutter App - Developer Guide

**Last Updated**: January 2025
**Status**: Active Development
**Branch**: `main`

---

## Quick Start

```bash
cd /Users/greg/repos/omi/app
flutter pub get
flutter run --flavor dev
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Flutter App (iOS/Android)                 │
├─────────────────────────────────────────────────────────────┤
│  UI Layer (lib/pages/)                                       │
│  ├── Home (conversations, memories, action items, apps)     │
│  ├── Settings (device, developer, profile)                  │
│  └── Onboarding (auth, device pairing, permissions)         │
├─────────────────────────────────────────────────────────────┤
│  State Management (lib/providers/)                           │
│  ├── CaptureProvider    - Recording, transcription          │
│  ├── DeviceProvider     - BLE connection, battery           │
│  ├── ConversationProvider - Memory CRUD                     │
│  └── 15+ specialized providers                              │
├─────────────────────────────────────────────────────────────┤
│  Services Layer (lib/services/)                              │
│  ├── DeviceService      - Multi-device BLE management       │
│  ├── VoiceModeV2Service - Real-time AI voice calls          │
│  ├── HeuristicsService  - Wake word detection               │
│  ├── OnDeviceASRService - iOS Speech framework              │
│  └── EllaTtsService     - Text-to-speech                    │
├─────────────────────────────────────────────────────────────┤
│  Native iOS (ios/Runner/)                                    │
│  ├── AppDelegate.swift  - MethodChannels, push handling     │
│  └── OnDeviceASRService.swift - iOS Speech (placeholder)    │
└─────────────────────────────────────────────────────────────┘
         │                              │
         │ BLE                          │ HTTPS/WSS
         ▼                              ▼
┌─────────────────┐          ┌─────────────────────┐
│  Omi Devices    │          │  Backend APIs       │
│  - Necklace     │          │  api.ella-ai-care   │
│  - Frame        │          │  .com               │
│  - Apple Watch  │          │                     │
└─────────────────┘          └─────────────────────┘
```

---

## Key Services

### VoiceModeV2Service (`lib/services/voice_mode_v2/`)
Real-time bidirectional voice conversations via WebSocket.

```dart
// Connect to voice endpoint
final wsUrl = 'wss://api.ella-ai-care.com/v2/voice?uid=$uid&pipeline_mode=$mode';

// Pipeline modes:
// - 'default'   : Standard Pipecat (higher latency)
// - 'grok_v2v'  : Grok voice-to-voice (~500ms latency)
```

**Flow**: Phone mic → WebSocket → Server VAD → LLM → TTS → Phone speaker

### HeuristicsService (`lib/services/heuristics/`)
On-device wake word detection.

```dart
// Default wake words
['hey ella', 'hi ella', 'hello ella', 'ella']

// Features:
// - 3 second debounce
// - Auto-starts V2 voice call
// - Disabled during active calls
// - Plays chime on detection
```

### OnDeviceASRService (`lib/services/asr/`)
iOS Speech framework for local transcription (no cloud upload).

**Status**: Dart side complete, Swift implementation is placeholder (needs work)

---

## Backend Integration

### Base URL
- **Production**: `https://api.ella-ai-care.com`
- **Custom**: Configurable in Developer Settings

### Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/conversations` | List memories |
| `POST /api/v1/tts/generate` | Generate TTS audio |
| `WSS /v2/voice` | Real-time voice mode |
| `WSS /listen` | Cloud transcription (Deepgram) |

### Authentication
Firebase JWT tokens in `Authorization: Bearer <token>` header.

---

## Device Support

### Omi Necklace (Primary)
- **Connection**: BLE
- **Audio**: OPUS or PCM16 at 16kHz
- **Services**:
  - Audio: `19b10000-e8f2-537e-4f6c-d104768a1214`
  - Battery: `0000180f-0000-1000-8000-00805f9b34fb`
  - Speaker: `cab1ab95-2ea5-4f4d-bb56-874b72cfc984`

### Other Devices
- **Frame**: AR glasses with camera
- **Apple Watch**: WatchConnectivity (not BLE)
- **XOR/PLAUD**: Third-party recorders

---

## Current Status (January 2025)

### Working ✅
- Memories display (fixed model mismatch)
- BLE device connection (fixed deadlock)
- Wake word detection (cloud path via n8n)
- V2 Voice WebSocket connection
- TTS API (backend deployed)

### In Progress 🔄
- V2 Voice response (connects but backend not responding)
- On-device wake word (needs ASR mode testing)
- Grok V2V pipeline mode (adding config)

### Not Started ❌
- Multi-device audio routing (necklace + headset parallel)
- On-device ASR Swift implementation

---

## Developer Settings

Location: Settings → Developer Settings

| Setting | Purpose | Values |
|---------|---------|--------|
| Custom API URL | Override backend | e.g., `http://localhost:8000` |
| ASR Mode | Transcription engine | `cloud` (Deepgram) / `on_device_ios` |
| Voice Mode V2 | Enable Pipecat voice | ON/OFF |
| Pipeline Mode | Voice backend | `default` / `grok_v2v` (~500ms) |
| Wake Words | Trigger phrases | ["hey ella", "hi ella", ...] |
| Auto-start Call | Call on wake word | ON/OFF |

---

## End-to-End Testing Guide

### Prerequisites
1. iPhone with app installed (TestFlight or dev build)
2. Backend API running (check with backend team)
3. Network connectivity to `api.ella-ai-care.com`

### Build & Run
```bash
cd /Users/greg/repos/omi/app
flutter clean
flutter pub get
flutter run --flavor dev
```

### Test 1: Voice Mode (Standard Pipeline)

1. **Enable V2 Voice Mode**:
   - Settings → Developer Settings
   - Toggle ON "Voice Mode V2 (Pipecat)"
   - Pipeline Mode → "Standard (Pipecat)"

2. **Start Voice Call**:
   - Go to Home screen
   - Tap the **phone button** (bottom right)
   - Wait for "Connected" state

3. **Test Conversation**:
   - Say: "Hello, can you hear me?"
   - Wait for response (~1-2s latency expected)
   - Say: "What's the weather like?"
   - End call by tapping phone button again

4. **Check Logs**:
   ```bash
   flutter logs | grep -i "VoiceModeV2"
   ```
   Expected:
   ```
   VoiceModeV2: Connecting with pipeline_mode=default
   VoiceModeV2: Connected!
   VoiceModeV2: Received X bytes TTS audio
   ```

### Test 2: Grok V2V (~500ms latency)

1. **Switch Pipeline**:
   - Developer Settings → Pipeline Mode → "Grok V2V (~500ms latency)"

2. **Start Voice Call**:
   - Tap phone button
   - Notice faster response time

3. **Compare Latency**:
   - Standard: ~1-2s response time
   - Grok V2V: ~500ms response time

4. **Logs should show**:
   ```
   VoiceModeV2: Connecting with pipeline_mode=grok_v2v
   ```

### Test 3: Wake Word Detection

1. **Setup** (requires no Omi device):
   - Developer Settings → ASR Mode → "On-Device iOS (Apple Speech)"
   - Ensure "Wake Word Detection" is enabled
   - "Auto-start voice call" should be ON

2. **Test**:
   - Go to Home screen
   - Press mic button to start ambient recording
   - Say "Hey Ella"
   - Should hear chime and see call start automatically

3. **Logs**:
   ```bash
   flutter logs | grep -i "Heuristics\|Wake"
   ```
   Expected:
   ```
   🎯 [Heuristics] WAKE WORD DETECTED: "hey ella"
   📞 [Heuristics] Auto-starting voice call...
   ```

### Test 4: Cloud Wake Word (with Omi Device)

1. **Connect Omi necklace** via Bluetooth
2. Say "Hey Ella" near the device
3. Should receive push notification from n8n
4. Tap notification to open app

### Troubleshooting

| Issue | Check |
|-------|-------|
| No response from voice call | Backend logs, API keys, Pipecat status |
| Wake word not detected | ASR mode setting, check if recording is active |
| Connection timeout | Network, firewall, WebSocket URL |
| Audio not playing | Volume, Bluetooth routing, audio session |

### Log Commands

```bash
# All logs
flutter logs

# Voice mode only
flutter logs | grep -i "voice"

# Heuristics/wake word
flutter logs | grep -i "heuristics\|wake"

# WebSocket
flutter logs | grep -i "websocket\|connecting"

# Errors only
flutter logs | grep -i "error\|fail"
```

---

## Key Files

| File | Purpose |
|------|---------|
| `lib/services/voice_mode_v2/voice_mode_v2_service.dart` | V2 voice calls |
| `lib/services/heuristics/heuristics_service.dart` | Wake word detection |
| `lib/providers/capture_provider.dart` | Audio routing |
| `lib/providers/device_provider.dart` | BLE management |
| `lib/pages/settings/developer.dart` | Dev settings UI |
| `lib/backend/preferences.dart` | SharedPreferences |

---

## Git Workflow

**CRITICAL**: This is a fork of `BasedHardware/omi`. Never push to upstream!

```bash
# Always use downstream repo
gh issue create --repo ellaaicare/omi --title "..."
gh pr create --repo ellaaicare/omi --title "..."
```

---

## Common Issues

### Build Fails After Merge
```bash
flutter clean
cd ios && pod install && cd ..
flutter pub get
flutter run --flavor dev
```

### BLE Connection Timeout
Check `device_provider.dart` for deadlock issues. Recent fix: `_setConnectedDeviceInternal()`.

### Wake Word Not Triggering
1. Check ASR mode is correct for your setup
2. Omi connected → uses cloud ASR
3. No device → uses on-device ASR (if enabled)

### V2 Voice No Response
Backend issue - check Pipecat logs, API keys, pipeline config.

---

## Contact

- **PM Agent**: `http://140.82.17.219:8284/v1/agents/...`
- **GitHub Issues**: https://github.com/ellaaicare/omi/issues
- **Architecture Doc**: `app/docs/APP_ARCHITECTURE_OVERVIEW.md`

---

*For comprehensive architecture details, see `docs/APP_ARCHITECTURE_OVERVIEW.md`*
