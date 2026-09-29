import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/ella/pages/ella_runtime_diagnostics_page.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/device_diagnostics_page.dart';
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

  testWidgets('Device diagnostics entry navigates to DeviceDiagnosticsPage', (tester) async {
    final capture = _NoopCaptureProvider();
    final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider<CaptureProvider>.value(value: capture),
          ChangeNotifierProvider<DeviceProvider>.value(value: device),
        ],
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaRuntimeDiagnosticsPage(),
        ),
      ),
    );
    await tester.pump();

    final entryFinder = find.byKey(const Key('runtime-diagnostics-device-diagnostics-entry'));
    await tester.drag(find.byType(ListView), const Offset(0, -500));
    await tester.pump();
    expect(entryFinder, findsOneWidget);

    await tester.tap(entryFinder);
    await tester.pumpAndSettle();

    expect(find.byType(DeviceDiagnosticsPage), findsOneWidget);

    capture.dispose();
    device.dispose();
  });
}

class _NoopCaptureProvider extends CaptureProvider {
  @override
  String get transcriptionSocketState => 'disconnected';

  @override
  double get wsSendRateKbps => 0.0;

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
