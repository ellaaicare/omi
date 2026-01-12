# Ella AI Care: Fork Migration Proposal

**Document Type**: Technical Proposal - Requires Team Sign-off
**Date**: January 11, 2026
**Author**: iOS Developer
**Status**: DRAFT - Pending Review

---

## Overview

This proposal outlines a strategic migration from our current divergent fork approach to a **modular plugin architecture** that keeps us in sync with upstream OMI while preserving all Ella-specific functionality.

**Key Decision Required**: Should we migrate to a fresh upstream fork with modular plugins?

---

## Table of Contents

1. [Current State Summary](#1-current-state-summary)
2. [Problem Statement](#2-problem-statement)
3. [Proposed Solution](#3-proposed-solution)
4. [Backend API Compatibility](#4-backend-api-compatibility)
5. [Migration Path](#5-migration-path)
6. [Risk Assessment](#6-risk-assessment)
7. [Team Sign-off](#7-team-sign-off)

---

## 1. Current State Summary

### iOS/Flutter App Changes

| Component | Lines Added | Purpose |
|-----------|-------------|---------|
| `ella_tts_service.dart` | 440 | TTS with backend integration |
| `voice_mode_manager.dart` | 848 | Voice conversation handling |
| `voice_mode_v2_service.dart` | 442 | Pipecat/Grok voice pipeline |
| `incoming_call_service.dart` | 445 | Inbound call detection |
| `on_device_asr_service.dart` | 190 | Local speech recognition |
| `heuristics_service.dart` | 167 | Behavior heuristics |
| Native iOS plugins | ~600 | TTS, audio push, ASR |
| **Total iOS Changes** | **~3,500** | |

### Backend API Changes

| Router/Component | Lines Added | Purpose |
|-----------------|-------------|---------|
| `routers/ella.py` | 1,034 | Ella n8n callback endpoints |
| `routers/tts.py` | 320 | TTS generation with caching |
| `routers/testing.py` | 728 | E2E testing endpoints |
| `routers/voice_v2.py` | 149 | Voice v2 (Grok) endpoints |
| `routers/notifications.py` | 405 | Enhanced push notifications |
| `routers/ai.py` | 238 | AI endpoints |
| `routers/analytics.py` | 227 | Analytics endpoints |
| `database/notifications_multi_device.py` | 251 | Multi-device notification storage |
| Documentation | ~15,000 | Extensive backend docs |
| **Total Backend Changes** | **~3,300+** | |

### What We're Missing from Upstream

| Feature | Lines | Value |
|---------|-------|-------|
| Limitless Device Support | +1,628 | New hardware platform |
| Custom STT Provider System | +486 | Local/private transcription |
| Goals & Daily Score | +800 | Progress tracking |
| Calendar Integration | +800 | Meeting context |
| Deepgram Nova-3 | ~100 | Better transcription |
| Android Companion Device | +258 | Android BLE improvements |
| Task Integrations (Asana/ClickUp) | +700 | External task sync |
| **Total Missing** | **~4,800** | |

---

## 2. Problem Statement

### Current Pain Points

1. **Divergence Increasing**: Upstream has 4,800+ lines of new features we can't easily adopt
2. **Merge Conflicts**: Cherry-picking becomes harder as codebases diverge
3. **Duplicated Effort**: Some of our code duplicates upstream features (on-device ASR, voice mode)
4. **Maintenance Burden**: Every upstream feature requires manual merge evaluation
5. **Technical Debt**: Fork-specific hacks accumulating

### Key Insight

**OMI is more configurable than we realized**:
- Backend URL is **runtime configurable** (developer settings)
- All API calls use `Env.apiBaseUrl` - just change the env file
- WebSockets auto-derive from API base URL
- Our backend just needs to implement OMI's endpoint contract

---

## 3. Proposed Solution

### Architecture: Fresh Fork with Modular Plugins

```
┌─────────────────────────────────────────────────────────────────┐
│                    ELLA AI CARE APP                              │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              ELLA PLUGIN LAYER (Our Code)                 │  │
│  │                                                           │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐ │  │
│  │  │ Audio Push  │ │ Wake Word   │ │ V2V Voice           │ │  │
│  │  │ Plugin      │ │ Plugin      │ │ (Grok/Ella)         │ │  │
│  │  │ (KEEP)      │ │ (NEW)       │ │ (PORT)              │ │  │
│  │  └─────────────┘ └─────────────┘ └─────────────────────┘ │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────────┐ │  │
│  │  │ Ella UI Shell (Optional - can supersede OMI UI)     │ │  │
│  │  └─────────────────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              OMI CORE (From Upstream)                     │  │
│  │                                                           │  │
│  │  • Hardware Layer (BLE, Limitless, Frame, etc.)          │  │
│  │  • Conversations, Memories, Apps                          │  │
│  │  • Goals, Calendar, Custom STT (NEW!)                     │  │
│  │                                                           │  │
│  │  API Base URL: https://api.ella-ai-care.com/             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              ELLA BACKEND (Our Backend)                   │  │
│  │                                                           │  │
│  │  Implements OMI endpoint contract + Ella extensions       │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### What We Keep (as Plugins)

| Plugin | Source | Effort |
|--------|--------|--------|
| **Audio Push Plugin** | Port from current AppDelegate | 1 day |
| **Wake Word Plugin** | Build new (from standalone Ella) | 2-3 days |
| **V2V Voice Plugin** | Port from standalone Ella app | 3-5 days |
| **Ella UI Shell** | Optional - simplified healthcare UI | 3-5 days |

### What We Drop (Upstream Has Better)

| Component | Why Drop |
|-----------|----------|
| `voice_mode_manager.dart` | Upstream voice mode is comprehensive |
| `ella_tts_service.dart` | Backend TTS works via API |
| `on_device_asr_service.dart` | Upstream has `on-device-stt` feature |
| `heuristics_service.dart` | Low value |

---

## 4. Backend API Compatibility

### Critical Question for Backend Team

**Can our backend maintain compatibility with OMI's endpoint contract while adding Ella extensions?**

### OMI Endpoints We Must Implement

| Endpoint | Method | Purpose | Our Status |
|----------|--------|---------|------------|
| `/v1/conversations` | GET | List conversations | ✅ Have |
| `/v1/conversations/{id}` | GET | Get single conversation | ✅ Have |
| `/v3/memories` | GET | List memories | ✅ Have |
| `/v1/messages` | POST | Chat/agent messages | ✅ Have |
| `/v4/listen` | WebSocket | Real-time transcription | ✅ Have (Deepgram proxy) |
| `/v1/apps` | GET | List available apps | ✅ Have |
| `/api/v1/tts/generate` | POST | TTS generation | ✅ Have |

### Ella-Specific Endpoints (Extensions)

| Endpoint | Purpose | Keep? |
|----------|---------|-------|
| `/api/ella/callback/scanner` | n8n scanner callback | ✅ Yes |
| `/api/ella/callback/summary` | n8n summary callback | ✅ Yes |
| `/api/ella/callback/memory` | n8n memory callback | ✅ Yes |
| `/api/v1/voice/init` | Grok V2V initialization | ✅ Yes |
| `/api/v1/testing/*` | E2E test endpoints | ✅ Yes (dev only) |

### Backend Migration Requirements

**Backend team must confirm**:

1. [ ] All OMI contract endpoints are implemented
2. [ ] Response formats match OMI schema exactly
3. [ ] WebSocket `/v4/listen` is fully compatible
4. [ ] Ella extensions don't break OMI contract
5. [ ] Can handle both OMI app and Ella app clients

### API Base URL Configuration

```python
# iOS app points here (set in .dev.env):
API_BASE_URL=https://api.ella-ai-care.com/

# Backend must handle:
# - Standard OMI endpoints (for OMI features)
# - Ella extensions (for Ella features)
```

---

## 5. Migration Path

### Phase 0: Validation (1-2 days)

**Goal**: Prove vanilla OMI works with Ella backend

```bash
# Create test branch from upstream
git fetch upstream
git checkout -b test/vanilla-upstream upstream/main

# Configure for Ella backend
echo "API_BASE_URL=https://api.ella-ai-care.com/" > .dev.env

# Update Firebase config (swap to Ella's project)

# Test
flutter run --flavor dev
```

**Success Criteria**:
- [ ] App connects to Ella backend
- [ ] Conversations load
- [ ] Memories display
- [ ] BLE device connects
- [ ] Transcription works

### Phase 1: Fresh Fork Setup (1-2 days)

```bash
# Create new main branch from upstream
git checkout -b feature/ella-v2 upstream/main

# Apply Ella configuration
# - .dev.env with Ella URLs
# - Firebase config swap
# - Bundle ID update
```

### Phase 2: Audio Push Plugin (1 day)

Port from current fork:
- `AppDelegate.swift` audio handling sections
- `BackgroundAudioPlayerPlugin`
- Notification audio payload handling

### Phase 3: Wake Word Plugin (2-3 days)

Build clean implementation:
- Port logic from standalone Ella voice app
- Create plugin interface
- Integrate with OMI conversation lifecycle

### Phase 4: V2V Voice Plugin (3-5 days)

Port from standalone Ella app:
- Grok/OpenAI realtime voice integration
- Mode switching (OMI voice vs Ella V2V)
- UI integration

### Phase 5: Ella UI Shell (Optional, 3-5 days)

If needed:
- Create simplified Ella home screen
- Healthcare-focused UI
- OMI features accessible via settings

### Phase 6: Upstream Sync Process

```bash
# Monthly sync script
git fetch upstream
git checkout feature/ella-v2
git merge upstream/main

# Plugins in lib/plugins/ won't conflict
# Env files won't conflict (gitignored)
```

---

## 6. Risk Assessment

### Low Risk

| Factor | Mitigation |
|--------|------------|
| Backend URL routing | Already configurable |
| Audio push | Already proven in current fork |
| Firebase swap | Standard flavor process |

### Medium Risk

| Factor | Mitigation |
|--------|------------|
| Backend API compatibility | Test phase 0 validates |
| V2V voice integration | Port working standalone code |
| Plugin architecture | Start simple, iterate |

### High Risk

| Factor | Mitigation |
|--------|------------|
| Unknown upstream breaking changes | Pin to specific commit initially |
| Backend contract mismatch | Backend team validates endpoints |
| Wake word detection | May need research |

### Rollback Plan

If migration fails:
1. Keep current fork branch (`main`)
2. New approach on separate branch (`feature/ella-v2`)
3. Can always return to current fork

---

## 7. Team Sign-off

### Required Approvals

This proposal requires sign-off from all team members before proceeding.

---

#### iOS Developer
**Responsibilities**: iOS app migration, plugin implementation

- [ ] I have reviewed this proposal
- [ ] I confirm the iOS migration path is feasible
- [ ] Estimated effort: _____ days

**Concerns/Comments**:
```
[iOS dev comments here]
```

**Signature**: _________________ **Date**: _________

---

#### Backend Developer
**Responsibilities**: Backend API compatibility, endpoint contract

- [ ] I have reviewed this proposal
- [ ] I confirm our backend implements OMI's endpoint contract
- [ ] I confirm Ella extensions won't break OMI compatibility
- [ ] I confirm the following endpoints are fully compatible:
  - [ ] `/v1/conversations` (GET/POST)
  - [ ] `/v3/memories` (GET)
  - [ ] `/v1/messages` (POST)
  - [ ] `/v4/listen` (WebSocket)
  - [ ] `/api/v1/tts/generate` (POST)

**Concerns/Comments**:
```
[Backend dev comments here]
```

**Signature**: _________________ **Date**: _________

---

#### Firmware Developer
**Responsibilities**: Hardware compatibility verification

- [ ] I have reviewed this proposal
- [ ] I confirm hardware integration is unaffected by this migration
- [ ] Upstream Limitless device support is: [ ] Wanted / [ ] Not needed

**Concerns/Comments**:
```
[Firmware dev comments here]
```

**Signature**: _________________ **Date**: _________

---

#### Project Manager / Product Owner
**Responsibilities**: Timeline, priorities, resource allocation

- [ ] I have reviewed this proposal
- [ ] I approve the migration timeline
- [ ] I confirm this aligns with product roadmap

**Concerns/Comments**:
```
[PM comments here]
```

**Signature**: _________________ **Date**: _________

---

## Appendix A: Files to Port (iOS)

### Must Port (Plugin Architecture)

```
# Audio Push Plugin
ios/Runner/AppDelegate.swift (audio sections only)
lib/services/notifications.dart (audio handling)

# Native Plugins
ios/Runner/BackgroundAudioPlayerPlugin.swift
ios/Runner/NativeTtsPlugin.swift (if needed)
```

### Probably Drop (Upstream Has)

```
lib/services/audio/ella_tts_service.dart
lib/services/voice_mode/voice_mode_manager.dart
lib/services/asr/on_device_asr_service.dart
lib/services/heuristics/heuristics_service.dart
```

---

## Appendix B: Backend Endpoint Contract

### OMI Contract Endpoints (Must Match)

```python
# Conversations
GET  /v1/conversations?limit=N&offset=N
GET  /v1/conversations/{id}
POST /v1/conversations/{id}/reprocess

# Memories
GET  /v3/memories?limit=N&offset=N

# Messages/Chat
POST /v1/messages

# Transcription
WS   /v4/listen?language=X&sample_rate=N&codec=X

# TTS
POST /api/v1/tts/generate
GET  /api/v1/tts/voices

# Apps
GET  /v1/apps
GET  /v1/apps/{id}
```

### Ella Extension Endpoints (Can Add)

```python
# Ella Callbacks
POST /api/ella/callback/scanner
POST /api/ella/callback/summary
POST /api/ella/callback/memory

# Voice V2
POST /api/v1/voice/init
WS   /api/v1/voice/grok

# Testing (dev only)
POST /api/v1/testing/*
```

---

## Appendix C: Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-11 | Propose modular plugin approach | Reduce maintenance, gain upstream features |
| | | |

---

## Next Steps

1. **All team members**: Review this proposal
2. **Backend dev**: Validate endpoint compatibility
3. **All**: Add comments/concerns in sign-off section
4. **Meeting**: Discuss and finalize decision
5. **If approved**: Begin Phase 0 validation

---

**Document Location**: `/FORK_MIGRATION_PROPOSAL.md` (root of repo)
**Related Docs**:
- `/app/docs/HARDWARE_INTEGRATION_DEEP_DIVE.md`
- `/app/docs/UPSTREAM_PROCESSING_CHANGES.md`
- `/app/docs/FORK_STRATEGY_ANALYSIS.md`

---

*End of Proposal*
