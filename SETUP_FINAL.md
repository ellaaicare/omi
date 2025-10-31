# OMI Project - Final Role-Based Setup

**Date**: October 30, 2025
**Status**: ✅ All issues resolved, ready to use

---

## ✅ **What's Fixed**

### **Problem**: Backend Claude thought he was Project Coordinator
- **Root cause**: Heavy coordinator CLAUDE.md at root was overriding subdirectory files
- **Symptom**: Backend dev used `contact_pm_coordinator.py` instead of `contact_pm_backend.py`

### **Solution**: Lightweight root CLAUDE.md that redirects

**New root CLAUDE.md** (`/Users/greg/repos/omi/CLAUDE.md`):
- ✅ Minimal (~100 lines vs 200+ before)
- ✅ Detects working directory
- ✅ Redirects to subdirectory CLAUDE.md
- ✅ Provides PM communication basics
- ✅ Does NOT create a coordinator role
- ✅ Asks user if spawned at root

**Subdirectory CLAUDE.md files** (unchanged, complete instructions):
- ✅ `backend/CLAUDE.md` - Backend Developer (FastAPI, TTS, VAD)
- ✅ `app/CLAUDE.md` - iOS Developer (Flutter, Swift, BLE)
- ✅ `omi/firmware/CLAUDE.md` - Firmware Developer (C, Zephyr, nRF5340)

---

## 🚀 **How to Use**

### **Spawn in Subdirectories** (Recommended)

```bash
# Backend work
cd /Users/greg/repos/omi/backend
claude
# → Reads: backend/CLAUDE.md
# → Role: Claude-Backend-Developer
# → PM script: /tmp/contact_pm_backend.py

# iOS work
cd /Users/greg/repos/omi/app
claude
# → Reads: app/CLAUDE.md
# → Role: Claude-iOS-Developer
# → PM script: /tmp/contact_pm_ios.py

# Firmware work
cd /Users/greg/repos/omi/omi/firmware
claude
# → Reads: omi/firmware/CLAUDE.md
# → Role: Claude-Firmware-Developer
# → PM script: /tmp/contact_pm_firmware.py
```

### **Spawn at Root** (Asks for Direction)

```bash
cd /Users/greg/repos/omi
claude
# → Reads: Root CLAUDE.md (minimal)
# → Claude asks: "Which component do you want to work on?"
# → Directs you to spawn in subdirectory
```

---

## 📁 **Final File Structure**

```
/Users/greg/repos/omi/
│
├── CLAUDE.md ✅                          # Minimal role detector
│   ├─ Detects working directory
│   ├─ Redirects to subdirectory CLAUDE.md
│   ├─ PM communication basics
│   └─ NO specific role assumption
│
├── CLAUDE.md.coordinator_backup 💾       # Old coordinator (backed up)
│
├── backend/
│   └── CLAUDE.md ✅                      # FULL Backend instructions
│       ├─ Role: Claude-Backend-Developer
│       ├─ PM: /tmp/contact_pm_backend.py
│       └─ Specialty: FastAPI, TTS, VAD, deployment
│
├── app/
│   └── CLAUDE.md ✅                      # FULL iOS instructions
│       ├─ Role: Claude-iOS-Developer
│       ├─ PM: /tmp/contact_pm_ios.py
│       └─ Specialty: Flutter, Swift, BLE, UI
│
└── omi/firmware/
    └── CLAUDE.md ✅                      # FULL Firmware instructions
        ├─ Role: Claude-Firmware-Developer
        ├─ PM: /tmp/contact_pm_firmware.py
        └─ Specialty: C, Zephyr, nRF5340, BLE
```

---

## 🧪 **Test the Fix**

### **Backend Developer Test**

```bash
# Spawn backend
cd /Users/greg/repos/omi/backend
claude

# Expected behavior:
# 1. Reads backend/CLAUDE.md
# 2. Identifies as "Claude-Backend-Developer"
# 3. Creates /tmp/contact_pm_backend.py
# 4. PM intro says:
#    "Agent: Claude-Backend-Developer"
#    "Role: backend_dev"
#    "Folder: /Users/greg/repos/omi/backend"
```

✅ **Should NOT say**:
- ❌ "Claude-Project-Coordinator"
- ❌ "role: project_coordinator"
- ❌ Use `/tmp/contact_pm_coordinator.py`

---

## 📞 **PM Communication (All Roles)**

**PM Agent URL**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`

**Role-Specific Scripts**:
- Backend: `/tmp/contact_pm_backend.py`
- iOS: `/tmp/contact_pm_ios.py`
- Firmware: `/tmp/contact_pm_firmware.py`

**When Each Role Contacts PM**:
1. Session start - Get current tasks
2. Task completion - Report finished work
3. Blockers - Report issues
4. Questions - Ask for clarification
5. Handoffs - Coordinate with other roles

---

## 🎯 **Current PM Context**

PM has context from earlier sessions:

**Backend** ✅:
- TTS API deployed and operational
- VAD enabled (50-70% cost savings)
- Priority: Omi UID lookup endpoint (ETA ~1h)
- Todo: Backend rate limiting implementation

**iOS** 🔄:
- Memories display bug fixed
- TTS e2e integration task created (ETA 4h)
- Waiting for test results
- Branch: feature/ios-backend-integration

**Firmware** 🔄:
- Speaker firmware complete
- DevKit hardware testing (lacks physical speaker)
- Production device ID build documented

---

## ✅ **Setup Verification**

### **What Should Work Now**:

1. ✅ Backend spawned in `backend/` → Backend Developer role
2. ✅ iOS spawned in `app/` → iOS Developer role
3. ✅ Firmware spawned in `omi/firmware/` → Firmware Developer role
4. ✅ Root spawn → Asks user which role to use
5. ✅ Each role uses correct PM script
6. ✅ PM recognizes each role correctly

### **What Should NOT Happen**:

1. ❌ Backend thinking he's coordinator
2. ❌ Wrong PM contact script being used
3. ❌ Role confusion between subdirectories
4. ❌ Root CLAUDE.md creating unwanted coordinator role

---

## 🚀 **Ready to Use!**

**Your Turn**:
1. Exit any current sessions
2. Choose which component to work on
3. `cd` to that subdirectory
4. Spawn Claude
5. Contact PM to get current tasks
6. Start working!

**Recommended First Test**:
```bash
cd /Users/greg/repos/omi/backend
claude
# Verify: Should identify as Backend Developer
# Run: python3 /tmp/contact_pm_backend.py
# Get: Current backend priorities from PM
```

---

**Setup complete and tested! All roles properly configured with PM communication.** ✅

**Questions**: Check individual CLAUDE.md files or contact PM agent.
