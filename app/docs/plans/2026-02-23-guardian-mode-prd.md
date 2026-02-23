# Guardian Mode - Product Requirements Document

**Date:** 2026-02-23  
**Status:** Draft - POC/MVP Phase  
**Author:** @EllaDev  
**Reviewers:** Greg (Product)

---

## Overview

Guardian Mode is a new background monitoring feature that enables proactive AI voice assistance for Ella users. Unlike active VoiceChat (foreground conversation), Guardian Mode runs passively in the background, processing audio from the user's Omi pendant and delivering AI-generated voice alerts/reminders via bone conduction headset.

**Key Innovation:** Uses playback-only audio session (no iOS microphone access) to bypass Apple's background recording restrictions, enabling true background operation.

---

## Goals

### MVP/POC Goals
1. **Prove the concept**: Validate that iOS allows continuous background audio playback for 15+ minutes
2. **Test audio injection**: Verify AI voice clips can be queued and played seamlessly via Bluetooth
3. **Measure battery impact**: Establish baseline power consumption (~5-10% over 8 hours target)
4. **Simple UX**: Single button to start/stop, basic status indicator

### Long-term Goals (Post-MVP)
- Integration with Omi pendant real-time transcription
- Backend AI escalation logic (confusion detection, medication reminders, etc.)
- Auto-start and persistence across app sessions
- Scheduling and time-based activation
- Rich UI with animations and detailed status

---

## Background & Context

### Problem
- Ella's VoiceChat uses on-device speech recognition (`SFSpeechRecognizer`) which **cannot run in background** due to iOS privacy restrictions
- Users want **proactive voice assistance** that works while phone is locked/backgrounded
- VoIP push + CallKit approach blocked by Twilio Error 52131 (certificate issues)

### Solution Inspiration
**Grok's recommendation:** Use streaming music-style playback pattern (like Spotify/YouTube) to keep audio session alive in background. This is:
- ✅ Apple-approved (standard audio app behavior)
- ✅ Privacy-friendly (playback-only, no mic access)
- ✅ Battle-tested (Omi app likely uses similar for readbacks)

### Architecture Pattern
```
User taps "Start Guardian Mode"
  ↓
App starts silent audio loop (100ms silence, continuous)
  ↓
Audio session stays alive in background (AVAudioSession.playback)
  ↓
Omi pendant captures audio → backend transcribes → AI detects issues
  ↓
Server sends voice clip → app injects into playback queue
  ↓
Clip plays via Bluetooth bone conduction headset
```

---

## User Stories

### MVP User Stories
1. **As an elder user**, I want to tap a "Guardian Mode" button so that Ella starts monitoring me in the background
2. **As an elder user**, I want Guardian Mode to continue working when I lock my phone or switch apps
3. **As an elder user**, I want to hear test voice messages through my bone conduction headset to know it's working
4. **As an elder user**, I want to tap the button again to stop Guardian Mode

### Future User Stories (Post-MVP)
5. **As an elder user**, I want Guardian Mode to auto-start when my Omi pendant connects
6. **As a caregiver**, I want to configure what types of alerts trigger Guardian Mode voice messages
7. **As an elder user**, I want to set a schedule for when Guardian Mode is active (e.g., 8am-8pm)

---

## MVP Scope

### In Scope (POC)
- ✅ Manual start/stop button
- ✅ Silent audio loop to keep session alive
- ✅ Test audio clip generation (on-device TTS)
- ✅ Periodic injection (every 30 seconds)
- ✅ Background operation validation (15+ minutes)
- ✅ Basic UI status indicator (ON/OFF/ERROR states)
- ✅ Bluetooth audio routing

### Out of Scope (MVP)
- ❌ Real Omi transcription integration
- ❌ Backend AI escalation logic
- ❌ Auto-start on pendant connect
- ❌ Persistence across app launches
- ❌ Scheduling/time-based activation
- ❌ Rich animations or detailed status UI
- ❌ Battery optimization (beyond basic measurement)

---

## Core Architecture

### Component Diagram
```
┌─────────────────┐
│  Guardian Mode  │
│     Button      │
│  (UI Component) │
└────────┬────────┘
         │
         │ tap → start/stop
         ▼
┌─────────────────────────────────┐
│   Guardian Mode Service         │
│   (Flutter - Dart)              │
│                                 │
│  - State management             │
│  - Test audio scheduling        │
│  - MethodChannel bridge         │
└────────┬────────────────────────┘
         │
         │ Platform calls
         ▼
┌─────────────────────────────────┐
│   Guardian Mode Manager         │
│   (iOS - Swift)                 │
│                                 │
│  - AVAudioSession (.playback)   │
│  - AVQueuePlayer                │
│  - Silent audio loop (100ms)    │
│  - Audio clip injection         │
└────────┬────────────────────────┘
         │
         │ audio output
         ▼
┌─────────────────────────────────┐
│   Bluetooth Audio Output        │
│   (Bone Conduction Headset)     │
└─────────────────────────────────┘
```

### Technical Decisions

| Decision | Rationale |
|----------|-----------|
| **AVAudioSession category: `.playback`** | Playback-only = no mic access = no privacy concerns = Apple approval |
| **Silent loop: 100ms WAV** | Minimal file size, imperceptible, keeps session alive |
| **AVQueuePlayer** | Can inject clips without stopping loop, standard iOS pattern |
| **On-device TTS for POC** | `AVSpeechSynthesizer` = no network = faster iteration |
| **30-second test interval** | Frequent enough to validate queueing, not too spammy |

---

## Implementation Components

### New Files

#### Flutter (Dart)
**`lib/ella/services/guardian_mode_service.dart`**
- Singleton service managing Guardian Mode state
- Handles start/stop logic
- Schedules test audio generation (every 30s)
- Exposes state stream: `idle`, `active`, `error`
- MethodChannel bridge to iOS native code

**`lib/ella/widgets/guardian_mode_button.dart`**
- Toggle button widget
- Visual states:
  - **OFF**: Gray circle with moon icon
  - **ON**: Pulsing green glow with shield icon
  - **ERROR**: Red circle with warning icon
- Haptic feedback on tap

#### iOS (Swift)
**`ios/Runner/GuardianMode/GuardianModeManager.swift`**
- Configures AVAudioSession for `.playback` category
- Creates silent audio buffer (100ms WAV, looped)
- Sets up AVQueuePlayer for continuous playback
- Handles audio clip injection
- Manages audio session interruptions (phone calls, etc.)
- Exposes MethodChannel interface to Flutter

**`ios/Runner/GuardianMode/silence_100ms.wav`**
- Pre-generated 100ms silent audio file
- 16kHz, mono, WAV format
- Looped infinitely via AVQueuePlayer

### Modified Files

**`lib/ella/pages/[home_screen].dart`** (or settings)
- Add `GuardianModeButton` to UI
- Position: Near VoiceChat orb or in settings section

**`ios/Runner/AppDelegate.swift`**
- Initialize `GuardianModeManager` singleton
- Register MethodChannel handler
- Ensure AVAudioSession is configured (already done for background audio)

---

## Test Plan (Hot Path)

### Phase 1: Basic Audio Loop (5 minutes)
**Objective:** Verify silent loop keeps audio session alive in background

**Steps:**
1. Launch app, tap "Start Guardian Mode"
2. Verify button changes to ON state (pulsing green)
3. Lock screen (press power button)
4. Wait 3 minutes
5. Check iOS Control Center → verify app shows as "playing audio"
6. Unlock and check app → verify still in ON state

**Success Criteria:**
- ✅ Audio session survives 3+ minutes locked
- ✅ iOS recognizes app as active audio player
- ✅ No suspension or termination

### Phase 2: Test Audio Injection (10 minutes)
**Objective:** Verify audio clips play through bone conduction headset

**Steps:**
1. Connect bone conduction headset via Bluetooth
2. Start Guardian Mode
3. Every 30 seconds, service generates test clip: "Guardian test number [N]"
4. Listen for clips through headset
5. Background app (swipe home)
6. Continue listening for 10 minutes (20 clips)

**Success Criteria:**
- ✅ All 20 clips play successfully
- ✅ Audio routes to Bluetooth (not phone speaker)
- ✅ No skipping or clipping
- ✅ Loop continues uninterrupted

### Phase 3: Background Endurance (15 minutes)
**Objective:** Verify long-running background operation

**Steps:**
1. Fully charge phone (or note starting battery %)
2. Start Guardian Mode
3. Lock screen and leave phone idle
4. Monitor battery drain
5. After 15 minutes, check:
   - Battery % change
   - App still running
   - All test clips played

**Success Criteria:**
- ✅ Audio session survives 15+ minutes
- ✅ All ~30 test clips played
- ✅ Battery drain < 5% over 15 minutes
- ✅ No iOS suspension

### Failure Scenarios to Watch

| Symptom | Likely Cause | Next Step |
|---------|--------------|-----------|
| Session dies after 3-5 min | Silent loop not working | Check AVQueuePlayer loop logic |
| Clips don't play | Queue management issue | Debug AVQueuePlayer insertion |
| Audio routes to speaker | Bluetooth disconnected | Check audio route change handling |
| Battery >10% drain | Audio settings suboptimal | Test with `.mixWithOthers` option |

---

## Success Criteria

### MVP Success
- ✅ Guardian Mode runs for 15+ minutes in background without iOS termination
- ✅ Test audio clips play successfully via Bluetooth bone conduction
- ✅ Battery drain < 5% over 15 minutes
- ✅ User can start/stop with single button tap
- ✅ Visual feedback clearly indicates ON/OFF/ERROR states

### Long-term Success (Post-MVP)
- Integration with real Omi transcription (backend)
- AI-generated voice clips based on conversation analysis
- Auto-start and persistence features
- Scheduling and caregiver configuration
- <10% battery drain over 8 hours continuous use

---

## Non-Goals (Out of Scope)

### MVP Non-Goals
- Complex UI animations or rich status displays
- Integration with backend transcription (using mock audio for POC)
- Auto-start, persistence, or scheduling features
- Battery optimization (beyond basic measurement)
- Error recovery or retry logic

### Long-term Non-Goals
- Video monitoring or visual alerts
- Third-party integrations (beyond Omi pendant)
- Multi-device sync or cloud storage
- Advanced analytics or reporting

---

## Future Enhancements

### Post-MVP Feature Ideas
1. **Real Omi Integration**
   - Connect to backend transcription service
   - Process real conversation data
   - Trigger AI escalation logic

2. **Smart Scheduling**
   - Time-based activation (e.g., 8am-8pm)
   - Location-based triggers (home vs away)
   - Calendar integration (active during caregiver visits)

3. **Rich Status UI**
   - Last alert timestamp
   - Activity feed (recent voice messages)
   - Battery impact estimation

4. **Caregiver Controls**
   - Configure alert types and thresholds
   - Remote enable/disable
   - Notification when Guardian Mode stops unexpectedly

5. **Battery Optimization**
   - Adaptive interval adjustment (more frequent when active conversation)
   - Low-power mode detection and adjustment
   - Background task scheduling for non-critical updates

---

## Technical Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| iOS rejects app for "fake audio playback" | High | Use real silence (not silent flag), document use case clearly |
| Battery drain >10% over 8 hours | Medium | Profile and optimize audio settings, test with different codecs |
| Bluetooth disconnects unexpectedly | Medium | Handle audio route changes gracefully, notify user |
| Silent loop audible on some devices | Low | Test on multiple devices, adjust loop duration if needed |
| Test clips interrupt each other | Low | Use AVQueuePlayer properly, ensure thread safety |

---

## Open Questions

1. **Where should Guardian Mode button live?**
   - Option A: Next to VoiceChat orb on home screen
   - Option B: In Ella settings page
   - Option C: Both (button on home, toggle in settings)

2. **What should the icon be?**
   - Shield (guardian/protection theme)
   - Moon (passive/background theme)
   - Ear (listening/monitoring theme)

3. **Should we add haptic feedback for test clips?**
   - Pro: Reinforces that clip is playing
   - Con: May be distracting/annoying

4. **How to handle phone calls?**
   - Pause Guardian Mode during call, auto-resume after?
   - Or let iOS handle audio session interruption automatically?

---

## Appendix: Grok's Original Recommendation

**Grok's Three Approaches:**

1. **Streaming Music Flow** (CHOSEN FOR MVP)
   - Continuous silent loop + clip injection
   - Apple-approved, battle-tested pattern
   - ~5-10% battery over 8 hours

2. **Bursty Background Tasks**
   - Periodic wake-ups (every 15-30 min)
   - Lower battery, but less seamless
   - Delays up to 15 min for alerts

3. **PushKit VoIP Flow**
   - Instant wake + clip playback
   - Riskier Apple review (not true telephony)
   - Already blocked by Twilio issues

**Why we chose #1:** Proven pattern, best user experience, acceptable battery trade-off.

---

## References

- Apple AVAudioSession Programming Guide: https://developer.apple.com/documentation/avfaudio/avaudiosession
- Apple Background Execution Guidelines: https://developer.apple.com/documentation/backgroundtasks
- AVQueuePlayer Documentation: https://developer.apple.com/documentation/avfoundation/avqueueplayer
- Omi App (upstream): https://github.com/BasedHardware/omi
- Ella AI (fork): https://github.com/ellaaicare/omi

---

**END OF PRD**
