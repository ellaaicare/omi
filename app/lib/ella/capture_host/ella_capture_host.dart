import 'package:flutter/widgets.dart';

/// Flag-OFF-safe registration point between the legacy Ella app graph and the
/// optional upstream capture graph (ellaaicare/ella-ai#1280).
///
/// This file must NEVER import `lib/upstream_capture/**` or
/// `lib/ella/upstream_capture/**`: it is reachable from the legacy entry point
/// (`lib/main.dart`) and the flag-OFF graph must not reach upstream code.
///
/// Activation is a single build setting, `ELLA_UPSTREAM_CAPTURE_ENABLED` in
/// `ios/Flutter/EllaUpstreamCapture.xcconfig` (default NO). The iOS build
/// derives from that one value (see ios/scripts/ella_upstream_capture_build_config.sh):
///   * the Swift compilation condition / excluded native sources,
///   * the `--dart-define-from-file` JSON carrying [upstreamCaptureDefine],
///   * the Flutter target (`lib/main_upstream_capture.dart` when YES).
/// Only `lib/main_upstream_capture.dart` calls [installUpstreamCapture], and it
/// refuses to run unless [upstreamCaptureDefine] is true.
class EllaCaptureHost {
  EllaCaptureHost._();

  /// Dart side of the single activation setting; generated from the xcconfig.
  static const bool upstreamCaptureDefine = bool.fromEnvironment('ELLA_UPSTREAM_CAPTURE_ENABLED');

  static Widget Function(BuildContext context)? _homeCaptureDockBuilder;

  /// True only in the flag-ON graph after the upstream capture entry point
  /// installed itself. The legacy necklace auto-connect and legacy WAL/device
  /// service start are suppressed while true so exactly one capture stack owns
  /// the microphone, the BLE pendant, and the on-disk WAL directory.
  static bool get upstreamCaptureActive => _homeCaptureDockBuilder != null;

  static bool get legacyCaptureSuppressed => upstreamCaptureActive;

  /// Home capture dock supplied by the upstream capture graph, or null (legacy dock).
  static Widget Function(BuildContext context)? get homeCaptureDockBuilder => _homeCaptureDockBuilder;

  static void installUpstreamCapture({required Widget Function(BuildContext context) homeCaptureDockBuilder}) {
    if (!upstreamCaptureDefine) {
      throw StateError('ELLA_UPSTREAM_CAPTURE_ENABLED is not set; refusing to install the upstream capture graph');
    }
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
  }

  @visibleForTesting
  static void installForTesting({required Widget Function(BuildContext context) homeCaptureDockBuilder}) {
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
  }

  @visibleForTesting
  static void resetForTesting() {
    _homeCaptureDockBuilder = null;
  }
}
