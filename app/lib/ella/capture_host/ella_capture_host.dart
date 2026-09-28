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
/// Flag-OFF-safe, plain-Dart snapshot of the native BLE discovery diagnostics
/// recorded by `OmiBleDiagnostics` (ellaaicare/ella-ai#1280 RUN-010). Counts
/// and a CoreBluetooth state label only — never a device name or UUID. Kept
/// here (not in `lib/upstream_capture/gen/pigeon_communicator.g.dart`) so
/// [DeviceDiagnosticsPage] can read it without importing the upstream graph.
class EllaNativeDiscoveryDiagnostics {
  const EllaNativeDiscoveryDiagnostics({
    required this.lastStartScanCbState,
    required this.scansStartedImmediately,
    required this.scansQueued,
    required this.queuedScansFired,
    required this.didDiscoverCount,
    required this.flutterApiNilDropCount,
  });

  /// CoreBluetooth state observed at the most recent startScan call.
  final String lastStartScanCbState;
  final int scansStartedImmediately;
  final int scansQueued;
  final int queuedScansFired;

  /// Incremented before the didDiscover Pigeon call reaches Dart.
  final int didDiscoverCount;

  /// Count of didDiscover callbacks dropped because flutterApi was nil.
  final int flutterApiNilDropCount;
}

class EllaCaptureHost {
  EllaCaptureHost._();

  /// Dart side of the single activation setting; generated from the xcconfig.
  static const bool upstreamCaptureDefine = bool.fromEnvironment('ELLA_UPSTREAM_CAPTURE_ENABLED');

  static Widget Function(BuildContext context)? _homeCaptureDockBuilder;

  static Future<EllaNativeDiscoveryDiagnostics> Function()? _nativeDiscoveryDiagnosticsLoader;

  /// True only in the flag-ON graph after the upstream capture entry point
  /// installed itself. The legacy necklace auto-connect and legacy WAL/device
  /// service start are suppressed while true so exactly one capture stack owns
  /// the microphone, the BLE pendant, and the on-disk WAL directory.
  static bool get upstreamCaptureActive => _homeCaptureDockBuilder != null;

  static bool get legacyCaptureSuppressed => upstreamCaptureActive;

  /// Home capture dock supplied by the upstream capture graph, or null (legacy dock).
  static Widget Function(BuildContext context)? get homeCaptureDockBuilder => _homeCaptureDockBuilder;

  /// Loader for cross-layer native BLE discovery diagnostics, supplied by the
  /// upstream capture graph, or null when that graph isn't active (flag OFF,
  /// or not yet booted) — [DeviceDiagnosticsPage] treats null as "unavailable".
  static Future<EllaNativeDiscoveryDiagnostics> Function()? get nativeDiscoveryDiagnosticsLoader =>
      _nativeDiscoveryDiagnosticsLoader;

  static void installUpstreamCapture({required Widget Function(BuildContext context) homeCaptureDockBuilder}) {
    if (!upstreamCaptureDefine) {
      throw StateError('ELLA_UPSTREAM_CAPTURE_ENABLED is not set; refusing to install the upstream capture graph');
    }
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
  }

  static void installNativeDiscoveryDiagnosticsLoader(Future<EllaNativeDiscoveryDiagnostics> Function() loader) {
    _nativeDiscoveryDiagnosticsLoader = loader;
  }

  @visibleForTesting
  static void installForTesting({required Widget Function(BuildContext context) homeCaptureDockBuilder}) {
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
  }

  @visibleForTesting
  static void resetForTesting() {
    _homeCaptureDockBuilder = null;
    _nativeDiscoveryDiagnosticsLoader = null;
  }
}
