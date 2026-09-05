#!/bin/bash
set -euo pipefail

echo "=== Xcode Cloud: ci_pre_xcodebuild.sh ==="

REPO_ROOT="$CI_PRIMARY_REPOSITORY_PATH"
APP_DIR="$REPO_ROOT/app"

export PATH="$HOME/flutter/bin:$PATH"

cd "$APP_DIR"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]]; then
  echo "Xcode Cloud source checkout is not clean; refusing unattributable release build." >&2
  exit 1
fi
SOURCE_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [[ ! "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Xcode Cloud source revision is not an exact Git SHA." >&2
  exit 1
fi

echo "=== Building Flutter for iOS (prod, release) ==="
flutter build ios \
  --flavor prod \
  --release \
  --no-codesign \
  --dart-define=ELLA_PUBLIC_BUILD=true \
  --dart-define="ELLA_SOURCE_REVISION=$SOURCE_REVISION"

echo "=== ci_pre_xcodebuild.sh complete ==="
