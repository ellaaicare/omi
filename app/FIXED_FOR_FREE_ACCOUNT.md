# ✅ FIXED FOR FREE APPLE DEVELOPER ACCOUNT

## 🎯 What Was Fixed

### Problem
The app required capabilities that need a **paid Apple Developer account** ($99/year):
- ❌ Push Notifications
- ❌ Sign in with Apple
- ❌ Associated Domains

Your personal (free) Apple ID **"Gregory Lindberg"** cannot use these capabilities.

### Solution Applied
✅ **Updated bundle ID** to `com.greg.friendapp` everywhere
✅ **Removed paid capabilities** from all entitlements files
✅ **Created minimal entitlements** that work with free account
✅ **Fixed all bundle ID references** in Xcode project

---

## 🚀 BUILD THE APP NOW

### Step 1: Open Xcode
```bash
cd /Users/greg/repos/omi/app
open ios/Runner.xcworkspace
```

### Step 2: Connect iPhone
1. Plug iPhone into Mac via USB
2. Unlock iPhone
3. Tap **Trust** when prompted
4. Wait for iPhone to appear in Xcode device list

### Step 3: Configure Signing
**In Xcode (left sidebar):**
1. Click **Runner** (blue icon)
2. Select **Runner** target
3. Go to **Signing & Capabilities** tab
4. Under **Team**: Select **"Gregory Lindberg (Personal Team)"**
5. Ensure **"Automatically manage signing"** is ✅ checked
6. Bundle Identifier should show: **com.greg.friendapp**

### Step 4: Clean & Build
**In Xcode menu:**
1. **Product** → **Clean Build Folder** (Shift+Cmd+K)
2. **Product** → **Run** (Cmd+R) or click ▶️

**Xcode will:**
- Generate provisioning profile automatically
- Install CocoaPods
- Build the app
- Install on your iPhone

---

## ⚠️ First Launch

**iPhone will show "Untrusted Developer":**

1. iPhone → **Settings**
2. **General** → **VPN & Device Management**
3. Tap **"Gregory Lindberg"**
4. Tap **Trust "Gregory Lindberg"**
5. Tap **Trust** again to confirm
6. Go back to home screen
7. **Open the app** - it will now launch!

---

## 🎯 What Features Still Work?

### ✅ Working (No Paid Account Needed)
- Bluetooth connection to device
- Microphone recording
- Local audio processing
- Custom backend URL configuration
- All core app features
- Firebase authentication (Google Sign-in works)

### ⚠️ Not Available (Require Paid Account)
- Push notifications
- Sign in with Apple
- Deep links (applinks:h.omi.me)

**These features are optional** - the app works fine without them for testing!

---

## 🔧 Troubleshooting

### "No provisioning profile"
- Xcode → Signing & Capabilities
- Uncheck → Re-check **"Automatically manage signing"**
- Make sure **Team** is set to "Gregory Lindberg (Personal Team)"

### "iPhone not detected"
- Disconnect and reconnect iPhone
- Unlock iPhone
- Trust computer
- Wait 10 seconds
- Check device dropdown in Xcode toolbar

### "Build Failed" with CocoaPods error
```bash
cd ios
rm -rf Pods Podfile.lock
pod install
cd ..
```
Then build again in Xcode.

### Build succeeds but app won't launch
- Check you **trusted the developer** on iPhone (Settings → General → VPN & Device Management)

---

## 📝 What Changed in This Commit

### Files Modified
- `ios/Runner.xcodeproj/project.pbxproj` - Updated all bundle IDs
- `ios/Runner/Info.plist` - Fixed bundle ID reference
- `ios/Runner/*.entitlements` - Removed paid capabilities (7 files)

### Before (Broken)
```xml
<!-- Entitlements that need paid account -->
<key>aps-environment</key>
<key>com.apple.developer.applesignin</key>
<key>com.apple.developer.associated-domains</key>
```

### After (Working)
```xml
<!-- Minimal entitlements for free account -->
<dict>
</dict>
```

### Bundle ID
- **Before:** `com.friend-app-with-wearable.ios12.development`
- **After:** `com.greg.friendapp` ✅

---

## 💡 About Free vs Paid Apple Developer Account

### Free Account (What You Have)
- ✅ Install apps on your own devices
- ✅ Test on physical iPhone
- ✅ Use most iOS features
- ✅ Local notifications
- ✅ Bluetooth, microphone, camera
- ❌ No push notifications
- ❌ No App Store distribution
- ❌ No Sign in with Apple
- ❌ No Associated Domains
- ⏰ Apps expire after 7 days (need to rebuild)

### Paid Account ($99/year)
- ✅ Everything from free
- ✅ Push notifications
- ✅ Sign in with Apple
- ✅ Associated Domains
- ✅ App Store distribution
- ✅ TestFlight distribution
- ✅ Apps don't expire

**For testing your backend, free account is perfect!** ✅

---

## 🎉 Next Steps

### 1. Build and Install (NOW!)
```bash
open ios/Runner.xcworkspace
# Click ▶️ in Xcode
```

### 2. Configure Backend
Once app is running on iPhone:
1. Open app → Profile icon → **Settings**
2. Scroll to **Developer Settings**
3. Tap **Developer Settings**
4. Scroll to **Infrastructure** section
5. Enter: `https://api.yourserver.com`
6. Tap **Save** (top right)
7. **Restart the app**

### 3. Test Your Backend!
- App now connects to your custom infrastructure
- All API calls go to your backend
- WebSocket connections work
- Test Bluetooth, recording, transcription

---

## 📚 Related Documents

- `CUSTOM_BACKEND_SETUP.md` - Backend URL configuration guide
- `IOS_INSTALLATION_GUIDE.md` - Detailed iOS setup instructions
- `INSTALLATION_SUMMARY.md` - Quick reference guide
- `fix_firebase_bundle_id.sh` - Bundle ID update script

---

## ✅ Summary

**You're all set!** The app is now configured for:
- ✅ Your free Apple Developer account ("Gregory Lindberg")
- ✅ Custom bundle ID: `com.greg.friendapp`
- ✅ Minimal capabilities (no paid features)
- ✅ Custom backend URL support

**Just open Xcode and hit Run!** 🚀

---

**Commit:** `9972047fc` - "fix: update bundle ID to com.greg.friendapp and remove paid capabilities"
**Branch:** `feature/ios-backend-integration`
