#!/bin/bash
# Compile the exact card policy block from SiriIntents.swift with the standalone tests and run them.
# Needs only the Swift toolchain (swiftc). Run ONLY in a coordinated SDK slot; outputs go to a temp dir.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${1:-$HERE/../..}"
SRC="$ROOT/ios/Runner/SiriIntegration/SiriIntents.swift"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/dev-card-policy.XXXXXX")"  # bare mktemp -d ignores TMPDIR on macOS
{ echo "import Foundation"; awk '/^\/\/ BEGIN DEV CARD POLICY$/{on=1} on{print} /^\/\/ END DEV CARD POLICY$/{on=0}' "$SRC"; } > "$OUT/policy.swift"
cp "$HERE/dev_card_policy_tests.swift" "$OUT/main.swift"  # top-level test code must be main.swift
grep -q "enum BaseTimedCaptureCardPolicy" "$OUT/policy.swift" || { echo "card policy block not found in $SRC"; exit 2; }
shasum -a 256 "$SRC" "$OUT/policy.swift"
swiftc -O "$OUT/policy.swift" "$OUT/main.swift" -o "$OUT/card_policy_tests"
"$OUT/card_policy_tests"
