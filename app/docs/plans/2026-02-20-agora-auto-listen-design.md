# Agora Auto-Listen Mode Design

**Date:** 2026-02-20  
**Status:** Implementation in progress (user asleep, autonomous work)

## Problem Statement

The Agora test button successfully connects to RTC channels but doesn't stream audio or handle voice interaction. This makes autonomous testing impossible.

**Symptoms:**
- Agora connects successfully (logs show "Successfully joined channel")
- No audio transmission (iPhone mic → Agora)
- No audio reception (bot.py → iPhone speaker)
- No transcription or responses visible

**Root Cause:**
The AgoraTestButton only tests connectivity - it doesn't implement audio streaming callbacks.

## Requirements

1. **For Autonomous Testing:**
   - Auto-start Agora voice test on app launch or tab switch
   - Stream iPhone microphone audio to Agora channel
   - Receive and play audio from bot.py
   - Work without manual interaction

2. **For Debugging:**
   - Detailed logging of audio frames
   - Connection state visibility
   - Error reporting

## Design

### Architecture

```
iPhone Mic → AgoraService (audio callbacks) → Agora RTC Channel
                                                      ↓
                                                   bot.py (Mac Mini)
                                                      ↓
                                              Deepgram STT → OpenClaw → ElevenLabs TTS
                                                      ↓
                                            Agora RTC Channel → AgoraService → iPhone Speaker
```

### Components Modified

1. **AgoraService** (`lib/services/agora_service.dart`)
   - Add `onAudioFrameReceived` callback
   - Add `onRecordAudioFrame` callback  
   - Enable audio streaming in channel options
   - Add logging for audio frame events

2. **AgoraTestButton** (`lib/pages/home/widgets/agora_test_button.dart`)
   - Add auto-start parameter/flag
   - Add audio state indicators
   - Add error handling for audio issues

3. **Testing Configuration**
   - Environment variable or compile-time flag to enable auto-start
   - Default: manual start (production)
   - Debug: auto-start on home tab

### Implementation Plan

**Phase 1: Audio Streaming (Critical)**
1. Modify AgoraService to register audio callbacks
2. Enable `publishMicrophoneTrack` in channel options
3. Add audio frame logging
4. Test: Play Mac Mini audio → verify iPhone receives it

**Phase 2: Auto-Start (For Testing)**
1. Add `autoStart` parameter to AgoraTestButton
2. Trigger on home tab didChangeDependencies
3. Add visual indicator when auto-started

**Phase 3: Backend Verification**
1. Verify bot.py spawns correctly
2. Check Deepgram API calls in backend logs
3. Confirm TTS audio returns via Agora

### Trade-offs

**Approach 1: Full Agora Voice Page** (NOT chosen)
- ✅ Clean separation of WebSocket vs Agora
- ❌ Too much work for testing
- ❌ User going to bed, needs quick solution

**Approach 2: Modify WebSocket Page** (NOT chosen)
- ✅ Reuses existing auto-listen logic
- ❌ Couples two different systems
- ❌ Risk breaking working WebSocket voice

**Approach 3: Enhance Agora Test Button** (CHOSEN)
- ✅ Minimal changes
- ✅ Isolated from WebSocket voice  
- ✅ Can test autonomously
- ✅ Quick to implement
- ❌ Not production-ready (fine for testing)

## Testing Strategy

**Autonomous Test Loop:**
1. Auto-start Agora on app launch
2. Play Mac Mini speech: `say "Hello Ella, test message"`
3. Monitor logs for:
   - Audio frames sent
   - Bot join event
   - Backend transcription
   - Audio frames received
4. Take screenshots of UI state
5. Document results

**Success Criteria:**
- iPhone sends audio to Agora channel (visible in logs)
- bot.py receives and transcribes audio
- TTS response plays on iPhone
- Full loop works without manual interaction

## Rollback Plan

All changes on `feature/agora-parallel-integration` branch.  
Working WebSocket voice untouched on `main` branch.  
Can revert if needed.

## Notes

- User is asleep - implementing autonomously for morning review
- Focus on making Agora work end-to-end first
- Auto-listen is secondary (nice-to-have for testing)
- If Agora audio fails, may need backend bot.py debugging

