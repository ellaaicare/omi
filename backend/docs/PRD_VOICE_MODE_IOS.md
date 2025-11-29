# PRD: Voice Mode - iOS Implementation

**Version**: 1.0
**Date**: November 29, 2025
**Author**: OMI Backend Team
**For**: iOS Development Team

---

## Executive Summary

Enable real-time voice conversations with Ella AI. iOS handles voice mode initiation (button or wake word), audio capture, and audio playback. Backend handles all AI processing and TTS generation.

**iOS Responsibility**: UI, audio I/O, voice mode triggers
**Backend Responsibility**: STT, agent routing, TTS, streaming

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  iOS App                                                         │
│                                                                  │
│  ┌─────────────────┐    ┌─────────────────┐                     │
│  │  Wake Word      │    │  "Talk to Ella" │                     │
│  │  Detection      │    │  Button         │                     │
│  │  (On-Device)    │    │                 │                     │
│  └────────┬────────┘    └────────┬────────┘                     │
│           │                      │                               │
│           └──────────┬───────────┘                               │
│                      ▼                                           │
│           ┌─────────────────────┐                               │
│           │  Voice Mode         │                               │
│           │  State Machine      │                               │
│           └─────────┬───────────┘                               │
│                     │                                            │
│    ┌────────────────┼────────────────┐                          │
│    ▼                ▼                ▼                          │
│  ┌─────┐      ┌──────────┐    ┌───────────┐                    │
│  │Audio│      │WebSocket │    │  Audio    │                    │
│  │Input│ ───▶ │  Events  │ ◀──│  Playback │                    │
│  └─────┘      └────┬─────┘    └───────────┘                    │
│                    │                                            │
└────────────────────┼────────────────────────────────────────────┘
                     │ WebSocket (bidirectional)
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  OMI Backend                                                     │
│  - Receives audio, transcribes                                  │
│  - Routes to n8n agents                                         │
│  - Generates TTS, streams back                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Phases

### Phase 1: Button-Triggered Voice Mode (MVP)

**Scope**: Manual activation via UI button

**Effort**: ~2-3 days

| Task | Description | Effort |
|------|-------------|--------|
| Voice mode button | "Talk to Ella" button in UI | 2h |
| WebSocket events | Send/receive voice mode events | 4h |
| Audio capture | PCM16 audio during voice mode | 4h |
| Streaming playback | Play audio chunks as received | 1d |
| Voice mode UI | Listening/thinking/speaking states | 4h |

### Phase 2: Wake Word Detection

**Scope**: Hands-free "Hey Ella" activation

**Effort**: ~1 week

| Task | Description | Effort |
|------|-------------|--------|
| Wake word model | Apple Speech or custom model | 2d |
| Background listening | Low-power audio monitoring | 2d |
| Confidence threshold | Tune false positive rate | 1d |
| Wake word config refresh | Pull updates from backend | 4h |

### Phase 3: Ella-Initiated Calls

**Scope**: Ella can call the user

**Effort**: ~2 days

| Task | Description | Effort |
|------|-------------|--------|
| Incoming call UI | "Ella wants to talk" notification | 4h |
| Push notification handling | Handle `incoming_voice_call` action | 4h |
| Auto-accept (critical) | Auto-answer for urgent calls | 2h |
| Decline handling | User declines, notify backend | 2h |

---

## WebSocket Protocol

### Voice Mode Events

Use the **existing WebSocket connection** (`/v4/listen`). Add these new event types:

#### iOS → Backend

```swift
// Start voice mode
struct VoiceModeStartEvent: Codable {
    let event = "voice_mode_start"
    let trigger: String  // "button" or "wake_word"
    let wakeWordConfidence: Double?  // Optional, for wake word
}

// Send audio chunk
struct VoiceAudioEvent: Codable {
    let event = "voice_audio"
    let data: String  // Base64-encoded PCM16 audio
    let sequence: Int
    let isFinal: Bool  // true = end of user utterance
}

// Stop voice mode
struct VoiceModeStopEvent: Codable {
    let event = "voice_mode_stop"
    let reason: String  // "user_request", "silence_timeout", "error"
}
```

#### Backend → iOS

```swift
// Voice mode activated
struct VoiceModeActiveEvent: Codable {
    let event = "voice_mode_active"
    let sessionId: String
    let timeoutSeconds: Int
}

// Transcription update (for UI)
struct VoiceTranscriptionEvent: Codable {
    let event = "voice_transcription"
    let text: String
    let isFinal: Bool
}

// Status update
struct VoiceStatusEvent: Codable {
    let event = "voice_status"
    let status: String  // "listening", "transcribing", "thinking", "speaking"
}

// Audio response chunk
struct VoiceResponseAudioEvent: Codable {
    let event = "voice_response_audio"
    let data: String  // Base64-encoded audio
    let sequence: Int
    let format: String  // "pcm16"
    let sampleRate: Int  // 24000
}

// Response complete
struct VoiceResponseCompleteEvent: Codable {
    let event = "voice_response_complete"
    let text: String  // Full response transcript
    let durationMs: Int
}

// Voice mode ended
struct VoiceModeEndedEvent: Codable {
    let event = "voice_mode_ended"
    let reason: String
    let sessionDurationSeconds: Int
}

// Error
struct VoiceErrorEvent: Codable {
    let event = "voice_error"
    let code: String
    let message: String
}
```

---

## Voice Mode UI States

```
┌─────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌──────────┐                                               │
│  │ INACTIVE │◀──────────────────────────────────────────┐   │
│  └────┬─────┘                                           │   │
│       │ Button press / Wake word                        │   │
│       ▼                                                 │   │
│  ┌──────────┐                                           │   │
│  │LISTENING │ ← "Listening..."                          │   │
│  │    🎤    │   (pulsing mic icon)                      │   │
│  └────┬─────┘                                           │   │
│       │ User stops speaking                             │   │
│       ▼                                                 │   │
│  ┌────────────┐                                         │   │
│  │TRANSCRIBING│ ← "Processing..."                       │   │
│  │     ⏳     │                                         │   │
│  └────┬───────┘                                         │   │
│       │ Transcript ready                                │   │
│       ▼                                                 │   │
│  ┌──────────┐                                           │   │
│  │ THINKING │ ← "Ella is thinking..."                   │   │
│  │    💭    │   (animated dots)                         │   │
│  └────┬─────┘                                           │   │
│       │ Response starts                                 │   │
│       ▼                                                 │   │
│  ┌──────────┐                                           │   │
│  │ SPEAKING │ ← "Ella is speaking..."                   │   │
│  │    🔊    │   (audio waveform)                        │   │
│  └────┬─────┘                                           │   │
│       │ Response complete                               │   │
│       ▼                                                 │   │
│  Back to LISTENING (multi-turn)                         │   │
│       │                                                 │   │
│       │ Silence timeout / User says stop / Error        │   │
│       └─────────────────────────────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Audio Implementation

### Audio Capture (iOS → Backend)

```swift
// Audio format for voice mode
let audioFormat = AVAudioFormat(
    commonFormat: .pcmFormatInt16,
    sampleRate: 16000,
    channels: 1,
    interleaved: true
)!

// Capture and send
func startVoiceCapture() {
    let inputNode = audioEngine.inputNode
    let recordingFormat = inputNode.outputFormat(forBus: 0)

    inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { buffer, time in
        // Convert to PCM16 if needed
        let pcmData = self.convertToPCM16(buffer)

        // Send via WebSocket
        let event = VoiceAudioEvent(
            data: pcmData.base64EncodedString(),
            sequence: self.audioSequence,
            isFinal: false
        )
        self.sendWebSocketEvent(event)
        self.audioSequence += 1
    }
}
```

### End of Utterance Detection

```swift
// Option A: Use Apple Speech for VAD
// When speech recognition detects end of utterance, send isFinal: true

// Option B: Silence detection
// If silence > 1.5 seconds, send isFinal: true

func detectEndOfUtterance() {
    // After detecting end of speech:
    let finalEvent = VoiceAudioEvent(
        data: lastAudioChunk.base64EncodedString(),
        sequence: audioSequence,
        isFinal: true  // ← Signals user finished speaking
    )
    sendWebSocketEvent(finalEvent)
}
```

### Audio Playback (Backend → iOS)

```swift
// Streaming audio player using AVAudioEngine
class StreamingAudioPlayer {
    private let audioEngine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var audioBufferQueue: [AVAudioPCMBuffer] = []

    func setup() {
        audioEngine.attach(playerNode)

        let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 24000,  // Match backend TTS output
            channels: 1,
            interleaved: true
        )!

        audioEngine.connect(playerNode, to: audioEngine.mainMixerNode, format: format)
        try? audioEngine.start()
        playerNode.play()
    }

    func enqueueAudioChunk(base64Data: String) {
        guard let data = Data(base64Encoded: base64Data) else { return }

        // Convert to AVAudioPCMBuffer
        let buffer = createBuffer(from: data)

        // Schedule for playback
        playerNode.scheduleBuffer(buffer) {
            // Buffer finished playing
        }
    }

    func stop() {
        playerNode.stop()
        audioEngine.stop()
    }
}
```

---

## Wake Word Detection (Phase 2)

### Option A: Apple Speech Framework

```swift
// Use existing on-device ASR, look for "hey ella" in transcript
class WakeWordDetector {
    private let recognizer = SFSpeechRecognizer()
    private var recognitionTask: SFSpeechRecognitionTask?

    func startListening() {
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true

        recognitionTask = recognizer?.recognitionTask(with: request) { result, error in
            guard let result = result else { return }

            let transcript = result.bestTranscription.formattedString.lowercased()
            if transcript.contains("hey ella") || transcript.contains("hi ella") {
                self.onWakeWordDetected(confidence: result.confidence)
            }
        }
    }

    func onWakeWordDetected(confidence: Float) {
        // Stop wake word listening
        recognitionTask?.cancel()

        // Start voice mode
        let event = VoiceModeStartEvent(
            trigger: "wake_word",
            wakeWordConfidence: Double(confidence)
        )
        sendWebSocketEvent(event)
    }
}
```

### Option B: Dedicated Wake Word Model

```swift
// Use a lightweight wake word model (Porcupine, Snowboy, or custom)
// Lower power consumption, faster detection
// Implementation depends on chosen library
```

### Wake Word Configuration Refresh

```swift
// On app start, check for wake word config updates
func refreshWakeWordConfig() async {
    let config = try? await api.get("/v1/config/wake-words")
    // config: { "phrases": ["hey ella", "hi ella", "okay ella"], "sensitivity": 0.7 }

    WakeWordDetector.shared.updateConfig(config)
}
```

---

## Ella-Initiated Calls (Phase 3)

### Push Notification Handling

```swift
// Handle incoming voice call push notification
func application(_ application: UIApplication,
                 didReceiveRemoteNotification userInfo: [AnyHashable: Any]) async {

    guard let action = userInfo["action"] as? String,
          action == "incoming_voice_call" else { return }

    let reason = userInfo["reason"] as? String ?? "Ella wants to talk"
    let urgency = userInfo["urgency"] as? String ?? "normal"
    let message = userInfo["message"] as? String

    if urgency == "critical" {
        // Auto-accept critical calls (medication emergency, etc.)
        startVoiceMode(ellaInitiated: true, openingMessage: message)
    } else {
        // Show incoming call UI
        showIncomingCallUI(reason: reason, message: message)
    }
}
```

### Incoming Call UI

```swift
// Simple incoming call notification
struct IncomingCallView: View {
    let reason: String
    let message: String?
    let onAccept: () -> Void
    let onDecline: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "phone.fill")
                .font(.system(size: 60))
                .foregroundColor(.green)

            Text("Ella wants to talk")
                .font(.title)

            Text(reason)
                .font(.subheadline)
                .foregroundColor(.secondary)

            if let message = message {
                Text("\"\(message)\"")
                    .font(.body)
                    .italic()
            }

            HStack(spacing: 40) {
                Button(action: onDecline) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 50))
                        .foregroundColor(.red)
                }

                Button(action: onAccept) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 50))
                        .foregroundColor(.green)
                }
            }
        }
    }
}
```

---

## Voice Mode Button UI (Phase 1)

### Placement Options

1. **Chat screen**: Floating mic button (like voice assistants)
2. **Home screen**: Dedicated "Talk to Ella" card
3. **Quick action**: Long-press on app icon

### Example Implementation

```swift
struct VoiceModeButton: View {
    @ObservedObject var voiceMode: VoiceModeManager

    var body: some View {
        Button(action: { voiceMode.toggle() }) {
            ZStack {
                Circle()
                    .fill(voiceMode.isActive ? Color.red : Color.blue)
                    .frame(width: 64, height: 64)

                Image(systemName: voiceMode.isActive ? "stop.fill" : "mic.fill")
                    .font(.system(size: 24))
                    .foregroundColor(.white)
            }
        }
        .overlay(
            // Pulsing animation when listening
            Circle()
                .stroke(Color.blue, lineWidth: 2)
                .scaleEffect(voiceMode.state == .listening ? 1.3 : 1.0)
                .opacity(voiceMode.state == .listening ? 0 : 1)
                .animation(.easeInOut(duration: 1).repeatForever(), value: voiceMode.state)
        )
    }
}
```

---

## Error Handling

| Error | iOS Handling |
|-------|--------------|
| `voice_error: tts_failed` | Show "Ella couldn't respond. Try again?" |
| `voice_error: agent_timeout` | Show "Ella is taking longer than usual..." |
| `voice_error: transcription_failed` | Show "I didn't catch that. Could you repeat?" |
| WebSocket disconnect | Auto-reconnect, show "Reconnecting..." |
| Microphone permission denied | Show permission request UI |
| Audio session interrupted | Pause voice mode, resume when possible |

---

## Testing Checklist

### Phase 1 (Button)
- [ ] Button starts voice mode
- [ ] Audio captured and sent correctly
- [ ] Receive and play audio response
- [ ] Multi-turn conversation works
- [ ] Silence timeout ends voice mode
- [ ] Error states handled gracefully

### Phase 2 (Wake Word)
- [ ] "Hey Ella" detected on device
- [ ] Low false positive rate
- [ ] Works in background (if enabled)
- [ ] Battery impact acceptable

### Phase 3 (Ella-Initiated)
- [ ] Push notification received
- [ ] Incoming call UI displays
- [ ] Accept starts voice mode with Ella speaking first
- [ ] Decline notifies backend
- [ ] Critical calls auto-accept

---

## Dependencies

- **Backend**: Voice mode WebSocket events (this PRD defines protocol)
- **Backend**: `/v1/ella/voice-call` endpoint for Ella-initiated calls
- **n8n**: Agent responses for voice conversations

---

## Questions for iOS Team

1. **Existing ASR**: Can we reuse on-device ASR code for wake word detection?

2. **Audio session**: Any conflicts with existing audio handling?

3. **Background mode**: Do we need background audio for wake word? Battery implications?

4. **UI location**: Where should the voice mode button live?

---

## Timeline

| Week | Phase | Deliverable |
|------|-------|-------------|
| Week 1 | Phase 1 | Voice mode button + WebSocket events |
| Week 1 | Phase 1 | Audio capture + streaming playback |
| Week 2 | Phase 1 | Voice mode UI states + polish |
| Week 3 | Phase 2 | Wake word detection |
| Week 4 | Phase 3 | Ella-initiated calls |

---

**Ready for review. Please reach out with questions or to schedule a technical sync.**
