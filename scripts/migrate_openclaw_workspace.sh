#!/bin/bash
# OpenClaw Workspace Migration — Issue #633
# Copies agent workspace data from old UID to new UID on Mac Mini.
#
# Old UID: 5aGC5YE9BnhcSoTxxtT4ar6ILQy2
# New UID: 9JG9j251ugNYEWiOm7Nmqjfs5Av2
#
# Run on Mac Mini (ellas-mac-mini-1):
#   bash migrate_openclaw_workspace.sh

set -euo pipefail

OPENCLAW_USERS="$HOME/.openclaw/users"
OLD_DIR="$OPENCLAW_USERS/omi-5agc5ye9bnhcsotxxtt4ar6ilqy2"
NEW_DIR="$OPENCLAW_USERS/omi-9jg9j251ugnyewiom7nmqjfs5av2"

if [ ! -d "$OLD_DIR" ]; then
    echo "ERROR: Old UID workspace not found at $OLD_DIR"
    exit 1
fi

if [ ! -d "$NEW_DIR" ]; then
    echo "ERROR: New UID workspace not found at $NEW_DIR"
    exit 1
fi

echo "=== OpenClaw Workspace Migration — Issue #633 ==="
echo "  Old: $OLD_DIR"
echo "  New: $NEW_DIR"
echo ""

# For each sub-workspace in old that doesn't have content in new
for workspace in "$OLD_DIR"/*/; do
    workspace_name=$(basename "$workspace")
    new_workspace="$NEW_DIR/$workspace_name"

    echo "Processing: $workspace_name"

    if [ ! -d "$new_workspace" ]; then
        echo "  [COPY] Creating $workspace_name in new UID"
        cp -a "$workspace" "$new_workspace"
        continue
    fi

    # Both exist — merge files (old wins for non-empty files)
    for file in "$workspace"/*; do
        [ -d "$file" ] && continue
        filename=$(basename "$file")
        new_file="$new_workspace/$filename"

        if [ ! -f "$new_file" ]; then
            echo "  [COPY] $workspace_name/$filename (new file)"
            cp "$file" "$new_file"
        else
            old_size=$(wc -c < "$file" | tr -d ' ')
            new_size=$(wc -c < "$new_file" | tr -d ' ')

            if [ "$old_size" -gt "$new_size" ] && [ "$new_size" -lt 50 ]; then
                echo "  [REPLACE] $workspace_name/$filename (old=${old_size}B > new=${new_size}B)"
                cp "$file" "$new_file"
            else
                echo "  [SKIP] $workspace_name/$filename (old=${old_size}B, new=${new_size}B)"
            fi
        fi
    done
done

echo ""
echo "Done. Restart OpenClaw gateway to pick up changes:"
echo "  cd ~/openclaw-docker && docker compose restart gateway"
