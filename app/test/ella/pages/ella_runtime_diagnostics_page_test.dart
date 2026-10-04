import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/ella/pages/ella_runtime_diagnostics_page.dart';
import 'package:omi/ella/pages/ella_settings_page.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

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
    MemoryArtworkQueueDiagnostics.clear();
  });
  tearDown(EllaCaptureHost.resetForTesting);

  for (final width in [320.0, 390.0]) {
    testWidgets('ordinary artwork snapshot is read-only and scroll safe at 3x width=$width', (tester) async {
      tester.view.physicalSize = Size(width, width == 320 ? 568 : 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      var current = true;
      var requests = 0;
      final authority = _ReadAuthority(() => current);
      final api = MemoryArtworkApi(
        baseUrl: 'https://private-fixture.invalid',
        authorityProvider: () => authority,
        request:
            ({
              required url,
              required headers,
              required body,
              required method,
              timeout,
              retries,
              requireAuthCheck,
              expectedAuthenticatedUid,
              exactAuthority,
              onSendAttempt,
            }) async {
              requests++;
              expect(method, 'GET');
              return null;
            },
      );
      final ticket = MemoryArtworkQueueDiagnostics.begin(isCurrent: authority.isExactCurrent);
      await api.queueStatusWithDiagnostics(ticket);
      MemoryArtworkQueueDiagnostics.project(ticket, applied: false);
      final capture = _DiagnosticsCaptureProvider('disconnected');
      final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);
      EllaCaptureHost.installForTesting(homeCaptureDockBuilder: (_) => const SizedBox.shrink());
      ApiTransportDiagnostics.lastError = 'private-runtime-type https://private.invalid';
      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<CaptureProvider>.value(value: capture),
            ChangeNotifierProvider<DeviceProvider>.value(value: device),
          ],
          child: MaterialApp(
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(3)),
              child: child!,
            ),
            home: const EllaRuntimeDiagnosticsPage(),
          ),
        ),
      );
      await tester.pump();
      await tester.scrollUntilVisible(
        find.byKey(const Key('runtime-diagnostics-artwork-read')),
        180,
        scrollable: find
            .descendant(of: find.byKey(const Key('ella-runtime-diagnostics')), matching: find.byType(Scrollable))
            .first,
      );
      expect(find.textContaining('outcome=no_response'), findsOneWidget);
      expect(find.textContaining('home=failed'), findsOneWidget);
      expect(find.textContaining('private-'), findsNothing);
      expect(find.textContaining('https://'), findsNothing);
      await tester.pump(const Duration(seconds: 3));
      expect(requests, 1);
      expect(capture.metricsListeners, 0);
      current = false;
      await tester.pump(const Duration(seconds: 1));
      expect(find.textContaining('outcome=no_response'), findsNothing);
      expect(MemoryArtworkQueueDiagnostics.latest, isNull);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      ApiTransportDiagnostics.lastError = '';
      capture.dispose();
      device.dispose();
    });
  }

  testWidgets('upstream Settings opens ordinary diagnostics without advanced controls', (tester) async {
    final capture = _DiagnosticsCaptureProvider('disconnected');
    final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);
    EllaCaptureHost.installForTesting(homeCaptureDockBuilder: (_) => const SizedBox.shrink());
    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider<CaptureProvider>.value(value: capture),
          ChangeNotifierProvider<DeviceProvider>.value(value: device),
          ChangeNotifierProvider(create: (_) => UserProvider()),
        ],
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaSettingsPage(runtimeSideEffectsEnabled: false, authenticatedUidOverride: ''),
        ),
      ),
    );
    await tester.pump();
    final entry = find.byKey(const Key('ella-runtime-diagnostics-entry'));
    await tester.scrollUntilVisible(entry, 180);
    await tester.tap(entry);
    await tester.pumpAndSettle();
    expect(find.byType(EllaRuntimeDiagnosticsPage), findsOneWidget);
    expect(find.text('Advanced settings'), findsNothing);
    expect(capture.metricsListeners, 0);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    capture.dispose();
    device.dispose();
  });

  testWidgets('ordinary runtime diagnostics never render arbitrary lease strings', (tester) async {
    final capture = _DiagnosticsCaptureProvider('disconnected');
    final device = DeviceProvider(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);
    AiConsentActiveSessionLease.diagnostics.value = const AiConsentLeaseDiagnostics(
      phase: AiConsentLeasePhase.retrying,
      supportCode: 'private-token https://private.invalid?secret=fixture',
      terminalReason: 'private-owner',
    );
    ConnectivityService().applyHealthProbeForTest(statusCode: 17);
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
    expect(find.textContaining('private-'), findsNothing);
    expect(find.textContaining('https://'), findsNothing);
    expect(find.textContaining('Connected · 17'), findsNothing);
    expect(find.textContaining('Connected · Unknown'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    AiConsentActiveSessionLease.diagnostics.value = const AiConsentLeaseDiagnostics();
    ConnectivityService().applyHealthProbeForTest(statusCode: 200);
    capture.dispose();
    device.dispose();
  });

  test('socket diagnostics retain only fixed metadata, never arbitrary payloads', () {
    expect(EllaCaptureSocketFailure.fromReason('https://private.test?token=secret', 1008, DateTime.now()), isNull);
    final value = EllaCaptureSocketFailure.fromReason('capture_socket_error', 123456, DateTime.now())!;
    expect(value.reason, EllaCaptureSocketFailureReason.captureSocketError);
    expect(value.closeCode, isNull);
    expect(value.at.isUtc, isTrue);
    final attempt = EllaCaptureSocketAttempt(
      EllaCaptureSocketAttemptPhase.closedBeforeReady,
      6000,
      DateTime.parse('2026-10-02T09:44:00-07:00'),
    );
    expect(attempt.phase.code, 'closed_before_ready');
    expect(attempt.status, EllaCaptureSocketAttemptStatus.failed);
    expect(attempt.closeCode, isNull);
    expect(attempt.at.toIso8601String(), '2026-10-02T16:44:00.000Z');
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
                  'capture_socket_closed_before_ready',
                  1013,
                  DateTime.utc(2026, 10, 2, 16, 44),
                ),
                lastAttempt: EllaCaptureSocketAttempt(
                  EllaCaptureSocketAttemptPhase.closedBeforeReady,
                  1013,
                  DateTime.parse('2026-10-02T09:44:00-07:00'),
                ),
              )
            : const EllaCaptureDiagnosticsSnapshot.uninitialized();
      });
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
      expect(reads, greaterThan(0));
      expect(capture.metricsListeners, 0);
      await tester.drag(find.byType(ListView), const Offset(0, -1000));
      await tester.pump();
      expect(find.textContaining('1.25 kbps'), findsNothing);
      if (initialized) {
        expect(find.textContaining('123 B'), findsOneWidget);
        expect(find.textContaining('closed_before_ready · failed · 1013'), findsOneWidget);
        expect(find.textContaining('2026-10-02T16:44:00.000Z'), findsOneWidget);
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

class _ReadAuthority implements ExactAccountAuthorityVerifier {
  _ReadAuthority(this.current);
  final bool Function() current;
  @override
  String get uid => 'private-fixture-owner';
  @override
  bool isExactCurrent() => current();
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
