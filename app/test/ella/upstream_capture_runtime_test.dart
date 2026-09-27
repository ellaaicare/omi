import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_adapter.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/upstream_capture/devices/connectors/omi_connection.dart';
import 'package:omi/upstream_capture/devices/discovery/device_discoverer.dart';
import 'package:omi/upstream_capture/devices/models.dart';
import 'package:omi/upstream_capture/devices/transports/device_transport.dart';
import 'package:omi/upstream_capture/mic/mic_recorder_interface.dart';

/// In-memory BLE transport: no CoreBluetooth/pigeon involved, so these tests
/// run in plain `flutter test` without a device or simulator.
class FakeDeviceTransport implements DeviceTransport {
  FakeDeviceTransport(this.deviceIdValue);

  final String deviceIdValue;
  @override
  String get deviceId => deviceIdValue;

  final _characteristicControllers = <String, StreamController<List<int>>>{};
  bool connected = false;

  StreamController<List<int>> controllerFor(String serviceUuid, String characteristicUuid) {
    return _characteristicControllers.putIfAbsent(
        '$serviceUuid|$characteristicUuid', () => StreamController.broadcast());
  }

  void emit(String serviceUuid, String characteristicUuid, List<int> bytes) {
    controllerFor(serviceUuid, characteristicUuid).add(bytes);
  }

  @override
  Future<void> connect() async => connected = true;

  @override
  Future<void> disconnect() async => connected = false;

  @override
  Future<bool> isConnected() async => connected;

  @override
  Future<bool> ping() async => true;

  @override
  Future<bool> requestBond() async => true;

  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) =>
      controllerFor(serviceUuid, characteristicUuid).stream;

  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) async => const [0];

  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) async {}

  @override
  Stream<DeviceTransportState> get connectionStateStream => const Stream.empty();

  @override
  Future<void> dispose() async {
    for (final controller in _characteristicControllers.values) {
      await controller.close();
    }
  }
}

class FakeMicRecorderService implements IMicRecorderService {
  void Function(Uint8List bytes)? onByteReceived;
  bool started = false;
  bool stopped = false;
  Object? startError;

  void emit(Uint8List bytes) => onByteReceived?.call(bytes);

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    Function(bool began)? onInterruption,
  }) async {
    if (startError != null) throw startError!;
    this.onByteReceived = onByteReceived;
    started = true;
  }

  @override
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  }) async {}

  @override
  void stop() => stopped = true;

  @override
  void probeStallAfterForeground() {}
}

class FakeDeviceDiscoverer implements DeviceDiscoverer {
  FakeDeviceDiscoverer(this.result);

  final DeviceDiscoveryResult result;

  @override
  String get name => 'fake';

  @override
  bool get isSupported => true;

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) async => result;

  @override
  Future<void> stop() async {}
}

BtDevice _fakeOmiDevice() => BtDevice(name: 'Fake Omi', id: 'fake-omi-1', type: DeviceType.omi, rssi: -40);

void main() {
  group('EllaUpstreamCaptureRuntime necklace path', () {
    late FakeDeviceTransport transport;
    late OmiDeviceConnection connection;
    late FakeDeviceDiscoverer discoverer;
    late List<Map<String, Object?>> routed;
    late EllaUpstreamCaptureAdapter adapter;
    late EllaUpstreamCaptureRuntime runtime;

    setUp(() {
      final device = _fakeOmiDevice();
      transport = FakeDeviceTransport(device.id);
      connection = OmiDeviceConnection(device, transport);
      discoverer = FakeDeviceDiscoverer(DeviceDiscoveryResult(devices: [device]));
      routed = [];
      adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
      adapter.replaceSession('account-a');
      runtime = EllaUpstreamCaptureRuntime(
        adapter: adapter,
        routeAudio: ({required isPhone, required bytes}) => routed.add({'isPhone': isPhone, 'bytes': bytes}),
        discoverer: discoverer,
        connectionFactory: (_) => connection,
      );
    });

    test('ensureConnection discovers and connects the fake necklace', () async {
      expect(await runtime.ensureConnection(), isTrue);
      expect(transport.connected, isTrue);
      expect(adapter.bleConnected, isTrue);
    });

    test('startNecklace routes each admitted frame as opus-source (isPhone=false)', () async {
      await runtime.ensureConnection();
      expect(await runtime.startNecklace(), isTrue);
      transport.emit(omiServiceUuid, audioDataStreamCharacteristicUuid, [1, 2, 3]);
      await Future<void>.delayed(Duration.zero);
      expect(routed, hasLength(1));
      expect(routed.single['isPhone'], isFalse);
      expect(routed.single['bytes'], [1, 2, 3]);
    });

    test('mayEmitAudio()==false: BLE connects but no audio is emitted', () async {
      var allowed = false;
      adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => allowed, enabled: true);
      adapter.replaceSession('account-a');
      runtime = EllaUpstreamCaptureRuntime(
        adapter: adapter,
        routeAudio: ({required isPhone, required bytes}) => routed.add({'isPhone': isPhone, 'bytes': bytes}),
        discoverer: discoverer,
        connectionFactory: (_) => connection,
      );

      expect(await runtime.ensureConnection(), isTrue);
      expect(transport.connected, isTrue);
      // startNecklace itself is gated by mayEmitAudio (matches upstream: BLE
      // may connect regardless of consent, but capture does not start).
      expect(await runtime.startNecklace(), isFalse);
      expect(routed, isEmpty);
    });

    test('no reconnect after a manual disconnect', () async {
      await runtime.ensureConnection();
      runtime.disconnect();
      expect(adapter.bleConnected, isFalse);
      expect(await runtime.ensureConnection(), isFalse);
    });
  });

  group('EllaUpstreamCaptureRuntime phone-mic path', () {
    test('startPhoneMic routes admitted frames as pcm-source (isPhone=true)', () async {
      final micRecorder = FakeMicRecorderService();
      final routed = <Map<String, Object?>>[];
      final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
      adapter.replaceSession('account-a');
      final runtime = EllaUpstreamCaptureRuntime(
        adapter: adapter,
        routeAudio: ({required isPhone, required bytes}) => routed.add({'isPhone': isPhone, 'bytes': bytes}),
        micRecorder: micRecorder,
      );

      expect(await runtime.startPhoneMic(), isTrue);
      expect(micRecorder.started, isTrue);
      micRecorder.emit(Uint8List.fromList([9, 9]));
      expect(routed, hasLength(1));
      expect(routed.single['isPhone'], isTrue);
    });

    test('phone and necklace are mutually exclusive through the runtime', () async {
      final micRecorder = FakeMicRecorderService();
      final device = _fakeOmiDevice();
      final transport = FakeDeviceTransport(device.id);
      final connection = OmiDeviceConnection(device, transport);
      final discoverer = FakeDeviceDiscoverer(DeviceDiscoveryResult(devices: [device]));
      final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
      adapter.replaceSession('account-a');
      final runtime = EllaUpstreamCaptureRuntime(
        adapter: adapter,
        routeAudio: ({required isPhone, required bytes}) {},
        micRecorder: micRecorder,
        discoverer: discoverer,
        connectionFactory: (_) => connection,
      );

      await runtime.ensureConnection();
      expect(await runtime.startNecklace(), isTrue);
      // Phone mic hands off from the necklace — never both live.
      expect(await runtime.startPhoneMic(), isTrue);
      expect(runtime.liveCaptureSource, UpstreamLiveSource.phone);
    });

    test('stopPhoneMic after finish leaves the runtime free to start again', () async {
      final micRecorder = FakeMicRecorderService();
      final routed = <Map<String, Object?>>[];
      final adapter = EllaUpstreamCaptureAdapter(mayEmitAudio: () => true, enabled: true);
      adapter.replaceSession('account-a');
      final runtime = EllaUpstreamCaptureRuntime(
        adapter: adapter,
        routeAudio: ({required isPhone, required bytes}) => routed.add({'isPhone': isPhone, 'bytes': bytes}),
        micRecorder: micRecorder,
      );

      expect(await runtime.startPhoneMic(), isTrue);
      runtime.stopPhoneMic();
      expect(micRecorder.stopped, isTrue);
      expect(runtime.liveCaptureSource, UpstreamLiveSource.none);
      expect(await runtime.startPhoneMic(), isTrue);
    });
  });
}
