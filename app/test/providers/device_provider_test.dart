import 'dart:async';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/services.dart';

class _TestConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async {
    return [ConnectivityResult.none];
  }

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

class _FakeDeviceService implements IDeviceService {
  _FakeDeviceService([this.status = DeviceServiceStatus.init]);

  DeviceServiceStatus status;
  final Map<Object, IDeviceServiceSubsciption> _subscriptions = {};

  void publish(DeviceServiceStatus next) {
    status = next;
    for (final subscriber in _subscriptions.values.toList()) {
      subscriber.onStatusChanged(next);
    }
  }

  @override
  void start() => publish(DeviceServiceStatus.ready);

  @override
  Future<void> stop() async => publish(DeviceServiceStatus.stop);

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {}

  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async => null;

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {
    _subscriptions[context] = subscription;
    subscription.onStatusChanged(status);
  }

  @override
  void unsubscribe(Object context) => _subscriptions.remove(context);

  @override
  DateTime? getFirstConnectedAt() => null;

  @override
  void setWifiSyncInProgress(bool value) {}

  @override
  Future<void> disconnectDevice() async {}
}

class _RecordingCaptureProvider extends CaptureProvider {
  _RecordingCaptureProvider({this.startGate});

  final Completer<void>? startGate;
  int deviceStarts = 0;
  int deviceDisconnects = 0;
  String? disconnectedDeviceId;

  @override
  Future<void> streamDeviceRecording({BtDevice? device}) async {
    deviceStarts++;
    await startGate?.future;
  }

  @override
  Future<bool> handleRecordingDeviceDisconnected(String deviceId) async {
    deviceDisconnects++;
    disconnectedDeviceId = deviceId;
    return true;
  }
}

void main() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({});
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Ignore if already initialized by another test.
    }
  });

  group('battery throttling', () {
    late DeviceProvider provider;
    late int notifyCount;

    setUp(() {
      provider = DeviceProvider();
      notifyCount = 0;
      provider.addListener(() => notifyCount++);
    });

    test('notifies on first battery reading', () {
      final result = provider.updateBatteryLevelForTesting(50);

      expect(result, true);
      expect(notifyCount, 1);
      expect(provider.batteryLevel, 50);
    });

    test('does not notify for small changes (<5%) within 15 minutes', () {
      final now = DateTime.now();

      // First reading - should notify
      provider.updateBatteryLevelForTesting(50, now: now);
      expect(notifyCount, 1);

      // Small change (2%) within 15 minutes - should NOT notify
      final result = provider.updateBatteryLevelForTesting(
        52,
        now: now.add(const Duration(minutes: 5)),
      );

      expect(result, false);
      expect(notifyCount, 1); // No additional notification
      expect(provider.batteryLevel, 52); // Level is still updated
    });

    test('notifies when delta >= 5%', () {
      final now = DateTime.now();

      // First reading
      provider.updateBatteryLevelForTesting(50, now: now);
      expect(notifyCount, 1);

      // 5% change - should notify
      final result = provider.updateBatteryLevelForTesting(
        45,
        now: now.add(const Duration(minutes: 1)),
      );

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies after 15 minutes even if delta < 5%', () {
      final now = DateTime.now();

      // First reading
      provider.updateBatteryLevelForTesting(50, now: now);
      expect(notifyCount, 1);

      // Small change but 15 minutes elapsed - should notify
      final result = provider.updateBatteryLevelForTesting(
        51,
        now: now.add(const Duration(minutes: 15)),
      );

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies when crossing 20% threshold downward', () {
      final now = DateTime.now();

      // Start above 20%
      provider.updateBatteryLevelForTesting(25, now: now);
      expect(notifyCount, 1);

      // Cross below 20% (only 6% change, but crosses threshold)
      final result = provider.updateBatteryLevelForTesting(
        19,
        now: now.add(const Duration(minutes: 1)),
      );

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies when crossing 20% threshold upward', () {
      final now = DateTime.now();

      // Start below 20%
      provider.updateBatteryLevelForTesting(15, now: now);
      expect(notifyCount, 1);

      // Cross above 20% (only 6% change, but crosses threshold)
      final result = provider.updateBatteryLevelForTesting(
        21,
        now: now.add(const Duration(minutes: 1)),
      );

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('does not notify for small changes that do not cross 20% threshold', () {
      final now = DateTime.now();

      // Start at 25%
      provider.updateBatteryLevelForTesting(25, now: now);
      expect(notifyCount, 1);

      // Small change staying above 20% - should NOT notify
      final result = provider.updateBatteryLevelForTesting(
        23,
        now: now.add(const Duration(minutes: 1)),
      );

      expect(result, false);
      expect(notifyCount, 1);
    });

    test('resetBatteryThrottlingForTesting resets state', () {
      final now = DateTime.now();

      // First reading
      provider.updateBatteryLevelForTesting(50, now: now);
      expect(notifyCount, 1);

      // Reset
      provider.resetBatteryThrottlingForTesting();

      // Now same value should trigger notification again (as if first reading)
      final result = provider.updateBatteryLevelForTesting(
        50,
        now: now.add(const Duration(minutes: 1)),
      );

      expect(result, true);
      expect(notifyCount, 2);
    });
  });

  test('device service stop clears stale connected presentation before restart', () {
    final provider = DeviceProvider();
    addTearDown(provider.dispose);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    provider
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true
      ..isConnecting = true;

    provider.onStatusChanged(DeviceServiceStatus.stop);

    expect(provider.presentationIsConnected, isFalse);
    expect(provider.connectedDevice, isNull);
    expect(provider.pairedDevice, isNull);
    expect(provider.isConnecting, isFalse);
  });

  test('physical necklace disconnect tears down capture and clears presentation', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider();
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final provider = DeviceProvider(deviceService: service)
      ..setProviders(capture)
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    // The production connection callback reaches this method after its
    // debounce. This test environment has no Firebase Crashlytics app, so
    // allow the unrelated post-teardown analytics call to fail afterward.
    try {
      await provider.onDeviceDisconnected();
    } catch (_) {}

    expect(capture.deviceDisconnects, 1);
    expect(capture.disconnectedDeviceId, necklace.id);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.connectedDevice, isNull);
  });

  test('queued connected callback is cancelled by device-service stop', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    var resolverCalls = 0;
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async {
        resolverCalls++;
        return BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
      },
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged('necklace-1', DeviceConnectionState.connected);
    service.publish(DeviceServiceStatus.stop);
    await Future<void>.delayed(const Duration(milliseconds: 150));

    expect(resolverCalls, 0);
    expect(capture.deviceStarts, 0);
    expect(provider.presentationIsConnected, isFalse);
  });

  test('in-flight connected resolution cannot repopulate after stop', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final resolution = Completer<BtDevice?>();
    final resolverEntered = Completer<void>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) {
        resolverEntered.complete();
        return resolution.future;
      },
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged('necklace-1', DeviceConnectionState.connected);
    await resolverEntered.future;
    service.publish(DeviceServiceStatus.stop);
    resolution.complete(BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30));
    await pumpEventQueue();

    expect(capture.deviceStarts, 0);
    expect(provider.connectedDevice, isNull);
    expect(provider.pairedDevice, isNull);
    expect(provider.presentationIsConnected, isFalse);
  });

  test('in-flight reconnect scan cannot commit after stop', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final scan = Completer<BtDevice?>();
    final scanEntered = Completer<void>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () {
        scanEntered.complete();
        return scan.future;
      },
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final reconnect = provider.scanAndConnectToDevice();
    await scanEntered.future;
    service.publish(DeviceServiceStatus.stop);
    scan.complete(BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30));
    await reconnect;

    expect(capture.deviceStarts, 0);
    expect(provider.connectedDevice, isNull);
    expect(provider.pairedDevice, isNull);
    expect(provider.presentationIsConnected, isFalse);
  });

  test('only a later ready generation reconnects the retained bound device', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await SharedPreferencesUtil.init();
    await SharedPreferencesUtil().btDeviceSet(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.init);
    var scanCalls = 0;
    final startGate = Completer<void>();
    final capture = _RecordingCaptureProvider(startGate: startGate);
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scanCalls++;
        return null;
      },
      connectionResolver: (_) async => necklace,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(scanCalls, 0);
    expect(capture.deviceStarts, 0);

    service.publish(DeviceServiceStatus.ready);
    await pumpEventQueue();
    expect(scanCalls, 1);
    expect(provider.pairedDevice?.id, necklace.id);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(capture.deviceStarts, 1);

    service.publish(DeviceServiceStatus.stop);
    startGate.complete();
    await pumpEventQueue();
    expect(provider.presentationIsConnected, isFalse);
  });
}
