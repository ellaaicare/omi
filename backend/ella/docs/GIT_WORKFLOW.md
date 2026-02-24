# Ella Backend API - Safe Git Workflow for Upstream Merges

**CRITICAL**: Follow these instructions EXACTLY to avoid losing Ella code.

**Last Updated**: January 11, 2026

---

## TL;DR - Safe Upstream Pull

```bash
# 1. FIRST: Backup Ella files
./backend/ella/scripts/backup_ella.sh

# 2. Pull upstream
git fetch upstream
git merge upstream/main

# 3. Restore Ella files if needed
./backend/ella/scripts/restore_ella.sh

# 4. Test
python -c "from ella import ELLA_ENABLED; print(f'Ella OK: {ELLA_ENABLED}')"
```

---

## Understanding the Risk

### Files That Are SAFE (Won't Conflict)

These are Ella-only files that don't exist in upstream:

```
backend/ella/                    # SAFE - Ella only
backend/routers/ella.py          # SAFE - Ella only
backend/routers/voice_v2.py      # SAFE - Ella only (unless upstream adds same name)
backend/utils/ella/              # SAFE - Ella only
backend/integrations/pipecat/    # SAFE - Ella only
backend/docs/ELLA_*.md           # SAFE - Ella only
```

### Files That MAY Conflict

These upstream files have Ella hooks added:

```
backend/main.py                              # Has Ella import (5 lines)
backend/utils/llm/conversation_processing.py # Has Ella hook (~10 lines)
backend/utils/llm/memories.py                # Has Ella hook (~10 lines)
backend/routers/transcribe.py                # Has Ella hook (~8 lines)
```

---

## Pre-Merge Checklist

Before ANY upstream merge:

```bash
# 1. Check current branch
git branch --show-current
# Should be: main or feature/ella-*

# 2. Check for uncommitted changes
git status
# Should be clean, or commit first

# 3. Verify Ella files exist
ls -la backend/ella/
ls -la backend/routers/ella.py
ls -la backend/utils/ella/

# 4. Create backup branch
git checkout -b backup/ella-$(date +%Y%m%d-%H%M%S)
git checkout -  # Return to previous branch
```

---

## Option A: Safe Merge (Recommended)

### Step 1: Create Backup Branch

```bash
cd /Users/greg/repos/omi

# Create backup of current state
git checkout -b backup/pre-upstream-merge-$(date +%Y%m%d)
git checkout main
```

### Step 2: Stash Ella Hook Changes

```bash
# If you have uncommitted Ella work
git stash push -m "Ella work in progress"
```

### Step 3: Fetch and Review Upstream

```bash
# Fetch upstream
git fetch upstream

# See what's coming
git log --oneline main..upstream/main | head -20

# Check for conflicts with Ella files
git diff main upstream/main -- backend/main.py
git diff main upstream/main -- backend/utils/llm/
git diff main upstream/main -- backend/routers/transcribe.py
```

### Step 4: Merge Upstream

```bash
# Merge with explicit conflict markers
git merge upstream/main --no-commit

# Check status
git status
```

### Step 5: Resolve Any Conflicts

If conflicts in hook files:

```bash
# Open conflicted file
code backend/main.py  # or your editor

# Look for:
# <<<<<<< HEAD (your Ella code)
# =======
# >>>>>>> upstream/main (upstream code)

# Keep BOTH - upstream code + Ella hook
# See ella/docs/UPSTREAM_HOOKS.md for exact hook code
```

### Step 6: Verify Ella Still Works

```bash
# Test import
cd backend
python -c "from ella import register_ella_extensions; print('OK')"

# Test adapters
python -c "from ella import get_adapter; print(get_adapter('summary'))"
```

### Step 7: Complete Merge

```bash
git add .
git commit -m "merge: upstream/main with Ella hooks preserved"
```

---

## Option B: Nuclear Option (Fresh Upstream + Restore Ella)

If merge is too messy, start fresh:

### Step 1: Backup ALL Ella Files

```bash
cd /Users/greg/repos/omi

# Create backup directory
mkdir -p /tmp/ella-backup-$(date +%Y%m%d)
BACKUP_DIR=/tmp/ella-backup-$(date +%Y%m%d)

# Copy all Ella files
cp -r backend/ella $BACKUP_DIR/
cp backend/routers/ella.py $BACKUP_DIR/
cp backend/routers/voice_v2.py $BACKUP_DIR/
cp -r backend/utils/ella $BACKUP_DIR/utils_ella
cp -r backend/integrations/pipecat $BACKUP_DIR/

# Save list of modified files
git diff --name-only upstream/main > $BACKUP_DIR/modified_files.txt

echo "Backup saved to: $BACKUP_DIR"
```

### Step 2: Reset to Upstream

```bash
# Create new branch from upstream
git checkout -b fresh-upstream upstream/main
```

### Step 3: Restore Ella Files

```bash
BACKUP_DIR=/tmp/ella-backup-$(date +%Y%m%d)

# Restore Ella module
cp -r $BACKUP_DIR/ella backend/

# Restore routers
cp $BACKUP_DIR/ella.py backend/routers/
cp $BACKUP_DIR/voice_v2.py backend/routers/

# Restore utils/ella
cp -r $BACKUP_DIR/utils_ella backend/utils/ella

# Restore pipecat integration
cp -r $BACKUP_DIR/pipecat backend/integrations/
```

### Step 4: Re-apply Upstream Hooks

Follow `ella/docs/UPSTREAM_HOOKS.md` to add hooks to:
- `backend/main.py`
- `backend/utils/llm/conversation_processing.py`
- `backend/utils/llm/memories.py`
- `backend/routers/transcribe.py`

### Step 5: Test and Commit

```bash
# Test
python -c "from ella import ELLA_ENABLED; print(f'Ella: {ELLA_ENABLED}')"

# Commit
git add .
git commit -m "feat: restore Ella extensions on fresh upstream"
```

---

## Ella Files Inventory

### MUST PRESERVE (Ella-only, no upstream equivalent)

```
backend/ella/                           # Ella module root
backend/ella/__init__.py                # Extension loader
backend/ella/config.py                  # Configuration
backend/ella/adapters/                  # n8n adapters
backend/ella/routers/                   # Ella endpoints
backend/ella/services/                  # Business logic
backend/ella/docs/                      # Documentation

backend/routers/ella.py                 # 1,034 lines - callbacks
backend/routers/voice_v2.py             # 149 lines - Grok V2V

backend/utils/ella/                     # Legacy adapter location
backend/utils/ella/__init__.py
backend/utils/ella/config.py
backend/utils/ella/summary.py
backend/utils/ella/memory.py
backend/utils/ella/scanner.py

backend/integrations/pipecat/           # Voice pipeline
backend/integrations/pipecat/pipeline/grok_v2v_pipeline.py
backend/integrations/pipecat/services/n8n_client.py
backend/integrations/pipecat/pipeline/config.py
```

### UPSTREAM FILES WITH ELLA HOOKS

```
backend/main.py
    Lines added: ~5
    Hook: "from ella import register_ella_extensions"

backend/utils/llm/conversation_processing.py
    Lines added: ~10
    Hook: Ella summary adapter call

backend/utils/llm/memories.py
    Lines added: ~10
    Hook: Ella memory adapter call

backend/routers/transcribe.py
    Lines added: ~8
    Hook: Ella scanner call
```

---

## Emergency Recovery

If you accidentally deleted Ella files:

```bash
# Check if in git history
git log --all --full-history -- backend/ella/

# Restore from specific commit
git checkout <commit-hash> -- backend/ella/

# Or restore from backup branch
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/ella/
```

---

## Automated Backup Script

Save this as `backend/ella/scripts/backup_ella.sh`:

```bash
#!/bin/bash
# Backup all Ella files before upstream merge

BACKUP_DIR="/tmp/ella-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "Backing up Ella files to: $BACKUP_DIR"

# Copy all Ella code
cp -r backend/ella "$BACKUP_DIR/" 2>/dev/null
cp backend/routers/ella.py "$BACKUP_DIR/" 2>/dev/null
cp backend/routers/voice_v2.py "$BACKUP_DIR/" 2>/dev/null
cp -r backend/utils/ella "$BACKUP_DIR/utils_ella" 2>/dev/null
cp -r backend/integrations/pipecat "$BACKUP_DIR/" 2>/dev/null

# Save git state
git rev-parse HEAD > "$BACKUP_DIR/git_commit.txt"
git diff > "$BACKUP_DIR/uncommitted_changes.patch"

echo "Backup complete: $BACKUP_DIR"
echo "Files backed up:"
ls -la "$BACKUP_DIR"
```

---

## Summary

| Action | Command |
|--------|---------|
| Backup Ella | `./backend/ella/scripts/backup_ella.sh` |
| Fetch upstream | `git fetch upstream` |
| Preview changes | `git diff main upstream/main -- backend/` |
| Safe merge | `git merge upstream/main` |
| Check conflicts | `git status` |
| Test Ella | `python -c "from ella import ELLA_ENABLED; print(ELLA_ENABLED)"` |
| Emergency restore | `git checkout backup/ella-YYYYMMDD -- backend/ella/` |

---

*Always backup before merge. When in doubt, use Option B (fresh restore).*
