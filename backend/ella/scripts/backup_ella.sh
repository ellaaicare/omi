#!/bin/bash
#
# Ella Backend API - Backup Script
#
# Run this BEFORE any upstream merge to safely backup all Ella files.
#
# Usage:
#   cd /Users/greg/repos/omi
#   ./backend/ella/scripts/backup_ella.sh
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Find repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# Create backup directory
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/tmp/ella-backup-$TIMESTAMP"
mkdir -p "$BACKUP_DIR"

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         ELLA BACKEND API - BACKUP SCRIPT                   ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Backup directory: ${YELLOW}$BACKUP_DIR${NC}"
echo ""

# Backup ella/ module
if [ -d "$BACKEND_DIR/ella" ]; then
    echo -e "📦 Backing up backend/ella/..."
    cp -r "$BACKEND_DIR/ella" "$BACKUP_DIR/"
    echo -e "   ${GREEN}✓${NC} ella/ module"
else
    echo -e "   ${YELLOW}⚠${NC} backend/ella/ not found"
fi

# Backup routers/ella.py
if [ -f "$BACKEND_DIR/routers/ella.py" ]; then
    mkdir -p "$BACKUP_DIR/routers"
    cp "$BACKEND_DIR/routers/ella.py" "$BACKUP_DIR/routers/"
    echo -e "   ${GREEN}✓${NC} routers/ella.py"
fi

# Backup routers/voice_v2.py
if [ -f "$BACKEND_DIR/routers/voice_v2.py" ]; then
    mkdir -p "$BACKUP_DIR/routers"
    cp "$BACKEND_DIR/routers/voice_v2.py" "$BACKUP_DIR/routers/"
    echo -e "   ${GREEN}✓${NC} routers/voice_v2.py"
fi

# Backup utils/ella/
if [ -d "$BACKEND_DIR/utils/ella" ]; then
    mkdir -p "$BACKUP_DIR/utils"
    cp -r "$BACKEND_DIR/utils/ella" "$BACKUP_DIR/utils/"
    echo -e "   ${GREEN}✓${NC} utils/ella/"
fi

# Backup integrations/pipecat/
if [ -d "$BACKEND_DIR/integrations/pipecat" ]; then
    mkdir -p "$BACKUP_DIR/integrations"
    cp -r "$BACKEND_DIR/integrations/pipecat" "$BACKUP_DIR/integrations/"
    echo -e "   ${GREEN}✓${NC} integrations/pipecat/"
fi

# Save git state
echo ""
echo -e "📋 Saving git state..."
cd "$REPO_ROOT"
git rev-parse HEAD > "$BACKUP_DIR/git_commit.txt"
git branch --show-current > "$BACKUP_DIR/git_branch.txt"
git status --short > "$BACKUP_DIR/git_status.txt"
git diff > "$BACKUP_DIR/uncommitted_changes.patch" 2>/dev/null || true
echo -e "   ${GREEN}✓${NC} Git commit: $(cat $BACKUP_DIR/git_commit.txt | head -c 8)"
echo -e "   ${GREEN}✓${NC} Git branch: $(cat $BACKUP_DIR/git_branch.txt)"

# Create restore script
cat > "$BACKUP_DIR/restore.sh" << 'RESTORE_EOF'
#!/bin/bash
# Restore Ella files from this backup
# Usage: ./restore.sh /path/to/backend

BACKEND_DIR="${1:-/Users/greg/repos/omi/backend}"
BACKUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Restoring Ella files to: $BACKEND_DIR"

[ -d "$BACKUP_DIR/ella" ] && cp -r "$BACKUP_DIR/ella" "$BACKEND_DIR/"
[ -d "$BACKUP_DIR/routers" ] && cp -r "$BACKUP_DIR/routers/"* "$BACKEND_DIR/routers/"
[ -d "$BACKUP_DIR/utils" ] && cp -r "$BACKUP_DIR/utils/"* "$BACKEND_DIR/utils/"
[ -d "$BACKUP_DIR/integrations" ] && cp -r "$BACKUP_DIR/integrations/"* "$BACKEND_DIR/integrations/"

echo "Restore complete!"
RESTORE_EOF
chmod +x "$BACKUP_DIR/restore.sh"

# Summary
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    BACKUP COMPLETE                         ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Backup location: ${YELLOW}$BACKUP_DIR${NC}"
echo ""
echo "Contents:"
ls -la "$BACKUP_DIR"
echo ""
echo -e "To restore later:"
echo -e "  ${YELLOW}$BACKUP_DIR/restore.sh /Users/greg/repos/omi/backend${NC}"
echo ""
echo -e "${GREEN}Safe to proceed with upstream merge!${NC}"
