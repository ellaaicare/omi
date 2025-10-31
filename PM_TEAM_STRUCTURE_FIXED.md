# PM Team Structure Confusion - RESOLVED

**Date**: October 31, 2025
**Issue**: PM thought there were 5+ developers instead of 3
**Status**: ✅ FIXED

---

## ❌ **What Was Wrong**

PM was tracking these "developers":
- ❌ Backend Developer
- ❌ Infrastructure & Integration Dev (doesn't exist!)
- ❌ QA/Coordinator (doesn't exist!)
- ❌ iOS Developer
- ❌ Firmware Developer
- ❌ Multiple other phantom roles

**Root cause**: Multiple Claude sessions were spawned from root directory before role structure was clear, causing PM to think each spawn was a different developer.

**Impact**: PM was assigning "infrastructure" tasks to non-existent "Infrastructure & Integration Dev" instead of Backend Developer. This confusion propagated to iOS dev's CLAUDE.md.

---

## ✅ **What's Fixed**

PM now understands there are **ONLY 3 DEVELOPERS**:

### **1. Backend Developer (backend_dev)**
- **Role**: Claude-Backend-Developer
- **Folder**: `/Users/greg/repos/omi/backend`
- **Contact**: `/tmp/contact_pm_backend.py`
- **Responsibilities**:
  - ✅ ALL backend work (FastAPI, APIs)
  - ✅ ALL infrastructure (VPS deployment, monitoring)
  - ✅ ALL integration (endpoints, webhooks, lookup APIs)
  - ✅ Database (Firestore)
  - ✅ TTS/STT/VAD
  - ✅ Rate limiting
  - ✅ Everything server-side

**KEY**: Backend = Infrastructure = Integration = ALL server work

### **2. iOS Developer (ios_dev)**
- **Role**: Claude-iOS-Developer
- **Folder**: `/Users/greg/repos/omi/app`
- **Contact**: `/tmp/contact_pm_ios.py`
- **Responsibilities**:
  - ✅ ALL mobile work (Flutter/Dart)
  - ✅ Native Swift code
  - ✅ BLE client
  - ✅ UI/UX
  - ✅ TTS e2e testing

### **3. Firmware Developer (firmware_dev)**
- **Role**: Claude-Firmware-Developer
- **Folder**: `/Users/greg/repos/omi/omi/firmware`
- **Contact**: `/tmp/contact_pm_firmware.py`
- **Responsibilities**:
  - ✅ ALL firmware work (nRF5340)
  - ✅ Embedded C, Zephyr
  - ✅ BLE GATT server
  - ✅ Audio drivers (mic/speaker)
  - ✅ Haptic control

### **4. Project Coordinator (temporary)**
- **NOT a permanent role**
- Only spawned at root for clarification/coordination
- Like this current session

---

## 📞 **What PM Did**

1. ✅ **Acknowledged** the correct team structure
2. ✅ **Re-assigned all tasks** previously labeled for "Infrastructure & Integration Dev" → Backend Developer
3. ✅ **Removed phantom roles** from memory
4. ✅ **Updated task tracking** to use only 3 developer roles
5. ✅ **Added agent registry** to PM memory (ella_pm_overview)

**PM's Response** (confirmed):
```
"Acknowledged — team structure updated and understood.

Actions I performed now:
- Re-assigned every task previously labeled for 'Infrastructure & Integration Dev'
  to Backend Developer.
- Removed duplicate/ambiguous agent identities from task assignments
- Updated ella_pm_tasks entries to reflect correct owner
- Backend Developer is the single owner of all server-side/infrastructure work"
```

---

## 🎯 **Going Forward**

### **When Developers Spawn**

Each dev spawns in their subdirectory:
```bash
# Backend work
cd /Users/greg/repos/omi/backend && claude
→ Backend Developer handles ALL server-side work

# iOS work
cd /Users/greg/repos/omi/app && claude
→ iOS Developer handles ALL mobile work

# Firmware work
cd /Users/greg/repos/omi/omi/firmware && claude
→ Firmware Developer handles ALL embedded work
```

### **PM Will Now**

- ✅ Assign backend/infra/integration tasks to **Backend Developer**
- ✅ Assign mobile tasks to **iOS Developer**
- ✅ Assign firmware tasks to **Firmware Developer**
- ✅ NOT create phantom "Infrastructure Dev" assignments
- ✅ Reference canonical agent registry in memory

### **Developers Will See**

- ✅ Clear task ownership
- ✅ No confusion about "infrastructure dev" vs "backend dev"
- ✅ All server-side work goes to one person (Backend Developer)

---

## 📋 **Updated iOS CLAUDE.md**

The iOS CLAUDE.md had incorrect references to "Infrastructure & Integration Dev" because it was updated based on PM's confused state. This should be corrected to reference only the 3 actual developers:

**OLD** (incorrect):
```
### **Infrastructure & Integration Dev**
- **Role**: Infra/integration
- **Contact for**: Lookup endpoint, infrastructure issues
```

**NEW** (correct):
```
### **Backend Developer** (Claude-Backend-Developer)
- **Role**: backend_dev
- **Location**: /Users/greg/repos/omi/backend
- **Contact for**: ALL backend work including infrastructure, APIs, deployment
- **Status**: TTS API operational, handling lookup endpoint
```

---

## ✅ **Verification**

**To verify PM understands, check his next response to a developer**:
- Should reference **Backend Developer** for all server work
- Should NOT mention "Infrastructure & Integration Dev"
- Should use only 3 developer roles

**To verify confusion is eliminated**:
- Spawn Backend Developer: `cd backend && claude`
- Contact PM: `python3 /tmp/contact_pm_backend.py`
- PM should assign all backend/infra work correctly

---

## 🚀 **Summary**

**What was wrong**: PM thought there were 5+ devs, created phantom "Infrastructure Dev" role

**What's fixed**: PM now knows there are exactly 3 devs, Backend = Infrastructure = Integration

**Impact**: Task assignments are now clear, no more confusion about role ownership

**Next**: Developers will get correct task assignments when they contact PM

---

**PM team structure confusion: RESOLVED ✅**

**Canonical team registry now in PM memory.**

**All backend/infra/integration tasks assigned to Backend Developer.**
