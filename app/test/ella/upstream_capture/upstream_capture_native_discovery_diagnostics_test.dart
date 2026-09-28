// ellaaicare/ella-ai#1280 RUN-010, review finding DISCOVERY-DIAGNOSTICS-001: the
// in-app "Device diagnostics" view must surface the native, bridge, and Dart
// layers of BLE discovery together, so a single copy/paste is enough to
// diagnose a one-run discovery failure across all three. This exercises that
// cross-layer surface with a fake BleHostApi standing in for the native
// Pigeon host (wired through the same `loadNativeDiscoveryDiagnostics`
// mapping production uses) — no real CoreBluetooth involved, and the page
// itself never imports lib/upstream_capture/ (see device_diagnostics_page.dart).
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/main.dart';
import 'package:omi/pages/settings/device_diagnostics_page.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/utils/debug_log_manager.dart';

class _FakeBleHostApi extends BleHostApi {
  _FakeBleHostApi(this.diagnostics);

  final BleNativeDiscoveryDiagnostics diagnostics;

  @override
  Future<BleNativeDiscoveryDiagnostics> getNativeDiscoveryDiagnostics() async => diagnostics;
}

class _FailingBleHostApi extends BleHostApi {
  @override
  Future<BleNativeDiscoveryDiagnostics> getNativeDiscoveryDiagnostics() async {
    throw PlatformException(code: 'channel-error');
  }
}

Widget _buildTestApp({Future<EllaNativeDiscoveryDiagnostics> Function()? nativeDiagnosticsLoader}) {
  return MaterialApp(
    navigatorKey: MyApp.navigatorKey,
    localizationsDelegates: const [
      AppLocalizations.delegate,
      GlobalMaterialLocalizations.delegate,
      GlobalWidgetsLocalizations.delegate,
      GlobalCupertinoLocalizations.delegate,
    ],
    supportedLocales: AppLocalizations.supportedLocales,
    home: DeviceDiagnosticsPage(nativeDiagnosticsLoader: nativeDiagnosticsLoader),
  );
}

void main() {
  setUp(() {
    DebugLogManager.resetDeviceDiagnostics();
    EllaCaptureHost.resetForTesting();
  });
  tearDown(() {
    DebugLogManager.resetDeviceDiagnostics();
    EllaCaptureHost.resetForTesting();
  });

  testWidgets('surfaces native, bridge, and Dart discovery diagnostics together, including on copy', (tester) async {
    final native = BleNativeDiscoveryDiagnostics(
      lastStartScanCbState: 'on',
      scansStartedImmediately: 2,
      scansQueued: 1,
      queuedScansFired: 1,
      didDiscoverCount: 5,
      flutterApiNilDropCount: 3,
    );
    final fakeHost = _FakeBleHostApi(native);
    DebugLogManager.bleFlutterApiSetUpAtMs = DateTime.utc(2026, 1, 2, 3, 4, 5).millisecondsSinceEpoch;
    DebugLogManager.deviceScansStarted = 2;
    DebugLogManager.deviceScansStopped = 1;
    DebugLogManager.deviceCandidatesSeen = 5;
    DebugLogManager.deviceCandidatesAdmitted = 1;
    DebugLogManager.recordCandidateRejected('no_name');

    await tester.pumpWidget(_buildTestApp(nativeDiagnosticsLoader: () => loadNativeDiscoveryDiagnostics(fakeHost)));
    await tester.pumpAndSettle();

    // Native layer: CB state, started-vs-queued, didDiscover count, the
    // flutterApi-nil drop count that would otherwise explain a silent drop.
    expect(find.textContaining('cbStateAtLastStartScan=on'), findsOneWidget);
    expect(find.textContaining('scansStartedImmediately=2'), findsOneWidget);
    expect(find.textContaining('scansQueued=1'), findsOneWidget);
    expect(find.textContaining('queuedScansFired=1'), findsOneWidget);
    expect(find.textContaining('didDiscoverCount=5'), findsOneWidget);
    expect(find.textContaining('flutterApiNilDropCount=3'), findsOneWidget);

    // Bridge layer: when BleFlutterApi.setUp(BleBridge.instance) ran.
    expect(find.textContaining('2026-01-02T03:04:05'), findsOneWidget);

    // Dart layer: the existing received/admitted/rejected-by-reason counters.
    expect(find.textContaining('candidatesSeen=5 candidatesAdmitted=1'), findsOneWidget);
    expect(find.textContaining('no_name=1'), findsOneWidget);

    // The Copy button's exported text must include all three layers in one payload.
    final clipboardCalls = <MethodCall>[];
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') clipboardCalls.add(call);
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null));

    await tester.tap(find.text('Copy Diagnostics'));
    await tester.pumpAndSettle();

    expect(clipboardCalls, hasLength(1));
    final copied = (clipboardCalls.single.arguments as Map)['text'] as String;
    expect(copied, contains('cbStateAtLastStartScan=on'));
    expect(copied, contains('flutterApiNilDropCount=3'));
    expect(copied, contains('2026-01-02T03:04:05'));
    expect(copied, contains('candidatesSeen=5'));
  });

  testWidgets('shows the native layer as unavailable and the bridge layer as never set up when the host call fails',
      (tester) async {
    await tester.pumpWidget(
      _buildTestApp(nativeDiagnosticsLoader: () => loadNativeDiscoveryDiagnostics(_FailingBleHostApi())),
    );
    await tester.pumpAndSettle();

    expect(find.textContaining('native: unavailable'), findsOneWidget);
    expect(find.textContaining('bridge: setUpAt=never'), findsOneWidget);
  });

  testWidgets('shows the native layer as unavailable when the upstream capture graph never installed a loader',
      (tester) async {
    await tester.pumpWidget(_buildTestApp());
    await tester.pumpAndSettle();

    expect(find.textContaining('native: unavailable'), findsOneWidget);
  });
}
