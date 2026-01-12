# Ella Extensions Architecture

**Last Updated**: January 11, 2026
**Status**: Skeleton implementation ready for porting

---

## Overview

Ella Extensions is a modular plugin system that keeps all Ella-specific functionality isolated from upstream OMI code. This allows us to:

1. **Pull fresh from upstream** without losing our customizations
2. **Re-apply extensions** quickly after upstream sync
3. **Port working code** from standalone Ella app cleanly
4. **Maintain clear boundaries** between OMI core and Ella features

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         OMI App                                  │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                  ELLA EXTENSIONS LAYER                     │  │
│  │                                                             │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐  │  │
│  │  │  WakeWord   │ │  VoiceV2V   │ │       TTS           │  │  │
│  │  │  Plugin     │ │  Plugin     │ │      Plugin         │  │  │
│  │  └─────────────┘ └─────────────┘ └─────────────────────┘  │  │
│  │                                                             │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │                  AudioPush Plugin                    │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  │                                                             │  │
│  │  Entry Point: EllaExtensions() (singleton)                 │  │
│  │  Config: EllaConfig() (SharedPreferences)                  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    OMI CORE (Upstream)                     │  │
│  │                                                             │  │
│  │  Conversations, Memories, BLE, Transcription, etc.         │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Folder Structure

```
lib/ella/
├── extensions.dart              # Main entry point (singleton)
├── config/
│   └── ella_config.dart         # Ella-specific settings
├── plugins/
│   ├── base_plugin.dart         # Plugin interface
│   ├── wake_word/
│   │   └── wake_word_plugin.dart
│   ├── voice_v2v/
│   │   └── voice_v2v_plugin.dart
│   ├── tts/
│   │   └── ella_tts_plugin.dart
│   └── audio_push/
│       └── audio_push_plugin.dart
└── ELLA_EXTENSIONS_README.md    # This file

ios/Runner/Ella/
├── WakeWordPlugin.swift         # Native wake word / ASR
├── VoiceV2VPlugin.swift         # Native mic capture
├── NativeTtsPlugin.swift        # Native TTS fallback
└── AudioPushPlugin.swift        # Push notification audio

ella_extensions_backup/          # Backup for re-application
├── lib/ella/                    # Copy of lib/ella
└── ios/Ella/                    # Copy of ios/Runner/Ella

scripts/
└── reapply_ella_extensions.sh   # Re-apply after upstream pull
```

---

## Plugins

### 1. WakeWord Plugin

**Purpose**: Detect wake words ("Hey Ella") and trigger voice calls

**Status**: Skeleton - PORT FROM STANDALONE ELLA APP

**Key Files**:
- `lib/ella/plugins/wake_word/wake_word_plugin.dart`
- `ios/Runner/Ella/WakeWordPlugin.swift`

**Features to Port**:
- [ ] On-device ASR (iOS SFSpeechRecognizer)
- [ ] Continuous listening with Bluetooth support
- [ ] Wake word string matching
- [ ] Chime playback on detection
- [ ] 3-second debounce

**Integration**:
```dart
// Automatically wired in EllaExtensions._setupDefaultWiring()
wakeWord.onWakeWordDetected = () {
  voiceV2V.startCall();
};
```

---

### 2. VoiceV2V Plugin

**Purpose**: Real-time voice conversations with Grok V2V backend

**Status**: Skeleton - PORT FROM STANDALONE ELLA APP

**Key Files**:
- `lib/ella/plugins/voice_v2v/voice_v2v_plugin.dart`
- `ios/Runner/Ella/VoiceV2VPlugin.swift`

**Features to Port**:
- [ ] WebSocket connection to /v2/voice
- [ ] Native mic capture (PCM16 @ 16kHz)
- [ ] TTS audio playback (PCM16 @ 24kHz)
- [ ] State management (inactive/connecting/active/speaking)
- [ ] Bluetooth audio routing

**Usage**:
```dart
// Start call
await EllaExtensions().voiceV2V.startCall(pipelineMode: 'grok_v2v');

// Send audio (called continuously from mic)
EllaExtensions().voiceV2V.sendAudio(audioBytes);

// End call
await EllaExtensions().voiceV2V.endCall();
```

---

### 3. TTS Plugin

**Purpose**: Text-to-speech with modular provider architecture

**Status**: IMPLEMENTED (ready for use)

**Key Files**:
- `lib/ella/plugins/tts/ella_tts_plugin.dart`
- `ios/Runner/Ella/NativeTtsPlugin.swift`

**Providers**:
- `EllaBackendTTSProvider`: Our backend API with Redis caching (default)
- `NativeTTSProvider`: iOS AVSpeechSynthesizer fallback

**Usage**:
```dart
// Speak with default voice
await EllaExtensions().tts.speak('Hello!');

// Speak with specific voice
await EllaExtensions().tts.speak('Time for medication', voice: 'shimmer');

// Change provider
EllaExtensions().tts.setProvider(NativeTTSProvider());
```

**Adding New Providers**:
```dart
class MyCustomProvider implements TTSProvider {
  @override String get name => 'MyCustom';

  @override
  Future<String> generateAudioUrl(String text, {String? voice}) async {
    // Call your TTS API
    return 'https://example.com/audio.mp3';
  }

  @override
  Future<List<String>> getVoices() async => ['voice1', 'voice2'];
}

// Use it
EllaExtensions().tts.setProvider(MyCustomProvider());
```

---

### 4. AudioPush Plugin

**Purpose**: Play audio from push notifications

**Status**: Skeleton - PORT FROM CURRENT FORK

**Key Files**:
- `lib/ella/plugins/audio_push/audio_push_plugin.dart`
- `ios/Runner/Ella/AudioPushPlugin.swift`

**Features to Port**:
- [ ] Background audio playback
- [ ] Push notification handling
- [ ] Audio session configuration
- [ ] Bluetooth routing

**Push Payload Format**:
```json
{
  "aps": { "alert": "Ella", "sound": "default" },
  "data": {
    "type": "audio_message",
    "audio_url": "https://storage.googleapis.com/.../message.mp3",
    "text": "Time to take your medication"
  }
}
```

---

## Configuration

All Ella settings are in `EllaConfig` (stored in SharedPreferences):

| Setting | Default | Description |
|---------|---------|-------------|
| `wakeWordEnabled` | true | Enable wake word detection |
| `wakeWords` | ["hey ella", "hi ella", "hello ella", "ella"] | Wake words to detect |
| `autoStartCallOnWakeWord` | true | Auto-start V2V call on detection |
| `wakeWordDebounceSec` | 3 | Seconds between detections |
| `voicePipelineMode` | "grok_v2v" | Voice pipeline mode |
| `voiceWsBaseUrl` | "wss://api.ella-ai-care.com" | Voice WebSocket URL |
| `voiceModeEnabled` | true | Enable voice mode |
| `ttsApiBaseUrl` | "https://api.ella-ai-care.com" | TTS API URL |
| `ttsDefaultVoice` | "nova" | Default TTS voice |
| `ttsCachingEnabled` | true | Enable TTS caching |
| `ttsFallbackToNative` | true | Fall back to native TTS |
| `audioPushEnabled` | true | Enable audio in push |

---

## Integration with OMI Core

### main.dart

Add after OMI initialization:

```dart
import 'package:omi/ella/extensions.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // ... OMI initialization ...

  // Initialize Ella extensions
  await EllaExtensions().initialize();

  runApp(MyApp());
}
```

### Forwarding Events

Connect Ella to OMI lifecycle:

```dart
// When OMI receives transcript
EllaExtensions().onTranscriptReceived(text);

// When conversation starts
EllaExtensions().onConversationStarted();

// When conversation ends
EllaExtensions().onConversationEnded();

// When device connects
EllaExtensions().onDeviceConnected(deviceId);
```

### iOS AppDelegate.swift

Register plugins:

```swift
@UIApplicationMain
class AppDelegate: FlutterAppDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {

    // Register Ella plugins
    if let registrar = self.registrar(forPlugin: "WakeWordPlugin") {
      WakeWordPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "VoiceV2VPlugin") {
      VoiceV2VPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "NativeTtsPlugin") {
      NativeTtsPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "AudioPushPlugin") {
      AudioPushPlugin.register(with: registrar)
    }

    // ... rest of initialization ...

    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }
}
```

---

## Porting from Standalone Ella App

### What to Port

| Component | Source | Target | Priority |
|-----------|--------|--------|----------|
| Wake Word Detection | Standalone Ella `WakeWordDetector.swift` | `WakeWordPlugin.swift` + `wake_word_plugin.dart` | HIGH |
| Voice V2V | Standalone Ella `GrokVoiceSession.swift` | `VoiceV2VPlugin.swift` + `voice_v2v_plugin.dart` | HIGH |
| Audio Push | Current fork `AppDelegate.swift` | `AudioPushPlugin.swift` + `audio_push_plugin.dart` | MEDIUM |

### Porting Checklist

1. **Read the skeleton code** - Each plugin has TODO comments explaining what to port
2. **Copy working code** from standalone Ella app
3. **Adapt to plugin pattern** - Use method channels for Flutter communication
4. **Test incrementally** - Test each plugin independently
5. **Wire together** - Connect plugins via EllaExtensions

---

## Debugging

### Get Extension Status

```dart
final status = EllaExtensions().getStatus();
print(jsonEncode(status));
```

Output:
```json
{
  "initialized": true,
  "config": { ... },
  "plugins": {
    "WakeWord": { "enabled": true, "isActive": true, ... },
    "VoiceV2V": { "state": "inactive", ... },
    "EllaTTS": { "provider": "EllaBackend", ... },
    "AudioPush": { "enabled": true, "isPlaying": false }
  }
}
```

### Plugin-Specific Status

```dart
print(EllaExtensions().wakeWord.getStatus());
print(EllaExtensions().voiceV2V.getStatus());
```

---

## Re-applying After Upstream Pull

See `UPSTREAM_PULL_INSTRUCTIONS.md` for complete instructions.

Quick version:
```bash
cd /Users/greg/repos/omi/app
./scripts/reapply_ella_extensions.sh
flutter pub get
flutter run --flavor dev
```

---

## FAQ

**Q: Why not modify upstream code directly?**
A: Keeping Ella code isolated makes upstream syncs easy. We just pull and re-apply.

**Q: Can I add new plugins?**
A: Yes! Extend `EllaPlugin`, add to `EllaExtensions`, and register in AppDelegate.

**Q: What if upstream changes break integration points?**
A: Integration points are minimal (main.dart + transcript forwarding). Usually just needs minor adjustments.

**Q: Is this backward compatible with current fork?**
A: The current fork's code doesn't work well anyway. This is a clean start designed for portability.

---

## Contact

- **iOS Developer**: Claude-iOS-Developer
- **GitHub Issues**: https://github.com/ellaaicare/omi/issues
- **PM Agent**: Contact via `/tmp/contact_pm_ios.py`
