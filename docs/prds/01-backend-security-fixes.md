# PRD: Backend Security Fixes (CRITICAL)

## Status
🔴 **CRITICAL** - Must be completed immediately

## Executive Summary
Address critical security vulnerabilities in the backend that pose immediate risk of remote code execution, credential exposure, and authentication bypass.

## Problem Statement
The backend contains several critical security vulnerabilities:
1. **Remote Code Execution (RCE)**: Use of `eval()` on Redis data (8+ instances)
2. **Credential Exposure**: Service account credentials written to disk
3. **Authentication Bypass**: Admin backdoor with substring matching vulnerability
4. **Missing Security Controls**: No CORS configuration, weak rate limiting

## Goals
- Eliminate RCE vulnerability by removing all `eval()` usage
- Secure credential management (in-memory only)
- Fix authentication bypass vulnerability
- Implement proper CORS and rate limiting

## Success Metrics
- ✅ Zero instances of `eval()` in codebase
- ✅ No credentials written to disk
- ✅ CORS properly configured with whitelist
- ✅ Distributed rate limiting implemented via Redis
- ✅ Security audit passes (automated + manual review)

## Technical Specification

### 1. Remove eval() Usage (RCE Vulnerability)

**Files Affected:**
- `/backend/database/redis_db.py` (lines 130, 141, 177, 186, 196, 246, 257, 324, 348)

**Current Code:**
```python
def get_app_usage_count_cache(app_id: str) -> int | None:
    count = r.get(f'apps:{app_id}:usage_count')
    if not count:
        return None
    return eval(count)  # ❌ CRITICAL VULNERABILITY
```

**Fixed Code:**
```python
def get_app_usage_count_cache(app_id: str) -> int | None:
    count = r.get(f'apps:{app_id}:usage_count')
    if not count:
        return None
    try:
        return int(count) if isinstance(count, bytes) else int(count.decode())
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid cache value for app {app_id}: {e}")
        return None
```

**Action Items:**
- [ ] Replace all 8+ instances of `eval()` with type-safe parsing
- [ ] Add proper error handling for invalid cache values
- [ ] Add validation tests for cache value parsing
- [ ] Document expected cache value formats

### 2. Secure Credential Management

**Files Affected:**
- `/backend/database/_client.py` (lines 8-14)
- `/backend/main.py` (lines 36-41)

**Current Code:**
```python
if os.environ.get('SERVICE_ACCOUNT_JSON'):
    service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    with open('google-credentials.json', 'w') as f:  # ❌ Writes to disk
        json.dump(service_account_info, f)
```

**Fixed Code:**
```python
if os.environ.get('SERVICE_ACCOUNT_JSON'):
    service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    credentials = firebase_admin.credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(credentials)
else:
    # Fallback to Application Default Credentials
    firebase_admin.initialize_app()
```

**Action Items:**
- [ ] Remove file write operations for credentials
- [ ] Use in-memory credentials only
- [ ] Add validation for required credential fields
- [ ] Add startup health check for Firebase connection

### 3. Fix Authentication Bypass

**Files Affected:**
- `/backend/utils/other/endpoints.py` (lines 17-18)

**Current Code:**
```python
if authorization and os.getenv('ADMIN_KEY') in authorization:  # ❌ Substring match
    return authorization.split(os.getenv('ADMIN_KEY'))[1]
```

**Fixed Code:**
```python
import secrets

def get_current_user_uid(authorization: str = Header(None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Admin key should use constant-time comparison
    admin_key = os.getenv('ADMIN_KEY')
    if admin_key and authorization.startswith(f"Bearer {admin_key}:"):
        # Extract UID after admin key
        uid = authorization[len(f"Bearer {admin_key}:"):]
        if uid:
            return uid
        raise HTTPException(status_code=401, detail="Invalid admin authorization format")

    # Regular Firebase token verification
    try:
        decoded_token = auth.verify_id_token(authorization)
        return decoded_token['uid']
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
```

**Action Items:**
- [ ] Implement constant-time comparison for admin key
- [ ] Add proper Bearer token format
- [ ] Add audit logging for admin access
- [ ] Document admin authentication in security docs

### 4. Add CORS Configuration

**Files Affected:**
- `/backend/main.py` (add after FastAPI initialization)

**Implementation:**
```python
from fastapi.middleware.cors import CORSMiddleware

# CORS Configuration
ALLOWED_ORIGINS = [
    "https://omi.me",
    "https://app.omi.me",
    "https://web.omi.me",
]

# Add development origins from environment
if os.getenv("ENVIRONMENT") == "development":
    ALLOWED_ORIGINS.extend([
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
```

**Action Items:**
- [ ] Add CORS middleware with whitelist
- [ ] Configure allowed origins from environment
- [ ] Test CORS with frontend applications
- [ ] Document CORS configuration in deployment docs

### 5. Implement Distributed Rate Limiting

**Files Affected:**
- `/backend/utils/other/endpoints.py` (replace in-memory cache)
- `/backend/dependencies.py` (add rate limiting dependency)

**Implementation:**
```python
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
import redis.asyncio as aioredis

# In main.py startup
@app.on_event("startup")
async def startup():
    redis_url = f"redis://{os.getenv('REDIS_DB_HOST')}:{os.getenv('REDIS_DB_PORT')}"
    redis_connection = await aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    await FastAPILimiter.init(redis_connection)

# Apply to endpoints
@router.post("/v2/messages", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def send_message(...):
    ...

@router.post("/v1/payments/checkout-session", dependencies=[Depends(RateLimiter(times=5, seconds=300))])
async def create_checkout_session(...):
    ...

@router.websocket("/v4/listen")
async def listen_handler(websocket: WebSocket, uid: str):
    # Check connection limit per user
    active_connections = await redis.get(f"ws:connections:{uid}")
    if active_connections and int(active_connections) >= 3:
        await websocket.close(code=1008, reason="Too many concurrent connections")
        return
    ...
```

**Action Items:**
- [ ] Add fastapi-limiter dependency
- [ ] Implement Redis-based rate limiting
- [ ] Configure rate limits per endpoint type
- [ ] Add rate limit headers to responses
- [ ] Monitor rate limiting metrics

## Implementation Plan

### Phase 1: Critical Fixes (Days 1-2)
- Remove all eval() usage
- Fix credential handling
- Add basic CORS

### Phase 2: Authentication & Rate Limiting (Days 3-4)
- Fix admin authentication bypass
- Implement distributed rate limiting
- Add audit logging

### Phase 3: Testing & Validation (Day 5)
- Security audit
- Penetration testing
- Documentation updates

## Testing Requirements

### Security Tests
```python
# tests/security/test_no_eval.py
def test_no_eval_in_codebase():
    """Ensure no eval() usage in production code."""
    backend_files = glob.glob("backend/**/*.py", recursive=True)
    for file in backend_files:
        with open(file) as f:
            content = f.read()
            assert "eval(" not in content, f"Found eval() in {file}"

# tests/security/test_credentials.py
def test_no_credentials_on_disk():
    """Ensure no credential files created."""
    assert not os.path.exists("google-credentials.json")

# tests/security/test_rate_limiting.py
@pytest.mark.asyncio
async def test_rate_limiting_enforced():
    """Test rate limiting blocks excessive requests."""
    for i in range(15):
        response = await client.post("/v2/messages", json={...})
        if i < 10:
            assert response.status_code == 200
        else:
            assert response.status_code == 429  # Too Many Requests
```

## Rollback Plan
- Keep backup branch before changes
- Feature flags for rate limiting (can disable if issues)
- Gradual rollout: staging → 10% → 50% → 100%

## Dependencies
- `fastapi-limiter` package
- Redis connection (already present)
- Updated Firebase Admin SDK

## Documentation Updates
- [ ] Security best practices guide
- [ ] Rate limiting documentation
- [ ] Admin authentication guide
- [ ] CORS configuration reference

## Sign-off
- [ ] Security team review
- [ ] Backend lead approval
- [ ] DevOps deployment plan
- [ ] Incident response plan updated

---

**Estimated Effort:** 5 days
**Priority:** 🔴 CRITICAL
**Risk Level:** High (if not fixed), Low (implementation risk)
**Dependencies:** None
**Assigned To:** TBD
**Target Completion:** Within 1 week
