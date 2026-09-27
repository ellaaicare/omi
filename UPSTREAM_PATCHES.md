# Upstream patches

No local patch is applied to an upstream-owned file. The snapshot under
`upstream-owned/` matches `BasedHardware/omi` `3219f05ced7fb518619abe92df856175b0a96985`
byte for byte.

Ella consent is not a row here. It waits on the hook drafted below. Until that
hook exists, `EllaUpstreamCaptureAdapter` gates audio at the Ella boundary and
the legacy capture path stays the one that runs (flag default off).

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
