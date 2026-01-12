# Upstream Pull Instructions

**For**: Session performing the upstream pull
**Date**: January 11, 2026
**Prepared by**: iOS Developer

---

## Overview

This document provides step-by-step instructions for pulling fresh from upstream OMI and re-applying Ella extensions.

**IMPORTANT**: Before proceeding, ensure you understand:
1. This will replace most of the current fork's code with upstream
2. Ella extensions are preserved in `ella_extensions_backup/`
3. The reapply script will restore Ella code after pull

---

## Pre-Pull Checklist

- [ ] All local changes committed and pushed
- [ ] `ella_extensions_backup/` folder exists and is up to date
- [ ] `scripts/reapply_ella_extensions.sh` exists and is executable
- [ ] You've read the `lib/ella/ELLA_EXTENSIONS_README.md`

Verify backup exists:
```bash
cd /Users/greg/repos/omi/app
ls -la ella_extensions_backup/
# Should see: lib/ and ios/ folders
```

---

## Step 1: Backup Current State

Even though we have `ella_extensions_backup/`, create an additional safety backup:

```bash
cd /Users/greg/repos/omi/app

# Create timestamped backup branch
git checkout -b backup/pre-upstream-pull-$(date +%Y%m%d)
git push origin backup/pre-upstream-pull-$(date +%Y%m%d)

# Return to main
git checkout main
```

---

## Step 2: Add Upstream Remote (if not already)

```bash
# Check existing remotes
git remote -v

# If upstream doesn't exist, add it
git remote add upstream https://github.com/BasedHardware/omi.git

# Verify
git remote -v
# Should show:
# origin    https://github.com/ellaaicare/omi.git (fetch)
# origin    https://github.com/ellaaicare/omi.git (push)
# upstream  https://github.com/BasedHardware/omi.git (fetch)
# upstream  https://github.com/BasedHardware/omi.git (push)
```

---

## Step 3: Fetch Upstream

```bash
git fetch upstream
```

---

## Step 4: Create Fresh Branch from Upstream

**Option A: Replace main entirely (recommended for clean start)**

```bash
# Create new branch from upstream
git checkout -b feature/ella-v2-fresh upstream/main

# This branch now has pure upstream code
```

**Option B: Merge upstream into existing branch**

```bash
# Stay on main
git checkout main

# Merge upstream (may have conflicts)
git merge upstream/main

# Resolve any conflicts
# Note: Most Ella code will be in conflict - let upstream win
# since we'll re-apply our extensions
```

---

## Step 5: Re-apply Ella Extensions

Run the reapply script:

```bash
cd /Users/greg/repos/omi/app
./scripts/reapply_ella_extensions.sh
```

Expected output:
```
============================================
  Ella Extensions Re-application Script
============================================

Step 1: Checking lib/ella/...
  lib/ella/ not found, restoring from backup...
  ✅ lib/ella/ restored

Step 2: Checking ios/Runner/Ella/...
  ios/Runner/Ella/ not found, restoring from backup...
  ✅ ios/Runner/Ella/ restored

Step 3: Checking pubspec.yaml dependencies...
  ⚠️  Missing dependencies: just_audio web_socket_channel
  ...

Step 4: Checking main.dart integration...
  ⚠️  EllaExtensions NOT found in main.dart
  ...

Step 5: Checking iOS plugin registration...
  ⚠️  Ella plugins NOT registered in AppDelegate.swift
  ...
```

---

## Step 6: Add Missing Dependencies

If the script reports missing dependencies, add them to `pubspec.yaml`:

```yaml
dependencies:
  # ... existing deps ...

  # Ella Extensions Dependencies
  just_audio: ^0.9.36
  web_socket_channel: ^2.4.0
  shared_preferences: ^2.2.2
  path_provider: ^2.1.2
  http: ^1.2.0
```

Then run:
```bash
flutter pub get
```

---

## Step 7: Integrate Ella Extensions in main.dart

Find the main initialization in `lib/main.dart` and add:

```dart
// At the top, add import
import 'package:omi/ella/extensions.dart';

// In main() after OMI initialization, add:
await EllaExtensions().initialize();
```

**Example integration point** (location may vary by upstream version):

```dart
void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // ... existing OMI initialization ...
  // await SharedPreferencesUtil().init();
  // await Firebase.initializeApp(...);
  // etc.

  // ADD THIS: Initialize Ella extensions
  await EllaExtensions().initialize();

  runApp(const MyApp());
}
```

---

## Step 8: Register iOS Plugins

Edit `ios/Runner/AppDelegate.swift`:

```swift
import Flutter
import UIKit
// ... other imports ...

@UIApplicationMain
@objc class AppDelegate: FlutterAppDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {

    // ADD THIS: Register Ella plugins
    if let registrar = self.registrar(forPlugin: "WakeWordPlugin") {
      WakeWordPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "VoiceV2VPlugin") {
      VoiceV2VPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "NativeTtsPlugin") {
      NativeTtsPlugin.register(with: registrar)
    }
    if let registrar = self.registrar(forPlugin: "AudioPushPlugin") {
      AudioPushPlugin.register(with: registrar)
    }

    // ... existing code ...

    GeneratedPluginRegistrant.register(with: self)
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }
}
```

---

## Step 9: Configure Environment

Create/update `.dev.env` with Ella backend:

```bash
API_BASE_URL=https://api.ella-ai-care.com/
```

---

## Step 10: Build and Test

```bash
# Clean build
flutter clean
cd ios && pod install && cd ..
flutter pub get

# Run
flutter run --flavor dev
```

---

## Step 11: Verify Ella Extensions

In the running app, check that extensions initialized:

```bash
# Check logs
flutter logs | grep -i "EllaExtensions\|WakeWord\|VoiceV2V\|TTS"
```

Expected:
```
[EllaExtensions] Initializing Ella extensions...
[EllaExtensions] Initializing WakeWord v1.0.0...
[EllaExtensions] WakeWord initialized
[EllaExtensions] Initializing VoiceV2V v1.0.0...
...
[EllaExtensions] All extensions initialized
```

---

## Step 12: Connect Transcript Forwarding

Find where OMI processes transcripts and add forwarding to Ella:

```dart
// In the transcript processing code (location varies by upstream)
// Look for where TranscriptSegment is received

void onTranscriptReceived(TranscriptSegment segment) {
  // ... existing OMI processing ...

  // ADD: Forward to Ella for wake word detection
  EllaExtensions().onTranscriptReceived(segment.text);
}
```

---

## Step 13: Commit Changes

```bash
git add .
git commit -m "feat: integrate Ella extensions with fresh upstream

- Added lib/ella/ plugin architecture
- Added ios/Runner/Ella/ native plugins
- Integrated EllaExtensions in main.dart
- Registered iOS plugins in AppDelegate
- Configured for Ella backend

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"

git push origin feature/ella-v2-fresh
```

---

## Troubleshooting

### Build Errors

```bash
# Full clean rebuild
flutter clean
rm -rf ios/Pods ios/Podfile.lock
cd ios && pod install && cd ..
flutter pub get
flutter run --flavor dev
```

### Missing Ella Folder

```bash
# Re-run reapply script
./scripts/reapply_ella_extensions.sh
```

### iOS Plugin Not Found

Check that:
1. Swift files are in `ios/Runner/Ella/`
2. Plugins are registered in `AppDelegate.swift`
3. Run `pod install` after changes

### Wake Word Not Detecting

The wake word plugin is a skeleton - needs porting from standalone Ella app.
See `lib/ella/plugins/wake_word/wake_word_plugin.dart` TODO comments.

---

## Post-Pull Tasks

After successful upstream pull:

1. **Test basic OMI functionality** - Conversations, memories, BLE
2. **Test Ella extensions** - TTS plugin should work, others are skeletons
3. **Port from standalone Ella app** - Wake word and Voice V2V need implementation
4. **Update backup** - Copy updated lib/ella to ella_extensions_backup

Update backup:
```bash
rm -rf ella_extensions_backup/lib/ella
rm -rf ella_extensions_backup/ios/Ella
cp -r lib/ella ella_extensions_backup/lib/
cp -r ios/Runner/Ella ella_extensions_backup/ios/
git add ella_extensions_backup
git commit -m "chore: update Ella extensions backup"
```

---

## Summary Checklist

- [ ] Backup created (branch)
- [ ] Upstream fetched
- [ ] New branch from upstream created
- [ ] Reapply script run
- [ ] Dependencies added to pubspec.yaml
- [ ] EllaExtensions initialized in main.dart
- [ ] iOS plugins registered in AppDelegate.swift
- [ ] .dev.env configured
- [ ] Build successful
- [ ] Extensions initializing (check logs)
- [ ] Transcript forwarding connected
- [ ] Changes committed and pushed

---

## Files Created/Modified Summary

**New files (from backup)**:
- `lib/ella/` - All Ella extension code
- `ios/Runner/Ella/` - Native iOS plugins

**Modified files**:
- `pubspec.yaml` - Add dependencies
- `lib/main.dart` - Initialize EllaExtensions
- `ios/Runner/AppDelegate.swift` - Register plugins
- `.dev.env` - Ella backend URL

---

**Questions?** Contact iOS Developer or check `lib/ella/ELLA_EXTENSIONS_README.md`
