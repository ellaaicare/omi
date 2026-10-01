import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as legacy_preferences;
import 'package:omi/backend/schema/bt_device/bt_device.dart' as legacy_device;
import 'package:omi/ella/upstream_capture/ella_upstream_device_service_adapter.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart' show DeviceConsentRequiredException;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart' as upstream_device;
import 'package:omi/upstream_capture/services/devices.dart' as upstream_service;
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart' as upstream_connection;
import 'package:omi/upstream_capture/services/devices/discovery/device_locator.dart' as upstream_locator;
import 'package:omi/upstream_capture/services/devices/transports/device_transport.dart' as upstream_transport;

class _FakeUpstreamTransport implements upstream_transport.DeviceTransport {
  _FakeUpstreamTransport(this.deviceId);

  @override
  final String deviceId;
  int disconnectCalls = 0;
  int disposeCalls = 0;

  @override
  Stream<upstream_transport.DeviceTransportState> get connectionStateStream => const Stream.empty();

  @override
  Future<void> disconnect() async => disconnectCalls++;

  @override
  Future<void> dispose() async => disposeCalls++;

  @override
  Future<bool> isConnected() async => true;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeUpstreamConnection implements upstream_connection.DeviceConnection {
  _FakeUpstreamConnection(this.device) : transport = _FakeUpstreamTransport(device.id);

  @override
  final upstream_device.BtDevice device;
  @override
  final _FakeUpstreamTransport transport;
  upstream_service.DeviceConnectionState state = upstream_service.DeviceConnectionState.connected;
  int disconnectCalls = 0;
  int micGainWrites = 0;
  int batteryLevel = 84;

  @override
  upstream_service.DeviceConnectionState get status => state;

  @override
  upstream_service.DeviceConnectionState get connectionState => state;

  @override
  Future<void> disconnect() async {
    disconnectCalls++;
    state = upstream_service.DeviceConnectionState.disconnected;
    await transport.disconnect();
  }

  @override
  Future<bool> isConnected() async => state == upstream_service.DeviceConnectionState.connected;

  @override
  Future<void> unpair() async {}

  @override
  Future<void> performSetMicGain(int gain) async => micGainWrites++;

  @override
  Future<int> performRetrieveBatteryLevel() async => batteryLevel;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeUpstreamDeviceService extends upstream_service.DeviceService {
  _FakeUpstreamDeviceService({required this.discovered, this.discoveryGate, this.activeConnection});

  final List<upstream_device.BtDevice> discovered;
  final Completer<void>? discoveryGate;
  int discoverCalls = 0;
  int stopDiscovererCalls = 0;
  int ensureConnectionCalls = 0;
  int startCalls = 0;
  int stopCalls = 0;
  _FakeUpstreamConnection? activeConnection;

  @override
  List<upstream_connection.DeviceConnection> get connections => [if (activeConnection != null) activeConnection!];

  @override
  void start() => startCalls++;

  @override
  Future<void> stop() async => stopCalls++;

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    discoverCalls++;
    await discoveryGate?.future;
    onDevices(discovered);
  }

  @override
  Future<void> stopDiscoverers() async {
    stopDiscovererCalls++;
    final gate = discoveryGate;
    if (discoverCalls > 0 && gate != null && !gate.isCompleted) gate.complete();
  }

  @override
  upstream_connection.DeviceConnection? connectionFor(String deviceId) {
    final connection = activeConnection;
    return connection?.device.id == deviceId ? connection : null;
  }

  @override
  Future<upstream_connection.DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    ensureConnectionCalls++;
    return connectionFor(deviceId);
  }

  void publishConnection(upstream_service.DeviceConnectionState state) {
    final deviceId = activeConnection?.device.id;
    if (deviceId != null) onDeviceConnectionStateChanged(deviceId, state);
  }
}

upstream_device.BtDevice _upstreamDevice(String name, String id, upstream_device.DeviceType type) {
  return upstream_device.BtDevice(
    name: name,
    id: id,
    type: type,
    rssi: -40,
    locator: upstream_locator.DeviceLocator.bluetooth(deviceId: id),
  );
}

EllaUpstreamDeviceServiceAdapter _adapter(
  _FakeUpstreamDeviceService service, {
  Future<bool> Function(String ownerId, upstream_device.BtDevice device)? connect,
  Future<void> Function(String? deviceId)? disconnect,
  String? Function()? connectionOwner,
}) {
  return EllaUpstreamDeviceServiceAdapter(
    serviceLoader: () async => service,
    connect: connect ?? (_, __) async => true,
    disconnect: disconnect ?? (_) async {},
    connectionOwner: connectionOwner,
  )..start();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({
      'uid': 'owner-a',
      'btDeviceOwnerBinding': 'owner-a',
    });
    await legacy_preferences.SharedPreferencesUtil.init();
  });

  test('production picker contract discovers Compass and Friend through upstream authority', () async {
    final service = _FakeUpstreamDeviceService(
      discovered: [
        _upstreamDevice('Compass', 'compass-1', upstream_device.DeviceType.fieldy),
        _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant),
      ],
    );
    final adapter = _adapter(service);
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    final onboarding = OnboardingProvider(deviceService: adapter)
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(onboarding.dispose);
    addTearDown(adapter.stop);

    await onboarding.scanDevices(onShowDialog: () {});

    expect(service.discoverCalls, 1);
    expect(onboarding.deviceList.map((device) => device.name), ['Compass', 'Friend']);
    expect(onboarding.deviceList.map((device) => device.id), ['compass-1', 'friend-1']);
  });

  test('ordinary Connect binds owner and commits the upstream-selected device without legacy capture', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(discovered: [friend]);
    String? connectedOwner;
    upstream_device.BtDevice? connectedDevice;
    final adapter = _adapter(
      service,
      connect: (ownerId, device) async {
        connectedOwner = ownerId;
        connectedDevice = device;
        service.activeConnection = _FakeUpstreamConnection(device);
        service.publishConnection(upstream_service.DeviceConnectionState.connected);
        return true;
      },
      connectionOwner: () => connectedOwner,
    );
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    var presentationCommits = 0;
    deviceProvider.onDeviceConnected = (_) => presentationCommits++;
    final onboarding = OnboardingProvider(deviceService: adapter)
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(onboarding.dispose);
    addTearDown(adapter.stop);

    await onboarding.scanDevices(onShowDialog: () {});
    await onboarding.handleTap(device: onboarding.deviceList.single, isFromOnboarding: false);

    expect(connectedOwner, 'owner-a');
    expect(connectedDevice?.id, friend.id);
    expect(deviceProvider.presentationIsConnected, isTrue);
    expect(deviceProvider.presentationConnectedDevice?.id, friend.id);
    expect(legacy_preferences.SharedPreferencesUtil().btDevice.id, friend.id);
    expect(legacy_preferences.SharedPreferencesUtil().btDeviceOwnerBinding, 'owner-a');
    expect(presentationCommits, 1);
  });

  test('consent-required selection retains the discovered device and never commits pairing', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(discovered: [friend]);
    final adapter = _adapter(service, connect: (_, __) async => throw const DeviceConsentRequiredException());
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    final onboarding = OnboardingProvider(deviceService: adapter)
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(onboarding.dispose);
    addTearDown(adapter.stop);

    await onboarding.scanDevices(onShowDialog: () {});
    final outcome = await onboarding.handleTap(device: onboarding.deviceList.single, isFromOnboarding: false);

    expect(outcome, DeviceSelectionOutcome.consentRequired);
    expect(deviceProvider.lastConnectionConsentRequired, isTrue);
    expect(deviceProvider.presentationIsConnected, isFalse);
    expect(onboarding.deviceList.single.id, friend.id);
    expect(onboarding.isClicked, isFalse);
  });

  test('disposing the picker cancels delayed upstream discovery and fences late results', () async {
    final gate = Completer<void>();
    final service = _FakeUpstreamDeviceService(
      discovered: [_upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant)],
      discoveryGate: gate,
    );
    final adapter = _adapter(service);
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    final onboarding = OnboardingProvider(deviceService: adapter)
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(adapter.stop);

    final scan = onboarding.scanDevices(onShowDialog: () {});
    while (service.discoverCalls == 0) {
      await Future<void>.delayed(Duration.zero);
    }
    final stopsBeforeDispose = service.stopDiscovererCalls;
    onboarding.dispose();
    await scan;

    expect(service.stopDiscovererCalls, stopsBeforeDispose + 1);
    expect(onboarding.deviceList, isEmpty);
  });

  test('legacy inspection cannot create a connection outside the owner-bound path', () async {
    final service = _FakeUpstreamDeviceService(
      discovered: [_upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant)],
    );
    final adapter = _adapter(service);
    addTearDown(adapter.stop);

    expect(await adapter.ensureConnection('friend-1', force: true), isNull);
    expect(service.ensureConnectionCalls, 0);
  });

  test('dock-origin connection projects into Home for the same bound owner', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(
      discovered: [friend],
      activeConnection: _FakeUpstreamConnection(friend),
    );
    final adapter = _adapter(service, connectionOwner: () => 'owner-a');
    await adapter.ensureConnection(friend.id);
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    addTearDown(deviceProvider.dispose);
    addTearDown(adapter.stop);

    await Future<void>.delayed(const Duration(milliseconds: 150));

    expect(deviceProvider.presentationConnectedDevice?.id, friend.id);
    expect(deviceProvider.presentationIsConnected, isTrue);
    expect(deviceProvider.presentationBatteryLevel, 84);
    expect(legacy_preferences.SharedPreferencesUtil().btDevice.id, friend.id);
    expect(legacy_preferences.SharedPreferencesUtil().btDeviceOwnerBinding, 'owner-a');
  });

  test('dock-origin connection from another owner stays outside Home presentation', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(
      discovered: [friend],
      activeConnection: _FakeUpstreamConnection(friend),
    );
    final adapter = _adapter(service, connectionOwner: () => 'owner-b');
    await adapter.ensureConnection(friend.id);
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    addTearDown(deviceProvider.dispose);
    addTearDown(adapter.stop);

    await Future<void>.delayed(const Duration(milliseconds: 150));

    expect(deviceProvider.presentationConnectedDevice, isNull);
    expect(deviceProvider.presentationIsConnected, isFalse);
    expect(legacy_preferences.SharedPreferencesUtil().btDevice.id, isEmpty);
  });

  test('disconnect preserves the active target after Settings clears persisted pairing', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(
      discovered: [friend],
      activeConnection: _FakeUpstreamConnection(friend),
    );
    final disconnectedTargets = <String?>[];
    final adapter = _adapter(
      service,
      connectionOwner: () => 'owner-a',
      disconnect: (deviceId) async => disconnectedTargets.add(deviceId),
    );
    addTearDown(adapter.stop);
    await adapter.ensureConnection(friend.id);

    await legacy_preferences.SharedPreferencesUtil().btDeviceSet(legacy_device.BtDevice.empty());
    await adapter.disconnectDevice();

    expect(disconnectedTargets, [friend.id]);
  });

  test('stale Settings wrapper cannot disconnect a forced replacement connection', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final firstConnection = _FakeUpstreamConnection(friend);
    final service = _FakeUpstreamDeviceService(discovered: [friend], activeConnection: firstConnection);
    final disconnectedTargets = <String?>[];
    final adapter = _adapter(
      service,
      connectionOwner: () => 'owner-a',
      disconnect: (deviceId) async => disconnectedTargets.add(deviceId),
    );
    addTearDown(adapter.stop);

    final firstWrapper = await adapter.ensureConnection(friend.id);
    final replacement = _FakeUpstreamConnection(friend);
    service.activeConnection = replacement;
    service.publishConnection(upstream_service.DeviceConnectionState.connected);
    final replacementWrapper = await adapter.ensureConnection(friend.id);

    expect(replacementWrapper, isNot(same(firstWrapper)));
    await firstWrapper!.disconnect();
    await firstWrapper.performSetMicGain(7);
    expect(firstConnection.disconnectCalls, 1);
    expect(firstConnection.micGainWrites, 0);
    expect(replacement.disconnectCalls, 0);
    expect(disconnectedTargets, isEmpty);

    await replacementWrapper!.disconnect();
    expect(disconnectedTargets, [friend.id]);
  });

  test('account stop waits for delayed upstream boot and quiesces the hardware service', () async {
    final service = _FakeUpstreamDeviceService(discovered: const []);
    final boot = Completer<upstream_service.DeviceService>();
    final adapter = EllaUpstreamDeviceServiceAdapter(
      serviceLoader: () => boot.future,
      connect: (_, __) async => false,
      disconnect: (_) async {},
    )..start();

    final stopping = adapter.stop();
    boot.complete(service);
    await stopping;

    expect(service.stopCalls, 1);
    expect(service.startCalls, 0);
  });

  test('consent denial leaves discovery available without connecting capture', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(discovered: [friend]);
    final adapter = _adapter(service, connect: (_, __) async => false);
    final deviceProvider = DeviceProvider(deviceService: adapter, automaticallyReconnectOnReady: false);
    final onboarding = OnboardingProvider(deviceService: adapter)
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(onboarding.dispose);
    addTearDown(adapter.stop);

    await onboarding.scanDevices(onShowDialog: () {});
    expect(onboarding.deviceList.single.id, friend.id);
    await onboarding.handleTap(device: onboarding.deviceList.single, isFromOnboarding: false);

    expect(deviceProvider.presentationIsConnected, isFalse);
    expect(service.ensureConnectionCalls, 0);
  });

  test('cancelling a delayed explicit connect fences the result and tears down the stale link', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(discovered: [friend]);
    final connectGate = Completer<bool>();
    var disconnectCalls = 0;
    final adapter = _adapter(
      service,
      connect: (_, __) => connectGate.future,
      disconnect: (_) async => disconnectCalls++,
    );
    addTearDown(adapter.stop);

    final pending = adapter.connectForCurrentUser('owner-a', _legacyDevice(friend));
    await Future<void>.delayed(Duration.zero);
    await adapter.cancelPendingConnection();
    connectGate.complete(true);

    expect(await pending, isNull);
    expect(disconnectCalls, 1);
  });

  test('provider timeout cancels a delayed upstream connect and tears down its late physical link', () async {
    final friend = _upstreamDevice('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final service = _FakeUpstreamDeviceService(discovered: [friend]);
    final connectGate = Completer<bool>();
    var disconnectCalls = 0;
    final adapter = _adapter(
      service,
      connect: (_, __) => connectGate.future,
      disconnect: (_) async => disconnectCalls++,
    );
    final deviceProvider = DeviceProvider(
      deviceService: adapter,
      automaticallyReconnectOnReady: false,
      connectionAttemptTimeout: const Duration(milliseconds: 10),
    );
    addTearDown(deviceProvider.dispose);
    addTearDown(adapter.stop);

    final pending = deviceProvider.connectDeviceForCurrentUser(_legacyDevice(friend));
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(await pending, isFalse);
    connectGate.complete(true);
    await Future<void>.delayed(Duration.zero);

    expect(disconnectCalls, 1);
    expect(deviceProvider.presentationIsConnected, isFalse);
  });
}

legacy_device.BtDevice _legacyDevice(upstream_device.BtDevice device) {
  return legacy_device.BtDevice.fromJson(Map<String, dynamic>.from(device.toJson()));
}
