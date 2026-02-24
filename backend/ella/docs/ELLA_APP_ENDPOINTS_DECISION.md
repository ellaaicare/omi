# Ella App Endpoints - Hosting Decision Request

**Date**: January 2026
**Status**: DECISION NEEDED
**Owner**: Engineering Team
**Impact**: Architecture, maintenance, scalability

---

## Context

The Ella AI app requires 4 webhook/API endpoints for the OMI native app integration:

| Endpoint | Purpose | Called By |
|----------|---------|-----------|
| `/webhook/transcript-scanner` | Receives real-time transcripts, scans for wake words | OMI backend (on every transcript segment) |
| `/ella-config` | User configuration page (wake words, voice settings) | OMI app (when user opens app settings) |
| `/verify-setup` | Verify user has completed setup | OMI backend (when enabling app) |
| `/tools-manifest` | Returns dynamic chat tools JSON | OMI backend (when loading chat) |

---

## Options

### Option A: n8n.ella-ai-care.com (n8n Webhooks)

**Current partial implementation exists.**

```
Endpoints:
- https://n8n.ella-ai-care.com/webhook/transcript-scanner
- https://n8n.ella-ai-care.com/ella-config
- https://n8n.ella-ai-care.com/verify-setup
- https://n8n.ella-ai-care.com/tools-manifest
```

#### Pros
| Benefit | Details |
|---------|---------|
| **No code deployment** | Changes are instant in n8n UI |
| **Visual workflow** | Non-developers can modify logic |
| **Rapid iteration** | Test changes without CI/CD |
| **Isolated from backend** | Won't affect OMI backend stability |
| **Already has some flows** | Scanner webhook partially exists |

#### Cons
| Concern | Details |
|---------|---------|
| **Scalability** | n8n may struggle with high-frequency transcript webhooks |
| **Latency** | Extra network hop (OMI backend → n8n → processing) |
| **Monitoring** | Harder to integrate with backend observability |
| **State management** | n8n not ideal for user preferences storage |
| **Version control** | n8n workflows harder to track in git |

#### Best For
- Rapid prototyping
- Low-traffic scenarios
- When PM/non-devs need to modify logic
- When backend team is unavailable

---

### Option B: api.ella-ai-care.com (Backend API)

**Would require new endpoints in OMI backend.**

```
Endpoints:
- https://api.ella-ai-care.com/v1/ella/transcript-scanner
- https://api.ella-ai-care.com/v1/ella/config
- https://api.ella-ai-care.com/v1/ella/verify-setup
- https://api.ella-ai-care.com/v1/ella/tools-manifest
```

#### Pros
| Benefit | Details |
|---------|---------|
| **Performance** | No extra network hop, same infrastructure |
| **Scalability** | Uses existing backend scaling |
| **Observability** | Same logging, metrics, alerting |
| **State management** | Direct access to Redis/Firestore |
| **Version control** | All code in git, proper PR reviews |
| **Type safety** | Python with Pydantic models |

#### Cons
| Concern | Details |
|---------|---------|
| **Deployment required** | Changes need CI/CD |
| **Merge conflicts** | More code in OMI backend = more conflicts with upstream |
| **Backend team dependency** | Requires dev resources |
| **Slower iteration** | PR → review → merge → deploy cycle |

#### Best For
- Production at scale
- When reliability is critical
- Long-term maintainability
- When backend team is available

---

### Option C: Hybrid Approach (RECOMMENDED)

**Split by frequency and complexity.**

```
High-frequency (Backend):
- https://api.ella-ai-care.com/v1/ella/transcript-scanner  ← Called every segment
- https://api.ella-ai-care.com/v1/ella/verify-setup        ← Simple boolean check

Low-frequency (n8n):
- https://n8n.ella-ai-care.com/ella-config                 ← User visits occasionally
- https://n8n.ella-ai-care.com/tools-manifest              ← Called once per chat session
```

#### Rationale

| Endpoint | Frequency | Latency Sensitive | Recommended Host |
|----------|-----------|-------------------|------------------|
| transcript-scanner | Very high (every segment) | Yes | **Backend** |
| verify-setup | Low (once per enable) | No | **Backend** (simple) |
| ella-config | Very low (user action) | No | **n8n** (visual editing) |
| tools-manifest | Low (once per chat) | No | **n8n** (easy updates) |

#### Pros
- Performance where it matters (transcript scanning)
- Flexibility where it helps (config UI, tools)
- Minimal backend code additions
- Easy to update chat tools without deployment

#### Cons
- Split architecture to maintain
- Two systems to monitor

---

## Technical Considerations

### 1. Transcript Scanner Volume

**Estimate:**
- 1 transcript segment every ~3 seconds per active user
- 100 active users = 2000 requests/minute
- Each request needs: parse → scan for wake words → optional notification

**Verdict:** n8n might handle this, but backend is safer for scale.

### 2. Upstream Merge Impact

| Host | Merge Conflict Risk |
|------|---------------------|
| n8n | None (separate system) |
| Backend `/v1/ella/*` | Low (new namespace) |
| Backend existing routes | High (avoid) |

**Recommendation:** If using backend, create `/v1/ella/` namespace to isolate from upstream.

### 3. Wake Word Response Time

User says "Hey Ella" → expects response in <1 second.

| Path | Estimated Latency |
|------|-------------------|
| Backend direct | ~50-100ms |
| n8n webhook | ~200-500ms |

**Verdict:** Backend preferred for wake word scanning.

### 4. Config Page Complexity

The `/ella-config` page needs to:
- Show current wake words
- Allow editing wake words
- Show webhook status
- Provide test buttons

**Verdict:** n8n with HTTP Response node can serve HTML easily.

---

## Implementation Effort

| Option | Backend Work | n8n Work | Total |
|--------|--------------|----------|-------|
| A (All n8n) | None | 4-6 hours | 4-6 hours |
| B (All Backend) | 8-12 hours | None | 8-12 hours |
| C (Hybrid) | 4-6 hours | 2-3 hours | 6-9 hours |

---

## Recommendation

### Short-term (Now): Option A - All n8n

**Why:** Get it working quickly, validate the flow, iterate rapidly.

```python
ELLA_ENDPOINTS = {
    "webhook_url": "https://n8n.ella-ai-care.com/webhook/transcript-scanner",
    "app_home_url": "https://n8n.ella-ai-care.com/ella-config",
    "setup_completed_url": "https://n8n.ella-ai-care.com/verify-setup",
    "chat_tools_manifest_url": "https://n8n.ella-ai-care.com/tools-manifest",
}
```

### Medium-term (After validation): Option C - Hybrid

**Why:** Move transcript-scanner to backend once volume increases.

```python
ELLA_ENDPOINTS = {
    "webhook_url": "https://api.ella-ai-care.com/v1/ella/transcript",
    "app_home_url": "https://n8n.ella-ai-care.com/ella-config",
    "setup_completed_url": "https://api.ella-ai-care.com/v1/ella/verify",
    "chat_tools_manifest_url": "https://n8n.ella-ai-care.com/tools-manifest",
}
```

---

## Decision Needed

Please choose:

- [ ] **Option A**: All n8n (fastest to implement, may need migration later)
- [ ] **Option B**: All Backend (most robust, more work upfront)
- [ ] **Option C**: Hybrid (balanced approach)

### Additional Questions

1. **Expected user volume?** (affects transcript-scanner hosting)
2. **Who will maintain the config page?** (dev vs non-dev affects n8n preference)
3. **Is latency critical for wake word?** (affects transcript-scanner hosting)
4. **Backend dev availability?** (affects option B/C feasibility)

---

## Next Steps After Decision

1. Update `create_ella_ai_app.py` with chosen URLs
2. Create n8n workflows (if using n8n)
3. Create backend endpoints (if using backend)
4. Run registration script with `--set-default`
5. Test end-to-end flow
6. Enable for all Ella users

---

*Document created: January 2026*
*Awaiting decision from: [Engineering Lead / PM]*
