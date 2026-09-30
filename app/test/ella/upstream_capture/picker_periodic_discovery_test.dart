import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart' as legacy;
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_device_service_adapter.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/capture/connect.dart';
import 'package:omi/pages/onboarding/find_device/page.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart' as upstream;
import 'package:omi/upstream_capture/services/devices.dart' as upstream_service;
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart' as upstream_connection;

final _friend = legacy.BtDevice(name: 'Friend', id: 'friend-test', type: legacy.DeviceType.omi, rssi: -30);
final _compass = legacy.BtDevice(name: 'Compass', id: 'compass-test', type: legacy.DeviceType.fieldy, rssi: -40);

class _ScriptedService implements IDeviceService {
  final Map<Object, IDeviceServiceSubsciption> listeners = {};
  final List<IDeviceServiceSubsciption> retired = [];
  List<List<legacy.BtDevice>> nextChunks = [];
  Completer<void>? gate;
  int discovers = 0;
  int concurrent = 0;
  int maxConcurrent = 0;
  int cancels = 0;
  int connects = 0;
  bool failDiscovery = false;
  DeviceServiceStatus status = DeviceServiceStatus.ready;

  @override
  void start() {}
  @override
  Future<void> stop() async {
    status = DeviceServiceStatus.stop;
    for (final listener in listeners.values.toList()) {
      listener.onStatusChanged(status);
    }
  }

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    expectSync(desirableDeviceId, isNull);
    expectSync(timeout, 5);
    discovers++;
    concurrent++;
    maxConcurrent = concurrent > maxConcurrent ? concurrent : maxConcurrent;
    final recipients = listeners.values.toList();
    final chunks = nextChunks;
    await gate?.future;
    if (failDiscovery) {
      concurrent--;
      throw StateError('synthetic discovery failure');
    }
    for (final chunk in chunks) {
      for (final listener in recipients) {
        listener.onDevices(chunk);
      }
    }
    concurrent--;
  }

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {
    listeners[context] = subscription;
    subscription.onStatusChanged(status);
  }

  @override
  void unsubscribe(Object context) {
    final listener = listeners.remove(context);
    if (listener != null) retired.add(listener);
  }

  @override
  Future<void> cancelPendingConnection() async => cancels++;
  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    connects++;
    return null;
  }

  @override
  DateTime? getFirstConnectedAt() => null;
  @override
  void setWifiSyncInProgress(bool value) {}
  @override
  Future<void> disconnectDevice() async {}
}

class _PickerDeviceProvider extends DeviceProvider {
  _PickerDeviceProvider(IDeviceService service) : super(deviceService: service, automaticallyReconnectOnReady: false);
  Completer<void>? prepareGate;
  Completer<bool>? connectionGate;
  int prepares = 0;
  int connects = 0;

  @override
  Future<void> prepareForExplicitDeviceSelection() async {
    prepares++;
    await prepareGate?.future;
  }

  @override
  Future<bool> connectDeviceForCurrentUser(legacy.BtDevice device, {bool requireFreshSession = false}) async {
    connects++;
    return await connectionGate?.future ?? false;
  }
}

class _PermissionProvider extends OnboardingProvider {
  _PermissionProvider(IDeviceService service, this.permissionGate) : super(deviceService: service);
  final Completer<bool> permissionGate;
  int permissionRequests = 0;

  @override
  Future<void> askForBluetoothPermissions() async {
    permissionRequests++;
    updateBluetoothPermission(await permissionGate.future);
  }
}

class _Harness {
  _Harness(this.service, {OnboardingProvider? provider}) {
    device = _PickerDeviceProvider(service);
    onboarding = (provider ?? OnboardingProvider(deviceService: service))..setDeviceProvider(device);
    if (provider == null) onboarding.hasBluetoothPermission = true;
  }
  final IDeviceService service;
  late final _PickerDeviceProvider device;
  late final OnboardingProvider onboarding;
  final navigator = GlobalKey<NavigatorState>();

  Future<void> mount(WidgetTester tester, {bool connectPage = false, bool Function()? canScan}) async {
    await tester.pumpWidget(ChangeNotifierProvider<OnboardingProvider>.value(
      value: onboarding,
      child: MaterialApp(
        navigatorKey: navigator,
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: connectPage
            ? const ConnectDevicePage(originUid: 'owner-test', authenticatedUid: _testUid)
            : Scaffold(
                body: SingleChildScrollView(
                  child: FindDevicesPage(goNext: () {}, includeSkip: false, canConnect: canScan),
                ),
              ),
      ),
    ));
    await tester.pump();
  }

  Future<void> leave(WidgetTester tester) async {
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  }

  void dispose() {
    onboarding.dispose();
    device.dispose();
  }
}

String _testUid() => 'owner-test';

class _UpstreamPasses extends upstream_service.DeviceService {
  int discovers = 0;
  int connects = 0;
  List<upstream.BtDevice> current = [];

  @override
  List<upstream.BtDevice> get devices => current;
  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    expectSync(desirableDeviceId, isNull);
    expectSync(timeout, 5);
    discovers++;
    await Future<void>.delayed(const Duration(seconds: 5));
    current = [
      upstream.BtDevice(name: 'Friend', id: 'friend-test', type: upstream.DeviceType.omi, rssi: -30),
      if (discovers >= 2)
        upstream.BtDevice(name: 'Compass', id: 'compass-test', type: upstream.DeviceType.fieldy, rssi: -40),
    ];
    onDevices(current);
  }

  @override
  Future<void> stopDiscoverers() async {}
  @override
  Future<upstream_connection.DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    connects++;
    return null;
  }
}

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'owner-test', 'btDeviceOwnerBinding': 'owner-test'});
    await SharedPreferencesUtil.init();
  });

  testWidgets('failed pass stops searching; explicit retry performs one read-only pass and recovers', (tester) async {
    final service = _ScriptedService()..failDiscovery = true;
    final harness = _Harness(service);
    await harness.mount(tester);
    expect(harness.onboarding.discoveryFailed, isTrue);
    expect(find.textContaining('Searching'), findsNothing);
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 1);
    service.failDiscovery = false;
    service.nextChunks = [
      [_compass]
    ];
    await tester.tap(find.widgetWithIcon(TextButton, Icons.refresh_rounded));
    await tester.pump();
    await tester.pump();
    expect(service.discovers, 2);
    expect(harness.onboarding.discoveryFailed, isFalse);
    expect(find.text('Compass'), findsOneWidget);
    expect(service.connects, 0);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('two production picker routes cancel only their lease and resume uncovered route', (tester) async {
    final service = _ScriptedService()
      ..nextChunks = [
        [_friend]
      ];
    final harness = _Harness(service);
    await harness.mount(tester);
    harness.navigator.currentState!.push(MaterialPageRoute<void>(
      builder: (_) => Scaffold(body: SingleChildScrollView(child: FindDevicesPage(goNext: () {}, includeSkip: false))),
    ));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(service.discovers, 2);
    await tester.pump(const Duration(seconds: 10));
    expect(service.discovers, 3);
    harness.navigator.currentState!.pop();
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(service.discovers, 4);
    await tester.pump(const Duration(seconds: 10));
    expect(service.discovers, 5);
    expect(service.maxConcurrent, 1);
    expect(service.connects, 0);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('popping production picker during active scan rejects completion and restores older route',
      (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester);
    final gate = service.gate = Completer<void>();
    service.nextChunks = [
      [_friend]
    ];
    harness.navigator.currentState!.push(MaterialPageRoute<void>(
      builder: (_) => Scaffold(body: SingleChildScrollView(child: FindDevicesPage(goNext: () {}, includeSkip: false))),
    ));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(service.discovers, 2);
    harness.navigator.currentState!.pop();
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    service.gate = null;
    service.nextChunks = [
      [_compass]
    ];
    gate.complete();
    await tester.pump();
    await tester.pump();
    expect(service.discovers, 3);
    expect(harness.onboarding.deviceList.map((device) => device.name), ['Compass']);
    expect(service.maxConcurrent, 1);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('delayed permission completion after production route pop cannot revive retired picker', (tester) async {
    final service = _ScriptedService();
    final permission = Completer<bool>();
    final provider = _PermissionProvider(service, permission);
    final harness = _Harness(service, provider: provider);
    await harness.mount(tester);
    harness.navigator.currentState!.push(MaterialPageRoute<void>(builder: (_) => const Scaffold()));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    permission.complete(true);
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 0);
    harness.navigator.currentState!.pop();
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(service.discovers, 1);
    expect(provider.permissionRequests, 1);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('popped picker permission completes only for current older route, with one request', (tester) async {
    final service = _ScriptedService();
    final permission = Completer<bool>();
    final provider = _PermissionProvider(service, permission)..hasBluetoothPermission = true;
    final harness = _Harness(service, provider: provider);
    await harness.mount(tester);
    expect(service.discovers, 1);
    provider.hasBluetoothPermission = false;
    harness.navigator.currentState!.push(MaterialPageRoute<void>(
      builder: (_) => Scaffold(body: SingleChildScrollView(child: FindDevicesPage(goNext: () {}, includeSkip: false))),
    ));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(provider.permissionRequests, 1);
    harness.navigator.currentState!.pop();
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    service.nextChunks = [
      [_compass]
    ];
    permission.complete(true);
    await tester.pump();
    await tester.pump();
    expect(provider.permissionRequests, 1);
    expect(service.discovers, 2);
    expect(provider.deviceList.map((device) => device.name), ['Compass']);
    expect(service.listeners.length, 2);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('production Connect finds Compass on later upstream pass, at 10s cadence, without connecting',
      (tester) async {
    final upstreamService = _UpstreamPasses();
    var connects = 0;
    final adapter = EllaUpstreamDeviceServiceAdapter(
      serviceLoader: () async => upstreamService,
      connect: (_, __) async {
        connects++;
        return false;
      },
      disconnect: (_) async {},
    )..start();
    final harness = _Harness(adapter);
    await harness.mount(tester, connectPage: true);
    expect(upstreamService.discovers, 1);
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(find.text('Friend'), findsOneWidget);
    expect(find.text('Compass'), findsNothing);
    await tester.pump(const Duration(seconds: 4));
    expect(upstreamService.discovers, 1);
    await tester.pump(const Duration(seconds: 1));
    expect(upstreamService.discovers, 2);
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(find.text('Compass'), findsOneWidget);
    expect(harness.onboarding.deviceList.last.type, legacy.DeviceType.fieldy);
    expect(connects, 0);
    expect(upstreamService.connects, 0);
    await harness.leave(tester);
    await tester.pump(const Duration(seconds: 30));
    expect(upstreamService.discovers, 2);
    harness.dispose();
    await adapter.stop();
  });

  testWidgets('single in-flight pass skips cadence ticks and coalesces repeated start', (tester) async {
    final service = _ScriptedService()..gate = Completer<void>();
    final harness = _Harness(service);
    await harness.mount(tester);
    final owner = tester.state(find.byType(FindDevicesPage));
    final initial = harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner);
    await tester.pump(const Duration(seconds: 35));
    expect(service.discovers, 1);
    service.gate!.complete();
    await tester.pump();
    await initial;
    await tester.pump(const Duration(seconds: 5));
    expect(service.discovers, 2);
    expect(service.maxConcurrent, 1);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('completed empty pass clears stale list, partial chunks preserve valid candidates', (tester) async {
    final service = _ScriptedService()
      ..nextChunks = [
        [_friend],
        [],
        [_compass],
      ];
    final harness = _Harness(service);
    await harness.mount(tester);
    expect(harness.onboarding.deviceList.map((d) => d.name), ['Friend', 'Compass']);
    service.nextChunks = [[]];
    await tester.pump(const Duration(seconds: 10));
    await tester.pump();
    expect(harness.onboarding.deviceList, isEmpty);
    expect(harness.onboarding.foundDevicesMap, isEmpty);
    expect(find.text('Compass'), findsNothing);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('route exit and reentry wait for retired scan and reject its callbacks', (tester) async {
    final oldGate = Completer<void>();
    final service = _ScriptedService()
      ..gate = oldGate
      ..nextChunks = [
        [_friend]
      ];
    final harness = _Harness(service);
    await harness.mount(tester);
    expect(service.discovers, 1);
    await harness.leave(tester);
    service.gate = null;
    service.nextChunks = [
      [_compass]
    ];
    await harness.mount(tester);
    expect(service.discovers, 1);
    for (final callback in service.retired) {
      callback.onDevices([_friend]);
      callback.onStatusChanged(DeviceServiceStatus.stop);
    }
    expect(harness.onboarding.deviceList, isEmpty);
    oldGate.complete();
    await tester.pump();
    await tester.pump();
    expect(service.discovers, 2);
    expect(harness.onboarding.deviceList.map((d) => d.name), ['Compass']);
    expect(service.maxConcurrent, 1);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('old route disposal cannot cancel replacement picker lease', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester);
    final owner = Object();
    await harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner);
    await harness.leave(tester);
    await tester.pump(const Duration(seconds: 10));
    expect(service.discovers, 3);
    await harness.onboarding.cancelDeviceDiscovery(owner: owner);
    harness.dispose();
  });

  for (final dispose in [false, true]) {
    testWidgets('delayed permission after ${dispose ? 'dispose' : 'route exit'} never starts/subscribes',
        (tester) async {
      final service = _ScriptedService();
      final permission = Completer<bool>();
      final provider = _PermissionProvider(service, permission);
      final harness = _Harness(service, provider: provider);
      await harness.mount(tester);
      expect(provider.permissionRequests, 1);
      await harness.leave(tester);
      if (dispose) harness.dispose();
      permission.complete(true);
      await tester.pump();
      await tester.pump(const Duration(seconds: 30));
      expect(service.discovers, 0);
      expect(service.listeners.keys.where((key) => !identical(key, harness.device)), isEmpty);
      expect(tester.takeException(), isNull);
      if (!dispose) harness.dispose();
    });
  }

  testWidgets('permission denial prompts once and never repeats from cadence', (tester) async {
    final service = _ScriptedService();
    final permission = Completer<bool>();
    final provider = _PermissionProvider(service, permission);
    final harness = _Harness(service, provider: provider);
    var prompts = 0;
    final initial = provider.scanDevices(onShowDialog: () => prompts++);
    await tester.pump();
    permission.complete(false);
    await tester.pump();
    await initial;
    await tester.pump(const Duration(minutes: 2));
    expect(prompts, 1);
    expect(provider.permissionRequests, 1);
    expect(service.discovers, 0);
    expect(provider.discoveryFailed, isTrue);
    harness.dispose();
  });

  testWidgets('exit during explicit-selection preparation cannot subscribe late', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    final gate = harness.device.prepareGate = Completer<void>();
    await harness.mount(tester);
    await harness.leave(tester);
    gate.complete();
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 0);
    expect(service.listeners.keys.where((key) => !identical(key, harness.device)), isEmpty);
    harness.dispose();
  });

  testWidgets('selection retires delayed preparation without a late subscription or scan', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    final gate = harness.device.prepareGate = Completer<void>();
    await harness.mount(tester);
    final selection = harness.onboarding.handleTap(device: _friend, isFromOnboarding: false);
    await tester.pump();
    expect(await selection, DeviceSelectionOutcome.unavailable);
    gate.complete();
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 0);
    expect(service.listeners.keys.where((key) => !identical(key, harness.device)), isEmpty);
    await harness.leave(tester);
    harness.dispose();
  });

  for (final reason in ['authority', 'shutdown', 'origin']) {
    testWidgets('$reason invalidation stops cadence and rejects delayed results', (tester) async {
      final service = _ScriptedService()
        ..nextChunks = [
          [_friend]
        ]
        ..gate = Completer<void>();
      final harness = _Harness(service);
      var current = true;
      await harness.mount(tester, canScan: () => current);
      if (reason == 'authority') {
        SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
      } else if (reason == 'shutdown') {
        await service.stop();
      } else {
        current = false;
      }
      service.gate!.complete();
      await tester.pump();
      await tester.pump(const Duration(seconds: 30));
      expect(service.discovers, 1);
      expect(harness.onboarding.deviceList, isEmpty);
      expect(service.listeners.keys.where((key) => !identical(key, harness.device)), isEmpty);
      await harness.leave(tester);
      harness.dispose();
    });
  }

  testWidgets('explicit selection stops scans; exit fences delayed selection without resuming', (tester) async {
    final service = _ScriptedService()
      ..nextChunks = [
        [_compass]
      ];
    final harness = _Harness(service);
    final gate = harness.device.connectionGate = Completer<bool>();
    await harness.mount(tester);
    final selection = harness.onboarding.handleTap(device: _compass, isFromOnboarding: false);
    await tester.pump();
    expect(harness.device.connects, 1);
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 1);
    final cancels = service.cancels;
    await harness.leave(tester);
    expect(service.cancels, greaterThan(cancels));
    gate.complete(false);
    await tester.pump();
    expect(await selection, DeviceSelectionOutcome.cancelled);
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 1);
    harness.dispose();
  });

  testWidgets('selection during active scan ignores its late candidates and never restarts cadence', (tester) async {
    final service = _ScriptedService()
      ..gate = Completer<void>()
      ..nextChunks = [
        [_friend]
      ];
    final harness = _Harness(service);
    await harness.mount(tester);
    final selection = harness.onboarding.handleTap(device: _compass, isFromOnboarding: false);
    await tester.pump();
    expect(await selection, DeviceSelectionOutcome.unavailable);
    service.gate!.complete();
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));
    expect(harness.onboarding.deviceList, isEmpty);
    expect(harness.device.connects, 1);
    expect(service.discovers, 1);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('active picker continues upstream cadence beyond a physical 90-second test window', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester);
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(seconds: 10));
      await tester.pump();
    }
    expect(service.discovers, 11);
    expect(service.connects, 0);
    await harness.leave(tester);
    await tester.pump(const Duration(minutes: 2));
    expect(service.discovers, 11);
    harness.dispose();
  });
}
