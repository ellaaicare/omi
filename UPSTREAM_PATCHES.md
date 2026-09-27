# Upstream Capture Patches (ella-ai#1280 P1)

This file is the durable registry required by ella-ai#1280 for the device/
capture layer's relationship to upstream `BasedHardware/omi`. It tracks
exactly which paths are claimed as byte-identical to a pinned upstream
commit, records any unavoidable exception, and states what is deliberately
**not** claimed yet.

## Pin

- **Upstream repository**: `https://github.com/BasedHardware/omi`
- **Pinned commit**: `f16699aea7fe9ba089baceb628922f2882c51153`
- **Manifest**: `UPSTREAM_CAPTURE_MANIFEST.json` (machine-readable copy of the
  repo/SHA/paths below, consumed by the guard script)
- **Guard script**: `scripts/verify_upstream_capture_identity.py` — re-fetches
  the pinned commit from the upstream repository and diffs every manifested
  path byte for byte. Run it with:
  ```bash
  python3 scripts/verify_upstream_capture_identity.py
  ```
  It exits non-zero on any drift, missing local file, or missing upstream
  file.

## Upstream-owned paths claimed in this PR: none

**This PR vendors zero files byte-identical to upstream.** `UPSTREAM_CAPTURE_MANIFEST.json`'s
`paths` list is intentionally empty, so the guard script currently guards
nothing (it still runs and passes trivially — that is an honest "0 of 0",
not a placeholder result).

### Why: every candidate this PR investigated had already drifted

Before writing this off, this PR checked every plausible "safe, self-contained,
leaf" vendoring candidate in `app/lib/services/devices/`, including files that
already exist in the fork at upstream's exact modern path (evidence the fork
previously made a partial attempt at this same migration):

| Path | Exists in fork? | Byte-identical to pin? |
|---|---|---|
| `app/lib/services/devices/transports/device_transport.dart` | Yes | **No** — the fork's copy has an additional required method, `getReadyCharacteristicStream`, that three call sites (`device_connection.dart`, `omi_connection.dart`, `omiglass_connection.dart`) depend on and upstream's copy does not define. |
| `app/lib/services/devices/models.dart` | Yes | No — the fork keeps `flutter_blue_plus`-based helpers (`getBleServices`, `getServiceByUuid`, `getCharacteristicByUuid`) that upstream removed after switching to its own native transport abstraction; deleting them to match upstream would break every existing `*_connection.dart` file. |
| `app/lib/services/devices/discovery/{apple_watch_discoverer,device_discoverer,device_locator}.dart` | Yes | No — diverged (renamed Pigeon output file `flutter_communicator.g.dart` vs. upstream's `pigeon_communicator.g.dart`, plus real logic differences, e.g. 75 diff lines in `device_locator.dart`). |
| `app/lib/services/devices/transports/watch_transport.dart` | Yes | No — same Pigeon-file rename as above. |
| `app/lib/services/devices/connectors/*.dart`, `discovery/native_bluetooth_discoverer.dart`, `discovery/rayban_meta_discoverer.dart`, `ring_protocol.dart`, `transports/{native_ble_transport,rayban_meta_transport}.dart` | No | N/A — upstream added these as part of a device/discovery/transport architecture reorganization the fork has not adopted; `ring_protocol.dart` and `bluetooth_readiness.dart` additionally import other upstream-only modules (`connectors/device_connection.dart`, `services/bridges/ble_bridge.dart`, `gen/pigeon_communicator.g.dart`), so vendoring them standalone would add dormant, unreachable code rather than something wired into the compile/runtime graph. |

**A near-miss worth recording:** this PR's first draft *did* copy
`transports/device_transport.dart` verbatim from the pin, believing (based on
an earlier upstream-only path listing) that the file did not exist in the
fork. It does exist, and overwriting it broke three call sites at analyze/test
time (`getReadyCharacteristicStream` is not defined for the upstream shape of
`DeviceTransport`). That local copy was reverted before commit; it never
reached a pushed commit. The lesson generalizes: nothing in this device/
capture layer is a safe drop-in replacement without a compile+test check per
file, which is exactly what this PR's guard script and CI wiring exist to
provide once a real vendoring attempt is made.

### The underlying gap (why full item-1 "AS-IS" parity is a separate phase)

- **Dart**: upstream reorganized `app/lib/services/devices/*.dart` into
  `connectors/`, `discovery/`, and `transports/` subpackages. The fork has
  partially and inconsistently adopted this shape (see table above) but none
  of the adopted files match upstream byte-for-byte, and the fork's main
  connection files (`app/lib/services/devices/*_connection.dart`,
  `app/lib/providers/capture_provider.dart`,
  `app/lib/services/sockets/transcription_service.dart`) still use the older
  flat shape with Ella's UID/consent/account-isolation logic
  (`WalOwnerAuthority`, `AiConsentActiveSessionLease`, etc.) written directly
  into `capture_provider.dart` and `transcription_service.dart`, not confined
  to the edges.
- **Swift/native**: upstream has an `app/ios/Runner/Ble/` module
  (`OmiBleManager.swift` and friends) and an `app/ios/Runner/PhoneMic/` module
  (background-capable phone-mic capture with Opus encoding) that do not exist
  in this fork at all. This fork's BLE handling stays in Dart via
  `flutter_blue_plus`, and its only native audio-adjacent Swift code
  (`RecorderHostApiImpl.swift`) is Apple Watch-only; there is no background
  phone-mic capture path today.

Bringing either side to real upstream parity is a genuine, multi-file
migration that needs device/simulator verification this cloud environment
cannot provide, and — per the fork's fetch-only-upstream repository
boundary — cannot route through an upstream PR for interim exceptions
either. It is tracked as a follow-up phase of ella-ai#1280, not represented
as done here.

## What this PR does add (independent of the vendoring scope above)

These apply to the *existing* fork capture orchestration in
`capture_provider.dart` and `transcription_service.dart` — ordinary edits to
files this PR does not claim as upstream-owned, not "exceptions":

- A fail-closed `mayEmitAudio()` check
  (`app/lib/ella/services/ella_audio_emission_gate.dart`) at every
  native-to-socket audio emission boundary for both necklace
  (`_sendDeviceFrame`) and phone (`onByteReceived` in `_streamRecording`),
  plus the transcription socket's own send authority
  (`TranscriptSegmentSocketService._hasProtectedSendAuthority`).
- An explicit nonempty-bound-UID check
  (`app/lib/ella/services/ella_capture_uid_gate.dart`) on every capture start
  and resume path: `_streamDeviceRecording` (necklace), `_streamRecording`
  (phone), and `_streamSystemAudioRecording` (desktop) — and therefore
  `resumeDeviceRecording` / `resumeSystemAudioRecording`, which delegate to
  the same functions.
- One coherent activation flag, `ELLA_UPSTREAM_CAPTURE_ENABLED`, default
  OFF, read from the same environment variable on both sides in
  `app/ios/build-and-upload.sh`:
  - Dart: `--dart-define=ELLA_UPSTREAM_CAPTURE_ENABLED=true` →
    `app/lib/ella/services/ella_upstream_capture_flag.dart`.
  - Swift: `SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) ELLA_UPSTREAM_CAPTURE_ENABLED`
    passed to `xcodebuild archive` → `app/ios/Runner/EllaUpstreamCaptureFlag.swift`
    (registered on the Runner target's Sources build phase in
    `project.pbxproj`, so it is on the real compile graph, not dormant —
    though its Swift/Xcode compilation itself is not verified in this cloud
    environment; see the PR description).
  - As noted above, this flag does not yet switch any real capture
    implementation — there is nothing vendored for it to switch to yet. It
    exists so the activation mechanism (and its coherence across Dart and
    Swift) is built, tested, and ready before the first real vendoring lands.

No new Ella-only auto-connect, polling, reconnect-on-schedule, or lifecycle
fencing is added by any of the above — all of it is passive gating (deny by
default) around existing, already-manual connection actions.

## Also fixed in this PR: pre-existing, unrelated CI drift

`.github/workflows/ella-ios-source-ci.yml`'s "Enforce focused test collection
counts" step hardcoded `expected_issue_test_count=49` and
`expected_app_test_count=79`. Both were already stale on this PR's base
branch/SHA before any change here (actual counts were 61 and 98
respectively) — unrelated to ella-ai#1280. Left uncorrected, this gate would
fail on this PR (and any other PR) for a reason predating it. Updated both
to the current true counts (62 and 99, i.e. base-branch count plus this PR's
one new test file, `app/test/ella/services/upstream_capture_p1_test.dart`).
