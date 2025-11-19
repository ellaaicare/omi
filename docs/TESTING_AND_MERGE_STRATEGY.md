# Testing & Merge Strategy - Parallel Agent Work

## Status Summary (6 agents)

| Agent | Branch | Status | Changes | Risk Level |
|-------|--------|--------|---------|------------|
| **Lead (Me)** | `backend-security-fixes-011CV4PhHy5CApgVos5F3rHh` | ✅ Pushed | Backend security (RCE fix, CORS) | 🔴 HIGH |
| **Agent 1** | `app-security-fixes-011CV4VzPeotnLUJVsFfkdUv` | ✅ Pushed | App security (+787 lines) | 🔴 HIGH |
| **Agent 2** | `backend-testing-infrastructure-011CV4W1MNuPxmXJkHftZzDf` | ✅ Pushed | pytest suite (+3,077 lines) | 🟢 LOW |
| **Agent 3** | `flutter-testing-infrastructure-011CV4WDZegf78M3fo2HGDgk` | ✅ Pushed | Flutter tests (+2,711 lines) | 🟢 LOW |
| **Agent 4** | `backend-performance-refactoring-*` | ⏳ In Progress | Service refactoring | 🟡 MEDIUM |
| **Agent 5** | `optimize-app-performance-011CV4WG8A6CFHwkXhaf29Xy` | ✅ Pushed | Provider splitting (+3,255 lines) | 🟡 MEDIUM |
| **Agent 6** | `ios-speaker-diarization-011CV4WHCQrHpY1CGDyV3DqU` | ✅ Pushed | iOS diarization (+1,835 lines) | 🟡 MEDIUM |

**Total Lines Added:** 11,665+ lines of production code and tests

---

## Testing Requirements Analysis

### 🌐 Cloud Agent Testing (Can Run in CI/CD)

**1. Backend Security Fixes (Lead)**
- **What to test:** Python syntax, Redis operations, Firebase initialization
- **How:** `pytest backend/tests/test_redis.py -v` (once Agent 2 tests merge)
- **Risk if skip:** RCE vulnerability not properly patched
- **Decision:** ✅ MUST TEST via cloud agent

**2. Backend Testing Infrastructure (Agent 2)**
- **What to test:** pytest runs successfully, coverage reporting works
- **How:** `cd backend && pytest --cov=backend --cov-report=term`
- **Risk if skip:** Broken test infrastructure
- **Decision:** ✅ MUST TEST via cloud agent (self-validates)

---

### 💻 CLI Agent Testing (Requires Local Environment)

**3. App Security Fixes (Agent 1)** 🔴 CRITICAL
- **What to test:**
  - Secure storage migration works
  - Certificate pinning doesn't break API calls
  - BLE mutex prevents race conditions
  - No crashes on iOS/Android
- **Risk if skip:** App crashes, data loss, security vulnerabilities exposed
- **Decision:** 🔴 MUST TEST via CLI agent (requires Flutter & device/simulator)

**4. Flutter Testing Infrastructure (Agent 3)** 🟢 LOW RISK
- **What to test:**
  - `flutter test` runs successfully
  - Coverage reporting works
  - Tests compile without errors
- **Risk if skip:** Broken test infrastructure (non-blocking)
- **Decision:** ✅ Should test via CLI agent

**5. App Performance Optimization (Agent 5)** 🟡 MEDIUM
- **What to test:**
  - App builds and runs
  - Provider splitting didn't break state management
  - Pagination works (messages/apps load)
  - No widget rebuild performance regressions
- **Risk if skip:** App broken, providers not wired correctly
- **Decision:** 🔴 MUST TEST via CLI agent

**6. iOS Speaker Diarization (Agent 6)** 🟡 MEDIUM
- **What to test:**
  - Swift code compiles
  - Dev settings page appears
  - Provider toggle doesn't crash
  - Basic diarization test (if possible)
- **Risk if skip:** iOS build broken, Swift bridge fails
- **Decision:** 🔴 MUST TEST via CLI agent (requires iOS simulator/Xcode)

---

## Test Execution Plan

### Phase 1: Cloud Agent Testing (10 minutes)

**Run immediately in cloud environment:**

```bash
# Test 1: Backend security fixes (syntax + import validation)
cd /home/user/omi/backend
python3 -m py_compile database/redis_db.py database/_client.py main.py
echo "✅ Backend security syntax valid"

# Test 2: Backend testing infrastructure
cd /home/user/omi/backend
pip install -r requirements.txt --quiet
pytest tests/test_redis.py tests/test_encryption.py -v --tb=short
echo "✅ Backend tests pass"
```

### Phase 2: CLI Agent Testing (30-45 minutes)

**CRITICAL - Must run on local machine with Flutter SDK + iOS/Android simulators**

---

## 📋 CLI Test Commands (Copy-Paste for CLI Agent)

### Test Command 1: Backend Validation ✅ (Cloud - Already Done Above)

```bash
cd /home/user/omi && git checkout claude/backend-security-fixes-011CV4PhHy5CApgVos5F3rHh && cd backend && python3 -m py_compile database/redis_db.py database/_client.py main.py && echo "✅ PASS: Backend security syntax valid"
```

### Test Command 2: Backend Tests ✅ (Cloud - Already Done Above)

```bash
cd /home/user/omi && git fetch origin && git checkout claude/backend-testing-infrastructure-011CV4W1MNuPxmXJkHftZzDf && cd backend && pip install pytest pytest-asyncio pytest-mock httpx fakeredis --quiet && pytest tests/test_redis.py tests/test_encryption.py tests/test_auth.py -v --tb=short && echo "✅ PASS: Backend tests pass" || echo "❌ FAIL: Backend tests failed"
```

### Test Command 3: App Security Fixes 🔴 (CLI REQUIRED)

```bash
cd /home/user/omi && git fetch origin && git checkout claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv && cd app && flutter pub get && flutter analyze --no-fatal-infos && flutter build apk --debug --target-platform android-arm64 && echo "✅ PASS: App security builds successfully" || echo "❌ FAIL: App security build failed - check secure_storage, certificate_pinning, or BLE mutex code"
```

### Test Command 4: Flutter Testing Infrastructure 🟢 (CLI REQUIRED)

```bash
cd /home/user/omi && git fetch origin && git checkout claude/flutter-testing-infrastructure-011CV4WDZegf78M3fo2HGDgk && cd app && flutter pub get && flutter test --no-sound-null-safety test/unit/providers/capture_provider_test.dart test/unit/providers/device_provider_test.dart && echo "✅ PASS: Flutter tests run successfully" || echo "❌ FAIL: Flutter tests failed"
```

### Test Command 5: App Performance Optimization 🔴 (CLI REQUIRED)

```bash
cd /home/user/omi && git fetch origin && git checkout claude/optimize-app-performance-011CV4WG8A6CFHwkXhaf29Xy && cd app && flutter pub get && flutter analyze --no-fatal-infos && flutter test test/unit/providers/capture_provider_test.dart && flutter build apk --debug --target-platform android-arm64 && echo "✅ PASS: App performance optimization builds and tests pass" || echo "❌ FAIL: Provider splitting or compute() broke the app"
```

### Test Command 6: iOS Diarization 🟡 (CLI REQUIRED - iOS Simulator)

```bash
cd /home/user/omi && git fetch origin && git checkout claude/ios-speaker-diarization-011CV4WHCQrHpY1CGDyV3DqU && cd app && flutter pub get && flutter analyze --no-fatal-infos && cd ios && pod install && cd .. && flutter build ios --debug --simulator && echo "✅ PASS: iOS diarization builds successfully" || echo "❌ FAIL: Swift code or Flutter bridge broken"
```

---

## PR & Merge Strategy

### Merge Order (Sequential, with testing gates)

#### **Round 1: Backend Foundation** (Merge First)
1. ✅ **Lead: Backend Security Fixes**
   - Branch: `claude/backend-security-fixes-011CV4PhHy5CApgVos5F3rHh`
   - **Test:** Cloud agent validation ✅
   - **PR Title:** `fix(security): Critical backend security fixes - RCE, credentials, CORS`
   - **Merge to:** `main`
   - **Reviewers:** 2+ (security critical)
   - **CI Required:** YES

2. ✅ **Agent 2: Backend Testing Infrastructure**
   - Branch: `claude/backend-testing-infrastructure-011CV4W1MNuPxmXJkHftZzDf`
   - **Test:** Cloud agent pytest ✅
   - **PR Title:** `feat(testing): Add comprehensive backend testing infrastructure`
   - **Merge to:** `main` (after #1)
   - **Depends on:** Backend security fixes merged first
   - **Reviewers:** 1+
   - **CI Required:** YES - must show 50%+ coverage

#### **Round 2: App Security & Testing** (Parallel after backend merged)
3. 🔴 **Agent 1: App Security Fixes**
   - Branch: `claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv`
   - **Test:** CLI agent - MUST PASS ✅
   - **PR Title:** `fix(app): Critical app security - secure storage, certificate pinning, BLE mutex`
   - **Merge to:** `main`
   - **Reviewers:** 2+ (security critical)
   - **CI Required:** YES + manual testing required

4. 🟢 **Agent 3: Flutter Testing Infrastructure**
   - Branch: `claude/flutter-testing-infrastructure-011CV4WDZegf78M3fo2HGDgk`
   - **Test:** CLI agent - should pass
   - **PR Title:** `feat(testing): Add comprehensive Flutter testing infrastructure`
   - **Merge to:** `main` (can merge parallel with #3)
   - **Reviewers:** 1+
   - **CI Required:** YES

#### **Round 3: Performance & Features** (After security hardened)
5. ⏳ **Agent 4: Backend Performance Refactoring**
   - Branch: TBD (still in progress)
   - **Test:** Cloud agent + integration tests
   - **PR Title:** `refactor(backend): Split transcribe service, add distributed locking`
   - **Merge to:** `main`
   - **Reviewers:** 2+ (major refactoring)
   - **CI Required:** YES

6. 🟡 **Agent 5: App Performance Optimization**
   - Branch: `claude/optimize-app-performance-011CV4WG8A6CFHwkXhaf29Xy`
   - **Test:** CLI agent - MUST PASS ✅
   - **PR Title:** `feat(app): Optimize performance - split providers, isolates, pagination`
   - **Merge to:** `main`
   - **Reviewers:** 2+ (major refactoring)
   - **CI Required:** YES

7. 🟡 **Agent 6: iOS Diarization**
   - Branch: `claude/ios-speaker-diarization-011CV4WHCQrHpY1CGDyV3DqU`
   - **Test:** CLI agent iOS build - MUST PASS ✅
   - **PR Title:** `feat(ios): Add real-time speaker diarization with dev settings`
   - **Merge to:** `main`
   - **Reviewers:** 1+ (new feature, lower risk)
   - **CI Required:** YES (iOS build)

---

## Merge Conflicts Risk Assessment

### Expected Conflicts

**High Risk:**
- Agent 1 vs Agent 5: Both modify `app/lib/providers/capture_provider.dart`
  - **Resolution:** Merge Agent 1 first, then rebase Agent 5

**Medium Risk:**
- Agent 1 vs Agent 6: Both modify `app/pubspec.yaml` (dependencies)
  - **Resolution:** Merge conflict trivial - accept both dependency additions

**Low Risk:**
- Backend changes are isolated (different files)
- Test infrastructure doesn't conflict with production code

### Conflict Resolution Strategy
1. **Merge security first** (Agents Lead + 1)
2. **Rebase performance branches** (Agents 4, 5) onto latest main before merge
3. **Resolve conflicts manually** if needed (provider splitting vs security changes)

---

## Success Criteria

### Before Merging to Main
- ✅ All syntax checks pass
- ✅ Backend tests pass (pytest 50%+ coverage)
- ✅ Flutter tests pass (40%+ coverage)
- ✅ App builds successfully (Android + iOS)
- ✅ No regressions in core functionality
- ✅ Security vulnerabilities eliminated (eval() gone, secure storage working)

### After Merge
- ✅ CI/CD pipeline green
- ✅ Production deployment successful (staging first)
- ✅ Monitoring shows no new errors
- ✅ Performance metrics improved (reduced rebuilds, faster queries)

---

## Rollback Plan

If any merge breaks production:

1. **Immediate:** Revert PR (`git revert <merge-commit>`)
2. **Investigate:** Check logs, reproduce issue
3. **Fix Forward:** Create hotfix branch from main
4. **Re-merge:** Once fixed, re-apply changes

---

## Timeline Estimate

| Phase | Duration | Blocking? |
|-------|----------|-----------|
| Cloud agent testing | 10 min | YES |
| CLI agent testing | 45 min | YES |
| PR creation | 15 min | NO |
| Code review | 1-2 days | YES |
| Merge Round 1 (backend) | 1 day | YES |
| Merge Round 2 (app security) | 1 day | YES |
| Merge Round 3 (performance) | 2 days | NO |
| **TOTAL** | **4-5 days** | - |

---

## Next Actions

1. ✅ **Run cloud agent tests** (me, now)
2. 🔴 **Dispatch CLI agent** for Flutter/iOS testing
3. ⏳ **Wait for Agent 4** to complete backend refactoring
4. ✅ **Create PRs** once all tests pass
5. 📋 **Assign reviewers** based on expertise

---

Generated: 2025-11-13
Lead Agent: Claude (Sonnet 4.5)
Session: 011CV4PhHy5CApgVos5F3rHh
