import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/discovery/device_discoverer.dart';
import 'package:omi/services/devices/omi_connection.dart';
import 'package:omi/services/devices/transports/device_transport.dart';

class _BlockingDiscoverer extends DeviceDiscoverer {
  final Completer<void> started = Completer<void>();
  final Completer<DeviceDiscoveryResult> result = Completer<DeviceDiscoveryResult>();

  @override
  bool get isSupported => true;

  @override
  String get name => 'blocking';

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) {
    started.complete();
    return result.future;
  }

  @override
  Future<void> stop() async {}
}

class _ImmediateDiscoverer extends DeviceDiscoverer {
  _ImmediateDiscoverer(this.device);

  final BtDevice device;

  @override
  bool get isSupported => true;

  @override
  String get name => 'immediate';

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) async => DeviceDiscoveryResult(devices: [device]);

  @override
  Future<void> stop() async {}
}

class _ConnectAfterCancelTransport implements DeviceTransport {
  _ConnectAfterCancelTransport(this.deviceId);

  @override
  final String deviceId;

  final Completer<void> connectStarted = Completer<void>();
  final Completer<void> releaseConnect = Completer<void>();
  final StreamController<DeviceTransportState> _states = StreamController<DeviceTransportState>.broadcast(sync: true);
  bool connected = false;
  int disconnectCalls = 0;

  @override
  Stream<DeviceTransportState> get connectionStateStream => _states.stream;

  @override
  Future<void> connect() async {
    connectStarted.complete();
    await releaseConnect.future;
    connected = true;
    _states.add(DeviceTransportState.connected);
  }

  @override
  Future<void> disconnect() async {
    disconnectCalls++;
    connected = false;
    _states.add(DeviceTransportState.disconnected);
  }

  @override
  Future<void> dispose() async => _states.close();

  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) => const Stream.empty();

  @override
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async {
    return getCharacteristicStream(serviceUuid, characteristicUuid);
  }

  @override
  Future<bool> isConnected() async => connected;

  @override
  Future<bool> ping() async => connected;

  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) async => const [];

  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) async {}
}

void main() {
  test('explicit cancellation fences discovery before it can create a late connection', () async {
    final discoverer = _BlockingDiscoverer();
    var connectionCreations = 0;
    final service = DeviceService(
      discoverers: [discoverer],
      connectionCreator: (_) {
        connectionCreations++;
        return null;
      },
    )..start();

    final discovery = service.discover(desirableDeviceId: 'necklace-1');
    await discoverer.started.future;

    await service.cancelPendingConnection();
    discoverer.result.complete(
      DeviceDiscoveryResult(
        devices: [BtDevice(name: 'Friend', id: 'necklace-1', type: DeviceType.omi, rssi: -30)],
      ),
    );
    await discovery;

    expect(service.status, DeviceServiceStatus.ready);
    expect(connectionCreations, 0);
  });

  test('explicit cancellation disconnects again when native startup settles late', () async {
    final device = BtDevice(name: 'Friend', id: 'necklace-late-connect', type: DeviceType.omi, rssi: -30);
    final transport = _ConnectAfterCancelTransport(device.id);
    final service = DeviceService(
      discoverers: [_ImmediateDiscoverer(device)],
      connectionCreator: (_) => OmiDeviceConnection(device, transport),
    )..start();

    await service.discover();
    final connection = service.ensureConnection(device.id, force: true);
    await transport.connectStarted.future;

    await service.cancelPendingConnection();
    expect(transport.disconnectCalls, 1);

    transport.releaseConnect.complete();
    expect(await connection, isNull);
    expect(transport.connected, isFalse);
    expect(transport.disconnectCalls, 2, reason: 'late native startup must be torn down after it settles');

    await transport.dispose();
  });
}
