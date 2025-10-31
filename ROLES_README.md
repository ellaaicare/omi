# OMI Project - Role-Based Development

⚠️ **IMPORTANT**: This project uses role-based CLAUDE.md files in subdirectories.

## 🚀 **DO NOT spawn Claude at root level**

Claude Code will not know which role to assume if spawned here.

## ✅ **Instead, spawn in the appropriate subdirectory:**

```bash
# Backend work (FastAPI, TTS, STT, VAD)
cd /Users/greg/repos/omi/backend
claude

# iOS work (Flutter, Swift, BLE, UI)
cd /Users/greg/repos/omi/app
claude

# Firmware work (nRF5340, Zephyr, BLE)
cd /Users/greg/repos/omi/omi/firmware
claude
```

## 📁 **Role-Specific CLAUDE.md Locations**

- **Backend**: `backend/CLAUDE.md` → Claude-Backend-Developer
- **iOS**: `app/CLAUDE.md` → Claude-iOS-Developer
- **Firmware**: `omi/firmware/CLAUDE.md` → Claude-Firmware-Developer

## 📞 **PM Communication**

Each role has instructions for contacting the PM agent:
- Backend: `/tmp/contact_pm_backend.py`
- iOS: `/tmp/contact_pm_ios.py`
- Firmware: `/tmp/contact_pm_firmware.py`

**PM URL**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`

## 🗂️ **Backup Files**

- `CLAUDE.md.coordinator_backup` - Root coordinator role (removed to avoid confusion)
- `~/.claude/CLAUDE.md.backup` - Global CryptoTaxCalc instructions (moved to project-specific)

---

**TL;DR**: Always `cd` into `backend/`, `app/`, or `omi/firmware/` before spawning Claude!
