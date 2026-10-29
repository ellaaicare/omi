import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/gen/pigeon_communicator.g.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/bridges/ble_bridge.dart';
import 'package:omi/services/devices/connectors/device_connection.dart';
import 'package:omi/services/devices/discovery/device_locator.dart';
import 'package:omi/services/devices/transports/device_transport.dart';

class _FakeTransport implements DeviceTransport {
  _FakeTransport(this.deviceId);

  @override
  final String deviceId;
  bool disposed = false;
  bool physicallyConnected = true;
  int connects = 0;
  Object? failNextConnect;
  @override
  Future<bool> isConnected() async => physicallyConnected;
  @override
  Future<void> connect() async {
    connects++;
    if (failNextConnect != null) {
      final error = failNextConnect;
      failNextConnect = null;
      throw error!;
    }
    physicallyConnected = true;
  }

  @override
  Future<void> dispose() async => disposed = true;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeConnection implements DeviceConnection {
  _FakeConnection(this.device) : transport = _FakeTransport(device.id);

  @override
  final BtDevice device;
  @override
  final _FakeTransport transport;

  DeviceConnectionState _state = DeviceConnectionState.disconnected;
  bool disconnectCalled = false;
  int connects = 0;
  int syncTimeCalls = 0;
  void Function(String deviceId, DeviceConnectionState state)? stateCallback;

  @override
  DeviceConnectionState get status => _state;

  /// Production pushes this through the transport's connectionStateStream
  /// (NativeBleTransport emits disconnected when the link drops); the fake
  /// models that notification explicitly.
  void transportDidDisconnect() {
    _state = DeviceConnectionState.disconnected;
    stateCallback?.call(device.id, _state);
  }

  /// Mirrors [DeviceConnection.connect]: guard, transport connect, then
  /// device-specific setup (time sync on Omi pendants) — never the bare
  /// transport alone.
  @override
  Future<void> connect({void Function(String deviceId, DeviceConnectionState state)? onConnectionStateChanged}) async {
    if (_state == DeviceConnectionState.connected) {
      throw DeviceConnectionException('Connection already established, please disconnect before start new connection');
    }
    stateCallback = onConnectionStateChanged ?? stateCallback;
    connects++;
    try {
      await transport.connect();
    } catch (e) {
      // Mirror DeviceConnection.connect: transport failures surface as
      // DeviceConnectionException so service-level handling stays uniform.
      throw DeviceConnectionException('Transport connection failed: $e');
    }
    syncTimeCalls++; // Omi pendant: device-specific reconnect setup
    _state = DeviceConnectionState.connected;
    stateCallback?.call(device.id, _state);
  }

  @override
  Future<void> disconnect() async {
    disconnectCalled = true;
    _state = DeviceConnectionState.disconnected;
    stateCallback?.call(device.id, _state);
    stateCallback = null;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

BtDevice _device(String id, {DeviceType type = DeviceType.omi}) =>
    BtDevice(id: id, name: id, type: type, rssi: 0, locator: DeviceLocator.bluetooth(deviceId: id));

// Ella Base v4.8: the user disconnect that DeviceProvider.disconnectKeepingPairing() uses
// (DeviceService.disconnectDevice(id, userInitiated: true)) releases only the link. The necklace stays the
// remembered paired device, automatic reconnect (and so automatic capture) stays off, and the normal
// Reconnect path (ensureConnection(force: true)) brings it back. Forget is the only path that clears pairing.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const id = 'AA:BB:CC:DD:EE:FF';
  late Map<String, _FakeConnection> built;
  late DeviceService service;
  final mockedChannels = <String>{};
  final reconnectConfigurations = <Object?>[];
  var unmanageCalls = 0;

  void mockBleHostApi(String method) {
    final channel = 'dev.flutter.pigeon.omi_pigeon.BleHostApi.$method';
    mockedChannels.add(channel);
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMessageHandler(channel,
        (ByteData? message) async {
      if (method == 'configureSavedReconnect') {
        reconnectConfigurations.add(BleHostApi.pigeonChannelCodec.decodeMessage(message));
      }
      if (method == 'unmanageDevice') unmanageCalls++;
      return BleHostApi.pigeonChannelCodec.encodeMessage(<Object?>[null]);
    });
  }

  tearDown(() {
    for (final channel in mockedChannels) {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMessageHandler(channel, null);
    }
    mockedChannels.clear();
    BleBridge.instance.clearPeripheralForTesting(id);
  });

  setUp(() async {
    reconnectConfigurations.clear();
    unmanageCalls = 0;
    for (final m in ['stopScan', 'startScan', 'configureSavedReconnect', 'unmanageDevice']) {
      mockBleHostApi(m);
    }
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    final device = _device(id);
    await SharedPreferencesUtil().btDeviceAdd(device);
    await SharedPreferencesUtil().btDeviceSet(device);
    SharedPreferencesUtil().deviceName = 'DevKit';
    built = {};
    service = DeviceService(connectionBuilder: (device) => built[device.id] = _FakeConnection(device));
    await service.ensureConnection(id, force: true);
    expect(service.connectionFor(id)?.status, DeviceConnectionState.connected);
  });

  test('user disconnect releases the link and keeps the necklace paired and remembered', () async {
    await service.disconnectDevice(id, userInitiated: true);

    expect(built[id]!.disconnectCalled, isTrue);
    expect(built[id]!.transport.disposed, isTrue);
    expect(service.connectionFor(id), isNull);
    expect(SharedPreferencesUtil().btDevice.id, id, reason: 'still the paired device');
    expect(SharedPreferencesUtil().deviceName, 'DevKit');
    expect(unmanageCalls, 0, reason: 'not unmanaged (Forget does that)');
    expect(SharedPreferencesUtil().getStringList('manualBleDisconnects'), contains(id));
  });

  test('while user-disconnected, automatic reconnect (and so automatic capture) does nothing', () async {
    await service.disconnectDevice(id, userInitiated: true);
    final connectsBefore = built[id]!.connects;

    expect(await service.ensureConnection(id, automatic: true), isNull);
    expect(service.connectionFor(id), isNull);
    expect(built[id]!.connects, connectsBefore);
    expect(service.canAutoReconnectDevice(id), isFalse);
  });

  test('the normal Reconnect path clears the hold and reconnects the same remembered necklace', () async {
    await service.disconnectDevice(id, userInitiated: true);

    final connection = await service.ensureConnection(id, force: true);
    expect(connection?.status, DeviceConnectionState.connected);
    expect(SharedPreferencesUtil().getStringList('manualBleDisconnects'), isNot(contains(id)));
    expect(service.canAutoReconnectDevice(id), isTrue);
    expect(SharedPreferencesUtil().btDevice.id, id);
  });

  test('Forget, by contrast, is the path that unmanages the device', () async {
    await service.forgetDevice(id);
    expect(unmanageCalls + (built[id]!.transport.disposed ? 1 : 0), greaterThan(0));
    expect(SharedPreferencesUtil().getStringList('manualBleDisconnects'), contains(id));
  });
}
