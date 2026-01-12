# Pre-Upstream Pull Review

**Date**: January 11, 2026
**Reviewer**: iOS Developer (Final Review Session)
**Status**: REVIEW COMPLETE - Action Items Identified

---

## Executive Summary

Both teams have done solid preparation work. The Ella modules are well-structured and isolated. **However, the iOS team's work is NOT committed yet**, which is a critical issue before the upstream pull.

### Status Matrix

| Component | Committed | Backup | Ready |
|-----------|-----------|--------|-------|
| Backend `ella/` module | ✅ Yes (159cd9364) | ✅ Script exists | ✅ Ready |
| iOS `lib/ella/` extensions | ❌ **NOT committed** | ✅ In backup folder | ⚠️ Need commit |
| iOS `Runner/Ella/` plugins | ❌ **NOT committed** | ✅ In backup folder | ⚠️ Need commit |
| Backup folders | ❌ **NOT committed** | N/A | ⚠️ Need commit |
| Scripts | ❌ **NOT committed** | N/A | ⚠️ Need commit |

---

## Team Work Review

### Backend Team ✅ GOOD

**Committed**: Yes (commit 159cd9364)

**Structure Created**:
```
backend/ella/
├── __init__.py              # Extension loader with ELLA_ENABLED flag
├── config.py                # Centralized configuration
├── ADMIN_INSTRUCTIONS.md    # Quick reference for merges
├── README.md                # Complete documentation
├── adapters/                # n8n adapters
├── routers/                 # API endpoints
├── services/                # Business logic
├── scripts/backup_ella.sh   # Backup script
└── docs/
    ├── GIT_WORKFLOW.md
    ├── UPSTREAM_HOOKS.md
    └── MIGRATION_CHECKLIST.md
```

**Strengths**:
- Clean module structure
- Backup script ready
- Well-documented hook points
- ELLA_ENABLED feature flag for safe disable

**No issues identified.**

---

### iOS Team ⚠️ NEEDS COMMIT

**Committed**: NO - All files are untracked!

**Structure Created**:
```
app/lib/ella/
├── extensions.dart              # Main entry point
├── ELLA_EXTENSIONS_README.md    # Documentation
├── config/
│   └── ella_config.dart         # Settings
└── plugins/
    ├── base_plugin.dart         # Plugin interface
    ├── wake_word/               # Wake word plugin (skeleton)
    ├── voice_v2v/               # V2V plugin (skeleton)
    ├── tts/                     # TTS plugin (ready)
    └── audio_push/              # Audio push plugin (skeleton)

app/ios/Runner/Ella/
├── WakeWordPlugin.swift         # Native wake word (skeleton)
├── VoiceV2VPlugin.swift         # Native V2V (skeleton)
├── NativeTtsPlugin.swift        # Native TTS (ready)
└── AudioPushPlugin.swift        # Native audio push (skeleton)

app/ella_extensions_backup/      # Backup folder
app/scripts/reapply_ella_extensions.sh
app/UPSTREAM_PULL_INSTRUCTIONS.md
```

**Strengths**:
- Clean plugin architecture
- Good separation from OMI core
- Backup folder ready
- Detailed instructions

**Issues**:
1. **CRITICAL: Nothing is committed!** All files are untracked
2. Plugins are skeletons - need implementation ported
3. `ella_extensions_backup/` must be committed to survive pull

---

## Gaps & Concerns

### 1. CRITICAL: iOS Files Not Committed

The following must be committed BEFORE upstream pull:

```bash
# Untracked files that MUST be committed:
app/lib/ella/                          # Dart plugins
app/ios/Runner/Ella/                   # Swift plugins
app/ella_extensions_backup/            # Backup (critical!)
app/scripts/reapply_ella_extensions.sh # Reapply script
app/UPSTREAM_PULL_INSTRUCTIONS.md      # Instructions
```

**If not committed, these will be lost when switching to upstream branch!**

### 2. Modified Files Not Committed

These files have uncommitted changes:

| File | Changes | Action Needed |
|------|---------|---------------|
| `developer.dart` | +56 lines (Ella UI) | Commit or capture in plugin |
| `capture_provider.dart` | +13 lines | Review if Ella-specific |
| `voice_mode_v2_service.dart` | +9 lines | Review if Ella-specific |
| `heuristics_service.dart` | +25 lines | Review if Ella-specific |
| `on_device_asr_service.dart` | +4 lines | Review if Ella-specific |
| `preferences.dart` | +5 lines | Review if Ella-specific |

**These changes will be LOST when pulling from upstream!**

### 3. AppDelegate Changes Not Captured

The current `AppDelegate.swift` (49KB) has significant Ella changes that need to be in the Ella plugins:

- Audio push handling
- Firebase messaging delegate
- Background audio player
- Native plugin registration

**The iOS plugins are skeletons** - the actual implementation from AppDelegate needs to be ported.

### 4. Empty Swift Files

```
app/ios/Runner/OnDeviceASRService.swift  (0 bytes)
app/ios/Runner/ParakeetASRService.swift  (0 bytes)
```

These appear to be placeholder files. Decide if they should be deleted or populated.

---

## Recommended Actions Before Upstream Pull

### Step 1: Commit iOS Ella Extensions (CRITICAL)

```bash
cd /Users/greg/repos/omi

# Stage iOS Ella files
git add app/lib/ella/
git add app/ios/Runner/Ella/
git add app/ella_extensions_backup/
git add app/scripts/reapply_ella_extensions.sh
git add app/UPSTREAM_PULL_INSTRUCTIONS.md
git add app/AGENTS.md

# Commit
git commit -m "feat(ella): add iOS Ella extensions with plugin architecture

- Add lib/ella/ with plugin framework and 4 plugins
- Add ios/Runner/Ella/ with native Swift plugins
- Add backup folder for safe upstream pulls
- Add reapply script and instructions

Plugins:
- WakeWordPlugin (skeleton)
- VoiceV2VPlugin (skeleton)
- EllaTtsPlugin (ready)
- AudioPushPlugin (skeleton)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

### Step 2: Review & Commit Modified Files

For each modified file, decide:
- **Commit**: If the change is important and should be kept
- **Stash**: If you want to save but not commit
- **Discard**: If the change is not needed

```bash
# Review changes
git diff app/lib/pages/settings/developer.dart
git diff app/lib/providers/capture_provider.dart
git diff app/lib/services/voice_mode_v2/voice_mode_v2_service.dart

# Option A: Commit all changes
git add -A
git commit -m "chore: commit local changes before upstream pull"

# Option B: Stash for later
git stash save "pre-upstream-pull-changes"

# Option C: Discard (careful!)
git checkout -- app/lib/...
```

### Step 3: Port AppDelegate Code to Plugins

Before or after upstream pull, the iOS plugins need actual implementation:

| Plugin | Port From | Priority |
|--------|-----------|----------|
| AudioPushPlugin | AppDelegate audio handling | HIGH |
| NativeTtsPlugin | Already has implementation | DONE |
| WakeWordPlugin | Standalone Ella app | MEDIUM |
| VoiceV2VPlugin | Standalone Ella app | MEDIUM |

### Step 4: Commit .gitignore Changes

```bash
git add .gitignore
git add app/.gitignore
git commit -m "chore: add docs folders to gitignore"
```

### Step 5: Push All Changes

```bash
git push origin main
```

---

## Pre-Pull Checklist

### Must Do (Blocking)

- [ ] Commit iOS `lib/ella/` extensions
- [ ] Commit iOS `Runner/Ella/` native plugins
- [ ] Commit `ella_extensions_backup/` folder
- [ ] Commit `scripts/reapply_ella_extensions.sh`
- [ ] Commit `UPSTREAM_PULL_INSTRUCTIONS.md`
- [ ] Commit `.gitignore` changes
- [ ] Push all commits to origin

### Should Do (Important)

- [ ] Review and commit modified Dart files
- [ ] Create backup branch: `git checkout -b backup/pre-upstream-$(date +%Y%m%d)`
- [ ] Verify backend Ella module works: `python -c "from ella import ELLA_ENABLED"`
- [ ] Run backend backup script: `./backend/ella/scripts/backup_ella.sh`

### Nice to Have

- [ ] Port AudioPushPlugin implementation from AppDelegate
- [ ] Delete empty Swift files (OnDeviceASRService.swift, ParakeetASRService.swift)
- [ ] Update FORK_MIGRATION_PROPOSAL.md with current status

---

## Verification Commands

After commits, verify everything is tracked:

```bash
# Check nothing important is untracked
git status

# Verify Ella files are committed
git log --oneline -5

# Verify backup folder exists
ls -la app/ella_extensions_backup/

# Verify backend works
cd backend && python -c "from ella import ELLA_ENABLED; print(f'Ella: {ELLA_ENABLED}')"
```

---

## Post-Commit: Ready for Upstream Pull

Once all items are committed, follow:

1. **iOS**: `app/UPSTREAM_PULL_INSTRUCTIONS.md`
2. **Backend**: `backend/ella/ADMIN_INSTRUCTIONS.md`

---

## Summary

| Item | Status | Action |
|------|--------|--------|
| Backend Ella module | ✅ Ready | None |
| iOS Ella extensions | ⚠️ Not committed | **COMMIT NOW** |
| Modified files | ⚠️ Uncommitted | Review & decide |
| Backup folders | ⚠️ Not committed | **COMMIT NOW** |
| Documentation | ✅ Good | None |

**Bottom Line**: The architecture is solid, but **iOS files must be committed before upstream pull** or they will be lost!

---

*End of Pre-Upstream Pull Review*
