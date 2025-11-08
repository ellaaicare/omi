# Edge ASR End-to-End Testing Guide

**Date**: November 8, 2025
**Purpose**: Verify iOS on-device ASR integration works correctly and can be distinguished from Deepgram audio transcription

---

## Overview

This guide explains how to test edge ASR from iOS app and verify the complete flow:

```
iOS Device (on-device ASR) → WebSocket (text segments) → Backend → Firestore
                                                            ↓
                                                    Scanner/Memory/Summary
                                                            ↓
                                                    iOS polls for results
```

---

## Testing Workflow

### Step 1: iOS Sends Edge ASR Segment

**iOS Code** (send via WebSocket):
```swift
func sendEdgeASRSegment(text: String) {
    let message: [String: Any] = [
        "type": "transcript_segment",
        "text": text,
        "speaker": "SPEAKER_00",
        "start": getCurrentTimestamp(),
        "end": getCurrentTimestamp() + Double(text.count) / 3.0,
        "is_final": true,
        "confidence": 0.95
    ]

    if let jsonData = try? JSONSerialization.data(withJSONObject: message),
       let jsonString = String(data: jsonData, encoding: .utf8) {
        webSocket.send(.string(jsonString))
    }
}

// Test with a simple sentence
sendEdgeASRSegment(text: "This is a test from edge ASR")
```

### Step 2: Backend Logs What It Receives

**What to Check in Backend Logs**:

```bash
# On VPS (production)
ssh root@100.101.168.91
journalctl -u omi-backend -f | grep "Edge ASR"

# On local (development)
# Check terminal where start_server.py is running
```

**Expected Log Output**:
```
📱 Edge ASR segment: This is a test from edge ASR HbBdbnRkPJhpYFIIsd34krM8FKD3 <session-id>
```

**What This Tells You**:
- ✅ WebSocket message received
- ✅ JSON parsed successfully
- ✅ Text extracted and fed to pipeline
- ✅ UID and session ID logged

### Step 3: Real-Time WebSocket Events iOS Receives

**iOS receives these events back** (via WebSocket):

#### Event 1: Transcript Segment Echo
```json
{
  "type": "transcript",
  "segments": [
    {
      "text": "This is a test from edge ASR",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 3.0,
      "is_user": false,
      "person_id": null
    }
  ]
}
```

**iOS Should See**:
- Same text echoed back
- Speaker assigned
- Timestamps preserved

#### Event 2: Scanner Result (if urgent)
```json
{
  "type": "urgency_detected",
  "level": "critical",
  "reasoning": "Medical emergency detected",
  "action_needed": true
}
```

**iOS Should See**:
- Real-time urgency detection (within ~1-2 seconds)

### Step 4: Verify Firestore Conversation

**Check Firestore Console**:
1. Visit: https://console.firebase.google.com/project/omi-dev-ca005/firestore/data
2. Navigate to `conversations` collection
3. Filter by your UID
4. Look for most recent conversation

**Expected Firestore Document**:
```json
{
  "uid": "your-uid",
  "created_at": "2025-11-08T...",
  "status": "processing",
  "transcript": "This is a test from edge ASR",
  "transcript_segments": [
    {
      "text": "This is a test from edge ASR",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 3.0,
      "is_user": false
    }
  ],
  "language": "en",
  "source": "mobile_app"
}
```

**What to Verify**:
- ✅ Conversation created
- ✅ Transcript matches your text
- ✅ Segments saved correctly
- ✅ Timestamps preserved

### Step 5: Verify Memories Extracted

**iOS Polls** (or wait for processing):
```
GET /v1/conversations/{conversation_id}/memories
```

**Expected Response**:
```json
{
  "memories": [
    {
      "content": "User tested edge ASR functionality",
      "category": "system",
      "created_at": "2025-11-08T...",
      "conversation_id": "..."
    }
  ]
}
```

**What to Verify**:
- ✅ Memories extracted from edge ASR text
- ✅ Memory content makes sense

### Step 6: Verify Summary Generated

**iOS Polls**:
```
GET /v1/conversations/{conversation_id}
```

**Expected Response** (after processing completes):
```json
{
  "id": "conversation-id",
  "structured": {
    "title": "Edge ASR Test",
    "overview": "User tested on-device ASR integration",
    "emoji": "🧪",
    "category": "other"
  }
}
```

**What to Verify**:
- ✅ Summary generated from edge ASR text
- ✅ Title and overview relevant

---

## How to Distinguish Edge ASR vs Deepgram

### Problem
Currently, there's **no way to tell** in Firestore whether a conversation came from:
- Edge ASR (iOS on-device)
- Deepgram (cloud audio transcription)

Both create identical conversation documents.

### Solution: Add Source Tracking

**Proposed Enhancement** (add to `TranscriptSegment`):

```python
# In models/conversation.py
class TranscriptSegment(BaseModel):
    text: str
    speaker: str
    speaker_id: int
    is_user: bool
    start: float
    end: float
    person_id: Optional[str] = None
    source: Optional[str] = None  # 🆕 Add this field
    # Values: "deepgram", "edge_asr", "soniox", "speechmatics"
```

**Backend Changes**:
```python
# In routers/transcribe.py (edge ASR handler)
segment = TranscriptSegment(
    text=text,
    speaker=json_data.get('speaker', 'SPEAKER_00'),
    speaker_id=0,
    is_user=False,
    start=json_data.get('start', 0),
    end=json_data.get('end', 0),
    person_id=None,
    source="edge_asr"  # 🆕 Mark as edge ASR
)
```

**Firestore Document** (with source tracking):
```json
{
  "transcript_segments": [
    {
      "text": "This is a test from edge ASR",
      "speaker": "SPEAKER_00",
      "source": "edge_asr",  // 🆕 Can distinguish!
      "start": 0.0,
      "end": 3.0
    }
  ]
}
```

**Benefits**:
- ✅ Can filter conversations by source
- ✅ Can compare edge ASR vs Deepgram quality
- ✅ Can track adoption rate
- ✅ Can A/B test accuracy

---

## Complete Test Scenarios

### Scenario 1: Single Edge ASR Segment

**iOS Sends**:
```json
{
  "type": "transcript_segment",
  "text": "I went for a walk in the park this morning"
}
```

**Expected Results**:
1. Backend logs: `📱 Edge ASR segment: I went for a walk in the park this mor...`
2. iOS receives WebSocket event with same text
3. Firestore conversation created with text
4. Memories extracted (if relevant)
5. Summary generated (if conversation ends)

**Verification Checklist**:
- [ ] Backend logs show edge ASR segment
- [ ] iOS receives transcript event
- [ ] Firestore has conversation
- [ ] Text matches exactly

---

### Scenario 2: Multiple Edge ASR Segments (Conversation)

**iOS Sends** (3 segments, 600ms apart):
```json
{"type": "transcript_segment", "text": "Hey, how are you doing today?"}
{"type": "transcript_segment", "text": "I had a really interesting morning"}
{"type": "transcript_segment", "text": "I went to the doctor for a checkup"}
```

**Expected Results**:
1. Backend logs 3 separate segments
2. iOS receives 3 transcript events
3. Firestore conversation has all 3 segments concatenated
4. Full transcript: "Hey, how are you doing today? I had a really interesting morning. I went to the doctor for a checkup."

**Verification Checklist**:
- [ ] All 3 segments logged
- [ ] All 3 segments in Firestore
- [ ] Segments in correct order
- [ ] Timestamps incremental

---

### Scenario 3: Edge ASR with Urgent Content

**iOS Sends**:
```json
{
  "type": "transcript_segment",
  "text": "I am having severe chest pain and trouble breathing"
}
```

**Expected Results**:
1. Backend logs edge ASR segment
2. Scanner agent called (within 1-2s)
3. Scanner returns `urgency_level: "critical"`
4. Ella Main Agent may trigger notification callback
5. iOS receives push notification alert

**Verification Checklist**:
- [ ] Backend logs segment
- [ ] Scanner called (check backend logs)
- [ ] Urgency detected correctly
- [ ] iOS notification received (if implemented)

---

### Scenario 4: Compare Edge ASR vs Deepgram Audio

**Test Plan**:
1. **Record audio clip** (30 seconds, clear speech)
2. **Test A**: Send audio to Deepgram (current flow)
3. **Test B**: Transcribe locally with iOS Speech, send via edge ASR
4. **Compare**:
   - Latency (time to first segment)
   - Accuracy (word error rate)
   - Memory extraction quality
   - Summary quality

**Metrics to Track**:

| Metric | Deepgram (Audio) | Edge ASR (Text) | Difference |
|--------|-----------------|-----------------|------------|
| **Latency to first segment** | ? | ? | ? |
| **Total segments** | ? | ? | ? |
| **Transcript accuracy** | ? | ? | ? |
| **Memories extracted** | ? | ? | ? |
| **Summary quality** | ? | ? | ? |
| **Bandwidth used** | ? | ? | ? |

**Verification Checklist**:
- [ ] Same audio tested both ways
- [ ] Latency measured accurately
- [ ] Transcripts compared word-by-word
- [ ] Quality rated subjectively

---

## Debugging Edge ASR Issues

### Issue: No Backend Logs

**Symptom**: iOS sends segments but backend doesn't log anything

**Check**:
```bash
# 1. Verify WebSocket connection established
journalctl -u omi-backend -f | grep "connection open"

# 2. Verify message received
journalctl -u omi-backend -f | grep "transcript_segment"

# 3. Check for errors
journalctl -u omi-backend -f | grep "ERROR"
```

**Common Causes**:
- WebSocket not connected
- JSON malformed
- Missing `type` field
- Backend handler not deployed

---

### Issue: Segments Received but Not Saved

**Symptom**: Backend logs segment but Firestore has no conversation

**Check**:
```bash
# 1. Verify conversation created
journalctl -u omi-backend -f | grep "conversation_id"

# 2. Check Firestore errors
journalctl -u omi-backend -f | grep "Firestore"
```

**Common Causes**:
- Conversation timeout (2 minutes)
- Need to send "stop" message
- Firestore permissions issue

**Solution**: Send stop message
```json
{"type": "stop"}
```

---

### Issue: No Memories Extracted

**Symptom**: Conversation saved but no memories

**Check**:
```bash
# 1. Check memory agent called
journalctl -u omi-backend -f | grep "memory-agent"

# 2. Check for memory agent errors
journalctl -u omi-backend -f | grep "Memory processing"
```

**Common Causes**:
- Text too short (< 50 words)
- No interesting content detected
- Memory agent timeout
- Ella endpoint down

**Solution**: Send longer, more interesting text

---

## Test Data Examples

### Good Test Sentences (Likely to Extract Memories)

```
"I take blood pressure medication every morning at 8am"
"My daughter Sarah is getting married next month in June"
"I've been experiencing knee pain when I go up stairs"
"I need to schedule a follow-up appointment with Dr. Johnson"
"I love hiking in the mountains on weekends"
```

### Test Sentences for Urgency Detection

```
"I am having severe chest pain and trouble breathing"  // Critical
"I fell and hit my head, feeling dizzy"                 // High
"Hey Ella, what's the weather today?"                   // Question
"I went for a pleasant walk in the park"                // None
```

---

## iOS Test App Checklist

### Minimal Test Implementation

```swift
// 1. Connect to WebSocket (existing code)
let url = URL(string: "wss://api.ella-ai-care.com/v4/listen?uid=\(uid)&language=en")!
let webSocket = URLSessionWebSocketTask(...)

// 2. Add edge ASR test button
Button("Test Edge ASR") {
    sendEdgeASRSegment(text: "This is a test from edge ASR")
}

// 3. Listen for WebSocket events
func receiveMessage() {
    webSocket.receive { result in
        switch result {
        case .success(let message):
            if case .string(let text) = message {
                print("✅ Received: \(text)")
                // Parse and display in UI
            }
        case .failure(let error):
            print("❌ Error: \(error)")
        }
    }
}

// 4. Add logging
func sendEdgeASRSegment(text: String) {
    print("📤 Sending edge ASR: \(text)")
    // ... send logic ...
    print("✅ Sent successfully")
}
```

### Full Test Checklist

- [ ] WebSocket connection established
- [ ] Send single segment
- [ ] Verify backend logs received
- [ ] Verify iOS receives echo
- [ ] Send multiple segments
- [ ] Verify all segments logged
- [ ] Send stop message
- [ ] Verify Firestore conversation created
- [ ] Poll for memories
- [ ] Verify memories extracted
- [ ] Poll for summary
- [ ] Verify summary generated
- [ ] Test urgent content
- [ ] Verify scanner detection
- [ ] Compare vs Deepgram baseline

---

## Production Monitoring

### Metrics to Track

**Backend Logs** (add to monitoring):
```python
# Count edge ASR segments received
log.info(f"edge_asr.segment_received uid={uid} text_length={len(text)}")

# Count conversations by source
log.info(f"conversation.created uid={uid} source=edge_asr")

# Track latency
log.info(f"edge_asr.processing_time uid={uid} duration_ms={duration}")
```

**Firestore Queries** (for analytics):
```python
# Count edge ASR conversations
db.collection('conversations') \
  .where('source', '==', 'edge_asr') \
  .count()

# Compare memory extraction rate
edge_memories = conversations.where('source', '==', 'edge_asr').count_memories()
deepgram_memories = conversations.where('source', '==', 'deepgram').count_memories()
```

---

## Next Steps After Testing

1. **Verify basic flow works** (send 1 segment, check logs)
2. **Verify multi-segment conversations** (send 3+ segments)
3. **Verify memories extracted** (send interesting content)
4. **Verify scanner detection** (send urgent content)
5. **Compare quality vs Deepgram** (A/B test with same audio)
6. **Add source tracking** (distinguish edge ASR in Firestore)
7. **Deploy to production** (if tests pass)
8. **Monitor adoption rate** (% of users using edge ASR)

---

## Support

**Issues?**
1. Check backend logs first
2. Check iOS WebSocket connection
3. Verify message format (use test script)
4. Check Firestore console
5. Tag backend dev on Discord

**Documentation**:
- Implementation: `docs/EDGE_ASR_INTEGRATION_GUIDE.md`
- Tests: `tests/test_edge_asr.py`
- Handler: `routers/transcribe.py` lines 1100-1115

---

**Last Updated**: November 8, 2025
**Status**: ✅ Ready for iOS testing
**Backend**: Deployed and ready to receive edge ASR segments
