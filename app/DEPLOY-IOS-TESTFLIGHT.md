# OMI iOS TestFlight Deployment Guide

This document covers how to build and deploy the OMI (Ella) iOS app to TestFlight from any machine with Xcode installed.

---

## Prerequisites

### Required software
- **Xcode** 15+ with valid Apple Developer account
- **Flutter** (for `flutter pub get` and build orchestration)
- **CocoaPods** (`pod install` in the `ios/` directory)
- **`rcodesign`** — custom Apple code signing tool (not `codesign` which fails in this environment)

### Required credentials (stored locally or via env)

| Credential | Location | Notes |
|-----------|----------|-------|
| Apple Distribution cert | `~/.signing/dist_cert.pem` | Base64-encoded cert |
| Distribution private key | `~/.signing/dist_privkey.pem` | Base64-encoded key |
| AppleWWDRCA certificate | `~/Library/MobileDevice/Provisioning Profiles/AppleWWDRCA.cer` | For code signing |
| Provisioning profile | `~/Library/MobileDevice/Provisioning Profiles/AppStore_*.mobileprovision` | App Store Connect profile |
| App Store Connect API key | `J77JD8RJXF` | Key ID |
| App Store Connect issuer | `5ed3a276-d6c0-43eb-ba70-13eed9b35a7e` | Issuer ID |

### Environment variables (optional for CI)

```bash
export CODE_SIGNING_CERT_B64="$(cat ~/.signing/dist_cert.pem | base64)"
export CODE_SIGNING_KEY_B64="$(cat ~/.signing/dist_privkey.pem | base64)"
export RCODESIGN_KEYCHAIN_PASSWORD="your-keychain-password"
```

---

## Repository Setup

### Clone the repo

```bash
git clone https://github.com/ellaaicare/omi.git
cd omi
```

### Checkout the target branch

```bash
# For PR branches:
git fetch origin pull/<PR_NUMBER>/head:<local-branch-name>
git checkout <local-branch-name>

# Example: fetch PR #109
git fetch origin pull/109/head:codex/issue-678-sim-battery
git checkout codex/issue-678-sim-battery

# Or for main:
git checkout main
```

---

## Build from Scratch

### Step 1: Install Flutter dependencies

```bash
cd app
flutter pub get
```

### Step 2: Install CocoaPods

```bash
cd ios
pod install --repo-update  # First time; use --repo-update if pods are stale
cd ..
```

### Step 3: Update version number

Before building, increment the build number in `app/pubspec.yaml`:

```yaml
version: 1.0.522+731   # increment +1 each time
```

The format is `major.minor+build`. Build numbers must be higher than any previously uploaded build.

### Step 4: Clean old artifacts

```bash
rm -f app/build/ios/EllaCare.ipa
rm -rf app/build/ios/EllaCare.xcarchive
```

### Step 5: Build the app (skip upload)

```bash
SKIP_PULL=1 SKIP_UPLOAD=1 ./app/ios/build-and-upload.sh
```

This will:
- Run `flutter pub get` (unless SKIP_PULL=1)
- Run `pod install` (unless SKIP_PULL=1)
- Build the xcarchive via `xcodebuild archive`
- Sign with `rcodesign` using your distribution cert
- Package to `build/ios/EllaCare.ipa`

### Step 6: Upload to TestFlight

```bash
xcrun altool --upload-app \
  -f app/build/ios/EllaCare.ipa \
  -t ios \
  --apiKey J77JD8RJXF \
  --apiIssuer 5ed3a276-d6c0-43eb-ba70-13eed9b35a7e
```

Expected output on success:
```
UPLOAD SUCCEEDED with no errors
Delivery UUID: <uuid>
```

---

## Using the build-and-upload.sh Script

Located at `app/ios/build-and-upload.sh`. Supports these environment flags:

| Flag | Purpose |
|------|---------|
| `SKIP_PULL=1` | Skip `git pull` and `flutter pub get` (use when working from a branch) |
| `SKIP_UPLOAD=1` | Build only, don't upload to TestFlight |
| `SKIP_CLEAN=1` | Don't remove old xcarchive/IPA before build |

### Full build + upload in one command:

```bash
SKIP_PULL=1 ./app/ios/build-and-upload.sh
```

---

## Troubleshooting

### "Bundle version must be higher than previously uploaded"

The build number in `pubspec.yaml` must increment. Find the last uploaded build number and add 1.

```bash
# Check current
grep '^version:' app/pubspec.yaml

# If last build was 729, increment to 730:
sed -i '' 's/version: 1.0.522+729/version: 1.0.522+730/' app/pubspec.yaml
```

### "rcodesign: command not found"

`rcodesign` is at `/usr/local/bin/rcodesign` (installed via homebrew or custom). If missing:

```bash
# Check if it's in the path
which rcodesign || ls /usr/local/bin/rcodesign

# If not found, install or check build script uses full path
```

### codesign errors ("errSecInternalComponent")

Use `rcodesign` not `codesign`. The build script (`build-and-upload.sh`) uses `rcodesign` automatically. If you see `codesign` failures, something is wrong with the signing path.

### CocoaPods fails with "source not found"

```bash
cd app/ios
pod install --repo-update
```

### Flutter analyze fails

```bash
cd app
flutter analyze lib/  # or specific file
```

### SSH credentials needed for git pull on CI

If CI needs to pull from private repos, configure SSH agent:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ella  # or your deploy key
```

---

## CI/CD (GitHub Actions)

See `.github/workflows/` in the repo for existing workflows. Key patterns:

```yaml
# Example: deploy on push to main
- name: Build iOS
  run: |
    cd app
    pod install --repo-update
    SKIP_PULL=1 ./ios/build-and-upload.sh

# Upload via altool with App Store Connect API key
- name: Upload to TestFlight
  run: |
    xcrun altool --upload-app \
      -f app/build/ios/EllaCare.ipa \
      -t ios \
      --apiKey ${{ secrets.APPSTORE_API_KEY_ID }} \
      --apiIssuer ${{ secrets.APPSTORE_API_ISSUER_ID }}
```

---

## Key Files

| File | Purpose |
|------|---------|
| `app/ios/build-and-upload.sh` | Main build orchestrator |
| `app/pubspec.yaml` | Version number (`version: 1.0.XXX+YYY`) |
| `app/ios/Runner/Info.plist` | Background modes, bundle ID |
| `app/ios/Runner.xcodeproj/project.pbxproj` | Xcode project (do not edit directly) |
| `~/.signing/` | Code signing credentials |
| `~/Library/MobileDevice/Provisioning Profiles/` | Provisioning profiles |

---

## Quick Reference: Mac Mini (this machine)

```bash
# Build and upload (current session)
cd /Users/ellaai/dev/omi/app
SKIP_PULL=1 ./ios/build-and-upload.sh

# Check build
ls -la build/ios/EllaCare.ipa

# TestFlight delivery UUID appears in altool output
```

---

## Build Number Tracking

| Build | Date | Branch | Notes |
|-------|------|--------|-------|
| 718 | 2026-04-18 | codex/issue-678-sim-battery | Battery polling reduction PR |
| 729 | 2026-04-19 | main | Phone mic recording feature |
| 730 | 2026-04-19 | codex/issue-678-sim-battery | Battery fix + phone mic combined |

*Update this table with each new build.*
