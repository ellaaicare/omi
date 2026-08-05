# Ella Backend API Extensions

**Purpose**: This directory contains ALL Ella-specific backend code, kept modular for easy upstream merges.

**Last Updated**: January 11, 2026

---

## Overview

The Ella extensions add healthcare-focused features to the OMI backend:

1. **n8n/Letta Processing** - Routes summary/memory generation to Letta agents via n8n
2. **Push Notifications with Audio** - Firestore-based notifications with TTS playback
3. **Voice V2 (Grok V2V)** - Ultra-low latency voice-to-voice via Grok API
4. **Unified Memory Context** - Canonical `/v1/ella/events` ingestion and `/v1/ella/timeline` reads

---

## Directory Structure

```
backend/
├── ella/                          # ALL Ella code (this directory)
│   ├── README.md                  # This file
│   ├── __init__.py                # Extension loader
│   ├── config.py                  # Feature flags, endpoints
│   │
│   ├── adapters/                  # Swap-in replacements for upstream functions
│   │   ├── __init__.py
│   │   ├── summary_adapter.py     # Routes to n8n instead of OpenAI
│   │   ├── memory_adapter.py      # Routes to n8n instead of OpenAI
│   │   └── notification_adapter.py # Adds audio to push notifications
│   │
│   ├── routers/                   # Ella-specific API endpoints
│   │   ├── __init__.py
│   │   ├── callbacks.py           # /api/ella/callback/* (n8n webhooks)
│   │   ├── voice_v2.py            # /v2/voice (Grok V2V)
│   │   └── testing.py             # /api/v1/testing/* (E2E tests)
│   │
│   ├── services/                  # Business logic
│   │   ├── __init__.py
│   │   ├── n8n_client.py          # HTTP client for n8n webhooks
│   │   └── grok_pipeline.py       # Grok V2V pipeline logic
│   │
│   └── docs/                      # Ella-specific documentation
│       ├── N8N_INTEGRATION.md
│       ├── UNIFIED_MEMORY_CONTEXT.md
│       ├── VOICE_V2_PROTOCOL.md
│       └── UPSTREAM_HOOKS.md
│
├── main.py                        # Upstream + Ella import (5 lines added)
└── ...                            # Rest of upstream code
```

---

## How It Works

### 1. Extension Loading

The **only change** to upstream `main.py`:

```python
# main.py (upstream file)
app = FastAPI()
# ... upstream routers ...

# === ELLA EXTENSION POINT ===
try:
    from ella import register_ella_extensions
    register_ella_extensions(app)
except ImportError:
    pass  # Ella not installed, vanilla mode
# === END ELLA ===
```

### 2. Adapter Pattern

Ella adapters implement the same interface as upstream functions but route to different backends:

```python
# Example: Summary generation
# Upstream: calls OpenAI directly
# Ella: calls n8n → Letta agent → returns same format

from ella import get_adapter

# In upstream processing code:
ella_summary = get_adapter("summary")
if ella_summary:
    result = await ella_summary(uid, conversation, transcript)
    if result:
        return result  # Ella handled it
# Fall through to upstream OpenAI...
```

### 3. Feature Flags

All features can be toggled via environment variables:

```bash
# Master switch
ELLA_ENABLED=true

# Individual features
ELLA_SUMMARY_ENABLED=true      # Use n8n for summaries
ELLA_MEMORY_ENABLED=true       # Use n8n for memories
ELLA_NOTIFICATIONS_ENABLED=true # Audio push notifications
ELLA_VOICE_V2_ENABLED=true     # Grok V2V endpoint
ELLA_TESTING_ENABLED=false     # E2E testing endpoints (dev only)
```

---

## Key Features

### Feature 1: n8n/Letta Processing

**What**: Routes conversation processing to Letta agents via n8n webhooks instead of direct OpenAI calls.

**Why**: Centralized AI brain with memory, context, and healthcare-specific prompts.

**Endpoints Called**:
- `POST https://n8n.ella-ai-care.com/webhook/summary-agent`
- `POST https://n8n.ella-ai-care.com/webhook/memory-agent`
- `POST https://n8n.ella-ai-care.com/webhook/scanner-agent`

**Files**:
- `ella/adapters/summary_adapter.py`
- `ella/adapters/memory_adapter.py`
- `ella/services/n8n_client.py`

---

### Feature 2: Push Notifications with Audio

**What**: Stores notifications in Firestore with audio URL, iOS app plays audio on receive.

**Why**: Proactive healthcare alerts with voice messages.

**Firestore Structure**:
```json
{
  "uid": "user123",
  "message": "Time for your medication",
  "audio_url": "https://storage.../tts/abc123.mp3",
  "play_audio": true,
  "urgency": "MEDICATION",
  "timestamp": "2026-01-11T12:00:00Z"
}
```

**Files**:
- `ella/adapters/notification_adapter.py`
- `ella/routers/callbacks.py` (receives n8n notification triggers)

---

### Feature 3: Voice V2 (Grok V2V)

**What**: WebSocket endpoint for ultra-low latency voice conversations via Grok's realtime API.

**Why**: ~500ms response time vs 2-3s with traditional STT→LLM→TTS pipeline.

**Endpoint**: `WS /v2/voice?uid={uid}&pipeline_mode=grok_v2v`

**Architecture**:
```
iOS App ←→ Ella Backend ←→ Ella Grok Proxy ←→ Grok API
   16kHz PCM      24kHz PCM        24kHz PCM
```

**Files**:
- `ella/routers/voice_v2.py`
- `ella/services/grok_pipeline.py`

---

## Upstream Merge Process

### When Upstream Updates

```bash
# 1. Fetch upstream
git fetch upstream

# 2. Create merge branch
git checkout -b merge/upstream-$(date +%Y%m%d)

# 3. Merge (ella/ won't conflict - it's ours only)
git merge upstream/main

# 4. Check hook points still work
grep -r "from ella import" .
grep -r "get_adapter" utils/

# 5. Test
ELLA_ENABLED=true python -c "from ella import register_ella_extensions; print('OK')"
pytest tests/

# 6. Merge to main
git checkout main
git merge merge/upstream-$(date +%Y%m%d)
```

### Hook Points (Files We Modify in Upstream)

These are the ONLY upstream files with Ella hooks:

| File | Hook Type | Lines Added |
|------|-----------|-------------|
| `main.py` | Router registration | 5 |
| `utils/llm/conversation_processing.py` | Adapter call | 8 |
| `utils/llm/memories.py` | Adapter call | 8 |
| `utils/notifications.py` | Adapter call | 8 |

**Total upstream modifications**: ~30 lines

---

## Testing

### Unit Tests
```bash
pytest ella/tests/
```

### Integration Tests
```bash
# Requires n8n to be running
ELLA_ENABLED=true pytest ella/tests/integration/
```

### Verify Ella Loads
```bash
python -c "
from ella import register_ella_extensions, ELLA_ENABLED
print(f'Ella enabled: {ELLA_ENABLED}')
print('Adapters:', list(get_all_adapters().keys()))
"
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ELLA_ENABLED` | `true` | Master switch for all Ella features |
| `ELLA_N8N_BASE_URL` | `https://n8n.ella-ai-care.com` | n8n webhook base URL |
| `ELLA_SUMMARY_ENABLED` | `true` | Use n8n for summary generation |
| `ELLA_MEMORY_ENABLED` | `true` | Use n8n for memory extraction |
| `ELLA_SCANNER_ENABLED` | `true` | Send real-time transcripts to scanner |
| `ELLA_NOTIFICATIONS_ENABLED` | `true` | Audio push notifications |
| `ELLA_VOICE_V2_ENABLED` | `true` | Enable /v2/voice endpoint |
| `ELLA_TESTING_ENABLED` | `false` | E2E testing endpoints |
| `GROK_V2V_PROXY_URL` | `wss://voice.ella-ai-care.com/ws` | Grok proxy WebSocket URL |

### Isolated Hermes onboarding

New-user Hermes provisioning is disabled by default and has two separate rollout controls:

- `ELLA_HERMES_PROVISIONING_ENABLED` allows authenticated, idempotent `/v1/ella/onboarding/ensure` jobs to call the Hermes-only 8210 provisioner.
- `ELLA_HERMES_PROVISION_API_URL` and `ELLA_HERMES_PROVISION_API_TOKEN` are both mandatory. The URL must be the canonical approved Mini endpoint or an exact endpoint recorded in `ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST`.
- `ELLA_HERMES_PROVISION_ATTESTATION_KEY` is a distinct 32+ byte shared key used only for the provisioner's `honcho-isolation-v2` HMAC. Source discovers secret-like environment credentials, dynamic Hermes keys, and configured authority-secret references on every verification; accessor-observed values remain in the process separation set so a configuration reload cannot hide a still-cached runtime credential. Equality is rejected before provider work, activation, or resolution. OMI sends a fresh UID/owner/target/binding/job challenge and rejects unsigned, replayed, stale, partial, or context-mismatched readback before publication.
- `ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF` must reference a root-only server secret containing the reviewed SHA-256 binding of that canonical URL/token pair. Provisioning fails before identity, job, database, or HTTP side effects when any coordinate drifts.
- `ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS` bounds each cold-start network operation to 30-300 seconds and defaults to 180 seconds. Together with `ELLA_HERMES_PROVISION_ATTESTATION_VERIFICATION_GRACE_SECONDS` (default 30 seconds), it forms one monotonic deadline over the complete request-body-read, bounded decode, verification, staging, activation, and publication transaction. That deadline plus the fixed clock-skew margin must be strictly less than the 360-second wall-clock proof lifetime or provisioning fails before external or binding work.
- OMI sends a stable content-free `Idempotency-Key` derived from the exact UID, invitation target, job, and deterministic binding identity. A post-provider attestation rejection remains retryable and reuses that job/binding while issuing a fresh nonce. The provisioner must reconcile that idempotency key to the same profile/runtime and must never create a second runtime or bind it to another UID.
- `ELLA_RUNTIME_BINDINGS_ENABLED` makes chat, history, resolver, voice, and `/v4/listen` require the authenticated user's active healthy Hermes binding. In this mode those routes fail closed and never create/fall back to OpenClaw or a shared Plato profile.
- `ELLA_ISOLATED_VOICE_ROUTING_ENABLED` is a separate default-off gate. The voice proxy and OMI backend use a short-lived, Firebase-subject-bound JWT plus the independent `ELLA_VOICE_PROXY_SERVICE_TOKEN` for context/search/tool calls; isolated failures do not fall back to OpenClaw. Keep this gate off until the coordinated proxy/OMI/8210 deployment and two-UID canary pass.

Apply the shared Prisma migration from `ellaaicare/ella-ai` first:
`packages/database/prisma/migrations/20260721000000_add_ella_provisioning_runtime/migration.sql` (merged in `ellaaicare/ella-ai#1066`). OMI intentionally does not duplicate this schema. Both onboarding endpoints preflight the two tables and every required isolation index; an incomplete deployment returns retryable `503 provisioning_schema_not_ready` before any identity write.

Enable provisioning first for two synthetic Firebase users; enable runtime dispatch only after distinct profile, gateway, account/profile runtime target, Hermes profile-memory, and canonical timeline receipts pass the two-user isolation canary. Existing Plato remains valid only for the exact `ELLA_PLATO_UID` binding.

The ensure job also creates or repairs the UID-scoped OMI Firestore identity. Missing cloud-sync and raw-recording permissions are initialized to `false`; iOS must enable the appropriate setting only after the account-bound consent flow completes.

### AI processing consent

`/v1/users/ai-consent` is the server authority for the versioned AI/data-sharing
policy. The current `ai-data-processors-v8` manifest includes Soniox and
Speechmatics STT, Inworld TTS, Ella's self-hosted Kokoro/Fish TTS, Hermes
Cloud, built-in Hermes profile-scoped memory, OpenAI Codex, and Photon in
addition to the remaining model, memory, infrastructure, and fallback
recipients. V8 requires renewed consent because the Cloud route no longer names
or uses Honcho Cloud; retained Plato/Honcho memory remains a separate legacy
processor path. Authorization still
requires the exact policy version, processor-set hash, scope version, and scope
hash together; matching hashes from a v7 receipt do not authorize v8. The
authenticated Firebase UID selects both the current state and the immutable
receipt subcollection; callers cannot submit or select another UID.

- `GET /v1/users/ai-consent/policy` returns the required legal-recipient
  manifest, policy version, processor-set hash, and managed-cloud scope
  version/hash.
- `GET /v1/users/ai-consent` returns the current decision and whether the exact
  current policy authorizes protected routes.
- `POST /v1/users/ai-consent` records `granted`, `declined`, or `revoked` with a
  caller request ID, app/build, locale, exact scope, server-derived opaque
  account/profile binding, and server timestamp. The same request ID is
  idempotent; reuse with different metadata fails with `409`.
- `GET /v1/users/ai-consent/receipts/{receipt_id}` verifies a receipt only
  within the authenticated user's path.
- Account/data deletion remains `DELETE /v1/users/delete-account`; deletion
  also removes the user's consent receipt subcollection and returns a
  non-identifying synchronous completion receipt with request ID and server
  completion time.

Exact-policy grants are required at the Ella chat stream, Hermes onboarding
ensure, voice-session issuance, necklace/web transcription sockets, direct TTS,
legacy message/audio/file upload routes, shared conversation processing,
stored-audio transcription, Guardian consolidation, and legacy callback TTS.
Signed voice-proxy requests recheck current consent on every request so a token
issued before revocation cannot continue sending audio. The consent authority
router remains registered when `ELLA_ENABLED=false`; a rollback cannot leave
generic OMI route gates active without grant/revoke endpoints. Read-only
status/history and first-party canonical storage remain available so
decline/revoke does not silently retransmit data.

Rollout is fail-safe and non-breaking:

1. Deploy the receipt API with `ELLA_AI_CONSENT_ENFORCEMENT_ENABLED=false`.
2. Update iOS to replace the legacy private-cloud boolean/client UUID with the
   authenticated receipt API.
3. Set `ELLA_INTERNAL_VOICE_TTS_TOKEN` on both the OMI backend and internal
   Guardian caller. Never expose it to iOS. Guardian also sends the target UID
   in `X-Ella-Subject-Uid`; the backend rechecks that user's current receipt,
   so the service token is not a consent bypass.
4. Add synthetic Firebase UIDs to `ELLA_AI_CONSENT_ENFORCEMENT_UIDS` and prove
   grant, stale-policy, decline, revoke, chat, voice, STT, and Guardian TTS.
   Canary clients must authenticate direct TTS calls. Legacy anonymous TTS has
   no trustworthy UID and remains a migration bridge during UID-only canaries;
   global enforcement rejects those unattributed calls.
5. Enable `ELLA_AI_CONSENT_ENFORCEMENT_ENABLED=true` only after the live
   privacy policy and App Store metadata match the server manifest.

Changing the canonical processor set or its legal recipients requires a new
policy version/hash. Do not reuse a prior hash or silently map an undisclosed
fallback provider.

Managed Hermes Cloud real-data egress has a second default-off gate:
`ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED=false` plus an optional exact UID canary
list. Real egress also requires exact deployed v8 policy and scope env values.
The backend rechecks the current account/profile-bound receipt immediately
before Hermes Cloud/OpenAI model calls and Photon outbound handoff. A v7,
missing, declined, revoked, deleted, malformed,
or route/profile-drifted receipt fails closed. Synthetic/content-free preflight
continues under `ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true`.

Upstream-managed patch points are tracked in `docs/POST_MERGE_PATCHES.md`. Review that file after every Basehardware upstream sync.

---

## Rollback

To disable all Ella features and run vanilla OMI:

```bash
# Option 1: Environment variable
ELLA_ENABLED=false uvicorn main:app

# Option 2: Remove import from main.py
# Comment out: from ella import register_ella_extensions
```

---

## File Migration Map

When consolidating from old structure:

| Old Location | New Location |
|--------------|--------------|
| `routers/ella.py` | `ella/routers/callbacks.py` |
| `utils/ella/*` | `ella/adapters/*` + `ella/services/*` |
| `integrations/pipecat/pipeline/grok_v2v_pipeline.py` | `ella/services/grok_pipeline.py` |
| `integrations/pipecat/services/n8n_client.py` | `ella/services/n8n_client.py` |
| `routers/voice_v2.py` | `ella/routers/voice_v2.py` |

---

## Contact

- **Backend Developer**: Claude-Backend-Developer
- **Documentation**: This file + `ella/docs/`
- **Issues**: github.com/ellaaicare/omi/issues

---

*This module is designed for minimal upstream conflict and maximum maintainability.*
