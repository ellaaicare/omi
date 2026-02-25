# Agora Auto-Listen Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable autonomous Agora voice testing by adding audio streaming and auto-start capabilities to the Agora test button.

**Architecture:** Enhance AgoraService with audio frame callbacks to stream iPhone microphone to Agora RTC channel and receive bot audio. Add auto-start trigger to AgoraTestButton for hands-free testing. Backend bot.py already handles transcription/TTS.

**Tech Stack:** Flutter/Dart, Agora RTC SDK 6.3.2, iOS (Swift/Objective-C interop), Mac Mini backend (Python bot.py)

---

## Task 1: Fix Agora App ID (Critical Blocker)

**Files:**
- Modify: `/Users/ellaai/dev/omi/app/lib/services/agora_service.dart:51`

**Problem:** App ID reverted to placeholder 'YOUR_AGORA_APP_ID' causing ERR_INVALID_APP_ID (-101)

**Step 1: Update App ID**

```dart
// Line 51 in agora_service.dart - change from:
appId: 'YOUR_AGORA_APP_ID', // Will be configured via environment/config

// To:
appId: '55dd93fbff4946d7bcbff6f6ebcee462', // Ella AI production App ID
```

**Step 2: Verify change**

Run on Mac Mini:
```bash
ssh ellaai@100.76.138.56 "grep 'appId:' /Users/ellaai/dev/omi/app/lib/services/agora_service.dart"
```

Expected: Shows real App ID, not placeholder

**Step 3: Commit**

```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git add lib/services/agora_service.dart && git commit -m 'fix(agora): restore production App ID

App ID was reverted to placeholder causing ERR_INVALID_APP_ID (-101).
Restored to production App ID from backend .env.'"
```

---

## Task 2: Enable Audio Streaming in AgoraService

**Files:**
- Modify: `/Users/ellaai/dev/omi/app/lib/services/agora_service.dart:56-81`

**Step 1: Add audio frame event handlers**

In `_ensureInitialized()` method, after existing event handlers (line 81), add:

```dart
      onAudioVolumeIndication: (connection, speakers, speakerNumber, totalVolume) {
        if (speakers.isNotEmpty) {
          debugPrint('[AgoraService] Audio volume: ${speakers[0].volume}');
        }
      },
      onFirstRemoteAudioFrame: (connection, userId, elapsed) {
        debugPrint('[AgoraService] First remote audio frame from user: $userId');
      },
      onFirstLocalAudioFrame: (connection, elapsed) {
        debugPrint('[AgoraService] First local audio frame sent (${elapsed}ms)');
      },
    ));
```

**Step 2: Enable audio volume indication**

After `enableAudio()` call (around line 84), add:

```dart
    await _engine!.enableAudioVolumeIndication(interval: 200, smooth: 3, reportVad: true);
```

**Step 3: Verify compiles**

Run on Mac Mini:
```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && /Users/ellaai/dev/flutter/bin/flutter analyze lib/services/agora_service.dart"
```

Expected: No errors

**Step 4: Commit**

```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git add lib/services/agora_service.dart && git commit -m 'feat(agora): add audio streaming event handlers

- Add onAudioVolumeIndication for monitoring audio levels
- Add onFirstRemoteAudioFrame to detect bot audio
- Add onFirstLocalAudioFrame to confirm mic transmission
- Enable volume indication with 200ms interval'"
```

---

## Task 3: Add Auto-Start to AgoraTestButton

**Files:**
- Modify: `/Users/ellaai/dev/omi/app/lib/pages/home/widgets/agora_test_button.dart`

**Step 1: Add auto-start state and lifecycle**

Add to `_AgoraTestButtonState` class (after line 15):

```dart
  bool _autoStarted = false;

  @override
  void initState() {
    super.initState();
    // Auto-start after 2 seconds for autonomous testing
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted && !_isInCall && !_autoStarted) {
        _autoStarted = true;
        debugPrint('[AgoraTest] Auto-starting for autonomous testing');
        _startAgoraTest();
      }
    });
  }
```

**Step 2: Add visual indicator for auto-start**

Update `_statusMessage` initialization (line 16):

```dart
  String _statusMessage = 'Test Agora (Auto)';
```

**Step 3: Verify compiles**

Run:
```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && /Users/ellaai/dev/flutter/bin/flutter analyze lib/pages/home/widgets/agora_test_button.dart"
```

Expected: No errors

**Step 4: Commit**

```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git add lib/pages/home/widgets/agora_test_button.dart && git commit -m 'feat(agora): add auto-start for autonomous testing

Auto-starts Agora test 2 seconds after widget initialization.
Enables hands-free testing with audio playback scripts.
Visual indicator shows (Auto) in button text.'"
```

---

## Task 4: Build and Deploy to iPhone

**Step 1: Kill existing flutter process**

```bash
ssh ellaai@100.76.138.56 "pkill -f 'flutter run' || true"
```

**Step 2: Build and deploy via tmux**

```bash
ssh ellaai@100.76.138.56 "tmux send-keys -t ios-build C-c" && sleep 1
ssh ellaai@100.76.138.56 "tmux send-keys -t ios-build 'cd /Users/ellaai/dev/omi/app && /Users/ellaai/dev/flutter/bin/flutter run --flavor dev -d 00008110-001A452C1A12401E' Enter"
```

**Step 3: Wait for build to complete**

```bash
sleep 120
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep -E 'Launching|Xcode build|error' | tail -10"
```

Expected: "Launching lib/main.dart on Greg's iPhone"

**Step 4: Verify app running**

```bash
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep -E 'flutter:|AgoraTest' | tail -20"
```

Expected: See "[AgoraTest] Auto-starting for autonomous testing"

---

## Task 5: Verify Backend Bot Starts

**Files:**
- Check: `/opt/ella-voice-bot/api.py` (Mac Mini)
- Check: `/opt/ella-voice-bot/bot.py` (Mac Mini)

**Step 1: Check API service is running**

```bash
ssh ellaai@100.76.138.56 "curl -s http://localhost:8090/health || echo 'API not responding'"
```

Expected: `{"status":"healthy"}` or similar

**Step 2: Monitor bot.py spawn**

Check if bot process starts when Agora connects:

```bash
ssh ellaai@100.76.138.56 "ps aux | grep -E 'bot.py|ella-voice' | grep -v grep"
```

Expected: See bot.py process OR uvicorn api:app process

**Step 3: Check backend logs**

```bash
ssh ellaai@100.76.138.56 "tail -50 /tmp/ella-voice-bot.log 2>/dev/null || journalctl -u ella-voice-bot -n 50 --no-pager 2>/dev/null || echo 'No backend logs found'"
```

Expected: See "Channel created" or "Bot started" messages

---

## Task 6: Test End-to-End with Audio Playback

**Step 1: Wait for auto-start**

```bash
sleep 5
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep 'Successfully joined channel' | tail -5"
```

Expected: See "[AgoraService] Successfully joined channel"

**Step 2: Play test speech from Mac Mini**

```bash
ssh ellaai@100.76.138.56 "say 'Hello Ella, this is an autonomous test. Can you hear me?'"
```

**Step 3: Monitor iPhone logs for audio detection**

```bash
sleep 5
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep -E 'Audio volume|remote audio|local audio' | tail -20"
```

Expected: See audio volume indicators or "First remote audio frame"

**Step 4: Check for transcription in backend**

```bash
ssh ellaai@100.76.138.56 "tail -30 /tmp/ella-voice-bot.log 2>/dev/null | grep -i 'transcript\|deepgram\|hello'"
```

Expected: See "Hello Ella" or transcription attempts

**Step 5: Capture screenshot**

```bash
ssh ellaai@100.76.138.56 "idevicescreenshot /tmp/agora-test-$(date +%s).png 2>&1"
```

---

## Task 7: Debug If No Audio Detected

**If Step 6 shows no audio frames:**

**Check 1: Verify microphone permissions**

```bash
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep -i 'permission\|microphone' | tail -10"
```

**Check 2: Verify channel options**

Grep for publishMicrophoneTrack in logs:

```bash
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p -S -200 | grep 'publishMicrophoneTrack'"
```

Expected: `"publishMicrophoneTrack":true`

**Check 3: Verify bot joined**

```bash
ssh ellaai@100.76.138.56 "tmux capture-pane -t ios-build -p | grep 'Remote user joined' | tail -5"
```

Expected: "[AgoraService] Remote user joined: [bot_uid]"

**Check 4: Test with louder audio**

```bash
ssh ellaai@100.76.138.56 "say -v Samantha -r 150 'Testing. Testing. One. Two. Three. This is a very loud test message.'"
```

---

## Task 8: Document Results

**Files:**
- Create: `/Users/ellaai/dev/omi/app/docs/testing/agora-autonomous-test-results.md`

**Step 1: Create results document**

```markdown
# Agora Autonomous Test Results

**Date:** 2026-02-20  
**Branch:** feature/agora-parallel-integration  
**Test Environment:** Mac Mini + iPhone 13

## Test Configuration

- **Auto-start:** Enabled (2s delay)
- **Audio source:** Mac Mini `say` command
- **Monitoring:** iOS device logs via `flutter run`

## Results

### Connection Status
- [x] Agora SDK initialized: YES/NO
- [x] Channel joined: YES/NO
- [x] Bot user joined: YES/NO

### Audio Transmission
- [ ] Local audio frames sent: YES/NO
- [ ] Remote audio frames received: YES/NO  
- [ ] Audio volume detected: YES/NO

### Voice Pipeline
- [ ] Backend transcription: YES/NO
- [ ] Ella response generated: YES/NO
- [ ] TTS audio received: YES/NO

## Issues Found

[List any errors, missing logs, or unexpected behavior]

## Screenshots

- `/tmp/agora-test-*.png`

## Next Steps

[What needs to be fixed or investigated]
```

**Step 2: Save document**

```bash
ssh ellaai@100.76.138.56 "cat > /Users/ellaai/dev/omi/app/docs/testing/agora-autonomous-test-results.md << 'EOF'
[paste template above]
EOF"
```

**Step 3: Fill in actual results**

Update checkboxes and sections based on test observations

**Step 4: Commit results**

```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git add docs/testing/agora-autonomous-test-results.md && git commit -m 'docs: add Agora autonomous test results

Test conducted while user asleep. Documents connection status,
audio transmission, and voice pipeline verification.'"
```

---

## Success Criteria

**Minimum (Connection):**
- ✅ Agora connects without ERR_INVALID_APP_ID
- ✅ Bot joins channel
- ✅ No crashes

**Target (Audio):**
- 🎯 iPhone sends audio to channel (volume indication logs)
- 🎯 iPhone receives audio from bot (remote audio frame logs)
- 🎯 Backend transcribes audio (Deepgram logs)

**Stretch (Full Pipeline):**
- 🚀 End-to-end: Mac Mini audio → transcription → Ella response → iPhone speaker
- 🚀 Autonomous testing loop works
- 🚀 User wakes up to working Agora voice

## Rollback Plan

If implementation causes issues:

```bash
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git reset --hard c763f8d26"
ssh ellaai@100.76.138.56 "cd /Users/ellaai/dev/omi/app && git checkout fec82600f"
```

This reverts to working WebSocket voice (no Agora).

---

**Implementation Notes:**
- User is asleep - work autonomously
- Prioritize making Agora audio work over auto-start feature
- Document everything for morning review
- If stuck on audio streaming, may need Agora SDK documentation or backend bot.py debugging
