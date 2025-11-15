# Edge ASR Integration Guide for iOS Team

**Date**: November 8, 2025
**Purpose**: Enable iOS team to send pre-transcribed text from on-device ASR (Automatic Speech Recognition)
**Target**: Bypass Deepgram to improve latency and reduce costs

---

## Current Architecture

### Flow with Cloud STT (Deepgram)
```
iOS Device → WebSocket /v4/listen (audio bytes)
          ↓
Backend receives opus/pcm audio
          ↓
Backend sends to Deepgram API
          ↓
Deepgram returns transcript segments
          ↓
Backend processes segments → Ella scanner → Memory/Summary agents
          ↓
iOS polls GET endpoints for results
```

**Latency**: ~600ms chunks + network round-trip + Deepgram processing (~200-500ms)
**Cost**: $0.0043 per minute of audio (Deepgram)

---

## Proposed Architecture with Edge ASR

### Flow with On-Device STT (iOS Speech / Parakeet)
```
iOS Device performs ASR locally (Speech framework / Parakeet)
          ↓
iOS sends transcript segments via WebSocket /v4/listen (text JSON)
          ↓
Backend bypasses Deepgram entirely
          ↓
Backend processes segments → Ella scanner → Memory/Summary agents
          ↓
iOS polls GET endpoints for results
```

**Latency**: Faster (no Deepgram round-trip, ~200-500ms saved)
**Cost**: Free (on-device processing)
**Bandwidth**: 99.6% reduction (text vs audio)

---

## Implementation Options

### Option 1: Use Existing WebSocket with New Message Type ✅ **RECOMMENDED**

The backend already supports JSON text messages via the WebSocket! Currently used for:
- Image chunks (`type: "image_chunk"`)
- Speaker assignments (`type: "speaker_assigned"`)

**Add a new message type**: `type: "transcript_segment"`

#### Backend Changes Required

**File**: `routers/transcribe.py` (around line 1093-1127)

**Current Code**:
```python
elif message.get("text") is not None:
    try:
        json_data = json.loads(message.get("text"))
        if json_data.get('type') == 'image_chunk':
            # Handle image
        elif json_data.get('type') == 'speaker_assigned':
            # Handle speaker assignment
```

**Add This**:
```python
        elif json_data.get('type') == 'transcript_segment':
            # Handle edge ASR transcript
            await handle_edge_transcript(json_data)
```

**New Handler Function**:
```python
async def handle_edge_transcript(json_data):
    \"\"\"Process transcript segment from edge ASR (iOS device)\"\"\"
    try:
        # Extract segment data
        text = json_data.get('text', '').strip()
        speaker = json_data.get('speaker', 'SPEAKER_00')
        start_time = json_data.get('start', 0)
        end_time = json_data.get('end', 0)
        is_final = json_data.get('is_final', True)

        if not text:
            return

        # Create TranscriptSegment object (same as Deepgram would create)
        segment = TranscriptSegment(
            text=text,
            speaker=speaker,
            speaker_id=0,
            is_user=False,
            start=start_time,
            end=end_time,
            person_id=None
        )

        # Call existing stream_transcript handler (same as STT services use)
        await stream_transcript([segment])

        print(f"📱 Edge ASR segment processed: {text[:50]}...", uid, session_id)

    except Exception as e:
        print(f"Error processing edge transcript: {e}", uid, session_id)
```

#### iOS Message Format

**Send via WebSocket as JSON text message**:

```json
{
  "type": "transcript_segment",
  "text": "I went for a walk in the park this morning",
  "speaker": "SPEAKER_00",
  "start": 0.0,
  "end": 3.5,
  "is_final": true,
  "confidence": 0.95
}
```

**Field Specifications**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | ✅ Yes | Must be "transcript_segment" |
| `text` | string | ✅ Yes | Transcribed text from ASR |
| `speaker` | string | ❌ No | Default: "SPEAKER_00" |
| `start` | float | ❌ No | Start time in seconds (default: 0) |
| `end` | float | ❌ No | End time in seconds (default: 0) |
| `is_final` | boolean | ❌ No | Is this final transcript? (default: true) |
| `confidence` | float | ❌ No | ASR confidence 0-1 (optional) |

**iOS Swift Example**:

```swift
// When iOS Speech / Parakeet produces transcript
func sendTranscriptSegment(text: String, start: Double, end: Double) {
    let message: [String: Any] = [
        "type": "transcript_segment",
        "text": text,
        "speaker": "SPEAKER_00",
        "start": start,
        "end": end,
        "is_final": true,
        "confidence": 0.95
    ]

    if let jsonData = try? JSONSerialization.data(withJSONObject: message),
       let jsonString = String(data: jsonData, encoding: .utf8) {
        webSocket.send(.string(jsonString))
    }
}
```

**Chunking Strategy**:

iOS should send segments every ~600ms to match current Deepgram chunking:

```swift
// Buffer approach (similar to current audio chunking)
var textBuffer = ""
var bufferTimer: Timer?

func onASRResult(text: String, isFinal: Bool) {
    textBuffer += text + " "

    // Send every 600ms or when final
    if isFinal || bufferTimer == nil {
        bufferTimer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: false) { _ in
            if !self.textBuffer.isEmpty {
                self.sendTranscriptSegment(
                    text: self.textBuffer.trimmingCharacters(in: .whitespaces),
                    start: self.currentStartTime,
                    end: self.currentEndTime
                )
                self.textBuffer = ""
            }
        }
    }
}
```

---

### Option 2: Create Separate Text-Only Endpoint ❌ **NOT RECOMMENDED**

**Endpoint**: `POST /v1/transcribe/text`

**Why Not Recommended**:
- Requires new endpoint + auth logic
- Loses WebSocket real-time benefits (heartbeat, connection state)
- More complex for iOS (need to manage HTTP + WebSocket separately)
- Harder to correlate text segments with conversation session

---

## Hybrid Mode: Best of Both Worlds

**Strategy**: iOS can switch between modes dynamically

### Mode 1: Edge ASR (Default)
- Use on-device ASR (iOS Speech / Parakeet)
- Send text segments via WebSocket
- Fast, free, works offline

### Mode 2: Cloud STT (Fallback)
- Use when edge ASR quality is poor
- Send audio bytes via WebSocket (current flow)
- Better accuracy for noisy environments

**iOS Detection Logic**:

```swift
enum ASRMode {
    case edge   // On-device (iOS Speech / Parakeet)
    case cloud  // Deepgram
}

var currentMode: ASRMode = .edge

func shouldFallbackToCloud() -> Bool {
    // Fallback conditions:
    // 1. User preference (settings toggle)
    // 2. Low edge ASR confidence
    // 3. Noisy environment
    // 4. WiFi available (cloud costs less concern)

    if userPreference == .cloud { return true }
    if averageConfidence < 0.6 { return true }
    if backgroundNoiseLevel > threshold { return true }

    return false
}

func sendTranscriptOrAudio(text: String?, audio: Data?) {
    if currentMode == .edge && text != nil {
        // Send transcript text
        sendTranscriptSegment(text: text!)
    } else if audio != nil {
        // Send audio bytes (current flow)
        webSocket.send(.data(audio!))
    }
}
```

**Backend Handling**:

The backend already handles both! No changes needed:
- If message contains `"bytes"` → Process as audio
- If message contains `"text"` with `type: "transcript_segment"` → Process as text

---

## Backend Changes Summary

### Minimal Changes Required

**File**: `routers/transcribe.py`

**Changes**:
1. Add `handle_edge_transcript()` function (~25 lines)
2. Add `elif json_data.get('type') == 'transcript_segment'` case (~3 lines)
3. Call handler when edge transcript received (~1 line)

**Total**: ~30 lines of code

**No changes needed**:
- All downstream processing works the same (Ella scanner, memory, summary)
- Authentication, session management, conversation tracking all work
- iOS polling endpoints unchanged

---

## Testing Plan

### Phase 1: Basic Integration
1. iOS sends single test segment
2. Backend logs receipt
3. Verify segment processed correctly

**Test Message**:
```json
{
  "type": "transcript_segment",
  "text": "This is a test from edge ASR",
  "speaker": "SPEAKER_00"
}
```

### Phase 2: Chunked Streaming
1. iOS sends segments every ~600ms
2. Verify backend receives all chunks
3. Check conversation assembly works

### Phase 3: End-to-End
1. iOS device does full conversation with edge ASR
2. Verify memories extracted
3. Verify summary generated
4. Verify Ella scanner detects urgency
5. Compare quality vs Deepgram baseline

---

## Performance Comparison

| Metric | Cloud STT (Deepgram) | Edge ASR (iOS/Parakeet) |
|--------|---------------------|------------------------|
| **Latency** | ~600-800ms | ~200-400ms |
| **Cost** | $0.0043/min | Free |
| **Bandwidth** | ~256 KB/s | ~1 KB/s (99.6% reduction) |
| **Accuracy** | ~95% WER | 90-95% WER (device dependent) |
| **Offline** | ❌ No | ✅ Yes |
| **Privacy** | Cloud processing | On-device (HIPAA friendly) |

---

## Migration Strategy

### Week 1: Backend Implementation
- [ ] Add `handle_edge_transcript()` function
- [ ] Add message type handler
- [ ] Deploy to staging
- [ ] Test with curl/Postman

### Week 2: iOS Integration
- [ ] Implement iOS Speech / Parakeet ASR
- [ ] Add WebSocket text message sending
- [ ] Test basic segment transmission
- [ ] Add chunking logic (600ms intervals)

### Week 3: Hybrid Mode
- [ ] Add fallback detection logic
- [ ] Implement mode switching
- [ ] A/B test quality comparison
- [ ] Monitor accuracy vs Deepgram

### Week 4: Production Rollout
- [ ] Deploy backend changes
- [ ] Release iOS app with feature flag
- [ ] Monitor metrics (latency, accuracy, cost)
- [ ] Gradual rollout to 100% users

---

## Monitoring & Metrics

### Backend Logs

Add logging for edge ASR:

```python
# In handle_edge_transcript()
print(f"📱 Edge ASR: uid={uid}, text_len={len(text)}, confidence={confidence}")

# In stream_transcript()
if source == "edge_asr":
    print(f"📊 Edge ASR segment processed: {segment.text[:50]}")
```

### Metrics to Track

1. **Adoption Rate**: % of users using edge ASR vs cloud
2. **Latency Improvement**: Average time saved per segment
3. **Cost Savings**: Deepgram API costs reduced
4. **Bandwidth Savings**: MB/s reduction
5. **Quality Comparison**: WER (Word Error Rate) edge vs cloud
6. **Fallback Rate**: How often iOS falls back to cloud

---

## Edge Cases & Handling

### 1. Empty Segments
**Issue**: Edge ASR might send empty text
**Solution**: Backend filters out empty segments

```python
if not text or not text.strip():
    return  # Ignore empty segments
```

### 2. Duplicate Segments
**Issue**: iOS might send interim + final for same text
**Solution**: Use `is_final` flag, only process final segments

```python
if not is_final:
    return  # Wait for final segment
```

### 3. Connection Loss Mid-Conversation
**Issue**: WebSocket drops, some segments lost
**Solution**: iOS buffers unsent segments, resends on reconnect

```swift
var unsentSegments: [TranscriptSegment] = []

func onWebSocketConnected() {
    // Resend buffered segments
    for segment in unsentSegments {
        sendTranscriptSegment(segment)
    }
    unsentSegments.removeAll()
}
```

### 4. Speaker Diarization
**Issue**: Edge ASR doesn't do speaker separation
**Solution**: Use existing backend speaker detection logic

Backend already has speaker detection that runs after transcription:
- `utils/speaker_identification.py::detect_speaker_from_text()`
- This works the same for edge ASR segments

---

## Security Considerations

### Authentication
- Edge ASR uses same WebSocket authentication (Firebase JWT or ADMIN_KEY)
- No new auth flow needed

### Privacy
- ✅ **Better privacy** - Text stays on-device until sent
- ✅ Audio never leaves device (HIPAA compliant)
- ✅ Same encryption (WSS/TLS) for text as audio

### Rate Limiting
- Same WebSocket rate limits apply
- No new attack vectors introduced

---

## FAQ

### Q: Can iOS send both audio and text in same session?
**A**: Yes! Backend handles both simultaneously. iOS can:
- Send audio bytes for background noise (VAD purposes)
- Send text segments for transcription
- Switch modes mid-conversation

### Q: What about speaker diarization?
**A**: Backend's existing speaker detection still works. It analyzes text patterns, not audio.

### Q: Will this break existing iOS app versions?
**A**: No! Old versions continue sending audio bytes as usual. New versions can optionally send text.

### Q: How does this affect conversation storage?
**A**: Zero impact. Backend stores TranscriptSegment objects the same way regardless of source (audio → Deepgram → text vs edge ASR → text).

### Q: Can we A/B test quality?
**A**: Yes! iOS can randomly send to edge vs cloud, backend logs the source, compare memory/summary quality.

---

## Code Template for iOS Team

### Complete Swift Implementation

```swift
class EdgeASRTranscriber {
    private let webSocket: WebSocket
    private var textBuffer: String = ""
    private var bufferTimer: Timer?
    private let chunkInterval: TimeInterval = 0.6  // 600ms

    // MARK: - Public API

    func sendTranscriptChunk(text: String, isFinal: Bool = true, confidence: Double = 0.95) {
        let message: [String: Any] = [
            "type": "transcript_segment",
            "text": text,
            "speaker": "SPEAKER_00",
            "start": getCurrentTimestamp(),
            "end": getCurrentTimestamp() + text.count / 3.0,  // Rough estimate
            "is_final": isFinal,
            "confidence": confidence
        ]

        sendJSON(message)
    }

    func sendTranscriptBuffered(text: String, isFinal: Bool) {
        textBuffer += text + " "

        bufferTimer?.invalidate()

        if isFinal {
            flushBuffer()
        } else {
            bufferTimer = Timer.scheduledTimer(withTimeInterval: chunkInterval, repeats: false) { [weak self] _ in
                self?.flushBuffer()
            }
        }
    }

    // MARK: - Private

    private func flushBuffer() {
        guard !textBuffer.isEmpty else { return }

        let text = textBuffer.trimmingCharacters(in: .whitespaces)
        sendTranscriptChunk(text: text, isFinal: true)
        textBuffer = ""
    }

    private func sendJSON(_ data: [String: Any]) {
        guard let jsonData = try? JSONSerialization.data(withJSONObject: data),
              let jsonString = String(data: jsonData, encoding: .utf8) else {
            return
        }

        webSocket.send(.string(jsonString))
    }

    private func getCurrentTimestamp() -> Double {
        return Date().timeIntervalSince1970
    }
}

// MARK: - Usage Example

// Initialize
let transcriber = EdgeASRTranscriber(webSocket: myWebSocket)

// When iOS Speech produces result
func speechRecognitionResult(_ text: String, isFinal: Bool, confidence: Double) {
    transcriber.sendTranscriptBuffered(text: text, isFinal: isFinal)
}
```

---

## Conclusion

✅ **Recommended Approach**: Option 1 (Use existing WebSocket with new message type)

**Benefits**:
- Minimal backend changes (~30 lines)
- Leverages existing infrastructure
- Supports hybrid mode (edge + cloud fallback)
- Better latency, lower cost, improved privacy

**Implementation Time**:
- Backend: 2-4 hours
- iOS: 1-2 weeks (includes ASR integration + testing)
- Testing: 1 week
- Total: ~3 weeks to production

**Ready to Start**: iOS team can begin implementing once backend changes are deployed.

---

**Questions?** Contact Backend Dev team on Discord.

**Status**: Specification complete, ready for implementation.
**Date**: November 8, 2025
