# PRD: Git Branch Consolidation

**Date**: November 29, 2025
**Priority**: Before Voice Mode Implementation

---

## Current State

```
BasedHardware/omi (upstream)
        │
        │ fork
        ▼
ellaaicare/omi (origin)
        │
        ├── main (our production)
        │
        └── feature/e2e-agent-testing-unified (current work)
                │
                ├── Ella integration
                ├── Edge ASR support
                ├── E2E testing endpoints
                ├── Assistant message tagging
                └── conversation_id in scanner payload
```

**Problem**: All our work is on feature branch, not merged to main.

---

## Recommended Merge Order

### Step 1: Merge Feature Branch to Our Main

```bash
# On ellaaicare/omi
git checkout main
git merge feature/e2e-agent-testing-unified
git push origin main
```

**Contains**:
- Ella AI integration (summary/memory agents)
- Edge ASR support
- E2E testing endpoints
- Assistant message tagging
- Scanner conversation_id

### Step 2: Deploy Main to VPS

```bash
# On VPS
cd /root/omi
git checkout main
git pull origin main
systemctl restart omi-backend
```

### Step 3: Create Voice Mode Branch (from main)

```bash
git checkout main
git checkout -b feature/voice-mode
# Implement voice mode here
```

---

## Upstream Sync (Separate Task)

**When**: After voice mode is stable
**Why**: Upstream deleted our TTS module and has significant changes

### Upstream Changes to Evaluate

| Change | Impact | Action |
|--------|--------|--------|
| TTS module deleted | High - we need it | **Keep ours** |
| LC3 codec added | Low - new codec support | Accept |
| conversation_processing.py changes | Medium - our Ella code there | Careful merge |
| memories.py changes | Medium - our Ella code there | Careful merge |
| iOS app changes | High - many conflicts | iOS team handles |

### Upstream Sync Process (When Ready)

```bash
# Create dedicated upstream sync branch
git checkout main
git checkout -b sync/upstream-nov-2025

# Attempt merge
git fetch upstream
git merge upstream/main

# Resolve conflicts (expect ~4 backend + ~12 iOS)
# Key: Keep our TTS module, Ella integration

# After conflicts resolved
git checkout main
git merge sync/upstream-nov-2025
```

---

## Recommendation

1. **Now**: Merge `feature/e2e-agent-testing-unified` → `main`
2. **Now**: Start voice mode from clean `main`
3. **Later**: Upstream sync as separate coordinated task (all teams)

---

## Branch Naming Convention (Going Forward)

```
main                           # Production
feature/voice-mode             # Voice mode implementation
feature/[name]                 # New features
fix/[name]                     # Bug fixes
sync/upstream-[date]           # Upstream sync attempts
```
