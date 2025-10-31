# OMI Project - Role Detection & PM Communication

**Important**: This is a multi-role project. Your specific role depends on which subdirectory you're in.

---

## 🎯 **DETECT YOUR ROLE**

Check your current working directory:

- **In `/Users/greg/repos/omi/backend`** → You are **Backend Developer**
  - Read: `backend/CLAUDE.md` for complete backend instructions
  - Role: `backend_dev`
  - Focus: FastAPI, TTS, STT, VAD, cloud deployment

- **In `/Users/greg/repos/omi/app`** → You are **iOS Developer**
  - Read: `app/CLAUDE.md` for complete iOS instructions
  - Role: `ios_dev`
  - Focus: Flutter, Swift, BLE, API integration, UI/UX

- **In `/Users/greg/repos/omi/omi/firmware`** → You are **Firmware Developer**
  - Read: `omi/firmware/CLAUDE.md` for complete firmware instructions
  - Role: `firmware_dev`
  - Focus: Embedded C, Zephyr, nRF5340, BLE, audio

- **At root** `/Users/greg/repos/omi/` → ⚠️ **No specific role**
  - Ask user: "Which component do you want to work on?"
  - Direct them to spawn in the appropriate subdirectory
  - **Do NOT** assume coordinator or any specific role

---

## 📞 **PM COMMUNICATION (All Roles)**

### **PM Agent Information**
- **PM Name**: Claude-PM (Project Manager)
- **API Endpoint**: `http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages`
- **Purpose**: Task coordination, status tracking, team communication

### **When to Contact PM**
1. **Session start** - Get current tasks and context
2. **Task completion** - Report what you finished
3. **Blockers** - Report issues preventing progress
4. **Questions** - Ask for clarification
5. **Handoffs** - Coordinate with other roles

### **PM Contact Script Template**

```python
#!/usr/bin/env python3
import requests
import json

url = "http://140.82.17.219:8284/v1/agents/agent-ddc2fdfd-fcdf-4417-a8df-36a6bfb404bb/messages"
headers = {"Content-Type": "application/json"}

data = {
    "messages": [{
        "role": "user",
        "content": """Agent: Claude-[YOUR-ROLE]-Developer
Role: [your_role]

Project: Ella AI Care / OMI [Component]
Folder: [your working directory]
Specialty: [your specialty]

Status: Just spawned, ready for tasks. What needs attention?

Recent context (if resuming):
- [List any context you have]

Questions for PM:
- What are current priorities?
- Any blockers or integration issues?
- Any coordination needed with other roles?"""
    }]
}

try:
    response = requests.post(url, headers=headers, json=data, timeout=30)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
```

### **Role-Specific PM Scripts**
- Backend: `/tmp/contact_pm_backend.py`
- iOS: `/tmp/contact_pm_ios.py`
- Firmware: `/tmp/contact_pm_firmware.py`

---

## 🚨 **IMPORTANT INSTRUCTIONS**

### **If spawned at root** (`/Users/greg/repos/omi/`):

1. **Check working directory**: `pwd`
2. **Ask user**: "I'm at the root level. Which component do you want to work on?"
   - Backend (FastAPI, TTS, VAD)?
   - iOS (Flutter, Swift, BLE)?
   - Firmware (nRF5340, Zephyr)?
3. **Direct user**: "Please respawn in the appropriate directory:"
   ```bash
   cd /Users/greg/repos/omi/[backend|app|omi/firmware]
   claude
   ```
4. **Do NOT**: Assume coordinator role or start working without role clarity

### **If spawned in subdirectory**:

1. **Read the subdirectory CLAUDE.md** for complete role instructions
2. **Identify your role** from that file (Backend/iOS/Firmware Developer)
3. **Contact PM** using the role-specific script
4. **Get priorities** and start working

---

## 📁 **Role-Specific CLAUDE.md Locations**

Complete instructions for each role:
- `backend/CLAUDE.md` - Backend Developer (FastAPI, TTS, VAD, deployment)
- `app/CLAUDE.md` - iOS Developer (Flutter, Swift, BLE, UI)
- `omi/firmware/CLAUDE.md` - Firmware Developer (C, Zephyr, nRF5340)

**Always prefer the subdirectory CLAUDE.md over this root file!**

---

## 🎯 **Quick Role Check**

If you're unsure which role you are:

```bash
# Check your working directory
pwd

# If output contains "backend" → Backend Developer
# If output contains "app" → iOS Developer
# If output contains "firmware" → Firmware Developer
# If output is just ".../omi" → At root, need direction
```

---

## 📊 **Current Project Status (High-Level)**

**Backend** ✅:
- TTS API deployed (https://api.ella-ai-care.com)
- VAD enabled
- Ready for iOS integration

**iOS** 🔄:
- Memories bug fixed
- TTS integration in progress
- Branch: feature/ios-backend-integration

**Firmware** 🔄:
- Speaker firmware complete (hardware testing needed)
- BLE services operational
- DevKit2 testing active

**For detailed status and priorities**: Contact PM agent

---

## 🛠️ **Recommended Workflow**

1. **Spawn in the correct subdirectory** (backend/app/firmware)
2. **Read that subdirectory's CLAUDE.md** (has complete role context)
3. **Run PM contact script** to get current tasks
4. **Start working** on assigned priorities
5. **Report back to PM** when done or blocked

---

**This is a minimal guide. For complete instructions, read the CLAUDE.md in your role's subdirectory!**

---

## 📝 **Git Commit Guidelines (All Roles)**

### **Before Committing**
1. **Check what changed**: `git status` and `git diff`
2. **Review your changes**: Make sure only your role's files are modified
3. **Test your changes**: Run tests if applicable
4. **Check branch**: Make sure you're on the right branch

### **Commit Message Format**
```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `refactor`: Code refactor (no behavior change)
- `test`: Adding tests
- `chore`: Maintenance (dependencies, config)

**Examples**:
```bash
# Good commit messages
git commit -m "feat(tts): add OpenAI TTS provider with Redis caching"
git commit -m "fix(ios): resolve memories display bug (model mismatch)"
git commit -m "feat(firmware): implement speaker I2S driver for nRF5340"
git commit -m "docs(backend): add TTS API deployment guide"

# Bad commit messages (avoid)
git commit -m "fix stuff"
git commit -m "wip"
git commit -m "updates"
```

### **What to Commit**
- ✅ **DO** commit: Your role's code changes
- ✅ **DO** commit: Documentation you created
- ✅ **DO** commit: Tests for your changes
- ❌ **DON'T** commit: Other roles' code (unless coordinated)
- ❌ **DON'T** commit: Secrets, API keys, credentials
- ❌ **DON'T** commit: Build artifacts, temporary files
- ❌ **DON'T** commit: Large binary files (unless necessary)

### **When to Commit**
- After completing a logical unit of work
- Before switching to a different task
- At the end of your session (if work is complete)
- When asked by PM to commit and push

### **Quick Commit Workflow**
```bash
# 1. Check status
git status

# 2. Review changes
git diff

# 3. Stage your files
git add path/to/your/files

# 4. Commit with message
git commit -m "type(scope): description"

# 5. Push to remote
git push origin your-branch-name
```

### **Multi-File Commits**
If you changed multiple files for one feature:
```bash
# Stage all related files
git add backend/routers/tts.py \
        backend/utils/tts/manager.py \
        backend/docs/TTS_API.md

# One commit for the complete feature
git commit -m "feat(tts): implement modular TTS API with provider abstraction"
```

### **DO NOT Push to Main Directly**
- Always work on a feature branch
- Create PR for code review
- Let PM merge to main

### **Current Branches**
- `main` - Production code (don't push directly)
- `feature/ios-backend-integration` - iOS work
- `feature/backend-infrastructure` - Backend work
- Create new branches as needed for firmware work

---
