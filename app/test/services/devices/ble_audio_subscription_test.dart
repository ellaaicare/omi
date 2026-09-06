import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/devices/models.dart';
import 'package:omi/services/devices/omi_connection.dart';
import 'package:omi/services/devices/transports/ble_transport.dart';
import 'package:omi/services/devices/transports/device_transport.dart';

class _AudioTransport extends DeviceTransport {
  _AudioTransport({required this.ready});

  final bool ready;
  final StreamController<List<int>> audio = StreamController<List<int>>.broadcast();
  final StreamController<DeviceTransportState> states = StreamController<DeviceTransportState>.broadcast();
  int readyRequests = 0;
  int legacyStreamRequests = 0;

  @override
  String get deviceId => 'necklace-1';

  @override
  Stream<DeviceTransportState> get connectionStateStream => states.stream;

  @override
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async {
    readyRequests++;
    return ready ? audio.stream : null;
  }

  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) {
    legacyStreamRequests++;
    return audio.stream;
  }

  @override
  Future<void> connect() async {}

  @override
  Future<void> disconnect() async {}

  @override
  Future<bool> isConnected() async => true;

  @override
  Future<bool> ping() async => true;

  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) async => const [];

  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) async {}

  @override
  Future<void> dispose() async {
    await audio.close();
    await states.close();
  }
}

void main() {
  BtDevice necklace() => BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

  test('audio subscriptions accept fresh notifications only', () {
    expect(bleCharacteristicUsesFreshNotifications(audioDataStreamCharacteristicUuid), isTrue);
    expect(bleCharacteristicUsesFreshNotifications('2A19'), isFalse, reason: 'battery may retain replay semantics');
  });

  test('Omi production audio entrypoint fails closed until the BLE subscription is ready', () async {
    final transport = _AudioTransport(ready: false);
    final connection = OmiDeviceConnection(necklace(), transport);
    addTearDown(transport.dispose);

    final subscription = await connection.performGetBleAudioBytesListener(onAudioBytesReceived: (_) {});

    expect(subscription, isNull);
    expect(transport.readyRequests, 1);
    expect(transport.legacyStreamRequests, 0);
  });

  test('Omi production audio entrypoint forwards bytes after subscription readiness', () async {
    final transport = _AudioTransport(ready: true);
    final connection = OmiDeviceConnection(necklace(), transport);
    final received = <List<int>>[];
    addTearDown(transport.dispose);

    final subscription = await connection.performGetBleAudioBytesListener(onAudioBytesReceived: received.add);
    addTearDown(() => subscription?.cancel());
    transport.audio.add([1, 2, 3]);
    await pumpEventQueue();

    expect(received, [
      [1, 2, 3],
    ]);
    expect(transport.readyRequests, 1);
    expect(transport.legacyStreamRequests, 0);
  });

  testWidgets('silent connected audio triggers one bounded notification recovery', (tester) async {
    var recoveries = 0;
    final recovery = BleAudioLivenessRecovery(window: const Duration(seconds: 2));
    addTearDown(recovery.dispose);

    recovery.arm(() async {
      recoveries++;
    });
    await tester.pump(const Duration(seconds: 2));
    expect(recoveries, 1);

    await tester.pump(const Duration(seconds: 10));
    expect(recoveries, 1, reason: 'a silent link must not enter a CCCD retry loop');
  });

  testWidgets('the first physical audio frame cancels notification recovery', (tester) async {
    var recoveries = 0;
    final recovery = BleAudioLivenessRecovery(window: const Duration(seconds: 2));
    addTearDown(recovery.dispose);

    recovery.arm(() async {
      recoveries++;
    });
    await tester.pump(const Duration(seconds: 1));
    recovery.observedAudio();
    await tester.pump(const Duration(seconds: 10));

    expect(recoveries, 0);
  });
}
