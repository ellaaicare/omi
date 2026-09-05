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
#   ELLA_PUBLIC_BUILD — "true"/"1" forces public launch mode (default: true)
#   ELLA_ENTITLEMENT_GATE — "true"/"1" enables invitation gating (default: false)
#   ELLA_GUARDIAN_ENABLED — "true"/"1" enables authenticated Whispers (default: true for prod)
#   SKIP_PULL      — set to "1" to skip git pull
#   RUN_TESTS      — set to "1" to run the Flutter suite after env generation
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
if [ -z "${ELLA_PUBLIC_BUILD+x}" ]; then
  if [ "$FLAVOR" = "prod" ]; then
    ELLA_PUBLIC_BUILD="true"
  else
    ELLA_PUBLIC_BUILD="false"
  fi
fi
if [ -z "${ELLA_GUARDIAN_ENABLED+x}" ]; then
  if [ "$FLAVOR" = "prod" ]; then
    ELLA_GUARDIAN_ENABLED="true"
  else
    ELLA_GUARDIAN_ENABLED="false"
  fi
fi
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
PUBSPEC_VERSION="$(awk '/^version:/ {print $2; exit}' "$APP_DIR/pubspec.yaml")"
EXPECTED_BUILD_NAME="${PUBSPEC_VERSION%%+*}"
EXPECTED_BUILD_NUMBER="${PUBSPEC_VERSION##*+}"

if [ "$FLAVOR" = "prod" ]; then
  BUNDLE_ID="com.ellaaicare.ella"
  CONFIG="Release-prod"
else
  BUNDLE_ID="com.ellaaicare.ella.dev"
  CONFIG="Release-dev"
fi

# The live OMI backend verifies Firebase tokens against this project. A stale
# upstream config can still complete Google sign-in, then fail every API call.
EXPECTED_FIREBASE_PROJECT_ID="omi-dev-ca005"

log() { echo "=== $(date '+%H:%M:%S') $1 ==="; }

SENSITIVE_BUILD_ENV_VARS=(
  A2A_BOT_REGISTRY_PATH
  A2A_GUIDANCE_COOLDOWN_SECONDS
  A2A_TRUST_REGISTRY_BOTS
  AI_API_KEY
  AI_PROVIDER_SECRETS_FILE
  ALLOWED_BOT_IDS
  ALLOWED_CHAT_IDS
  ALLOWED_SENDER_IDS
  ALLOWED_USER_IDS
  ANTHROPIC_API_KEY
  CLOUDFLARE_API_TOKEN
  CODEX_ADD_DIRS
  CODEX_BRIDGE_LOG_FILE
  CODEX_BRIDGE_PORT
  CODEX_BRIDGE_STATE_DIR
  CODEX_CI
  CODEX_DEFAULT_FOLDER
  CODEX_DANGEROUS_BYPASS
  CODEX_FULL_AUTO
  CODEX_MANAGED_BY_NPM
  CODEX_MANAGED_PACKAGE_ROOT
  CODEX_MODEL
  CODEX_SANDBOX
  CODEX_SKIP_GIT_REPO_CHECK
  CODEX_TELEGRAM_BOT_TOKEN
  CODEX_THREAD_ID
  CODEX_TIMEOUT
  GH_TOKEN
  GITHUB_TOKEN
  GOOGLE_API_KEY
  OPENAI_API_KEY
  OPENROUTER_API_KEY
  TELEGRAM_BOT_TOKEN
)

run_with_release_env() {
  local env_cmd=(env)
  local var
  for var in "${SENSITIVE_BUILD_ENV_VARS[@]}"; do
    env_cmd+=("-u" "$var")
  done
  "${env_cmd[@]}" "$@"
}

DART_DEFINES=()
if [ "$ELLA_PUBLIC_BUILD" = "true" ] || [ "$ELLA_PUBLIC_BUILD" = "1" ]; then
  DART_DEFINES+=(--dart-define=ELLA_PUBLIC_BUILD=true)
fi
if [ "${ELLA_ENTITLEMENT_GATE:-false}" = "true" ] || [ "${ELLA_ENTITLEMENT_GATE:-false}" = "1" ]; then
  DART_DEFINES+=(--dart-define=ELLA_ENTITLEMENT_GATE=true)
fi
if [ "$ELLA_GUARDIAN_ENABLED" = "true" ] || [ "$ELLA_GUARDIAN_ENABLED" = "1" ]; then
  DART_DEFINES+=(--dart-define=ELLA_GUARDIAN_ENABLED=true)
fi
if { [ "$ELLA_PUBLIC_BUILD" = "true" ] || [ "$ELLA_PUBLIC_BUILD" = "1" ]; } &&
  { [ "${ELLA_ENTITLEMENT_STUBS:-false}" = "true" ] || [ "${ELLA_ENTITLEMENT_STUBS:-false}" = "1" ]; }; then
  echo "ERROR: ELLA_ENTITLEMENT_STUBS cannot be enabled in a public build."
  exit 1
fi

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
if [ "$FLAVOR" = "prod" ] && [ ! -f .prod.env ]; then
  cat > .prod.env <<'EOF'
API_BASE_URL=https://api.ella-ai-care.com/
USE_WEB_AUTH=false
USE_AUTH_CUSTOM_TOKEN=false
EOF
fi
$FLUTTER pub get
$DART run build_runner build --delete-conflicting-outputs
# Envied's worker may not resolve annotation dotfile paths from APP_DIR.
# Regenerate only the selected flavor with an absolute path before packaging.
$DART run build_runner build \
  --delete-conflicting-outputs \
  --build-filter="lib/env/${FLAVOR}_env.g.dart" \
  --define="envied_generator:envied=path=$APP_DIR/.${FLAVOR}.env" \
  --define="envied_generator:envied=override=true"

if [ "$FLAVOR" = "prod" ] && grep -q "static final String? apiBaseUrl = null;" lib/env/prod_env.g.dart; then
  echo "ERROR: ProdEnv.apiBaseUrl is null after build_runner; refusing to ship a backend-disconnected prod app."
  exit 1
fi

# ── Step 3: Ensure Firebase/env configs exist ─────────────────
log "Checking configs"
mkdir -p ios/Config/Dev/ ios/Config/Prod/ ios/Runner/
if [ "$FLAVOR" = "prod" ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Prod/
elif [ ! -f ios/Config/Prod/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Prod/
fi
if [ ! -f ios/Config/Dev/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Dev/
fi
if [ ! -f ios/Runner/GoogleService-Info.plist ]; then
  cp setup/prebuilt/GoogleService-Info.plist ios/Runner/
fi
# The tracked prebuilt file is the release source of truth. Always refresh the
# generated prod options so persistent build hosts cannot silently drift.
if [ "$FLAVOR" = "prod" ]; then
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

# Runner must use the same flavor-specific Firebase config selected above.
cp "$PLIST_PATH" ios/Runner/GoogleService-Info.plist
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
FIREBASE_OPTIONS_FILE="lib/firebase_options_${FLAVOR}.dart"

if [ "$PLIST_PROJECT_ID" != "$EXPECTED_FIREBASE_PROJECT_ID" ]; then
  echo "ERROR: Firebase project mismatch in $PLIST_PATH (expected $EXPECTED_FIREBASE_PROJECT_ID, got ${PLIST_PROJECT_ID:-missing})."
  exit 1
fi
if [ "$FLAVOR" = "prod" ] && [ "$PLIST_BUNDLE_ID" != "$BUNDLE_ID" ]; then
  echo "ERROR: Firebase bundle mismatch in $PLIST_PATH (expected $BUNDLE_ID, got ${PLIST_BUNDLE_ID:-missing})."
  exit 1
fi
if [ ! -f "$FIREBASE_OPTIONS_FILE" ]; then
  echo "ERROR: Missing $FIREBASE_OPTIONS_FILE"
  exit 1
fi
if ! grep -q "projectId: '$EXPECTED_FIREBASE_PROJECT_ID'" "$FIREBASE_OPTIONS_FILE"; then
  echo "ERROR: $FIREBASE_OPTIONS_FILE does not target $EXPECTED_FIREBASE_PROJECT_ID."
  exit 1
fi
if grep -E "projectId:" "$FIREBASE_OPTIONS_FILE" | grep -Fv "projectId: '$EXPECTED_FIREBASE_PROJECT_ID'" >/dev/null; then
  echo "ERROR: $FIREBASE_OPTIONS_FILE contains a conflicting Firebase project."
  exit 1
fi
log "Config OK: flavor=$FLAVOR plist=$PLIST_FLAVOR bundle=$BUNDLE_ID project=$PLIST_PROJECT_ID"

if [ "${RUN_TESTS:-0}" = "1" ]; then
  log "Running Flutter test suite"
  run_with_release_env "$FLUTTER" test --no-pub
fi

# ── Step 4: Flutter build iOS (no codesign) ───────────────────
# Stamp diagnostics only after generated release inputs and optional tests are
# complete. A release artifact must never claim an immutable source revision
# while tracked or untracked repository inputs have drifted.
if [ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]; then
  echo "ERROR: Release source/config tree changed before source attribution; refusing to build."
  git -C "$REPO_ROOT" status --short --untracked-files=all
  exit 1
fi
SOURCE_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
DART_DEFINES+=(--dart-define=ELLA_SOURCE_REVISION="$SOURCE_REVISION")

log "Flutter build ios --flavor $FLAVOR --release --no-codesign ELLA_PUBLIC_BUILD=$ELLA_PUBLIC_BUILD ELLA_ENTITLEMENT_GATE=${ELLA_ENTITLEMENT_GATE:-false} ELLA_GUARDIAN_ENABLED=$ELLA_GUARDIAN_ENABLED"
FLUTTER_BUILD_ARGS=(
  build ios
  --flavor "$FLAVOR" \
  --release \
  --no-codesign \
  --build-name "$EXPECTED_BUILD_NAME" \
  --build-number "$EXPECTED_BUILD_NUMBER"
)
if [ "${#DART_DEFINES[@]}" -gt 0 ]; then
  FLUTTER_BUILD_ARGS+=("${DART_DEFINES[@]}")
fi
run_with_release_env "$FLUTTER" "${FLUTTER_BUILD_ARGS[@]}"

# ── Step 5: CocoaPods ─────────────────────────────────────────
log "Pod install"
cd "$IOS_DIR"
pod install --repo-update

# ── Step 6: xcodebuild archive (unsigned) ────────────────────
log "Archiving ($SCHEME) — unsigned"
mkdir -p "$BUILD_DIR"
rm -rf "$ARCHIVE_PATH"
run_with_release_env xcodebuild archive \
  -workspace Runner.xcworkspace \
  -scheme "$SCHEME" \
  -configuration "$CONFIG" \
  -archivePath "$ARCHIVE_PATH" \
  -destination "generic/platform=iOS" \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee /tmp/ella-xcodebuild-archive.log

if [ ! -d "$ARCHIVE_PATH" ]; then
  echo "ERROR: Archive failed. Full log at /tmp/ella-xcodebuild-archive.log"
  tail -50 /tmp/ella-xcodebuild-archive.log
  exit 1
fi
log "Archive succeeded: $ARCHIVE_PATH"

APP_BUNDLE="$ARCHIVE_PATH/Products/Applications/Ella Care.app"
ACTUAL_BUILD_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_BUNDLE/Info.plist" 2>/dev/null || true)"
ACTUAL_BUILD_NUMBER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_BUNDLE/Info.plist" 2>/dev/null || true)"
if [ "$ACTUAL_BUILD_NAME" != "$EXPECTED_BUILD_NAME" ] || [ "$ACTUAL_BUILD_NUMBER" != "$EXPECTED_BUILD_NUMBER" ]; then
  echo "ERROR: Archive version mismatch. Expected $EXPECTED_BUILD_NAME+$EXPECTED_BUILD_NUMBER, got $ACTUAL_BUILD_NAME+$ACTUAL_BUILD_NUMBER"
  exit 1
fi

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

# Verify with rcodesign as well; the host's system verifier is not reliable for
# distribution signatures in this environment.
SIGNATURE_INFO="/tmp/ella-rcodesign-signature-info.yaml"
APP_EXECUTABLE_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP_BUNDLE/Info.plist")"
APP_EXECUTABLE="$APP_BUNDLE/$APP_EXECUTABLE_NAME"
if [ ! -f "$APP_EXECUTABLE" ]; then
  echo "ERROR: Signed app executable is missing"
  exit 1
fi
"$RCODESIGN" verify "$APP_EXECUTABLE"
"$RCODESIGN" print-signature-info "$APP_EXECUTABLE" > "$SIGNATURE_INFO"
if ! grep -q "apple_certificate_profile: apple-distribution" "$SIGNATURE_INFO"; then
  echo "ERROR: App signature does not contain an Apple Distribution certificate profile"
  exit 1
fi
log "Signature verified by rcodesign with Apple Distribution profile"

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
