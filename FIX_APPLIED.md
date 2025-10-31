# Fix Applied - Backend Role Confusion

**Date**: October 30, 2025
**Issue**: Backend Claude was reading root CLAUDE.md instead of backend-specific one

---

## ❌ **Problem**

When spawning Claude from `/Users/greg/repos/omi/backend`:
- Expected: Backend Developer role with `contact_pm_backend.py`
- Actually got: Project Coordinator role with `contact_pm_coordinator.py`

**Root cause**: Claude Code searches UP the directory tree for CLAUDE.md files, and was reading `/Users/greg/repos/omi/CLAUDE.md` (root) instead of `/Users/greg/repos/omi/backend/CLAUDE.md`

---

## ✅ **Fix Applied**

1. **Replaced root CLAUDE.md** with lightweight role-detection version:
   - Old: 200+ lines, created "Project Coordinator" role (caused confusion)
   - New: Minimal guide that detects working directory and redirects
   - Backed up: `CLAUDE.md.coordinator_backup`

2. **New root CLAUDE.md behavior**:
   - ✅ Detects which subdirectory Claude is spawned in
   - ✅ Redirects to appropriate subdirectory CLAUDE.md
   - ✅ Provides PM communication basics for all roles
   - ✅ Does NOT assume any specific role
   - ✅ Asks user for direction if spawned at root

3. **Kept all role-specific files intact**:
   - ✅ `backend/CLAUDE.md` - Backend Developer (complete instructions)
   - ✅ `app/CLAUDE.md` - iOS Developer (complete instructions)
   - ✅ `omi/firmware/CLAUDE.md` - Firmware Developer (complete instructions)

---

## 🚀 **How to Use Now**

### **Always spawn in subdirectories**:

```bash
# Backend work
cd /Users/greg/repos/omi/backend
claude
# → Reads backend/CLAUDE.md
# → Role: Claude-Backend-Developer
# → Uses: /tmp/contact_pm_backend.py

# iOS work
cd /Users/greg/repos/omi/app
claude
# → Reads app/CLAUDE.md
# → Role: Claude-iOS-Developer
# → Uses: /tmp/contact_pm_ios.py

# Firmware work
cd /Users/greg/repos/omi/omi/firmware
claude
# → Reads omi/firmware/CLAUDE.md
# → Role: Claude-Firmware-Developer
# → Uses: /tmp/contact_pm_firmware.py
```

### **Never spawn at root**:
```bash
# ❌ DON'T DO THIS
cd /Users/greg/repos/omi
claude
# → No CLAUDE.md, no role context
```

---

## 🧪 **Test the Fix**

**Try spawning backend again**:
```bash
cd /Users/greg/repos/omi/backend
claude
```

**First thing Claude should do**:
1. Read `backend/CLAUDE.md`
2. Identify as "Claude-Backend-Developer"
3. Create `/tmp/contact_pm_backend.py` (not coordinator!)
4. Introduce itself as backend_dev to PM

**Expected PM introduction**:
```
Agent: Claude-Backend-Developer
Role: backend_dev

Project: Ella AI Care / OMI Backend (FastAPI/Python)
Folder: /Users/greg/repos/omi/backend
```

**NOT**:
```
Agent: Claude-Project-Coordinator  ❌ This was the bug
Role: project_coordinator          ❌
```

---

## 📋 **Current File Structure**

```
/Users/greg/repos/omi/
├── CLAUDE.md ✅                          # Lightweight role detector & PM comm guide
├── CLAUDE.md.coordinator_backup 💾       # Old coordinator role (backed up)
├── ROLES_README.md ✅                    # Simple guide
├── PROJECT_STRUCTURE_GUIDE.md ✅         # Documentation
├── FIX_APPLIED.md ✅                     # This file
│
├── backend/
│   └── CLAUDE.md ✅                      # COMPLETE Backend Developer instructions
│
├── app/
│   └── CLAUDE.md ✅                      # COMPLETE iOS Developer instructions
│
└── omi/firmware/
    └── CLAUDE.md ✅                      # COMPLETE Firmware Developer instructions
```

**Key change**: Root CLAUDE.md is now minimal (role detection + PM comm) instead of full coordinator role.

---

## 🎯 **Next Steps**

1. **Exit current backend session** (if still open)
2. **Respawn fresh** in backend directory:
   ```bash
   cd /Users/greg/repos/omi/backend
   claude
   ```
3. **Verify role**: Should see "Claude-Backend-Developer"
4. **Contact PM**: Run `python3 /tmp/contact_pm_backend.py`
5. **Get tasks**: PM will give you current backend priorities

---

## 🔍 **Why This Happened**

Claude Code's CLAUDE.md search behavior:
1. Starts in working directory
2. **Searches UP** the directory tree
3. May prioritize **root-level** CLAUDE.md over subdirectory ones
4. Result: Root file was being read even when spawned in backend/

**Solution**: Remove root CLAUDE.md entirely, only use subdirectory files.

---

**Fix confirmed! Backend Claude should now properly identify as Backend Developer.** ✅
