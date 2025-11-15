# E2E Agent Testing - Next Steps & Integration

**Date:** November 15, 2025
**Status:** ✅ Backend Implementation Complete | ⚠️ Blocked on n8n Authentication
**Branch:** `claude/e2e-agent-testing-endpoints-012842HTqxEogXqSw1EfFkGF`
**Commit:** `be5521f41`

---

## ✅ **What's Complete**

### Backend Implementation (6 Endpoints)
- ✅ `POST /v1/test/scanner-agent` - Urgency detection
- ✅ `POST /v1/test/memory-agent` - Memory extraction
- ✅ `POST /v1/test/summary-agent` - Daily summaries
- ✅ `POST /v1/test/chat-sync` - Synchronous chat
- ✅ `POST /v1/test/chat-async` - Asynchronous chat
- ✅ `GET /v1/test/chat-response/{job_id}` - Async polling

### Features Implemented
- ✅ Audio + Text input support (base64 audio → Deepgram STT)
- ✅ Real n8n webhook integration
- ✅ Comprehensive latency metrics (STT, agent, total)
- ✅ Proper error handling and timeouts
- ✅ Router registered in main.py
- ✅ Documentation created

### Files Modified
- ✅ `backend/routers/testing.py` (NEW - 458 lines)
- ✅ `backend/main.py` (router registration)
- ✅ `backend/docs/E2E_TESTING_ENDPOINTS_USAGE.md` (NEW - usage guide)

---

## ⚠️ **Current Blocker: n8n Authentication**

**Issue:** All endpoints returning "Access denied" from n8n webhooks

**Error Example:**
```json
{
  "detail": "Agent call failed: 401 Client Error: Unauthorized for url: https://n8n.ella-ai-care.com/webhook/scanner-agent"
}
```

**Root Cause:** n8n webhooks require authentication but we're not sending credentials

---

## 🔧 **Action Items**

### **1. Ella Team: Configure n8n Webhook Authentication**

**Priority:** URGENT - Blocking backend and iOS testing

**Options:**

#### **Option A: Make Webhooks Public (Recommended for Testing)**
- Remove authentication requirement from test webhooks
- Fastest path to unblock development
- Can re-enable auth later for production

**n8n Configuration:**
```javascript
// In each webhook workflow node:
Authentication: None
Allow anonymous: true
```

#### **Option B: Provide API Key**
- Add authentication to backend requests
- More secure, but requires coordination

**Required from Ella Team:**
1. API key or auth token
2. Header name (e.g., `X-API-Key`, `Authorization`)
3. Auth scheme (e.g., `Bearer`, custom)

**Backend Code Update (if Option B):**
```python
# backend/routers/testing.py (line ~140)
headers = {
    "Content-Type": "application/json",
    "X-API-Key": os.getenv("N8N_API_KEY")  # Add this
}

response = requests.post(
    N8N_SCANNER_AGENT,
    json={...},
    headers=headers,  # Add this
    timeout=10
)
```

**Please respond with:**
- [ ] Option A implemented (webhooks now public)
- [ ] Option B details (API key: `xxx`, header: `yyy`)
- [ ] ETA if not ready yet

---

### **2. Backend Developer: Create Feature Branch**

Once n8n auth is resolved:

```bash
# Checkout cloud agent's branch
git checkout claude/e2e-agent-testing-endpoints-012842HTqxEogXqSw1EfFkGF

# Create proper feature branch
git checkout -b feature/backend-e2e-agent-testing

# Test endpoints with real n8n agents
# (See backend/docs/E2E_TESTING_ENDPOINTS_USAGE.md)

# If working: Push feature branch
git push -u origin feature/backend-e2e-agent-testing

# Create PR to main
gh pr create --title "feat(testing): E2E agent testing endpoints" \
  --body "$(cat <<EOF
## Summary
Implements 6 E2E testing endpoints calling real n8n Letta agents.

## Endpoints
- POST /v1/test/scanner-agent
- POST /v1/test/memory-agent
- POST /v1/test/summary-agent
- POST /v1/test/chat-sync
- POST /v1/test/chat-async
- GET /v1/test/chat-response/{job_id}

## Features
- Audio + Text input (Deepgram STT)
- Real n8n integration
- Latency metrics
- Sync/async patterns

## Testing
- ✅ Code tested locally
- ✅ n8n webhooks verified
- ✅ Ready for iOS integration

## Files Changed
- backend/routers/testing.py (NEW)
- backend/main.py (router registration)
- backend/docs/E2E_TESTING_ENDPOINTS_USAGE.md (NEW)
EOF
)"
```

---

### **3. Testing & Verification Checklist**

Once n8n auth works:

#### **Test Scanner Agent:**
```bash
curl -X POST "http://localhost:8000/v1/test/scanner-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I am having chest pain",
    "source": "phone_mic"
  }'

# Expected: urgency_level: "critical"
```

#### **Test Memory Agent:**
```bash
curl -X POST "http://localhost:8000/v1/test/memory-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "I had lunch with Sarah at noon",
    "source": "phone_mic"
  }'

# Expected: memories array with 1+ items
```

#### **Test Summary Agent:**
```bash
curl -X POST "http://localhost:8000/v1/test/summary-agent" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "REAL_CONV_ID",
    "date": "2025-11-15"
  }'

# Expected: summary with title, overview, emoji
```

#### **Test Chat Sync:**
```bash
curl -X POST "http://localhost:8000/v1/test/chat-sync" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "What is the weather today?",
    "source": "phone_mic"
  }'

# Expected: chat response with text
```

#### **Test Chat Async:**
```bash
# Submit job
RESPONSE=$(curl -X POST "http://localhost:8000/v1/test/chat-async" \
  -H "Authorization: Bearer ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Tell me a joke", "source": "phone_mic"}')

JOB_ID=$(echo $RESPONSE | jq -r '.job_id')

# Poll for result
curl "http://localhost:8000/v1/test/chat-response/$JOB_ID" \
  -H "Authorization: Bearer ADMIN_KEY"

# Expected: status: "completed" with agent_response
```

---

### **4. iOS Team: Integration Readiness**

**Current Status:** ⏳ Wait for backend endpoints to be verified

**Once Backend Ready:**
1. Backend endpoints available at production URL
2. iOS can start integration testing
3. Follow `/tmp/CLOUD_AGENT_INSTRUCTIONS_REVISED.md`

**Backend API URL:**
- Local: `http://localhost:8000/v1/test/*`
- Production: `https://api.ella-ai-care.com/v1/test/*`

---

## 📊 **Progress Tracking**

### Backend Implementation
- [x] Implement 6 endpoints (cloud agent)
- [x] Register router in main.py
- [x] Create documentation
- [ ] Fix n8n authentication (BLOCKED - Ella team)
- [ ] Test all endpoints with real agents
- [ ] Create feature branch
- [ ] Create PR to main
- [ ] Merge to main

### n8n/Ella Team
- [x] Endpoint specifications provided (docs/ELLA_TEAM_E2E_ENDPOINT_SPECS.md)
- [ ] Configure webhook authentication (URGENT)
- [ ] Verify endpoint response formats
- [ ] Test with backend curl examples

### iOS Team
- [ ] Wait for backend endpoints verified
- [ ] Implement E2E testing UI
- [ ] Test all 4 agents
- [ ] Create demo video

---

## ⏱️ **Timeline Estimate**

**If n8n auth fixed today:**
- Backend testing & PR: 2 hours
- iOS integration: 6-8 hours
- **Total completion:** 1-2 days

**If n8n auth delayed:**
- iOS can build UI with mock responses
- Full integration blocked until n8n ready

---

## 📞 **Immediate Actions**

**Ella Team (URGENT):**
1. Review `backend/docs/ELLA_TEAM_E2E_ENDPOINT_SPECS.md`
2. Choose Option A (make public) or Option B (provide API key)
3. Reply with status/ETA

**Backend Developer:**
1. Wait for Ella team response
2. Test endpoints once auth fixed
3. Create feature branch and PR

**iOS Team:**
1. Can start UI work now (use mock backend responses)
2. Full testing after backend endpoints verified

---

## 🔗 **Related Documentation**

- **Implementation Guide:** `backend/docs/BACKEND_E2E_TESTING_INSTRUCTIONS_REVISED.md`
- **Usage Guide:** `backend/docs/E2E_TESTING_ENDPOINTS_USAGE.md`
- **Ella Team Specs:** `backend/docs/ELLA_TEAM_E2E_ENDPOINT_SPECS.md`
- **Sync/Async Patterns:** `backend/docs/BACKEND_E2E_UPDATES_v2.md`

---

**Status:** ⚠️ **BLOCKED ON N8N AUTHENTICATION**
**Next:** Ella team to configure webhooks (Option A or B)
**ETA:** Can complete in 2 hours after auth resolved

🚀 Ready to unblock as soon as n8n auth is configured!
