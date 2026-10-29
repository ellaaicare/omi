#!/bin/bash
# Compile the exact policy block from PhoneMicController.swift with the standalone tests and run them.
# Needs only the Swift toolchain (swiftc). Run ONLY in a coordinated SDK slot; outputs go to a temp dir.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${1:-$HERE/../..}"
SRC="$ROOT/ios/Runner/PhoneMic/PhoneMicController.swift"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/dev-token-policy.XXXXXX")"  # bare mktemp -d ignores TMPDIR on macOS
{ echo "import Foundation"; awk '/^\/\/ BEGIN DEV TOKEN POLICY$/{on=1} on{print} /^\/\/ END DEV TOKEN POLICY$/{on=0}' "$SRC"; } > "$OUT/policy.swift"
cp "$HERE/dev_token_policy_tests.swift" "$OUT/main.swift"  # top-level test code must be main.swift
grep -q "enum PhoneMicDevTokenPolicy" "$OUT/policy.swift" || { echo "policy block not found in $SRC"; exit 2; }
shasum -a 256 "$SRC" "$OUT/policy.swift"
swiftc -O "$OUT/policy.swift" "$OUT/main.swift" -o "$OUT/policy_tests"
"$OUT/policy_tests"
