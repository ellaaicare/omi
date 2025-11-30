# Voice Mode ASR vs Audio Analysis

**Date**: November 30, 2025
**Branch**: `feature/voice-mode`
**Purpose**: Document ASR behavior and challenges for backend team review

---

## Overview

Voice mode uses on-device ASR (Apple's SFSpeechRecognizer) to transcribe user speech, then sends transcripts to the backend for Ella AI to respond. We're encountering challenges with detecting when the user has finished speaking.

---

## Current Architecture

```
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐    ┌─────────┐
│ User Speech │───▶│ On-Device ASR│───▶│ VoiceModeManager│───▶│ Backend │
│   (audio)   │    │ (iOS native) │    │   (Flutter)     │    │   WS    │
└─────────────┘    └──────────────┘    └─────────────────┘    └─────────┘
                          │                     │
                          ▼                     ▼
                   Partial transcripts    voice_utterance event
                   (final: false)         (when silence detected)
```

---

## Audio Stream (Consistent, Working Well)

The raw audio stream is sent to the backend reliably:

| Property | Value | Notes |
|----------|-------|-------|
| Format | PCM16 (raw) | 16-bit signed integers |
| Sample Rate | 16000 Hz | Standard for speech |
| Channels | Mono | Single channel |
| Chunk Size | Variable | ~4096 bytes typical |
| Delivery | WebSocket | Continuous stream |
| Latency | ~50-100ms | Real-time |

**Audio is consistently correct** - the backend receives the full audio stream without issues.

---

## On-Device ASR Behavior (Problematic)

### What the ASR Provides

| Property | Behavior | Issue |
|----------|----------|-------|
| `text` | Accumulated transcript | Works well |
| `isFinal` | **Never true** | ASR configured for continuous listening |
| Partials | Sent every ~100-300ms | Very frequent |
| Keep-alive | Sends same text repeatedly | Confuses silence detection |

### The Keep-Alive Problem

The on-device ASR is configured with `shouldReportPartialResults = true` and continuous recognition. This means:

1. **During speech**: Partials arrive with growing text
2. **During silence**: Same text keeps arriving (keep-alive packets)
3. **No finalization**: `isFinal` is never `true` because recognizer stays open

```
Timeline:
0.0s: "Hello"           (user speaking)
0.3s: "Hello testing"   (user speaking)
0.6s: "Hello testing the" (user speaking)
1.0s: "Hello testing the connection" (user stops)
1.3s: "Hello testing the connection" (silence - keep-alive)
1.6s: "Hello testing the connection" (silence - keep-alive)
1.9s: "Hello testing the connection" (silence - keep-alive)
... continues indefinitely
```

---

## Current iOS Silence Detection (Client-Side)

### Implementation

```dart
// voice_mode_manager.dart
static const Duration silenceTimeout = Duration(seconds: 3);

void onTranscriptUpdate(String transcript) {
  // Only reset timer if TEXT actually changed
  final normalizedNew = transcript.trim().toLowerCase();
  final normalizedOld = _lastTranscriptText.trim().toLowerCase();

  if (normalizedNew != normalizedOld && normalizedNew.isNotEmpty) {
    _lastTranscriptText = transcript;
    _resetSilenceTimer();  // Reset 3s timer
  }
}

void _resetSilenceTimer() {
  _silenceTimer?.cancel();
  _silenceTimer = Timer(silenceTimeout, () {
    // 3 seconds of unchanged text = user stopped speaking
    onUserSpeechFinal(_currentTranscript);  // Send to backend
  });
}
```

### Limitations

| Limitation | Impact |
|------------|--------|
| Fixed 3s timeout | May be too long or too short depending on user |
| No audio-level awareness | Can't detect actual silence in audio |
| Text comparison only | Doesn't account for ASR processing delays |
| No confidence scores | Can't distinguish confident vs uncertain transcripts |

---

## Variables Available from iOS ASR

### SFSpeechRecognitionResult Properties

| Property | Type | Available | Notes |
|----------|------|-----------|-------|
| `bestTranscription.formattedString` | String | ✅ Yes | The transcript text |
| `isFinal` | Bool | ✅ Yes | Always `false` in continuous mode |
| `transcriptions` | [SFTranscription] | ✅ Yes | Alternative interpretations |
| `speechRecognitionMetadata` | Metadata? | ⚠️ iOS 14.5+ | Contains speech/silence timing |

### SFTranscription Properties

| Property | Type | Notes |
|----------|------|-------|
| `segments` | [SFTranscriptionSegment] | Word-level data |
| `formattedString` | String | Full text |

### SFTranscriptionSegment Properties (Per Word)

| Property | Type | Potential Use |
|----------|------|---------------|
| `substring` | String | The word |
| `timestamp` | TimeInterval | When word was spoken |
| `duration` | TimeInterval | How long word took |
| `confidence` | Float | 0.0-1.0 confidence score |
| `alternativeSubstrings` | [String] | Other possibilities |

### SFSpeechRecognitionMetadata (iOS 14.5+)

| Property | Type | Potential Use |
|----------|------|---------------|
| `speechStartTimestamp` | TimeInterval | When speech started |
| `speechDuration` | TimeInterval | How long speech lasted |
| `voiceAnalytics` | VoiceAnalytics? | Pitch, jitter, shimmer |

---

## Potential Backend-Side Solutions

### Option 1: Backend VAD on Audio Stream

Since the backend already receives the raw audio stream, it could:

1. Run Voice Activity Detection (VAD) on the audio
2. Detect actual silence in the audio waveform
3. Send `voice_end_detected` event to iOS when silence threshold met
4. iOS triggers transcript send on receiving this event

**Pros**: More accurate than text comparison, uses actual audio
**Cons**: Additional backend processing, latency

### Option 2: Backend Transcript Timing

Backend could analyze the transcript segments:

1. iOS sends word-level timing data (timestamp, duration per word)
2. Backend calculates gap between last word and current time
3. When gap exceeds threshold, backend requests final transcript

### Option 3: Hybrid Approach

Combine client-side and backend-side detection:

1. iOS sends preliminary "user_may_be_done" signal after 1.5s of unchanged text
2. Backend confirms with VAD analysis of recent audio
3. Backend sends "confirmed_end_of_turn"
4. iOS sends final transcript

---

## Questions for Backend Team

### 1. Audio Stream Analysis
- Can the backend run VAD on the incoming audio stream?
- What's the latency impact of server-side VAD?
- Is there existing VAD infrastructure we can use?

### 2. Turn-Taking Protocol
- Should the backend control turn-taking timing?
- What's the ideal silence duration before triggering a response?
- Should silence threshold be user-configurable?

### 3. Transcript Enrichment
- Would word-level timestamps be useful?
  ```json
  {
    "event": "voice_utterance",
    "text": "Hello testing",
    "segments": [
      {"word": "Hello", "start": 0.0, "end": 0.4, "confidence": 0.95},
      {"word": "testing", "start": 0.5, "end": 0.9, "confidence": 0.88}
    ],
    "last_word_end": 0.9,
    "silence_duration": 2.1
  }
  ```

### 4. Interruption Handling
- How should we handle user interrupting Ella's response?
- Should iOS detect speech during playback and signal "barge-in"?

### 5. Backend Events
Would any of these events be helpful?

| Event | Direction | Purpose |
|-------|-----------|---------|
| `voice_silence_detected` | Backend → iOS | Backend VAD detected silence |
| `voice_request_transcript` | Backend → iOS | Backend wants current transcript now |
| `voice_turn_complete` | Backend → iOS | Backend confirms turn is over |
| `voice_barge_in` | iOS → Backend | User interrupted during playback |

---

## Current Event Flow (Reference)

### iOS → Backend
```json
{"event": "voice_mode_start"}
{"event": "voice_utterance", "text": "Hello, how are you?"}
{"event": "voice_mode_stop", "reason": "user_request"}
```

### Backend → iOS
```json
{"event": "voice_mode_active", "session_id": "...", "timeout_seconds": 120}
{"event": "voice_status", "status": "thinking"}
{"event": "voice_response_audio", "data": "<base64>", "sequence": 1, ...}
{"event": "voice_response_complete", "text": "I'm doing well!", "duration_ms": 2500}
{"event": "voice_mode_ended", "reason": "session_timeout"}
```

---

## Recommendations

### Short-term (iOS-only)
1. ✅ Implemented: Text-change-based silence detection (3s)
2. Consider: Adjustable silence timeout based on user preference
3. Consider: Send word-level confidence scores with utterances

### Medium-term (Requires Backend)
1. Backend VAD as authoritative silence detector
2. Backend-controlled turn-taking with `voice_request_transcript` event
3. Interruption/barge-in support

### Long-term
1. Adaptive silence thresholds based on conversation context
2. Prosodic analysis (intonation suggests question vs statement)
3. Multi-modal cues (if camera available)

---

## Test Scenarios

| Scenario | Expected Behavior | Current Status |
|----------|-------------------|----------------|
| User speaks, pauses 3s | Transcript sent after 3s | ✅ Fixed |
| User speaks continuously | Timer keeps resetting | ✅ Working |
| ASR sends keep-alive | Timer NOT reset | ✅ Fixed |
| User speaks very slowly | May timeout mid-sentence | ⚠️ Possible issue |
| Background noise | May prevent silence detection | ⚠️ Unknown |

---

## Files Reference

| File | Purpose |
|------|---------|
| `ios/Runner/OnDeviceASRService.swift` | iOS native ASR wrapper |
| `lib/services/asr/on_device_asr_service.dart` | Flutter ASR service |
| `lib/services/voice_mode/voice_mode_manager.dart` | Voice mode state machine |
| `lib/providers/capture_provider.dart` | Routes transcripts to voice mode |

---

**Feedback welcome from backend team on optimal approach for silence/turn detection!**
