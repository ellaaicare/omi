# Upstream patches — BasedHardware/omi@f16699aea7fe9ba089baceb628922f2882c51153

Tracks ellaaicare/ella-ai#1280.

## Transcript continuity: guarded Finish admission

The Ella dock retains read-only transcript access while its socket recovers.
That presentation change must not turn a queued Finish into processing for a
replacement account or conversation. The Ella runtime supplies a synchronous
owner/binding/recording/window predicate; the coordinator evaluates it when the
event dequeues, before transition or effects. False or throwing predicates deny
the event without effects. Unguarded existing callers retain their behavior.
The controller forwards the optional predicate and reports rejected admission
as a failure, rather than treating it as a saved conversation. A read-only
monotonic presentation generation advances on capture-session roll, conversation
replacement, reset and user-data clear; same-conversation refresh leaves it
unchanged. This prevents seconds-based WAL timestamps from admitting a replaced
window. The WAL timestamp itself is unchanged. No reducer,
WAL processing, transport or consent policy is changed. The predicate is not
rechecked after each effect: phone Finish intentionally retires its own session
while executing. Existing emission authority checks remain in force.

| File | Pin blob (unchanged upstream) | Reviewed local blob |
| --- | --- | --- |
| `app/lib/services/capture/capture_controller.dart` | `dee714b169e4f4e45777399f72db4672599fed3a` | `139ddbe23bbaa0a7d40f2fd43ae032423688d5ee` |
| `app/lib/services/capture/capture_coordinator.dart` | `17c33870dd18366a03cd599bcec9befb512f0b39` | `b0b79a5bd6acd748329bddb093ffe60c55b66765` |

The controller is explicitly declared `patched` for this forwarding seam; the
coordinator layers this guard on its existing documented patches. The manifest
still verifies the unchanged upstream pin and exact local blobs. Queue admission
regressions live in Ella-owned `ella_capture_finish_admission_test.dart`.

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

Flag-ON legacy UI routes use the same rule. `EllaUpstreamDeviceServiceAdapter` is fork-side adapter
code outside `lib/upstream_capture/**`; it delegates the existing picker/settings `IDeviceService`
contract to upstream `DeviceService`, and delegates explicit owner-bound connect/disconnect to
`EllaUpstreamCaptureRuntime`. Dock-origin connections are projected back into the legacy Home and
Settings presentation only when the upstream authority owner matches the current account; account
stop and stale wrappers are fenced at this adapter boundary. It adds no scanner, reconnect policy,
or vendored-source divergence, so there is no additional upstream patch artifact or blob exception
to record for this routing fix.

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

### Three: cross-layer discovery diagnostics (native / bridge / Dart) for a one-run diagnosis

**Symptom**: patch **Two** surfaced Dart-side discovery counters (candidates seen/admitted/
rejected-by-reason), but a review of that work (DISCOVERY-DIAGNOSTICS-001) found the in-app view
still could not distinguish which of the three layers dropped a discovery on a single run.
`OmiBleManager.startScan` only logged the CoreBluetooth state and the queued-vs-started outcome to
`NSLog`, not to Device Diagnostics. `OmiBleManager.didDiscover` had no native counter incremented
before the Pigeon call and no recorded `flutterApi != nil` state — the optional call at that call
site can silently drop every discovery (`onPeripheralDiscovered` is a no-op when `flutterApi` is
nil) while the Dart summary still reads `candidatesSeen=0`, with nothing to tell the two failure
modes apart. `BleFlutterApi.setUp(BleBridge.instance)` (the bridge registration) had no diagnostic
timestamp either.

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/ios/Runner/Ble/BleHostApiImpl.swift` | `patched` (was `verbatim`) | `415903a72829adfc83ca4c1321158db3b1ee059c` | `21618a4c26f51b5cce2c22e9db7588c8a5607797` |
| `app/ios/Runner/Ble/OmiBleManager.swift` | `patched` | `889d135a5a3fe1cbfccbb5baf88d980003df5c77` | `f6904c00f7b9cf28ed127e3ea95f60b6e9c10daf` |
| `app/ios/Runner/PigeonCommunicator.g.swift` | `patched` | `b774502d0c755cecdab9efefbb7db7d7606c287a` | `23bc2e8eaa23d20b116bf09cffb0902880ea1a8d` |
| `app/lib/gen/pigeon_communicator.g.dart` | `patched` | `25034c9152ceac9b4a4cc9a264027697b372a539` | `93c039f0938575adb3ec3929e6024506f07c3407` |

(`OmiBleDiscoveryNaming.swift` and `native_bluetooth_discoverer.dart` are unchanged by this patch;
their rows in patch **Two**'s table above still carry the current approved blobs.)

**What changed**:

1. A new `OmiBleDiagnostics` singleton (defined in `OmiBleManager.swift`, lock-guarded) records:
   the CoreBluetooth state observed at each `startScan` call and whether it started immediately or
   was queued; when a queued scan fires once CoreBluetooth reaches `poweredOn`; the native
   `didDiscover` count, incremented before the Pigeon call to Dart; and a count of `didDiscover`
   calls where `flutterApi` was nil (the silent-drop path above). Counts and a CoreBluetooth state
   label only — never a device name or UUID.
2. `BleNativeDiscoveryDiagnostics` (a new struct, codec discriminator `136`) and
   `BleHostApi.getNativeDiscoveryDiagnostics()` (a new host method) expose that snapshot to Dart —
   added to both generated Pigeon definitions, `PigeonCommunicator.g.swift` and
   `app/lib/gen/pigeon_communicator.g.dart`. `BleHostApiImpl.swift` implements the new method by
   forwarding to `OmiBleManager.getNativeDiscoveryDiagnostics()`, which is the only reason it moves
   from `verbatim` to `patched` in this patch.
3. `DebugLogManager.recordBleFlutterApiSetUp()` (fork-owned, not upstream-owned) records a
   wall-clock timestamp; `EllaUpstreamCaptureRuntime._bootProduction` (fork-owned) calls it
   immediately after `BleFlutterApi.setUp(BleBridge.instance)` — the "bridge" layer.
4. `DeviceDiagnosticsPage` (fork-owned) now fetches the native snapshot via
   `BleHostApi.getNativeDiscoveryDiagnostics()`, reads the bridge timestamp from
   `DebugLogManager`, and renders native + bridge + the existing Dart counters together; the Copy
   button's exported text includes all three layers.

**Confirmed identical upstream (before this patch)**: `BleHostApiImpl.swift` was `verbatim`
(byte-identical to the pin) before this patch — it exposes exactly upstream's own BLE host
surface, with no discovery-diagnostics accessor. `OmiBleManager.swift` and
`PigeonCommunicator.g.swift` were already `patched` by patch **Two** for the naming-diagnostics
fields; this patch layers a second, independent change onto each (the new diagnostics recorder and
Pigeon method) rather than replacing patch **Two**'s hunks.

**Upstream base SHA**: same pin, `f16699aea7fe9ba089baceb628922f2882c51153`. Upstream PR: none —
recorded as a local patch only, same as patches **One** and **Two**.

**Upstreamable patch artifact**: `patches/upstream_capture/0003-native-discovery-cross-layer-diagnostics.patch`.
It applies to the upstream paths at the base SHA, on top of patches **One** and **Two**.

**Upstreamability**: the new host method and struct are additive (a new Pigeon method + struct, a
new fork-owned singleton) with no fork-specific dependencies in the touched upstream-owned files.
Local regressions live in `app/test/ella/upstream_capture/upstream_capture_native_discovery_diagnostics_test.dart`
(Dart, via a fake `BleHostApi`) and `upstream_capture_byte_identity_test.dart` (now six patched
entries); the patch artifact contains no private identifiers or environment assignments.

### Four: native discovery still admits nothing on build 877 (RUN-016) — scan-response timing, not a filter or naming bug

**Symptom** (ellaaicare/ella-ai#1280 RUN-016): build 877 — flag ON, carrying patches **One**
through **Three** — still lists no necklace. Native `didDiscoverCount` == 9 and Dart
`candidatesSeen` == 9 (so the bridge/Pigeon layer is not the bug, confirming patch **Three**'s
diagnostics). Of those 9: `admitted` = 0, `no_name` = 8, `no_signature_match` = 1. **All 9
rejected candidates had `uuidCount == 0` AND `hasAdvName == false` AND no `peripheral.name`
either** — including the strongest-RSSI candidate (~-30 to -20 dBm), believed to be the actual
necklace. The legacy `flutter_blue_plus` path finds and names the same physical device within
~5s on the same phone, same room.

**Investigation of the legacy path** (`app/lib/services/devices/discovery/bluetooth_discoverer.dart`
and `app/lib/utils/bluetooth/bluetooth_adapter.dart`, cross-checked against
`flutter_blue_plus` 2.1.0 / `flutter_blue_plus_darwin` 8.1.0's iOS implementation, pulled from
pub.dev for this investigation):

1. **Service-UUID filter**: `bluetooth_discoverer.dart:46` calls
   `BluetoothAdapter.startScan(timeout: Duration(seconds: timeout))` with **no** `withServices`
   argument. `BluetoothAdapter.startScan` (`bluetooth_adapter.dart:31-40`) defaults an omitted
   `withServices` to `<Guid>[]`. In `flutter_blue_plus_darwin`'s Obj-C plugin
   (`FlutterBluePlusPlugin.m:271-290`), an empty `with_services` array becomes
   `scanForPeripheralsWithServices:@[] options:...]` — CoreBluetooth's documented "match
   everything" form. **The legacy path is already unfiltered**, exactly like
   `NativeBluetoothDiscoverer` (confirmed already by patch **Two**). Ruled out; no change made.
2. **`CBCentralManagerScanOptionAllowDuplicatesKey`**: `FlutterBluePlus.startScan`
   (`flutter_blue_plus.dart:217-234`) defaults `continuousUpdates: false`, which the legacy
   discoverer never overrides. In the Obj-C plugin (`FlutterBluePlusPlugin.m:257-261`), the key
   is only added to the scan options dictionary `if ([continuousUpdates boolValue])` — i.e. when
   `continuousUpdates` is false, the key is **omitted entirely** rather than explicitly set to
   `NO`. `NativeBluetoothDiscoverer`/`OmiBleManager` (pre-this-patch) instead explicitly passed
   `CBCentralManagerScanOptionAllowDuplicatesKey: false` — the documented default, so *as coded*
   this was not a difference either. But CoreBluetooth's real (not just documented) behavior is
   that duplicate filtering caps each peripheral at exactly one `didDiscover` callback for the
   whole scan session — if that single delivered packet is the bare primary advertisement
   (common when the scan-response carrying the local name/service UUIDs is a separate radio
   event that hasn't arrived yet when CoreBluetooth reports the discovery), the app never gets a
   second chance within that scan, matching RUN-016's evidence exactly (bare on the one and only
   callback). This is consistent with, though not provably identical to, why the legacy path —
   subject to the same race in principle — happened to get a fuller first packet in its ~5s
   window during the comparison run.
3. **`retrieveConnectedPeripherals(withServices:)` / `retrievePeripherals(withIdentifiers:)`**:
   the legacy discoverer's discovery path (`bluetooth_discoverer.dart`) calls neither; nothing in
   `app/lib/services/devices.dart`'s discovery flow does either.
   `retrievePeripheralsWithIdentifiers:` is used only later, by `OmiBleManager.connectPeripheral`,
   for reconnecting a *previously connected* device by known UUID — not applicable to RUN-016's
   never-bonded necklace, which is not connected anywhere and so would not appear in
   `retrieveConnectedPeripherals` either. Not implemented.

**Fix implemented: (b) only** — `AllowDuplicatesKey` on, plus a targeted re-emit:

- `OmiBleManager.startScan` now passes `CBCentralManagerScanOptionAllowDuplicatesKey: true`
  (`app/ios/Runner/Ble/OmiBleManager.swift`), so CoreBluetooth is no longer capped at one
  `didDiscover` callback per peripheral for the scan session.
- A new pure helper, `OmiBleDiscoveryNaming.shouldForwardRediscovery` (in
  `app/ios/Runner/Ble/OmiBleDiscoveryNaming.swift`), decides whether a later sighting of an
  already-seen peripheral (this scan) carries a name or service UUIDs the first sighting
  lacked. `OmiBleManager` tracks each peripheral's most recently forwarded naming state in a
  per-scan-session dictionary (`scanSightings`, cleared on every `startScan`/`stopScan`) and
  only calls `flutterApi?.onPeripheralDiscovered` again when that helper says yes — so a
  peripheral that keeps re-advertising identical (still-bare, or already-fully-named) data does
  **not** flood the Pigeon channel every scan interval; only a genuinely fuller packet does.
- `(a)` and `(c)` were investigated and are not implemented: the scan is already unfiltered
  (unchanged from patch **Two**), and no discovery-time code path — legacy or native — calls
  `retrieveConnectedPeripherals`/`retrievePeripherals(withIdentifiers:)`, which would not help a
  peripheral that was never connected anyway.

**Diagnostics**: a new `nameArrivedLate` counter (`OmiBleDiagnostics`, surfaced through
`BleNativeDiscoveryDiagnostics` / `EllaNativeDiscoveryDiagnostics` / `DeviceDiagnosticsPage`,
same three-layer plumbing patch **Three** built) counts re-discoveries that were forwarded
because a later packet added naming/UUID information. The existing `didDiscoverCount` /
`candidatesSeen` counters are kept, but with `AllowDuplicatesKey` on they are expected to
diverge going forward (raw CoreBluetooth callback volume vs. Dart-forwarded candidates) where
they previously matched 1:1 — the gap between them is itself now a useful signal, not a
regression.

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/ios/Runner/Ble/OmiBleDiscoveryNaming.swift` | `patched` | `d078da9cb337a4a2a176861f91951e469bc3efcb` | `936a06128c1af218beefe217b72064d3da718aae` |
| `app/ios/Runner/Ble/OmiBleManager.swift` | `patched` | `889d135a5a3fe1cbfccbb5baf88d980003df5c77` | `db4893d5864eefffd462e6161dc90e6e23342418` |
| `app/ios/Runner/PigeonCommunicator.g.swift` | `patched` | `b774502d0c755cecdab9efefbb7db7d7606c287a` | `55b306e3d7cefacd185450ab17fe4b640887003f` |
| `app/lib/gen/pigeon_communicator.g.dart` | `patched` | `25034c9152ceac9b4a4cc9a264027697b372a539` | `c8f25e93ec06b7a8b7f1c3a0fc8afb58d9d92945` |

(`native_bluetooth_discoverer.dart` and `BleHostApiImpl.swift` are unchanged by this patch;
their rows in the tables above still carry their current approved blobs.)

**Confirmed identical upstream (before this patch)**: `OmiBleDiscoveryNaming.swift` and
`OmiBleManager.swift` carried patch **Two**'s and **Three**'s local fixes only — the
`AllowDuplicatesKey` value and the single-sighting-per-scan behavior were otherwise as upstream
wrote them (upstream's own `didDiscover` also omits `CBCentralManagerScanOptionAllowDuplicatesKey`
entirely, the FBP-equivalent default). This diverges further from upstream's native scan
behavior for the reasons above; there is no upstream fix to re-vendor.

**Upstream base SHA**: same pin, `f16699aea7fe9ba089baceb628922f2882c51153`. Upstream PR: none —
recorded as a local patch only, same as patches **One** through **Three**.

**Tests**: `app/test/ella/upstream_capture/upstream_capture_discovery_admission_test.dart` gained
a regression test simulating two `peripheralDiscoveredCallback` deliveries for the same
peripheral uuid (first bare, second named) and asserting exactly one admitted device.
`app/test/ella/upstream_capture/upstream_capture_native_discovery_diagnostics_test.dart` was
extended to cover `nameArrivedLate`. `app/ios/Tests/OmiBleDiscoveryNamingTests.swift` (new,
`swiftc`-executable, following the existing `GuardianNativePolicyTests.swift` pattern — there is
no XCTest target for this native code) covers `shouldForwardRediscovery` directly, wired into
`.github/workflows/ella-ios-source-ci.yml`. `upstream_capture_byte_identity_test.dart`'s
hardcoded blob expectations were updated for all four touched files.

**Upstreamability**: `AllowDuplicatesKey` and the re-emit dedup are a behavior change to native
scan semantics (more callback volume, traded for correctness on slow-to-respond peripherals),
not upstreamable as a drop-in fix without upstream also wanting the added Pigeon field and
diagnostics; recorded as a local patch only, as with patches **One** through **Three**.

### Five: native discovery still admits nothing on build 879 (RUN-018) — the necklace was already connected/known, not late-advertising

**Symptom** (ellaaicare/ella-ai#1287 RUN-018, build 879, flag ON): patch **Four**'s fix works —
`didDiscoverCount` = 12 vs. Dart `candidatesSeen` = 6 in this run, confirming more raw
CoreBluetooth callbacks are reaching the candidate pipeline than before. But the deeper bug is
**not** fixed: `nameArrivedLate` = 0, and **every** candidate observed in this run has
`hasAdvName == false` **and** `uuidCount == 0`. This rules out patch **Four**'s "scan-response
arrives late" theory entirely — these candidates never carry a usable advertised name or
service UUID at all, at any point in the scan.

**Hypothesis**: the necklace is often already connected at the iOS/CoreBluetooth level when the
app starts scanning (e.g. a prior app lifetime connected it seconds earlier, or CoreBluetooth
state restoration reconnected it before the scan even starts). A peripheral already connected
to the system stops advertising, so a fresh `scanForPeripherals` scan will never surface it as a
discovery event — no name, no service UUIDs, nothing to admit on. This matches patch **Four**'s
own investigation, which found `OmiBleManager` already uses state restoration
(`restoreIdentifier: "com.omi.ble.restore"`) and that the legacy/known-device reconnect path
finds its target via `retrievePeripherals(withIdentifiers:)`, not scanning — neither of which the
discovery *candidate* path (as opposed to the reconnect path) ever consulted.

**Fix**: in addition to the existing unfiltered scan, the discovery path now also surfaces
peripherals through three additional CoreBluetooth sources, each forwarded to Dart as a
discovery candidate (`BlePeripheral`, new `source` field) exactly like a scanned one:

- `retrieveConnectedPeripherals(withServices:)`, filtered to the Omi service UUID Dart already
  owns (`omiServiceUuid`) — peripherals already connected system-wide that expose it. Tagged
  `source: "retrievedConnected"`.
- `retrievePeripherals(withIdentifiers:)`, for the saved/paired device id(s) Dart already reads
  from `SharedPreferencesUtil().btDevice` / `.btDevices` (the same store the legacy reconnect
  path uses — no new store introduced). Tagged `source: "retrievedKnown"`.
- `centralManager(_:willRestoreState:)` (the existing state-restoration delegate), which now also
  forwards each restored peripheral through `onPeripheralDiscovered` alongside its existing
  `onStateRestored` call. Tagged `source: "restored"`.

A single new native method, `BleHostApi.retrieveConnectedAndKnownPeripherals(serviceUuids:,
knownDeviceIds:)`, covers the first two (Dart calls it once per `discover()`, right after
`startScan`, passing `[omiServiceUuid]` and the ids from the persisted device store); the third
piggybacks on the existing restoration delegate. A new pure-logic file,
`OmiBleRetrievalTagging.swift`, merges and deduplicates the two retrieval results (a peripheral
present in both keeps the stronger `retrievedConnected` tag rather than double-counting).

None of these three sources involve an active scan, so none of them ever carry an advertised
name — only whatever `CBPeripheral.name` (the OS's cached GAP name) happens to already be. The
Dart admission classifier (`NativeBluetoothDiscoverer._isAdmittedBySource`) now admits a nameless
candidate on source evidence alone: `retrievedConnected`/`retrievedKnown` are admitted
unconditionally (CoreBluetooth itself already vetted them against the service filter or the known
id list); a `restored` candidate is evaluated the same way — Omi service present, or its id is a
saved/paired device — since state restoration doesn't pre-filter by service the way (a) does.

**Diagnostics**: three new counters — `retrievedConnectedCount`, `retrievedKnownCount`,
`restoredCount` — added to `OmiBleDiagnostics` / `BleNativeDiscoveryDiagnostics` /
`EllaNativeDiscoveryDiagnostics`, wired through the same three-layer plumbing patch **Three**
built, and surfaced on `DeviceDiagnosticsPage` alongside `didDiscoverCount` / `candidatesSeen` /
`nameArrivedLate`.

**Feature flag**: this patch only adds a new capture-layer path behind the existing
`ELLA_UPSTREAM_CAPTURE_ENABLED` flag (default `NO`, unchanged) — no default is flipped by this
patch.

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/ios/Runner/Ble/BleHostApiImpl.swift` | `patched` | `415903a72829adfc83ca4c1321158db3b1ee059c` | `0965ab69aadaa375b6be7dd03bb62f28801907cb` |
| `app/ios/Runner/Ble/OmiBleManager.swift` | `patched` | `889d135a5a3fe1cbfccbb5baf88d980003df5c77` | `3cfc843cd51b5b43e913a247c3fef5ebc6be0431` |
| `app/ios/Runner/PigeonCommunicator.g.swift` | `patched` | `b774502d0c755cecdab9efefbb7db7d7606c287a` | `de1bba67938c468d07ae766360634036649d6b0b` |
| `app/lib/gen/pigeon_communicator.g.dart` | `patched` | `25034c9152ceac9b4a4cc9a264027697b372a539` | `8a1c25790b78a3f0b650005b15f19e4a38db887b` |
| `app/lib/services/devices/discovery/native_bluetooth_discoverer.dart` | `patched` | `0a7aec27f031d61972599823158d8f77731dc2b4` | `5136dde20405c645d0f3dc1d86231e674a401a77` |

(`OmiBleDiscoveryNaming.swift` is unchanged by this patch; its row in patch **Four**'s table above
still carries the current approved blob. `OmiBleRetrievalTagging.swift` is a brand-new,
Ella-only file with no upstream counterpart — it is not listed in `UPSTREAM_OWNED.txt`, same as
any other fork-owned file that happens to live alongside vendored native sources.)

**Confirmed identical upstream (before this patch)**: all five touched files carried patches
**Two** through **Four**'s local fixes only, otherwise unchanged from the pin
(`f16699aea7fe9ba089baceb628922f2882c51153`). Upstream's own discovery path has no
`retrieveConnectedPeripherals`/`retrievePeripherals(withIdentifiers:)` usage in its candidate
pipeline either (confirmed by patch **Four**'s investigation into item (a)/(c), left unimplemented
there); there is no upstream fix to re-vendor.

**Upstream base SHA**: same pin, `f16699aea7fe9ba089baceb628922f2882c51153`. **No upstream PR** —
this is recorded as a local, Ella-side-only capture-layer addition, same as patches **One**
through **Four**.

**Tests**: `app/test/ella/upstream_capture/upstream_capture_discovery_admission_test.dart` gained
a group covering admission of each of the three new sources (`retrievedConnected`,
`retrievedKnown`, `restored`) with no advertised name present. `app/ios/Tests/OmiBleRetrievalTaggingTests.swift`
(new, `swiftc`-executable, following the existing `OmiBleDiscoveryNamingTests.swift` /
`GuardianNativePolicyTests.swift` pattern — no XCTest target exists for this native code) covers
`OmiBleRetrievalTagging.mergeTaggedCandidates` directly, wired into
`.github/workflows/ella-ios-source-ci.yml`. `upstream_capture_byte_identity_test.dart`'s hardcoded
blob expectations were updated for the four re-patched files plus
`native_bluetooth_discoverer.dart`.

**Upstreamability**: the two new retrieval sources and the restoration forward are additive (a new
Pigeon method, a new field on an existing struct, three new counters, one new pure-logic file)
with no fork-specific dependencies in the touched upstream-owned files; recorded as a local patch
only, as with patches **One** through **Four**.

### Six: necklace admitted via retrievedKnown never actually connects (RUN-020) — plus an unwanted location prompt and a dishonest dock

**Symptom** (ellaaicare/ella-ai#1287 RUN-020, build 881, flag ON): patch Five's discovery-admission
fix works — a necklace already connected/known at the CoreBluetooth level is now admitted
(`source=retrievedKnown`, `retrievedKnownCount=1`, `admitted=1`, confirmed on build 881). But after
admission the dock shows "Recording with your necklace" while necklace audio is 0 B/s and Settings →
Connected Device shows "Not connected." Two further problems were reproduced on the same build: an
"Allow While Using App" **location** prompt during connect, and the flag-ON dock missing controls
(Transcript, Whispers) the flag-OFF Home dock has, with **Finish conversation** stuck greyed out.

**Root cause (the connect bug)**: `OmiBleManager.connectPeripheral(uuid:)` already tracks the
peripheral once it has been surfaced through `retrieveConnectedAndKnownPeripherals` (patch Five sets
`peripherals[info.uuid] = peripheral` for every retrieved candidate). When Dart's
`NativeBleTransport.connect()` calls `manageDevice` → `connectPeripheral` for that uuid, the
peripheral can already report `CBPeripheralState.connected` at the CoreBluetooth/system level (a
prior app lifetime connected it, or the OS itself still holds the link). The pre-patch code took a
shortcut for that case:

```swift
if peripheral.state == .connected {
    NSLog("[OmiBle] connectPeripheral: \(uuid) already connected, skipping")
    return
}
```

This never called `centralManager.connect(peripheral, options: nil)` — correctly, since CoreBluetooth
treats that as a no-op on an already-connected peripheral and will not invoke `didConnect` — but it
also never called `peripheral.discoverServices(nil)` directly, unlike the equivalent
already-connected branch in `willRestoreState`. So this process's own GATT session (service +
characteristic discovery, then the audio-notify subscribe `NativeBleTransport` performs once services
are known) was never established. Dart's `_deviceReadyCompleter` in `NativeBleTransport.connect()`
then never completes, `RecordingState` never reliably reaches a genuinely connected state, and the
dock's `starting`/Finish-button logic (driven by that same pending state) stays stuck.

**Fix**: `connectPeripheral` now drives the same ready flow `didConnect` drives — service discovery,
plus the same reconnection-count/timestamp bookkeeping `didConnect` already did — instead of a bare
`return`, whenever the peripheral is already connected at the CoreBluetooth level. The decision itself
(`connect` vs. `discoverServicesDirectly`) is extracted into a new pure-logic file,
`OmiBleReconnectPolicy.swift` (fork-owned, no upstream counterpart, same as `OmiBleRetrievalTagging.swift`),
so it is covered by a `swiftc`-executable test without a live `CBPeripheral`.

**Root cause (the location prompt)**: `capture_coordinator.dart`'s pendant-session-start transition
had one call site, `_reduceDeviceStart`, that built `StartDeviceSessionStage` with
`promptLocation: device != null` — i.e. every necklace session start requested
`Geolocator.requestPermission()` (routed through `capture_controller.dart`'s
`_startDeviceSessionBody` → `_captureSessionLocation(promptIfDenied: true)` →
`ConversationLocationCapture`). Every other `StartDeviceSessionStage` construction in this same file
already passes `promptLocation: false` — this was the one outlier. Location is not part of Ella's
consent model and is not needed for BLE.

Grepped `CLLocationManager`, `Permission.location`, `Geolocator.requestPermission`, and
`Geolocator.getCurrentPosition` across `app/lib/upstream_capture/` and `app/lib/ella/upstream_capture/`:
the only other location-permission-adjacent reference is `bluetooth_readiness.dart`'s Android-only
`Permission.locationWhenInUse.isGranted` **check** (not a request) gating pre-Android-12 BLE scan
readiness — unrelated to iOS necklace connect, and a check rather than a prompt. No other call site
requests location during a plain necklace connect.

**Fix**: `promptLocation: device != null` → `promptLocation: false` at that one call site.

**Root cause (the dock)**: `EllaUpstreamCaptureDock` (fork-owned) only had phone/necklace record
buttons and Finish — no live Transcript view or Whispers on/off controls, unlike
`today_page.dart`'s flag-OFF Home dock. Finish's `onPressed: starting ? null : ...` stayed disabled
because `starting` (`_busy || state == RecordingState.initialising`) never cleared while the connect
bug above left `connectNecklace()`'s `ensureConnection` call pending.

**Fix**: added a Transcript toggle (reads `CaptureProvider.segments`, the same field
`capture_controller.dart` already populates — no transcript logic reimplemented) and a Whispers
on/off row that reuses `today_page.dart`'s own `whisperStatusLead`/`whisperStatusDetail` status-text
functions and the same `GuardianModeLoader`/`GuardianModeSetter`/`GuardianNativeLifecycle`/
`GuardianAvailability` testing seams `today_page.dart` already defines (imported, not duplicated),
calling the same underlying `guardian_mode_api`/`guardian_mode_service` APIs `today_page.dart`'s
`_setWhispers` calls. Finish's greyed-out state needed no separate code change: fixing the connect bug
above means `connectNecklace()` resolves promptly instead of hanging, so `starting` clears correctly;
this is verified directly by a widget test that drives a scripted `connectNecklace` to genuinely-live
state and asserts Finish becomes tappable.

| File | Manifest kind | Pin blob (unchanged upstream) | Approved local blob |
| --- | --- | --- | --- |
| `app/ios/Runner/Ble/OmiBleManager.swift` | `patched` | `889d135a5a3fe1cbfccbb5baf88d980003df5c77` | `c08b59564c9338c54439696d7e95ad27c16e1eeb` |
| `app/lib/upstream_capture/services/capture/capture_coordinator.dart` | `patched` (was `dart-relocated`) | `17c33870dd18366a03cd599bcec9befb512f0b39` | `c86b5ab4c2fbd5b23635b755cd46de32e0ed33ae` |

(Every other patched file's row from patches One through Five is unchanged by this patch; their
current approved blobs are recorded in those tables above. `OmiBleReconnectPolicy.swift` is a
brand-new, Ella-only file with no upstream counterpart — like `OmiBleRetrievalTagging.swift`, it is
not listed in `UPSTREAM_OWNED.txt`.)

**Confirmed identical upstream (before this patch)**: `OmiBleManager.swift` carried patches Two,
Three, and Five's local fixes only; `capture_coordinator.dart` was `dart-relocated` (byte-identical to
the pin modulo the import relocation) — the `promptLocation: device != null` call site was upstream's
own code, unchanged by this fork until now. Upstream has no `retrieveConnectedAndKnownPeripherals`
equivalent at all (that capability is this fork's own addition from patch Five), so there is no
upstream connect-flow bug to compare against for the first fix; the location-prompt call site is
upstream's own `DeviceStartRequested` handling, prompting on every device connect — this fork already
diverges from that default at every other call site in this file (the "recording is the tap; location
is metadata" product decision predates this patch).

**Upstream base SHA**: same pin, `f16699aea7fe9ba089baceb628922f2882c51153`. **No upstream PR** — both
fixes are fork-specific (the first patches a fork-only code path added by patch Five; the second is
Ella's own consent-model product decision, consistent with every other call site in this file),
recorded as local patches only, same as patches One through Five.

**Tests**: `app/ios/Tests/OmiBleReconnectPolicyTests.swift` (new, `swiftc`-executable, following the
`OmiBleDiscoveryNamingTests.swift` / `OmiBleRetrievalTaggingTests.swift` pattern — no XCTest target
exists for this native BLE code) covers `OmiBleReconnectPolicy.decision(for:)` for `.connected`,
`.disconnected`, `.connecting`, and `.disconnecting` states, wired into
`.github/workflows/ella-ios-source-ci.yml`.
`app/test/ella/upstream_capture/upstream_capture_no_location_prompt_test.dart` (new) statically
asserts every `StartDeviceSessionStage(...)` construction in `capture_coordinator.dart` passes a
literal `promptLocation: false`.
`app/test/ella/upstream_capture/ella_upstream_capture_dock_test.dart` gained two widget tests: one
asserting Transcript and Whispers render once a necklace session is genuinely live (not just
selected), the other asserting Finish stays disabled while a scripted `connectNecklace` is in flight
and becomes enabled once it resolves.
`upstream_capture_byte_identity_test.dart`'s hardcoded blob expectations were updated for the two
re-patched files (seven patched entries total).

**Upstreamability**: neither fix is upstreamable as a drop-in — the connect fix patches a fork-only
code path (patch Five's retrieval sources have no upstream equivalent), and the location-prompt
removal is Ella's own consent-model product decision, not a general bug fix; both are recorded as
local patches only, as with patches One through Five.

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
