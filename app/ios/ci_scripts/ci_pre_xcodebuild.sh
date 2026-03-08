#!/bin/bash
set -e

echo "=== Xcode Cloud: ci_pre_xcodebuild.sh ==="

REPO_ROOT="$CI_PRIMARY_REPOSITORY_PATH"
APP_DIR="$REPO_ROOT/app"

export PATH="$HOME/flutter/bin:$PATH"

cd "$APP_DIR"

echo "=== Building Flutter for iOS (prod, release) ==="
flutter build ios --flavor prod --release --no-codesign

echo "=== ci_pre_xcodebuild.sh complete ==="
