# Guardian Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build Guardian Mode MVP - a background audio playback system that keeps the app active and allows AI voice clips to be injected via bone conduction headset.

**Architecture:** Streaming music pattern using AVAudioSession(.playback) + AVQueuePlayer. Silent 100ms audio loop keeps session alive in background. Test audio clips (on-device TTS) injected every 30 seconds via MethodChannel bridge between Flutter and iOS.

**Tech Stack:** Swift (iOS AVFoundation), Flutter (Dart), MethodChannel, AVSpeechSynthesizer

**Related:** Issue #93, PRD at `docs/plans/2026-02-23-guardian-mode-prd.md`

---

## Task 1: Generate Silent Audio Loop File

**Files:**
- Create: `ios/Runner/GuardianMode/silence_100ms.wav`

**Step 1: Create GuardianMode directory**

```bash
mkdir -p ios/Runner/GuardianMode
```

**Step 2: Generate silence_100ms.wav using ffmpeg**

```bash
ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 0.1 -acodec pcm_s16le ios/Runner/GuardianMode/silence_100ms.wav
```

Expected: Creates 100ms silent WAV file (16kHz, mono, 16-bit PCM)

**Step 3: Verify file properties**

```bash
ffprobe ios/Runner/GuardianMode/silence_100ms.wav
```

Expected output should show:
- Duration: 0.10 seconds
- Sample rate: 16000 Hz
- Channels: 1 (mono)
- Format: WAV

**Step 4: Add to Xcode project**

Open Xcode → Right-click Runner folder → Add Files to "Runner"
- Select `ios/Runner/GuardianMode/silence_100ms.wav`
- Check "Copy items if needed"
- Ensure "Add to targets: Runner" is checked

**Step 5: Commit**

```bash
git add ios/Runner/GuardianMode/silence_100ms.wav
git commit -m "feat(guardian): add silent audio loop file for background playback

- 100ms silent WAV (16kHz, mono, PCM)
- Keeps AVAudioSession alive in background
- Part of Guardian Mode POC (#93)
"
```

---

## Task 2: Create GuardianModeManager (iOS Native)

**Files:**
- Create: `ios/Runner/GuardianMode/GuardianModeManager.swift`

**Step 1: Write GuardianModeManager.swift**

```swift
import Foundation
import AVFoundation

class GuardianModeManager: NSObject {
    static let shared = GuardianModeManager()
    
    private var audioPlayer: AVQueuePlayer?
    private var playerLooper: AVPlayerLooper?
    private var isActive = false
    
    private override init() {
        super.init()
    }
    
    /// Start Guardian Mode - begins silent audio loop
    func start() throws {
        guard !isActive else {
            print("GuardianMode: Already active, ignoring start()")
            return
        }
        
        // Configure audio session for playback only
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try audioSession.setActive(true)
        
        // Load silent audio file
        guard let silenceURL = Bundle.main.url(forResource: "silence_100ms", withExtension: "wav", subdirectory: "GuardianMode") else {
            throw NSError(domain: "GuardianMode", code: 1, userInfo: [NSLocalizedDescriptionKey: "Silent audio file not found"])
        }
        
        let playerItem = AVPlayerItem(url: silenceURL)
        let queuePlayer = AVQueuePlayer(playerItem: playerItem)
        
        // Loop the silent audio infinitely
        let looper = AVPlayerLooper(player: queuePlayer, templateItem: playerItem)
        
        self.audioPlayer = queuePlayer
        self.playerLooper = looper
        
        // Start playback
        queuePlayer.play()
        isActive = true
        
        print("GuardianMode: Started - silent loop playing")
    }
    
    /// Stop Guardian Mode - stops audio and deactivates session
    func stop() {
        guard isActive else {
            print("GuardianMode: Already stopped, ignoring stop()")
            return
        }
        
        audioPlayer?.pause()
        audioPlayer = nil
        playerLooper = nil
        
        do {
            try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            print("GuardianMode: Error deactivating audio session: \(error)")
        }
        
        isActive = false
        print("GuardianMode: Stopped")
    }
    
    /// Inject an audio clip into the playback queue
    /// - Parameter audioURL: URL to audio file to play
    func injectAudioClip(audioURL: URL) {
        guard isActive, let player = audioPlayer else {
            print("GuardianMode: Cannot inject clip - not active")
            return
        }
        
        let clipItem = AVPlayerItem(url: audioURL)
        player.insert(clipItem, after: player.currentItem)
        
        print("GuardianMode: Injected audio clip: \(audioURL.lastPathComponent)")
    }
    
    /// Get current Guardian Mode state
    func getState() -> String {
        return isActive ? "active" : "idle"
    }
}
```

**Step 2: Add file to Xcode project**

Open Xcode → Runner group → Right-click GuardianMode folder → New File → Swift File
- Name: GuardianModeManager.swift
- Add to targets: Runner

**Step 3: Build to verify Swift compiles**

```bash
cd ios
xcodebuild -workspace Runner.xcworkspace -scheme dev -configuration Debug build
```

Expected: Build succeeds with no errors

**Step 4: Commit**

```bash
git add ios/Runner/GuardianMode/GuardianModeManager.swift
git commit -m "feat(guardian): add iOS native GuardianModeManager

- Manages AVAudioSession for background playback
- Loops silent audio to keep session alive
- Supports audio clip injection for test messages
- Part of Guardian Mode POC (#93)
"
```

---

## Task 3: Add MethodChannel Bridge in AppDelegate

**Files:**
- Modify: `ios/Runner/AppDelegate.swift`

**Step 1: Import GuardianModeManager at top of AppDelegate.swift**

Find the imports section and add:

```swift
import TwilioVoice  // existing
// Add below:
```

(GuardianModeManager will be available via same module since it's in Runner target)

**Step 2: Add MethodChannel property**

Find the existing properties section (around line 20-35) and add:

```swift
private var guardianModeChannel: FlutterMethodChannel?
```

**Step 3: Register MethodChannel in didFinishLaunchingWithOptions**

Find the section where other MethodChannels are registered (around line 70-90) and add:

```swift
// Guardian Mode MethodChannel
guardianModeChannel = FlutterMethodChannel(
    name: "com.ellaaicare.omi/guardian_mode",
    binaryMessenger: controller.binaryMessenger
)
guardianModeChannel?.setMethodCallHandler(handleGuardianModeMethodCall)
print("AppDelegate: Guardian Mode MethodChannel registered")
```

**Step 4: Add MethodChannel handler method**

Add this method at the end of the AppDelegate class (before the final closing brace):

```swift
// MARK: - Guardian Mode MethodChannel Handler

private func handleGuardianModeMethodCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "start":
        do {
            try GuardianModeManager.shared.start()
            result(["status": "active"])
        } catch {
            result(FlutterError(
                code: "START_FAILED",
                message: "Failed to start Guardian Mode",
                details: error.localizedDescription
            ))
        }
        
    case "stop":
        GuardianModeManager.shared.stop()
        result(["status": "idle"])
        
    case "injectAudioClip":
        guard let args = call.arguments as? [String: Any],
              let audioPath = args["audioPath"] as? String else {
            result(FlutterError(
                code: "INVALID_ARGS",
                message: "Missing audioPath parameter",
                details: nil
            ))
            return
        }
        
        let audioURL = URL(fileURLWithPath: audioPath)
        GuardianModeManager.shared.injectAudioClip(audioURL: audioURL)
        result(nil)
        
    case "getState":
        let state = GuardianModeManager.shared.getState()
        result(["status": state])
        
    default:
        result(FlutterMethodNotImplemented)
    }
}
```

**Step 5: Build and verify**

```bash
cd ios
xcodebuild -workspace Runner.xcworkspace -scheme dev -configuration Debug build
```

Expected: Build succeeds

**Step 6: Commit**

```bash
git add ios/Runner/AppDelegate.swift
git commit -m "feat(guardian): add MethodChannel bridge for Guardian Mode

- Registered MethodChannel: com.ellaaicare.omi/guardian_mode
- Methods: start, stop, injectAudioClip, getState
- Part of Guardian Mode POC (#93)
"
```

---

## Task 4: Create Guardian Mode Service (Flutter)

**Files:**
- Create: `lib/ella/services/guardian_mode_service.dart`

**Step 1: Write guardian_mode_service.dart**

```dart
import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:flutter_tts/flutter_tts.dart';

enum GuardianModeState {
  idle,
  active,
  error,
}

class GuardianModeService {
  static final GuardianModeService _instance = GuardianModeService._internal();
  factory GuardianModeService() => _instance;
  GuardianModeService._internal();

  static const MethodChannel _channel = MethodChannel('com.ellaaicare.omi/guardian_mode');

  final _stateController = StreamController<GuardianModeState>.broadcast();
  Stream<GuardianModeState> get stateStream => _stateController.stream;

  GuardianModeState _currentState = GuardianModeState.idle;
  GuardianModeState get currentState => _currentState;

  Timer? _testAudioTimer;
  int _testClipCounter = 0;
  final FlutterTts _tts = FlutterTts();

  /// Start Guardian Mode
  Future<void> start() async {
    if (_currentState == GuardianModeState.active) {
      print('GuardianMode: Already active');
      return;
    }

    try {
      // Call iOS native to start silent loop
      final result = await _channel.invokeMethod('start');
      print('GuardianMode: Native start result: $result');

      _updateState(GuardianModeState.active);

      // Start test audio injection timer (every 30 seconds)
      _startTestAudioTimer();
    } catch (e) {
      print('GuardianMode: Error starting: $e');
      _updateState(GuardianModeState.error);
      rethrow;
    }
  }

  /// Stop Guardian Mode
  Future<void> stop() async {
    if (_currentState == GuardianModeState.idle) {
      print('GuardianMode: Already stopped');
      return;
    }

    try {
      // Stop test audio timer
      _stopTestAudioTimer();

      // Call iOS native to stop
      await _channel.invokeMethod('stop');
      print('GuardianMode: Stopped');

      _updateState(GuardianModeState.idle);
    } catch (e) {
      print('GuardianMode: Error stopping: $e');
      _updateState(GuardianModeState.error);
    }
  }

  /// Start timer to inject test audio clips
  void _startTestAudioTimer() {
    _testClipCounter = 0;
    _testAudioTimer?.cancel();

    _testAudioTimer = Timer.periodic(const Duration(seconds: 30), (timer) async {
      _testClipCounter++;
      await _generateAndInjectTestClip(_testClipCounter);
    });

    // Inject first clip immediately
    _generateAndInjectTestClip(0);
  }

  /// Stop test audio timer
  void _stopTestAudioTimer() {
    _testAudioTimer?.cancel();
    _testAudioTimer = null;
    _testClipCounter = 0;
  }

  /// Generate test audio clip using TTS and inject it
  Future<void> _generateAndInjectTestClip(int clipNumber) async {
    try {
      final text = 'Guardian test number $clipNumber';
      print('GuardianMode: Generating test clip: $text');

      // Configure TTS
      await _tts.setLanguage('en-US');
      await _tts.setSpeechRate(0.5);
      await _tts.setVolume(1.0);

      // Generate audio file
      final tempDir = await getTemporaryDirectory();
      final audioPath = '${tempDir.path}/guardian_test_$clipNumber.wav';

      await _tts.synthesizeToFile(text, audioPath);
      print('GuardianMode: Generated TTS file: $audioPath');

      // Inject into native audio queue
      await _channel.invokeMethod('injectAudioClip', {'audioPath': audioPath});
      print('GuardianMode: Injected clip $clipNumber');
    } catch (e) {
      print('GuardianMode: Error generating test clip: $e');
    }
  }

  /// Update state and notify listeners
  void _updateState(GuardianModeState newState) {
    _currentState = newState;
    _stateController.add(newState);
  }

  /// Dispose resources
  void dispose() {
    _stopTestAudioTimer();
    _stateController.close();
  }
}
```

**Step 2: Verify dependencies exist in pubspec.yaml**

```bash
grep -E "flutter_tts|path_provider" pubspec.yaml
```

Expected: Both packages should already be present

**Step 3: Run Flutter build to verify Dart compiles**

```bash
flutter build ios --no-codesign --debug
```

Expected: Build succeeds with no Dart compilation errors

**Step 4: Commit**

```bash
git add lib/ella/services/guardian_mode_service.dart
git commit -m "feat(guardian): add Flutter service for Guardian Mode

- Manages Guardian Mode state (idle/active/error)
- Bridges to iOS native via MethodChannel
- Generates test TTS clips every 30 seconds
- Injects clips into native audio queue
- Part of Guardian Mode POC (#93)
"
```

---

## Task 5: Create Guardian Mode Button Widget

**Files:**
- Create: `lib/ella/widgets/guardian_mode_button.dart`

**Step 1: Write guardian_mode_button.dart**

```dart
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/guardian_mode_service.dart';

class GuardianModeButton extends StatefulWidget {
  const GuardianModeButton({super.key});

  @override
  State<GuardianModeButton> createState() => _GuardianModeButtonState();
}

class _GuardianModeButtonState extends State<GuardianModeButton> with SingleTickerProviderStateMixin {
  final _guardianService = GuardianModeService();
  late AnimationController _pulseController;

  @override
  void initState() {
    super.initState();
    
    // Pulse animation for active state
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    )..repeat(reverse: true);

    // Listen to state changes
    _guardianService.stateStream.listen((state) {
      if (mounted) {
        setState(() {});
        if (state == GuardianModeState.active) {
          _pulseController.repeat(reverse: true);
        } else {
          _pulseController.stop();
        }
      }
    });
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  Future<void> _onTap() async {
    HapticFeedback.mediumImpact();

    final currentState = _guardianService.currentState;
    if (currentState == GuardianModeState.idle || currentState == GuardianModeState.error) {
      await _guardianService.start();
    } else {
      await _guardianService.stop();
    }
  }

  Color _getButtonColor() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return Colors.grey.shade700;
      case GuardianModeState.active:
        return EllaColors.primary;
      case GuardianModeState.error:
        return Colors.red.shade600;
    }
  }

  IconData _getIcon() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return Icons.shield_outlined;
      case GuardianModeState.active:
        return Icons.shield;
      case GuardianModeState.error:
        return Icons.warning;
    }
  }

  String _getStatusText() {
    switch (_guardianService.currentState) {
      case GuardianModeState.idle:
        return 'Guardian Mode OFF';
      case GuardianModeState.active:
        return 'Guardian Mode ON';
      case GuardianModeState.error:
        return 'Guardian Mode Error';
    }
  }

  @override
  Widget build(BuildContext context) {
    final isActive = _guardianService.currentState == GuardianModeState.active;

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        // Button
        GestureDetector(
          onTap: _onTap,
          child: AnimatedBuilder(
            animation: _pulseController,
            builder: (context, child) {
              final pulseValue = isActive ? _pulseController.value : 0.0;
              final glowRadius = 20.0 + (pulseValue * 10.0);

              return Container(
                width: 80,
                height: 80,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: _getButtonColor(),
                  boxShadow: isActive
                      ? [
                          BoxShadow(
                            color: EllaColors.primary.withOpacity(0.6),
                            blurRadius: glowRadius,
                            spreadRadius: glowRadius / 4,
                          ),
                        ]
                      : null,
                ),
                child: Icon(
                  _getIcon(),
                  color: Colors.white,
                  size: 40,
                ),
              );
            },
          ),
        ),
        const SizedBox(height: 12),
        // Status text
        Text(
          _getStatusText(),
          style: const TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.w600,
            color: EllaColors.textSecondary,
          ),
        ),
      ],
    );
  }
}
```

**Step 2: Build to verify widget compiles**

```bash
flutter build ios --no-codesign --debug
```

Expected: Build succeeds

**Step 3: Commit**

```bash
git add lib/ella/widgets/guardian_mode_button.dart
git commit -m "feat(guardian): add Guardian Mode toggle button widget

- Visual states: OFF (gray), ON (pulsing green), ERROR (red)
- Haptic feedback on tap
- Animated glow effect when active
- Part of Guardian Mode POC (#93)
"
```

---

## Task 6: Integrate Button into Home Screen

**Files:**
- Modify: `lib/pages/home/page.dart` (or appropriate Ella home screen file)

**Step 1: Find the correct home screen file**

```bash
find lib -name "*home*.dart" | grep -E "ella|page" | head -5
```

Expected: Should show the main Ella home screen file path

**Step 2: Import GuardianModeButton**

At the top of the home screen file, add:

```dart
import 'package:omi/ella/widgets/guardian_mode_button.dart';
```

**Step 3: Add button to UI**

Find the appropriate location in the widget tree (likely near VoiceChat orb or in a row/column of action buttons) and add:

```dart
const GuardianModeButton(),
```

**Step 4: Build and install to device**

```bash
flutter build ios --flavor dev --debug
xcrun devicectl device install app --device 00008110-001A452C1A12401E build/ios/iphoneos/Runner.app
```

Expected: App installs successfully

**Step 5: Manual verification**

1. Launch app on iPhone
2. Verify Guardian Mode button appears
3. Tap button → should show ON state with green glow
4. Check console logs for "GuardianMode: Started"

**Step 6: Commit**

```bash
git add lib/pages/home/page.dart
git commit -m "feat(guardian): integrate Guardian Mode button into home screen

- Added GuardianModeButton to main UI
- Positioned near VoiceChat feature
- Part of Guardian Mode POC (#93)
"
```

---

## Task 7: Hot Path Testing (POC Validation)

**Files:**
- None (testing only)

**Step 1: Phase 1 - Basic Audio Loop (5 min)**

**Test steps:**
1. Launch app, tap "Start Guardian Mode"
2. Verify button changes to ON state (pulsing green)
3. Check Xcode console for: `GuardianMode: Started - silent loop playing`
4. Lock screen (press power button)
5. Wait 3 minutes
6. Open iOS Control Center
7. Verify app shows as "playing audio" (music icon)
8. Unlock and check app → verify still in ON state

**Expected results:**
- ✅ Audio session survives 3+ minutes locked
- ✅ iOS recognizes app as active audio player
- ✅ No suspension or termination

**If failed:** Check `GuardianModeManager.swift` AVAudioSession configuration

**Step 2: Phase 2 - Test Audio Injection (10 min)**

**Prerequisites:**
- Connect bone conduction headset via Bluetooth
- Verify Bluetooth audio routing in iOS Settings

**Test steps:**
1. Start Guardian Mode
2. Listen for TTS clip: "Guardian test number 0" (plays immediately)
3. Wait 30 seconds → should hear "Guardian test number 1"
4. Background app (swipe home)
5. Continue listening for 10 minutes (20 clips total)
6. Check Xcode console for injection logs

**Expected results:**
- ✅ All 20 clips play successfully
- ✅ Audio routes to Bluetooth (not phone speaker)
- ✅ No skipping or clipping
- ✅ Loop continues uninterrupted
- ✅ Console shows: `GuardianMode: Injected clip N` for each

**If failed:** Check `guardian_mode_service.dart` TTS generation or `GuardianModeManager.swift` injection logic

**Step 3: Phase 3 - Background Endurance (15 min)**

**Prerequisites:**
- Fully charge phone (or note starting battery %)
- Close all other apps

**Test steps:**
1. Start Guardian Mode
2. Note starting battery %
3. Lock screen and leave phone idle
4. Monitor for 15 minutes (set timer)
5. After 15 minutes:
   - Note ending battery %
   - Check app still running
   - Verify ~30 clips played (console or by listening)

**Expected results:**
- ✅ Audio session survives 15+ minutes
- ✅ All ~30 test clips played
- ✅ Battery drain < 5% over 15 minutes
- ✅ No iOS suspension

**If failed:**
- Battery >5% drain → Check AVAudioSession options, consider `.mixWithOthers`
- Session dies → Verify AVPlayerLooper is properly retaining playerItem
- Clips missing → Check timer is running and TTS generation succeeds

**Step 4: Document test results**

Create test report:

```bash
cat > docs/plans/2026-02-23-guardian-mode-test-results.md << 'TESTEOF'
# Guardian Mode POC Test Results

**Date:** $(date +%Y-%m-%d)
**Tester:** [Your name]
**Device:** iPhone 13 (iOS 26.3)
**Headset:** [Bone conduction model]

## Phase 1: Basic Audio Loop ✅/❌
- Audio session survived 3+ min locked: [YES/NO]
- iOS recognized as audio player: [YES/NO]
- Notes: [Any observations]

## Phase 2: Test Audio Injection ✅/❌
- All 20 clips played: [YES/NO]
- Routed to Bluetooth: [YES/NO]
- No interruptions: [YES/NO]
- Notes: [Any observations]

## Phase 3: Background Endurance ✅/❌
- Session survived 15+ min: [YES/NO]
- Clips played: [X/30]
- Battery drain: [X%]
- Notes: [Any observations]

## Overall Result: [PASS/FAIL]

## Next Steps:
- [If PASS: List post-MVP features to implement]
- [If FAIL: List issues to debug and fix]
TESTEOF
```

**Step 5: Commit test results**

```bash
git add docs/plans/2026-02-23-guardian-mode-test-results.md
git commit -m "test(guardian): add POC test results for Guardian Mode

- Documented Phase 1, 2, 3 test outcomes
- Battery impact measurement
- Part of Guardian Mode POC (#93)
"
```

---

## Task 8: Update GitHub Issue with Results

**Files:**
- None (GitHub update)

**Step 1: Add test results comment to issue**

```bash
GH_TOKEN="***REMOVED_GITHUB_PAT***" gh issue comment 93 --repo ellaaicare/ella-ai --body "## POC Test Results

**Status:** [✅ PASS / ❌ FAIL]

### Phase 1: Basic Audio Loop
- Audio session survived 3+ min locked: [YES/NO]
- iOS recognized as audio player: [YES/NO]

### Phase 2: Test Audio Injection  
- All 20 clips played: [YES/NO]
- Routed to Bluetooth: [YES/NO]

### Phase 3: Background Endurance
- Session survived 15+ min: [YES/NO]
- Battery drain: [X%]

### Conclusion
[Summary of results and next steps]

**Test report:** \`docs/plans/2026-02-23-guardian-mode-test-results.md\`
"
```

**Step 2: Label issue based on results**

If PASS:
```bash
GH_TOKEN="..." gh issue edit 93 --repo ellaaicare/ella-ai --add-label "tested"
```

If FAIL:
```bash
GH_TOKEN="..." gh issue edit 93 --repo ellaaicare/ella-ai --add-label "bug"
```

---

## Success Criteria Checklist

After completing all tasks, verify:

- [ ] Guardian Mode starts with single button tap
- [ ] Silent audio loop keeps app alive in background for 15+ minutes
- [ ] Test audio clips (TTS) play every 30 seconds
- [ ] Audio routes to Bluetooth bone conduction headset
- [ ] Battery drain < 5% over 15 minutes
- [ ] Visual feedback shows ON/OFF/ERROR states clearly
- [ ] No crashes or iOS suspension
- [ ] All code committed with clear messages
- [ ] Test results documented

**If all checked:** POC is successful! Ready to discuss post-MVP features (Omi integration, AI escalation logic, auto-start, etc.)

**If any unchecked:** Debug failed criteria, iterate on implementation, re-test.

---

## Notes for Engineer

### Key Testing Tips
1. **Always test with screen locked** - that's the real background scenario
2. **Monitor Xcode console** - logs are your friend for debugging
3. **Use real Bluetooth headset** - speaker routing is different behavior
4. **Check iOS Control Center** - verify app shows as "playing audio"
5. **Battery drain baseline** - test with app inactive first to establish baseline

### Common Issues & Fixes

| Issue | Likely Cause | Fix |
|-------|--------------|-----|
| Session dies after 3-5 min | AVPlayerLooper not working | Verify looper retains templateItem |
| No audio through Bluetooth | Route not configured | Check AVAudioSession options |
| TTS files don't generate | flutter_tts config issue | Add await to synthesizeToFile |
| Clips don't inject | MethodChannel error | Check audioPath is absolute path |
| High battery drain | Audio settings | Try .mixWithOthers option |

### DRY Principles Applied
- Singleton pattern for GuardianModeService (no duplicate instances)
- Reused FlutterTts instance across all clips
- Centralized state management via Stream

### YAGNI Applied
- No auto-start/persistence (not needed for POC)
- No scheduling (not needed for POC)
- No rich UI animations (basic pulse is enough)
- No error recovery/retry (can add post-MVP)

### TDD Approach
This implementation is hardware-dependent (audio I/O) so traditional unit tests are limited. Instead:
- **Manual test plan** serves as acceptance tests
- **Console logs** provide verification at each step
- **Incremental commits** allow rollback if issues arise

---

**END OF IMPLEMENTATION PLAN**
