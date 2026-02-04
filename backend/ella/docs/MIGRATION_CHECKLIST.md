# Ella Backend API - Migration Checklist

**Purpose**: Step-by-step guide to consolidate existing Ella code into the `ella/` module.

---

## Current State (Pre-Migration)

Ella code is scattered across:

```
backend/
├── routers/ella.py                      # 1,034 lines - callbacks
├── routers/voice_v2.py                  # 149 lines - Grok V2V router
├── utils/ella/                          # 401 lines - n8n adapters
│   ├── __init__.py
│   ├── config.py
│   ├── memory.py
│   ├── scanner.py
│   └── summary.py
├── integrations/pipecat/
│   ├── pipeline/grok_v2v_pipeline.py    # 500+ lines - Grok pipeline
│   ├── pipeline/config.py               # Pipeline config
│   └── services/n8n_client.py           # n8n HTTP client
```

---

## Target State (Post-Migration)

```
backend/
├── ella/                                # ALL Ella code here
│   ├── README.md
│   ├── __init__.py                      # Extension loader
│   ├── config.py                        # Consolidated config
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── summary.py                   # From utils/ella/summary.py
│   │   ├── memory.py                    # From utils/ella/memory.py
│   │   └── scanner.py                   # From utils/ella/scanner.py
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── callbacks.py                 # From routers/ella.py
│   │   └── voice_v2.py                  # From routers/voice_v2.py
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── n8n_client.py                # From integrations/pipecat/services/
│   │   └── grok_pipeline.py             # From integrations/pipecat/pipeline/
│   │
│   └── docs/
│       ├── UPSTREAM_HOOKS.md
│       └── MIGRATION_CHECKLIST.md
│
├── main.py                              # + 5 lines for Ella import
└── (upstream files with hooks)
```

---

## Migration Steps

### Phase 1: Create Structure (DONE)

- [x] Create `ella/` directory
- [x] Create `ella/README.md`
- [x] Create `ella/__init__.py` (extension loader)
- [x] Create `ella/docs/` directory
- [x] Create `ella/docs/UPSTREAM_HOOKS.md`
- [x] Create `ella/docs/MIGRATION_CHECKLIST.md`

### Phase 2: Consolidate Adapters

```bash
# Create adapters directory
mkdir -p backend/ella/adapters

# Move existing utils/ella files
cp backend/utils/ella/summary.py backend/ella/adapters/
cp backend/utils/ella/memory.py backend/ella/adapters/
cp backend/utils/ella/scanner.py backend/ella/adapters/
cp backend/utils/ella/config.py backend/ella/config.py

# Update imports in copied files
# Change: from .config import ELLA_CONFIG
# To: from ella.config import ELLA_CONFIG
```

- [ ] Move `utils/ella/summary.py` → `ella/adapters/summary.py`
- [ ] Move `utils/ella/memory.py` → `ella/adapters/memory.py`
- [ ] Move `utils/ella/scanner.py` → `ella/adapters/scanner.py`
- [ ] Consolidate config into `ella/config.py`
- [ ] Update imports in moved files
- [ ] Create `ella/adapters/__init__.py`

### Phase 3: Consolidate Routers

```bash
# Create routers directory
mkdir -p backend/ella/routers

# Move routers
cp backend/routers/ella.py backend/ella/routers/callbacks.py
cp backend/routers/voice_v2.py backend/ella/routers/voice_v2.py

# Update imports
```

- [ ] Move `routers/ella.py` → `ella/routers/callbacks.py`
- [ ] Move `routers/voice_v2.py` → `ella/routers/voice_v2.py`
- [ ] Update router prefixes if needed
- [ ] Create `ella/routers/__init__.py`

### Phase 4: Consolidate Services

```bash
# Create services directory
mkdir -p backend/ella/services

# Move services
cp backend/integrations/pipecat/services/n8n_client.py backend/ella/services/
cp backend/integrations/pipecat/pipeline/grok_v2v_pipeline.py backend/ella/services/grok_pipeline.py
```

- [ ] Move `integrations/pipecat/services/n8n_client.py` → `ella/services/`
- [ ] Move `integrations/pipecat/pipeline/grok_v2v_pipeline.py` → `ella/services/grok_pipeline.py`
- [ ] Update imports in moved files
- [ ] Create `ella/services/__init__.py`

### Phase 5: Add Hooks to Upstream

- [ ] Add Ella import to `main.py`
- [ ] Add hook to `utils/llm/conversation_processing.py`
- [ ] Add hook to `utils/llm/memories.py`
- [ ] Add hook to `routers/transcribe.py` (scanner)

### Phase 6: Update Old Locations

- [ ] Update `utils/ella/__init__.py` to import from new location
- [ ] Add deprecation warnings to old files
- [ ] Update any direct imports throughout codebase

### Phase 7: Testing

```bash
# Test Ella loads
python -c "from ella import register_ella_extensions; print('OK')"

# Test adapters
python -c "from ella import get_adapter; print(get_adapter('summary'))"

# Run tests
pytest ella/tests/ -v

# Integration test
ELLA_ENABLED=true python -m pytest tests/integration/
```

- [ ] Ella module loads without errors
- [ ] All adapters registered
- [ ] Routers work at expected paths
- [ ] n8n callbacks functional
- [ ] Voice V2 endpoint functional

### Phase 8: Cleanup (After Verification)

- [ ] Remove old `utils/ella/` (or keep as symlinks)
- [ ] Remove old router files from `routers/`
- [ ] Update any remaining imports
- [ ] Update documentation

---

## Backward Compatibility

During migration, maintain both locations:

```python
# utils/ella/__init__.py (keep temporarily)

import warnings
warnings.warn(
    "utils.ella is deprecated, use 'from ella import ...' instead",
    DeprecationWarning
)

# Re-export from new location
from ella.adapters.summary import call_summary_agent
from ella.adapters.memory import call_memory_agent
from ella.adapters.scanner import send_to_scanner
from ella.config import ELLA_CONFIG, is_ella_enabled

__all__ = [...]
```

---

## Rollback Plan

If migration causes issues:

1. Revert `main.py` changes
2. Revert hook additions
3. Keep old files in place (don't delete until verified)
4. Set `ELLA_ENABLED=false`

---

## Post-Migration Verification

```bash
# 1. Check structure
tree backend/ella/

# 2. Check imports
python -c "
from ella import (
    ELLA_ENABLED,
    register_ella_extensions,
    get_adapter
)
print('Imports OK')
print(f'ELLA_ENABLED: {ELLA_ENABLED}')
"

# 3. Check adapters registered
python -c "
from ella import get_all_adapters
adapters = get_all_adapters()
print(f'Adapters: {list(adapters.keys())}')
assert 'summary' in adapters
assert 'memory' in adapters
print('Adapters OK')
"

# 4. Start server and test
uvicorn main:app --reload
# Check logs for "ELLA AI CARE - Backend Extensions Loading"
```

---

## Timeline

| Phase | Estimated Time | Risk |
|-------|----------------|------|
| Phase 1-2 (Structure + Adapters) | 2 hours | Low |
| Phase 3-4 (Routers + Services) | 2 hours | Low |
| Phase 5 (Hooks) | 1 hour | Medium |
| Phase 6 (Updates) | 1 hour | Low |
| Phase 7 (Testing) | 2 hours | Low |
| Phase 8 (Cleanup) | 1 hour | Low |
| **Total** | **~9 hours** | |

---

*Complete each phase fully before moving to the next.*
