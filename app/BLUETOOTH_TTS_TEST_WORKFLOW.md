# Bluetooth TTS Testing Workflow

**Project:** Ella AI Care - Bluetooth Headset TTS Integration
**Date:** October 29, 2025
**Status:** Test Plan Ready for Implementation

---

## 🎯 Testing Challenge

**Problem:** How do we test Bluetooth TTS audio routing when there are no memories, transcripts, or real user data yet?

**Solution:** Add a **Developer Test Interface** in Settings → Developer Settings for isolated TTS testing.

---

## 🧪 Test Implementation Plan

### Phase 1: Add TTS Test Interface to Developer Settings

**Location:** Extend existing Developer Settings page with new "Audio & TTS Testing" section

**File to Modify:** `lib/pages/settings/developer.dart`

#### UI Components to Add:

```dart
// New section in Developer Settings:
'Audio & TTS Testing',
├─ [Toggle] Enable TTS for Notifications
├─ [Button] Test TTS Output
├─ [TextField] Custom Test Message
├─ [Dropdown] Voice Selection
├─ [Info Card] Bluetooth Status
└─ [Button] Speak Sample Health Reminder
```

---

### Phase 2: Create TTS Service

**New File:** `lib/services/audio/ella_tts_service.dart`

#### Features:
- Flutter TTS integration
- Bluetooth device detection
- Audio routing logic
- Test mode for development

#### Sample Implementation:

```dart
import 'package:flutter_tts/flutter_tts.dart';

class EllaTtsService {
  final FlutterTts _flutterTts = FlutterTts();
  bool _isInitialized = false;

  // Initialize TTS engine
  Future<void> initialize() async {
    if (_isInitialized) return;

    await _flutterTts.setLanguage("en-US");
    await _flutterTts.setSpeechRate(0.5); // Normal speed
    await _flutterTts.setVolume(1.0); // Full volume
    await _flutterTts.setPitch(1.0); // Normal pitch

    // iOS automatically routes to connected Bluetooth audio!

    _isInitialized = true;
  }

  // Speak text (auto-routes to Bluetooth if available)
  Future<void> speak(String text) async {
    await initialize();
    await _flutterTts.speak(text);
  }

  // Stop speaking
  Future<void> stop() async {
    await _flutterTts.stop();
  }

  // Get available voices
  Future<List<dynamic>> getVoices() async {
    return await _flutterTts.getVoices;
  }

  // Check if Bluetooth audio is connected (iOS-specific)
  Future<bool> isBluetoothAudioConnected() async {
    // iOS AVAudioSession detection
    // Note: This requires platform channel implementation
    return false; // Placeholder
  }
}
```

---

## 📱 Test Interface Design

### Developer Settings → Audio Testing Section

```
┌─────────────────────────────────────────┐
│  Developer Settings                      │
├─────────────────────────────────────────┤
│                                          │
│  [Existing Infrastructure section...]   │
│                                          │
│  ────────────────────────────────────   │
│                                          │
│  📢 Audio & TTS Testing                 │
│                                          │
│  Bluetooth Status:                       │
│  ┌──────────────────────────────────┐  │
│  │ ✅ AirPods Pro Connected         │  │
│  │ 🎧 Audio will route to headset   │  │
│  └──────────────────────────────────┘  │
│                                          │
│  ⚙️ Enable TTS for Notifications        │
│     [Toggle Switch: OFF]                │
│     When enabled, notifications will     │
│     be spoken instead of shown.          │
│                                          │
│  ────────────────────────────────────   │
│                                          │
│  🧪 TTS Test Controls                   │
│                                          │
│  Test Message:                           │
│  ┌──────────────────────────────────┐  │
│  │ Hello, this is Ella AI Care.     │  │
│  │ I'm your health companion.       │  │
│  └──────────────────────────────────┘  │
│                                          │
│  [🔊 Speak Test Message]                │
│  [🛑 Stop Speaking]                     │
│                                          │
│  ────────────────────────────────────   │
│                                          │
│  Quick Test Samples:                     │
│  [💊 Medication Reminder]               │
│  [📅 Appointment Alert]                 │
│  [💬 New Health Message]                │
│  [🏃 Activity Goal Achieved]            │
│                                          │
└─────────────────────────────────────────┘
```

---

## 🧪 Test Scenarios

### Test Scenario 1: Basic TTS Functionality
**Steps:**
1. Open app → Settings → Developer Settings
2. Scroll to "Audio & TTS Testing"
3. Tap "Speak Test Message" button
4. **Expected:** Hear default message through phone speaker

**Pass Criteria:** Audio plays successfully

---

### Test Scenario 2: Bluetooth Headset Routing
**Prerequisites:** AirPods or Bluetooth headset connected to iPhone

**Steps:**
1. Connect AirPods to iPhone (verify in iOS Bluetooth settings)
2. Open app → Developer Settings → Audio Testing
3. Verify "Bluetooth Status" shows "✅ AirPods Connected"
4. Tap "Speak Test Message"
5. **Expected:** Hear audio through AirPods, NOT phone speaker

**Pass Criteria:** Audio routes to Bluetooth device automatically

---

### Test Scenario 3: Custom Message Testing
**Steps:**
1. Developer Settings → Audio Testing
2. Edit "Test Message" field with custom text:
   - "Your medication reminder: Take your blood pressure pill now."
3. Tap "Speak Test Message"
4. **Expected:** Custom message is spoken

**Pass Criteria:** Any text can be spoken via TTS

---

### Test Scenario 4: Sample Health Reminders
**Steps:**
1. Developer Settings → Audio Testing
2. Tap "💊 Medication Reminder" quick test button
3. **Expected:** Hear: "Reminder: It's time to take your medication."
4. Repeat for other sample buttons

**Pass Criteria:** All pre-configured messages work

---

### Test Scenario 5: Bluetooth Disconnect Handling
**Steps:**
1. Start with AirPods connected
2. Verify audio routes to AirPods
3. Disconnect AirPods (put in case or turn off Bluetooth)
4. Tap "Speak Test Message" again
5. **Expected:** Audio routes to phone speaker automatically

**Pass Criteria:** Graceful fallback when Bluetooth disconnects

---

### Test Scenario 6: Background/Foreground Testing
**Steps:**
1. Start TTS playback
2. Press home button (app goes to background)
3. **Expected:** Audio continues playing
4. Return to app
5. **Expected:** No crashes, smooth transition

**Pass Criteria:** TTS works in background

---

### Test Scenario 7: Voice Selection (Advanced)
**Steps:**
1. Developer Settings → Audio Testing
2. Tap "Voice Selection" dropdown
3. Choose different iOS voices (Samantha, Alex, etc.)
4. Speak test message
5. **Expected:** Voice changes accordingly

**Pass Criteria:** Different voices can be selected

---

## 📋 Test Sample Messages

Pre-configure these sample messages for one-tap testing:

### Medication Reminder:
```
"Reminder: It's time to take your blood pressure medication. Please take one pill with water."
```

### Appointment Alert:
```
"You have a doctor's appointment tomorrow at 2 PM with Dr. Smith. Don't forget to bring your insurance card."
```

### New Health Message:
```
"New message from your healthcare provider. Please check your Ella app when convenient."
```

### Activity Goal:
```
"Congratulations! You've reached your daily step goal of 10,000 steps. Keep up the great work!"
```

### Health Check-In:
```
"Good morning! How are you feeling today? Remember to log your symptoms in the Ella app."
```

---

## 🛠️ Implementation Checklist

### Step 1: Add Dependencies
**File:** `pubspec.yaml`

```yaml
dependencies:
  flutter_tts: ^3.8.5  # Latest TTS package
```

Run: `flutter pub get`

---

### Step 2: Create TTS Service
**File:** `lib/services/audio/ella_tts_service.dart`

- [ ] Implement FlutterTts wrapper
- [ ] Add speak(), stop(), initialize() methods
- [ ] Add Bluetooth detection (iOS AVAudioSession)
- [ ] Add voice selection
- [ ] Add error handling

---

### Step 3: Extend Developer Settings
**File:** `lib/pages/settings/developer.dart`

- [ ] Add "Audio & TTS Testing" section UI
- [ ] Add test message TextField
- [ ] Add "Speak Test Message" button
- [ ] Add sample message quick buttons
- [ ] Add Bluetooth status indicator
- [ ] Add TTS enable/disable toggle

---

### Step 4: Wire Up Test Buttons

```dart
// Example button implementation:
ElevatedButton(
  onPressed: () async {
    final tts = EllaTtsService();
    await tts.speak("Hello, this is Ella AI Care. I'm your health companion.");
  },
  child: Text('🔊 Speak Test Message'),
)
```

---

### Step 5: Add Provider for Settings
**File:** `lib/providers/audio_settings_provider.dart`

```dart
class AudioSettingsProvider extends ChangeNotifier {
  bool _ttsEnabled = false;
  String _selectedVoice = 'en-US-default';

  bool get ttsEnabled => _ttsEnabled;
  String get selectedVoice => _selectedVoice;

  void toggleTts(bool value) {
    _ttsEnabled = value;
    notifyListeners();
  }

  void setVoice(String voice) {
    _selectedVoice = voice;
    notifyListeners();
  }
}
```

---

## 🔍 Testing Without Real Data

### Why This Approach Works:

1. **Isolated Testing:** Test TTS independently of app data
2. **Immediate Feedback:** No need to wait for notifications
3. **Controllable:** Trigger tests on demand
4. **Debuggable:** See exactly what's being spoken
5. **Iteration:** Quickly test different messages, voices, settings

### Future Integration:

Once TTS is proven working in Developer Settings, integrate it with actual notifications:

```dart
// In notification_service_fcm.dart:
if (SharedPreferencesUtil().ttsEnabled) {
  // Speak notification instead of showing it
  await EllaTtsService().speak(message.notification?.body ?? '');
} else {
  // Show traditional notification
  _showForegroundNotification(noti: noti, payload: payload);
}
```

---

## 📊 Success Metrics

### Test Completion Criteria:

- [ ] TTS plays through phone speaker
- [ ] TTS automatically routes to connected Bluetooth headset
- [ ] TTS works in background
- [ ] TTS handles disconnection gracefully
- [ ] Custom messages can be tested
- [ ] Multiple voices selectable
- [ ] No crashes or memory leaks
- [ ] Latency < 1 second from button press to audio start

---

## 🚀 Quick Start Testing Guide

### For You (Greg) - 5-Minute Test:

1. **I'll implement the TTS test interface** (30 min)
2. **Build and install on your iPhone** (5 min)
3. **Open app → Settings → Developer Settings**
4. **Connect your AirPods**
5. **Tap "Speak Test Message"**
6. **Listen for audio in AirPods!** ✅

**No memories or transcripts needed - just instant TTS testing!**

---

## 💡 Advanced Testing Features (Future)

Once basics work, add:

- [ ] **Audio waveform visualization** while speaking
- [ ] **Volume control** in test interface
- [ ] **Speed adjustment** (slow/normal/fast)
- [ ] **Pitch adjustment** (deep/normal/high)
- [ ] **Recording of TTS output** for quality testing
- [ ] **A/B testing** different TTS engines
- [ ] **Latency measurement** (time from trigger to audio)

---

## 🎯 Timeline

### Immediate (Today):
- Add flutter_tts dependency: **5 min**
- Create basic TTS service: **30 min**
- Add test UI to Developer Settings: **45 min**
- Test on your iPhone with AirPods: **15 min**

**Total: ~90 minutes to working TTS demo!**

### This Week:
- Refine voice selection
- Add all sample messages
- Bluetooth status detection
- Polish UI

### Next Week:
- Integrate with actual notifications
- Add user preferences
- Production testing

---

## ✅ What You'll See After Implementation

When you open Developer Settings, you'll have a complete TTS testing laboratory:

```
You: *Tap "Speak Test Message"*
Your AirPods: "Hello, this is Ella AI Care. I'm your health companion."

You: *Tap "Medication Reminder"*
Your AirPods: "Reminder: It's time to take your blood pressure medication."

You: *Type custom message: "Testing 1, 2, 3"*
You: *Tap "Speak"*
Your AirPods: "Testing 1, 2, 3"
```

**No memories needed - just pure TTS testing bliss!** 🎧✨

---

**Ready to implement?** I can have this working on your iPhone in ~90 minutes!
