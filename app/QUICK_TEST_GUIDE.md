# 🚀 Quick Test Guide - Flutter Command Line vs Xcode

**For:** Testing the Ella AI app on your iPhone
**Date:** October 29, 2025

---

## 📱 Two Ways to Run the App

### Method 1: Flutter Command Line (RECOMMENDED - FASTER!)
### Method 2: Xcode Build Button (What you've been using)

---

## ⚡ Method 1: Flutter Command Line (FASTEST)

### Step 1: Connect iPhone with USB Cable
```bash
# Plug in your iPhone via Lightning/USB-C cable
# Make sure phone is UNLOCKED
```

### Step 2: Check Device is Detected
```bash
cd /Users/greg/repos/omi/app
flutter devices
```

**You should see:**
```
Found 3 connected devices:
  Doge 13 Pro (mobile) • 00008110-00165D41262B801E • ios • iOS 26.0.1 ✅
  macOS (desktop)      • macos                     • darwin-arm64
  Chrome (web)         • chrome                    • web-javascript
```

### Step 3: Run the App
```bash
# Simple version (Flutter picks device automatically):
flutter run --flavor dev

# OR specific device (if you have multiple):
flutter run --device-id 00008110-00165D41262B801E --flavor dev
```

**First time takes ~60 seconds (building)**
**After that: ~5 seconds!** ⚡

### Step 4: Hot Reload During Development
While app is running, you can:
- Press `r` → **Hot reload** (instant UI updates!)
- Press `R` → **Hot restart** (full app restart)
- Press `q` → **Quit** (stop app)

**This is HUGE:** You can edit code and press `r` to see changes in <1 second! 🔥

---

## 🐢 Method 2: Xcode Build Button (SLOWER)

### What You've Been Doing:
1. Open `ios/Runner.xcworkspace` in Xcode
2. Select your iPhone from device dropdown
3. Click ▶️ Build button
4. Wait 60-90 seconds
5. App installs and runs

### Why It's Slower:
- Xcode rebuilds EVERYTHING every time
- No hot reload capability
- More clicks and UI navigation

### When to Use Xcode:
- Viewing iOS-specific logs
- Debugging iOS crash issues
- Changing build settings/entitlements
- **First time setup only**

---

## 🚀 Complete Quick Test Workflow

### Fresh Start (First Time):
```bash
# 1. Connect iPhone via USB
# 2. Open Terminal

cd /Users/greg/repos/omi/app

# 3. Check device connected
flutter devices

# 4. Run app
flutter run --flavor dev

# Wait ~60 seconds for build...
# App launches on iPhone! ✅
```

### Testing Changes (Subsequent Times):
```bash
# App is already running from previous step

# 1. Make code changes in VS Code/editor
# 2. Save files
# 3. Go to Terminal where flutter run is active
# 4. Press 'r' key

# Changes appear in <1 second! 🔥
```

### Testing TTS:
```bash
# 1. App running on iPhone
# 2. Connect AirPods to iPhone
# 3. In app: Settings → Developer Settings
# 4. Scroll to "🎧 Audio & TTS Testing"
# 5. Tap "🔊 Test Message"
# 6. Hear audio in AirPods! ✅
```

---

## 🎯 Quick Command Reference

```bash
# From app directory:
cd /Users/greg/repos/omi/app

# List devices
flutter devices

# Run on iOS (dev flavor)
flutter run --flavor dev

# Run on specific device
flutter run --device-id 00008110-00165D41262B801E --flavor dev

# Run in release mode (faster, no debug)
flutter run --flavor dev --release

# Clean build (if something breaks)
flutter clean
flutter pub get
flutter run --flavor dev

# While app running:
# r  = Hot reload (instant updates)
# R  = Hot restart (full restart)
# q  = Quit app
# d  = Detach (app keeps running, terminal exits)
```

---

## 💡 Pro Tips

### Tip 1: Keep Terminal Open
```bash
# Start flutter run
flutter run --flavor dev

# Terminal shows:
# "Flutter run key commands:"
# "r Hot reload. 🔥🔥🔥"
# "q Quit"

# LEAVE THIS TERMINAL OPEN!
# You can hot reload anytime by pressing 'r'
```

### Tip 2: Hot Reload is Your Friend
```dart
// Edit code in lib/pages/settings/developer.dart
const Text('🎧 Audio & TTS Testing')

// Change to:
const Text('🎧 Ella Audio Testing')

// Save file
// Press 'r' in terminal
// UI updates instantly! (no rebuild!)
```

### Tip 3: View Logs in Real-Time
```bash
flutter run --flavor dev

# Terminal shows:
# "flutter: ----------------FIREBASE CRASHLYTICS----------------"
# "flutter: EllaTtsService speak error: ..."
# All print() and debugPrint() statements appear here!
```

### Tip 4: Multiple Devices
```bash
# Run on iPhone
flutter run -d 00008110-00165D41262B801E --flavor dev

# While that's running, open new terminal and:
flutter run -d macos --flavor dev

# Now app runs on BOTH iPhone AND Mac!
# Great for testing different screen sizes
```

---

## 🔍 Troubleshooting

### Issue: "No devices found"
**Fix:**
```bash
# 1. Unplug iPhone
# 2. Unlock iPhone
# 3. Plug in iPhone
# 4. Trust computer (popup on iPhone)
# 5. Try again:
flutter devices
```

### Issue: "Xcode signing error"
**Fix:**
```bash
# Open Xcode project once
open ios/Runner.xcworkspace

# In Xcode:
# 1. Select "Runner" project
# 2. Select "Signing & Capabilities" tab
# 3. Select your personal team
# 4. Close Xcode
# 5. Now flutter run works!
```

### Issue: "Build failed"
**Fix:**
```bash
# Nuclear option - clean everything
flutter clean
flutter pub get
cd ios && pod install && cd ..
flutter run --flavor dev
```

### Issue: "App won't install (7-day expiration)"
**Fix:**
```bash
# Free Apple Developer accounts: apps expire after 7 days
# Just rebuild:
flutter run --flavor dev
# App re-signs and installs fresh
```

---

## 🎯 Your Exact Workflow for Testing TTS

### Complete Start-to-Finish:
```bash
# ===== STEP 1: Connect & Prepare =====
# - Connect iPhone via USB cable
# - Unlock iPhone
# - Connect AirPods to iPhone via Bluetooth

# ===== STEP 2: Terminal Commands =====
cd /Users/greg/repos/omi/app

# Check device
flutter devices
# Look for: "Doge 13 Pro" or your iPhone name ✅

# Run app
flutter run --flavor dev

# Wait ~60 seconds...
# You'll see: "✓ Built build/ios/iphoneos/Runner.app"

# ===== STEP 3: On iPhone =====
# App opens automatically
# Tap Profile icon → Settings
# Tap "Developer Settings"
# Scroll to "🎧 Audio & TTS Testing"
# Tap "🔊 Test Message"

# ===== STEP 4: Listen =====
# Audio plays through AirPods! 🎧✨
```

---

## ⏱️ Speed Comparison

| Method | First Build | Subsequent Changes | Hot Reload |
|--------|-------------|-------------------|------------|
| **Flutter CLI** | 60 sec | Press 'r' → <1 sec ⚡ | ✅ YES |
| **Xcode Button** | 90 sec | Rebuild → 90 sec 🐢 | ❌ NO |

**Winner:** Flutter CLI by a LANDSLIDE! 🏆

---

## 🎉 You're Ready!

**Current Command to Run:**
```bash
cd /Users/greg/repos/omi/app
flutter run --flavor dev
```

**Then test TTS:**
1. Connect AirPods
2. Settings → Developer Settings
3. Tap TTS test buttons
4. Hear Ella speak! 🎧

**No Xcode needed!** (unless you want to see iOS-specific logs)

---

## 📝 Summary

### Before (Your Old Workflow):
1. Open Xcode
2. Click build button
3. Wait 90 seconds
4. Test change
5. Repeat steps 2-4 for every change 😫

### After (New Workflow):
1. `flutter run --flavor dev` (once)
2. Make changes
3. Press `r` in terminal
4. See changes in <1 second ⚡
5. Repeat steps 2-4 forever 🎉

**You just unlocked TURBO MODE!** 🚀
