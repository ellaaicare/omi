# Upstream capture stack (BasedHardware/omi@f16699a) — ellaaicare/ella-ai#1280

The necklace + phone-mic capture layer is **upstream code as-is**, pinned at
`f16699aea7fe9ba089baceb628922f2882c51153`, namespaced so it coexists with the legacy
(fork) capture path. Ella code only composes it and gates audio through upstream's own seams.

## What is vendored (see `UPSTREAM_OWNED.txt` for the exact map + upstream blob ids)

| Kind | Where | Count |
| --- | --- | --- |
| `dart-relocated` | `app/lib/<rel>` → `app/lib/upstream_capture/<rel>` (capture, devices, mic, sockets, wals, audio_sources, bridges, providers, Pigeon Dart bindings, and the upstream schema/http/prefs/auth/env files they need) | 165 |
| `dart-relocated` | upstream capture tests + replay fixtures: `app/test/<rel>` → `app/test/upstream_capture/<rel>` | 9 |
| `verbatim` | native hosts at their upstream paths: `app/ios/Runner/{Ble,PhoneMic,Batch,Limitless}/**`, `PigeonCommunicator.g.swift`, `SyncTransferBackgroundLease.swift` (27) + the 3 upstream capture docs | 30 |
| `in-place` | `app/lib/models/stt_response_schema.dart` (pin bytes at the fork path; strict superset) | 1 |

The only permitted difference is the mechanical Dart import relocation
`'package:omi/<rel>'` → `'package:omi/upstream_capture/<rel>'`, applied only when `<rel>` is
itself vendored. Nothing else (no reformatting — `scripts/pre-commit` and CI skip these files).

* Check: `python3 scripts/verify_upstream_capture_identity.py [--require-pin]` (offline: recorded
  upstream blob ids; online, after `git fetch https://github.com/BasedHardware/omi.git <pin>`:
  also a byte diff against the real pin).
* Re-vendor: `python3 scripts/vendor_upstream_capture.py --pin <pin> --list scripts/upstream_capture_files.txt`.
* Patches to upstream-owned files: none — see `UPSTREAM_PATCHES.md`.

## One activation setting

`app/ios/Flutter/EllaUpstreamCapture.xcconfig`: `ELLA_UPSTREAM_CAPTURE_ENABLED = NO` (default).
Flip that one value; everything else is derived from it:

* **Swift sources** — `EXCLUDED_SOURCE_FILE_NAMES` excludes the 27 vendored native files when
  `NO`; when `YES` it excludes the fork's `FlutterCommunicator.g.swift` instead (its Watch Pigeon
  types collide with upstream's `PigeonCommunicator.g.swift`, which declares the same Swift API).
  All vendored files are real Runner Compile Sources members in `project.pbxproj`.
* **Swift `#if`** — `SWIFT_ACTIVE_COMPILATION_CONDITIONS += ELLA_UPSTREAM_CAPTURE_ENABLED_YES|NO`;
  `AppDelegate.swift` calls `EllaUpstreamCaptureNativeHost.shared.register(...)` (BLE + PhoneMic
  Pigeon hosts, `com.omi/capture_policy`, `com.friend.ios/sync_transfer`) only under `_YES`.
* **Dart** — `app/ios/scripts/ella_upstream_capture_build_config.sh` reads the same value and emits
  the `--dart-define-from-file` JSON (`ELLA_UPSTREAM_CAPTURE_ENABLED: true|false`) plus the Flutter
  entry point (`lib/main_upstream_capture.dart` when `YES`, `lib/main.dart` when `NO`).
  `ios/build-and-upload.sh` consumes both. For a manual build:

  ```bash
  cd app
  eval "$(bash ios/scripts/ella_upstream_capture_build_config.sh)"
  flutter build ios --flavor prod -t "$ELLA_UPSTREAM_CAPTURE_FLUTTER_TARGET" \
    --dart-define-from-file="$ELLA_UPSTREAM_CAPTURE_DART_DEFINE_FILE"   # or: --config-only, then Xcode
  ```

`lib/main.dart` never imports `lib/upstream_capture/**` or `lib/ella/upstream_capture/**`
(proven by `upstream_capture_graph_wiring_test.dart`). `lib/main_upstream_capture.dart` refuses to
run unless the Dart define is true, installs the upstream home dock through
`lib/ella/capture_host/ella_capture_host.dart` (import-free registration point), and then runs the
normal app; while installed, the legacy necklace scan/connect and legacy device/WAL service start
are suppressed so exactly one stack owns the microphone, the pendant, and the WAL directory.

## Ella adapter code (outside the vendored trees)

* `lib/ella/services/ella_audio_emission_gate.dart` — `mayEmitAudio(boundUid, lease, expectedGeneration)`.
* `lib/ella/services/ai_consent_active_session_lease.dart` — existing lease, now with a
  process-wide monotonic `generation` minted on every `start()` and `stop()`.
* `lib/ella/upstream_capture/ella_capture_authority.dart` — account binding + fresh lease per
  bind; `admitsFrame()` = `mayEmitAudio` + authenticated-uid check; first refusal revokes.
* `lib/ella/upstream_capture/ella_gated_capture_seams.dart`, `ella_gated_device_connection.dart` —
  per-frame decorators over upstream's `IMicRecorderService`, `DeviceConnection` and
  `TranscriptSegmentSocketService` seams.
* `lib/ella/upstream_capture/ella_upstream_capture_runtime.dart` — composition root (boots the
  vendored stack like upstream `main.dart`, builds upstream `CaptureProvider` through its
  constructor seams, configures upstream `RecordingTransferCoordinator`, and on revocation mutes
  upstream's `CapturePolicy` latch and stops capture through upstream's public API).
* `lib/ella/upstream_capture/ella_upstream_capture_dock.dart` — flag-ON home dock.
* `ios/Runner/EllaUpstreamCaptureNativeHost.swift` — flag-ON native registration.

## Tests

`app/test/upstream_capture/**` (upstream's own tests on the vendored stack) and
`app/test/ella/upstream_capture/*_test.dart` (identity guard, OFF/ON wiring, source isolation,
account switch, per-frame consent, WAL finalization, restart after finish). Both run from
`app/test.sh`.

## Must be verified locally in Xcode (not possible in the Linux CI/agent environment)

1. `pod install` (the `flutter_contacts` 2.1.0 / `flutter_timezone` 5.1.0 bumps change pods) and
   commit the refreshed `ios/Podfile.lock`.
2. Build **OFF** (default) for device and simulator: the vendored native files must be excluded,
   `FlutterCommunicator.g.swift` compiled, and the bridging-header shim import must resolve.
3. Flip `ELLA_UPSTREAM_CAPTURE_ENABLED = YES`, run the build-config script, build **ON** for
   device and simulator: vendored Swift + `PhoneMicOpusShim.m` (`@import OpusKit` from
   `opus_flutter_ios`) compile, `FlutterCommunicator.g.swift` is excluded with no duplicate
   Pigeon symbols, `EllaUpstreamCaptureNativeHost` registration resolves.
4. On a device with the ON build: sign in, grant AI consent, record with the phone (frames reach
   `/v4/listen`), manually connect the Omi necklace (discover → connect → device recording),
   switch between them, finish a conversation and start again, then revoke consent / sign out
   mid-capture and confirm capture stops immediately.
5. Confirm the legacy Watch channel behavior you need: with the flag ON the Watch Pigeon host is
   upstream's (`dev.flutter.pigeon.omi_pigeon.*` channel names), not the fork's
   (`dev.flutter.pigeon.watch.*`).
