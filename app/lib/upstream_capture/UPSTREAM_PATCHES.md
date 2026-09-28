# Upstream patches — BasedHardware/omi@f16699aea7fe9ba089baceb628922f2882c51153

Tracks ellaaicare/ella-ai#1280.

## Behavior patches to upstream-owned files

**One**, tracking ellaaicare/ella-ai#1280 RUN-009:

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/lib/services/devices/discovery/native_bluetooth_discoverer.dart` | `patched` | `0a7aec27f031d61972599823158d8f77731dc2b4` | `e4b26c6f0eccccdcef0f6cf8a449baad6f4c187d` |

**Two**, tracking ellaaicare/ella-ai#1280 RUN-010 / #1287 (layers on top of **One** above — see
below):

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/lib/services/devices/discovery/native_bluetooth_discoverer.dart` | `patched` | `0a7aec27f031d61972599823158d8f77731dc2b4` | `e4b26c6f0eccccdcef0f6cf8a449baad6f4c187d` |
| `app/ios/Runner/Ble/OmiBleDiscoveryNaming.swift` | `patched` | `d078da9cb337a4a2a176861f91951e469bc3efcb` | `82da0ef8a59db3e5917a31bb61f15957a3cbbb87` |
| `app/ios/Runner/Ble/OmiBleManager.swift` | `patched` | `889d135a5a3fe1cbfccbb5baf88d980003df5c77` | `8b819ba660b115440bae942fe63e70695b85666a` |
| `app/ios/Runner/PigeonCommunicator.g.swift` | `patched` | `b774502d0c755cecdab9efefbb7db7d7606c287a` | `f68687905bdfd76fe884e8a3e1c33488a6c62c7e` |
| `app/lib/gen/pigeon_communicator.g.dart` | `patched` | `25034c9152ceac9b4a4cc9a264027697b372a539` | `a49cdc1a0503909b5caa801fac96ed9ffde7b8a2` |

(`native_bluetooth_discoverer.dart`'s row is the same file in both tables — patch **Two**'s diff
for it is incremental on top of patch **One**, not a second independent patch to the same file;
the manifest carries one row per file with the final approved blob.)

Every other file listed in `UPSTREAM_OWNED.txt` is byte-identical to the pin except for the
mechanical Dart import relocation
`'package:omi/<rel>'` → `'package:omi/upstream_capture/<rel>'` (only for `<rel>` that are
themselves vendored). Swift/ObjC/Markdown files and the two `in-place` Dart files are
byte-identical with no rewrite at all, unless listed as `patched` in a table above.
`scripts/verify_upstream_capture_identity.py` (and
`app/test/ella/upstream_capture/upstream_capture_byte_identity_test.dart`) fail on any other
difference. A `patched` entry is relocated and placed exactly like it would be if unpatched
(`dart-relocated` placement for a vendored `.dart` file, `verbatim` placement for a native/doc
file at its upstream path), so the import graph still resolves. It records both the upstream pin
blob and the exact approved local git blob. Both identity guards fail if the upstream base moves
or if the local patch changes without an explicit manifest update. `scripts/verify_upstream_capture_identity.py`
was extended by patch **Two** to recognize `patched` for non-Dart (Swift) files — until then the
guard only structurally supported `patched` on vendored `.dart` files, which patch **One** was.

### `native_bluetooth_discoverer.dart`: native BLE discovery admission drops production necklaces

**Symptom** (ellaaicare/ella-ai#1280 RUN-009): with the upstream-capture flag ON, the app
lists no necklace during BLE discovery, reproduced on two phones and two necklace
generations. With the flag OFF (the legacy `flutter_blue_plus` path), discovery works.

**Root cause**: `OmiBleManager.didDiscover` (iOS, unpatched, forwards the scan-time name and
the advertised service UUIDs as-is) is fine. The bug is in this file's admission classifier.
`_isOmi` only ever checked the advertised service UUID:

```dart
static bool _isOmi(BlePeripheral p) {
  return _hasService(p, omiServiceUuid);
}
```

Production necklaces advertise as the bare local name `Friend` (the product's pre-rebrand
name) or `Omi`, with **no service UUID in the advertisement packet** — so every production
necklace is silently dropped before a GATT connection is ever attempted. `_isFriendPendant`
already had a case-insensitive name fallback (`friend_` prefix) for the *separate* Friend
Pendant product; `_isOmi` had no equivalent name fallback at all.

**Confirmed identical upstream**: diffed against `BasedHardware/omi` at
`a74e4cfca376a7c8212687a23d9354e7e755671d` (`main`, checked live for this patch) — the file's
blob (`0a7aec27f031d61972599823158d8f77731dc2b4`) is byte-for-byte the same as at this fork's
pin (`f16699aea7fe9ba089baceb628922f2882c51153`); upstream has not touched this file since the
pin, and carries the identical bug. Same for every other file this port's discovery path
touches (`OmiBleManager.swift`, `OmiBleDiscoveryNaming.swift`, `bt_device.dart`,
`ble_bridge.dart`, `device_discoverer.dart`, `models.dart`) — all identical to `main` modulo
the mechanical import relocation. So this is patched here, in upstream style, rather than
re-vendored from a fixed upstream (there isn't one yet).

**Upstream base SHA**: `a74e4cfca376a7c8212687a23d9354e7e755671d`.

Upstream PR: pending Greg's approval to file on BasedHardware/omi.

**Upstreamable patch artifact**:
`patches/upstream_capture/0001-native-discovery-admission.patch`. It applies to the upstream
path at the base SHA without the fork's import relocation.

**Fix** — the smallest change that admits the missing names while leaving every other
signature (and the GATT service check at connect time, which stays authoritative) untouched:

```dart
static bool _isOmi(BlePeripheral p) {
  final name = p.name.toLowerCase();
  return name == 'friend' || name.startsWith('omi') || _hasService(p, omiServiceUuid);
}
```

`_isFriendPendant`'s existing `friend_`-prefix check is unchanged and still wins for that
distinct product (checked earlier in the `_isSupportedPeripheral` / `peripheralToDevice`
chains), so a `friend_`-prefixed name is never reclassified as `DeviceType.omi`.

Also added: redacted, count-only discovery telemetry behind `Logger.debug` (seen / admitted /
rejected-by-reason — `no_name`, `no_signature_match`), with no names or UUIDs logged.

The same patch also hardens the Pigeon host boundary: `startScan()` must succeed before the timeout is
armed, `stopScan()` is awaited before callbacks are restored, and host/channel failures return a
safe empty result while restoring the previous callback. Constructor injection is limited to the
existing `BleHostApi` and `BluetoothReadiness` types so delayed and failing host calls are covered
without changing production defaults.

**Upstreamability**: this is a minimal, self-contained diff to
`native_bluetooth_discoverer.dart` with no fork-specific dependencies. Local regressions live in
`app/test/ella/upstream_capture/upstream_capture_discovery_admission_test.dart`; the patch artifact
contains no private identifiers or environment assignments.

The consent/account gate (review item 4) is wired entirely through upstream's existing
constructor seams from Ella adapter code outside the vendored trees:

| Audio boundary | Upstream seam used | Ella adapter |
| --- | --- | --- |
| Phone mic (native → Dart frames) | `CaptureController(phoneMicRecorder: IMicRecorderService)` | `EllaGatedMicRecorderService` |
| Necklace (BLE audio packets) | `CaptureController(deviceConnectionLoader: ...)` → `DeviceConnection.getBleAudioBytesListener` | `EllaGatedDeviceConnection` |
| Transcription socket (Dart → network) | `CaptureController(openSocket: CaptureConversationSocketOpen)` | `EllaGatedTranscriptSocket` / `ellaGatedConversationSocketOpen` |
| WAL upload (disk → network) | `RecordingTransferCoordinator.configure(autoUploadEnabled: ...)` | `EllaUpstreamCaptureRuntime.configureTransferCoordinator` |
| Native batch writers (no Dart frames) | upstream `CapturePolicy` latch (`SharedPreferencesUtil.setCaptureMuted` → `com.omi/capture_policy` → `CaptureAdmissionPolicy`) | `EllaUpstreamCaptureRuntime._stopUpstreamCapture` |

### Two: native discovery still finds no necklace on build 872 (RUN-010) — diagnostics, not a naming-fallback bug

**Symptom** (ellaaicare/ella-ai#1280 RUN-010, ellaaicare/ella-ai#1287): build 872 — which already
carries the upstream-capture flag ON and patch **One** above — still lists no necklace during BLE
discovery on the same phone/necklace that build 869 (the old `flutter_blue_plus` path) finds
within seconds.

**Hypothesis given for this investigation, and what was actually found**: the task hypothesized
that `OmiBleManager.didDiscover` forwards only the advertisement's local name
(`CBAdvertisementDataLocalNameKey`) and the advertised service UUIDs, and that the Dart classifier
never sees a fallback to `peripheral.name` (the OS's cached GAP name). **That hypothesis does not
match this code.** `didDiscover` already calls `OmiBleDiscoveryNaming.discoveredName`, which:

```swift
static func discoveredName(
    advertisedLocalName: String?,
    cachedName: String?,
    advertisementData: [String: Any]
) -> String {
    if let advertised = normalized(advertisedLocalName) { return advertised }
    if let cached = normalized(cachedName) { return cached }
    if isNotePinAdvertisement(advertisementData) { return notePinFallbackName }
    return ""
}
```

— i.e. it already falls back from the advertised local name to `peripheral.name`, then to a
NotePin-specific fallback. `OmiBleDiscoveryNaming.swift` was, before this patch, listed `verbatim`
in the manifest (byte-identical to the pin) — so **upstream itself already has this same
fallback**; it is not a fork-only fix and there is no "local-name-or-nothing" bug to fix here.

A second hypothesis (from new evidence attached mid-investigation, ellaaicare/ella-ai#1287 comment
5879501816) was that `NativeBluetoothDiscoverer` passes a non-empty `serviceUuids` filter down to
`CBCentralManager.scanForPeripherals(withServices:)`, so CoreBluetooth silently drops any necklace
that doesn't advertise the exact Omi/Friend service UUID before the Dart classifier ever runs.
**This also does not match this code**: `NativeBluetoothDiscoverer.discover()` already calls
`_hostApi.startScan(timeout, [])` — an empty list — and `OmiBleManager.startScan` already maps an
empty `serviceUuids` to `nil` before calling `scanForPeripherals(withServices:)`, which is an
unfiltered scan. A regression test (`requests an unfiltered scan (no serviceUuids filter)` in
`upstream_capture_discovery_admission_test.dart`) now guards this.

**What is actually still true**: with both the naming fallback and the scan already correct, the
one remaining gap for a genuinely unnamed, never-bonded necklace is that `peripheral.name` is
itself `nil` on iOS until the OS has done a GAP name lookup (typically only after a prior
connection) — a first-ever scan of a virgin necklace that advertises neither a local name nor a
service UUID can still resolve to an empty name, and `NativeBluetoothDiscoverer` correctly (if
silently) drops it as `no_name`. This can't be fixed by reclassifying names or unfiltering the
scan; it needs to be *observable*. So this patch is diagnostics, not another admission-logic
change:

1. `OmiBleDiscoveryNaming` gains `discoveredNameResult`, returning the resolved name alongside
   `hasAdvertisedLocalName` / `hasPeripheralName` — which source, if either, actually carried a
   name.
2. `OmiBleManager.didDiscover` forwards those two booleans on `BlePeripheral` (extended in both
   generated Pigeon definitions — `PigeonCommunicator.g.swift` and `app/lib/gen/pigeon_communicator.g.dart`
   — with the two new fields appended after `serviceUuids`, and a `false` default on the Dart side
   so every existing construction site keeps compiling).
3. `NativeBluetoothDiscoverer` records per-candidate redacted diagnostics (`hasAdvName`,
   `hasPeripheralName`, `uuidCount`, a coarse 10dBm RSSI bucket — never the raw device name, UUID,
   or precise RSSI) plus scan-started/stopped and rejected-by-reason counters, routed into
   `DebugLogManager`'s new always-on (not gated by the "Debug Logs" dev toggle) in-memory buffer,
   surfaced at Settings → Developer → Device Diagnostics with a "Copy Diagnostics" button. It also
   explicitly documents (see the code comment at the `startScan` call) that the scan is
   deliberately unfiltered, so a future change can't silently reintroduce the service-UUID filter
   hypothesis above.

**Confirmed identical upstream**: `OmiBleDiscoveryNaming.swift`, `OmiBleManager.swift`, and
`PigeonCommunicator.g.swift` were all `verbatim` (byte-identical to the pin) before this patch —
this fork has not diverged from upstream's discovery/naming/Pigeon-struct behavior at
`f16699aea7fe9ba089baceb628922f2882c51153`. Diffed against `BasedHardware/omi` at
`a74e4cfca376a7c8212687a23d9354e7e755671d` (`main`, same reference commit patch **One** used) —
these files were unchanged there too, so upstream's `didDiscover` has the identical
local-name-then-`peripheral.name` fallback, and there is no upstream fix to re-vendor.

**Upstream base SHA**: `a74e4cfca376a7c8212687a23d9354e7e755671d`. Upstream PR: none — same as
patch **One**, this is recorded as a local patch only.

**Upstreamable patch artifact**: `patches/upstream_capture/0002-native-discovery-diagnostics.patch`.
It applies to the upstream paths at the base SHA, on top of patch **One** already applied to
`native_bluetooth_discoverer.dart` (patch **Two**'s hunks for that file are incremental, not a
second independent diff against the pristine pin).

`scripts/verify_upstream_capture_identity.py` was extended by this patch: the `patched` manifest
kind previously only worked structurally for vendored `.dart` files (`structural_kind` was
hard-coded to `'dart-relocated'`); it now derives the expected structural kind from the file
extension, so `verbatim` native files (Swift here) can be `patched` too. The corresponding
assertions in `upstream_capture_byte_identity_test.dart` were generalized from a single
`entries.singleWhere(kind == 'patched')` to iterate all patched entries.

**Upstreamability**: the diagnostics addition is self-contained (new fields on an existing Pigeon
struct, new counters/buffer in a fork-owned utility class, redacted logging calls) with no
fork-specific dependencies in the touched upstream-owned files themselves. Local regressions live
in `upstream_capture_discovery_admission_test.dart` (peripheral.name-only admission, unfiltered
scan) and `upstream_capture_byte_identity_test.dart` (the five patched entries); the patch artifact
contains no private identifiers or environment assignments.

## Fork-side changes that are NOT upstream patches

These touch fork (non-upstream-owned) files so the vendored files can stay byte-identical:

1. `app/lib/models/stt_response_schema.dart` — replaced **in place** by the pin's exact bytes
   (listed as kind `in-place` in the manifest and byte-checked). The pin's copy is a strict
   superset of the fork's (four optional segment field names and their JSON keys).
   `app/lib/services/auth/auth_token_result.dart` — upstream's dependency-free token-result /
   session-expiry types, added **in place** at their upstream path (kind `in-place`).
2. `app/lib/services/auth_service.dart` — upstream's `auth_service.dart` is NOT vendored (it signs
   in/out of Firebase directly, bypassing Ella's quiesced identity transitions, which
   `test/ella/services/p1_hygiene_test.dart` enforces). The fork's `AuthService` gained the typed
   surface the vendored HTTP/capture files call: `refreshIdToken()` (upstream's result classes and
   terminal codes, no identity mutation), `expireSession()` (routes to
   `signOutForReauthentication()`), and an inert `recordAuthenticatedRequest401()`.
3. `app/lib/utils/analytics/analytics_manager.dart`, `app/lib/utils/platform/platform_manager.dart`
   — additive, inert members that the vendored code calls (`PlatformManager.analytics`,
   `appBuild`, `appNamespace`, `AnalyticsManager.track/isFeatureEnabled/omiDoubleTap/...`).
   Ella does not forward upstream product analytics; every member is a no-op and
   `isFeatureEnabled` fails closed. Vendoring upstream's analytics instead was not possible:
   it needs `posthog_flutter` 5.x APIs this fork's pinned toolchain does not resolve.
4. `app/pubspec.yaml` — `flutter_contacts` 1.1.9+2 → 2.1.0 and `flutter_timezone` 3.0.1 → 5.1.0
   (the APIs the vendored `phone_call_provider.dart` / `backend/http/api/users.dart` are written
   against). Upstream pins `flutter_contacts ^2.3.1`, which requires Dart 3.12/Flutter 3.44; this
   fork's CI pins Flutter 3.41.1, so 2.1.0 (same `permissions`/`getAll`/`ContactProperty` API)
   is used. Four fork call sites were ported to the new return types.
5. `app/lib/l10n/*.arb` — `transcriptionPausedReconnecting` (used by upstream
   `CaptureController`; values copied from the pin's ARB files where the locale exists).

## Proposed upstream hooks (drafted; NOT required by this port, nothing patched)

The port did not need these, but they would remove the two places where the Ella composition
has to reach past `composeCaptureProvider`. Draft PR body for BasedHardware/omi:

> **Title:** capture: expose `processInProgressConversation` on `CaptureDependencies`
>
> `composeCaptureProvider(CaptureDependencies)` is documented as the single production
> constructor path, but `CaptureDependencies` omits the `processInProgressConversation`
> seam that `CaptureController` already accepts. Hosts that want deterministic finish/
> processing tests (or a custom processing endpoint) must bypass the composition helper
> and call `CaptureProvider(...)` directly, duplicating its forwarding.
>
> This adds an optional `processInProgressConversation` field to `CaptureDependencies`
> and forwards it in `composeCaptureProvider`. Default `null` keeps today's behavior
> (the REST `processInProgressConversation()`); no production call site changes.
>
> Tests: extend `capture_composition_test.dart` to assert the new seam is forwarded
> intact.

> **Title:** capture: optional per-frame audio admission seam
>
> Capture admission today is the durable `CapturePolicy` (mute + revision). Hosts with an
> additional, live authorization requirement (e.g. a consent lease that can be revoked
> server-side mid-session) currently have to decorate three seams (`phoneMicRecorder`,
> `deviceConnectionLoader`, `openSocket`) to gate each frame. This proposes an optional
> `bool Function() audioAdmission` on `CaptureController`/`CaptureDependencies`, checked
> alongside `_admitsCapture(revision)` in the phone `onByteReceived`, the BLE
> `onAudioBytesReceived` and before `_socket.send` of audio frames; `null` means
> always-admitted (no behavior change).
