import 'dart:async';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/enums.dart';

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

  void publishConnection(String deviceId, DeviceConnectionState state, {required int connectionGeneration}) {
    for (final subscriber in _subscriptions.values.toList()) {
      subscriber.onDeviceConnectionStateChanged(deviceId, state, connectionGeneration: connectionGeneration);
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
  _RecordingCaptureProvider({this.startGate, this.disconnectGate, this.failuresBeforeStart = 0, this.onDeviceStart});

  final Completer<void>? startGate;
  final Completer<void>? disconnectGate;
  final int failuresBeforeStart;
  final void Function(int attempt)? onDeviceStart;
  int deviceStarts = 0;
  final List<String> disconnectedDeviceIds = [];

  @override
  Future<void> streamDeviceRecording({BtDevice? device}) async {
    deviceStarts++;
    if (deviceStarts <= failuresBeforeStart) {
      updateRecordingState(RecordingState.error);
      onDeviceStart?.call(deviceStarts);
      throw StateError('synthetic necklace setup failure');
    }
    onDeviceStart?.call(deviceStarts);
    await startGate?.future;
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<bool> handleRecordingDeviceDisconnected(String deviceId) async {
    disconnectedDeviceIds.add(deviceId);
    await disconnectGate?.future;
    return true;
  }
}

Future<void> bindRememberedDeviceForCurrentTestAuthority(
  BtDevice device, {
  String uid = 'test-user',
  String profileBindingId = 'test-profile',
}) async {
  await SharedPreferencesUtil.init();
  final preferences = SharedPreferencesUtil()..uid = uid;
  await preferences.saveString('aiConsentProfileBindingId', profileBindingId);
  await preferences.btDeviceSet(device);
  await preferences.btDeviceOwnerBindingSet('$uid\u001f$profileBindingId');
}

void main() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('com.omi/floating_control_bar'),
      (_) async => null,
    );
    SharedPreferences.setMockInitialValues({});
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Ignore if already initialized by another test.
    }
  });

  tearDownAll(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('com.omi/floating_control_bar'),
      null,
    );
  });

  setUp(() async {
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil.resetProcessLocalAuthorityStateForTesting();
    final preferences = SharedPreferencesUtil()..uid = '';
    await preferences.saveString('aiConsentProfileBindingId', '');
    await preferences.btDeviceSet(BtDevice.empty());
    await preferences.btDeviceOwnerBindingSet('');
  });

  test('empty remembered-device sentinels never present as a paired necklace', () async {
    final provider = DeviceProvider(deviceService: _FakeDeviceService(DeviceServiceStatus.ready));
    addTearDown(provider.dispose);

    provider.pairedDevice = BtDevice.empty();
    provider.connectedDevice = BtDevice.empty();

    expect(provider.presentationPairedDevice, isNull);
    expect(provider.presentationConnectedDevice, isNull);
    expect(provider.presentationIsConnected, isFalse);

    await provider.getDeviceInfo();
    expect(provider.pairedDevice, isNull, reason: 'storage sentinels are not Home presentation state');
  });

  test('a legacy saved necklace stays a Home confirmation candidate without reconnecting or capture', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'legacy-necklace', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'legacy-user';
    await preferences.saveString('aiConsentProfileBindingId', 'legacy-profile');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('');
    var scans = 0;
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 1),
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(provider.presentationPairedDevice, isNull);
    expect(provider.legacyUntrustedDeviceCandidate?.id, necklace.id);
    expect(scans, 0, reason: 'legacy storage is never an implicit reconnect authority');
    expect(capture.deviceStarts, 0, reason: 'capture cannot begin before Home confirmation');
  });

  test('Home confirmation binds a legacy necklace before reconnect and capture', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'legacy-necklace', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'legacy-user';
    await preferences.saveString('aiConsentProfileBindingId', 'legacy-profile');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('');
    final capture = _RecordingCaptureProvider();
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 1),
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    expect(await provider.confirmLegacyNecklaceForCurrentAuthority(reason: 'test Home confirmation'), isTrue);
    for (var attempt = 0; attempt < 100 && !provider.presentationIsConnected; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(preferences.btDeviceOwnerBinding, 'legacy-user\u001flegacy-profile');
    expect(scans, greaterThanOrEqualTo(1));
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(capture.deviceStarts, 1);
  });

  test('a legacy confirmation cannot bind or capture after account authority drift', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'legacy-necklace', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'account-a';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('');
    final writeStarted = Completer<void>();
    final allowWrite = Completer<void>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      rememberedDeviceWriter: (device) async {
        if (device.id == necklace.id && !writeStarted.isCompleted) {
          writeStarted.complete();
          await allowWrite.future;
        }
        await preferences.btDeviceSet(device);
      },
      scanConnector: () async => necklace,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final confirmation = provider.confirmLegacyNecklaceForCurrentAuthority(reason: 'test drift');
    await writeStarted.future.timeout(const Duration(seconds: 1));
    preferences.uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    allowWrite.complete();

    expect(await confirmation, isFalse);
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(preferences.btDeviceOwnerBinding, isEmpty);
    expect(capture.deviceStarts, 0, reason: 'account B cannot inherit account A confirmation');
  });

  test('a legacy confirmation cannot bind or capture after profile authority drift', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'legacy-necklace', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'same-account';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('');
    final writeStarted = Completer<void>();
    final allowWrite = Completer<void>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      rememberedDeviceWriter: (device) async {
        if (device.id == necklace.id && !writeStarted.isCompleted) {
          writeStarted.complete();
          await allowWrite.future;
        }
        await preferences.btDeviceSet(device);
      },
      scanConnector: () async => necklace,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final confirmation = provider.confirmLegacyNecklaceForCurrentAuthority(reason: 'test profile drift');
    await writeStarted.future.timeout(const Duration(seconds: 1));
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    allowWrite.complete();

    expect(await confirmation, isFalse);
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(preferences.btDeviceOwnerBinding, isEmpty);
    expect(capture.deviceStarts, 0, reason: 'a replacement profile cannot inherit a prior confirmation');
  });

  test('clearing an active device restores only the exact bound necklace for Home', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final provider = DeviceProvider(deviceService: _FakeDeviceService(DeviceServiceStatus.ready));
    addTearDown(provider.dispose);

    await provider.setConnectedDevice(null);

    expect(provider.presentationPairedDevice?.id, necklace.id);
    expect(provider.presentationConnectedDevice, isNull);
  });

  group('battery throttling', () {
    late DeviceProvider provider;
    late int notifyCount;

    setUp(() {
      provider = DeviceProvider();
      notifyCount = 0;
      provider.addListener(() => notifyCount++);
    });

    tearDown(() => provider.dispose());

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
      final result = provider.updateBatteryLevelForTesting(52, now: now.add(const Duration(minutes: 5)));

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
      final result = provider.updateBatteryLevelForTesting(45, now: now.add(const Duration(minutes: 1)));

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies after 15 minutes even if delta < 5%', () {
      final now = DateTime.now();

      // First reading
      provider.updateBatteryLevelForTesting(50, now: now);
      expect(notifyCount, 1);

      // Small change but 15 minutes elapsed - should notify
      final result = provider.updateBatteryLevelForTesting(51, now: now.add(const Duration(minutes: 15)));

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies when crossing 20% threshold downward', () {
      final now = DateTime.now();

      // Start above 20%
      provider.updateBatteryLevelForTesting(25, now: now);
      expect(notifyCount, 1);

      // Cross below 20% (only 6% change, but crosses threshold)
      final result = provider.updateBatteryLevelForTesting(19, now: now.add(const Duration(minutes: 1)));

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('notifies when crossing 20% threshold upward', () {
      final now = DateTime.now();

      // Start below 20%
      provider.updateBatteryLevelForTesting(15, now: now);
      expect(notifyCount, 1);

      // Cross above 20% (only 6% change, but crosses threshold)
      final result = provider.updateBatteryLevelForTesting(21, now: now.add(const Duration(minutes: 1)));

      expect(result, true);
      expect(notifyCount, 2);
    });

    test('does not notify for small changes that do not cross 20% threshold', () {
      final now = DateTime.now();

      // Start at 25%
      provider.updateBatteryLevelForTesting(25, now: now);
      expect(notifyCount, 1);

      // Small change staying above 20% - should NOT notify
      final result = provider.updateBatteryLevelForTesting(23, now: now.add(const Duration(minutes: 1)));

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
      final result = provider.updateBatteryLevelForTesting(50, now: now.add(const Duration(minutes: 1)));

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

  test('connected presentation requires the concrete necklace used to start capture', () {
    final provider = DeviceProvider();
    addTearDown(provider.dispose);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);

    provider.isConnected = true;
    expect(provider.presentationIsConnected, isFalse);

    provider.connectedDevice = necklace;
    expect(provider.presentationIsConnected, isTrue);

    provider.isConnected = false;
    expect(provider.presentationIsConnected, isFalse);
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

    provider.onDeviceConnectionStateChanged('necklace-1', DeviceConnectionState.connected, connectionGeneration: 1);
    service.publish(DeviceServiceStatus.stop);
    await Future<void>.delayed(const Duration(milliseconds: 150));

    expect(resolverCalls, 0);
    expect(capture.deviceStarts, 0);
    expect(provider.presentationIsConnected, isFalse);
  });

  test('an unowned necklace callback cannot attach or start capture for the current account', () async {
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = '';
    await preferences.saveString('aiConsentProfileBindingId', '');
    await preferences.btDeviceSet(BtDevice.empty());
    await preferences.btDeviceOwnerBindingSet('');
    var resolverCalls = 0;
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      connectionResolver: (_) async {
        resolverCalls++;
        return BtDevice(name: 'Ella', id: 'legacy-necklace', type: DeviceType.omi, rssi: -30);
      },
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(
      'legacy-necklace',
      DeviceConnectionState.connected,
      connectionGeneration: 1,
    );
    await Future<void>.delayed(const Duration(milliseconds: 150));

    expect(resolverCalls, 0);
    expect(capture.deviceStarts, 0);
    expect(provider.connectedDevice, isNull);
    expect(SharedPreferencesUtil().btDevice.id, isEmpty);
  });

  test('a same-owner authority refresh preserves the active necklace capture', () async {
    final necklace = BtDevice(name: 'Ella', id: 'same-owner-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();
    expect(capture.recordingState, RecordingState.deviceRecord);

    SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
    await Future<void>.delayed(Duration.zero);
    await pumpEventQueue();

    expect(provider.connectedDevice?.id, necklace.id);
    expect(provider.presentationIsConnected, isTrue);
    expect(capture.recordingState, RecordingState.deviceRecord);
  });

  test('authority reconciliation cannot notify after the device provider is disposed', () async {
    final necklace = BtDevice(name: 'Ella', id: 'dispose-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      automaticallyReconnectOnReady: false,
    );
    var notificationsAfterInvalidation = 0;
    provider.addListener(() => notificationsAfterInvalidation++);

    SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
    provider.dispose();
    await pumpEventQueue();

    expect(notificationsAfterInvalidation, 0);
  });

  test('a stale disconnect session cannot tear down a later reconnect to the same necklace', () async {
    final necklace = BtDevice(name: 'Ella', id: 'shared-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace, uid: 'account-a', profileBindingId: 'profile-a');
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    service.publishConnection(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();
    expect(provider.presentationIsConnected, isTrue);

    final preferences = SharedPreferencesUtil()..uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('account-b\u001fprofile-b');
    SharedPreferencesUtil().invalidateAccountAuthorityForTransition();
    await pumpEventQueue();

    await provider.confirmConnectedDeviceForCurrentAuthority(necklace);
    service.publishConnection(necklace.id, DeviceConnectionState.connected, connectionGeneration: 2);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();
    final disconnectsBeforeStaleCallback = capture.disconnectedDeviceIds.length;
    expect(provider.presentationIsConnected, isTrue);

    service.publishConnection(necklace.id, DeviceConnectionState.disconnected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 600));
    await pumpEventQueue();

    expect(provider.presentationIsConnected, isTrue);
    expect(capture.disconnectedDeviceIds, hasLength(disconnectsBeforeStaleCallback));
  });

  test('device connection cannot start necklace capture while phone owns audio', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider()..updateRecordingState(RecordingState.record);
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();

    expect(capture.deviceStarts, 0);
    expect(capture.recordingState, RecordingState.record);
    expect(provider.connectedDevice?.id, necklace.id);
  });

  test('necklace capture resumes when phone releases audio without reconnecting', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider()..updateRecordingState(RecordingState.record);
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();
    expect(capture.deviceStarts, 0);

    capture.updateRecordingState(RecordingState.stop);
    await pumpEventQueue();

    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.connectedDevice?.id, necklace.id);
  });

  test('necklace retry re-defers when phone reacquires audio', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    late final _RecordingCaptureProvider capture;
    capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      onDeviceStart: (attempt) {
        if (attempt == 1) capture.updateRecordingState(RecordingState.record);
      },
    );
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();

    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, RecordingState.record);

    capture.updateRecordingState(RecordingState.stop);
    await pumpEventQueue();

    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.connectedDevice?.id, necklace.id);
  });

  test('in-flight connected resolution cannot repopulate after stop', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final resolution = Completer<BtDevice?>();
    final resolverEntered = Completer<void>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) {
        resolverEntered.complete();
        return resolution.future;
      },
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged('necklace-1', DeviceConnectionState.connected, connectionGeneration: 1);
    await resolverEntered.future;
    service.publish(DeviceServiceStatus.stop);
    resolution.complete(necklace);
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

  test('automatic reconnect stops after a bounded number of failed scans', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scanCalls++;
        return null;
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test bounded reconnect');
    for (var attempt = 0; attempt < 20 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, 3);
    expect(provider.automaticReconnectAttempts, 3);
    expect(provider.automaticReconnectExhausted, isTrue);
    expect(provider.isConnecting, isFalse);
  });

  test('automatic reconnect recovers from scan exceptions and exhausts', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scanCalls++;
        throw StateError('synthetic scan failure');
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test throwing reconnect');
    for (var attempt = 0; attempt < 20 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, 3);
    expect(provider.automaticReconnectAttempts, 3);
    expect(provider.automaticReconnectExhausted, isTrue);
    expect(provider.isConnecting, isFalse);
  });

  test('automatic reconnect keeps a low-power watchdog after the initial burst', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async => ++scanCalls > 3 ? necklace : null,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
      automaticReconnectCooldown: const Duration(milliseconds: 5),
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test persistent reconnect');
    for (var attempt = 0; attempt < 40 && !provider.presentationIsConnected; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, greaterThanOrEqualTo(4));
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.automaticReconnectAttempts, 0);
    expect(provider.automaticReconnectExhausted, isFalse);
  });

  test('foreground resume immediately retries an exhausted saved necklace', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'resume-user';
    await preferences.saveString('aiConsentProfileBindingId', 'resume-profile');
    await preferences.btDeviceSet(BtDevice.empty());
    await preferences.btDeviceOwnerBindingSet('');
    addTearDown(() async {
      await preferences.btDeviceSet(BtDevice.empty());
      await preferences.btDeviceOwnerBindingSet('');
    });
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async => ++scanCalls > 3 ? necklace : null,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
      automaticReconnectCooldown: const Duration(hours: 1),
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test resume reconnect');
    for (var attempt = 0; attempt < 20 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }
    expect(provider.automaticReconnectExhausted, isTrue);

    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('resume-user\u001fresume-profile');
    provider.didChangeAppLifecycleState(AppLifecycleState.resumed);
    for (var attempt = 0; attempt < 100 && !provider.presentationIsConnected; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, greaterThanOrEqualTo(4));
    expect(provider.presentationIsConnected, isTrue);
  });

  test('automatic reconnect clears a partially assigned device and exhausts after storage failure', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    var scanCalls = 0;
    var storageCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scanCalls++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async {
        storageCalls++;
        throw StateError('synthetic storage probe failure');
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test partial reconnect rollback');
    for (var attempt = 0; attempt < 20 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, 3);
    expect(storageCalls, 3);
    expect(provider.connectedDevice, isNull);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.automaticReconnectAttempts, 3);
    expect(provider.automaticReconnectExhausted, isTrue);
    expect(provider.isConnecting, isFalse);
  });

  test('automatic reconnect preserves a connection committed before its scan future throws', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    late DeviceProvider provider;
    provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        provider.connectedDevice = necklace;
        provider.setIsConnected(true);
        throw StateError('synthetic late scan failure');
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test successful event racing scan failure');
    await Future<void>.delayed(const Duration(milliseconds: 5));

    expect(provider.connectedDevice, same(necklace));
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.automaticReconnectAttempts, 0);
    expect(provider.automaticReconnectExhausted, isFalse);
    expect(provider.isConnecting, isFalse);
  });

  test('connected callback publishes device and connection atomically before reconnect scan failure', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final scanEntered = Completer<void>();
    final scanResult = Completer<BtDevice?>();
    final storageEntered = Completer<void>();
    final storageResult = Completer<List<int>>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () {
        scanEntered.complete();
        return scanResult.future;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) {
        storageEntered.complete();
        return storageResult.future;
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    await provider.periodicConnect('test callback publication ordering');
    await scanEntered.future;

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await storageEntered.future.timeout(const Duration(seconds: 1));

    expect(provider.connectedDevice, same(necklace));
    expect(provider.presentationIsConnected, isTrue);

    scanResult.completeError(StateError('synthetic reconnect scan failed after callback publication'));
    await pumpEventQueue();

    expect(provider.connectedDevice, same(necklace));
    expect(provider.presentationIsConnected, isTrue);

    storageResult.complete(const []);
    await pumpEventQueue();

    expect(provider.connectedDevice, same(necklace));
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.automaticReconnectAttempts, 0);
    expect(provider.automaticReconnectExhausted, isFalse);
  });

  test('connected callback retries necklace capture and ignores optional setup failure', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider(failuresBeforeStart: 1);
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) => throw StateError('synthetic optional storage failure'),
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();

    expect(provider.connectedDevice, same(necklace));
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.isConnecting, isFalse);
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
  });

  test('a connected necklace is persisted before optional metadata work so startup can reconnect it', () async {
    await SharedPreferencesUtil.init();
    await SharedPreferencesUtil().btDeviceSet(BtDevice.empty());
    addTearDown(() => SharedPreferencesUtil().btDeviceSet(BtDevice.empty()));
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(
      name: 'Ella necklace',
      id: 'remembered-necklace-1',
      type: DeviceType.omi,
      rssi: -30,
      firmwareRevision: '1.0.0',
    );
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();

    expect(SharedPreferencesUtil().btDevice.id, necklace.id);
    expect(provider.presentationPairedDevice?.id, necklace.id);
  });

  test('a delayed remembered-necklace write cannot restore account A after account B takes authority', () async {
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'account-a';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    await preferences.btDeviceSet(BtDevice.empty());
    addTearDown(() => preferences.btDeviceSet(BtDevice.empty()));
    final writeStarted = Completer<void>();
    final allowWrite = Completer<void>();
    final necklace = BtDevice(
      name: 'Ella necklace',
      id: 'account-a-necklace',
      type: DeviceType.omi,
      rssi: -30,
      firmwareRevision: '1.0.0',
    );
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      rememberedDeviceWriter: (device) async {
        if (device.id == necklace.id && !writeStarted.isCompleted) {
          writeStarted.complete();
          await allowWrite.future;
        }
        await preferences.btDeviceSet(device);
      },
    );
    addTearDown(provider.dispose);

    unawaited(provider.confirmConnectedDeviceForCurrentAuthority(necklace));
    await writeStarted.future.timeout(const Duration(seconds: 1));

    preferences.uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    await preferences.btDeviceSet(BtDevice.empty());
    allowWrite.complete();
    await pumpEventQueue();

    expect(
      preferences.btDevice.id,
      isEmpty,
      reason: 'a stale account callback must not repopulate the replacement account',
    );
  });

  test('an account transition quiesces an in-flight remembered reconnect before it can start capture for B', () async {
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil()..uid = 'account-a';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-a');
    final necklace = BtDevice(
      name: 'Ella necklace',
      id: 'account-a-necklace',
      type: DeviceType.omi,
      rssi: -30,
      firmwareRevision: '1.0.0',
    );
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('account-a\u001fprofile-a');
    addTearDown(() async {
      await preferences.btDeviceSet(BtDevice.empty());
      await preferences.btDeviceOwnerBindingSet('');
    });
    final service = _FakeDeviceService(DeviceServiceStatus.init);
    final scanStarted = Completer<void>();
    final allowScanResult = Completer<BtDevice?>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () {
        if (!scanStarted.isCompleted) scanStarted.complete();
        return allowScanResult.future;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 1),
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    service.publish(DeviceServiceStatus.ready);
    await scanStarted.future.timeout(const Duration(seconds: 1));

    preferences.uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    allowScanResult.complete(necklace);
    await pumpEventQueue();

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await pumpEventQueue();

    expect(provider.presentationIsConnected, isFalse);
    expect(provider.connectedDevice, isNull);
    expect(
      capture.deviceStarts,
      0,
      reason: 'audio captured under account A must not begin after account B takes authority',
    );
  });

  test('a persisted necklace reconnects after an app-provider restart without revisiting Settings', () async {
    await SharedPreferencesUtil.init();
    final necklace = BtDevice(
      name: 'Ella necklace',
      id: 'restart-necklace',
      type: DeviceType.omi,
      rssi: -30,
      firmwareRevision: '1.0.0',
    );
    final preferences = SharedPreferencesUtil()..uid = 'restart-user';
    await preferences.saveString('aiConsentProfileBindingId', 'restart-profile');
    await preferences.btDeviceSet(BtDevice.empty());
    await preferences.btDeviceOwnerBindingSet('');
    final firstProvider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
    );
    await firstProvider.confirmConnectedDeviceForCurrentAuthority(necklace);
    expect(SharedPreferencesUtil().btDevice.id, necklace.id);
    firstProvider.dispose();

    var reconnectScans = 0;
    final restartedProvider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async {
        reconnectScans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
    );
    addTearDown(restartedProvider.dispose);
    for (var attempt = 0; attempt < 100 && !restartedProvider.presentationIsConnected; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(reconnectScans, greaterThanOrEqualTo(1));
    expect(restartedProvider.presentationConnectedDevice?.id, necklace.id);
  });

  test('device service restart waits for exact necklace capture teardown', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.init);
    final disconnectGate = Completer<void>();
    final capture = _RecordingCaptureProvider(disconnectGate: disconnectGate);
    var reconnectScans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        reconnectScans++;
        return null;
      },
    )
      ..setProviders(capture)
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    service.publish(DeviceServiceStatus.stop);
    await pumpEventQueue();
    expect(capture.disconnectedDeviceIds, [necklace.id]);

    service.publish(DeviceServiceStatus.ready);
    await pumpEventQueue();
    expect(reconnectScans, 0);

    disconnectGate.complete();
    await pumpEventQueue();
    expect(reconnectScans, 1);
  });

  test('only a later ready generation reconnects the retained bound device', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await SharedPreferencesUtil.init();
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
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

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(scanCalls, 0);
    expect(capture.deviceStarts, 0);

    service.publish(DeviceServiceStatus.ready);
    await pumpEventQueue();
    expect(scanCalls, 1);
    expect(provider.pairedDevice?.id, necklace.id);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 2);
    await Future<void>.delayed(const Duration(milliseconds: 150));
    expect(capture.deviceStarts, 1);

    service.publish(DeviceServiceStatus.stop);
    startGate.complete();
    await pumpEventQueue();
    expect(provider.presentationIsConnected, isFalse);
  });
}
