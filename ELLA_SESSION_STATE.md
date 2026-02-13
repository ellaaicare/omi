# Ella Session State

**Last Updated**: 2026-02-12 (n8n workflows deployed + E2E verified, DB migration applied)
**Branch**: `feature/ella-v2-fresh`
**Team**: ella-dev (use TeamCreate to respawn)

---

## What Is Ella?

Elder care AI companion built on the OMI open-source wearable platform. Alzheimer's/dementia focus. The OMI device captures audio conversations passively; Ella processes them for memories, health alerts, and caregiver summaries.

- **Backend**: OMI FastAPI backend + Ella extension layer (`backend/ella/`, `backend/utils/ella/`)
- **App**: Flutter (iOS primary target), forked from OMI with Ella branding
- **Integration**: Separate repo at `/Users/greg/repos/ella-ai/` -- LLM proxy, n8n workflows, Letta agents
- **OpenClaw production**: Running on letta-iMac (`ssh letta@100.75.8.74`, `~/clawd/`)
- **OpenClaw dev**: Setting up on admin-macbookair1 (`ssh admin@100.67.113.120`)
- **Production**: `https://api.ella-ai-care.com`, VPS at `ssh root@100.101.168.91`
- **n8n**: `n8n.ella-ai-care.com` (creds in ella-ai/.env)

---

## Architecture Decisions (FINAL)

1. **Hybrid OpenClaw + OMI Backend** -- OMI stays for audio/transcription/storage, OpenClaw becomes agent intelligence + communication layer (Phase 2)
2. **Phase 1 (NOW)**: Letta/n8n stack runs production via OMI backend hooks
3. **Phase 2**: OpenClaw goes live alongside OMI backend
4. **Phase 3**: Full OpenClaw, n8n phased out
5. **Primary user = the elder** -- they sign up first
6. **Caregiver access = invite flow** -- elder adds family/caregivers with different access levels
7. **3-tab navigation**: Home, Chat, Settings (removed Action Items, Apps)
8. **Teal brand**: #14B8A6, elder-friendly sizing (18px min body, 48dp touch targets)

---

## Completed Work (All Committed + Pushed to origin/feature/ella-v2-fresh)

### Backend (LIVE on api.ella-ai-care.com)
- [x] Ella summary adapter bug fix (3-tuple return type)
- [x] `POST /v1/ella/notification` -- TTS via OpenAI + GCS upload + FCM push
- [x] `POST /v1/ella/emergency` -- FCM to elder + n8n dispatch for SMS/calls
- [x] `POST /v1/ella/daily-summary` -- Dispatches to n8n for caregiver summaries
- [x] `POST /v1/ella/emergency-contact` -- Create emergency contact (Firestore CRUD, requires uid in body)
- [x] `GET /v1/ella/emergency-contacts/{uid}` -- List contacts
- [x] `PUT /v1/ella/emergency-contact/{id}` -- Update contact
- [x] `DELETE /v1/ella/emergency-contact/{id}` -- Delete contact (uid as query param)
- [x] `GET /v1/ella/caregiver-dashboard-data` -- Dashboard data with HMAC token auth
- [x] `POST /v1/ella/generate-dashboard-token` -- Token generation for dashboard links
- [x] `GET /v1/ella/health` -- Health check (lists all endpoints)
- [x] Config: emergency_endpoint, daily_summary_endpoint, ELLA_CONFIG consistency
- [x] All 116 backend tests pass
- [x] All endpoints smoke tested in production (CRUD create/list/delete verified)

### Caregiver Invite Flow (All Committed + Pushed + DEPLOYED)
- [x] `POST /v1/ella/caregivers/invite` -- 6-digit invite code, 7-day expiry, max 5 caregivers, duplicate phone check
- [x] `GET /v1/ella/caregivers` -- List caregivers with permissions
- [x] `DELETE /v1/ella/caregivers/{id}` -- Remove caregiver
- [x] `PUT /v1/ella/caregivers/{id}/permissions` -- Update daily summary toggle (emergency always on)
- [x] `POST /v1/ella/caregivers/resend-invite` -- Fresh invite code
- [x] Firestore CRUD at `users/{uid}/ella_caregivers/{id}`
- [x] All 5 endpoints smoke tested in production (201/200/204 verified)
- [x] ~57 l10n keys added + flutter gen-l10n regenerated
- [x] EllaSettingsPage -- Full settings tab with care team, device, about sections
- [x] EllaCareTeamPage -- Caregiver list, empty state, add button, spots counter
- [x] EllaAddCaregiverPage -- Name/phone/email/relationship form, permission toggles, validation
- [x] EllaInviteSentScreen -- Teal checkmark animation, auto-pop 3s
- [x] EllaCaregiverDetailPage -- View/edit/remove with confirmation dialog, resend invite
- [x] EllaEmergencyContactPage -- Radio list to select primary emergency contact
- [x] 4 reusable widgets: settings row, caregiver row, relationship picker, permission toggle
- [x] Caregiver + InviteResponse data models
- [x] CaregiverAPI service layer (5 methods)

### Flutter App (All Committed + Pushed)
- [x] Theme swap -- `ellaThemeData()` replaces stock OMI theme
- [x] 3-tab bottom nav -- Home/Chat/Settings (removed Action Items, Apps, record button)
- [x] Color migration -- 412 replacements across 96 files (deepPurple, bg colors, text colors -> EllaColors)
- [x] Zero remaining hardcoded OMI colors
- [x] FCM notification handler -- `EllaNotificationHandler` with just_audio playback
- [x] Emergency button widget -- 72dp red button with pulse animation, full-screen overlay, 10-second cancel flow, haptics, accessibility, API integration
- [x] Emergency audio player -- just_audio + flutter_tts fallback
- [x] Elder onboarding -- 3-screen flow (Welcome/Connect/Emergency) with auth gate, PageView transitions, l10n strings, BLE pairing via OnboardingProvider
- [x] Onboarding wrapper gate -- `isEllaApp = true` redirects from 9-step OMI to 3-step Ella
- [x] 23 l10n keys added for onboarding
- [x] App builds and runs in iOS simulator (iPhone 16e, iOS 26.1, dev flavor)
- [x] MobileApp Ella routing gate -- bypasses DeviceSelectionPage, routes to EllaOnboarding
- [x] Firebase init crash fix -- try-catch for duplicate-app on simulator
- [x] Debug auth bypass -- kDebugMode skips Firebase auth for simulator testing
- [x] Onboarding screens visible in simulator -- "Hi there!" Welcome screen confirmed working

### Caregiver Dashboard (DEPLOYED + E2E VERIFIED)
- [x] Static HTML dashboard deployed to VPS at `https://ella-ai-care.com/dashboard/`
- [x] Caddy serves it automatically, no config needed
- [x] Token-based auth via URL param, fetches from `/v1/ella/caregiver-dashboard-data`
- [x] Token generation works: `GET /v1/ella/generate-dashboard-token?uid=X&caregiver_id=Y` (query params, NOT JSON body)
- [x] Dashboard data endpoint returns conversations + memories for the token's uid
- [x] CORS fix applied in Caddy config (api.ella-ai-care.com -> ella-ai-care.com)
- [x] Error handling verified: invalid/expired tokens return 401, missing params return 422
- [ ] ELLA_DASHBOARD_SECRET not set (using fallback "ella-dashboard-dev-secret") -- Greg needs to set proper secret

### UX Specs (Written, Ready for Implementation)
- [x] `app/lib/ella/ELLA_THEME_SPEC.md` -- Colors, typography, touch targets, nav
- [x] `app/lib/ella/ELLA_ONBOARDING_SPEC.md` -- 3-screen elder onboarding flow
- [x] `app/lib/ella/ELLA_EMERGENCY_BUTTON_SPEC.md` -- Emergency button spec (15 sections)
- [x] `app/lib/ella/ELLA_CAREGIVER_INVITE_SPEC.md` -- Caregiver invite flow (14 sections)

### Product
- [x] `backend/ella/docs/MVP_FEATURE_SPECS.md` -- 4 MVP specs

### n8n Workflows (DEPLOYED + E2E VERIFIED)
- [x] `ella-ai/services/n8n-workflows/cron-daily-summary-v1.json` -- Hourly cron, timezone-aware (not deployed yet)
- [x] `ella-ai/services/n8n-workflows/callback-daily-summary-v1.json` -- On-demand webhook (not deployed yet)
- [x] **7 caregiver workflows** deployed to n8n.ella-ai-care.com and ACTIVE (see IDs below)
- [x] **1 provisioning workflow** deployed but INACTIVE (needs ELLA_PROVISION_API_KEY)
- [x] **1 Omi→OpenClaw routing** deployed but INACTIVE (needs OpenClaw agent provisioning)
- [x] **Prisma DB migration** applied directly to ella-postgres (ALTER TABLE for missing columns + indexes)
- [x] Full E2E smoke test: invite→list→set-emergency→get-emergency→permissions→resend→remove all passing

### Infrastructure
- [x] VPS OPENAI_API_KEY and GOOGLE_APPLICATION_CREDENTIALS verified set
- [x] claude-mem PATH fix (uvx symlinked to ~/.bun/bin/)
- [x] ELLA_SESSION_STATE.md + CLAUDE.md pointer for crash recovery

---

## Not Yet Done (Next Session)

### High Priority (MVP)
| Task | Notes |
|------|-------|
| ~~Replace splash/launch screen images~~ | DONE - Grok-generated images for splash + 3 onboarding screens. LaunchImage @1x/2x/3x updated. |
| ~~Caregiver invite flow implementation~~ | DONE - see completed work below |
| Wire native splash integration | `ella_splash.png` generated but needs `flutter_native_splash` package config to replace the storyboard-based splash |
| Wire dashboard token URL into email template | The "View Full Dashboard" button in daily summary email needs tokenized URL |
| ~~Deploy n8n caregiver workflows~~ | **DONE** — 9 workflows deployed, 7 active, 2 inactive. Full E2E verified. |
| Deploy n8n daily summary workflows | Import to n8n -- BLOCKED on credentials (see below) |
| Set XAI_API_KEY on VPS | Needed for Grok chat streaming endpoint to work in production |
| ~~Wire OpenClaw chat endpoint~~ | **DONE + E2E VERIFIED**. Full chain: App→VPS Backend→LLM Proxy (`:8100`)→OpenClaw webhook (macbookair1 `:8090`)→Kimi K2.5. Response: "moonshotai/kimi-k2.5". See `backend/ella/docs/CHAT_FLOW_ARCHITECTURE.md`. |
| Register Ella chat apps in backend | Create two "apps" (Ella Quick Chat + Ella AI Companion) selectable in chat UI |
| Physical device testing | Test emergency button, notifications, BLE on real iPhone |
| Fix Xcode code signing for watch companion | Needed for `flutter run` hot reload (Team "9536L8KLMP") |

### Needs Greg
| Task | Notes |
|------|-------|
| ~~n8n Gmail/SMTP credential~~ | **DONE** — Greg refreshed Gmail OAuth2 credential (ID: hlKJHF9C8NL7Blua). Wired into Invite + Resend workflows. E2E verified. |
| ~~ELLA_PROVISION_API_KEY in n8n~~ | **DONE** — Added to /opt/n8n/.env + docker-compose.yml. Both provisioning workflows now ACTIVE. |
| OMI_BACKEND_URL for n8n | Set in n8n container env (probably `http://localhost:8000` or `http://host.docker.internal:8000`) |
| OMI_MCP_SERVICE_KEY for n8n | Service key for authenticated OMI API calls |
| Twilio account setup | For SMS/voice calls in emergency alerts + caregiver notifications |
| ELLA_DASHBOARD_SECRET env var | Set on VPS for dashboard token signing |
| ~~Ella branding assets~~ | DONE - Grok-generated splash + onboarding illustrations |

### In Progress
| Task | Notes |
|------|-------|
| ~~**n8n caregiver webhooks**~~ | **DONE + DEPLOYED** — 9 workflows on n8n.ella-ai-care.com. 7 caregiver ACTIVE, 2 provisioning INACTIVE. Full E2E smoke test passing. DB migration applied to ella-postgres. |
| **OpenClaw agent migration** | APPROVED — Issue #55 has full spec from OpenClaw team (2-agent model: user + caregiver with shared folder). Provisioning script done on letta-iMac. |
| ~~**Role-based dashboard**~~ | **DONE** — 5 access levels (admin/elevated_family/doctor/family/limited) with 10-permission matrix. Family views at `/family`, workspace viewer, token auth. Issue #56 on ella-ai repo. |
| ~~**Web onboarding flow**~~ | **DONE** — 4-step wizard at `/onboarding`. Firebase auth + form → ella-provision-user webhook. Elder-friendly sizing (18px/48dp). |

### Recently Completed (This Session — Integration Layer)
| Task | Notes |
|------|-------|
| **Prisma schema migration** | `agentServerUrl`, `conditions`, `medications` on User. `inviteCode`, `accessLevel`, `isEmergencyContact` on Caregiver. `updatedAt` on Caregiver + AgentCluster. AgentCluster.agents JSONB documented for OpenClaw format. Migration SQL ready. |
| **8 n8n workflow JSONs** | 7 caregiver webhooks + 1 provisioning. Match iOS caregiver_api.dart contract exactly. SQL input sanitization in Code nodes. |
| **Role-based dashboard** | `src/lib/roles.ts` (5 levels, 10 permissions), `RoleGate` component, `/family` route group, workspace viewer, token auth (HMAC-SHA256, 30-day expiry). |
| **Web onboarding** | `/onboarding` route: Welcome→Account→PersonalInfo→Success. Firebase email+Google auth. Calls ella-provision-user webhook. |
| **Code review + fixes** | Fixed: updated_at on caregivers/agent_clusters, SQL injection sanitization, OpenClaw agent ID support in workspace API, hardcoded token secret removed, token expiry→30 days, date_of_birth in provisioning. |
| **GitHub Issue #56** | Full summary posted to ellaaicare/ella-ai for OpenClaw team review. |
| **iOS handoff PRD** | `docs/IOS_INTEGRATION_HANDOFF.md` — model updates needed for invite_code, access_level, is_emergency_contact. |

### Architecture Decisions (New — 2026-02-12)
| Decision | Rationale |
|----------|-----------|
| **Keep existing Postgres** | 18 Prisma models, n8n connections, dashboard queries already wired. Evolve schema with new OpenClaw tables, don't create new DB. |
| **2-agent model approved** | User agent + caregiver agent (with shared folder). Replaces 5-agent Letta clusters. Caregivers get isolated sessions via `dmScope: per-channel-peer`. |
| **Prod = ellas-mac-mini-1** | M4 Mac Mini at 100.76.138.56. NOT letta-iMac (legacy). Dev/test = admin-MacBookAir1 (100.67.113.120). |
| **Web onboarding parallel to iOS** | Faster iteration on UX, then mirror to iOS when finalized. |

### Recently Completed (This Session — n8n Deployment)
| Task | Notes |
|------|-------|
| **9 n8n workflows deployed** | ALL 9 ACTIVE. 7 caregiver + 1 provisioning + 1 Omi routing. ELLA_PROVISION_API_KEY added to n8n container. |
| **Gmail email on invite** | Both Invite and Resend-Invite workflows now send teal-themed HTML email via Gmail (credential hlKJHF9C8NL7Blua). Fire-and-forget (continueOnFail=true). E2E verified: threadId returned, labelIds=['SENT']. |
| **OpenClaw branch merged** | `feature/openclaw-integration` merged to main on ella-ai repo. Pushed to origin. Prisma schema + migration + routing sync + templates all in main now. |
| **Docker network fix** | n8n compose was using wrong ella-network name (docker_ella-network vs ella-ai_ella-network). Fixed in docker-compose.yml and reconnected container. |
| **DB schema reconciled** | Fixed conditions/medications to TEXT[] (was TEXT), invite_code to VARCHAR(6), access_level to VARCHAR(20), timestamps to TIMESTAMP(3). Partial indexes recreated. |
| **Prisma DB migration applied** | ALTER TABLE on ella-postgres: users (agent_server_url, conditions, medications), caregivers (invite_code, invite_code_expiry, invite_attempts, last_attempt_at, access_level, is_emergency_contact, updated_at), agent_clusters (updated_at). 3 indexes created. |
| **Full E2E smoke test** | invite(201)→list(200)→set-emergency(200)→get-emergency(200)→permissions(200)→resend(200)→remove(200). All passing. |
| **n8n workflow pattern** | Resolved 5 critical n8n issues: Postgres v2.4 vs v2.5, Webhook v2 body nesting, empty result handling, IF node v2 unreliability, JSONB path conflicts. Documented in session state for future reference. |
| **Gmail credential discovery** | Confirmed no Gmail/SMTP/email credential exists in n8n (97 workflows scanned). Still needed for invite emails. |

### Recently Completed (This Session — Architecture Cleanup)
| Task | Notes |
|------|-------|
| **Removed caregiver endpoints from OMI backend** | All `/v1/ella/caregivers/*` endpoints deleted from `callbacks.py`. OMI repo is OMI-only. |
| **iOS caregiver API → n8n webhooks** | `caregiver_api.dart` now calls `n8n.ella-ai-care.com/webhook/caregiver-*` instead of OMI backend. Uses `http` package directly (no Firebase auth needed for n8n). |
| **Caregiver sign-on PRD** | Full PRD at `ella-ai/docs/caregiver-onboarding/ELLA_CAREGIVER_SIGNON_PRD.md`. |
| **iOS invite UX** | Email required (primary), phone optional. Invite code display (36px monospace, tap to copy), share button, removed auto-pop. |
| **Join page + email templates** | 9 production-ready files moved to `ella-ai/docs/caregiver-onboarding/`. |
| **OpenClaw migration issue** | GitHub Issue #55: full schema docs, migration plan, questions for OpenClaw team. |
| **Session handoff doc** | `backend/docs/ELLA_SESSION_HANDOFF_2026_02_12.md` — complete state for new session. |

### Recently Completed (This Session - OpenClaw Chat E2E)
| Task | Notes |
|------|-------|
| **Simplified chat architecture** | Abandoned complex LLM Proxy + graph chat. Now: App → `/v1/ella/chat/stream` (debug level 4) → OpenClaw Gateway `:19001` → Pony model. Single message, single response. |
| Debug level 4 (OpenClaw) | Added `_stream_level_4_openclaw()` to `backend/ella/routers/chat.py`. Calls gateway's OpenAI-compatible `/v1/chat/completions`. Deployed to VPS. |
| VPS config for OpenClaw | Set `ELLA_DEBUG_LEVEL=4`, `OPENCLAW_URL=http://100.67.113.120:19001`, `OPENCLAW_GATEWAY_TOKEN` in VPS `.env`. Backend restarted, E2E verified with curl. |
| Flutter Ella chat wiring | Added `sendEllaMessageStream()` in `messages.dart` — calls `/v1/ella/chat/stream`, parses OMI SSE format. Provider routes to it when `isEllaApp=true`. |
| SSE format fix (thinking bug) | Changed backend from OpenAI SSE (`data: {"choices":[...]}`) to OMI-compatible format (`data: <raw text>` + `done: <base64 JSON>`). Fixes 1024-byte buffer splitting in `makeStreamingApiCall`. |
| **SSE keep-alive (30s timeout fix)** | Backend sends `: keepalive\n\n` every 5s while waiting for OpenClaw. App's `sendEllaMessageStream()` skips SSE comments (lines starting with `:`). Caddy gets `flush_interval -1`. Tested: 24s response stays alive. |
| **EllaThinkingIndicator** | Animated widget: pulsing teal dot, animated dots ("Ella is thinking..."), elapsed timer after 5s, text changes at 8s/15s. Replaces static `ShimmerWithTimeout`. |
| API docs | `backend/ella/docs/ELLA_CHAT_API_SPEC.md` + `OPENCLAW_SESSIONS_GUIDE.md` |
| Test suite | `test_chat_chain.py` — 4-node test suite (OpenClaw local/remote, proxy, backend). 5/5 backend tests pass. |
| Chat flow architecture doc | `backend/ella/docs/CHAT_FLOW_ARCHITECTURE.md` — full documentation |
| LLM Proxy v2.0 | Built but no longer in critical path — Ella uses direct OpenClaw gateway call now |

### Previously Completed (Design Overhaul)
| Task | Notes |
|------|-------|
| Light mode theme | EllaColors updated: bgPrimary=#FAFAF8 (warm off-white), textPrimary=#1A1A1A (near-black). ThemeData rebuilt with Brightness.light, ColorScheme.light, white cards, light inputs, dark status bar icons. ThemeMode.light in main.dart. |
| Hardcoded color fixes (11 files) | Fixed ~50+ instances of Colors.white/Colors.black assumptions in chat page, AI/user message widgets, markdown widget, chart widget, typing indicator, voice recorder, message action menu, files handler. User bubble now teal instead of dark grey. |
| Navigation black screen fix | Removed duplicate ChatPage push in home/page.dart deep link handler. Chat AppBar hides back button when isPivotBottom=true (tab mode). |
| Hide OMI widgets for Ella | DailyScoreWidget and OutOfCreditsWidget now return SizedBox.shrink() when isEllaApp=true. |
| Grok chat streaming endpoint | `POST /v1/ella/chat/stream` - SSE streaming from xAI Grok API with Ella system prompt. Registered in FastAPI app, listed in health endpoint. |
| Splash + onboarding images | 4 Grok-generated watercolor illustrations: splash (elderly woman, teal heart), welcome (grandmother+grandchild), connect (wearable device), emergency (teal shield). Saved to app/assets/images/ella_*.png + iOS LaunchImage set. |
| Onboarding screen visual polish | All 3 screens updated: images added (200-240px), light backgrounds, dark text, white input fields with teal focus borders. Elder-friendly sizing preserved. |
| All tests passing | 14 backend + 21 app tests pass. |

### Previously Completed
| Task | Notes |
|------|-------|
| Caregiver invite flow (FULL) | 21 commits: Firestore CRUD, 5 backend endpoints, ~57 l10n keys, 12 Flutter files. All endpoints smoke tested in production. |
| iOS simulator working | App loads, onboarding screens visible. Fixed: MobileApp routing, Firebase init crash, debug auth bypass. |
| OpenClaw dev instance | COMPLETE on admin-macbookair1. Gateway port 19001, webhook port 8090. |
| OpenClaw webhook wiring | COMPLETE. Scanner (Kimi K2.5), summary + memory (OpenClaw agent). All E2E tested from VPS. |
| Dashboard E2E | COMPLETE. Token flow works, CORS fixed in Caddy. ELLA_DASHBOARD_SECRET still needs setting. |
| Emergency contact smoke test | All CRUD operations verified. |
| Legacy letta-server Docker | STOPPED on letta-iMac. Freed ~108MB RAM. |
| claude-mem fixed | Symlinked both `uvx` and `uv` to `~/.bun/bin/`. Worker restarted. |

### Phase 2 (Lower Priority)
| Task | Notes |
|------|-------|
| OpenClaw Phase 2 architecture | Hybrid architecture design, webhook receiver, container isolation |
| Cognitive recall prompts in chat | MVP Spec #4, Alzheimer's-specific companion feature |
| Caregiver web dashboard (full) | React/Next.js version with real-time updates |
| Re-enable Tailscale netfilter on dev server | Currently disabled (`--netfilter-mode=off`), iptables/nftables flushed. Restore once ACL is confirmed working. |

---

## Key Ella Files

### Backend (Python)
- `backend/ella/routers/callbacks.py` -- All Ella endpoints (notification, emergency, contacts, daily-summary, dashboard)
- `backend/ella/config.py` -- EllaConfig dataclass with all settings
- `backend/database/ella_contacts.py` -- Firestore CRUD for emergency contacts
- `backend/utils/ella/summary.py` -- Summary adapter (hooks into conversation processing)
- `backend/utils/llm/conversation_processing.py` -- Upstream file with Ella hook

### App (Flutter/Dart)
- `app/lib/ella/ella_theme.dart` -- EllaColors, EllaSizes, ellaThemeData()
- `app/lib/ella/widgets/ella_emergency_button.dart` -- Emergency button widget
- `app/lib/ella/widgets/ella_emergency_overlay.dart` -- Emergency overlay with cancel
- `app/lib/ella/services/emergency_api.dart` -- Emergency API calls
- `app/lib/ella/services/emergency_audio_player.dart` -- TTS audio player
- `app/lib/ella/models/emergency.dart` -- Emergency data models
- `app/lib/services/notifications/ella_notification_handler.dart` -- FCM handler
- `app/lib/widgets/bottom_nav_bar.dart` -- 3-tab Ella nav
- `app/lib/pages/home/page.dart` -- Simplified home with emergency button
- `app/lib/pages/onboarding/ella/ella_onboarding.dart` -- 3-screen onboarding wrapper
- `app/lib/pages/onboarding/ella/ella_welcome.dart` -- Screen 1: Welcome + name
- `app/lib/pages/onboarding/ella/ella_connect.dart` -- Screen 2: BLE pairing
- `app/lib/pages/onboarding/ella/ella_emergency.dart` -- Screen 3: Emergency contact
- `app/lib/pages/onboarding/wrapper.dart` -- Modified with isEllaApp gate

### Caregiver Invite Flow (Flutter/Dart)
- `app/lib/ella/models/caregiver.dart` -- Caregiver + InviteResponse data models
- `app/lib/ella/services/caregiver_api.dart` -- API calls: invite, list, remove, permissions, resend
- `app/lib/ella/widgets/ella_settings_row.dart` -- Reusable settings row (64dp, icon, chevron)
- `app/lib/ella/widgets/ella_caregiver_row.dart` -- Caregiver list row (avatar, status dot)
- `app/lib/ella/widgets/ella_relationship_picker.dart` -- Bottom sheet picker (7 options)
- `app/lib/ella/widgets/ella_permission_toggle.dart` -- Toggle with lock support
- `app/lib/ella/pages/ella_settings_page.dart` -- Settings tab (care team, device, about sections)
- `app/lib/ella/pages/ella_care_team_page.dart` -- Caregiver list + empty state + add button
- `app/lib/ella/pages/ella_add_caregiver_page.dart` -- Add caregiver form with validation
- `app/lib/ella/pages/ella_invite_sent_screen.dart` -- Success screen with animation + auto-pop
- `app/lib/ella/pages/ella_caregiver_detail_page.dart` -- View/edit/remove caregiver
- `app/lib/ella/pages/ella_emergency_contact_page.dart` -- Select primary emergency contact

### Caregiver Backend
- `backend/database/ella_caregivers.py` -- Firestore CRUD for caregivers
- `backend/ella/routers/callbacks.py` -- 5 new endpoints: invite, list, delete, permissions, resend

### Specs (Reference)
- `app/lib/ella/ELLA_THEME_SPEC.md`
- `app/lib/ella/ELLA_ONBOARDING_SPEC.md`
- `app/lib/ella/ELLA_EMERGENCY_BUTTON_SPEC.md`
- `app/lib/ella/ELLA_CAREGIVER_INVITE_SPEC.md`
- `backend/ella/docs/MVP_FEATURE_SPECS.md`

### Splash/Launch Images (UPDATED)
- `app/assets/images/ella_splash.png` -- Grok-generated watercolor splash (elderly woman, teal heart)
- `app/assets/images/ella_onboarding_1.png` -- Welcome illustration (grandmother+grandchild)
- `app/assets/images/ella_onboarding_2.png` -- Connect device illustration (wearable)
- `app/assets/images/ella_onboarding_3.png` -- Emergency illustration (teal shield)
- `app/ios/Runner/Assets.xcassets/LaunchImage.imageset/LaunchImage*.png` -- Updated with ella_splash
- `app/assets/images/splash.png` -- Still OMI branding (can delete, replaced by ella_splash)
- `app/assets/images/splash_icon.png` -- Still OMI branding (can delete)
- `app/assets/images/onboarding-bg-*.webp` -- Old OMI onboarding backgrounds (can delete)
- `app/assets/images/onboarding.mp4` -- Old OMI onboarding video (can delete)

### Backend Chat
- `backend/ella/routers/chat.py` -- Grok streaming chat endpoint (POST /v1/ella/chat/stream)
- `backend/ella/docs/CHAT_FLOW_ARCHITECTURE.md` -- **CRITICAL**: Full chat flow documentation (App→Backend→LLM Proxy→OpenClaw)
- `backend/utils/llm/clients.py` -- LLM client config + Ella proxy patch (injects UID into all LLM calls)

### Agent State Files (for session continuity)
- `app/lib/ella/state/ios-1-state.md` -- Nav fixes + widget hiding + onboarding screens
- `app/lib/ella/state/ios-2-state.md` -- Light theme + hardcoded color fixes
- `backend/ella/state/backend-state.md` -- Grok chat endpoint
- `app/lib/ella/state/ux-designer-state.md` -- Image generation

---

## OpenClaw Infrastructure

### Production (ellas-mac-mini-1, 100.76.138.56)
- M4 Mac Mini — production OpenClaw target
- NOT letta-iMac (that's legacy, being phased out)
- Needs OpenClaw gateway setup (pending provisioning script)

### Legacy (letta-iMac, 100.75.8.74)
- OpenClaw v2026.2.3 CLI, v2026.1.30 service
- Agent: "Ella" with Kimi K2.5 model — Greg's personal agent
- Has `ella-routing-sync.py` at `~/.openclaw/workspace/scripts/` (routing only, not full provisioning yet)
- Workspace templates: SOUL.md, USER.md, AGENTS.md, IDENTITY.md, MEMORY.md (personalized for Greg, not templatized)
- Port: 18789 (override from base 42858)
- 5 cron jobs: sleeptime-learning, dreamtime, daily-wakeup, morning-x-snapshot, afternoon-brief
- SearXNG search on localhost:8888
- Legacy letta-server Docker on port 8283 (approved to stop)

### Dev (admin-macbookair1, 100.67.113.120)
- Ubuntu 24.04, 8GB RAM, 4 cores, 458GB disk
- Node.js v22.22.0 installed via nvm
- git, jq, python3-pip, python3-venv installed
- OpenClaw Gateway v2026.2.3 on port 19001 (localhost only) -- systemd: `openclaw-gateway.service`
- Webhook Receiver (FastAPI) on port 8090 (Tailscale) -- systemd: `ella-webhook-receiver.service`
- Workspace: `/home/plato/ella-dev/`
- Identity: "Ella (Dev)" -- headless, no Telegram
- Model: Kimi K2.5 (NVIDIA, free), Heartbeat: Nemotron Nano 9B
- Webhook secret: (see ella-ai/.env)
- Gateway token: (see ella-ai/.env)
- Tailscale ACL: `tag:vps` -> `tag:workstation:8090,19001`
- SSH: `ssh plato@admin-macbookair1` or `ssh plato@100.67.113.120`
- NOTE: Tailscale netfilter disabled, iptables/nftables flushed -- revisit security later

#### Webhook Endpoints (v0.2.0, all E2E verified from VPS)
| Endpoint | Method | Backend | Latency | Notes |
|----------|--------|---------|---------|-------|
| `/webhook/health` | GET | -- | <100ms | No auth required |
| `/webhook/chat` | POST | OpenClaw agent (Kimi K2.5) | 10-30s | Interactive chat. Body: `{uid, message, conversation_id, source}`. Returns `{reply, uid, session_id}`. E2E verified from VPS via LLM proxy. |
| `/webhook/scanner` | POST | NVIDIA Kimi K2.5 direct API | 10-25s | Urgency classification (EMERGENCY/URGENT/QUESTION/ROUTINE) |
| `/webhook/summary` | POST | OpenClaw agent (Kimi K2.5) | 15-25s | Structured JSON summary with title, overview, action_items, mood, topics |
| `/webhook/memory` | POST | OpenClaw agent (Kimi K2.5) | 20-30s | Memory extraction with content, category, importance |

- Scanner latency: ~10-25s (Kimi K2.5 reasoning model). Groq would be <1s but no key available yet.
- All endpoints require `X-Ella-Webhook-Secret` header (except health)
- Webhook receiver code: `/home/plato/ella-dev/services/omi-webhook-receiver/main.py`

---

## n8n Details (for workflow deployment)

- **URL**: n8n.ella-ai-care.com (port 5678 on VPS)
- **Auth**: Basic auth (creds in ella-ai/.env)
- **API Key**: (in ella-ai/.env)
- **Current state**: 9 Ella workflows deployed (7 active, 2 inactive), ~97 total workflows
- **Postgres credential**: ID `tFirqSRMiRXJWDvc` / name "Postgres account" — connects to `ella_ai` database
- **Missing env vars**: OMI_BACKEND_URL, OMI_MCP_SERVICE_KEY, ELLA_PROVISION_API_KEY
- **Missing credentials**: Gmail/SMTP (does NOT exist — user thought it did but it doesn't), Twilio (low priority)

### Deployed Ella Workflow IDs
| Workflow | n8n ID | Status | Webhook Path |
|----------|--------|--------|--------------|
| Provision User v1.1 | Wcre1vvxTPnv8oqf | ACTIVE | `/webhook/ella-provision-user` |
| Caregiver Invite v1.1 | aZ2PMP2El1oxWbb3 | ACTIVE | `/webhook/caregiver-invite` |
| Caregiver List v1.1 | viQbSPhkGPJoZidk | ACTIVE | `/webhook/caregiver-list` |
| Caregiver Remove v1.1 | JCCO4oGaB0MxB4Cy | ACTIVE | `/webhook/caregiver-remove` |
| Caregiver Permissions v1.1 | OcQALaX6j0bKVnnv | ACTIVE | `/webhook/caregiver-permissions` |
| Caregiver Resend Invite v1.1 | YcRZmnbytANT2rKD | ACTIVE | `/webhook/caregiver-resend-invite` |
| Caregiver Set Emergency v1.1 | gc4twPZbk0FLqSg4 | ACTIVE | `/webhook/caregiver-set-emergency` |
| Caregiver Get Emergency v1.1 | RX4Mxyhs2LqxMuPi | ACTIVE | `/webhook/caregiver-get-emergency` |
| Omi to OpenClaw Routing v1.0 | YIP2PaRPET8ejIzp | ACTIVE | `/webhook/omi-openclaw-webhook` |

### n8n Workflow Development Notes (CRITICAL for future edits)
- **Postgres node**: MUST use typeVersion 2.4 (NOT 2.5) with `=` prefix on query strings: `"query": "={{ $json._sql }}"`
- **Webhook v2**: Body is at `$json.body`, not flat. Code nodes must do `$input.first().json.body || $input.first().json`
- **Empty results**: Add `alwaysOutputData: true` on ALL Postgres nodes + Code node to handle 0-row results
- **IF node v2**: `isNotEmpty` is unreliable on Postgres output. Use Code node to set a boolean, then IF on the boolean
- **JSONB paths**: PostgreSQL `'{key}'` conflicts with n8n `{{ }}`. Build entire SQL in Code node, pass as `$json._sql`
- **SQL injection**: All user input escaped via `esc()`: `String(val).replace(/'/g, "''")`

---

## Team Setup (for respawning)

When respawning the team, use `TeamCreate` with name `ella-dev`, then spawn agents:

1. **backend** -- Owns `backend/ella/`, `backend/utils/ella/`, `backend/database/ella_contacts.py`, VPS deployment.
2. **ios** -- Owns `app/lib/ella/`, theme, nav, widgets, onboarding. Can build/run in simulator.
3. **integrations** -- Owns `/Users/greg/repos/ella-ai/`, n8n workflows. Blocked on n8n creds.
4. **openclaw-engineer** -- Owns OpenClaw setup on letta-iMac + dev server. Setting up dev instance.
5. **ux-designer** (optional) -- Owns specs. Next: splash screen assets, design review.
6. **product-manager** (optional) -- Vision, MVP validation, prioritization.

---

## Recent Git History (feature/ella-v2-fresh)

```
2c1a951de feat(ella): integrate settings page as tab 2 in home nav
56cbf5134 feat(ella): add emergency contact selection page
5ba89617f feat(ella): add caregiver detail page with edit/remove
9659b5962 feat(ella): add invite sent success screen with animation
647c45f96 feat(ella): add caregiver invite form with validation
9256309d8 feat(ella): add care team page with empty state and caregiver list
b71607926 feat(ella): add settings page with care team, device, and about sections
440274f36 feat(ella): add permission toggle widget with lock support
0dd9bb1fd feat(ella): add relationship bottom sheet picker
0495df706 feat(ella): add caregiver list row widget
dddbae8b2 feat(ella): add reusable settings row widget
c0b4dfff2 feat(ella): add caregiver API service layer
d1338916c feat(ella): add caregiver and invite response data models
3d38af227 chore(ella): regenerate l10n files for caregiver invite keys
b5a3235b0 feat(ella): add l10n keys for caregiver invite flow
2fc721912 feat(ella): add caregiver invite/list/remove/permissions endpoints
001cffac1 feat(ella): add Firestore CRUD layer for caregivers
6af13bb75 feat(ella): add debug auth bypass for simulator testing
dd1455b50 fix(ella): handle Firebase duplicate-app init in simulator
25c351be9 feat(ella): add Ella routing gate in MobileApp
fbabec8c7 fix(ella): include uid in emergency contact POST body
c723c9555 feat(ella): add l10n keys for elder onboarding screens
3afc94395 feat(ella): gate onboarding wrapper to use Ella 3-screen flow
326068daa feat(ella): implement 3-screen elder onboarding flow
e9ddb3d48 chore(ella): add flutter_tts dependency for emergency audio fallback
5b33ad2d4 refactor(ella): migrate all colors from OMI deepPurple to Ella teal
3df692539 feat(ella): simplify home page with 3-tab nav and emergency button
578e4d78e feat(ella): add emergency button widget system
4c52a7b10 feat(ella): add caregiver dashboard endpoints + align contact model to spec
34c8c8326 feat(ella): add emergency contact CRUD endpoints
```

---

## How to Resume

```
1. Read this file (ELLA_SESSION_STATE.md)
2. Run `git status` and `git log --oneline -10` to see current state
3. Check `~/.claude/tasks/ella-dev/` for task list (if team exists)
4. Respawn team if needed (see Team Setup above)
5. Continue with "Not Yet Done" items above
```
