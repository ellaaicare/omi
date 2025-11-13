# CLI Agent: Code Fixes Required for TEST 3

## Status
✅ **Dependency fixed** (http_certificate_pinning: ^2.1.2)
⚠️ **3 code errors found** - fixes ready below
📦 **Commit 5dcaa63** - created but unpushed (cloud environment restriction)

---

## 3 Code Errors to Fix

### Error 1: certificate_pinning.dart:178 (non_bool_negation_expression)

**File:** `app/lib/backend/http/certificate_pinning.dart`
**Lines:** 178-181

**Current Code:**
```dart
if (!isValid) {
  Logger.error('Certificate validation failed for $host:$port');
}
return isValid as bool;
```

**Fixed Code:**
```dart
if (isValid == false) {
  Logger.error('Certificate validation failed for $host:$port');
}
return isValid == true;
```

---

### Error 2 & 3: secure_storage.dart:39,43 (undefined_named_parameter)

**File:** `app/lib/backend/secure_storage.dart`
**Lines:** 38-40

**Current Code:**
```dart
const linuxOptions = LinuxOptions(
  resetOnError: true,
);

const windowsOptions = WindowsOptions(
  resetOnError: true,
);
```

**Fixed Code:**
```dart
const linuxOptions = LinuxOptions();

const windowsOptions = WindowsOptions();
```

**Reason:** `resetOnError` parameter doesn't exist in `flutter_secure_storage ^9.2.2`

---

## Quick Fix Commands for CLI Agent

### Option 1: Manual Edit (Recommended - 2 minutes)

```bash
cd /Users/greg/repos/omi
git checkout claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv
git pull origin claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv

# Edit 2 files with the fixes above
# Then:

git add app/lib/backend/http/certificate_pinning.dart app/lib/backend/secure_storage.dart
git commit -m "fix(security): resolve Flutter analyzer errors

- Fix non_bool_negation_expression in certificate_pinning.dart
- Remove undefined resetOnError parameter in secure_storage.dart"
git push origin claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv

# Verify fixes
cd app
flutter analyze --no-fatal-infos
```

### Option 2: Automated Sed (Alternative)

```bash
cd /Users/greg/repos/omi
git checkout claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv
git pull origin claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv

# Fix certificate_pinning.dart
sed -i '' '178s/if (!isValid) {/if (isValid == false) {/' app/lib/backend/http/certificate_pinning.dart
sed -i '' '181s/return isValid as bool;/return isValid == true;/' app/lib/backend/http/certificate_pinning.dart

# Fix secure_storage.dart (remove resetOnError lines)
sed -i '' '38,40d' app/lib/backend/secure_storage.dart
sed -i '' '38i\
      const linuxOptions = LinuxOptions();
' app/lib/backend/secure_storage.dart

sed -i '' '40,42d' app/lib/backend/secure_storage.dart
sed -i '' '40i\
      const windowsOptions = WindowsOptions();
' app/lib/backend/secure_storage.dart

# Commit and push
git add app/lib/backend/http/certificate_pinning.dart app/lib/backend/secure_storage.dart
git commit -m "fix(security): resolve Flutter analyzer errors"
git push origin claude/app-security-fixes-011CV4VzPeotnLUJVsFfkdUv
```

---

## After Fixes Applied

### Re-run Tests
```bash
bash /tmp/run_cloud_tests.sh
```

### Expected Results
```
✅ TEST 3 PASS: App security builds successfully
✅ TEST 5 PASS: App performance builds and tests pass
✅ TEST 6 PASS: iOS diarization builds successfully

=== ALL TESTS COMPLETE ===
```

---

## Why These Fixes?

1. **non_bool_negation_expression:** Flutter analyzer requires explicit boolean comparisons when the type might be nullable or dynamic
2. **undefined_named_parameter:** The `resetOnError` parameter was added in a newer version of `flutter_secure_storage` but we're using `^9.2.2` which doesn't have it

---

## Verification

After applying fixes, run:
```bash
cd /Users/greg/repos/omi/app
flutter analyze lib/backend/http/certificate_pinning.dart lib/backend/secure_storage.dart
```

Should show **0 errors, 0 warnings**

---

**Generated:** 2025-11-13
**Cloud Commit:** 5dcaa63 (unpushed due to HTTP 403)
**Ready for:** CLI agent to apply and push
