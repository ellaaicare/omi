# Ella Branding & Bluetooth TTS Analysis

**Date:** October 29, 2025
**Status:** Analysis Complete - Ready for Implementation
**App:** Omi iOS (customized as Ella AI Care)

---

## 📱 Part 1: Ella Branding Implementation

### Current State: Omi Branding Locations

The app currently uses **Omi-branded images** in multiple locations:

#### **Critical Images to Replace:**

| Image File | Size | Usage | Priority |
|------------|------|-------|----------|
| `splash.png` | 23KB | Native splash screen (first thing users see) | **HIGH** |
| `onboarding-bg-2.webp` | ? | Auth/Sign-in screen background | **HIGH** |
| `onboarding-bg-5-1.webp` | ? | Welcome page - "Connect Device" half | **HIGH** |
| `onboarding-bg-5-2.webp` | ? | Welcome page - "Continue Without Device" half | **HIGH** |
| `onboarding-bg-1.webp` | ? | Name input screen | MEDIUM |
| `onboarding-bg-3.webp` | ? | Permissions screen | MEDIUM |
| `onboarding-bg-4.webp` | ? | Language selection screen | MEDIUM |
| `onboarding-bg-6.webp` | ? | User review screen | MEDIUM |
| `background.png` | 301KB | Chat/conversation backgrounds | LOW |
| `new_background.png` | 149KB | Persona/profile pages | LOW |
| `herologo.png` | 12KB | App icon in various places | **HIGH** |

#### **Text Changes Required:**

**File:** `lib/pages/onboarding/auth.dart:64`
```dart
// CURRENT:
'Speak. Transcribe. Summarize.',

// CHANGE TO:
'Ella AI Care - Your Personal Health Companion',
```

**File:** `lib/pages/onboarding/device_onboarding/device_onboarding_page.dart`
Lines 36-60: Update all device references from "Omi" to "Ella":
- "Charging Your Omi" → "Charging Your Ella Device"
- "your Omi" → "your Ella device"
- "Omi will respond" → "Ella will respond"

### Implementation Steps:

1. **Get Ella Logo from Website**
   - Download logo from https://www.ella-ai-care.com
   - Create multiple sizes needed (splash, icon, backgrounds)

2. **Create Branded Background Images**
   - Use Ella brand colors and logo
   - Maintain same dimensions as originals
   - Convert to WebP format for onboarding screens

3. **Replace Image Assets**
   ```bash
   # Backup originals
   cp -r assets/images assets/images.backup

   # Replace files (after creating new ones)
   cp ella-splash.png assets/images/splash.png
   cp ella-logo.png assets/images/herologo.png
   # ... etc for all onboarding backgrounds
   ```

4. **Update Text References**
   - Search and replace "Omi" with "Ella" in onboarding files
   - Update tagline to Ella AI branding

5. **Regenerate Splash Screen**
   ```bash
   flutter pub run flutter_native_splash:create
   ```

---

## 🎧 Part 2: Bluetooth Headset TTS Analysis

### Current Bluetooth Architecture

**✅ EXCELLENT NEWS:** The app **already has robust Bluetooth Low Energy (BLE) support!**

#### **Existing BLE Infrastructure:**

**File:** `lib/services/devices/transports/ble_transport.dart`
- Full BLE device connection management
- MTU negotiation (up to 512 bytes for better performance)
- Service discovery
- Characteristic read/write/notify support
- Connection state management

**File:** `lib/services/devices/omi_connection.dart`
- Manages connection to Omi necklace
- Handles audio streaming over BLE
- Device pairing and state management

**File:** `lib/backend/schema/bt_device/bt_device.dart`
- Device model with all BLE metadata

### Feasibility Analysis: Bluetooth Headset TTS Routing

#### **Option 1: Route to Phone Audio (EASIEST - iOS Native)**
**Feasibility:** ✅ **HIGHLY FEASIBLE**

**How it works:**
- Use iOS native audio routing
- When Bluetooth headset is connected (e.g., AirPods), iOS automatically routes to it
- No custom BLE needed - uses standard iOS audio APIs

**Implementation:**
```dart
// Use flutter_tts package (already proven)
import 'package:flutter_tts/flutter_tts.dart';

class EllaAudioService {
  final FlutterTts _tts = FlutterTts();

  Future<void> speak(String text) async {
    // iOS automatically routes to connected Bluetooth audio devices
    await _tts.setLanguage("en-US");
    await _tts.setSpeechRate(0.5);
    await _tts.speak(text);
  }
}
```

**Pros:**
- iOS handles routing automatically
- Works with ALL Bluetooth headsets (AirPods, Beats, etc.)
- Simple implementation
- No special permissions needed

**Cons:**
- Requires notification to be "spoken" instead of shown
- Users must have Bluetooth audio device paired

---

#### **Option 2: Route to Omi Necklace Speaker (CUSTOM BLE)**
**Feasibility:** ⚠️ **POSSIBLE BUT COMPLEX**

**Requirements:**
- Omi necklace must have a speaker (check hardware specs)
- Custom audio codec over BLE (likely Opus, already used in app)
- BLE bandwidth limitations (may affect quality)

**Existing Audio Infrastructure:**
```dart
// lib/utils/audio/wav_bytes.dart - Already handles audio encoding
// lib/providers/capture_provider.dart - Audio streaming to server
```

**How it would work:**
1. Generate TTS audio on phone (Whisper, Cartesia, etc.)
2. Encode audio to Opus format (already implemented)
3. Send via BLE to Omi necklace
4. Necklace plays audio through speaker

**Challenges:**
- Omi device may not have speaker (it's primarily a microphone)
- BLE bandwidth ~1 Mbps (may not be enough for high-quality audio)
- Battery drain on necklace
- Custom firmware needed on Omi device

---

#### **Option 3: Dual Audio Routing (HYBRID)**
**Feasibility:** ✅ **RECOMMENDED APPROACH**

**How it works:**
1. Check if Bluetooth headset is connected → Use Option 1 (iOS routing)
2. If no headset, but Omi connected → Use Option 2 (necklace speaker)
3. Fallback to phone speaker

**Implementation:**
```dart
class EllaSmartAudioRouter {
  final FlutterTts _tts = FlutterTts();

  Future<void> speakNotification(String message) async {
    // Check for connected Bluetooth audio
    bool hasBluetoothAudio = await _checkBluetoothAudioConnected();

    if (hasBluetoothAudio) {
      // Route via iOS to headset
      await _tts.speak(message);
    } else if (omiDeviceConnected && omiHasSpeaker) {
      // Route via BLE to Omi necklace
      await _sendAudioToOmi(message);
    } else {
      // Fallback to notification
      _showNotification(message);
    }
  }
}
```

---

### Apple iOS Allowances & Restrictions

#### **✅ ALLOWED:**
- Playing audio via TTS to any connected Bluetooth audio device
- Using AVAudioSession to manage audio routing
- Background audio playback (requires background mode)
- BLE communication for custom protocols

#### **⚠️ RESTRICTIONS:**
- Cannot force audio to specific Bluetooth device (iOS controls routing)
- Background BLE has limitations (must use specific patterns)
- Battery usage concerns with continuous BLE audio streaming

#### **Required Info.plist Permissions:**
```xml
<!-- Already have Bluetooth permission -->
<key>NSBluetoothAlwaysUsageDescription</key>
<string>Bluetooth permission is required to connect with your Ella device.</string>

<!-- Need to add audio background mode -->
<key>UIBackgroundModes</key>
<array>
  <string>audio</string>  <!-- Already present! -->
  <string>bluetooth-central</string>  <!-- Already present! -->
</array>
```

**✅ ALL PERMISSIONS ALREADY IN PLACE!**

---

### Recommended Implementation Plan

#### **Phase 1: Basic TTS to Bluetooth Headset (1-2 days)**
1. Add `flutter_tts` package to pubspec.yaml
2. Create `EllaAudioService` class
3. Integrate with notification system
4. Test with AirPods/Bluetooth headphones

#### **Phase 2: Smart Audio Routing (3-5 days)**
1. Detect connected Bluetooth audio devices
2. Implement fallback logic (headset → necklace → notification)
3. Add user preferences for audio output
4. Settings toggle: "Speak notifications" vs "Show notifications"

#### **Phase 3: Advanced TTS Integration (1-2 weeks)**
1. Integrate Whisper for on-device TTS (fast, private)
2. Integrate Cartesia API for high-quality voices (cloud-based)
3. Voice selection in settings
4. Caching for frequently used phrases

#### **Phase 4: Omi Necklace Speaker (Advanced - IF hardware supports)**
1. Confirm Omi has speaker capability
2. Implement audio encoding for BLE transmission
3. Custom BLE characteristic for audio streaming
4. Necklace firmware update (if needed)

---

### TTS Engine Options

| Engine | Type | Pros | Cons |
|--------|------|------|------|
| **flutter_tts** | iOS native | Simple, free, works offline | Generic voice |
| **Whisper** | On-device | Fast, private, offline | Requires ML model |
| **Cartesia** | Cloud API | High quality, natural voices | Requires internet, costs $ |
| **ElevenLabs** | Cloud API | Ultra-realistic voices | Higher cost |

**Recommendation:** Start with `flutter_tts` for MVP, add Cartesia for premium voices later.

---

### Code Structure for TTS Implementation

```
lib/
├── services/
│   ├── audio/
│   │   ├── ella_audio_service.dart          # Main audio routing service
│   │   ├── tts_engine_interface.dart        # Abstract TTS interface
│   │   ├── native_tts_engine.dart           # flutter_tts implementation
│   │   ├── whisper_tts_engine.dart          # Whisper integration
│   │   └── cartesia_tts_engine.dart         # Cartesia API integration
│   └── notifications/
│       └── audio_notification_handler.dart   # Convert notifications to audio
└── providers/
    └── audio_settings_provider.dart         # User preferences
```

---

## 🎯 Next Steps

### Immediate Actions:

1. **Ella Branding:**
   - [ ] Download Ella logo from website
   - [ ] Create 10 branded images (splash + 7 onboarding + herologo + backgrounds)
   - [ ] Replace image files in `assets/images/`
   - [ ] Update text references ("Omi" → "Ella")
   - [ ] Rebuild app and test on iPhone

2. **Bluetooth TTS Proof of Concept:**
   - [ ] Add `flutter_tts: ^3.8.5` to pubspec.yaml
   - [ ] Create simple TTS service
   - [ ] Test with AirPods connected to your iPhone
   - [ ] Verify audio routing works automatically

### Questions to Answer:

1. **Does Omi necklace have a speaker?** (Check hardware specs)
2. **What TTS voice quality do you want?** (Free/basic vs paid/premium)
3. **Should ALL notifications be spoken?** Or only certain types?
4. **User control:** Toggle in settings for TTS on/off?

---

## 💡 Key Insights

### ✅ Excellent Foundation:
- App already has complete BLE infrastructure
- Background audio mode already enabled
- All necessary iOS permissions in place
- Audio encoding/decoding already implemented

### 🚀 Quick Wins Available:
- Basic TTS to Bluetooth headset: **2 hours of work**
- Replace all images with Ella branding: **4 hours of work** (after getting logo)
- Both can be tested on your iPhone immediately

### 🎯 Strategic Recommendation:
Start with **Bluetooth headset TTS** (Option 1) because:
1. Works with devices users already own (AirPods, etc.)
2. Requires no custom hardware
3. iOS handles routing automatically
4. Can be implemented TODAY

Then add **Ella branding** to complete the customization.

---

**Ready to proceed?** Let me know if you want me to:
1. Download Ella logo and create branded images
2. Implement basic TTS service for testing
3. Both!

