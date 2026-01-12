#!/bin/bash
#
# Ella Extensions Re-application Script
#
# Run this after pulling fresh from upstream to restore Ella-specific code.
#
# Usage:
#   cd /Users/greg/repos/omi/app
#   ./scripts/reapply_ella_extensions.sh
#
# What this script does:
# 1. Checks if lib/ella/ exists, restores from backup if not
# 2. Checks if ios/Runner/Ella/ exists, restores from backup if not
# 3. Verifies pubspec.yaml has required dependencies
# 4. Reminds you to add Ella initialization to main.dart
#

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$APP_DIR/ella_extensions_backup"

echo "============================================"
echo "  Ella Extensions Re-application Script"
echo "============================================"
echo ""
echo "App directory: $APP_DIR"
echo "Backup directory: $BACKUP_DIR"
echo ""

# Check if backup exists
if [ ! -d "$BACKUP_DIR" ]; then
    echo "ERROR: Backup directory not found: $BACKUP_DIR"
    echo ""
    echo "If this is a fresh clone, the ella_extensions_backup folder"
    echo "should be committed to the repo. Check git status."
    exit 1
fi

# 1. Restore lib/ella/ if missing
echo "Step 1: Checking lib/ella/..."
if [ ! -d "$APP_DIR/lib/ella" ]; then
    echo "  lib/ella/ not found, restoring from backup..."
    cp -r "$BACKUP_DIR/lib/ella" "$APP_DIR/lib/ella"
    echo "  ✅ lib/ella/ restored"
else
    echo "  ✅ lib/ella/ already exists"
fi

# 2. Restore ios/Runner/Ella/ if missing
echo ""
echo "Step 2: Checking ios/Runner/Ella/..."
if [ ! -d "$APP_DIR/ios/Runner/Ella" ]; then
    echo "  ios/Runner/Ella/ not found, restoring from backup..."
    cp -r "$BACKUP_DIR/ios/Ella" "$APP_DIR/ios/Runner/Ella"
    echo "  ✅ ios/Runner/Ella/ restored"
else
    echo "  ✅ ios/Runner/Ella/ already exists"
fi

# 3. Check pubspec.yaml dependencies
echo ""
echo "Step 3: Checking pubspec.yaml dependencies..."

REQUIRED_DEPS=("just_audio" "web_socket_channel" "shared_preferences" "path_provider")
MISSING_DEPS=()

for dep in "${REQUIRED_DEPS[@]}"; do
    if ! grep -q "$dep:" "$APP_DIR/pubspec.yaml"; then
        MISSING_DEPS+=("$dep")
    fi
done

if [ ${#MISSING_DEPS[@]} -eq 0 ]; then
    echo "  ✅ All required dependencies present"
else
    echo "  ⚠️  Missing dependencies: ${MISSING_DEPS[*]}"
    echo ""
    echo "  Add these to pubspec.yaml dependencies section:"
    for dep in "${MISSING_DEPS[@]}"; do
        echo "    $dep: ^latest"
    done
fi

# 4. Check main.dart integration
echo ""
echo "Step 4: Checking main.dart integration..."
if grep -q "EllaExtensions" "$APP_DIR/lib/main.dart" 2>/dev/null; then
    echo "  ✅ EllaExtensions found in main.dart"
else
    echo "  ⚠️  EllaExtensions NOT found in main.dart"
    echo ""
    echo "  Add to main.dart after OMI initialization:"
    echo ""
    echo "    import 'package:omi/ella/extensions.dart';"
    echo ""
    echo "    // In main() or after OMI init:"
    echo "    await EllaExtensions().initialize();"
fi

# 5. Check iOS plugin registration
echo ""
echo "Step 5: Checking iOS plugin registration..."
if grep -q "WakeWordPlugin" "$APP_DIR/ios/Runner/AppDelegate.swift" 2>/dev/null; then
    echo "  ✅ Ella plugins registered in AppDelegate"
else
    echo "  ⚠️  Ella plugins NOT registered in AppDelegate.swift"
    echo ""
    echo "  Add to AppDelegate.swift in didFinishLaunchingWithOptions:"
    echo ""
    echo "    WakeWordPlugin.register(with: registrar(forPlugin: \"WakeWordPlugin\")!)"
    echo "    VoiceV2VPlugin.register(with: registrar(forPlugin: \"VoiceV2VPlugin\")!)"
    echo "    NativeTtsPlugin.register(with: registrar(forPlugin: \"NativeTtsPlugin\")!)"
    echo "    AudioPushPlugin.register(with: registrar(forPlugin: \"AudioPushPlugin\")!)"
fi

echo ""
echo "============================================"
echo "  Re-application Complete"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Run 'flutter pub get'"
echo "  2. Fix any missing dependencies in pubspec.yaml"
echo "  3. Add EllaExtensions().initialize() to main.dart if needed"
echo "  4. Register iOS plugins in AppDelegate.swift if needed"
echo "  5. Run 'flutter run --flavor dev' to test"
echo ""
