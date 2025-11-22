# Conversation ID Fix - Upstream Merge Guide

**Date**: November 22, 2025
**Commit**: `dcf05f8c4`
**Issue**: conversation_id was missing from n8n calls since Ella integration (commit `acf4505ac`)
**Branch**: `feature/e2e-agent-testing-unified`

---

## 🔍 **Root Cause**

**Upstream** (BasedHardware/omi):
- Only has local LLM memory extraction
- No n8n/Letta integration at all

**We Added** (commit `acf4505ac`):
- n8n integration for memory extraction
- **BUT**: Forgot to pass `conversation_id` parameter
- Result: n8n rejected requests, backend fell back to local LLM

**Evidence**:
- Live app was working = using LOCAL LLM fallback (not n8n!)
- Test endpoints failing = n8n schema validation errors
- This was incomplete implementation from day 1, NOT a regression

---

## ✅ **What We Fixed**

### **1. Live Flow** (`utils/llm/memories.py`)

**Function Signature** (Line 45-51):
```python
def new_memories_extractor(
    uid: str,
    segments: List[TranscriptSegment],
    conversation_id: Optional[str] = None,  # ← ADDED
    user_name: Optional[str] = None,
    memories_str: Optional[str] = None
) -> List[Memory]:
```

**n8n Call** (Line 78-86):
```python
response = requests.post(
    "https://n8n.ella-ai-care.com/webhook/memory-agent",
    json={
        "uid": uid,
        "conversation_id": conversation_id,  # ← ADDED
        "segments": segments_data
    },
    timeout=120
)
```

**Fallback Disabled** (Lines 105-115):
```python
else:
    print(f"❌ Ella memory agent returned status {response.status_code}", flush=True)
    print(f"❌ Response: {response.text[:500]}", flush=True)
    print(f"❌ FALLBACK DISABLED - Returning empty to avoid confusion", flush=True)
    return []  # ← No more local LLM fallback!

except Exception as e:
    print(f"❌ Ella memory agent failed: {e}", flush=True)
    print(f"❌ FALLBACK DISABLED - Returning empty to avoid confusion", flush=True)
    print(f"💡 Check: Is conversation_id being passed? uid={uid}, conversation_id={conversation_id}", flush=True)
    return []  # ← Return empty, don't fall back
```

---

### **2. Live Flow Caller** (`utils/conversations/process_conversation.py`)

**Before** (Line 307):
```python
new_memories = new_memories_extractor(uid, conversation.transcript_segments)
```

**After** (Lines 307-311):
```python
new_memories = new_memories_extractor(
    uid,
    conversation.transcript_segments,
    conversation_id=conversation.id  # ← ADDED
)
```

---

### **3. Test Endpoints** (`routers/testing.py`)

**Memory Agent** (Lines 334-346):
```python
response = requests.post(
    N8N_MEMORY_AGENT,
    json={
        "uid": uid,
        "conversation_id": conversation_id,  # ← ADDED
        "segments": [create_segment(transcript, stt_source=source)],
        ...
    },
    timeout=10
)
```

**Summary Agent Signature** (Lines 397-404):
```python
@router.post("/v1/test/summary-agent")
async def test_summary_agent(
    text: str = Body(...),
    conversation_id: str = Body("test_conv"),  # ← ADDED
    date: Optional[str] = Body(None),
    debug: bool = Body(False),
    uid: str = Body("test_user_123"),
):
```

**Summary Agent Call** (Lines 423-432):
```python
response = requests.post(
    N8N_SUMMARY_AGENT,
    json={
        "uid": uid,
        "conversation_id": conversation_id,  # ← ADDED
        "transcript": text,
        "started_at": started_at
    },
    timeout=15
)
```

---

## 🔄 **Upstream Merge Strategy**

**Files We Modified**:
1. `utils/llm/memories.py` - Memory extraction function
2. `utils/conversations/process_conversation.py` - Caller
3. `routers/testing.py` - Test endpoints (our addition, not in upstream)

**Upstream Doesn't Have**:
- n8n integration (only local LLM)
- Test endpoints (`routers/testing.py`)
- Ella callback routes (`routers/ella.py`)

**Merge Conflicts**: **LOW RISK**

---

### **Scenario 1: Upstream Changes `memories.py`**

**Likely Changes**:
- Improve local LLM prompts
- Add new Pydantic models
- Change function logic

**Our Code Block** (Lines 61-115 in our version):
```python
# ====== ELLA INTEGRATION ======
# Try calling Ella's memory agent first
try:
    # ... our n8n integration code ...

except Exception as e:
    print(f"❌ Ella memory agent failed: {e}", flush=True)
    return []

# ====== FALLBACK DISABLED ======
# NOTE: Local LLM fallback intentionally disabled
# ... old upstream code follows ...
```

**Merge Resolution**:
1. Accept upstream changes to local LLM code (lines 122+ in our version)
2. Keep our Ella integration block (lines 61-115)
3. Keep fallback disabled (don't re-enable local LLM)

---

### **Scenario 2: Upstream Changes `process_conversation.py`**

**Likely Changes**:
- Add new conversation types
- Change processing logic
- Add new fields

**Our Change** (Lines 307-311):
```python
new_memories = new_memories_extractor(
    uid,
    conversation.transcript_segments,
    conversation_id=conversation.id  # ← OUR ADDITION
)
```

**Merge Resolution**:
- Accept upstream changes
- **Keep conversation_id parameter** in the call

---

### **Scenario 3: We Add New Files**

**Files Upstream Doesn't Have**:
- `routers/testing.py` - Our E2E test endpoints
- `routers/ella.py` - Our callback endpoints
- `docs/*.md` - Our documentation

**Merge Resolution**:
- No conflicts! These are our additions.

---

## 📋 **Checklist for Future Merges**

When pulling from `upstream/main`:

```bash
git fetch upstream
git merge upstream/main
```

**If conflicts in `utils/llm/memories.py`**:
- [ ] Keep Ella integration block (lines 61-115)
- [ ] Keep conversation_id parameter in function signature
- [ ] Keep fallback disabled (return empty, not local LLM)
- [ ] Accept upstream changes to local LLM code (if any)

**If conflicts in `utils/conversations/process_conversation.py`**:
- [ ] Keep conversation_id parameter when calling new_memories_extractor()
- [ ] Accept upstream changes to surrounding code

**If conflicts in `routers/testing.py` or `routers/ella.py`**:
- [ ] These files don't exist upstream - keep our version

---

## 🧪 **Testing After Merge**

**1. Test Live Flow**:
```bash
# Use iOS app or create real conversation via WebSocket
# Check logs for Ella calls:
ssh root@100.101.168.91 "journalctl -u omi-backend -f | grep -E 'Ella|❌'"

# Should see:
# "📤 Calling Ella memory agent for uid=..."
# "✅ Ella memory agent returned N memories"
# OR
# "❌ Ella memory agent failed: ..."
# "❌ FALLBACK DISABLED - Returning empty"
```

**2. Test Endpoints**:
```bash
curl -X POST 'https://api.ella-ai-care.com/v1/test/memory-agent' \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Test conversation",
    "conversation_id": "test_post_merge",
    "uid": "test_user_123",
    "debug": true
  }'

# Check response includes conversation_id in debug section
```

**3. Verify n8n Receives conversation_id**:
```bash
# Ask n8n team to check their logs
# Should see incoming requests with:
# {"uid": "...", "conversation_id": "...", "segments": [...]}
```

---

## ⚠️ **What NOT to Do**

**Don't Re-enable Fallback**:
```python
# ❌ WRONG - Don't do this:
except Exception as e:
    print(f"⚠️ Ella failed, falling back to local LLM")
    return original_local_llm_extraction()  # NO!

# ✅ CORRECT - Keep this:
except Exception as e:
    print(f"❌ Ella memory agent failed: {e}", flush=True)
    return []  # Return empty, fail clearly
```

**Why**: We want to know when n8n fails. Silent fallback hides problems.

---

## 📊 **Impact Summary**

**Before Fix**:
- Live app: Used local LLM (n8n calls failed silently)
- Test endpoints: Failed with n8n schema errors
- iOS team: Blocked on E2E testing

**After Fix**:
- Live app: Uses n8n/Letta (no fallback)
- Test endpoints: Send correct schema to n8n
- iOS team: Can test (pending n8n endpoint fixes)
- Failures: Clear and logged (no silent fallback)

---

## 🔗 **Related Documentation**

- **Upstream Merge Strategy**: `docs/UPSTREAM_MERGE_STRATEGY.md` (plugin architecture)
- **Ella Integration**: `docs/ELLA_INTEGRATION.md` (original integration docs)
- **iOS Debug Flags**: `docs/IOS_DEBUG_FLAG_INTEGRATION.md` (modular approach)
- **n8n Schema**: `/Users/greg/repos/ella-ai/docs/api/N8N_V5_CORRECTIONS_SUMMARY.md`

---

## 📝 **Git Commit Reference**

**Key Commits**:
- `acf4505ac` - Original Ella integration (missing conversation_id)
- `dcf05f8c4` - Fixed conversation_id + disabled fallback
- `41db74cf0` - Bumped timeouts to 120s
- `0e7bf46fa` - Return async processing status instead of placeholder data
- `bcbcb839c` - Created this merge guide

**Branch**: `feature/e2e-agent-testing-unified`

**Files Changed**:
```
backend/utils/llm/memories.py                      | 23 ++++++--
backend/utils/conversations/process_conversation.py |  6 ++-
backend/routers/testing.py                         | 75 ++++++++++-----------
backend/docs/CONVERSATION_ID_FIX_MERGE_GUIDE.md    | 339 ++++++++++
backend/docs/IOS_E2E_TESTING_ASYNC_FLOW.md         | 420 ++++++++++
```

---

## 🔄 **Async Flow Pattern (Nov 22, 2025)**

**Additional Change**: Test endpoints now return realistic async processing status instead of placeholder data.

**Why**: iOS tests were "passing" but using fake placeholder data, not real Letta processing. This was confusing for iOS team.

**What Changed** (`routers/testing.py`):

**Before (Placeholder)**:
```python
except ValueError:
    agent_result = {
        "memories": [{"content": "Test memory...", "_placeholder": True}]
    }
```

**After (Async Status)**:
```python
except ValueError:
    agent_result = {
        "status": "processing",
        "conversation_id": conversation_id,
        "polling_endpoint": f"/v3/memories?conversation_id={conversation_id}",
        "expected_completion_time_seconds": 30
    }
```

**iOS Flow**:
1. iOS submits test request → Backend returns `{"status": "processing", "conversation_id": "..."}`
2. iOS polls `GET /v3/memories?conversation_id=...` every 5 seconds
3. After 20-30s, n8n callback completes and memories appear
4. iOS retrieves results and test passes

**Documentation**: `backend/docs/IOS_E2E_TESTING_ASYNC_FLOW.md`

---

**Next**: When pulling from upstream, use this guide to resolve conflicts and maintain our n8n integration.
