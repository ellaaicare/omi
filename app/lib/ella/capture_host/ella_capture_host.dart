import 'package:flutter/widgets.dart';

import 'package:omi/services/devices.dart';

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
    this.nameArrivedLate = 0,
    this.retrievedConnectedCount = 0,
    this.retrievedKnownCount = 0,
    this.restoredCount = 0,
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

  /// Count of re-discoveries forwarded because a later advertisement packet
  /// added a name or service UUID the first sighting lacked
  /// (ellaaicare/ella-ai#1280 RUN-016). See `OmiBleDiscoveryNaming.shouldForwardRediscovery`.
  final int nameArrivedLate;

  /// Count of peripherals surfaced via `retrieveConnectedPeripherals(withServices:)`
  /// (ellaaicare/ella-ai#1287 RUN-018).
  final int retrievedConnectedCount;

  /// Count of peripherals surfaced via `retrievePeripherals(withIdentifiers:)` for a
  /// saved/paired device id (ellaaicare/ella-ai#1287 RUN-018).
  final int retrievedKnownCount;

  /// Count of peripherals delivered via `centralManager(_:willRestoreState:)`
  /// (ellaaicare/ella-ai#1287 RUN-018).
  final int restoredCount;
}

enum EllaCaptureSocketFailureReason {
  captureOriginRetired('capture_origin_retired'),
  transportConnectFailed('transport_connect_failed'),
  captureProtocolReadyUnavailable('capture_protocol_ready_unavailable'),
  invalidCaptureProtocolReady('invalid_capture_protocol_ready'),
  captureSocketClosed('capture_socket_closed'),
  captureSocketClosedBeforeReady('capture_socket_closed_before_ready'),
  captureSocketError('capture_socket_error');

  const EllaCaptureSocketFailureReason(this.code);
  final String code;
}

enum EllaCaptureSocketAttemptStatus { pending, ready, failed }

enum EllaCaptureSocketAttemptPhase {
  connecting('connecting'),
  transportConnected('transport_connected'),
  captureReady('capture_ready'),
  transportUnavailable('transport_unavailable'),
  originRetired('origin_retired'),
  readyUnavailable('ready_unavailable'),
  invalidReady('invalid_ready'),
  closedBeforeReady('closed_before_ready'),
  closedAfterReady('closed_after_ready'),
  socketError('socket_error');

  const EllaCaptureSocketAttemptPhase(this.code);
  final String code;
}

/// One in-memory, content-free state for the current socket attempt only.
class EllaCaptureSocketAttempt {
  EllaCaptureSocketAttempt(this.phase, int? closeCode, DateTime at)
    : closeCode = closeCode != null && closeCode >= 1000 && closeCode <= 4999 ? closeCode : null,
      at = at.toUtc();

  final EllaCaptureSocketAttemptPhase phase;
  final int? closeCode;
  final DateTime at;

  EllaCaptureSocketAttemptStatus get status => switch (phase) {
    EllaCaptureSocketAttemptPhase.connecting ||
    EllaCaptureSocketAttemptPhase.transportConnected => EllaCaptureSocketAttemptStatus.pending,
    EllaCaptureSocketAttemptPhase.captureReady => EllaCaptureSocketAttemptStatus.ready,
    _ => EllaCaptureSocketAttemptStatus.failed,
  };
}

/// Content-free support metadata. Unknown reasons and invalid codes are omitted.
class EllaCaptureSocketFailure {
  const EllaCaptureSocketFailure(this.reason, this.closeCode, this.at);
  final EllaCaptureSocketFailureReason reason;
  final int? closeCode;
  final DateTime at;

  static EllaCaptureSocketFailure? fromReason(String reason, int? closeCode, DateTime at) {
    final matches = EllaCaptureSocketFailureReason.values.where((value) => value.code == reason);
    if (matches.isEmpty) return null;
    return EllaCaptureSocketFailure(
      matches.single,
      closeCode != null && closeCode >= 1000 && closeCode <= 4999 ? closeCode : null,
      at.toUtc(),
    );
  }
}

class EllaCaptureDiagnosticsSnapshot {
  const EllaCaptureDiagnosticsSnapshot.uninitialized()
    : initialized = false,
      ready = false,
      receivedBytes = null,
      sentBytes = null,
      lastFailure = null,
      lastAttempt = null;

  const EllaCaptureDiagnosticsSnapshot.upstream({
    required this.ready,
    required this.receivedBytes,
    required this.sentBytes,
    this.lastFailure,
    this.lastAttempt,
  }) : initialized = true;

  final bool initialized;
  final bool ready;
  final int? receivedBytes;
  final int? sentBytes;
  final EllaCaptureSocketFailure? lastFailure;
  final EllaCaptureSocketAttempt? lastAttempt;
}

class EllaCaptureHost {
  EllaCaptureHost._();

  /// Dart side of the single activation setting; generated from the xcconfig.
  static const bool upstreamCaptureDefine = bool.fromEnvironment('ELLA_UPSTREAM_CAPTURE_ENABLED');

  static Widget Function(BuildContext context)? _homeCaptureDockBuilder;

  static IDeviceService? _deviceService;

  static Future<EllaNativeDiscoveryDiagnostics> Function()? _nativeDiscoveryDiagnosticsLoader;
  static EllaCaptureDiagnosticsSnapshot Function()? _captureDiagnosticsReader;

  /// Reading this seam must never initialize capture or open a transport.
  static EllaCaptureDiagnosticsSnapshot get captureDiagnostics =>
      _captureDiagnosticsReader?.call() ?? const EllaCaptureDiagnosticsSnapshot.uninitialized();

  static void installCaptureDiagnosticsReader(EllaCaptureDiagnosticsSnapshot Function() reader) {
    _captureDiagnosticsReader = reader;
  }

  /// True only in the flag-ON graph after the upstream capture entry point
  /// installed itself. The legacy necklace auto-connect and legacy WAL/device
  /// service start are suppressed while true so exactly one capture stack owns
  /// the microphone, the BLE pendant, and the on-disk WAL directory.
  static bool get upstreamCaptureActive => _homeCaptureDockBuilder != null;

  static bool get legacyCaptureSuppressed => upstreamCaptureActive;

  /// Home capture dock supplied by the upstream capture graph, or null (legacy dock).
  static Widget Function(BuildContext context)? get homeCaptureDockBuilder => _homeCaptureDockBuilder;

  /// Flag-ON adapter that routes legacy picker/settings call sites through the
  /// same upstream hardware authority as the capture dock.
  static IDeviceService? get deviceService => _deviceService;

  /// Loader for cross-layer native BLE discovery diagnostics, supplied by the
  /// upstream capture graph, or null when that graph isn't active (flag OFF,
  /// or not yet booted) — [DeviceDiagnosticsPage] treats null as "unavailable".
  static Future<EllaNativeDiscoveryDiagnostics> Function()? get nativeDiscoveryDiagnosticsLoader =>
      _nativeDiscoveryDiagnosticsLoader;

  static void installUpstreamCapture({
    required Widget Function(BuildContext context) homeCaptureDockBuilder,
    IDeviceService? deviceService,
  }) {
    if (!upstreamCaptureDefine) {
      throw StateError('ELLA_UPSTREAM_CAPTURE_ENABLED is not set; refusing to install the upstream capture graph');
    }
    if (deviceService == null) {
      throw StateError('The upstream capture graph requires a shared device-service adapter');
    }
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
    _deviceService = deviceService;
  }

  static void installNativeDiscoveryDiagnosticsLoader(Future<EllaNativeDiscoveryDiagnostics> Function() loader) {
    _nativeDiscoveryDiagnosticsLoader = loader;
  }

  @visibleForTesting
  static void installForTesting({
    required Widget Function(BuildContext context) homeCaptureDockBuilder,
    IDeviceService? deviceService,
  }) {
    _homeCaptureDockBuilder = homeCaptureDockBuilder;
    _deviceService = deviceService;
  }

  @visibleForTesting
  static void resetForTesting() {
    _homeCaptureDockBuilder = null;
    _deviceService = null;
    _nativeDiscoveryDiagnosticsLoader = null;
    _captureDiagnosticsReader = null;
  }
}
