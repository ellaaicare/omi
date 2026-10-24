#!/bin/bash
set -e

echo "=== Xcode Cloud: ci_pre_xcodebuild.sh ==="

REPO_ROOT="$CI_PRIMARY_REPOSITORY_PATH"
APP_DIR="$REPO_ROOT/app"

export PATH="$HOME/flutter/bin:$PATH"

cd "$APP_DIR"

UPSTREAM_CAPTURE_CONFIG="$(bash "$APP_DIR/ios/scripts/ella_upstream_capture_build_config.sh" "$APP_DIR/build/ella_upstream_capture_dart_defines.json")"
eval "$UPSTREAM_CAPTURE_CONFIG"

echo "=== Building Flutter for iOS (prod, release) ==="
flutter build ios --flavor prod --release --no-codesign --dart-define=ELLA_PUBLIC_BUILD=true \
  -t "$ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET" \
  --dart-define-from-file="$ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE"

echo "=== ci_pre_xcodebuild.sh complete ==="
