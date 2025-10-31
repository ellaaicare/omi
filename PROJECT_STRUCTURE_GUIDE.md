# OMI Project - Multi-Role Structure Guide

**Created**: October 30, 2025
**Status**: ✅ Role-based CLAUDE.md files configured

---

## 📁 **Project Directory → Role Mapping**

```
/Users/greg/repos/omi/
│
├── backend/                      → Backend Developer
│   ├── CLAUDE.md ✅              Backend-specific instructions
│   ├── routers/                  FastAPI endpoints
│   ├── utils/                    Utilities (TTS, STT, VAD)
│   ├── docs/                     Backend documentation
│   └── main.py                   FastAPI application
│
├── app/                          → iOS/Mobile Developer
│   ├── CLAUDE.md ✅              iOS/Flutter-specific instructions
│   ├── lib/                      Flutter/Dart code
│   │   ├── backend/              API clients
│   │   ├── pages/                UI screens
│   │   ├── providers/            State management
│   │   └── utils/                Utilities
│   ├── ios/                      Swift native code
│   └── pubspec.yaml              Flutter dependencies
│
├── omi/firmware/                 → Firmware Developer
│   ├── CLAUDE.md ✅              Firmware-specific instructions
│   ├── devkit/                   Friend Dev Kit 2 firmware
│   │   ├── src/                  C source code
│   │   └── prj.conf              Zephyr configuration
│   ├── modules/                  Reusable modules (Opus)
│   └── scripts/                  Build/flash scripts
│
└── docs/                         → Shared documentation
    └── (project-wide docs)
```

---

## 🚀 **How to Use Claude Code by Role**

### **Backend Development**
```bash
cd /Users/greg/repos/omi/backend
claude

# Claude will read: backend/CLAUDE.md
# Role: Backend Developer
# Focus: FastAPI, TTS API, VAD, STT, cloud deployment
```

### **iOS/Mobile Development**
```bash
cd /Users/greg/repos/omi/app
claude

# Claude will read: app/CLAUDE.md
# Role: iOS/Flutter Developer
# Focus: Flutter, Swift, BLE, API integration, UI/UX
```

### **Firmware Development**
```bash
cd /Users/greg/repos/omi/omi/firmware
claude

# Claude will read: omi/firmware/CLAUDE.md
# Role: Firmware Developer
# Focus: Embedded C, Zephyr, nRF5340, BLE, audio
```

---

## ❌ **What NOT to Do**

### **Don't Spawn at Root** (unless you need multi-role work)
```bash
# ❌ BAD: No clear role
cd /Users/greg/repos/omi
claude

# ✅ GOOD: Specific role
cd /Users/greg/repos/omi/backend
claude
```

**Why?** Root directory has no CLAUDE.md, so Claude won't know which role to assume.

---

## 🔀 **Session Forking Explained**

### **What Is Session Forking?**
- **Fork** = Create new conversation thread from existing session
- In Claude Code, you can fork a conversation to continue work in a new context

### **What Gets Inherited**
✅ Working directory
✅ File context (recently viewed files)
✅ Conversation summary

### **What Doesn't Get Inherited**
❌ CLAUDE.md re-reading (loaded once at startup)
❌ Full conversation history (only summary)

### **Best Practice for Forking**
If you fork a session and change roles:
1. **Exit the forked session**
2. **Spawn fresh Claude in the target role's directory**
3. This ensures clean CLAUDE.md loading

---

## 🗂️ **CLAUDE.md Locations**

### **Project-Specific (OMI)**
- `/Users/greg/repos/omi/backend/CLAUDE.md` ✅
- `/Users/greg/repos/omi/app/CLAUDE.md` ✅
- `/Users/greg/repos/omi/omi/firmware/CLAUDE.md` ✅

### **Other Projects**
- `/Users/greg/repos/CryptoTaxCalc/CLAUDE.md` ✅ (tax project)

### **Global (Removed)**
- `~/.claude/CLAUDE.md` ❌ **REMOVED** (was polluting all sessions)
- `~/.claude/CLAUDE.md.backup` 💾 (backup saved)

**Why removed?**
- Was CryptoTaxCalc-specific
- Polluted OMI sessions with tax calculation context
- CryptoTaxCalc already has its own project CLAUDE.md

---

## 📊 **Current Status**

### ✅ **Completed**
- Backend CLAUDE.md created
- iOS/Mobile CLAUDE.md created
- Firmware CLAUDE.md created
- Global CLAUDE.md removed (backed up)
- Role-based spawning guide documented

### 🎯 **Active Development**
- **Backend**: TTS API deployed, VAD enabled, M4 diarization ready
- **iOS**: Memories bug fixed, TTS integration next (4h ETA)
- **Firmware**: Speaker integration ongoing

---

## 🔗 **Inter-Team Coordination**

### **PM Agent**
- URL: http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages
- Contact: Use `/tmp/contact_pm_*.py` scripts
- Tasks tracked in PM memory system

### **Backend ↔ iOS**
- **Backend provides**: TTS API, STT, memories API
- **iOS consumes**: REST APIs, plays TTS audio, shows memories
- **Coordination**: PM assigns tasks, both report status

### **iOS ↔ Firmware**
- **iOS provides**: BLE GATT client
- **Firmware provides**: BLE GATT server, audio streaming
- **Coordination**: Service UUIDs must match

### **Backend ↔ Firmware**
- **Backend provides**: Audio processing (Deepgram STT)
- **Firmware provides**: Opus-encoded audio stream
- **Coordination**: Codec format must match (16 kHz, Opus)

---

## 🛠️ **Quick Reference Commands**

### **Backend Work**
```bash
cd /Users/greg/repos/omi/backend
claude

# Common tasks:
# - Deploy TTS features
# - Add API endpoints
# - Fix backend bugs
# - VPS deployment
```

### **iOS Work**
```bash
cd /Users/greg/repos/omi/app
claude

# Common tasks:
# - TTS integration
# - UI fixes
# - API integration
# - BLE connection
```

### **Firmware Work**
```bash
cd /Users/greg/repos/omi/omi/firmware
claude

# Common tasks:
# - Speaker development
# - BLE services
# - Power optimization
# - Build and flash
```

---

## 📞 **Getting Help**

### **Role-Specific Questions**
- Spawn Claude in the appropriate directory
- Claude will have role-specific context from CLAUDE.md

### **Cross-Role Coordination**
- Use PM agent to coordinate tasks
- Each role reports to PM
- PM tracks dependencies and ETAs

### **Multi-Role Tasks**
If a task requires multiple roles:
1. PM creates sub-tasks for each role
2. Each role completes their part
3. Coordinate via PM or direct communication

---

**Setup complete! All role-based CLAUDE.md files ready.**

**To work on a specific role, just `cd` to that directory and spawn Claude!**
