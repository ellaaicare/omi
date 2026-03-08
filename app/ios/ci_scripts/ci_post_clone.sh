#!/bin/bash
set -e

echo "=== Xcode Cloud: ci_post_clone.sh ==="
echo "CI_WORKSPACE: $CI_WORKSPACE"
echo "CI_PRIMARY_REPOSITORY_PATH: $CI_PRIMARY_REPOSITORY_PATH"

# Xcode Cloud clones into CI_PRIMARY_REPOSITORY_PATH
# Our structure: repo_root/app/ios is the Xcode project
# So repo root = two levels up from where Xcode Cloud runs (app/ios/)
REPO_ROOT="$CI_PRIMARY_REPOSITORY_PATH"
APP_DIR="$REPO_ROOT/app"
IOS_DIR="$APP_DIR/ios"

echo "=== Step 1: Install Flutter ==="
FLUTTER_VERSION="3.35.3"
FLUTTER_DIR="$HOME/flutter"

if [ ! -d "$FLUTTER_DIR" ]; then
  echo "Downloading Flutter $FLUTTER_VERSION..."
  curl -sL "https://storage.googleapis.com/flutter_infra_release/releases/stable/macos/flutter_macos_arm64_${FLUTTER_VERSION}-stable.zip" -o /tmp/flutter.zip
  unzip -q /tmp/flutter.zip -d "$HOME"
  rm /tmp/flutter.zip
fi

export PATH="$FLUTTER_DIR/bin:$PATH"
flutter --version
flutter precache --ios

echo "=== Step 2: Set up environment files ==="
cd "$APP_DIR"

# Create .dev.env from Xcode Cloud environment variables (or defaults)
cat > .dev.env << DEVEOF
OPENAI_API_KEY=${OPENAI_API_KEY:-}
API_BASE_URL=${API_BASE_URL:-https://api.ella-ai-care.com/}
GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_API_KEY:-}
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}
USE_WEB_AUTH=false
DEVEOF

# Create .prod.env
cat > .prod.env << PRODEOF
OPENAI_API_KEY=${OPENAI_API_KEY:-}
API_BASE_URL=${PROD_API_BASE_URL:-https://api.ella-ai-care.com/}
GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_API_KEY:-}
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}
PRODEOF

echo "=== Step 3: Set up Firebase configs ==="
# Copy prebuilt Firebase configs (same as setup.sh does)
mkdir -p ios/Config/Dev/ ios/Config/Prod/ ios/Runner/
cp setup/prebuilt/firebase_options.dart lib/firebase_options_dev.dart
cp setup/prebuilt/firebase_options.dart lib/firebase_options_prod.dart
cp setup/prebuilt/GoogleService-Info.plist ios/Config/Dev/
cp setup/prebuilt/GoogleService-Info.plist ios/Config/Prod/
cp setup/prebuilt/GoogleService-Info.plist ios/Runner/

echo "=== Step 4: Generate iOS custom config ==="
# Generate GOOGLE_REVERSE_CLIENT_ID from GoogleService-Info.plist
bash scripts/generate_ios_custom_config.sh ios/Config/Dev/GoogleService-Info.plist ios/Flutter

# Set the Ella bundle identifier (not the upstream default)
echo "APP_BUNDLE_IDENTIFIER=com.ellaaicare.ella" >> ios/Flutter/Custom.xcconfig

echo "Custom.xcconfig contents:"
cat ios/Flutter/Custom.xcconfig

echo "=== Step 5: Flutter pub get + build_runner ==="
flutter pub get
dart run build_runner build --delete-conflicting-outputs

echo "=== Step 6: Install CocoaPods ==="
cd "$IOS_DIR"

# Xcode Cloud has CocoaPods pre-installed but may need repo update
pod install --repo-update

echo "=== ci_post_clone.sh complete ==="
