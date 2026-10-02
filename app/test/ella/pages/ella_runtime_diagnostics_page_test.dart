import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/ella/pages/ella_runtime_diagnostics_page.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/services.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {
      // Process-global test services may already be initialized.
    }
  });

  setUp(() {
    SharedPreferences.setMockInitialValues(const {});
    EllaCaptureHost.resetForTesting();
  });
  tearDown(EllaCaptureHost.resetForTesting);

  test('socket diagnostics retain only fixed metadata, never arbitrary payloads', () {
    expect(EllaCaptureSocketFailure.fromReason('https://private.test?token=secret', 1008, DateTime.now()), isNull);
    final value = EllaCaptureSocketFailure.fromReason('capture_socket_error', 123456, DateTime.now())!;
    expect(value.reason, EllaCaptureSocketFailureReason.captureSocketError);
    expect(value.closeCode, isNull);
    expect(value.at.isUtc, isTrue);
  });

  for (final initialized in [false, true]) {
    testWidgets('flag ON diagnostics use the active snapshot, initialized=$initialized', (tester) async {
      final capture = _DiagnosticsCaptureProvider('connected');
      final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);
      EllaCaptureHost.installForTesting(homeCaptureDockBuilder: (_) => const SizedBox.shrink());
      var reads = 0;
      EllaCaptureHost.installCaptureDiagnosticsReader(() {
        reads++;
        return initialized
            ? EllaCaptureDiagnosticsSnapshot.upstream(
                ready: false,
                receivedBytes: 321,
                sentBytes: 123,
                lastFailure: EllaCaptureSocketFailure.fromReason(
                    'capture_socket_closed_before_ready', 1013, DateTime.utc(2026, 10, 2, 16, 44)),
              )
            : const EllaCaptureDiagnosticsSnapshot.uninitialized();
      });
      await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<CaptureProvider>.value(value: capture),
          ChangeNotifierProvider<DeviceProvider>.value(value: device),
        ],
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaRuntimeDiagnosticsPage(),
        ),
      ));
      await tester.pump();
      expect(reads, greaterThan(0));
      expect(capture.metricsListeners, 0);
      await tester.drag(find.byType(ListView), const Offset(0, -1000));
      await tester.pump();
      expect(find.textContaining('1.25 kbps'), findsNothing);
      if (initialized) {
        expect(find.textContaining('123 B'), findsOneWidget);
        expect(find.textContaining('capture_socket_closed_before_ready · 1013'), findsOneWidget);
      } else {
        expect(find.textContaining('Unknown B'), findsWidgets);
        expect(find.textContaining('0 B/s'), findsNothing);
      }
      await tester.pumpWidget(const SizedBox.shrink());
      capture.dispose();
      device.dispose();
    });
  }

  testWidgets('socket state uses localized values instead of raw storage tokens', (tester) async {
    for (final state in ['connected', 'disconnected', 'none']) {
      final capture = _DiagnosticsCaptureProvider(state);
      final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<CaptureProvider>.value(value: capture),
            ChangeNotifierProvider<DeviceProvider>.value(value: device),
          ],
          child: const MaterialApp(
            locale: Locale('ja'),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: EllaRuntimeDiagnosticsPage(),
          ),
        ),
      );
      await tester.pump();
      await tester.drag(find.byType(ListView), const Offset(0, -500));
      await tester.pump();

      final l10n = AppLocalizations.of(tester.element(find.byType(EllaRuntimeDiagnosticsPage)));
      final localizedState = switch (state) {
        'connected' => l10n.connected,
        'disconnected' => l10n.disconnected,
        _ => l10n.unknown,
      };

      expect(find.text('$localizedState · 1.25 kbps'), findsOneWidget);
      expect(find.text('$state · 1.25 kbps'), findsNothing);

      await tester.pumpWidget(const SizedBox.shrink());
      capture.dispose();
      device.dispose();
    }
  });
}

class _DiagnosticsCaptureProvider extends CaptureProvider {
  _DiagnosticsCaptureProvider(this.socketState);

  final String socketState;
  int metricsListeners = 0;

  @override
  String get transcriptionSocketState => socketState;

  @override
  double get wsSendRateKbps => 1.25;

  @override
  void addMetricsListener() => metricsListeners++;

  @override
  void removeMetricsListener() {}
}

class _NoopDeviceService implements IDeviceService {
  @override
  void start() {}

  @override
  Future<void> stop() async {}

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {}

  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async => null;

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {}

  @override
  void unsubscribe(Object context) {}

  @override
  DateTime? getFirstConnectedAt() => null;

  @override
  void setWifiSyncInProgress(bool value) {}

  @override
  Future<void> cancelPendingConnection() async {}

  @override
  Future<void> disconnectDevice() async {}
}
