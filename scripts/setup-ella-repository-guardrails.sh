#!/usr/bin/env bash

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hooks_dir="$(git rev-parse --git-path hooks)"
origin_url="$(git remote get-url origin)"

if [[ "$origin_url" != *"ellaaicare/omi.git" ]]; then
  echo "Refusing setup: origin is not ellaaicare/omi ($origin_url)." >&2
  exit 1
fi

if git remote get-url upstream >/dev/null 2>&1; then
  git remote set-url --push upstream DISABLED
fi

git config remote.pushDefault origin
mkdir -p "$hooks_dir"
install -m 0755 "$repo_root/scripts/pre-commit" "$hooks_dir/pre-commit"
install -m 0755 "$repo_root/scripts/pre-push" "$hooks_dir/pre-push"

echo "Ella repository guardrails installed:"
echo "  issues: ellaaicare/ella-ai (explicit --repo required)"
echo "  pull requests: ellaaicare/omi"
echo "  upstream pushes: disabled"
