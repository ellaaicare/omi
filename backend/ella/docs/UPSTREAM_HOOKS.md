# Ella Backend API - Upstream Hook Points

**Purpose**: Documents the EXACT modifications needed to upstream OMI backend files.

**Total Lines Modified**: ~35 lines across 4 files

---

## Overview

Ella extensions use a **hook pattern** - minimal changes to upstream files that call into the `ella/` module. This keeps merge conflicts to a minimum.

---

## Hook Point 1: main.py (Router Registration)

**File**: `main.py`
**Location**: After all upstream routers are registered
**Lines Added**: 5

### Code to Add

```python
# main.py

# ... upstream imports ...
# ... upstream router registration ...

# ============================================================================
# ELLA EXTENSIONS (add this block after upstream routers)
# ============================================================================
try:
    from ella import register_ella_extensions
    register_ella_extensions(app)
except ImportError:
    pass  # Ella extensions not installed, running vanilla OMI
# ============================================================================
```

### Why Here

- Runs after FastAPI app is created
- Doesn't interfere with upstream routers
- Safe try/except means vanilla OMI works if ella/ is missing

---

## Hook Point 2: utils/llm/conversation_processing.py (Summary Generation)

**File**: `utils/llm/conversation_processing.py`
**Function**: `get_transcript_structure()`
**Lines Added**: 10

### Code to Add

```python
# utils/llm/conversation_processing.py

async def get_transcript_structure(
    uid: str,
    conversation: Conversation,
    transcript: str,
    # ... other params ...
) -> Structured:
    """Generate structured summary for conversation."""

    # ========================================================================
    # ELLA HOOK: Route to n8n/Letta if enabled
    # ========================================================================
    try:
        from ella import get_adapter, ELLA_SUMMARY_ENABLED
        if ELLA_SUMMARY_ENABLED:
            ella_summary = get_adapter("summary")
            if ella_summary:
                result = await ella_summary(uid, conversation.id, transcript)
                if result:
                    print(f"✅ Ella summary: {result.get('title', 'N/A')}", flush=True)
                    return Structured(**result)
    except Exception as e:
        print(f"⚠️ Ella summary failed, using fallback: {e}", flush=True)
    # ========================================================================

    # Upstream OpenAI implementation continues below...
    # ... existing code ...
```

### Why Here

- Single point where all summaries are generated
- Returns same `Structured` object type
- Falls back to upstream if Ella fails

---

## Hook Point 3: utils/llm/memories.py (Memory Extraction)

**File**: `utils/llm/memories.py`
**Function**: `new_memories_extractor()` or similar
**Lines Added**: 10

### Code to Add

```python
# utils/llm/memories.py

async def new_memories_extractor(
    uid: str,
    conversation: Conversation,
    transcript: str,
    # ... other params ...
) -> List[Memory]:
    """Extract memories from conversation."""

    # ========================================================================
    # ELLA HOOK: Route to n8n/Letta if enabled
    # ========================================================================
    try:
        from ella import get_adapter, ELLA_MEMORY_ENABLED
        if ELLA_MEMORY_ENABLED:
            ella_memory = get_adapter("memory")
            if ella_memory:
                result = await ella_memory(uid, conversation.id, transcript)
                if result and result.get("memories"):
                    memories = [Memory(**m) for m in result["memories"]]
                    print(f"✅ Ella memories: {len(memories)} extracted", flush=True)
                    return memories
    except Exception as e:
        print(f"⚠️ Ella memory failed, using fallback: {e}", flush=True)
    # ========================================================================

    # Upstream OpenAI implementation continues below...
    # ... existing code ...
```

---

## Hook Point 4: routers/transcribe.py (Real-time Scanner)

**File**: `routers/transcribe.py`
**Location**: Inside transcript processing loop
**Lines Added**: 8

### Code to Add

```python
# routers/transcribe.py

# Inside the function that processes transcript segments...

async def process_transcript_segment(uid: str, segment: TranscriptSegment, ...):
    # ... existing processing ...

    # ========================================================================
    # ELLA HOOK: Send to real-time scanner (fire-and-forget)
    # ========================================================================
    try:
        from ella import get_adapter, ELLA_SCANNER_ENABLED
        if ELLA_SCANNER_ENABLED:
            ella_scanner = get_adapter("scanner")
            if ella_scanner:
                # Non-blocking, 1-second timeout
                asyncio.create_task(ella_scanner(uid, segment.text))
    except Exception:
        pass  # Scanner is optional, don't fail on errors
    # ========================================================================

    # ... rest of processing ...
```

---

## Verification Script

After applying hooks, verify with:

```bash
# Check hooks exist
grep -n "ELLA HOOK" backend/main.py backend/utils/llm/*.py backend/routers/transcribe.py

# Expected output:
# main.py:XX:# ELLA HOOK
# utils/llm/conversation_processing.py:XX:# ELLA HOOK
# utils/llm/memories.py:XX:# ELLA HOOK
# routers/transcribe.py:XX:# ELLA HOOK

# Test Ella loads
cd backend
python -c "from ella import register_ella_extensions; print('Ella loads OK')"
```

---

## Conflict Resolution

When merging upstream updates:

### If Hook File Changed

1. Check if hook location is still valid
2. Reapply hook after merge
3. Test: `python -c "from ella import get_adapter; print(get_adapter('summary'))"`

### If Hook Removed by Merge

```bash
# Find original hook
git diff HEAD~1 -- utils/llm/conversation_processing.py | grep "ELLA"

# Reapply from this document
```

---

## Files Summary

| File | Hook Type | Impact |
|------|-----------|--------|
| `main.py` | Import + register | Low (end of file) |
| `utils/llm/conversation_processing.py` | Adapter call | Low (start of function) |
| `utils/llm/memories.py` | Adapter call | Low (start of function) |
| `routers/transcribe.py` | Fire-and-forget | Very Low (optional) |

**Total Upstream Changes**: 4 files, ~35 lines

---

## Removing Ella

To convert back to vanilla OMI:

1. Remove `ella/` directory
2. Remove hook blocks (search for `# ELLA HOOK`)
3. Or just set `ELLA_ENABLED=false` (hooks become no-ops)

---

*Keep this document updated when adding new hook points.*
