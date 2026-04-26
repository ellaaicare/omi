#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# build-and-upload.sh — Ella iOS CI build script
#
# Builds Flutter iOS (prod), archives unsigned, signs with
# rcodesign using credentials in ~/.signing/, and uploads to
# TestFlight via xcrun altool.
#
# Signing credentials in ~/.signing/ (chmod 700 dir, 600 files):
#   dist_privkey.pem  — RSA private key (from fastlane AZ7KXT4377.p12)
#   dist_cert.pem     — Distribution cert (from AZ7KXT4377.cer)
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
RCODESIGN="/usr/local/bin/rcodesign"

FLAVOR="${FLAVOR:-prod}"
TEAM_ID="H6S4582TRM"
SCHEME="$FLAVOR"
ASC_ISSUER_ID="${ASC_ISSUER_ID:-5ed3a276-d6c0-43eb-ba70-13eed9b35a7e}"
ASC_KEY_PATH="/Users/ellaai/.private_keys/AuthKey_J77JD8RJXF.p8"
ASC_KEY_ID="J77JD8RJXF"

# Signing credentials — persistent, owner-only
SIGNING_DIR="$HOME/.signing"
PRIVKEY_PEM="$SIGNING_DIR/dist_privkey.pem"
CERT_PEM="$SIGNING_DIR/dist_cert.pem"
ENTITLEMENTS="$APP_DIR/entitlements.plist"
PROFILE="$HOME/Library/MobileDevice/Provisioning Profiles/AppStore_com.ellaaicare.ella.mobileprovision"

BUILD_DIR="$APP_DIR/build/ios"
ARCHIVE_PATH="$BUILD_DIR/EllaCare.xcarchive"
PAYLOAD_DIR="/tmp/ella-payload"
IPA_PATH="$BUILD_DIR/EllaCare.ipa"

if [ "$FLAVOR" = "prod" ]; then
  BUNDLE_ID="com.ellaaicare.ella"
  CONFIG="Release-prod"
else
  BUNDLE_ID="com.ellaaicare.ella.dev"
  CONFIG="Release-dev"
fi

log() { echo "=== $(date '+%H:%M:%S') $1 ==="; }

# ── Preflight: verify signing credentials exist ───────────────
if [ ! -f "$PRIVKEY_PEM" ] || [ ! -s "$PRIVKEY_PEM" ]; then
  echo "ERROR: Missing signing private key at $PRIVKEY_PEM"
  echo "Regenerate: cp /tmp/fastlane_certs/AZ7KXT4377.p12 $PRIVKEY_PEM && chmod 600 $PRIVKEY_PEM"
  exit 1
fi
if [ ! -f "$CERT_PEM" ] || [ ! -s "$CERT_PEM" ]; then
  echo "ERROR: Missing distribution cert at $CERT_PEM"
  echo "Regenerate: openssl x509 -inform DER -in /tmp/fastlane_certs/AZ7KXT4377.cer -out $CERT_PEM && chmod 600 $CERT_PEM"
  exit 1
fi
log "Signing credentials OK"

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
if [ ! -f .prod.env ]; then
  cat > .prod.env <<'EOF'
API_BASE_URL=https://api.ella-ai-care.com/
USE_WEB_AUTH=false
USE_AUTH_CUSTOM_TOKEN=false
EOF
fi
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
# Ensure firebase_options_*.dart exist (tracked in git; defensive fallback from prebuilt)
if [ ! -f lib/firebase_options_prod.dart ]; then
  cp setup/prebuilt/firebase_options.dart lib/firebase_options_prod.dart
fi
if [ ! -f lib/firebase_options_dev.dart ]; then
  cp setup/prebuilt/firebase_options.dart lib/firebase_options_dev.dart
fi
# Generate Custom.xcconfig from the plist matching the flavor
# Dev -> "Dev", Prod -> "Prod"
case "$FLAVOR" in
  prod) PLIST_FLAVOR="Prod" ;;
  dev)  PLIST_FLAVOR="Dev" ;;
  *)    echo "ERROR: Unknown FLAVOR=$FLAVOR (expected prod or dev)"; exit 1 ;;
esac
PLIST_PATH="ios/Config/${PLIST_FLAVOR}/GoogleService-Info.plist"
if [ ! -f "$PLIST_PATH" ]; then
  echo "ERROR: $PLIST_PATH not found for FLAVOR=$FLAVOR"
  exit 1
fi
bash scripts/generate_ios_custom_config.sh "$PLIST_PATH" ios/Flutter
echo "APP_BUNDLE_IDENTIFIER=$BUNDLE_ID" >> ios/Flutter/Custom.xcconfig

# ── Step 3b: Validate generated config ──────────────────────
XCCONFIG="ios/Flutter/Custom.xcconfig"
if ! grep -q "APP_BUNDLE_IDENTIFIER=$BUNDLE_ID" "$XCCONFIG" 2>/dev/null; then
  echo "ERROR: Custom.xcconfig APP_BUNDLE_IDENTIFIER mismatch (expected $BUNDLE_ID)"
  exit 1
fi
PLIST_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :BUNDLE_ID' "$PLIST_PATH" 2>/dev/null || true)"
PLIST_PROJECT_ID="$(/usr/libexec/PlistBuddy -c 'Print :PROJECT_ID' "$PLIST_PATH" 2>/dev/null || true)"
log "Config OK: flavor=$FLAVOR plist=$PLIST_FLAVOR bundle=$BUNDLE_ID project=$PLIST_PROJECT_ID"

# ── Step 4: Flutter build iOS (no codesign) ───────────────────
log "Flutter build ios --flavor $FLAVOR --release --no-codesign"
$FLUTTER build ios --flavor "$FLAVOR" --release --no-codesign

# ── Step 5: CocoaPods ─────────────────────────────────────────
log "Pod install"
cd "$IOS_DIR"
pod install --repo-update

# ── Step 6: xcodebuild archive (unsigned) ────────────────────
log "Archiving ($SCHEME) — unsigned"
mkdir -p "$BUILD_DIR"
xcodebuild archive \
  -workspace Runner.xcworkspace \
  -scheme "$SCHEME" \
  -configuration "$CONFIG" \
  -archivePath "$ARCHIVE_PATH" \
  -destination "generic/platform=iOS" \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee /tmp/ella-xcodebuild-archive.log | grep -E "(error:|ARCHIVE|succeeded|BUILD)" || true

if [ ! -d "$ARCHIVE_PATH" ]; then
  echo "ERROR: Archive failed. Full log at /tmp/ella-xcodebuild-archive.log"
  tail -50 /tmp/ella-xcodebuild-archive.log
  exit 1
fi
log "Archive succeeded: $ARCHIVE_PATH"

APP_BUNDLE="$ARCHIVE_PATH/Products/Applications/Ella Care.app"

# ── Step 7: Embed provisioning profile ───────────────────────
log "Embedding provisioning profile"
cp "$PROFILE" "$APP_BUNDLE/embedded.mobileprovision"

# ── Step 8: Sign with rcodesign ──────────────────────────────
log "Signing with rcodesign (Distribution)"
"$RCODESIGN" sign \
  --pem-source "$PRIVKEY_PEM" \
  --pem-source "$CERT_PEM" \
  -e "$ENTITLEMENTS" \
  "$APP_BUNDLE"

# Verify
AUTHORITY=$(codesign -dvvv "$APP_BUNDLE" 2>&1 | grep "^Authority=" | head -1)
log "Signature: $AUTHORITY"
if [[ "$AUTHORITY" != *"Apple Distribution"* ]]; then
  echo "ERROR: App not signed with Distribution cert"
  exit 1
fi

# ── Step 9: Package IPA ──────────────────────────────────────
log "Packaging IPA"
rm -rf "$PAYLOAD_DIR"
mkdir -p "$PAYLOAD_DIR/Payload"
cp -r "$APP_BUNDLE" "$PAYLOAD_DIR/Payload/"
mkdir -p "$BUILD_DIR"
rm -f "$IPA_PATH"  # remove old IPA to prevent zip from merging stale files
(cd "$PAYLOAD_DIR" && zip -qr "$IPA_PATH" Payload/)
rm -rf "$PAYLOAD_DIR"
log "IPA: $IPA_PATH ($(du -sh "$IPA_PATH" | cut -f1))"

# ── Step 10: Upload to TestFlight ─────────────────────────────
if [ "${SKIP_UPLOAD:-0}" = "1" ]; then
  log "Skipping upload (SKIP_UPLOAD=1)"
  echo "IPA ready at: $IPA_PATH"
  exit 0
fi

log "Uploading to TestFlight"
UPLOAD_LOG="/tmp/ella-altool-upload.log"
if ! xcrun altool --upload-app \
  -f "$IPA_PATH" \
  -t ios \
  --apiKey "$ASC_KEY_ID" \
  --apiIssuer "$ASC_ISSUER_ID" \
  2>&1 | tee "$UPLOAD_LOG"; then
  echo "ERROR: altool upload failed. Full log at $UPLOAD_LOG"
  exit 1
fi
if grep -Eq "ERROR:|Failed to upload|ENTITY_ERROR" "$UPLOAD_LOG"; then
  echo "ERROR: altool reported an upload failure. Full log at $UPLOAD_LOG"
  exit 1
fi

log "Done! Build uploaded to TestFlight."
