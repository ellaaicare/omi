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
import 'package:omi/pages/onboarding/ella/ella_connect.dart';
import 'package:omi/pages/onboarding/find_device/page.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/devices/discovery/device_discoverer.dart';
import 'package:omi/services/devices/omi_connection.dart';
import 'package:omi/services/devices/transports/device_transport.dart';
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

class _PickerTransport implements DeviceTransport {
  _PickerTransport(this.deviceId);
  @override
  final String deviceId;
  final states = StreamController<DeviceTransportState>.broadcast(sync: true);
  bool connected = false;
  int disconnects = 0;
  @override
  Stream<DeviceTransportState> get connectionStateStream => states.stream;
  @override
  Future<void> connect() async {
    connected = true;
    states.add(DeviceTransportState.connected);
  }

  @override
  Future<void> disconnect() async {
    disconnects++;
    connected = false;
    states.add(DeviceTransportState.disconnected);
  }

  @override
  Future<bool> isConnected() async => connected;
  @override
  Future<bool> ping() async => connected;
  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) => const Stream.empty();
  @override
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async =>
      getCharacteristicStream(serviceUuid, characteristicUuid);
  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) async => const [];
  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) async {}
  @override
  Future<void> dispose() => states.close();
}

class _ImmediatePickerDiscoverer extends DeviceDiscoverer {
  @override
  bool get isSupported => true;
  @override
  String get name => 'picker-fixture';
  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) async => DeviceDiscoveryResult(devices: [_friend]);
  @override
  Future<void> stop() async {}
}

class _SuccessfulPickerDeviceProvider extends _PickerDeviceProvider {
  _SuccessfulPickerDeviceProvider(this.service) : super(service);
  final IDeviceService service;
  @override
  Future<bool> connectDeviceForCurrentUser(legacy.BtDevice device, {bool requireFreshSession = false}) async {
    connects++;
    final authoritative = service;
    final selected = authoritative is IAuthoritativeDeviceService
        ? (await (authoritative as IAuthoritativeDeviceService).connectForCurrentUser('owner-test', device))?.device
        : (await service.ensureConnection(device.id, force: true))?.device;
    if (selected == null) return false;
    connectedDevice = selected;
    pairedDevice = selected;
    setIsConnected(true);
    return true;
  }
}

class _PickerHomeProvider extends HomeProvider {
  @override
  Future<void> setupHasSpeakerProfile() async {}
}

class _Harness {
  _Harness(this.service, {OnboardingProvider? provider, _PickerDeviceProvider? deviceProvider}) {
    device = deviceProvider ?? _PickerDeviceProvider(service);
    onboarding = (provider ?? OnboardingProvider(deviceService: service))..setDeviceProvider(device);
    if (provider == null) onboarding.hasBluetoothPermission = true;
  }
  final IDeviceService service;
  late final _PickerDeviceProvider device;
  late final OnboardingProvider onboarding;
  final navigator = GlobalKey<NavigatorState>();

  Future<void> mount(WidgetTester tester,
      {bool connectPage = false,
      bool legacyConnect = false,
      bool isFromOnboarding = false,
      VoidCallback? goNext,
      bool Function()? canScan}) async {
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<OnboardingProvider>.value(value: onboarding),
        ChangeNotifierProvider<HomeProvider>(create: (_) => _PickerHomeProvider()),
      ],
      child: MaterialApp(
        navigatorKey: navigator,
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: legacyConnect
            ? EllaConnect(onNext: () {}, onSkip: () {}, onBack: () {})
            : connectPage
                ? const ConnectDevicePage(originUid: 'owner-test', authenticatedUid: _testUid)
                : Scaffold(
                    body: SingleChildScrollView(
                      child: FindDevicesPage(
                        goNext: goNext ?? () {},
                        includeSkip: false,
                        isFromOnboarding: isFromOnboarding,
                        canConnect: canScan,
                      ),
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

  for (final upstreamEnabled in [false, true]) {
    testWidgets('successful ${upstreamEnabled ? 'upstream' : 'legacy'} selection survives goNext route disposal',
        (tester) async {
      final transport = _PickerTransport(_friend.id);
      final legacyService = DeviceService(
        discoverers: [_ImmediatePickerDiscoverer()],
        connectionCreator: (device) => OmiDeviceConnection(device, transport),
      )..start();
      final upstreamService = _UpstreamPasses();
      var upstreamConnected = false;
      var disconnects = 0;
      final adapter = EllaUpstreamDeviceServiceAdapter(
        serviceLoader: () async => upstreamService,
        connect: (_, __) async => upstreamConnected = true,
        disconnect: (_) async {
          disconnects++;
          upstreamConnected = false;
        },
      )..start();
      final IDeviceService service = upstreamEnabled ? adapter : legacyService;
      final harness = _Harness(service, deviceProvider: _SuccessfulPickerDeviceProvider(service));
      var advanced = false;
      await harness.mount(tester, isFromOnboarding: true, goNext: () {
        advanced = true;
        harness.navigator.currentState!.pushReplacement(MaterialPageRoute<void>(builder: (_) => const Scaffold()));
      });
      if (upstreamEnabled) {
        await tester.pump(const Duration(seconds: 5));
        await tester.pump();
      }
      await tester.ensureVisible(find.text('Friend'));
      await tester.tap(find.text('Friend'));
      await tester.pump();
      expect(harness.device.presentationIsConnected, isTrue);
      await tester.pump(const Duration(seconds: 2));
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(advanced, isTrue);
      expect(find.byType(FindDevicesPage), findsNothing);
      expect(harness.device.presentationIsConnected, isTrue);
      if (upstreamEnabled) {
        expect(upstreamConnected, isTrue);
        expect(disconnects, 0);
        expect(adapter.ownerBindingForConnection(_friend.id), 'owner-test');
      } else {
        expect(transport.connected, isTrue);
        expect(transport.disconnects, 0);
        expect(await legacyService.ensureConnection(_friend.id), isNotNull);
      }
      expect(tester.takeException(), isNull);
      await harness.leave(tester);
      harness.onboarding.dispose();
      if (!upstreamEnabled) {
        expect(transport.connected, isTrue, reason: 'provider disposal no longer owns this connection');
      }
      harness.device.dispose();
      await adapter.stop();
      await legacyService.disconnectDevice();
      await transport.dispose();
    });
  }

  testWidgets('abandoned legacy selection still cancels its connection before successful handoff', (tester) async {
    final transport = _PickerTransport(_friend.id);
    final service = DeviceService(
      discoverers: [_ImmediatePickerDiscoverer()],
      connectionCreator: (device) => OmiDeviceConnection(device, transport),
    )..start();
    final harness = _Harness(service, deviceProvider: _SuccessfulPickerDeviceProvider(service));
    await harness.mount(tester);
    final selection = harness.onboarding.handleTap(device: _friend, isFromOnboarding: true, goNext: () {});
    await tester.pump();
    expect(transport.connected, isTrue);
    await harness.leave(tester);
    expect(transport.connected, isFalse);
    expect(transport.disconnects, 1);
    await tester.pump(const Duration(seconds: 2));
    expect(await selection, DeviceSelectionOutcome.cancelled);
    harness.dispose();
    await transport.dispose();
  });

  testWidgets('covered EllaConnect cannot auto-select candidates owned by production FindDevices', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester, legacyConnect: true);
    service.nextChunks = [
      [_friend]
    ];
    harness.navigator.currentState!.push(MaterialPageRoute<void>(
      builder: (_) => Scaffold(body: SingleChildScrollView(child: FindDevicesPage(goNext: () {}, includeSkip: false))),
    ));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.text('Friend'), findsOneWidget);
    expect(harness.device.connects, 0);
    expect(service.discovers, 2);
    final cancels = service.cancels;
    await tester.pump(const Duration(seconds: 10));
    await tester.pump();
    expect(service.discovers, 3);
    expect(service.cancels, cancels);
    expect(harness.device.connects, 0);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('queued EllaConnect candidate rejects a route covered earlier in the same frame', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester, legacyConnect: true);
    service.nextChunks = [
      [_friend]
    ];
    final owner = tester.state(find.byType(EllaConnect));
    await harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner);
    tester.binding.addPostFrameCallback((_) {
      harness.navigator.currentState!.push(MaterialPageRoute<void>(builder: (_) => const Scaffold()));
    });
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(harness.device.connects, 0);
    expect(harness.onboarding.discoveryLeaseFor(owner), isNull);
    expect(tester.takeException(), isNull);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('queued EllaConnect candidate rejects route removal and disposal', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester);
    final route = MaterialPageRoute<void>(builder: (_) => EllaConnect(onNext: () {}, onSkip: () {}, onBack: () {}));
    harness.navigator.currentState!.push(route);
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    final owner = tester.state(find.byType(EllaConnect));
    service.nextChunks = [
      [_friend]
    ];
    await harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner);
    tester.binding.addPostFrameCallback((_) => harness.navigator.currentState!.removeRoute(route));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.byType(EllaConnect), findsNothing);
    expect(harness.onboarding.discoveryLeaseFor(owner), isNull);
    expect(harness.device.connects, 0);
    expect(tester.takeException(), isNull);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('queued EllaConnect candidate cannot adopt a replacement lease for the same owner', (tester) async {
    final service = _ScriptedService();
    final harness = _Harness(service);
    await harness.mount(tester, legacyConnect: true);
    final owner = tester.state(find.byType(EllaConnect));
    service.nextChunks = [
      [_friend]
    ];
    await harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner);
    tester.binding.addPostFrameCallback((_) {
      service.nextChunks = [];
      unawaited(harness.onboarding.cancelDeviceDiscovery(owner: owner));
      unawaited(harness.onboarding.scanDevices(onShowDialog: () {}, owner: owner));
    });
    await tester.pump();
    await tester.pump();
    expect(harness.device.connects, 0);
    expect(harness.onboarding.deviceList, isEmpty);
    await harness.leave(tester);
    harness.dispose();
  });

  testWidgets('active EllaConnect retains inherited first-candidate selection and stops its scan', (tester) async {
    final service = _ScriptedService()
      ..nextChunks = [
        [_friend]
      ];
    final harness = _Harness(service);
    await harness.mount(tester, legacyConnect: true);
    await tester.pump();
    await tester.pump();
    expect(harness.device.connects, 1);
    await tester.pump(const Duration(seconds: 30));
    expect(service.discovers, 1);
    expect(harness.device.connects, 1);
    await harness.leave(tester);
    harness.dispose();
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
