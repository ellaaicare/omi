# 🔒 SECURITY & HIPAA COMPLIANCE CHECKLIST

## ⚠️ CRITICAL - BEFORE PRODUCTION DEPLOYMENT

This document outlines **mandatory security changes** required before deploying the OMI backend to production, especially given **HIPAA compliance requirements** for healthcare data (Alzheimer's care use case).

---

## 🚨 IMMEDIATE ACTION ITEMS

### 1. **Firestore Security Rules** 🔥 HIGH PRIORITY

**Current Status**: ⚠️ **OPEN** (Allow anyone to read/write for 30 days)
**Required**: ❌ **MUST CHANGE** before production

**Action Required**:
```bash
# Visit Firebase Console:
https://console.firebase.google.com/project/omi-dev-ca005/firestore/rules

# Replace with restrictive rules:
```

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    // Default: Deny all access
    match /{document=**} {
      allow read, write: if false;
    }

    // Users can only access their own data
    match /users/{userId} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }

    // Conversations: User can only access their own
    match /conversations/{conversationId} {
      allow read, write: if request.auth != null
        && resource.data.uid == request.auth.uid;
    }

    // Photos: User can only access their own
    match /photos/{photoId} {
      allow read, write: if request.auth != null
        && resource.data.uid == request.auth.uid;
    }
  }
}
```

**Deadline**: ⏰ **Within 7 days** (before 30-day open period expires)

---

### 2. **Environment Variables - Remove Development Flags**

**Current Status**: ⚠️ Development mode enabled
**Required**: ❌ **MUST DISABLE** before production

**Action Required**:

```bash
# In .env file:

# REMOVE OR SET TO FALSE:
LOCAL_DEVELOPMENT=false  # Currently: true

# ENSURE THESE ARE SET:
ENVIRONMENT=production
DEBUG=false
```

**Why**: `LOCAL_DEVELOPMENT=true` bypasses authentication (uses UID '123' for all requests)

---

### 3. **HIPAA Compliance - PHI Data Protection**

**Protected Health Information (PHI) in OMI**:
- ✅ Audio recordings (conversations)
- ✅ Transcriptions (speech-to-text)
- ✅ User profile data (name, medical info)
- ✅ Geolocation data (where conversations occurred)
- ✅ Photos (from OpenGlass)

**Required Actions**:

#### 3.1 Encryption at Rest
```bash
# Verify Firestore encryption (enabled by default in Google Cloud):
# - AES-256 encryption at rest
# - Automatic key rotation
# - FIPS 140-2 validated

# Verify in Google Cloud Console:
https://console.cloud.google.com/security/kms?project=omi-dev-ca005
```

#### 3.2 Encryption in Transit
```bash
# ENFORCE HTTPS ONLY
# In production server config (uvicorn/nginx):

# uvicorn with SSL:
uvicorn main:app --ssl-keyfile=/path/to/key.pem --ssl-certfile=/path/to/cert.pem

# Or use nginx/Caddy reverse proxy with automatic HTTPS
```

#### 3.3 Access Logging & Audit Trails
```python
# Enable in .env:
ENABLE_AUDIT_LOGGING=true
LOG_LEVEL=INFO

# Ensure all PHI access is logged:
# - Who accessed what data
# - When they accessed it
# - What operations were performed
```

#### 3.4 Data Retention Policies
```bash
# Implement automated data deletion after retention period
# HIPAA requires minimum 6 years, but check state laws

# Create Cloud Functions for automated cleanup:
# - Delete conversations older than retention period
# - Delete audio files from storage
# - Maintain audit logs separately (7 years minimum)
```

---

### 4. **API Security Hardening**

#### 4.1 Rate Limiting (Already Implemented ✅)
```python
# Verify these are active in production:
# - routers/transcribe.py has rate limiting
# - Default: 60 requests per 60 seconds per IP
```

#### 4.2 API Key Security
```bash
# ROTATE ALL API KEYS BEFORE PRODUCTION:

# 1. Deepgram API Key
DEEPGRAM_API_KEY=<new-production-key>

# 2. OpenAI API Key
OPENAI_API_KEY=<new-production-key>

# 3. Pinecone API Key
PINECONE_API_KEY=<new-production-key>

# 4. Hugging Face Token
HUGGINGFACE_TOKEN=<new-production-token>

# 5. Create production Firebase project (separate from dev)
FIREBASE_PROJECT_ID=<production-project-id>
```

#### 4.3 CORS Configuration
```python
# In main.py, restrict CORS origins:

# DEVELOPMENT (current):
origins = ["*"]  # ⚠️ INSECURE

# PRODUCTION (required):
origins = [
    "https://yourdomain.com",
    "https://app.yourdomain.com",
    # NO wildcards
]
```

---

### 5. **Network Security**

#### 5.1 VPC Configuration (if using cloud deployment)
```bash
# Isolate backend in private VPC
# Only expose specific endpoints via load balancer
# Use Cloud Armor for DDoS protection
```

#### 5.2 Tailscale Security (for M1 iMac deployment)
```bash
# If using Tailscale for VPS↔iMac:
# ✅ Good: End-to-end encryption
# ✅ Good: Zero-trust architecture

# Ensure:
# - MFA enabled on Tailscale admin account
# - ACLs configured (least privilege)
# - Regular access reviews
```

---

### 6. **Authentication & Authorization**

#### 6.1 Firebase Authentication
```bash
# Enforce strong authentication:
# - Multi-factor authentication (MFA) for admin users
# - Password complexity requirements
# - Session timeout configuration
```

#### 6.2 Service Account Security
```bash
# google-credentials.json contains sensitive keys

# NEVER commit to git (already in .gitignore ✅)
# Rotate service account keys annually
# Use least-privilege IAM roles

# For production, use:
# - Google Cloud Workload Identity
# - Or Google Secret Manager
```

---

### 7. **Data Backup & Disaster Recovery**

#### 7.1 Automated Backups
```bash
# Enable Firestore automated backups:
https://console.cloud.google.com/firestore/databases/-default-/import-export?project=omi-dev-ca005

# Schedule:
# - Daily automated backups
# - 30-day retention minimum
# - Offsite backup storage (different region)
```

#### 7.2 Business Continuity Plan
```bash
# Document and test:
# - Recovery Time Objective (RTO): <4 hours
# - Recovery Point Objective (RPO): <24 hours
# - Incident response procedures
```

---

### 8. **HIPAA Business Associate Agreement (BAA)**

**Required for HIPAA Compliance**:

#### 8.1 Sign BAAs with all service providers:
- ✅ **Google Cloud Platform**: https://cloud.google.com/security/compliance/hipaa
- ✅ **Deepgram**: Contact sales for HIPAA BAA
- ⚠️ **OpenAI**: May not offer HIPAA BAA (check current policy)
- ⚠️ **Pinecone**: Verify HIPAA compliance options

**Alternative**:
- Run all ML models on-premise (M1 iMac) to avoid cloud processing
- Use WhisperX + PyAnnote locally (already downloaded)
- This eliminates Deepgram dependency

---

### 9. **Monitoring & Incident Response**

#### 9.1 Security Monitoring
```bash
# Set up alerts for:
# - Failed authentication attempts (> 5 in 5 min)
# - Unusual data access patterns
# - API quota exceeded
# - Unauthorized API calls
# - Database rule violations
```

#### 9.2 Incident Response Plan
```
1. Detect: Automated alerting
2. Contain: Immediately revoke compromised credentials
3. Investigate: Review audit logs
4. Notify: HIPAA breach notification (within 60 days if >500 records)
5. Remediate: Fix vulnerabilities
6. Document: Complete incident report
```

---

### 10. **Code Security**

#### 10.1 Dependency Scanning
```bash
# Regular security audits:
pip install safety
safety check --json

# Or use GitHub Dependabot (enable in repo settings)
```

#### 10.2 Secrets Scanning
```bash
# Prevent accidental secret commits:
git secrets --install
git secrets --register-aws
git secrets --scan

# Or use pre-commit hooks
```

---

## 📋 PRE-PRODUCTION CHECKLIST

Before deploying to production, verify:

- [ ] Firestore security rules changed from "Open" to "Restrictive"
- [ ] `LOCAL_DEVELOPMENT=false` in production .env
- [ ] All API keys rotated to production versions
- [ ] Separate production Firebase project created
- [ ] CORS origins restricted to specific domains
- [ ] HTTPS enforced (no HTTP access)
- [ ] Automated backups configured
- [ ] Audit logging enabled
- [ ] BAAs signed with all cloud providers
- [ ] Incident response plan documented
- [ ] Security monitoring alerts configured
- [ ] Data retention policies implemented
- [ ] MFA enabled for admin accounts
- [ ] Penetration testing completed
- [ ] HIPAA compliance review completed
- [ ] Legal review of privacy policy
- [ ] Staff HIPAA training completed

---

## 🔐 DEVELOPMENT vs PRODUCTION CONFIGURATION

### Current Development Setup ⚠️
```bash
LOCAL_DEVELOPMENT=true           # Bypasses auth
Firestore Rules: Open            # Anyone can read/write
CORS: * (all origins)            # No origin restrictions
Service: Deepgram Cloud          # PHI sent to third party
Logging: Minimal                 # Limited audit trail
```

### Required Production Setup ✅
```bash
LOCAL_DEVELOPMENT=false          # Full authentication
Firestore Rules: Restrictive     # User-scoped access only
CORS: Specific domains           # Whitelist only
Service: Local WhisperX          # PHI stays on-premise
Logging: Complete audit trail    # HIPAA compliance
HTTPS: Enforced                  # TLS 1.3 minimum
Backups: Automated daily         # 30-day retention
Monitoring: 24/7 alerts          # Security team notified
```

---

## 📞 RESOURCES & CONTACTS

- **HIPAA Compliance Guide**: https://www.hhs.gov/hipaa/for-professionals/security/guidance/index.html
- **Google Cloud HIPAA**: https://cloud.google.com/security/compliance/hipaa
- **Firebase Security Rules**: https://firebase.google.com/docs/rules
- **HIPAA Risk Assessment Tool**: https://www.hhs.gov/hipaa/for-professionals/security/guidance/guidance-risk-analysis/index.html

---

## 🚀 DEPLOYMENT TIMELINE

**Before Production Launch**:
- Week 1: Security hardening (Firestore rules, env vars)
- Week 2: HIPAA compliance review (BAAs, policies)
- Week 3: Security testing (penetration test, audit)
- Week 4: Final review and launch approval

**Minimum Timeline**: 4 weeks from development to production

---

**Last Updated**: 2025-10-27
**Review Schedule**: Monthly during development, Quarterly in production
**Owner**: Technical Lead + Security Officer
**Compliance Framework**: HIPAA + SOC 2 Type II (recommended)
