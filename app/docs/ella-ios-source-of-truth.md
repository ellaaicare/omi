# Ella iOS Source Of Truth

Last verified: 2026-07-20

## Where The App Lives

The current Ella iOS app lives in the Ella fork of OMI:

```bash
https://github.com/ellaaicare/omi.git
```

The Flutter app is under:

```bash
app/
```

Use `main` as the latest shared baseline unless a specific active PR says otherwise. The Mac Mini worktree is only a checkout of this repository, not the source of truth:

```bash
/Users/ellaai/worktrees/omi-ios-agent/app
```

The upstream BasedHardware repository is retained as `upstream` for comparison/cherry-picks only:

```bash
https://github.com/BasedHardware/omi.git
```

Do not use upstream directly for Ella iOS app work unless the task is explicitly an upstream comparison or cherry-pick.

## Current Shared Baseline

As of 2026-07-19, `ellaaicare/omi` `main` includes the recent iOS screenshot/simulator work:

- PR #270: iOS Simulator Soloud linker fix.
- PR #271: Demo Mode curated screenshot fixtures.
- PR #271 feature branch `feature/demo-mode-fixtures` was merged and then deleted remotely. Do not try to pull that branch for current work.

Current verified remote head at the time this document was written:

```bash
origin/main = 3bc091aea9ffa050ad7fb12edac4e2d3be189239
```

Because `main` moves, always fetch before starting work.

## Clone Or Update From Another Machine

Fresh clone:

```bash
git clone https://github.com/ellaaicare/omi.git
cd omi
git remote add upstream https://github.com/BasedHardware/omi.git
git fetch origin --prune
git switch main
git pull --ff-only origin main
cd app
```

Existing clone:

```bash
cd /path/to/omi
git fetch origin --prune
git switch main
git pull --ff-only origin main
cd app
```

Start a new iOS feature branch from the shared baseline:

```bash
cd /path/to/omi
git fetch origin --prune
git switch -c codex/<short-ios-task-name> origin/main
cd app
```

Do not base new iOS work on old stacked branches unless the GitHub issue or PR explicitly names that branch.

## Setup And Validation

From `app/`:

```bash
flutter pub get
./test.sh
```

Simulator smoke build:

```bash
flutter build ios --simulator --flavor prod --debug
```

If you need a specific simulator:

```bash
xcrun simctl list devices available
flutter build ios --simulator --flavor prod --debug -d <SIMULATOR_UDID>
xcrun simctl boot <SIMULATOR_UDID>
xcrun simctl install <SIMULATOR_UDID> "build/ios/iphonesimulator/Ella Care.app"
xcrun simctl launch <SIMULATOR_UDID> com.ellaaicare.ella
```

Focused analyzer example for app-side changes:

```bash
flutter analyze --no-fatal-infos <changed-dart-files>
```

## TestFlight And Signing

TestFlight builds must preserve the Ella app identity:

```text
Bundle ID: com.ellaaicare.ella
Firebase project: omi-dev-ca005
```

Do not change Firebase, auth, bundle ID, signing, provisioning, or App Store Connect configuration just to make a local build pass. If TestFlight is required, use the repo deploy scripts and the Mac Mini App Store Connect environment documented in the agent runbooks.

### Design v2 TestFlight handoff

The current release candidate is pinned by the `testflight/design-v2-792` tag. From any
machine authenticated to the Ella GitHub organization, this one command queues
Sophia's self-hosted Mac Mini runner:

```bash
gh workflow run ios-build.yml --repo ellaaicare/omi --ref testflight/design-v2-792
```

The workflow checks out that immutable tag, runs the Flutter test suite, and
then invokes the existing `app/ios/build-and-upload.sh` TestFlight path. It does
not change signing, bundle ID, Firebase, or App Store Connect configuration.

Build 790 used stale tracked Firebase fallback files and must not be used for
authentication testing. Build 791 restored the production `omi-dev-ca005`
identity. Build 792 retains that identity and prevents the initial Memories
fetch from rendering as an empty account while data is still loading.

## Branch Hygiene

Use this rule of thumb:

- `main`: current shared Ella iOS baseline.
- `codex/<task>` or `feature/<task>`: active work intended for PR.
- Deleted merged branches: historical only; do not use as a baseline.
- Local Mac Mini worktrees: temporary working copies; never assume changes are durable until committed and pushed to GitHub.

Before handing work to another laptop/server, confirm:

```bash
git status --short --branch
git fetch origin --prune
git log --oneline --decorate --max-count=10 origin/main
gh pr status --repo ellaaicare/omi
```

If a needed change exists only locally, push a branch and open a draft PR before handing it off.
