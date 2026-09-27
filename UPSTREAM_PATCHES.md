# Upstream patches

No local patch is applied to any file still sitting under `upstream-owned/`.
That snapshot matches `BasedHardware/omi` `3219f05ced7fb518619abe92df856175b0a96985`
byte for byte (P1 pinned 73 files; this pass widened it by 7 — see
UPSTREAM_OWNED.txt — still byte for byte at the same SHA).

This pass (ellaaicare/ella-ai#1280 P1 wiring) promotes a subset of that
snapshot — device connection + capture for the Omi necklace and phone mic —
into the real compile graph at `app/lib/upstream_capture/`. Promoted files
are copies, not the vendored originals, and every patch below is applied only
to those copies. The three patch classes below are the only ones used, and
every occurrence is called out by an `ELLA PATCH` comment at its exact
location in the promoted file, with a pointer back to the matching heading
here.

## Patch class 1 — namespaced cross-references (mechanical, no logic change)

Promoting `services/devices/**` and `services/mic/**` to their literal
upstream package paths (`package:omi/services/devices/...`) would collide
with Ella's own, independently-evolved files at those same paths (different
`models.dart`, `device_locator.dart`, `device_discoverer.dart`,
`device_transport.dart`, `watch_transport.dart` — see the divergence note in
UPSTREAM_OWNED.txt) and silently swap out the legacy path's types, breaking
rule 2/4 (unchanged default behavior). Every promoted file instead lives
under `app/lib/upstream_capture/`, and every promoted file's own imports of
another promoted file were rewritten from their literal upstream package
path to the `upstream_capture/` path it now lives at — a pure `sed`
find/replace over `package:omi/services/devices/...`,
`package:omi/services/mic/...`, `package:omi/services/bridges/...`,
`package:omi/gen/pigeon_communicator.g.dart`, and
`package:omi/gen/phone_mic_pigeon.g.dart`. No other line changed in any file
this class touches. Imports of genuinely shared, compatible Ella
infrastructure (`backend/schema/bt_device/bt_device.dart`, `utils/logger.dart`,
`services/notifications.dart`, `backend/preferences.dart`,
`utils/debug_log_manager.dart`) are untouched and still resolve to Ella's
real files.

Two symbols needed the same treatment one level deeper, because reusing
Ella's real file would have pulled in a type Ella's own version does not
carry (`DeviceConnectionState.connecting`, added upstream after Ella forked)
or pulled in a much larger graph than this pass carries at all
(`services/services.dart`'s whole orchestration hub, needed only for the
`IMicRecorderService` interface):

- `app/lib/upstream_capture/devices/device_service_facade.dart` — extracted
  verbatim from upstream's `services/devices.dart`: `DeviceConnectionState`
  and `OmiFeatures` only.
- `app/lib/upstream_capture/mic/mic_recorder_interface.dart` — extracted
  verbatim from upstream's `services/services.dart`: `IMicRecorderService`
  only.

Both are byte-for-byte copies of upstream's own definitions at the pinned
SHA; nothing was authored here that upstream does not already have.

## Patch class 2 — device_connection.dart factory trimmed to the Omi necklace

`services/devices/connectors/device_connection.dart`'s `DeviceConnectionFactory.create`
switches over `device.locator.kind` (`TransportKind`) and every `DeviceType`
upstream supports (apple_watch, bee, custom, fieldy, friend_pendant,
limitless, omiglass, plaud, rayban_meta). Two of upstream's own enum members
it needs — `DeviceType.raybanMeta` and `TransportKind.metaDat` — do not exist
on Ella's real `BtDevice`/`DeviceLocator` (`backend/schema/bt_device/bt_device.dart`),
because Ella forked before upstream added Ray-Ban Meta support and instead
added its own `wifi` transport kind and `frame` device type that upstream
does not carry. Adding upstream's missing members to Ella's shared,
pervasively-used enums risks turning every existing exhaustive switch over
them elsewhere in the app into a compile error (this fork's Dart language
version treats a non-exhaustive `switch` statement as an error, not a lint) —
a change with a blast radius far outside this pass's scope, and one this
pass has no way to audit exhaustively.

Given the acceptance goal is BLE connect/reconnect and capture for the
necklace and phone mic specifically (not every device upstream supports),
`DeviceConnectionFactory.create` was trimmed to construct a
`NativeBleTransport` directly from `device.id` and return an
`OmiDeviceConnection` only when `device.type == DeviceType.omi`, returning
null otherwise. The apple_watch, bee, custom, fieldy, friend_pendant,
limitless, omiglass, plaud and rayban_meta connectors, discoverers,
transports and bridges are not promoted this pass; they stay vendored
(dormant) under `upstream-owned/` for a later device-support phase — see
UPSTREAM_OWNED.txt.

Two more call sites in the same file class needed a matching trim, both
marked `ELLA PATCH` inline:

- `DeviceConnection.connect()` no longer calls
  `device = await device.getDeviceInfo(this)`. `BtDevice.getDeviceInfo` is
  Ella's own method, typed with Ella's own `DeviceConnection` hierarchy
  (`app/lib/backend/schema/bt_device/bt_device.dart`), and dispatches per
  `DeviceType` including types this pass does not promote — it cannot accept
  `this` (the promoted `DeviceConnection`). A device-info refresh for the
  necklace, if the acceptance run needs one, is the Ella adapter's job, not
  this file's.
- `native_bluetooth_discoverer.dart`'s scan result no longer sets
  `locator: DeviceLocator.bluetooth(...)` on the `BtDevice` it returns — that
  field is typed with Ella's own (incompatible) `DeviceLocator`. `locator` is
  optional on `BtDevice` and unused by the trimmed factory above, which
  resolves the transport from `device.id` directly, so it is left null.

**Upstream PR needed:** none. This is a scoping decision internal to this
fork (which device types to wire up this pass), not a bug in upstream's
factory.

## Patch class 3 — draft upstream PR: `mayEmitAudio()`

Ella consent is not a row upstream carries. It waits on the hook drafted
below. Until that hook exists, `EllaUpstreamCaptureAdapter` gates audio at
the Ella boundary (`app/lib/ella/upstream_capture/`, not upstream-owned) and
the legacy capture path stays the one that actually runs (flag default off).

## Draft upstream PR — `mayEmitAudio()`

**Title:** capture: fail closed on a host `mayEmitAudio()` check

**Body:**

`CapturePolicy` v1 is a three-field document (`version`, `revision`, `muted`)
parsed by native code. Adding a consent bit, or reusing `muted`, is the wrong
seam: `muted` is the user pause, and `fromJson` rejects any extra field
(`json.length != 3`).

Please add a host callback next to every place `_admitsCapture` is checked,
for both phone frames and device frames that are about to be handed to a
socket or WAL:

```dart
bool mayEmitAudio();
```

Default the callback to `true` so existing apps do not change. Ella Care will
implement it from the current consent receipt (App Store 5.1.1 / 5.1.2). When
it returns false:

- BLE connect and `ensureConnection` may still succeed;
- phone `start()` does not run;
- device frames are not written to the socket or promoted from a WAL `.bin`.

Do not treat a missing receipt as mute.

**Ella will not patch** `capture_controller.dart`, `capture_policy.dart`, or
`OmiBleManager.swift` locally to land this.
