import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/ella/pages/ella_runtime_diagnostics_page.dart';
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

  setUp(() => SharedPreferences.setMockInitialValues(const {}));

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

  @override
  String get transcriptionSocketState => socketState;

  @override
  double get wsSendRateKbps => 1.25;

  @override
  void addMetricsListener() {}

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
