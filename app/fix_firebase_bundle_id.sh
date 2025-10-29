#!/bin/bash

# Fix Firebase Bundle ID Script
# Updates Firebase configuration to use com.greg.friendapp bundle ID

set -e  # Exit on error

echo "🔧 Fixing Firebase Bundle IDs..."
echo ""

# Define the bundle ID
NEW_BUNDLE_ID="com.greg.friendapp"
OLD_BUNDLE_ID="com.friend-app-with-wearable.ios12.development"

# Update firebase_options_dev.dart
echo "📝 Updating lib/firebase_options_dev.dart..."
sed -i '' "s|iosBundleId: '$OLD_BUNDLE_ID'|iosBundleId: '$NEW_BUNDLE_ID'|g" lib/firebase_options_dev.dart

# Update firebase_options_prod.dart
echo "📝 Updating lib/firebase_options_prod.dart..."
sed -i '' "s|iosBundleId: '$OLD_BUNDLE_ID'|iosBundleId: '$NEW_BUNDLE_ID'|g" lib/firebase_options_prod.dart

# Verify changes
echo ""
echo "✅ Verification:"
echo ""
echo "Dev bundle ID:"
grep "iosBundleId:" lib/firebase_options_dev.dart | head -2

echo ""
echo "Prod bundle ID:"
grep "iosBundleId:" lib/firebase_options_prod.dart | head -2

echo ""
echo "✅ Firebase bundle IDs updated successfully!"
echo ""
echo "📱 Next Steps:"
echo "1. Open Xcode: open ios/Runner.xcworkspace"
echo "2. Connect your iPhone via USB"
echo "3. Select your iPhone from device dropdown"
echo "4. Product → Clean Build Folder (Shift+Cmd+K)"
echo "5. Product → Run (Cmd+R)"
echo ""
echo "🎯 Xcode will handle CocoaPods, signing, and building automatically!"
