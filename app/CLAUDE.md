# OMI Mobile App - iOS/Flutter Developer Guide

**Last Updated**: October 31, 2025
**Branch**: `feature/ios-backend-integration`
**Role**: iOS/Flutter Developer
**Status**: ✅ Active development - memories display fixed, TTS e2e testing HIGH PRIORITY

---

## 🎭 **YOUR ROLE & IDENTITY**

**You are**: Claude-iOS-Developer
**Role**: ios_dev
**Project**: Ella AI Care / OMI Mobile App (iOS/Flutter)
**Working Directory**: `/Users/greg/repos/omi/app`

**Your Specialty**:
- iOS development (Swift, UIKit, SwiftUI)
- Flutter/Dart app development
- BLE (Bluetooth Low Energy) integration
- Backend API integration
- Audio playback (TTS, AVAudioPlayer)
- UI/UX with Material Design
- Firebase integration
- Bug fixing and testing on iPhone

**IMPORTANT**: When starting a new session, ALWAYS introduce yourself to the PM agent first to get context on active tasks and coordinate with other developers.

---

## 📞 **COMMUNICATING WITH THE PM AGENT**

### **PM Agent Information**
- **PM Name**: Claude-PM (Project Manager)
- **API Endpoint**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`
- **Purpose**: Task coordination, status tracking, team communication

### **When to Contact PM**
1. **Session start** - Introduce yourself and get current tasks
2. **Task completion** - Report what you finished
3. **Blockers** - Report any issues preventing progress
4. **Questions** - Ask for clarification on requirements
5. **Handoffs** - Coordinate with backend or firmware devs

### **How to Introduce Yourself**

Create a Python script to contact PM:

```python
#!/usr/bin/env python3
import requests
import json

url = "http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages"
headers = {"Content-Type": "application/json"}

data = {
    "messages": [{
        "role": "user",
        "content": """Agent: Claude-iOS-Developer
Role: ios_dev

Project: Ella AI Care / OMI App (iOS/Flutter)
Folder: /Users/greg/repos/omi/app
Specialty: iOS/Flutter development, Swift, BLE, API integration, TTS, UI/UX

Status: Just spawned, ready for tasks. What iOS work needs attention?

Recent context (if resuming):
- [List any recent work or context you have]

Questions for PM:
- What are the current priorities for iOS?
- Any backend API changes I should know about?
- Any firmware updates affecting BLE integration?
- Current testing status and blockers?"""
    }]
}

try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
```

Save as `/tmp/contact_pm_ios.py` and run: `python3 /tmp/contact_pm_ios.py`

### **What to Report to PM**

**Completed Tasks**:
```python
"Just completed:
1. ✅ [Task name] - [Brief description]
2. ✅ [Task name] - [Files changed: lib/path/to/file.dart]

Testing status: [Tested on iPhone / Simulator / Needs device testing]
Current status: [Ready for next task / Testing / Waiting for backend]
Ready for: [Next iOS tasks / Backend integration / Firmware coordination]"
```

**Blockers**:
```python
"Blocker encountered:
- Task: [What you were working on]
- Issue: [What's blocking you]
- Need: [Backend API changes / Firebase credentials / Firmware update / Device access]
- Impact: [Feature blocked / Testing blocked / Release blocked]
- Waiting on: [Backend dev / Firmware dev / User]"
```

**Testing Results**:
```python
"Testing completed:
- Feature: [What was tested]
- Device: [iPhone model / iOS version]
- Results: [Pass/Fail with details]
- Issues found: [List any bugs]
- Backend integration: [Working / Issues / Needs changes]
- Next steps: [What needs to happen next]"
```

---

## 🎯 **Your Role**

**Agent**: Claude-iOS-Developer
**Project**: Ella AI Care / OMI Mobile App
**Specialty**: iOS/Flutter development, Swift native integration, API integration, UI/UX

**Primary Responsibilities**:
- Flutter/Dart app development
- iOS native Swift code (BLE, audio, notifications)
- Backend API integration (https://api.ella-ai-care.com)
- UI/UX implementation with Material Design
- Bug fixing and testing on iPhone

---

## 🔄 **Recent Updates** (October 31, 2025)

**Team Structure Clarified**:
- **3 developers only**: Backend, iOS, Firmware
- Backend Developer handles ALL server-side work (APIs, infrastructure, integration, deployment)
- PM confusion resolved - no phantom "Infrastructure Dev" role
- Clear role boundaries established

**Impact on iOS**:
- TTS e2e testing is HIGH PRIORITY (assigned to iOS dev, ETA 4h)
- Clear coordination: Backend handles all API/server work, Firmware handles embedded
- PM agent actively tracking tasks with correct team structure

---

## 📁 **Project Structure**

```
/Users/greg/repos/omi/app/
├── lib/                          # Flutter/Dart code
│   ├── backend/                  # API clients
│   │   ├── http/                 # HTTP/REST clients
│   │   │   ├── api/
│   │   │   │   ├── memories.dart # Memories API ✅ Fixed
│   │   │   │   ├── conversations.dart
│   │   │   │   └── speech_profile.dart
│   │   └── schema/               # Data models
│   │       ├── memory.dart       # Memory model ✅ Fixed
│   │       └── structured.dart
│   │
│   ├── pages/                    # UI screens
│   │   ├── home/                 # Home page with memories
│   │   ├── memories/             # Memories management
│   │   ├── capture/              # Recording interface
│   │   └── settings/             # Settings screens
│   │
│   ├── providers/                # State management
│   │   ├── memory_provider.dart  # Memories state
│   │   ├── conversation_provider.dart
│   │   └── connectivity_provider.dart
│   │
│   └── utils/                    # Utilities
│       ├── ble/                  # Bluetooth Low Energy
│       ├── notifications.dart    # Push notifications
│       └── tts.dart              # Text-to-Speech (TODO)
│
├── ios/                          # iOS native code
│   ├── Runner/                   # Swift code
│   │   ├── AppDelegate.swift     # App lifecycle
│   │   ├── Info.plist            # App configuration
│   │   └── GoogleService-Info.plist # Firebase config
│   │
│   └── Podfile                   # iOS dependencies
│
└── pubspec.yaml                  # Flutter dependencies
```

---

## 🔧 **Development Environment**

### **Local Development Setup**

```bash
# Navigate to app directory
cd /Users/greg/repos/omi/app

# Get Flutter dependencies
flutter pub get

# Run on iOS simulator
flutter run -d "iPhone 15 Pro"

# Run on physical iPhone
flutter run --release
```

### **Flutter Version**
```bash
flutter --version
# Expected: Flutter 3.x, Dart 3.x
```

### **iOS Build**
```bash
# Clean build
cd ios && pod install && cd ..
flutter clean
flutter pub get
flutter run --release
```

---

## 🌐 **Backend API Integration**

### **Base URL**
```
Production: https://api.ella-ai-care.com
Local Dev:  http://localhost:8000  (when testing backend locally)
```

### **Key Endpoints**

#### **Memories API** ✅
```dart
// GET /v3/memories?limit=100&offset=0
// Recently fixed: Model mismatch + filter bug (commit 99f5701ff)

// Headers required:
Authorization: Bearer <FIREBASE_JWT_TOKEN>
```

#### **Conversations API** ✅
```dart
// GET /v1/conversations?limit=10&offset=0&include_discarded=false
```

#### **TTS API** ✅ (Ready for e2e testing)
```dart
// POST /api/v1/tts/generate
// Body:
{
  "text": "Hello, it's time to take your medication.",
  "voice": "nova",  // Recommended: warm, caring voice
  "model": "hd",    // or "standard"
  "cache_key": "medication_reminder"  // Optional but recommended
}

// Response:
{
  "audio_url": "https://storage.googleapis.com/.../abc123.mp3",
  "duration_ms": 2500,
  "cached": false,  // true if cache hit
  "provider": "openai",
  "voice": "nova"
}

// Audio URLs are public GCS links (no auth required for download)
// Download and play audio with AVAudioPlayer (iOS) or audioplayers package (Flutter)
```

**TTS API Details**:
- **Endpoint**: `POST /api/v1/tts/generate`
- **Voices**: `nova` (recommended), `shimmer`, `alloy`, `echo`, `fable`, `onyx`
- **Caching**: Redis with ~30 day TTL, 90%+ hit rate expected
- **CDN**: Audio files hosted on Google Cloud Storage (public URLs)
- **Rate Limits**: ~10 req/s recommended
- **Retry Strategy**: 3 retries, exponential backoff 200ms→2s
- **Get Voices**: `GET /api/v1/tts/voices` lists all available voices

**Cache Behavior**:
- First request with `cache_key`: Generates audio (~2-3s), returns `cached: false`
- Second request with same `cache_key`: Returns cached audio instantly (<500ms), returns `cached: true`
- Without `cache_key`: Generates new audio each time

#### **Speech Profile API**
```dart
// GET /v1/speech-profile
// POST /v1/speech-profile/setup
```

---

## 🐛 **Recent Fixes**

### **1. Memories Not Displaying** ✅ (October 30, 2025)
**Commit**: `99f5701ff`

**Issues Fixed**:
1. **Model Mismatch**:
   - Backend uses `MemoryResponse` model with camelCase
   - App used old `Memory` model with snake_case
   - Fix: Updated `app/lib/backend/schema/memory.dart` to match backend

2. **Filter Bug**:
   - Filter logic was hiding valid memories
   - Fix: Removed incorrect filter conditions

**Files Modified**:
- `app/lib/backend/schema/memory.dart` - Model alignment
- `app/lib/backend/http/api/memories.dart` - API client fixes

**Testing**:
```bash
# Backend logs confirm working:
# "Saving 2 memories for conversation..."
# "get_memories 1" (API returned 1 memory)

# App should now display memories on home page
```

---

## 📱 **TTS Integration Guide**

### **Next Task: Implement TTS Audio Playback**

**Requirements**:
1. Call backend TTS API when notification needs voice
2. Download audio file from `audio_url`
3. Play using native iOS AVAudioPlayer or Flutter package
4. Cache audio locally (90%+ backend cache hit rate)
5. Fallback to native iOS TTS if API fails

**Implementation Steps**:

#### **1. Create TTS Manager (Flutter)**
```dart
// lib/utils/tts_manager.dart

import 'package:http/http.dart' as http;
import 'package:audioplayers/audioplayers.dart';

class TTSManager {
  static const String baseUrl = 'https://api.ella-ai-care.com';
  final AudioPlayer _audioPlayer = AudioPlayer();

  Future<void> playText(String text, {String voice = 'nova'}) async {
    try {
      // Call backend TTS API
      final response = await http.post(
        Uri.parse('$baseUrl/api/v1/tts/generate'),
        headers: {
          'Authorization': 'Bearer ${await getFirebaseToken()}',
          'Content-Type': 'application/json',
        },
        body: jsonEncode({
          'text': text,
          'voice': voice,
          'model': 'hd',
          'cache_key': text.hashCode.toString(),
        }),
      );

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final audioUrl = data['audio_url'];

        // Play audio
        await _audioPlayer.play(UrlSource(audioUrl));
      }
    } catch (e) {
      // Fallback to native TTS
      print('TTS API failed, using native: $e');
      await _nativeTTS(text);
    }
  }

  Future<void> _nativeTTS(String text) async {
    // Fallback using flutter_tts package
  }
}
```

#### **2. Add Dependencies**
```yaml
# pubspec.yaml
dependencies:
  audioplayers: ^5.0.0  # Audio playback
  http: ^1.0.0          # API calls
```

#### **3. Test Voices**

Available voices (from `/api/v1/tts/voices`):
- **nova** ✅ Recommended for healthcare (warm, caring female)
- **shimmer** - Soft, friendly female
- **alloy** - Neutral, balanced
- **echo** - Male, authoritative
- **fable** - British accent, warm
- **onyx** - Deep, confident male

**Test in app**:
```dart
await ttsManager.playText(
  "Hello, it's time to take your medication.",
  voice: 'nova'
);
```

---

## 🔐 **Authentication & Permissions**

### **Firebase JWT Token**
```dart
// Get current user token
final user = FirebaseAuth.instance.currentUser;
final token = await user?.getIdToken();

// Use in API headers
headers: {
  'Authorization': 'Bearer $token',
}
```

### **ADMIN_KEY Bypass (Local Development)** 🔑
For local testing without Firebase setup, you can use ADMIN_KEY:

```bash
# Example curl request with ADMIN_KEY
curl -X POST "https://api.ella-ai-care.com/api/v1/tts/generate" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ADMIN_KEY_VALUE" \
  -d '{"text":"Test voice","voice":"nova","cache_key":"test_1"}'
```

**Note**: Ask Backend Developer or PM for the ADMIN_KEY value if needed for testing.

```dart
// In Flutter for dev testing
headers: {
  'Authorization': 'Bearer YOUR_ADMIN_KEY_HERE',  // Replace with actual key
  'Content-Type': 'application/json',
}
```

### **iOS Permissions (Info.plist)**
```xml
<!-- Microphone for recording -->
<key>NSMicrophoneUsageDescription</key>
<string>We need microphone access to record your conversations</string>

<!-- Bluetooth for necklace connection -->
<key>NSBluetoothAlwaysUsageDescription</key>
<string>We need Bluetooth to connect to your OMI necklace</string>

<!-- Notifications -->
<key>NSUserNotificationsUsageDescription</key>
<string>We send medication and health reminders</string>
```

---

## 🧪 **Testing**

### **Run Tests**
```bash
# Unit tests
flutter test

# Widget tests
flutter test test/widget_test.dart

# Integration tests
flutter drive --target=test_driver/app.dart
```

### **Debug on Device**
```bash
# Check connected devices
flutter devices

# Run with verbose logging
flutter run --verbose

# View logs
flutter logs
```

---

## 🚀 **Current Status & Priorities**

### ✅ **Completed**
- Memories display bug fixed (model + filter) - Commit 99f5701ff
- Backend API confirmed working
- App running on iPhone
- All code pushed to `feature/ios-backend-integration`

### 🔥 **CURRENT PRIORITIES** (from PM - October 31, 2025)

#### **1. TTS E2E Test** - 🔴 HIGH PRIORITY (ETA 4h)
**Assigned**: iOS Developer
**Goal**: Test all TTS API functionality end-to-end

**Test Steps**:
1. **Setup**: Use ADMIN_KEY bypass for auth (ask Backend/PM for key)
2. **Test All 6 Voices**: nova, shimmer, alloy, echo, fable, onyx
3. **Test Caching**:
   - First request with `cache_key`: Should take ~2-3s, `cached: false`
   - Second request with same `cache_key`: Should be instant <500ms, `cached: true`
4. **Test Without cache_key**: Should generate new audio each time
5. **Download & Play**: Download audio_url and play on device (AVAudioPlayer)
6. **Error Handling**: Simulate failure (wrong endpoint) to verify fallback
7. **Report Results**: Record timings, any playback issues, voice quality per voice

**Example Test Request**:
```bash
curl -X POST "https://api.ella-ai-care.com/api/v1/tts/generate" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -d '{"text":"Test voice","voice":"nova","cache_key":"test_voice_1"}'
```

**Report to PM**: Pass/fail per voice, timings, any issues found

#### **2. Integrate speakFromBackend** - ⏳ NEXT
- Location: `lib/services/audio/ella_tts_service.dart`
- Implement: Call TTS API, download audio, play
- Add: Fallback to native TTS on failure

#### **3. Add Settings UI** - 📋 TODO
- Location: `lib/pages/settings/developer.dart`
- Add: Developer toggle for TTS testing
- Add: Voice selector dropdown (6 voices)

#### **4. Report Test Results** - Required after #1
- Report all findings back to PM
- PM will convert issues into tasks

#### **5. Automated Unit Tests** - Optional
- Mock network calls for TTS service
- Test error handling and fallback paths

---

## 🐛 **Known Issues & Blockers**

### **1. Firestore Composite Index** ✅
**Symptom**: App timeout when loading memories
**Fix**: Backend created composite index (October 30)
**Status**: ✅ Resolved

### **2. BLE Connection Stability** ℹ️
**Symptom**: Necklace disconnects randomly
**Status**: No issues reported after re-org. Firmware dev available for verification if needed.
**Test**: Use `/Users/greg/repos/omi/check_ble_connection.py` if issues occur

### **3. Lookup Endpoint Missing** ⏳ (Doesn't block iOS)
**What**: `/api/admin/lookup-agent` endpoint not yet implemented
**Impact**: Blocks n8n & dashboard flows (not iOS development)
**Status**: Backend Developer implementing (all backend/infra work)
**Your Action**: None - doesn't affect iOS TTS testing

### **4. Speaker Hardware Unavailable** ℹ️
**What**: Firmware speaker code complete but DevKit2 lacks physical speaker hardware
**Impact**: Can't test device speaker output (TTS audio playback on phone is unaffected)
**Status**: Hardware availability unknown
**Workaround**: Test TTS playback on iPhone directly (not through device)

---

## 📞 **Team Coordination**

### **PM Agent** (Claude-PM)
- **Endpoint**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`
- **Script**: `/tmp/contact_pm_ios.py`
- **Contact for**: Task updates, test results, bugs, blockers, priorities
- **Tasks tracked**: PM memory system with detailed task history

### **Backend Developer** (Claude-Backend-Developer)
- **Role**: `backend_dev`
- **Location**: `/Users/greg/repos/omi/backend`
- **Contact for**:
  - ALL backend API work (TTS, STT, VAD, endpoints)
  - ALL infrastructure (deployment, VPS, monitoring)
  - ALL integration (webhooks, lookup APIs, n8n)
  - Auth tokens (ADMIN_KEY), backend logs, API changes
  - Database (Firestore) issues
- **Status**: TTS API fully operational, handles all server-side work

### **Firmware Developer** (Claude-Firmware-Developer)
- **Role**: `firmware_dev`
- **Location**: `/Users/greg/repos/omi/omi/firmware`
- **Contact for**: BLE issues, device identity, speaker firmware, hardware questions
- **Status**: Speaker firmware complete, DevKit2 testing available (no physical speaker)

### **Firmware Updates Affecting iOS**:
- Device identity: DevKit currently hardcodes `omi_uid = "ABC123"`
- Production: Uses last 3 bytes of chip ID (6 uppercase hex chars)
- BLE services: Operational, no stability issues reported
- Speaker: Firmware ready but hardware unavailable on DevKit2

---

## 🛠️ **Common Commands**

```bash
# Start development
cd /Users/greg/repos/omi/app
flutter pub get
flutter run

# 🔴 IMPORTANT: Run with dev flavor (required for phone deployment)
cd /Users/greg/repos/omi/app
flutter run --flavor dev

# Clean rebuild
flutter clean
cd ios && pod install && cd ..
flutter pub get
flutter run --flavor dev

# Check logs
flutter logs

# Build release
flutter build ios --release --flavor prod
```

---

---

**🎯 Ready to work on iOS tasks!**

**🔴 CURRENT HIGH PRIORITY**: TTS E2E Testing (ETA 4h)
- Test all 6 voices end-to-end
- Verify caching behavior
- Download and play audio on iPhone
- Report results to PM

**Backend API**: https://api.ella-ai-care.com/docs
**TTS Docs**: See TTS_BACKEND_PRD.md in app folder
**Contact PM**: Use `/tmp/contact_pm_ios.py` for updates

---

## 📝 **Git Commit Guidelines (iOS/Flutter)**

### **Commit Message Examples**
```bash
# Features
git commit -m "feat(tts): integrate backend TTS API with audio playback"
git commit -m "feat(ui): add TTS voice selector in settings"
git commit -m "feat(ble): improve connection stability handling"

# Fixes
git commit -m "fix(memories): resolve display bug from model mismatch"
git commit -m "fix(tts): add fallback to native TTS on API failure"
git commit -m "fix(ui): correct alignment in memories list view"

# Testing
git commit -m "test(tts): add e2e tests for all 6 voice options"
git commit -m "test(api): add unit tests for TTS service"

# Documentation
git commit -m "docs(setup): update Flutter installation instructions"
```

### **Files You Own**
iOS developers commit:
- `app/lib/**/*.dart` - All Flutter/Dart code
- `app/ios/**/*.swift` - Native Swift code
- `app/ios/Runner/Info.plist` - iOS configuration
- `app/pubspec.yaml` - Flutter dependencies
- `app/test/**` - Test files
- `app/README.md` - App documentation

### **Before Committing iOS Code**
```bash
# Format code
cd /Users/greg/repos/omi/app
dart format .

# Run tests
flutter test

# Build to verify no errors
flutter build ios --debug

# Review changes
git status
git diff

# Commit
git add app/path/to/files
git commit -m "feat(scope): description"
```

### **Current Branch**: `feature/ios-backend-integration`

### **iOS-Specific Notes**
- Always test on physical device before committing UI changes
- Include screenshots in PR for UI changes
- Update `pubspec.yaml` version when releasing
- Run `flutter pub get` after dependency changes

See root CLAUDE.md for general git guidelines.
