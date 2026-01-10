# PRD: Grok Voice-to-Voice Integration for iOS

**Document Version**: 1.0
**Date**: January 10, 2026
**Author**: Backend Developer
**Status**: Ready for iOS Review
**Related Issue**: [#30 - Grok Voice-to-Voice Integration](https://github.com/ellaaicare/omi/issues/30)

---

## Executive Summary

The backend now supports **two voice pipeline modes**:

1. **Pipecat** (default): STT → LLM → TTS pipeline (2-3s latency)
2. **Grok V2V**: Grok's native voice-to-voice API (~500ms latency)

**Good news**: iOS requires **minimal changes** to test Grok V2V mode.

---

## iOS Changes Required

### TL;DR: Almost Nothing

| Aspect | Current | Grok V2V Mode | iOS Change Needed? |
|--------|---------|---------------|-------------------|
| **Endpoint** | `/v2/voice` | `/v2/voice?pipeline_mode=grok_v2v` | Add query param |
| **Input Audio** | 16kHz PCM16 | 16kHz PCM16 | **No change** |
| **Output Audio** | 24kHz PCM16 | 24kHz PCM16 | **No change** |
| **WebSocket Protocol** | Binary audio frames | Binary audio frames | **No change** |

---

## Detailed Specification

### 1. WebSocket URL

**Current (Pipecat mode)**:
```
wss://api.ella-ai-care.com/v2/voice?uid={uid}
```

**Grok V2V mode** (add `pipeline_mode` parameter):
```
wss://api.ella-ai-care.com/v2/voice?uid={uid}&pipeline_mode=grok_v2v
```

### 2. Audio Format (UNCHANGED)

| Parameter | Value | Notes |
|-----------|-------|-------|
| **Input Sample Rate** | 16kHz | Backend resamples to 24kHz for Grok |
| **Input Format** | PCM16 (signed 16-bit little-endian) | Same as current |
| **Input Channels** | Mono (1) | Same as current |
| **Output Sample Rate** | 24kHz | Same as current (OpenAI TTS also outputs 24kHz) |
| **Output Format** | PCM16 (signed 16-bit little-endian) | Same as current |
| **Output Channels** | Mono (1) | Same as current |

### 3. Protocol Flow (UNCHANGED)

```
iOS                                    Backend
 │                                        │
 │──── WebSocket Connect ────────────────>│
 │                                        │ (Initialize Grok or Pipecat)
 │<─── Ready ────────────────────────────│
 │                                        │
 │──── Audio Frame (16kHz PCM16) ────────>│
 │──── Audio Frame (16kHz PCM16) ────────>│
 │     (continue streaming audio...)      │
 │                                        │ (VAD detects speech end)
 │                                        │ (Process audio)
 │<─── Audio Frame (24kHz PCM16) ─────────│
 │<─── Audio Frame (24kHz PCM16) ─────────│
 │     (receive TTS response...)          │
 │                                        │
 │──── Close ────────────────────────────>│
```

---

## Implementation Options

### Option A: Quick A/B Test (Recommended First Step)

Add a toggle in developer settings to switch between modes:

```swift
// In VoiceModeService.swift or similar
class VoiceModeService {
    enum PipelineMode: String {
        case pipecat = "pipecat"
        case grokV2V = "grok_v2v"
    }

    var pipelineMode: PipelineMode = .pipecat

    func buildWebSocketURL(uid: String) -> URL {
        var components = URLComponents(string: "wss://api.ella-ai-care.com/v2/voice")!
        components.queryItems = [
            URLQueryItem(name: "uid", value: uid),
            URLQueryItem(name: "pipeline_mode", value: pipelineMode.rawValue)
        ]
        return components.url!
    }
}
```

### Option B: User Setting

Allow users to choose their preferred voice mode in settings:

```swift
// In Settings
Toggle("Use Ultra-Fast Voice Mode (Beta)", isOn: $useGrokV2V)
    .help("Reduces response time from ~2s to ~0.5s")
```

### Option C: Automatic Fallback

Backend can be configured to automatically use Grok V2V when available:

```
VOICE_PIPELINE_MODE=grok_v2v  # Set on backend
```

iOS doesn't need any changes for this option.

---

## Testing Checklist

### Basic Functionality
- [ ] Connect with `pipeline_mode=grok_v2v` query param
- [ ] Send 16kHz PCM16 audio
- [ ] Receive 24kHz PCM16 response
- [ ] Verify audio plays correctly
- [ ] Verify conversation transcripts appear in app

### Latency Comparison
- [ ] Measure time from end-of-speech to first audio response (Pipecat)
- [ ] Measure time from end-of-speech to first audio response (Grok V2V)
- [ ] Target: Grok V2V should be 60-70% faster

### Edge Cases
- [ ] Barge-in (interrupt AI mid-response)
- [ ] Long utterances (30+ seconds)
- [ ] Network interruption handling
- [ ] Fallback if Grok proxy unavailable

---

## Backend Health Check

Verify Grok V2V is available:

```bash
curl https://api.ella-ai-care.com/v2/voice/health
```

Response includes:
```json
{
  "pipeline_mode": "pipecat",
  "grok_v2v": {
    "enabled": false,
    "proxy_url": "wss://voice.ella-ai-care.com/ws",
    "model": "grok-2-voice-preview",
    "input_sample_rate": 24000,
    "output_sample_rate": 24000
  }
}
```

When `grok_v2v.enabled` is `true`, iOS can use the Grok V2V mode.

---

## Timeline

1. **Phase 1** (Now): Backend implementation complete
2. **Phase 2** (iOS): Add `pipeline_mode` query parameter support
3. **Phase 3** (Testing): A/B test latency comparison
4. **Phase 4** (Rollout): Enable for all users if successful

---

## Questions for iOS Team

1. Do you need any additional transcript/event data during the session?
2. Should we add a "connecting to ultra-fast mode" indicator?
3. Any concerns about the 24kHz output audio handling?

---

## Contact

- **Backend**: Claude-Backend-Developer
- **Ella Team**: See Issue #30 for Grok proxy details
- **Testing**: Use `pipeline_mode=grok_v2v` query param

---

*Document created: January 10, 2026*
