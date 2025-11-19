# N8N Scanner Webhook Schema

**Endpoint**: `POST https://n8n.ella-ai-care.com/webhook/scanner-agent`
**Purpose**: Real-time transcript segment streaming to Letta scanner agent
**Frequency**: Every 600ms during active conversation
**Source**: OMI Backend (`routers/transcribe.py` lines 929-949)
**Last Updated**: November 18, 2025 (Fixed device_type + stt_source schema)

---

## ⚠️ IMPORTANT SCHEMA UPDATE (Nov 18, 2025)

**Previous schema was WRONG** - conflated two different "source" fields:
1. **Device hardware type** (omi, friend, openglass) - was NOT being sent
2. **STT provider source** (edge_asr, deepgram, soniox) - was partially sent

**New schema separates these into**:
- `device_type` - Hardware device (omi, friend, openglass, etc.)
- `stt_source` - STT provider (edge_asr, deepgram, soniox, etc.)

---

## Request Schema

### Full Payload Structure

```json
{
  "uid": "string (required)",
  "device_type": "string (required)",
  "segments": [
    {
      "text": "string (required)",
      "speaker": "string (required)",
      "start": "float (required)",
      "end": "float (required)",
      "stt_source": "string (optional)"
    }
  ]
}
```

---

## Field Definitions

### Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uid` | string | ✅ Yes | Firebase User ID (e.g., "5aGC5YE9BnhcSoTxxtT4ar6ILQy2") |
| `device_type` | string | ✅ Yes | **NEW**: Hardware device type (see "Device Type Values" below) |
| `segments` | array | ✅ Yes | Array of transcript segment objects (1-N segments per batch) |

### Segment Object Fields

| Field | Type | Required | Description | Example Values |
|-------|------|----------|-------------|----------------|
| `text` | string | ✅ Yes | Transcribed text content | "I have chest pain", "Hello, how are you?" |
| `speaker` | string | ✅ Yes | Speaker identifier | "SPEAKER_00", "SPEAKER_01", "John Doe" |
| `start` | float | ✅ Yes | Segment start time in seconds | 0.0, 12.5, 45.8 |
| `end` | float | ✅ Yes | Segment end time in seconds | 4.8, 18.2, 52.3 |
| `stt_source` | string | ❌ Optional | **RENAMED**: Speech-to-text provider source | See "STT Source Values" below |

---

## Device Type Values

The `device_type` field identifies the **hardware device** capturing audio:

| Value | Description | Use Case |
|-------|-------------|----------|
| `"omi"` | OMI wearable device | Official OMI hardware, 24/7 monitoring |
| `"friend"` | Friend.com pendant | Friend pendant device |
| `"openglass"` | OpenGlass AR glasses | AR glasses with camera + audio |
| `"screenpipe"` | Screenpipe capture | Screen + audio recording |
| `"workflow"` | External workflow | Automated data ingestion |
| `"sdcard"` | SD card import | Offline audio import |
| `"external_integration"` | External integration | Third-party integrations |

**Source**: `models/conversation.py` - `ConversationSource` enum

---

## STT Source Values (Optional)

The `stt_source` field identifies the **speech-to-text provider** (when known):

| Value | Description | When Used |
|-------|-------------|-----------|
| `"edge_asr"` | iOS on-device ASR | Apple Speech Framework, Parakeet, Whisper.cpp |
| `"deepgram"` | Deepgram cloud STT | Cloud transcription (most common) |
| `"soniox"` | Soniox cloud STT | Alternative cloud provider |
| `"speechmatics"` | Speechmatics cloud STT | Alternative cloud provider |
| `null` / missing | Unknown STT source | Backend doesn't set source for Deepgram/Soniox yet |

**Source**: `models/transcript_segment.py` - `TranscriptSegment.source` field

**Note**: Currently only `"edge_asr"` is explicitly set in production. Deepgram/Soniox segments have `null` or missing `stt_source`.

---

## Real-World Examples

### Example 1: OMI Device with Deepgram (Most Common)

```json
{
  "uid": "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
  "device_type": "omi",
  "segments": [
    {
      "text": "I have chest pain and shortness of breath",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 4.8,
      "stt_source": null
    }
  ]
}
```

**Context**:
- **Device**: OMI wearable (hardware)
- **STT**: Deepgram cloud (stt_source not set yet)
- **Action**: Scanner should flag as EMERGENCY urgency due to OMI device + health keywords

---

### Example 2: Friend Pendant with Deepgram

```json
{
  "uid": "user-abc-123",
  "device_type": "friend",
  "segments": [
    {
      "text": "How are you feeling today?",
      "speaker": "SPEAKER_01",
      "start": 0.0,
      "end": 2.5,
      "stt_source": null
    },
    {
      "text": "I'm feeling much better, thanks for asking",
      "speaker": "SPEAKER_00",
      "start": 2.8,
      "end": 5.2,
      "stt_source": null
    }
  ]
}
```

**Context**:
- **Device**: Friend.com pendant (hardware)
- **STT**: Deepgram cloud
- **Action**: Scanner extracts conversation, lower urgency than OMI device

---

### Example 3: iOS App with Edge ASR (On-Device)

```json
{
  "uid": "test_user_123",
  "device_type": "omi",
  "segments": [
    {
      "text": "Hey Ella, remind me to take my blood pressure medication at 8am tomorrow",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 6.4,
      "stt_source": "edge_asr"
    }
  ]
}
```

**Context**:
- **Device**: OMI device (could also be phone_mic if iOS team sets it)
- **STT**: iOS on-device ASR (Apple Speech Framework, Parakeet, etc.)
- **Action**: Scanner should:
  1. Detect wake word ("Hey Ella")
  2. Extract action item (medication reminder)
  3. Trigger notification callback with urgency=WAKE_WORD

---

### Example 4: OpenGlass AR Glasses

```json
{
  "uid": "prod-user-456",
  "device_type": "openglass",
  "segments": [
    {
      "text": "I went to the grocery store and bought some vegetables",
      "speaker": "SPEAKER_00",
      "start": 12.5,
      "end": 16.8,
      "stt_source": null
    },
    {
      "text": "then I stopped by the pharmacy to pick up my prescription",
      "speaker": "SPEAKER_00",
      "start": 17.2,
      "end": 21.0,
      "stt_source": null
    }
  ]
}
```

**Context**:
- **Device**: OpenGlass AR glasses with camera
- **STT**: Deepgram cloud
- **Action**: Scanner can combine visual + audio context (if photos also sent)

---

## Device Type vs STT Source - Key Differences

### Device Type (`device_type`)
- **What it tracks**: Physical hardware capturing audio
- **Set by**: Backend at conversation creation (line 355 in transcribe.py)
- **Granularity**: Per conversation (all segments have same device_type)
- **Use case**: Routing logic (OMI = higher urgency, Friend = social context, OpenGlass = visual context)

### STT Source (`stt_source`)
- **What it tracks**: Speech-to-text processing method
- **Set by**: Backend at segment creation (only for edge_asr currently)
- **Granularity**: Per segment (could mix Deepgram + Edge ASR in same conversation)
- **Use case**: Quality tracking, cost analysis, A/B testing STT providers

### Why Both Matter

**Example Scenario**: User has OMI device but tests iOS app

```json
{
  "uid": "user-123",
  "device_type": "omi",          ← Hardware: OMI wearable
  "segments": [
    {
      "text": "Testing on-device ASR",
      "stt_source": "edge_asr"   ← STT: iOS on-device processing
    }
  ]
}
```

Scanner knows:
1. **OMI device** → Apply health monitoring urgency rules
2. **Edge ASR** → Transcription happened on-device (privacy preserved)

---

## Integration Flow

```
iOS Device/OMI Wearable/Friend Pendant/OpenGlass
        ↓
  Audio Stream (Opus/PCM/AAC)
        ↓
Backend WebSocket (/v4/listen)
  ├─ Sets device_type from conversation.source (omi, friend, openglass)
  └─ Creates conversation with device metadata
        ↓
STT Provider (Deepgram/Edge ASR/Soniox)
  └─ Sets stt_source in segment (edge_asr, deepgram, soniox, null)
        ↓
Transcript Segments (600ms batches)
        ↓
n8n Scanner Webhook ← YOU ARE HERE
  ├─ device_type: Hardware device (omi, friend, etc.)
  └─ segments[].stt_source: STT provider (edge_asr, deepgram, null)
        ↓
Letta Scanner Agent
  ├─ Route by device_type (OMI=health, Friend=social, OpenGlass=visual)
  └─ Track quality by stt_source (edge_asr vs cloud)
        ↓
[Intent Classification]
  ├─ EMERGENCY → Notification Callback
  ├─ WAKE_WORD → Notification Callback
  ├─ QUESTION → Main Agent → Notification Callback
  └─ NORMAL → Continue listening
```

---

## Processing Guidelines for Letta Scanner

### Device-Based Routing

**OMI Device** (`device_type: "omi"`):
- **Higher urgency threshold** - User wearing 24/7, expect health monitoring
- **Prioritize emergency detection** - Chest pain, falls, distress
- **Context**: Personal health assistant

**Friend Pendant** (`device_type: "friend"`):
- **Social conversation context** - More likely multi-speaker
- **Medium urgency** - Social interactions, not medical monitoring
- **Context**: Social companion

**OpenGlass** (`device_type: "openglass"`):
- **Visual context available** - May include photos
- **Medium urgency** - Task-oriented (shopping, navigation)
- **Context**: AR assistant with visual input

**iOS/External** (`device_type: "workflow"`, `"external_integration"`):
- **Explicit user action** - User manually triggered
- **Variable urgency** - Depends on content
- **Context**: User actively engaged with app

### STT Source Handling

**Edge ASR** (`stt_source: "edge_asr"`):
- User explicitly opened iOS app
- Higher intent signal (actively engaged)
- Prioritize wake word detection
- Privacy-conscious (transcription on-device)

**Cloud STT** (`stt_source: null`, `"deepgram"`, `"soniox"`):
- Standard cloud processing
- Reliable transcription quality
- Most common path

### Speaker Handling

- **SPEAKER_00**: Typically the user (device owner)
- **SPEAKER_01+**: Other participants in conversation
- **Named speakers** (future): "John Doe", "Dr. Smith" after voice profile learning

### Temporal Continuity

- `start` and `end` times are cumulative from conversation start
- Segments arrive in chronological order
- Gaps between segments indicate silence/pauses

---

## Error Handling

### Backend Behavior

- **Timeout**: 2 seconds (realtime processing)
- **Failure Mode**: Silent (does not break transcription pipeline)
- **Retry Logic**: None (fire-and-forget for realtime performance)
- **Fallback**: If conversation.source is null, defaults to "omi"

### Expected Scanner Response

- **Status Code**: 200 OK (any response is accepted)
- **Response Body**: Ignored by backend
- **Processing Time**: Should be < 1 second for realtime UX

**Important**: Scanner webhook is non-blocking. Slow responses do not delay transcription.

---

## Testing

### E2E Testing Endpoint

**Endpoint**: `POST /v1/test/scanner-agent`
**Authorization**: `Bearer {ADMIN_KEY}{uid}` (e.g., `dev_testing_key_12345test_user_123`)

**Test Payload** (New Schema):
```bash
curl -X POST "https://api.ella-ai-care.com/v1/test/scanner-agent" \
  -H "Authorization: Bearer dev_testing_key_12345test_user_123" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I have chest pain and difficulty breathing",
    "uid": "test_user_123",
    "device_type": "omi",
    "source": "edge_asr",
    "debug": true
  }'
```

**Expected Behavior**:
1. Backend creates single segment with provided text and stt_source
2. Sends to n8n scanner webhook with device_type field
3. Returns n8n response (should detect EMERGENCY urgency)

---

## Monitoring

### Backend Logs

```bash
# Watch scanner webhook calls in production
ssh root@100.101.168.91 'journalctl -u omi-backend -f | grep scanner'

# Example output (New format):
# 🔍 [Scanner] Device Type: omi
# 🔍 [Scanner] STT Source: edge_asr
# Sending 3 segments to scanner webhook for uid=5aGC5YE9...
```

### n8n Webhook Logs

Check n8n workflow execution logs for:
- Successful payload reception with device_type field
- Letta agent processing time
- Intent classification results

---

## Migration Guide

### For Letta/n8n Team

**Old Schema** (Before Nov 18, 2025):
```json
{
  "uid": "user-123",
  "segments": [
    {
      "text": "...",
      "source": "omi"  // ❌ Ambiguous - device or STT?
    }
  ]
}
```

**New Schema** (After Nov 18, 2025):
```json
{
  "uid": "user-123",
  "device_type": "omi",  // ✅ Clear - hardware device
  "segments": [
    {
      "text": "...",
      "stt_source": "edge_asr"  // ✅ Clear - STT provider
    }
  ]
}
```

### Breaking Changes

1. **`device_type` now required** at top level
2. **`segments[].source` renamed to `segments[].stt_source`**
3. **`segments[].stt_source` is now optional** (can be null/missing)

### Backward Compatibility

**None** - This is a breaking change. n8n workflows must update to handle new schema.

---

## Change History

| Date | Version | Changes |
|------|---------|---------|
| 2025-11-18 | 2.0 | **BREAKING**: Separated device_type and stt_source fields |
| 2025-11-16 | 1.0 | Initial documentation with source field (incorrect schema) |

---

## Contact

- **Backend Team**: See `backend/CLAUDE.md` for architecture details
- **Letta Integration**: See `docs/LETTA_INTEGRATION_ARCHITECTURE.md`
- **API Callbacks**: See `docs/ELLA_CALLBACK_ENDPOINTS.md`

---

**Production URL**: https://n8n.ella-ai-care.com/webhook/scanner-agent
**Status**: ✅ Active and processing realtime segments
**Device Type Field**: ✅ Added November 18, 2025
**STT Source Field**: ✅ Renamed from "source" November 18, 2025
