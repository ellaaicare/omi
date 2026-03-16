#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# build-and-upload.sh — Ella iOS CI build script
#
# Pulls latest main, builds Flutter iOS (prod), archives with
# xcodebuild, and uploads to TestFlight via fastlane.
#
# Required env vars:
#   ASC_ISSUER_ID  — App Store Connect API Issuer ID
#
# Optional env vars:
#   FLAVOR         — "prod" (default) or "dev"
#   SKIP_PULL      — set to "1" to skip git pull
#   SKIP_UPLOAD    — set to "1" to build only, no TestFlight upload
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_DIR="$REPO_ROOT/app"
IOS_DIR="$APP_DIR/ios"
FLUTTER="/Users/ellaai/dev/flutter/bin/flutter"

FLAVOR="${FLAVOR:-prod}"
TEAM_ID="H6S4582TRM"
SCHEME="$FLAVOR"
KEYCHAIN_PATH="$HOME/Library/Keychains/login.keychain-db"
KEYCHAIN_PASSWORD="comp3000"

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

# ── Step 0: Validate prerequisites ────────────────────────────
if [ -z "${ASC_ISSUER_ID:-}" ] && [ "${SKIP_UPLOAD:-0}" != "1" ]; then
  echo "ERROR: ASC_ISSUER_ID env var is required for TestFlight upload."
  echo "  Find it at: App Store Connect → Users and Access → Integrations → Team Key → Issuer ID"
  echo "  Or set SKIP_UPLOAD=1 to build without uploading."
  exit 1
fi

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
dart run build_runner build --delete-conflicting-outputs

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

# ── Step 6: Unlock keychain ───────────────────────────────────
log "Unlocking keychain"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"

# ── Step 7: xcodebuild archive ───────────────────────────────
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
  -authenticationKeyPath "$HOME/.private_keys/AuthKey_J77JD8RJXF.p8" \
  -authenticationKeyID "J77JD8RJXF" \
  -authenticationKeyIssuerID "${ASC_ISSUER_ID:-}" \
  | xcpretty || true

if [ ! -d "$ARCHIVE_PATH" ]; then
  echo "ERROR: Archive failed. Check xcodebuild output above."
  exit 1
fi

# ── Step 8: Export IPA ────────────────────────────────────────
log "Exporting IPA"
mkdir -p "$IPA_DIR"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE_PATH" \
  -exportPath "$IPA_DIR" \
  -exportOptionsPlist "$APP_DIR/ExportOptions.plist" \
  -allowProvisioningUpdates \
  -authenticationKeyPath "$HOME/.private_keys/AuthKey_J77JD8RJXF.p8" \
  -authenticationKeyID "J77JD8RJXF" \
  -authenticationKeyIssuerID "${ASC_ISSUER_ID:-}" \
  | xcpretty || true

IPA_FILE=$(find "$IPA_DIR" -name "*.ipa" -type f | head -1)
if [ -z "$IPA_FILE" ]; then
  echo "ERROR: IPA export failed."
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
export PATH="/opt/homebrew/opt/ruby/bin:/opt/homebrew/lib/ruby/gems/4.0.0/bin:$PATH"
cd "$IOS_DIR"
fastlane upload_testflight ipa:"$IPA_FILE"

log "Done! Build uploaded to TestFlight."
