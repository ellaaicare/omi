#!/usr/bin/env bash
# Derives the Dart side of the single upstream-capture activation setting
# (ellaaicare/ella-ai#1280) from ios/Flutter/EllaUpstreamCapture.xcconfig.
#
# The xcconfig value ELLA_UPSTREAM_CAPTURE_ENABLED (YES|NO) is the ONLY source of
# truth. This script never accepts an override: it reads that value, writes a
# --dart-define-from-file JSON, and prints shell assignments the build sources:
#
#   ELLA_UPSTREAM_CAPTURE_ENABLED=YES|NO
#   ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE=<absolute path to the JSON>
#   ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET=lib/main_upstream_capture.dart|lib/main.dart
#
# Usage: eval "$(bash ios/scripts/ella_upstream_capture_build_config.sh [output.json])"
#        flutter build ios -t "$ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET" \
#          --dart-define-from-file="$ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE" ...
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
XCCONFIG="$APP_DIR/ios/Flutter/EllaUpstreamCapture.xcconfig"
OUTPUT="${1:-$APP_DIR/build/ella_upstream_capture_dart_defines.json}"

if [[ ! -f "$XCCONFIG" ]]; then
  echo "ella_upstream_capture_build_config: missing $XCCONFIG" >&2
  exit 1
fi

# Exactly one assignment of the setting is allowed (ignoring // comments).
# (bash 3.2 compatible: macOS build hosts run /bin/bash 3.2.)
assignments="$(sed -e 's#//.*$##' "$XCCONFIG" | grep -E '^[[:space:]]*ELLA_UPSTREAM_CAPTURE_ENABLED[[:space:]]*=' || true)"
count=0
if [[ -n "$assignments" ]]; then
  count="$(printf '%s\n' "$assignments" | wc -l | tr -d ' ')"
fi
if [[ "$count" -ne 1 ]]; then
  echo "ella_upstream_capture_build_config: expected exactly one ELLA_UPSTREAM_CAPTURE_ENABLED assignment in $XCCONFIG, found $count" >&2
  exit 1
fi
value="$(printf '%s\n' "$assignments" | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//')"

case "$value" in
  YES) dart_value=true; target=lib/main_upstream_capture.dart ;;
  NO) dart_value=false; target=lib/main.dart ;;
  *)
    echo "ella_upstream_capture_build_config: ELLA_UPSTREAM_CAPTURE_ENABLED must be YES or NO, got '$value'" >&2
    exit 1
    ;;
esac

mkdir -p "$(dirname "$OUTPUT")"
printf '{\n  "ELLA_UPSTREAM_CAPTURE_ENABLED": %s\n}\n' "$dart_value" > "$OUTPUT"
OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")"

printf 'ELLA_UPSTREAM_CAPTURE_ENABLED=%s\n' "$value"
printf 'ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE=%q\n' "$OUTPUT"
printf 'ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET=%s\n' "$target"
