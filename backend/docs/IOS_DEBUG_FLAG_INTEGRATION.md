# iOS Debug Flag Integration - Modular Approach

**Date**: November 21, 2025
**Requirement**: iOS team needs `debug` flag on test endpoints
**Source**: `/tmp/E2E_TESTING_ENDPOINTS_STATUS_AND_PRD.md`
**Goal**: Add debug support WITHOUT breaking upstream merges

---

## 📋 **iOS Requirements**

From E2E testing doc, iOS needs:

1. **Debug flag on test endpoints**:
   ```python
   @router.post("/v1/test/scanner-agent")
   async def test_scanner(
       text: str,
       debug: bool = Body(False),  # ← iOS wants this
       uid: str = Body("test_user_123")
   ):
   ```

2. **Debug output in responses**:
   ```json
   {
     "agent_response": {...},
     "_debug": {  // ← Only when debug=true
       "backend_status": "✅ Request authenticated",
       "n8n_endpoint": "https://n8n.ella-ai-care.com/webhook/scanner-agent",
       "n8n_payload_sent": {...},
       "n8n_status_code": 200,
       "issue": "n8n returned empty JSON"
     },
     "metrics": {
       "stt_latency_ms": 0,
       "agent_latency_ms": 513,
       "total_latency_ms": 513
     }
   }
   ```

3. **Detailed metrics**:
   - STT latency (Deepgram processing time)
   - Agent latency (n8n/Letta processing time)
   - Total latency (end-to-end)

---

## ✅ **Modular Solution: Middleware + Decorator**

Instead of adding `debug` logic to every endpoint, create reusable components.

### **File Structure**:
```
backend/
├── utils/
│   ├── debug/
│   │   ├── __init__.py
│   │   ├── middleware.py    # Debug response formatter
│   │   ├── decorators.py    # @with_debug_support decorator
│   │   └── metrics.py       # Metrics tracking utilities
│   └── config.py            # Feature flags
└── routers/
    └── testing.py           # Test endpoints (uses debug utils)
```

**Benefits**:
- ✅ All debug logic in ONE place (`utils/debug/`)
- ✅ Upstream changes to `routers/` files won't affect debug code
- ✅ Easy to enable/disable debug globally
- ✅ Reusable across all endpoints

---

## 📝 **Implementation**

### **Step 1: Debug Metrics Tracker**

**File**: `utils/debug/metrics.py`

```python
from dataclasses import dataclass
from typing import Optional, Dict, Any
import time

@dataclass
class RequestMetrics:
    """Track latency metrics for debug mode"""
    stt_start: Optional[float] = None
    stt_end: Optional[float] = None
    agent_start: Optional[float] = None
    agent_end: Optional[float] = None
    total_start: Optional[float] = None
    total_end: Optional[float] = None

    def start_stt(self):
        self.stt_start = time.time()

    def end_stt(self):
        self.stt_end = time.time()

    def start_agent(self):
        self.agent_start = time.time()

    def end_agent(self):
        self.agent_end = time.time()

    def start_total(self):
        self.total_start = time.time()

    def end_total(self):
        self.total_end = time.time()

    def get_metrics(self) -> Dict[str, float]:
        """Get metrics in milliseconds"""
        return {
            "stt_latency_ms": self._calc_ms(self.stt_start, self.stt_end),
            "agent_latency_ms": self._calc_ms(self.agent_start, self.agent_end),
            "total_latency_ms": self._calc_ms(self.total_start, self.total_end),
        }

    def _calc_ms(self, start, end):
        if start and end:
            return round((end - start) * 1000, 2)
        return 0


@dataclass
class DebugInfo:
    """Debug information container"""
    backend_status: str = ""
    n8n_endpoint: str = ""
    n8n_payload_sent: Optional[Dict[str, Any]] = None
    n8n_status_code: Optional[int] = None
    n8n_response_body: str = ""
    issue: str = ""
    using_placeholder: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict, excluding empty fields"""
        return {
            k: v for k, v in {
                "backend_status": self.backend_status,
                "n8n_endpoint": self.n8n_endpoint,
                "n8n_payload_sent": self.n8n_payload_sent,
                "n8n_status_code": self.n8n_status_code,
                "n8n_response_body": self.n8n_response_body,
                "issue": self.issue,
                "using_placeholder": self.using_placeholder,
            }.items() if v
        }
```

---

### **Step 2: Debug Response Formatter**

**File**: `utils/debug/middleware.py`

```python
from typing import Dict, Any, Optional
from .metrics import RequestMetrics, DebugInfo

def format_debug_response(
    response_data: Dict[str, Any],
    debug: bool,
    metrics: Optional[RequestMetrics] = None,
    debug_info: Optional[DebugInfo] = None
) -> Dict[str, Any]:
    """
    Add debug information to response if debug=True

    This is the ONLY place where debug formatting happens.
    All endpoints use this utility.
    """
    if not debug:
        return response_data

    # Add metrics if provided
    if metrics:
        response_data["metrics"] = metrics.get_metrics()

    # Add debug info if provided
    if debug_info:
        response_data["_debug"] = debug_info.to_dict()

    return response_data
```

---

### **Step 3: Decorator for Test Endpoints**

**File**: `utils/debug/decorators.py`

```python
from functools import wraps
from fastapi import Request
from .metrics import RequestMetrics

def with_debug_support(endpoint_func):
    """
    Decorator that adds debug tracking to test endpoints

    Usage:
        @router.post("/v1/test/scanner-agent")
        @with_debug_support
        async def test_scanner(text: str, debug: bool = False, ...):
            metrics = request.state.metrics  # Available via decorator
            debug_info = request.state.debug_info
            ...
    """
    @wraps(endpoint_func)
    async def wrapper(*args, request: Request, **kwargs):
        # Initialize metrics tracking
        request.state.metrics = RequestMetrics()
        request.state.debug_info = None  # Endpoint can set this

        request.state.metrics.start_total()

        try:
            # Call original endpoint
            result = await endpoint_func(*args, request=request, **kwargs)
            return result
        finally:
            request.state.metrics.end_total()

    return wrapper
```

---

### **Step 4: Update Test Endpoints**

**File**: `routers/testing.py` (MINIMAL CHANGES)

```python
from fastapi import APIRouter, Body, Request
from utils.debug.decorators import with_debug_support
from utils.debug.middleware import format_debug_response
from utils.debug.metrics import DebugInfo

router = APIRouter()

@router.post("/v1/test/scanner-agent")
@with_debug_support  # ← NEW: Add decorator
async def test_scanner_agent(
    request: Request,  # ← NEW: Injected by decorator
    text: str = Body(...),
    source: str = Body("phone_mic"),
    debug: bool = Body(False),  # ← iOS requirement
    uid: str = Body("test_user_123")
):
    metrics = request.state.metrics  # ← From decorator
    debug_info = DebugInfo() if debug else None  # ← Initialize if needed

    # Process audio → text (if needed)
    metrics.start_stt()
    transcript = text  # (or transcribe audio)
    metrics.end_stt()

    # Call n8n agent
    metrics.start_agent()

    if debug:
        debug_info.n8n_endpoint = "https://n8n.ella-ai-care.com/webhook/scanner-agent"
        debug_info.n8n_payload_sent = {"uid": uid, "segments": [...]}

    response = requests.post(
        "https://n8n.ella-ai-care.com/webhook/scanner-agent",
        json={...},
        timeout=30
    )

    if debug:
        debug_info.n8n_status_code = response.status_code
        debug_info.n8n_response_body = response.text or "(empty)"

    if response.status_code == 200 and response.text:
        agent_response = response.json()
    else:
        # Placeholder fallback
        if debug:
            debug_info.issue = "n8n returned empty JSON (using placeholder)"
            debug_info.using_placeholder = True
        agent_response = {
            "urgency_level": "low",
            "_placeholder": True
        }

    metrics.end_agent()

    # Build response
    result = {
        "test_type": "scanner_agent",
        "transcript": transcript,
        "agent_response": agent_response
    }

    # Format with debug info (if requested)
    return format_debug_response(result, debug, metrics, debug_info)
```

**Key Points**:
- ✅ Decorator handles metrics automatically
- ✅ Debug info only created when `debug=True`
- ✅ All debug logic in separate files
- ✅ Upstream changes to testing.py have minimal conflict risk

---

## 🔄 **Upstream Merge Impact**

### **Files We Create** (No upstream conflicts):
```
utils/debug/
├── __init__.py          # NEW - our code
├── metrics.py           # NEW - our code
├── middleware.py        # NEW - our code
└── decorators.py        # NEW - our code
```

### **Files We Modify** (Minimal conflicts):
```
routers/testing.py:
- Add: from utils.debug.decorators import with_debug_support
- Add: from utils.debug.middleware import format_debug_response
- Add: @with_debug_support decorator (1 line per endpoint)
- Add: debug parameter (1 line per endpoint)
- Add: format_debug_response call (1 line per endpoint)
```

**Conflict Risk**: 🟢 **LOW**
- If upstream adds new test endpoints, we just add our decorator
- If upstream modifies existing endpoints, conflicts are minimal (3 lines)

---

## 📋 **Feature Flag Support**

**File**: `utils/config.py`

```python
import os

# Feature flags
DEBUG_ENABLED = os.getenv("DEBUG_ENABLED", "true").lower() == "true"
DEBUG_VERBOSE = os.getenv("DEBUG_VERBOSE", "false").lower() == "true"

def is_debug_enabled() -> bool:
    """Check if debug mode is globally enabled"""
    return DEBUG_ENABLED
```

**Environment Variables** (`.env`):
```bash
# Enable debug features (default: true for development)
DEBUG_ENABLED=true

# Include verbose debug info (SQL queries, stack traces, etc.)
DEBUG_VERBOSE=false
```

**Usage in endpoints**:
```python
from utils.config import is_debug_enabled

@router.post("/v1/test/scanner-agent")
async def test_scanner(debug: bool = False, ...):
    # Only allow debug if globally enabled
    debug = debug and is_debug_enabled()

    # ... rest of endpoint ...
```

**Production Safety**:
```bash
# In production .env
DEBUG_ENABLED=false  # Disable debug globally
```

Even if iOS sends `debug=true`, production will ignore it.

---

## 🧪 **Testing Debug Integration**

```bash
# Test WITH debug flag
curl -X POST https://api.ella-ai-care.com/v1/test/scanner-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I have chest pain",
    "debug": true,
    "uid": "test_user_123"
  }'

# Expected: Response includes _debug and metrics sections

# Test WITHOUT debug flag
curl -X POST https://api.ella-ai-care.com/v1/test/scanner-agent \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I have chest pain",
    "debug": false,
    "uid": "test_user_123"
  }'

# Expected: Response excludes _debug section (clean response)
```

---

## 📊 **Benefits Summary**

### **Modular Approach**:
- ✅ All debug code in `utils/debug/` (separate from upstream)
- ✅ Minimal changes to endpoint files (3 lines per endpoint)
- ✅ Easy to enable/disable globally (feature flag)
- ✅ Reusable across all test endpoints
- ✅ Won't break on upstream merges

### **vs. Inline Debug Code** (Bad Approach):
- ❌ Debug logic scattered across all endpoints
- ❌ Difficult to maintain
- ❌ High merge conflict risk
- ❌ Hard to enable/disable globally

---

## 📋 **Implementation Checklist**

- [ ] Create `utils/debug/` directory
- [ ] Create `metrics.py` (RequestMetrics, DebugInfo classes)
- [ ] Create `middleware.py` (format_debug_response function)
- [ ] Create `decorators.py` (@with_debug_support decorator)
- [ ] Add feature flags to `utils/config.py`
- [ ] Update `routers/testing.py` (add decorator + debug support)
- [ ] Test with debug=true (verify _debug section appears)
- [ ] Test with debug=false (verify clean response)
- [ ] Test with DEBUG_ENABLED=false (verify debug ignored)
- [ ] Update documentation
- [ ] Add to README.md

---

## 🎯 **Migration Timeline**

**Hour 1**: Create debug utilities
- `metrics.py`, `middleware.py`, `decorators.py`

**Hour 2**: Update test endpoints
- Add decorators and debug support to 5 endpoints

**Hour 3**: Testing
- Test all endpoints with/without debug
- Test feature flag toggling
- Document usage

**Total**: 3 hours for complete debug integration

---

## 📝 **Documentation for iOS Team**

**Once implemented**, add to API docs:

```markdown
## Debug Mode

All test endpoints support an optional `debug` flag:

**Request**:
```json
{
  "text": "Test input",
  "debug": true  // ← Add this for detailed debugging
}
```

**Response** (with debug=true):
```json
{
  "agent_response": {...},
  "_debug": {
    "backend_status": "✅ Request authenticated",
    "n8n_endpoint": "https://n8n.ella-ai-care.com/webhook/...",
    "n8n_status_code": 200,
    "issue": "n8n returned empty JSON"
  },
  "metrics": {
    "stt_latency_ms": 450,
    "agent_latency_ms": 513,
    "total_latency_ms": 963
  }
}
```

**Response** (with debug=false or omitted):
```json
{
  "agent_response": {...}
  // No _debug or metrics sections
}
```

**Production**: Debug mode is disabled in production (returns clean responses).
```

---

**Next**: Implement this modular debug system to support iOS testing while maintaining clean upstream merges.
