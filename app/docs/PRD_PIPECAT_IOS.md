# PRD: Pipecat v2 iOS Integration

**Version**: 1.0
**Date**: December 1, 2025
**Owner**: iOS Team
**Status**: Approved
**Depends On**: Backend PRD (must be deployed first)

---

## Overview

Integrate iOS app with new `/v2/voice` Pipecat endpoint for real-time voice conversations with Ella AI.

**Goal**: Replace custom turn-detection logic with server-side VAD, enabling natural voice conversations.

---

## Scope

### In Scope
- New `VoiceModeV2Manager` class
- WebSocket connection to `/v2/voice`
- Audio capture and playback
- Feature flag for v1/v2 switching
- Remove client-side turn-detection code

### Out of Scope
- Changes to memory recording flow (`/v4/listen`)
- On-device ASR (not used for voice mode v2)
- WebRTC transport (Phase 2)
- Pipecat Swift SDK (evaluate for Phase 2)

---

## Architecture

### Current v1 Flow (Keep for Fallback)

```
Voice Button → On-device ASR → Text-change detection (1.5s) →
voice_utterance event → Backend LLM → TTS chunks → Audio playback
```

**Problems**: Fragile turn detection, no interruption support, 500+ lines of detection code.

### New v2 Flow

```
Voice Button → Raw Audio → WebSocket /v2/voice →
[Backend: VAD → STT → LLM → TTS] → Audio chunks → Playback
```

**Benefits**: Server-side VAD, automatic turn detection, native interruption support.

---

## Technical Specification

### File Structure

```
app/lib/
├── providers/
│   └── voice_mode_v2_provider.dart    # NEW: V2 state management
├── services/
│   └── voice_mode_v2_service.dart     # NEW: WebSocket + audio
├── pages/
│   └── capture/
│       └── widgets/
│           └── voice_mode_button.dart # MODIFY: Add v2 support
```

### Feature Flag

```dart
// lib/config/feature_flags.dart

class FeatureFlags {
  static const bool usePipecatVoiceV2 = true;  // Toggle v1/v2

  static String get voiceEndpoint =>
      usePipecatVoiceV2 ? '/v2/voice' : '/v4/listen';
}
```

### VoiceModeV2Service

```dart
// lib/services/voice_mode_v2_service.dart

import 'dart:async';
import 'dart:typed_data';
import 'package:web_socket_channel/web_socket_channel.dart';

class VoiceModeV2Service {
  WebSocketChannel? _channel;
  StreamSubscription? _audioSubscription;

  final String baseUrl = 'wss://api.ella-ai-care.com';

  // Audio format: PCM16, 16kHz, mono
  static const int sampleRate = 16000;
  static const int channels = 1;
  static const int bitsPerSample = 16;

  /// Start voice session
  Future<void> startSession(String uid) async {
    final sessionId = const Uuid().v4();
    final url = '$baseUrl/v2/voice?uid=$uid&session_id=$sessionId';

    _channel = WebSocketChannel.connect(Uri.parse(url));

    // Listen for audio responses from backend
    _channel!.stream.listen(
      (data) => _handleServerMessage(data),
      onError: (error) => _handleError(error),
      onDone: () => _handleSessionEnd(),
    );

    // Start capturing microphone audio
    await _startAudioCapture();
  }

  /// Stop voice session
  Future<void> stopSession() async {
    await _stopAudioCapture();
    await _channel?.sink.close();
    _channel = null;
  }

  /// Handle incoming audio from backend (TTS)
  void _handleServerMessage(dynamic data) {
    if (data is Uint8List) {
      // Audio chunk from TTS - play it
      _playAudioChunk(data);
    } else if (data is String) {
      // JSON message (status updates, errors)
      final json = jsonDecode(data);
      _handleJsonMessage(json);
    }
  }

  /// Start microphone capture
  Future<void> _startAudioCapture() async {
    // Use flutter_sound or audio_streamer package
    // Send raw PCM16 audio to WebSocket

    _audioSubscription = AudioStreamer.stream.listen((buffer) {
      if (_channel != null) {
        // Send raw audio bytes to backend
        // Backend handles VAD, STT, LLM, TTS
        _channel!.sink.add(buffer);
      }
    });
  }

  /// Play TTS audio chunk
  void _playAudioChunk(Uint8List audioData) {
    // Use flutter_sound or just_audio for playback
    // Queue chunks for smooth playback
    AudioPlayer.instance.playBytes(audioData);
  }
}
```

### VoiceModeV2Provider

```dart
// lib/providers/voice_mode_v2_provider.dart

import 'package:flutter_riverpod/flutter_riverpod.dart';

enum VoiceModeV2State {
  idle,
  connecting,
  listening,      // User speaking, backend doing VAD
  processing,     // Backend processing (STT → LLM)
  responding,     // Playing TTS audio
  error,
}

class VoiceModeV2Notifier extends StateNotifier<VoiceModeV2State> {
  final VoiceModeV2Service _service;

  VoiceModeV2Notifier(this._service) : super(VoiceModeV2State.idle);

  Future<void> startVoiceMode(String uid) async {
    state = VoiceModeV2State.connecting;

    try {
      await _service.startSession(uid);
      state = VoiceModeV2State.listening;
    } catch (e) {
      state = VoiceModeV2State.error;
    }
  }

  Future<void> stopVoiceMode() async {
    await _service.stopSession();
    state = VoiceModeV2State.idle;
  }
}

final voiceModeV2Provider =
    StateNotifierProvider<VoiceModeV2Notifier, VoiceModeV2State>((ref) {
  return VoiceModeV2Notifier(VoiceModeV2Service());
});
```

### Voice Button Integration

```dart
// lib/pages/capture/widgets/voice_mode_button.dart

class VoiceModeButton extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (FeatureFlags.usePipecatVoiceV2) {
      return _buildV2Button(context, ref);
    } else {
      return _buildV1Button(context, ref);  // Existing implementation
    }
  }

  Widget _buildV2Button(BuildContext context, WidgetRef ref) {
    final state = ref.watch(voiceModeV2Provider);
    final notifier = ref.read(voiceModeV2Provider.notifier);
    final uid = ref.watch(authProvider).uid;

    return GestureDetector(
      onTapDown: (_) => notifier.startVoiceMode(uid),
      onTapUp: (_) => notifier.stopVoiceMode(),
      onTapCancel: () => notifier.stopVoiceMode(),
      child: _buildButtonUI(state),
    );
  }
}
```

---

## Audio Configuration

### Capture Settings

```dart
// PCM16, 16kHz mono - matches Pipecat expectations
const audioConfig = AudioConfig(
  sampleRate: 16000,
  channels: 1,
  bitsPerSample: 16,
  encoding: AudioEncoding.pcm16,
);
```

### Playback Settings

```dart
// Match TTS output format (OpenAI returns PCM16 24kHz)
const playbackConfig = AudioConfig(
  sampleRate: 24000,
  channels: 1,
  bitsPerSample: 16,
);
```

---

## Code to Remove (After V2 Stable)

Once v2 is stable, remove these v1 components:

```
lib/services/voice_mode_service.dart       # ~200 lines
lib/providers/voice_mode_provider.dart     # ~150 lines
lib/utils/voice_turn_detector.dart         # ~100 lines
lib/utils/voice_silence_detector.dart      # ~80 lines
```

**Total**: ~530 lines of turn-detection code can be deleted.

---

## Testing

### Manual Testing

1. **Basic Flow**:
   - Tap and hold voice button
   - Speak "Hello Ella"
   - Release button
   - Verify Ella responds with audio

2. **Turn Detection**:
   - Speak, pause naturally (1.5s)
   - Verify Ella responds without manual trigger
   - No more 3s timeout feeling

3. **Interruption** (Phase 2):
   - While Ella is speaking, start speaking
   - Verify Ella stops and listens

4. **Error Handling**:
   - Disconnect network mid-session
   - Verify graceful fallback/error UI

### Automated Testing

```dart
// test/services/voice_mode_v2_test.dart

void main() {
  group('VoiceModeV2Service', () {
    test('connects to v2 endpoint', () async {
      final service = VoiceModeV2Service();
      await service.startSession('test-uid');
      expect(service.isConnected, true);
    });

    test('sends audio to backend', () async {
      // Mock WebSocket, verify audio bytes sent
    });

    test('plays received TTS audio', () async {
      // Mock audio player, verify playback
    });
  });
}
```

---

## Rollback Plan

If v2 has issues:

```dart
// Instant rollback via feature flag
class FeatureFlags {
  static const bool usePipecatVoiceV2 = false;  // Switch back to v1
}
```

No code changes needed for rollback - just flip the flag.

---

## Dependencies

### Backend (Must Be Ready First)
- [ ] `/v2/voice` endpoint deployed
- [ ] WebSocket accepts audio, returns TTS
- [ ] n8n config hook working

### iOS Packages

```yaml
# pubspec.yaml
dependencies:
  web_socket_channel: ^2.4.0
  flutter_sound: ^9.2.13      # Or audio_streamer
  just_audio: ^0.9.34         # For playback
  uuid: ^4.2.1
```

---

## Timeline

| Task | Effort | Status |
|------|--------|--------|
| Create VoiceModeV2Service | 4h | Pending |
| Create VoiceModeV2Provider | 2h | Pending |
| Integrate voice button | 2h | Pending |
| Audio capture setup | 3h | Pending |
| Audio playback setup | 2h | Pending |
| Testing & debugging | 4h | Pending |
| **Total** | **~17h** | |

---

## Success Metrics

| Metric | v1 Current | v2 Target |
|--------|------------|-----------|
| Turn detection accuracy | ~70% (timeout-based) | ~95% (VAD-based) |
| User-reported latency feel | "Slow" (3s timeout) | "Natural" (<500ms VAD) |
| Code complexity | 530+ lines | ~200 lines |
| Interruption support | None | Native |

---

## References

- [GitHub Discussion #4](https://github.com/ellaaicare/omi/discussions/4)
- [Backend PRD](../backend/docs/PRD_PIPECAT_BACKEND.md)
- [flutter_sound package](https://pub.dev/packages/flutter_sound)
- [web_socket_channel](https://pub.dev/packages/web_socket_channel)
