#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

flutter_test() {
  local args=(test)
  if [[ "${ELLA_TEST_NO_PUB:-false}" == "true" ]]; then
    args+=(--no-pub)
  fi
  flutter "${args[@]}" "$@"
}

missing_files=()
required_files=(
  "lib/firebase_options_dev.dart"
  "lib/firebase_options_prod.dart"
  "lib/env/dev_env.g.dart"
  "lib/env/prod_env.g.dart"
)

for file in "${required_files[@]}"; do
  if [[ ! -f "$file" ]]; then
    missing_files+=("$file")
  fi
done

if [[ ${#missing_files[@]} -gt 0 ]]; then
  echo "Missing generated files: ${missing_files[*]}"
  echo "Running setup prerequisites..."

  mkdir -p android/app/src/dev/ ios/Config/Dev/ ios/Runner/ macos/ macos/Config/Dev
  cp setup/prebuilt/firebase_options.dart lib/firebase_options_dev.dart
  cp setup/prebuilt/google-services.json android/app/src/dev/
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Dev/
  cp setup/prebuilt/GoogleService-Info.plist ios/Runner/
  cp setup/prebuilt/GoogleService-Info.plist macos/
  cp setup/prebuilt/GoogleService-Info.plist macos/Config/Dev/

  mkdir -p android/app/src/prod/ ios/Config/Prod/ macos/Config/Prod
  cp setup/prebuilt/firebase_options.dart lib/firebase_options_prod.dart
  cp setup/prebuilt/google-services.json android/app/src/prod/
  cp setup/prebuilt/GoogleService-Info.plist ios/Config/Prod/
  cp setup/prebuilt/GoogleService-Info.plist macos/Config/Prod/

  echo "API_BASE_URL=https://api.omiapi.com/" > .dev.env
  echo "USE_WEB_AUTH=true" >> .dev.env
  echo "USE_AUTH_CUSTOM_TOKEN=true" >> .dev.env

  if [[ "${ELLA_TEST_NO_PUB:-false}" == "true" ]]; then
    [[ -f .dart_tool/package_config.json ]] || {
      echo "ELLA_TEST_NO_PUB requires a completed flutter pub get." >&2
      exit 1
    }
  else
    flutter pub get
  fi
  dart run build_runner build --delete-conflicting-outputs
fi


# Each suite runs regardless of an earlier one's outcome, so a pre-existing
# failure in one suite (e.g. environment-only plugin-channel flakiness) can
# never silently skip a later suite — every suite's real result is reported,
# and the script's final exit code reflects all of them together.
overall_status=0

run_step() {
  local description="$1"
  shift
  echo "==> $description"
  if "$@"; then
    echo "==> $description: PASSED"
  else
    echo "==> $description: FAILED"
    overall_status=1
  fi
}

run_step "test/backend/http/conversation_finalization_test.dart" \
  flutter_test test/backend/http/conversation_finalization_test.dart
run_step "test/providers/capture_provider_test.dart" \
  flutter_test test/providers/capture_provider_test.dart
run_step "test/widgets/transcript_test.dart" \
  flutter_test test/widgets/transcript_test.dart

# Upstream capture port (ellaaicare/ella-ai#1280): identity guard, upstream's own
# capture tests on the vendored stack, and the Ella adapter/wiring tests.
run_step "scripts/verify_upstream_capture_identity.py" \
  python3 ../scripts/verify_upstream_capture_identity.py
run_step "test/upstream_capture test/ella/upstream_capture" \
  flutter_test test/upstream_capture test/ella/upstream_capture

exit "$overall_status"
