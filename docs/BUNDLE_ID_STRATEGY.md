# Bundle Identifier Strategy for Omi Project

**Date:** 2025-11-13
**Issue:** Confusion between two different bundle identifiers in use

---

## Two Valid Bundle ID Configurations

### Configuration 1: Production/Official (Paid Apple Developer Account)
**Bundle IDs:**
- Production: `com.friend-app-with-wearable.ios12`
- Development: `com.friend-app-with-wearable.ios12.development`

**Used By:**
- `origin/main` branch
- Official Omi app distribution
- Team: H6S4582TRM
- Requires: Paid Apple Developer Program membership ($99/year)
- Supports: All iOS capabilities including push notifications, HealthKit, etc.

---

### Configuration 2: Personal/Free Account (Free Apple Developer Account)
**Bundle ID:**
- All flavors: `com.greg.friendapp`

**Used By:**
- `origin/feature/ios-backend-integration` branch
- User's personal development fork
- Team: Personal Apple ID (free)
- Requires: Free Apple Developer account
- Limitations: No push notifications, restricted capabilities

**Git Commit:** `9972047` - "fix: update bundle ID to com.greg.friendapp and remove paid capabilities for free Apple Developer account"

---

## Current Cloud Claude Test Branch Issue

### Problem
Cloud Claude's test branches were created from `origin/main`, so they inherited:
- Bundle ID: `com.friend-app-with-wearable.ios12*`
- Team: H6S4582TRM

But the user (CLI agent) is testing locally with:
- Firebase config expecting: `com.greg.friendapp`
- Personal Apple Developer account (free)

This causes:
1. ❌ Bundle ID mismatch with Firebase
2. ❌ Cannot build with team H6S4582TRM (user not a member)
3. ⚠️ User must manually change bundle ID for local testing

### Cloud Claude Test Branches Affected
- `claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv` (TEST 3)
- `claude/optimize-app-performance-011CV4WG8A6CFHwkXhaf29Xy` (TEST 5)
- `claude/ios-speaker-diarization-011CV4WHCQrHpY1CGDyV3DqU` (TEST 6)

All use: `com.friend-app-with-wearable.ios12*`

---

## Recommended Bundle ID Going Forward

### For User's Personal Development
**Use:** `com.greg.friendapp`

**Reasoning:**
1. Works with free Apple Developer account
2. Matches user's Firebase configuration
3. Already established in `feature/ios-backend-integration` branch
4. Removes paid capability requirements

**Actions Required:**
- ✅ No action - user already using this locally
- ✅ Firebase configured for `com.greg.friendapp`

---

### For Cloud Claude's Test Branches

**Option A: Keep Production Bundle ID** (Current State)
- Bundle ID: `com.friend-app-with-wearable.ios12*`
- **Pros:** Matches main branch, tests production configuration
- **Cons:** User cannot test locally without manual bundle ID changes

**Option B: Change to Personal Bundle ID** (Recommended)
- Bundle ID: `com.greg.friendapp`
- **Pros:** User can test directly, matches their Firebase config
- **Cons:** Deviates from main branch, requires bundle ID change commits

**Recommendation:** Use **Option B** for test branches intended for user testing

If Cloud Claude's branches are meant to be merged back to main eventually, they should:
1. Be tested with production bundle ID on CI/CD
2. Allow temporary local bundle ID changes for user testing
3. Document that local testing requires bundle ID swap

---

## Implementation Guide

### For Cloud Claude: Update Test Branches to Use Personal Bundle ID

Run on each of the 3 test branches:

```bash
#!/bin/bash
BRANCHES=(
  "claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv"
  "claude/optimize-app-performance-011CV4WG8A6CFHwkXhaf29Xy"
  "claude/ios-speaker-diarization-011CV4WHCQrHpY1CGDyV3DqU"
)

for BRANCH in "${BRANCHES[@]}"; do
  echo "=== Updating $BRANCH to use personal bundle ID ==="
  git checkout $BRANCH
  git pull origin $BRANCH

  # Change all bundle ID references to com.greg.friendapp
  sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = "com.friend-app-with-wearable.ios12"/PRODUCT_BUNDLE_IDENTIFIER = com.greg.friendapp/g' app/ios/Runner.xcodeproj/project.pbxproj
  sed -i '' 's/PRODUCT_BUNDLE_IDENTIFIER = "com.friend-app-with-wearable.ios12.development"/PRODUCT_BUNDLE_IDENTIFIER = com.greg.friendapp/g' app/ios/Runner.xcodeproj/project.pbxproj

  # Verify changes
  grep "PRODUCT_BUNDLE_IDENTIFIER" app/ios/Runner.xcodeproj/project.pbxproj | head -5

  git add app/ios/Runner.xcodeproj/project.pbxproj
  git commit -m "fix(ios): update bundle ID to com.greg.friendapp for local testing with free Apple Developer account"
  git push -u origin $BRANCH

  echo "✅ Done with $BRANCH"
done
```

**Result:** All test branches will use `com.greg.friendapp`, matching user's local Firebase config.

---

### For User: Current Local Development (Already Working)

No changes needed! User is already using `com.greg.friendapp` locally.

**Current Setup:**
- Bundle ID: `com.greg.friendapp` (all flavors)
- Firebase: Configured for `com.greg.friendapp`
- Apple Account: Free personal account
- Status: ✅ Working

---

## Firebase Configuration Requirements

### For Production Bundle ID
**Firebase Project:** (Production)
- Bundle ID: `com.friend-app-with-wearable.ios12`
- GoogleService-Info.plist with matching bundle ID

### For Personal Bundle ID
**Firebase Project:** (User's Personal)
- Bundle ID: `com.greg.friendapp`
- GoogleService-Info.plist with matching bundle ID
- **Location:** Currently in user's `app/ios/Runner/GoogleService-Info.plist`

**Important:** These are DIFFERENT Firebase projects. Cannot mix bundle IDs and Firebase configs.

---

## Summary of Actions

### Immediate Actions for Cloud Claude

1. ✅ **Acknowledge** that Cloud Claude test branches currently use production bundle ID
2. 🔧 **Decide** whether to update test branches to use `com.greg.friendapp`
3. 📝 **Document** which bundle ID each branch should use
4. 🔄 **Update** test branches if Option B is chosen

### Immediate Actions for User (CLI Agent)

1. ✅ **Continue** using `com.greg.friendapp` for local development
2. ✅ **Keep** local bundle ID changes uncommitted (as they are now)
3. ✅ **Do NOT** push bundle ID changes to Cloud Claude's branches
4. ⏳ **Wait** for Cloud Claude to decide on bundle ID strategy

### Going Forward

**Rule:** Always use `com.greg.friendapp` for:
- User's local development
- Any branches meant for user testing
- Commits to `feature/ios-backend-integration` branch

**Rule:** Use `com.friend-app-with-wearable.ios12*` only for:
- Official production releases
- Merges to `main` branch
- CI/CD testing with paid Apple Developer account

---

## Related Files

- **Xcode Project:** `app/ios/Runner.xcodeproj/project.pbxproj`
- **Firebase Config:** `app/ios/Runner/GoogleService-Info.plist`
- **Entitlements:** `app/ios/Runner/*.entitlements` (different for each bundle ID)
- **Info.plist:** `app/ios/Runner/Info.plist` (bundle ID referenced)

---

## Commit References

- **Free Account Setup:** `9972047` - "fix: update bundle ID to com.greg.friendapp and remove paid capabilities"
- **Location:** `origin/feature/ios-backend-integration` branch

---

**Conclusion:** Use `com.greg.friendapp` as the proper bundle identifier for all personal development and testing going forward. Cloud Claude should update test branches to match this configuration.
