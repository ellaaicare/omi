import 'dart:async';

import 'package:flutter_blue_plus/flutter_blue_plus.dart';
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

class _FakeBleNotificationEndpoint implements BleNotificationEndpoint {
  final StreamController<List<int>> fresh = StreamController<List<int>>.broadcast();
  final StreamController<List<int>> replaying = StreamController<List<int>>.broadcast();
  final List<(bool, int)> notifyCalls = [];
  int failedEnableAttempts = 0;
  int freshValueRequests = 0;
  int replayingValueRequests = 0;
  Completer<void>? notifyBarrier;
  final Map<int, Completer<void>> notifyCallBarriers = {};

  @override
  Stream<List<int>> get freshValues {
    freshValueRequests++;
    return fresh.stream;
  }

  @override
  Stream<List<int>> get replayingValues {
    replayingValueRequests++;
    return replaying.stream;
  }

  @override
  Future<void> setNotifyValue(bool enabled, {required int timeout}) async {
    notifyCalls.add((enabled, timeout));
    await notifyCallBarriers[notifyCalls.length]?.future;
    await notifyBarrier?.future;
    if (enabled && failedEnableAttempts > 0) {
      failedEnableAttempts--;
      throw StateError('transient CCCD failure');
    }
  }

  Future<void> dispose() async {
    await fresh.close();
    await replaying.close();
  }
}

BleTransport _testBleTransport(
  _FakeBleNotificationEndpoint endpoint, {
  BleAudioLivenessRecovery? recovery,
  bool Function()? connectionProbe,
  Stream<BluetoothConnectionState>? connectionStates,
}) {
  return BleTransport(
    BluetoothDevice.fromId('00000000-0000-0000-0000-000000000001'),
    audioLivenessRecovery: recovery,
    notificationEndpointResolver: (_, __) async => endpoint,
    connectionProbe: connectionProbe ?? () => true,
    disconnectRegistrar: (_) {},
    connectionStateStream: connectionStates ?? const Stream<BluetoothConnectionState>.empty(),
  );
}

void main() {
  BtDevice necklace() => BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

  test('audio subscriptions accept fresh notifications only', () {
    expect(bleCharacteristicUsesFreshNotifications(audioDataStreamCharacteristicUuid), isTrue);
    expect(bleCharacteristicUsesFreshNotifications('2A19'), isFalse, reason: 'battery may retain replay semantics');
  });

  test('production BLE transport retries one transient CCCD failure and forwards only fresh audio', () async {
    final endpoint = _FakeBleNotificationEndpoint()..failedEnableAttempts = 1;
    final transport = _testBleTransport(endpoint);
    addTearDown(endpoint.dispose);
    addTearDown(transport.dispose);

    final stream = await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    final received = <List<int>>[];
    final subscription = stream?.listen(received.add);
    addTearDown(() => subscription?.cancel());
    endpoint.replaying.add([9, 9, 9]);
    endpoint.fresh.add([1, 2, 3]);
    await pumpEventQueue();

    expect(stream, isNotNull);
    expect(endpoint.notifyCalls, [
      (true, bleNotificationEnableTimeoutSeconds),
      (true, bleNotificationEnableTimeoutSeconds),
    ]);
    expect(endpoint.freshValueRequests, 2);
    expect(endpoint.replayingValueRequests, 0);
    expect(received, [
      [1, 2, 3],
    ]);
  });

  test('production BLE transport resets one silent CCCD inside the physical-audio deadline', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final recovery = BleAudioLivenessRecovery(window: const Duration(milliseconds: 1));
    final transport = _testBleTransport(endpoint, recovery: recovery);
    addTearDown(endpoint.dispose);
    addTearDown(transport.dispose);

    final stream = await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    final received = <List<int>>[];
    final subscription = stream?.listen(received.add);
    addTearDown(() => subscription?.cancel());
    await Future<void>.delayed(const Duration(milliseconds: 10));
    await pumpEventQueue(times: 20);
    endpoint.fresh.add([4, 5, 6]);
    await pumpEventQueue();

    expect(endpoint.notifyCalls, [
      (true, bleNotificationEnableTimeoutSeconds),
      (false, bleNotificationResetTimeoutSeconds),
      (true, bleNotificationEnableTimeoutSeconds),
    ]);
    expect(received, [
      [4, 5, 6],
    ]);
    final worstCaseRecovery = bleAudioLivenessWindow +
        const Duration(seconds: bleNotificationResetTimeoutSeconds + bleNotificationEnableTimeoutSeconds);
    expect(worstCaseRecovery, lessThan(const Duration(seconds: 5)));
  });

  test('production BLE setup fails closed when the account/device connection generation drifts', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final connectionStates = StreamController<BluetoothConnectionState>.broadcast();
    final notifyBarrier = Completer<void>();
    var connected = true;
    endpoint.notifyBarrier = notifyBarrier;
    final transport = _testBleTransport(
      endpoint,
      connectionProbe: () => connected,
      connectionStates: connectionStates.stream,
    );
    addTearDown(endpoint.dispose);
    addTearDown(connectionStates.close);
    addTearDown(transport.dispose);

    final streamFuture = transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    await pumpEventQueue();
    connected = false;
    connectionStates.add(BluetoothConnectionState.disconnected);
    await pumpEventQueue();
    notifyBarrier.complete();

    expect(await streamFuture, isNull);
    await pumpEventQueue();
    expect(endpoint.fresh.hasListener, isFalse);
  });

  test('production BLE transport discards a stale subscription before reconnect', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final connectionStates = StreamController<BluetoothConnectionState>.broadcast();
    var connected = true;
    final transport = _testBleTransport(
      endpoint,
      connectionProbe: () => connected,
      connectionStates: connectionStates.stream,
    );
    addTearDown(endpoint.dispose);
    addTearDown(connectionStates.close);
    addTearDown(transport.dispose);

    final firstStream = await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    final firstSubscription = firstStream?.listen((_) {});
    addTearDown(() => firstSubscription?.cancel());
    expect(endpoint.notifyCalls, [(true, bleNotificationEnableTimeoutSeconds)]);

    connected = false;
    connectionStates.add(BluetoothConnectionState.disconnected);
    await pumpEventQueue();
    connected = true;
    connectionStates.add(BluetoothConnectionState.connected);
    await pumpEventQueue();

    final secondStream =
        await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    final secondSubscription = secondStream?.listen((_) {});
    addTearDown(() => secondSubscription?.cancel());

    expect(secondStream, isNotNull);
    expect(endpoint.notifyCalls, [
      (true, bleNotificationEnableTimeoutSeconds),
      (true, bleNotificationEnableTimeoutSeconds),
    ]);
    expect(endpoint.freshValueRequests, 2);
  });

  test('disconnect during silent reset prevents a stale notification re-enable', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final connectionStates = StreamController<BluetoothConnectionState>.broadcast();
    final resetBarrier = Completer<void>();
    endpoint.notifyCallBarriers[2] = resetBarrier;
    var connected = true;
    final transport = _testBleTransport(
      endpoint,
      recovery: BleAudioLivenessRecovery(window: const Duration(milliseconds: 1)),
      connectionProbe: () => connected,
      connectionStates: connectionStates.stream,
    );
    addTearDown(endpoint.dispose);
    addTearDown(connectionStates.close);
    addTearDown(transport.dispose);

    final stream = await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    final subscription = stream?.listen((_) {});
    addTearDown(() => subscription?.cancel());
    await Future<void>.delayed(const Duration(milliseconds: 10));
    while (endpoint.notifyCalls.length < 2) {
      await pumpEventQueue();
    }

    connected = false;
    connectionStates.add(BluetoothConnectionState.disconnected);
    await pumpEventQueue();
    resetBarrier.complete();
    await pumpEventQueue(times: 20);

    expect(endpoint.notifyCalls, [
      (true, bleNotificationEnableTimeoutSeconds),
      (false, bleNotificationResetTimeoutSeconds),
    ]);
    expect(endpoint.fresh.hasListener, isFalse);
  });

  test('dispose waits for in-flight setup and permits no late listener', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final enableBarrier = Completer<void>();
    endpoint.notifyCallBarriers[1] = enableBarrier;
    final transport = _testBleTransport(endpoint);
    addTearDown(endpoint.dispose);

    final streamFuture = transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    while (endpoint.notifyCalls.isEmpty) {
      await pumpEventQueue();
    }
    var disposeCompleted = false;
    final disposeFuture = transport.dispose().then((_) => disposeCompleted = true);
    await pumpEventQueue();
    expect(disposeCompleted, isFalse);

    enableBarrier.complete();
    expect(await streamFuture, isNull);
    await disposeFuture;

    expect(disposeCompleted, isTrue);
    expect(endpoint.fresh.hasListener, isFalse);
    expect(endpoint.notifyCalls, [(true, bleNotificationEnableTimeoutSeconds)]);
  });

  test('ready request overlapping silent recovery reuses one serialized listener setup', () async {
    final endpoint = _FakeBleNotificationEndpoint();
    final resetBarrier = Completer<void>();
    endpoint.notifyCallBarriers[2] = resetBarrier;
    final transport = _testBleTransport(
      endpoint,
      recovery: BleAudioLivenessRecovery(window: const Duration(milliseconds: 1)),
    );
    addTearDown(endpoint.dispose);
    addTearDown(transport.dispose);

    expect(
      await transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid),
      isNotNull,
    );
    await Future<void>.delayed(const Duration(milliseconds: 10));
    while (endpoint.notifyCalls.length < 2) {
      await pumpEventQueue();
    }

    final overlappingReady = transport.getReadyCharacteristicStream(omiServiceUuid, audioDataStreamCharacteristicUuid);
    await pumpEventQueue();
    resetBarrier.complete();

    expect(await overlappingReady, isNotNull);
    expect(endpoint.notifyCalls, [
      (true, bleNotificationEnableTimeoutSeconds),
      (false, bleNotificationResetTimeoutSeconds),
      (true, bleNotificationEnableTimeoutSeconds),
    ]);
    expect(endpoint.freshValueRequests, 2);
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
