/// Single source of truth for the upstream-vendored capture path activation
/// flag. The exact same environment variable name (`ELLA_UPSTREAM_CAPTURE_ENABLED`)
/// is used to derive both this Dart constant (via `--dart-define`) and the Swift
/// `ELLA_UPSTREAM_CAPTURE_ENABLED` compilation condition consumed by
/// `app/ios/Runner/EllaUpstreamCaptureFlag.swift` — both are set from the same
/// shell variable in `app/ios/build-and-upload.sh`, so the two sides cannot
/// diverge. Default OFF.
const String ellaUpstreamCaptureFlagName = 'ELLA_UPSTREAM_CAPTURE_ENABLED';

const bool isEllaUpstreamCaptureEnabled = bool.fromEnvironment(
  ellaUpstreamCaptureFlagName,
  defaultValue: false,
);

/// Which capture implementation the flag selects. As of ella-ai#1280 P1 there
/// is no vendored upstream capture implementation yet (see UPSTREAM_PATCHES.md
/// for why: every device/capture-layer path this PR evaluated as a vendoring
/// candidate had already drifted from upstream in the fork). `vendoredUpstreamCapture`
/// is therefore a placeholder marker reserved for a follow-up phase; today the
/// flag only proves the selection mechanism itself is coherent and testable —
/// it does not yet switch any real capture code path in production.
enum CaptureCompileGraphPath {
  /// The existing Ella-owned necklace/phone capture orchestration
  /// (`CaptureProvider` in `app/lib/providers/capture_provider.dart`). This is
  /// the only path actually live in production today, regardless of the flag.
  legacyEllaCapture,

  /// Reserved for the vendored upstream BasedHardware/omi capture path once
  /// one exists. Selecting this value today is a no-op placeholder.
  vendoredUpstreamCapture,
}

CaptureCompileGraphPath selectCaptureCompileGraphPath({bool? flagOverride}) {
  final enabled = flagOverride ?? isEllaUpstreamCaptureEnabled;
  return enabled ? CaptureCompileGraphPath.vendoredUpstreamCapture : CaptureCompileGraphPath.legacyEllaCapture;
}
