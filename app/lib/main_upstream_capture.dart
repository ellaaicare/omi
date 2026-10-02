import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_dock.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/ella/upstream_capture/ella_capture_memory_bridge.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_device_service_adapter.dart';
import 'package:omi/main.dart' as legacy;

/// Flag-ON entry point (ellaaicare/ella-ai#1280).
///
/// Built only when `ELLA_UPSTREAM_CAPTURE_ENABLED = YES` in
/// `ios/Flutter/EllaUpstreamCapture.xcconfig`: the iOS build script derives
/// `-t lib/main_upstream_capture.dart` and the
/// `--dart-define-from-file` JSON (ELLA_UPSTREAM_CAPTURE_ENABLED=true) from that
/// single value, and the same value compiles the vendored native hosts.
/// `lib/main.dart` (flag OFF) never imports this file or anything under
/// `lib/upstream_capture/` / `lib/ella/upstream_capture/`.
void main() {
  final runtime = EllaUpstreamCaptureRuntime.instance;
  // Refuses unless the Dart define generated from the xcconfig is true.
  EllaCaptureHost.installUpstreamCapture(
    homeCaptureDockBuilder: (context) => EllaCaptureMemoryBinding(
      bridge: runtime.memoryBridge,
      child: const EllaUpstreamCaptureDock(),
    ),
    deviceService: EllaUpstreamDeviceServiceAdapter.production(runtime),
  );
  // Device Diagnostics reads native BLE discovery diagnostics through this
  // seam instead of importing lib/upstream_capture/ directly, so the flag-OFF
  // graph never reaches it.
  EllaCaptureHost.installNativeDiscoveryDiagnosticsLoader(loadNativeDiscoveryDiagnostics);
  // Account transitions (sign-out / switch) revoke the bound capture session
  // through the existing Ella account-isolation hook before the next account.
  EllaAccountIsolationService.registerCaptureProducer(runtime.releaseAccount);
  legacy.main();
}
