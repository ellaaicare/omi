# iOS Installation Guide - Installing Omi App on Physical iPhone

This guide walks you through installing the Omi Flutter app on your physical iPhone for testing.

## Prerequisites Checklist

✅ **Mac with Xcode** (You have: Xcode 26.0.1)
✅ **Flutter installed** (You have: Flutter 3.35.7)
✅ **Physical iPhone** with iOS 15.0 or later
✅ **USB-C to Lightning cable** (or USB-C to USB-C for newer iPhones)
✅ **Apple ID** (free or paid Apple Developer account)

## Quick Start (5 Minutes)

### Step 1: Enable Developer Mode on iPhone

**On iPhone (iOS 16+):**
1. Go to **Settings** → **Privacy & Security**
2. Scroll to bottom and tap **Developer Mode**
3. Toggle **Developer Mode** ON
4. Enter your passcode
5. Tap **Restart**
6. After restart, confirm you want to enable Developer Mode

**On iPhone (iOS 15):**
- Developer Mode is enabled automatically when you first connect to Xcode

### Step 2: Connect iPhone to Mac

1. **Connect iPhone to Mac** using USB cable
2. **Unlock your iPhone**
3. **Trust this computer** prompt will appear on iPhone
   - Tap **Trust**
   - Enter your iPhone passcode

### Step 3: Open Project in Xcode

```bash
# Navigate to the app directory
cd /Users/greg/repos/omi/app

# Open the iOS workspace in Xcode
open ios/Runner.xcworkspace
```

**Important:** Always open the `.xcworkspace` file, NOT the `.xcodeproj` file (because this project uses CocoaPods).

### Step 4: Configure Signing in Xcode

1. In Xcode, select **Runner** in the left sidebar (blue icon)
2. Select the **Runner** target (under TARGETS)
3. Go to **Signing & Capabilities** tab
4. Under **Team**, click the dropdown and select:
   - **Add Account...** if you haven't signed in
   - Sign in with your Apple ID
   - Select your team (personal team is fine for testing)
5. Xcode will automatically generate a provisioning profile
6. Ensure **Automatically manage signing** is checked

**Bundle Identifier:**
- The app uses bundle ID: `me.omi.app` (for prod) or `me.omi.app.dev` (for dev)
- Xcode may ask you to change it if there's a conflict
- You can change it to something like: `com.yourname.omi.app`

**Repeat for Extensions:**
If you see additional targets (like ImageNotification, omiWatchApp), configure signing for them too:
1. Select each target
2. Go to **Signing & Capabilities**
3. Select the same Team

### Step 5: Select Your iPhone as Target

1. At the top of Xcode, next to the Run button (▶️)
2. Click the device selector dropdown
3. Choose your connected iPhone from the list
   - It should show: **"Your iPhone Name" (iOS version)**
4. If you don't see your iPhone:
   - Make sure it's connected and unlocked
   - Check if you trusted the computer
   - Wait a few seconds for Xcode to detect it

### Step 6: Build and Run

**Option A: Using Xcode (Recommended for first time)**

1. Click the **Run** button (▶️) in Xcode, or press `Cmd + R`
2. Xcode will:
   - Install CocoaPods dependencies
   - Build the Flutter app
   - Sign the app with your certificate
   - Install the app on your iPhone
3. **First time only:** On your iPhone:
   - Go to **Settings** → **General** → **VPN & Device Management**
   - Tap on your Apple ID under **Developer App**
   - Tap **Trust "Your Name"**
   - Tap **Trust** again to confirm
4. The app will now launch on your iPhone!

**Option B: Using Flutter CLI**

```bash
cd /Users/greg/repos/omi/app

# Install dependencies
flutter pub get

# Install iOS pods
cd ios
pod install
cd ..

# Run on connected device
flutter run
```

To specify a specific device:
```bash
# List available devices
flutter devices

# Run on specific device
flutter run -d <device-id>
```

## Troubleshooting

### Issue: "Developer Mode Required"
**Solution:**
- Go to Settings → Privacy & Security → Developer Mode
- Enable it and restart iPhone

### Issue: "Could not find an option named 'flutter'"
**Solution:**
```bash
# Check Flutter is in PATH
which flutter

# If not found, add to PATH in ~/.zshrc or ~/.bash_profile:
export PATH="$PATH:$HOME/flutter/bin"

# Or use full path:
/path/to/flutter/bin/flutter run
```

### Issue: "The sandbox is not in sync with the Podfile.lock"
**Solution:**
```bash
cd ios
pod deintegrate
pod install
cd ..
flutter clean
flutter pub get
```

### Issue: "No provisioning profiles found"
**Solution:**
1. In Xcode, go to Signing & Capabilities
2. Uncheck "Automatically manage signing"
3. Wait 2 seconds
4. Re-check "Automatically manage signing"
5. Select your team again

### Issue: "Code signature invalid"
**Solution:**
```bash
# Clean build
cd ios
rm -rf Pods
pod install
cd ..
flutter clean
flutter run
```

### Issue: "iPhone not detected"
**Solution:**
1. Disconnect and reconnect iPhone
2. Trust computer on iPhone
3. Restart iPhone
4. Restart Xcode
5. Check cable (try different USB port)

### Issue: Bluetooth not working
**Solution:**
- Bluetooth permissions are in `Info.plist`
- Go to iPhone Settings → Omi → Enable Bluetooth permission
- Grant Bluetooth permission when prompted in app

### Issue: Microphone not working
**Solution:**
- Go to iPhone Settings → Omi → Enable Microphone permission
- Grant microphone permission when prompted

## Building Different Flavors

This app has two flavors: **dev** and **prod**

### Using Xcode:
1. Product → Scheme → Select **dev** or **prod**
2. Run as usual

### Using Flutter CLI:

**Development flavor:**
```bash
flutter run --flavor dev
```

**Production flavor:**
```bash
flutter run --flavor prod
```

## Environment Configuration

The app uses environment variables from `.env` files:

**For Development:**
```bash
# Create/edit .dev.env
cp .env.template .dev.env
nano .dev.env
```

**For Production:**
```bash
# Create/edit .prod.env
cp .env.template .prod.env
nano .prod.env
```

Add your backend URL:
```
API_BASE_URL=https://api.yourserver.com/
```

**Or use the in-app settings:**
- Settings → Developer Settings → Infrastructure → Custom API Base URL

## Testing Backend Integration

Once the app is running on your iPhone:

1. **Open the app**
2. Go to **Settings** (profile icon)
3. Scroll down to **Developer Settings**
4. Tap **Developer Settings**
5. Scroll to **Infrastructure** section
6. Enter your custom backend URL
7. Tap **Save** (top right)
8. **Close and reopen the app**

Your iPhone will now connect to your custom backend!

## Wireless Debugging (Optional)

Once your iPhone is set up, you can debug wirelessly:

### Enable Wireless Debugging:
1. Connect iPhone to Mac via USB
2. In Xcode: Window → Devices and Simulators
3. Select your iPhone
4. Check "Connect via network"
5. Wait for network icon to appear next to iPhone
6. Disconnect USB cable
7. iPhone and Mac must be on the same WiFi network

### Run wirelessly:
```bash
flutter devices  # Should show your iPhone
flutter run      # Will deploy wirelessly
```

## Performance Tips

For better development experience:

### Hot Reload
While app is running:
- Press `r` in terminal for hot reload
- Press `R` in terminal for hot restart
- In Xcode: Cmd+R to rebuild

### Debug vs Release Build

**Debug build** (default):
- Slower performance
- Larger app size
- Hot reload enabled
- Debug console available

**Release build** (for testing performance):
```bash
flutter run --release
```

## Checking Device Logs

### Using Xcode:
1. Window → Devices and Simulators
2. Select your iPhone
3. Click "Open Console"
4. See real-time logs

### Using Terminal:
```bash
# Stream device logs
idevicesyslog

# Or use Xcode's console
```

## Next Steps

Now that the app is installed:

1. ✅ **Test Bluetooth:** Connect to your Omi device
2. ✅ **Test Microphone:** Record audio
3. ✅ **Test Backend:** Configure custom backend URL
4. ✅ **Test Real-time Transcription:** Check WebSocket connections
5. ✅ **Review Logs:** Check Developer Settings → Debug logs

## Common Permissions to Grant

When you first run the app, you'll be prompted for:

- ✅ **Bluetooth** - For connecting to Omi device
- ✅ **Microphone** - For recording audio
- ✅ **Notifications** - For alerts and reminders
- ✅ **Location** (optional) - For location-based features

Grant all permissions for full functionality.

## Keeping the App Updated

After making code changes:

```bash
# Quick rebuild
flutter run

# Full clean rebuild (if issues occur)
flutter clean
flutter pub get
cd ios
pod install
cd ..
flutter run
```

## App Store Distribution (Future)

If you want to distribute to TestFlight or App Store:

1. **Enroll in Apple Developer Program** ($99/year)
2. **Create App Store Connect record**
3. **Archive and upload** via Xcode
4. **Submit for review**

For now, installing directly via Xcode is perfect for testing!

## Support

If you encounter issues:
1. Check this troubleshooting guide
2. Review Flutter docs: https://docs.flutter.dev/deployment/ios
3. Check Omi documentation
4. Ask in Omi community Discord

---

**Happy Testing! 🚀**

You're all set to test the Omi app on your iPhone with custom backend support.
