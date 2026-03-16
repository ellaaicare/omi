#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# build-and-upload.sh — Ella iOS CI build script
#
# Pulls latest main, builds Flutter iOS (prod), archives with
# xcodebuild, and uploads to TestFlight via fastlane.
#
# Optional env vars:
#   FLAVOR         — "prod" (default) or "dev"
#   SKIP_PULL      — set to "1" to skip git pull
#   SKIP_UPLOAD    — set to "1" to build only, no TestFlight upload
#   ASC_ISSUER_ID  — App Store Connect API Issuer ID (has default)
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_DIR="$REPO_ROOT/app"
IOS_DIR="$APP_DIR/ios"
FLUTTER="/Users/ellaai/dev/flutter/bin/flutter"
DART="/Users/ellaai/dev/flutter/bin/dart"

FLAVOR="${FLAVOR:-prod}"
TEAM_ID="H6S4582TRM"
SCHEME="$FLAVOR"
KEYCHAIN_PATH="$HOME/Library/Keychains/login.keychain-db"
KEYCHAIN_PASSWORD="comp3000"
ASC_ISSUER_ID="${ASC_ISSUER_ID:-5ed3a276-d6c0-43eb-ba70-13eed9b35a7e}"
ASC_KEY_PATH="/Users/ellaai/.private_keys/AuthKey_J77JD8RJXF.p8"
ASC_KEY_ID="J77JD8RJXF"

BUILD_DIR="$APP_DIR/build/ios"
ARCHIVE_PATH="$BUILD_DIR/EllaCare.xcarchive"
IPA_DIR="$BUILD_DIR/ipa"

if [ "$FLAVOR" = "prod" ]; then
  BUNDLE_ID="com.ellaaicare.ella"
  CONFIG="Release-prod"
else
  BUNDLE_ID="com.ellaaicare.ella.dev"
  CONFIG="Release-dev"
fi

log() { echo "=== $(date '+%H:%M:%S') $1 ==="; }

log "ASC Issuer ID: $ASC_ISSUER_ID  Key ID: $ASC_KEY_ID"

# ── Step 1: Pull latest main ──────────────────────────────────
if [ "${SKIP_PULL:-0}" != "1" ]; then
  log "Pulling latest main"
  cd "$REPO_ROOT"
  git checkout main
  git pull origin main
fi

# ── Step 2: Flutter setup ─────────────────────────────────────
log "Flutter pub get + build_runner"
cd "$APP_DIR"
$FLUTTER pub get
$DART run build_runner build --delete-conflicting-outputs

# ── Step 3: Ensure Firebase/env configs exist ─────────────────
log "Checking configs"
mkdir -p ios/Config/Dev/ ios/Config/Prod/ ios/Runner/
if [ ! -f ios/Config/Prod/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Prod/
fi
if [ ! -f ios/Config/Dev/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Dev/
fi
if [ ! -f ios/Runner/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Runner/
fi

# Generate Custom.xcconfig
bash scripts/generate_ios_custom_config.sh ios/Config/Dev/GoogleService-Info.plist ios/Flutter
echo "APP_BUNDLE_IDENTIFIER=$BUNDLE_ID" >> ios/Flutter/Custom.xcconfig

# ── Step 4: Flutter build iOS ─────────────────────────────────
log "Flutter build ios --flavor $FLAVOR --release"
$FLUTTER build ios --flavor "$FLAVOR" --release --no-codesign

# ── Step 5: CocoaPods ─────────────────────────────────────────
log "Pod install"
cd "$IOS_DIR"
pod install --repo-update

# ── Step 6: Unlock keychain + ensure Distribution cert ───────
log "Unlocking keychain and granting codesign access"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" > /dev/null 2>&1 || true

log "Ensuring Distribution certificate via fastlane"
export PATH="/opt/homebrew/opt/ruby/bin:/opt/homebrew/lib/ruby/gems/4.0.0/bin:$PATH"
BUNDLE_ID="$BUNDLE_ID" fastlane setup_signing

# ── Step 7: xcodebuild archive ───────────────────────────────
# CODE_SIGN_STYLE=Automatic with -allowProvisioningUpdates + ASC key allows
# xcodebuild to download/create a Distribution certificate automatically.
# Do NOT override CODE_SIGN_IDENTITY here — that conflicts with Pod targets
# which use Automatic signing. Let xcodebuild select the right cert type.
log "Archiving ($SCHEME)"
mkdir -p "$BUILD_DIR"
xcodebuild archive \
  -workspace Runner.xcworkspace \
  -scheme "$SCHEME" \
  -configuration "$CONFIG" \
  -archivePath "$ARCHIVE_PATH" \
  -destination "generic/platform=iOS" \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CODE_SIGN_STYLE=Automatic \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID" \
  2>&1 | tee /tmp/ella-xcodebuild-archive.log | grep -E "(error:|ARCHIVE|Signing|certificate|profile|succeeded)" || true

if [ ! -d "$ARCHIVE_PATH" ]; then
  echo "ERROR: Archive failed. Full log at /tmp/ella-xcodebuild-archive.log"
  echo "--- Last 50 lines ---"
  tail -50 /tmp/ella-xcodebuild-archive.log
  exit 1
fi
log "Archive succeeded: $ARCHIVE_PATH"

# ── Step 8: Export IPA ────────────────────────────────────────
log "Exporting IPA"
mkdir -p "$IPA_DIR"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE_PATH" \
  -exportPath "$IPA_DIR" \
  -exportOptionsPlist "$APP_DIR/ExportOptions.plist" \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$ASC_KEY_PATH" \
  -authenticationKeyID "$ASC_KEY_ID" \
  -authenticationKeyIssuerID "$ASC_ISSUER_ID" \
  2>&1 | tee /tmp/ella-xcodebuild-export.log | grep -E "(error:|EXPORT|ipa|IPA|succeeded)" || true

IPA_FILE=$(find "$IPA_DIR" -name "*.ipa" -type f | head -1)
if [ -z "$IPA_FILE" ]; then
  echo "ERROR: IPA export failed. Full log at /tmp/ella-xcodebuild-export.log"
  tail -30 /tmp/ella-xcodebuild-export.log
  exit 1
fi
echo "IPA: $IPA_FILE"

# ── Step 9: Upload to TestFlight ──────────────────────────────
if [ "${SKIP_UPLOAD:-0}" = "1" ]; then
  log "Skipping upload (SKIP_UPLOAD=1)"
  echo "IPA ready at: $IPA_FILE"
  exit 0
fi

log "Uploading to TestFlight via fastlane"
cd "$IOS_DIR"
fastlane upload_testflight ipa:"$IPA_FILE"

log "Done! Build uploaded to TestFlight."
