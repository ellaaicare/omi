# ADMIN: Safe Upstream Merge Instructions

**For**: Any developer doing `git pull upstream` or merging vanilla OMI code
**Purpose**: Keep Ella backend extensions safe during upstream updates

---

## QUICK REFERENCE

```bash
# BEFORE upstream merge - RUN THIS FIRST:
cd /Users/greg/repos/omi
./backend/ella/scripts/backup_ella.sh

# AFTER upstream merge - verify Ella works:
cd backend
python -c "from ella import ELLA_ENABLED; print(f'Ella: {ELLA_ENABLED}')"

# IF BROKEN - restore from backup:
/tmp/ella-backup-YYYYMMDD-HHMMSS/restore.sh /Users/greg/repos/omi/backend
```

---

## WHAT IS ELLA?

Ella is our **custom backend extensions** for the OMI fork. It adds:

1. **n8n/Letta Integration** - Summaries & memories via Letta agents
2. **Voice V2 (Grok)** - Ultra-low latency voice conversations
3. **Audio Notifications** - Push notifications with TTS playback
4. **E2E Testing** - Testing endpoints for iOS team

**All Ella code lives in**: `backend/ella/` + a few other locations

---

## ELLA FILE LOCATIONS

### Primary Location (NEW - consolidated)
```
backend/ella/                    # Main Ella module
├── __init__.py                  # Extension loader
├── config.py                    # Configuration
├── adapters/                    # n8n adapters
├── routers/                     # API endpoints
├── services/                    # Business logic
├── scripts/                     # Utility scripts
└── docs/                        # Documentation
```

### Legacy Locations (still in use during migration)
```
backend/routers/ella.py          # n8n callback endpoints
backend/routers/voice_v2.py      # Grok V2V endpoint
backend/utils/ella/              # n8n adapter functions
backend/integrations/pipecat/    # Voice pipeline
```

---

## STEP-BY-STEP: SAFE UPSTREAM MERGE

### Step 1: Backup Ella (REQUIRED)

```bash
cd /Users/greg/repos/omi
./backend/ella/scripts/backup_ella.sh
```

This creates: `/tmp/ella-backup-YYYYMMDD-HHMMSS/`

### Step 2: Fetch Upstream

```bash
git fetch upstream
```

### Step 3: Check What's Coming

```bash
# See changed files
git diff --name-only main upstream/main | grep backend/

# Check for conflicts with Ella files
git diff main upstream/main -- backend/main.py | head -50
```

### Step 4: Merge

```bash
git merge upstream/main
```

### Step 5: Resolve Conflicts (if any)

If conflict in `backend/main.py`:
- Keep BOTH upstream code AND Ella import
- See `backend/ella/docs/UPSTREAM_HOOKS.md` for exact code

### Step 6: Verify Ella Works

```bash
cd backend
python -c "from ella import ELLA_ENABLED; print(f'Ella: {ELLA_ENABLED}')"

# Should print: Ella: True
```

### Step 7: Test Server Starts

```bash
cd backend
source venv/bin/activate
python -c "from main import app; print('Server OK')"
```

---

## IF SOMETHING BREAKS

### Option 1: Restore from Backup

```bash
# Find your backup
ls /tmp/ella-backup-*

# Restore
/tmp/ella-backup-YYYYMMDD-HHMMSS/restore.sh /Users/greg/repos/omi/backend
```

### Option 2: Restore from Git

```bash
# Find backup branch
git branch | grep backup

# Restore Ella files from backup branch
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/ella/
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/routers/ella.py
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/routers/voice_v2.py
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/utils/ella/
git checkout backup/pre-upstream-merge-YYYYMMDD -- backend/integrations/pipecat/
```

### Option 3: Re-add Hooks Manually

If Ella files are fine but hooks are missing, see:
`backend/ella/docs/UPSTREAM_HOOKS.md`

---

## FILES THAT NEED ELLA HOOKS

These upstream files have small Ella additions (~5-10 lines each):

| File | Hook Purpose |
|------|--------------|
| `backend/main.py` | Import and register Ella extensions |
| `backend/utils/llm/conversation_processing.py` | Route to n8n for summaries |
| `backend/utils/llm/memories.py` | Route to n8n for memories |
| `backend/routers/transcribe.py` | Send to scanner agent |

If these get overwritten by upstream, re-add hooks from `UPSTREAM_HOOKS.md`.

---

## DO NOT DELETE THESE FILES

```
backend/ella/                    # NEVER DELETE
backend/routers/ella.py          # NEVER DELETE
backend/routers/voice_v2.py      # NEVER DELETE
backend/utils/ella/              # NEVER DELETE
backend/integrations/pipecat/    # NEVER DELETE
```

---

## ENVIRONMENT VARIABLES

Ella can be disabled without deleting code:

```bash
# Disable all Ella features
ELLA_ENABLED=false

# Disable specific features
ELLA_SUMMARY_ENABLED=false
ELLA_VOICE_V2_ENABLED=false
```

---

## CONTACT

If you break something or need help:

1. Check backup exists: `ls /tmp/ella-backup-*`
2. Check git history: `git log --oneline backend/ella/`
3. Read docs: `backend/ella/docs/`

---

## CHECKLIST

Before upstream merge:
- [ ] Ran backup script
- [ ] Noted backup location
- [ ] Committed any local Ella changes

After upstream merge:
- [ ] Resolved any conflicts
- [ ] Verified `from ella import ELLA_ENABLED` works
- [ ] Tested server starts
- [ ] Pushed merged code

---

*Last Updated: January 11, 2026*
