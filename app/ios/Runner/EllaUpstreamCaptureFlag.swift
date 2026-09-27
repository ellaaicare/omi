// Single source of truth for whether the vendored upstream capture path is
// active on the native side. Mirrors the Dart-side `ELLA_UPSTREAM_CAPTURE_ENABLED`
// flag (app/lib/ella/services/ella_upstream_capture_flag.dart) — both are driven
// by the same `ELLA_UPSTREAM_CAPTURE_ENABLED` environment variable in
// app/ios/build-and-upload.sh so the two sides can never diverge. Default OFF.
#if ELLA_UPSTREAM_CAPTURE_ENABLED
let ellaUpstreamCaptureEnabled = true
#else
let ellaUpstreamCaptureEnabled = false
#endif
