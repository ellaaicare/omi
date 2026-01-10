# Ella Team Request: Grok Voice Proxy Deployment

**Date**: January 10, 2026
**From**: Backend Developer
**To**: Ella AI Team
**Re**: Issue #30 - Grok Voice-to-Voice Integration

---

## Summary

Backend integration for Grok V2V is **complete**. We need the Ella team to deploy their Grok voice proxy.

---

## What Backend Has Done

1. Added `pipeline_mode=grok_v2v` query parameter to `/v2/voice` endpoint
2. Created `GrokVoicePipeline` class that connects to `wss://voice.ella-ai-care.com/ws`
3. Handles 16kHz→24kHz audio resampling
4. Integrates with n8n for config fetch and post-call processing
5. iOS PRD created (minimal iOS changes needed)

---

## What Ella Team Needs To Do

### 1. Deploy Grok Voice Proxy

Deploy the voice proxy to `wss://voice.ella-ai-care.com/ws`

**Expected protocol from backend's perspective:**

```
Backend → WebSocket Connect: wss://voice.ella-ai-care.com/ws?uid={uid}

Backend → Send: {"type": "session.init", "system_prompt": "...", "voice": "Cove", "temperature": 0.7}

Backend → Send: [24kHz PCM16 audio bytes]

Proxy → Send: [24kHz PCM16 audio response bytes]

Proxy → Send: {"type": "transcript", "role": "user", "text": "..."}
Proxy → Send: {"type": "transcript", "role": "assistant", "text": "..."}
```

### 2. Confirm Audio Specs

| Parameter | Expected Value |
|-----------|----------------|
| Input Format | PCM16 signed little-endian |
| Input Sample Rate | 24kHz |
| Output Format | PCM16 signed little-endian |
| Output Sample Rate | 24kHz |

### 3. SSL Certificate

Current issue: `wss://voice.ella-ai-care.com/ws` returns SSL cert error.

---

## One-Liner for Ella Team

> "Backend Grok V2V integration done. Need Ella team to deploy voice proxy at `wss://voice.ella-ai-care.com/ws`. Expected protocol: WebSocket accepting 24kHz PCM16 audio, returning 24kHz PCM16 + transcript JSON events. See Issue #30."

---

## Testing Once Proxy is Up

```bash
# Backend will route to Grok proxy when:
ws://api.ella-ai-care.com/v2/voice?uid=test&pipeline_mode=grok_v2v

# Or set globally via env var:
VOICE_PIPELINE_MODE=grok_v2v
```

---

## Contact

Backend changes committed: `491f1b3f1` (voice-init) + new Grok V2V files (pending commit)

Ready to test as soon as proxy is deployed.
