# Background Audio Setup Checklist

## ✅ Code Changes (COMPLETED)
The following changes have been made to `AppDelegate.swift`:

1. ✅ Fixed plugin registration method signatures (`register(withRegistry:)`)
2. ✅ Improved audio session configuration with `.default` mode
3. ✅ Added `.mixWithOthers` option for better compatibility
4. ✅ Added audio session interruption handlers
5. ✅ Added audio route change observers

## 📝 Info.plist Changes (ACTION REQUIRED)

### Step 1: Open Info.plist
Navigate to `ios/Runner/Info.plist` in Xcode or your file editor.

### Step 2: Add Background Modes
Add the following entries (see `INFO_PLIST_ADDITIONS.xml` for complete XML):

```xml
<key>UIBackgroundModes</key>
<array>
    <string>audio</string>
    <string>voip</string>
    <string>processing</string>
    <string>fetch</string>
    <string>remote-notification</string>
</array>
```

### Step 3: Verify Permissions
Ensure these permissions are present:
- `NSMicrophoneUsageDescription`
- `NSSpeechRecognitionUsageDescription`
- `NSBluetoothAlwaysUsageDescription`

## 🔧 Xcode Project Settings (VERIFY)

### Background Modes Capability
In Xcode:
1. Open `ios/Runner.xcworkspace`
2. Select the **Runner** target
3. Go to **Signing & Capabilities** tab
4. Verify **Background Modes** capability is enabled with:
   - ✅ Audio, AirPlay, and Picture in Picture
   - ✅ Voice over IP
   - ✅ Background processing
   - ✅ Background fetch
   - ✅ Remote notifications

If not present, click **+ Capability** and add it.

## 🧪 Testing Background Audio

### Test on Physical Device (Required)
Background audio does NOT work reliably in the iOS Simulator.

### Test Steps:
1. **Clean Build**: In Xcode, press `Cmd+Shift+K`
2. **Rebuild**: Press `Cmd+B`
3. **Deploy** to physical device
4. **Start Recording**: Begin audio recording in your app
5. **Background App**: Press Home button or lock device
6. **Verify**: Check that:
   - Audio recording continues
   - No interruption errors appear
   - Watch connectivity remains active

### Debug Logging
Check the Xcode console for these messages:
- ✅ "AppDelegate: Audio session configured for background recording"
- ✅ "AppDelegate: Audio session reactivated" (after interruption)
- ❌ "AppDelegate: Failed to configure audio session" (indicates a problem)

## 🐛 Troubleshooting

### Audio Stops When App Goes to Background
**Possible causes:**
1. Missing `UIBackgroundModes` in Info.plist
2. Background Modes not enabled in Xcode capabilities
3. Audio session not active
4. Testing on simulator instead of device

**Solution:** Verify all items in this checklist

### Audio Session Interruption
**Symptoms:** Audio stops after phone call or other interruption

**Solution:** The interruption handler in AppDelegate should automatically reactivate the session. Check console logs.

### Watch Audio Not Working
**Possible causes:**
1. Watch Connectivity not properly initialized
2. Audio chunks not being reassembled correctly
3. Flutter side not handling audio data

**Solution:** Check that `WCSession` is activated and `flutterWatchAPI` is initialized

## 📱 Platform-Specific Notes

### iOS 17+ Considerations
- Audio session behavior may differ slightly
- Test on iOS 17+ devices specifically
- Check for deprecation warnings

### watchOS Integration
- Ensure Watch Connectivity is active before recording
- Background audio transfer uses `didReceiveUserInfo` (confirmed in code)
- Audio chunks are properly prefixed with dummy bytes

## ✅ Final Verification Checklist

Before submitting/deploying:
- [ ] Info.plist contains `UIBackgroundModes` with `audio`
- [ ] Xcode capabilities show Background Modes enabled
- [ ] Code compiles without errors
- [ ] Tested on physical iOS device (iPhone)
- [ ] Tested with app backgrounded (Home button)
- [ ] Tested with device locked
- [ ] Tested with Bluetooth audio devices
- [ ] Tested Watch audio recording in background
- [ ] No console errors during background recording
- [ ] Audio data successfully transmitted to Flutter

## 🎯 What Changed in AppDelegate.swift

### Audio Session Configuration
**Before:**
```swift
try audioSession.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
try audioSession.setActive(true)
```

**After:**
```swift
try audioSession.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetooth, .mixWithOthers])
try audioSession.setActive(true, options: [])
// + Added interruption handlers
```

**Why:** `.default` mode is more reliable for background audio, and `.mixWithOthers` prevents conflicts with other audio apps.

### Plugin Registration
**Before:** `register(with:)`
**After:** `register(withRegistry:)`
**Why:** Correct API signature for Flutter plugin registration

### Added Handlers
- `handleAudioSessionInterruption`: Reactivates audio session after interruptions
- `handleAudioSessionRouteChange`: Logs audio route changes for debugging
