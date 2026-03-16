#!/bin/bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# setup-github-runner.sh — Register this Mac as a GitHub Actions
# self-hosted runner for the ellaaicare/omi repository.
#
# Prerequisites:
#   1. gh CLI authenticated: gh auth login
#   2. Or set RUNNER_TOKEN manually from GitHub Settings →
#      Actions → Runners → New self-hosted runner
#
# Usage:
#   ./setup-github-runner.sh
# ──────────────────────────────────────────────────────────────

REPO="ellaaicare/omi"
RUNNER_DIR="$HOME/actions-runner"
RUNNER_NAME="ella-mac-mini"
RUNNER_LABELS="self-hosted,macOS,ARM64,ios-builder"

echo "=== Setting up GitHub Actions self-hosted runner ==="

# Download runner if not already present
if [ ! -f "$RUNNER_DIR/config.sh" ]; then
  echo "Downloading GitHub Actions runner..."
  mkdir -p "$RUNNER_DIR"
  cd "$RUNNER_DIR"

  # Get latest runner version
  RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | grep '"tag_name"' | sed 's/.*"v\(.*\)".*/\1/')
  echo "Runner version: $RUNNER_VERSION"

  curl -sL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz" -o actions-runner.tar.gz
  tar xzf actions-runner.tar.gz
  rm actions-runner.tar.gz
fi

cd "$RUNNER_DIR"

# Get registration token
if [ -z "${RUNNER_TOKEN:-}" ]; then
  echo "Getting registration token via gh CLI..."
  if ! command -v gh &>/dev/null || ! gh auth status &>/dev/null; then
    echo "ERROR: gh CLI not authenticated. Either:"
    echo "  1. Run: gh auth login"
    echo "  2. Or get a token from: https://github.com/$REPO/settings/actions/runners/new"
    echo "     Then run: RUNNER_TOKEN=<token> $0"
    exit 1
  fi
  RUNNER_TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq '.token')
fi

echo "Configuring runner: $RUNNER_NAME"
./config.sh \
  --url "https://github.com/$REPO" \
  --token "$RUNNER_TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work "_work" \
  --replace

echo ""
echo "=== Runner configured! ==="
echo ""
echo "To start the runner:"
echo "  cd $RUNNER_DIR && ./run.sh"
echo ""
echo "To install as a launchd service (runs on boot):"
echo "  cd $RUNNER_DIR && sudo ./svc.sh install && sudo ./svc.sh start"
echo ""
echo "To verify it's registered:"
echo "  gh api repos/$REPO/actions/runners --jq '.runners[] | .name'"
