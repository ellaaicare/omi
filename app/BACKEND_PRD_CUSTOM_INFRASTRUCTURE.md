# Backend PRD: Custom Infrastructure Support

**Document Version:** 1.0
**Date:** October 29, 2025
**Status:** Ready for Implementation
**Priority:** High

---

## 📋 Executive Summary

The iOS app now supports **runtime-configurable backend URLs**, allowing users to point the app to custom infrastructure. Backend services must be updated to handle requests from custom domains and ensure API compatibility.

---

## 🎯 Objectives

### Primary Goal
Enable the iOS app to communicate with **custom-deployed backend infrastructure** without code changes or app rebuilds.

### Success Criteria
- [ ] Backend accepts requests from custom domains
- [ ] All API endpoints return expected responses
- [ ] WebSocket connections work with custom URLs
- [ ] CORS configured for custom domains
- [ ] Authentication flows work end-to-end
- [ ] Real-time features (WebSocket) function properly

---

## 🏗️ Architecture Overview

### Current Flow (Default)
```
┌─────────────┐         HTTPS          ┌──────────────────┐
│             │  ───────────────────▶   │                  │
│  iOS App    │  api.production.com     │  Production      │
│             │  ◀───────────────────   │  Backend         │
└─────────────┘                         └──────────────────┘
```

### New Flow (Custom Infrastructure)
```
┌─────────────┐         HTTPS          ┌──────────────────┐
│             │  ───────────────────▶   │                  │
│  iOS App    │  api.yourserver.com     │  Custom          │
│             │  ◀───────────────────   │  Backend         │
└─────────────┘                         └──────────────────┘
```

### App Behavior
When user sets custom URL in **Settings → Developer Settings → Infrastructure**:
1. App stores URL in SharedPreferences
2. All `Env.apiBaseUrl` calls return custom URL
3. **ALL** HTTP and WebSocket clients use custom URL
4. No fallback to default URL (full override)

---

## 🔌 API Requirements

### 1. HTTP REST API Endpoints

#### Base URL Format
```
https://api.yourserver.com/
```

The iOS app will append paths like:
- `v1/users`
- `v1/memories`
- `v1/conversations`
- etc.

#### Required Endpoints (Minimum Viable)

| Endpoint | Method | Purpose | iOS Usage |
|----------|--------|---------|-----------|
| `/v1/health` | GET | Health check | Optional connection test |
| `/v1/auth/login` | POST | User authentication | Sign-in flow |
| `/v1/users/{userId}` | GET | User profile | Load user data |
| `/v1/memories` | GET/POST | Memories CRUD | Core feature |
| `/v1/conversations` | GET | Conversation history | Chat feature |
| `/v1/transcriptions` | POST | Audio transcription | Recording feature |

**Note:** These are example endpoints. Backend team should verify actual API routes used by reviewing app source code at `lib/backend/` and `lib/services/`.

#### Response Format
- **Content-Type:** `application/json`
- **Status Codes:** Standard HTTP (200, 201, 400, 401, 404, 500, etc.)
- **Error Format:**
```json
{
  "error": "string",
  "message": "Human-readable error message",
  "code": "ERROR_CODE"
}
```

### 2. WebSocket Real-Time Communication

#### WebSocket URL Format
```
wss://api.yourserver.com/ws
```

#### Requirements
- Protocol: WSS (WebSocket Secure)
- Support for authentication (likely via query params or headers)
- Handle reconnection attempts from app
- Proper close handshake

#### Expected Messages
The app likely sends/receives:
- Audio stream data
- Transcription updates
- Real-time conversation state
- Heartbeat/ping messages

**Action Item:** Backend team should instrument existing WebSocket server to log all message types for documentation.

---

## 🔐 Security Requirements

### 1. HTTPS/TLS
- **Mandatory:** All connections must use HTTPS (WSS for WebSocket)
- **Certificate:** Valid SSL/TLS certificate (not self-signed)
- **TLS Version:** TLS 1.2 or higher
- **Cipher Suites:** Modern, secure ciphers only

### 2. CORS Configuration
Custom domains will send requests with different `Origin` headers.

#### Required CORS Headers
```http
Access-Control-Allow-Origin: https://api.yourserver.com
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, X-Custom-Header
Access-Control-Allow-Credentials: true
Access-Control-Max-Age: 86400
```

#### Preflight Requests
Handle `OPTIONS` requests for all endpoints.

### 3. Authentication
- **Current:** Likely Firebase Auth tokens
- **Requirement:** Accept Firebase ID tokens from custom domain origins
- **Header:** `Authorization: Bearer <firebase-token>`

**Action Item:** Verify Firebase Auth works across domains (should be transparent if using Firebase Admin SDK).

---

## 📊 Data Flow Requirements

### Audio Upload Flow
```
┌─────────┐  1. Record Audio  ┌─────────┐
│ iOS App │ ─────────────────▶│ Local   │
└─────────┘                   │ Storage │
     │                        └─────────┘
     │ 2. Upload
     ▼
┌──────────────────────────────────────┐
│ POST /v1/transcriptions              │
│ Content-Type: multipart/form-data    │
│ Body: audio file (opus/wav/m4a?)     │
└──────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────┐
│ Backend Processing                   │
│ - Receive audio file                 │
│ - Queue for transcription            │
│ - Return job ID or immediate result  │
└──────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────┐
│ WebSocket Real-Time Updates          │
│ - Transcription progress             │
│ - Final text result                  │
│ - Metadata (timestamp, confidence)   │
└──────────────────────────────────────┘
```

### Data Persistence
- **User Data:** Must persist across sessions
- **Memories:** Stored in database, retrievable via API
- **Audio Files:** May be stored temporarily or long-term (backend decision)

---

## 🧪 Testing Requirements

### 1. API Compatibility Testing

#### Test Matrix
| Test Case | Endpoint | Expected Result |
|-----------|----------|-----------------|
| Health check | GET /v1/health | 200 OK |
| User auth | POST /v1/auth/login | 200 + auth token |
| Get user profile | GET /v1/users/{id} | 200 + user JSON |
| Create memory | POST /v1/memories | 201 + memory object |
| List memories | GET /v1/memories | 200 + array of memories |
| Upload audio | POST /v1/transcriptions | 200/201 + transcription |

#### curl Test Examples
```bash
# Health check
curl -X GET https://api.yourserver.com/v1/health

# Auth (example - actual flow may differ)
curl -X POST https://api.yourserver.com/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"token": "firebase-id-token"}'

# Get user
curl -X GET https://api.yourserver.com/v1/users/123 \
  -H "Authorization: Bearer <token>"
```

### 2. WebSocket Testing

```javascript
// Test WebSocket connection
const ws = new WebSocket('wss://api.yourserver.com/ws');

ws.onopen = () => {
  console.log('Connected');
  ws.send(JSON.stringify({ type: 'ping' }));
};

ws.onmessage = (event) => {
  console.log('Received:', event.data);
};

ws.onerror = (error) => {
  console.error('Error:', error);
};
```

### 3. Load Testing
- **Tool:** Apache Bench, Locust, or k6
- **Scenario:** 100 concurrent iOS clients
- **Endpoints to test:**
  - GET /v1/memories (read-heavy)
  - POST /v1/transcriptions (write-heavy)
  - WebSocket connections (persistent)

---

## 🚀 Deployment Checklist

### Infrastructure Setup
- [ ] Deploy backend services to custom domain
- [ ] Configure DNS for `api.yourserver.com`
- [ ] Obtain and install SSL/TLS certificate
- [ ] Configure firewall rules (allow HTTPS 443, WSS 443)
- [ ] Set up load balancer (if needed)
- [ ] Configure CORS for custom domain

### Backend Configuration
- [ ] Update environment variables (API_BASE_URL, etc.)
- [ ] Configure Firebase Admin SDK
- [ ] Set up database connections
- [ ] Configure file storage (audio uploads)
- [ ] Enable logging and monitoring

### Testing
- [ ] Run API compatibility tests
- [ ] Test WebSocket connections
- [ ] Verify HTTPS/TLS certificate
- [ ] Check CORS configuration
- [ ] Test authentication flow end-to-end
- [ ] Verify audio upload and transcription

### Monitoring
- [ ] Set up error tracking (Sentry, etc.)
- [ ] Configure metrics (requests/sec, latency, errors)
- [ ] Set up alerts (API downtime, high error rate)
- [ ] Log all iOS app requests for debugging

---

## 📝 Implementation Recommendations

### 1. Use Existing Production Backend
**Recommended:** Deploy existing production backend to custom domain.

```bash
# Example using Docker
docker pull your-registry/omi-backend:latest
docker run -p 443:443 \
  -e API_BASE_URL=https://api.yourserver.com \
  -e FIREBASE_PROJECT_ID=omi-dev-ca005 \
  your-registry/omi-backend:latest
```

### 2. Reverse Proxy Pattern
Use NGINX or Caddy as reverse proxy:

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourserver.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket support
    location /ws {
        proxy_pass http://backend:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### 3. Serverless Option
If using serverless (AWS Lambda, Cloud Functions):
- Ensure cold start times < 3s
- Configure API Gateway for custom domain
- Set up WebSocket API Gateway for real-time features

---

## 🐛 Debugging & Troubleshooting

### Common Issues

#### Issue: iOS App Can't Connect
**Symptoms:** Network errors, timeouts
**Check:**
- [ ] DNS resolves correctly: `nslookup api.yourserver.com`
- [ ] HTTPS certificate is valid: `curl -v https://api.yourserver.com`
- [ ] Firewall allows inbound traffic on port 443
- [ ] Backend service is running: `curl https://api.yourserver.com/health`

#### Issue: CORS Errors
**Symptoms:** Browser console shows CORS errors (if testing in web view)
**Check:**
- [ ] `Access-Control-Allow-Origin` header present
- [ ] OPTIONS preflight requests return 200
- [ ] `Access-Control-Allow-Credentials: true` if using cookies

#### Issue: WebSocket Disconnects
**Symptoms:** Frequent reconnections, dropped messages
**Check:**
- [ ] Load balancer timeout settings (increase to 300s+)
- [ ] WebSocket keep-alive/ping interval
- [ ] Network stability (cellular vs WiFi)

### Logging Strategy
```python
# Example logging for debugging iOS app requests
@app.middleware("http")
async def log_ios_requests(request: Request, call_next):
    user_agent = request.headers.get("User-Agent", "")
    if "iOS" in user_agent or "Darwin" in user_agent:
        logger.info(f"iOS Request: {request.method} {request.url}")
        logger.info(f"Headers: {dict(request.headers)}")

    response = await call_next(request)
    return response
```

---

## 📚 Reference Documentation

### App Source Code Locations
- **API Clients:** `lib/backend/http/api_client.dart`
- **WebSocket:** `lib/backend/websocket/ws_client.dart`
- **Auth Service:** `lib/services/auth_service.dart`
- **Environment Config:** `lib/env/env.dart`

### Backend Repository
- **Location:** [Provide backend repo URL]
- **Docs:** [Link to backend API documentation]
- **Deploy Scripts:** [Link to deployment guides]

---

## ✅ Acceptance Criteria

### Definition of Done
- [ ] Backend deployed to custom domain with HTTPS
- [ ] All API endpoints respond correctly
- [ ] WebSocket real-time features work
- [ ] iOS app successfully authenticates
- [ ] Audio upload and transcription functional
- [ ] CORS configured correctly
- [ ] No errors in backend logs from iOS requests
- [ ] Monitoring and alerts configured
- [ ] Documentation updated with custom domain setup

### Performance Benchmarks
- API latency: < 200ms (p95)
- WebSocket latency: < 100ms (p95)
- Audio upload: < 5s for 1min audio file
- Uptime: 99.9% SLA

---

## 🤝 Support & Escalation

### Questions?
- **iOS Implementation:** Review code in `lib/` directory
- **API Specs:** Check existing backend docs or Swagger/OpenAPI spec
- **Firebase Auth:** Firebase Admin SDK documentation

### Escalation Path
1. Try deploying existing production backend to custom domain
2. Test with iOS app and check logs
3. If API incompatibility found, document specific issues
4. Escalate to iOS team for potential app-side fixes

---

## 🎯 Next Steps

1. **Backend Team:**
   - Deploy backend to custom domain
   - Configure HTTPS and CORS
   - Test all endpoints with curl
   - Verify WebSocket connections

2. **iOS Team (Greg):**
   - Set custom URL in app settings
   - Test authentication flow
   - Test audio recording → upload → transcription
   - Verify real-time features

3. **Integration Testing:**
   - End-to-end testing with custom backend
   - Load testing with realistic workload
   - Error handling and edge cases

---

**Document Owner:** Greg Lindberg
**Last Updated:** October 29, 2025
**Next Review:** After initial backend deployment
