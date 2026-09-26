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
  int ensureConnectionCalls = 0;
  int disconnectCalls = 0;
  Object? disconnectError;
  bool nativeSessionRetained = false;
  Completer<DeviceConnection?>? ensureConnectionGate;
  Completer<void>? disconnectGate;
  Future<void> Function()? cancelPendingConnectionHook;
  int cancelPendingConnectionCalls = 0;
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
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    ensureConnectionCalls++;
    final gate = ensureConnectionGate;
    if (gate != null) return gate.future;
    return null;
  }

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
  Future<void> cancelPendingConnection() async {
    cancelPendingConnectionCalls++;
    await cancelPendingConnectionHook?.call();
  }

  @override
  Future<void> disconnectDevice() async {
    disconnectCalls++;
    await disconnectGate?.future;
    final error = disconnectError;
    if (error != null) throw error;
    nativeSessionRetained = false;
  }
}

class _RecordingCaptureProvider extends CaptureProvider {
  _RecordingCaptureProvider({
    this.startGate,
    this.disconnectGate,
    this.failuresBeforeStart = 0,
    this.onDeviceStart,
    this.forcedDiagnosticFailure,
    this.requireConsent = false,
  });

  final Completer<void>? startGate;
  final Completer<void>? disconnectGate;
  final int failuresBeforeStart;
  final void Function(int attempt)? onDeviceStart;
  final CaptureDiagnosticFailure? forcedDiagnosticFailure;
  final bool requireConsent;
  int deviceStarts = 0;
  CaptureDiagnosticFailure? simulatedFailure;
  final List<String> disconnectedDeviceIds = [];

  @override
  CaptureDiagnostics get captureDiagnostics => forcedDiagnosticFailure == null && simulatedFailure == null
      ? super.captureDiagnostics
      : CaptureDiagnostics(
          phase: CaptureDiagnosticPhase.failed,
          failure: forcedDiagnosticFailure ?? simulatedFailure!,
        );

  @override
  Future<void> streamDeviceRecording({BtDevice? device}) async {
    deviceStarts++;
    if (requireConsent && !SharedPreferencesUtil().aiConsentAccepted) {
      simulatedFailure = CaptureDiagnosticFailure.consentUnavailable;
      updateRecordingState(RecordingState.error);
      return;
    }
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
  await preferences.btDeviceOwnerBindingSet(uid);
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
    await preferences.saveBool('aiConsentAccepted', true);
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

  test('a legacy UID plus consent-profile binding migrates to UID-only ownership', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'migrated-necklace', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'same-user';
    await preferences.saveString('aiConsentProfileBindingId', 'old-profile');
    await preferences.btDeviceSet(necklace);
    await preferences.btDeviceOwnerBindingSet('same-user\u001fold-profile');

    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      automaticallyReconnectOnReady: false,
    );
    addTearDown(provider.dispose);

    expect(provider.presentationPairedDevice?.id, necklace.id);
    await pumpEventQueue();
    expect(preferences.btDeviceOwnerBinding, 'same-user');
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

    expect(preferences.btDeviceOwnerBinding, 'legacy-user');
    expect(scans, greaterThanOrEqualTo(1));
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(capture.deviceStarts, 1);
  });

  test('Home reconnect repairs capture when BLE is connected but necklace audio is not active', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider()..updateRecordingState(RecordingState.error);
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider.connectedDevice = necklace;
    provider.pairedDevice = necklace;
    provider.setIsConnected(true);

    final ready = await provider.connectDeviceForCurrentUser(necklace);

    expect(ready, isTrue);
    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('Home reconnect attempts to hydrate a stale connected session before failing', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider()..updateRecordingState(RecordingState.error);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final provider = DeviceProvider(
      deviceService: service,
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..pairedDevice = necklace
      ..setIsConnected(true);

    final ready = await provider.connectDeviceForCurrentUser(necklace);

    expect(ready, isFalse);
    expect(service.ensureConnectionCalls, greaterThanOrEqualTo(1),
        reason: 'a stale BLE flag must not bypass transport hydration');
    expect(capture.deviceStarts, 0);
  });

  test('Home reconnect awaits connection and necklace capture before reporting success', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
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
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final ready = await provider.connectDeviceForCurrentUser(necklace);

    expect(ready, isTrue);
    expect(scans, 1);
    expect(capture.deviceStarts, 1);
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.isConnecting, isFalse);
  });

  test('declined consent permits BLE connection but prevents necklace capture', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    await SharedPreferencesUtil().saveBool('aiConsentAccepted', false);
    final capture = _RecordingCaptureProvider(requireConsent: true);
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async => necklace,
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);

    expect(provider.presentationIsConnected, isTrue);
    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, isNot(RecordingState.deviceRecord));
  });

  test('Home reconnect fails closed when account authority changes during connection', () async {
    final necklace = BtDevice(name: 'Ella necklace', id: 'bound-necklace', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace, uid: 'account-a', profileBindingId: 'profile-a');
    final scanStarted = Completer<void>();
    final scanResult = Completer<BtDevice?>();
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () {
        scanStarted.complete();
        return scanResult.future;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final reconnect = provider.connectDeviceForCurrentUser(necklace);
    await scanStarted.future;
    final preferences = SharedPreferencesUtil()..uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    scanResult.complete(necklace);

    expect(await reconnect, isFalse);
    await pumpEventQueue();
    expect(capture.deviceStarts, 0);
    expect(provider.presentationIsConnected, isFalse);
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

  test('a same-UID consent profile refresh does not invalidate explicit necklace binding', () async {
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

    expect(await confirmation, isTrue);
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(preferences.btDeviceOwnerBinding, 'same-account');
    expect(capture.deviceStarts, 1, reason: 'capture authority remains enforced independently at capture start');
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

  test('explicit connect reports BLE success separately while retrying capture', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
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
    expect(capture.recordingState, RecordingState.error);

    final recovered = await provider.connectDeviceForCurrentUser(necklace);
    await pumpEventQueue();

    expect(recovered, isTrue);
    expect(service.disconnectCalls, 1);
    expect(scans, 1);
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
  });

  test('connected silent necklace automatically replaces its stale BLE session', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.recordingState != RecordingState.deviceRecord; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, 1);
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.connectedCaptureRecoveryAttempts, 0, reason: 'live audio resets the bounded recovery budget');
  });

  test('mid-stream physical audio stall automatically replaces its stale BLE session', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.recordingState != RecordingState.deviceRecord; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
    expect(capture.deviceStarts, 1);

    capture.updateRecordingState(RecordingState.error);
    for (var attempt = 0; attempt < 100 && capture.deviceStarts < 2; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, 1);
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('stopped capture cancels delayed connected-silent recovery without spending its budget', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectedCaptureRecoveryDelay: const Duration(milliseconds: 50),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.recordingState != RecordingState.deviceRecord; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
    expect(capture.deviceStarts, 1);

    capture.updateRecordingState(RecordingState.error);
    capture.updateRecordingState(RecordingState.stop);
    await Future<void>.delayed(const Duration(milliseconds: 80));
    await pumpEventQueue();

    expect(service.disconnectCalls, 0, reason: 'an intentionally stopped capture must not reset BLE');
    expect(scans, 0);
    expect(provider.connectedCaptureRecoveryAttempts, 0, reason: 'cancelled recovery must not spend its budget');
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('connected silent necklace recovery stops after its bounded retry budget', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 10,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      maxConnectedCaptureRecoveryAttempts: 2,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && provider.connectedCaptureRecoveryAttempts < 2; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
    await pumpEventQueue();

    expect(service.disconnectCalls, 2);
    expect(scans, 2);
    expect(capture.deviceStarts, 3, reason: 'the initial attempt plus two fresh BLE sessions are allowed');
    expect(capture.recordingState, RecordingState.error);
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.connectedCaptureRecoveryAttempts, 2);

    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(service.disconnectCalls, 2, reason: 'a persistently silent device must not enter a reconnect loop');
  });

  test('connected silent recovery continues through a transient reconnect scan miss', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return scans == 1 ? null : necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.recordingState != RecordingState.deviceRecord; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, greaterThanOrEqualTo(2));
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('connected silent recovery continues through a transient reconnect scan exception', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        if (scans == 1) throw StateError('synthetic recovery scan failure');
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.recordingState != RecordingState.deviceRecord; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, greaterThanOrEqualTo(2));
    expect(capture.deviceStarts, 2);
    expect(capture.recordingState, RecordingState.deviceRecord);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('connected silent recovery stops after bounded owner-only reconnect scan misses', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return null;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 2,
      automaticReconnectCooldown: const Duration(minutes: 1),
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, 3, reason: 'one fresh-session scan plus two bounded owner-only reconnect scans are allowed');
    expect(capture.deviceStarts, 1);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.automaticReconnectAttempts, 2);
    expect(provider.automaticReconnectExhausted, isTrue);

    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(scans, 3, reason: 'the reconnect loop must remain stopped throughout its cooldown');
  });

  test('connected silent recovery stops after bounded owner-only reconnect scan exceptions', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 1,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        throw StateError('synthetic persistent recovery scan failure');
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 2,
      automaticReconnectCooldown: const Duration(minutes: 1),
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    expect(service.disconnectCalls, 1);
    expect(scans, 3, reason: 'one fresh-session scan plus two bounded owner-only reconnect scans are allowed');
    expect(capture.deviceStarts, 1);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.automaticReconnectAttempts, 2);
    expect(provider.automaticReconnectExhausted, isTrue);

    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(scans, 3, reason: 'throwing reconnect scans must remain stopped throughout cooldown');
  });

  test('account authority change cancels pending connected-silent recovery', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace, uid: 'account-a', profileBindingId: 'profile-a');
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 10,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    );
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      connectedCaptureRecoveryDelay: const Duration(milliseconds: 50),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    provider.onDeviceConnectionStateChanged(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    for (var attempt = 0; attempt < 100 && capture.deviceStarts == 0; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
    await pumpEventQueue();

    final preferences = SharedPreferencesUtil()..uid = 'account-b';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-b');
    await Future<void>.delayed(const Duration(milliseconds: 80));
    await pumpEventQueue();

    expect(service.disconnectCalls, 0, reason: 'stale account A recovery cannot mutate account B BLE state');
    expect(scans, 0);
    expect(capture.deviceStarts, 1);
    expect(provider.presentationIsConnected, isFalse);
  });

  test('transcription-only retry preserves the healthy BLE session', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider(
      forcedDiagnosticFailure: CaptureDiagnosticFailure.transcriptionUnavailable,
    )..updateRecordingState(RecordingState.error);
    final provider = DeviceProvider(
      deviceService: service,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider.connectedDevice = necklace;
    provider.pairedDevice = necklace;
    provider.setIsConnected(true);

    final recovered = await provider.connectDeviceForCurrentUser(necklace);
    await pumpEventQueue();

    expect(recovered, isTrue);
    expect(service.disconnectCalls, 0);
    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, RecordingState.deviceRecord);
  });

  test('explicit fresh-session retry resets BLE after phone diagnostics replace the necklace failure', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..nativeSessionRetained = true;
    final capture = _RecordingCaptureProvider()..updateRecordingState(RecordingState.stop);
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..connectedDevice = necklace
      ..pairedDevice = necklace
      ..setIsConnected(true);

    final recovered = await provider.connectDeviceForCurrentUser(
      necklace,
      requireFreshSession: true,
    );
    await pumpEventQueue();

    expect(recovered, isTrue);
    expect(service.disconnectCalls, 1);
    expect(service.nativeSessionRetained, isFalse);
    expect(scans, 1);
    expect(capture.deviceStarts, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('explicit fresh-session retry cannot reset a device outside the current UID pairing', () async {
    final necklaceA = BtDevice(name: 'Ella A', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    final necklaceB = BtDevice(name: 'Ella B', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklaceA);
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..nativeSessionRetained = true;
    final provider = DeviceProvider(
      deviceService: service,
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    );
    addTearDown(provider.dispose);
    provider
      ..connectedDevice = necklaceA
      ..pairedDevice = necklaceA
      ..setIsConnected(true);

    expect(
      await provider.connectDeviceForCurrentUser(necklaceB, requireFreshSession: true),
      isFalse,
    );
    expect(service.disconnectCalls, 0);
    expect(service.ensureConnectionCalls, 0);
    expect(provider.presentationConnectedDevice?.id, necklaceA.id);
    expect(provider.presentationPairedDevice?.id, necklaceA.id);
  });

  test('failed native disconnect joins capture teardown and leaves explicit retry usable', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final disconnectGate = Completer<void>();
    final service = _FakeDeviceService(DeviceServiceStatus.init)
      ..disconnectError = StateError('synthetic native disconnect failure')
      ..nativeSessionRetained = true;
    final capture = _RecordingCaptureProvider(
      disconnectGate: disconnectGate,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    )..updateRecordingState(RecordingState.error);
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return service.nativeSessionRetained ? null : necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      deviceCaptureRetryDelay: Duration.zero,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..connectedDevice = necklace
      ..pairedDevice = necklace
      ..setIsConnected(true);
    service.publish(DeviceServiceStatus.ready);
    await pumpEventQueue();
    expect(scans, 0, reason: 'the initial ready event must preserve the connected session');

    var firstRetryCompleted = false;
    final firstRetry = provider.connectDeviceForCurrentUser(necklace).whenComplete(() => firstRetryCompleted = true);
    await pumpEventQueue();

    expect(capture.disconnectedDeviceIds, [necklace.id]);
    expect(firstRetryCompleted, isFalse, reason: 'native failure must not release the capture teardown early');
    expect(provider.isConnecting, isTrue);

    disconnectGate.complete();
    expect(await firstRetry, isFalse);
    expect(provider.isConnecting, isFalse);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.presentationConnectedDevice, isNull);
    expect(service.nativeSessionRetained, isTrue, reason: 'the failed native disconnect retains the stale session');
    expect(scans, 0, reason: 'a failed reset must not start a hidden reconnect attempt');

    service.publish(DeviceServiceStatus.ready);
    provider.didChangeAppLifecycleState(AppLifecycleState.resumed);
    await pumpEventQueue();

    expect(scans, 0, reason: 'service-ready and app-resume must not reuse a retained native session');
    expect(service.ensureConnectionCalls, 0);
    expect(capture.deviceStarts, 0);
    expect(service.disconnectCalls, 1, reason: 'automatic recovery must wait for explicit user retry');

    service.disconnectError = null;
    final recovered = await provider.connectDeviceForCurrentUser(necklace);
    await pumpEventQueue();

    expect(recovered, isTrue);
    expect(service.disconnectCalls, 2, reason: 'the next retry must prove native disconnect before reconnecting');
    expect(service.nativeSessionRetained, isFalse);
    expect(scans, 1);
    expect(capture.deviceStarts, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.isConnecting, isFalse);
  });

  test('timed-out fresh-session reset cannot report or apply a late disconnect as success', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final disconnectGate = Completer<void>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)
      ..disconnectGate = disconnectGate
      ..nativeSessionRetained = true;
    final capture = _RecordingCaptureProvider(
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    )..updateRecordingState(RecordingState.error);
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectionAttemptTimeout: const Duration(milliseconds: 20),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..connectedDevice = necklace
      ..pairedDevice = necklace
      ..setIsConnected(true);

    expect(await provider.connectDeviceForCurrentUser(necklace), isFalse);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.presentationConnectedDevice, isNull);
    expect(provider.presentationPairedDevice?.id, necklace.id);
    expect(provider.connectionAttemptFailed, isTrue);
    expect(provider.isConnecting, isFalse);
    expect(scans, 0);

    disconnectGate.complete();
    await pumpEventQueue();

    expect(provider.presentationIsConnected, isFalse, reason: 'late reset completion must remain superseded');
    expect(provider.presentationConnectedDevice, isNull);
    expect(scans, 0, reason: 'the timed-out attempt must not begin a late scan');
    expect(capture.deviceStarts, 0);

    service.disconnectGate = null;
    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);
    expect(service.disconnectCalls, 2, reason: 'explicit retry must prove a fresh native session');
    expect(scans, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
  });

  test('fresh connection committed before timeout survives slow post-connect capture startup', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final captureStartGate = Completer<void>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..nativeSessionRetained = true;
    final capture = _RecordingCaptureProvider(
      startGate: captureStartGate,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.physicalAudioUnavailable,
    )..updateRecordingState(RecordingState.error);
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectionAttemptTimeout: const Duration(milliseconds: 20),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..connectedDevice = necklace
      ..pairedDevice = necklace
      ..setIsConnected(true);

    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);
    expect(service.disconnectCalls, 1);
    expect(scans, 1);
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.connectionAttemptFailed, isFalse);
    expect(provider.isConnecting, isFalse);

    captureStartGate.complete();
    await pumpEventQueue();

    expect(provider.presentationIsConnected, isTrue, reason: 'late setup must retain the committed fresh session');
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(capture.recordingState, RecordingState.deviceRecord);
  });

  test('timed-out connect clears stale isConnecting and permits the next attempt', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final firstConnection = Completer<DeviceConnection?>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..ensureConnectionGate = firstConnection;
    final capture = _RecordingCaptureProvider();
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectionAttemptTimeout: const Duration(milliseconds: 20),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    expect(await provider.connectDeviceForCurrentUser(necklace), isFalse);
    expect(provider.isConnecting, isFalse);
    expect(provider.connectionAttemptFailed, isTrue);

    service.ensureConnectionGate = null;
    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);
    firstConnection.complete(null);
    await pumpEventQueue();

    expect(scans, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.isConnecting, isFalse);
    expect(provider.connectionAttemptFailed, isFalse);
  });

  test('explicit selection cancels an in-flight automatic reconnect before connecting', () async {
    final necklace = BtDevice(name: 'Friend', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final automaticScan = Completer<BtDevice?>();
    var scans = 0;
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () {
        scans++;
        return scans == 1 ? automaticScan.future : Future.value(necklace);
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      reconnectionInterval: const Duration(hours: 1),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    service.cancelPendingConnectionHook = () async {
      if (!automaticScan.isCompleted) automaticScan.complete(null);
    };

    unawaited(provider.periodicConnect('test automatic reconnect', boundDeviceOnly: true));
    await pumpEventQueue();
    expect(provider.isConnecting, isTrue);

    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);

    expect(service.cancelPendingConnectionCalls, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.isConnecting, isFalse);
  });

  test('failed explicit replacement restores the remembered necklace', () async {
    final necklaceA = BtDevice(name: 'Ella A', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    final necklaceB = BtDevice(name: 'Ella B', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklaceA);
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async => null,
      automaticallyReconnectOnReady: false,
    );
    addTearDown(provider.dispose);

    expect(await provider.connectDeviceForCurrentUser(necklaceB), isFalse);

    expect(provider.presentationConnectedDevice, isNull);
    expect(provider.presentationPairedDevice?.id, necklaceA.id);
    expect(provider.connectionAttemptFailed, isTrue);
  });

  test('failed first explicit connection leaves no false paired necklace', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    final preferences = SharedPreferencesUtil()..uid = 'test-user';
    await preferences.btDeviceSet(BtDevice.empty());
    await preferences.btDeviceOwnerBindingSet('');
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      scanConnector: () async => null,
      automaticallyReconnectOnReady: false,
    );
    addTearDown(provider.dispose);

    expect(await provider.connectDeviceForCurrentUser(necklace), isFalse);

    expect(provider.presentationConnectedDevice, isNull);
    expect(provider.presentationPairedDevice, isNull);
    expect(provider.connectionAttemptFailed, isTrue);
  });

  test('ambient resume rejects connected A while replacement B is pending', () async {
    final necklaceA = BtDevice(name: 'Ella A', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    final necklaceB = BtDevice(name: 'Ella B', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklaceA);
    final connectionGate = Completer<DeviceConnection?>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..ensureConnectionGate = connectionGate;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async => null,
      automaticallyReconnectOnReady: false,
    )
      ..connectedDevice = necklaceA
      ..pairedDevice = necklaceA
      ..isConnected = true;
    addTearDown(provider.dispose);

    final replacement = provider.connectDeviceForCurrentUser(necklaceB);
    for (var attempt = 0; attempt < 20 && service.ensureConnectionCalls == 0; attempt++) {
      await Future<void>.delayed(Duration.zero);
    }

    expect(provider.presentationConnectedDevice?.id, necklaceA.id);
    expect(provider.presentationPairedDevice?.id, necklaceB.id);
    expect(provider.canResumeAmbientCaptureFor(necklaceA), isFalse);

    connectionGate.complete(null);
    expect(await replacement, isFalse);
  });

  test('ambient resume rejects a device after durable unpair starts', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final provider = DeviceProvider(
      deviceService: _FakeDeviceService(DeviceServiceStatus.ready),
      automaticallyReconnectOnReady: false,
    )
      ..connectedDevice = necklace
      ..pairedDevice = necklace
      ..isConnected = true;
    addTearDown(provider.dispose);

    expect(provider.canResumeAmbientCaptureFor(necklace), isTrue);

    provider.updateConnectingStatus(true);
    expect(
      provider.canResumeAmbientCaptureFor(necklace),
      isTrue,
      reason: 'a stale presentation flag without a live attempt cannot strand ambient capture',
    );

    await SharedPreferencesUtil().btDeviceSet(BtDevice.empty());

    expect(provider.presentationConnectedDevice?.id, necklace.id, reason: 'native unpair may still be in flight');
    expect(provider.canResumeAmbientCaptureFor(necklace), isFalse);
  });

  test('a newer explicit connect owns presentation when an older attempt completes late', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final firstConnection = Completer<DeviceConnection?>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..ensureConnectionGate = firstConnection;
    final capture = _RecordingCaptureProvider();
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final staleAttempt = provider.connectDeviceForCurrentUser(necklace);
    for (var attempt = 0; attempt < 20 && service.ensureConnectionCalls == 0; attempt++) {
      await Future<void>.delayed(Duration.zero);
    }
    service.ensureConnectionGate = null;

    expect(await provider.connectDeviceForCurrentUser(necklace), isTrue);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.isConnecting, isFalse);

    firstConnection.complete(null);
    expect(await staleAttempt, isFalse);
    await pumpEventQueue();

    expect(scans, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.isConnecting, isFalse);
    expect(provider.connectionAttemptFailed, isFalse);
  });

  test('a timed-out target cannot report another connected necklace as success', () async {
    final necklaceA = BtDevice(name: 'Ella A', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    final necklaceB = BtDevice(name: 'Ella B', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklaceB);
    final pendingConnection = Completer<DeviceConnection?>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..ensureConnectionGate = pendingConnection;
    final capture = _RecordingCaptureProvider();
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) async => necklaceB,
      storageListResolver: (_) async => const [],
      connectionAttemptTimeout: const Duration(milliseconds: 20),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);
    provider
      ..connectedDevice = necklaceA
      ..pairedDevice = necklaceA
      ..setIsConnected(true);

    expect(await provider.connectDeviceForCurrentUser(necklaceB), isFalse);
    expect(provider.presentationConnectedDevice?.id, necklaceA.id);
    expect(provider.presentationPairedDevice?.id, necklaceA.id);
    expect(provider.connectionAttemptFailed, isTrue);
    expect(provider.isConnecting, isFalse);

    pendingConnection.complete(null);
    await pumpEventQueue();

    expect(provider.presentationConnectedDevice?.id, necklaceA.id);
    expect(provider.presentationPairedDevice?.id, necklaceA.id);
  });

  test('an older timeout cannot invalidate a newer in-flight connection attempt', () async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final firstConnection = Completer<DeviceConnection?>();
    final secondConnection = Completer<DeviceConnection?>();
    final service = _FakeDeviceService(DeviceServiceStatus.ready)..ensureConnectionGate = firstConnection;
    final capture = _RecordingCaptureProvider();
    var scans = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () async {
        scans++;
        return necklace;
      },
      connectionResolver: (_) async => necklace,
      storageListResolver: (_) async => const [],
      connectionAttemptTimeout: const Duration(milliseconds: 100),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    final staleAttempt = provider.connectDeviceForCurrentUser(necklace);
    for (var attempt = 0; attempt < 20 && service.ensureConnectionCalls == 0; attempt++) {
      await Future<void>.delayed(Duration.zero);
    }
    await Future<void>.delayed(const Duration(milliseconds: 30));
    service.ensureConnectionGate = secondConnection;
    final currentAttempt = provider.connectDeviceForCurrentUser(necklace);
    for (var attempt = 0; attempt < 20 && service.ensureConnectionCalls < 2; attempt++) {
      await Future<void>.delayed(Duration.zero);
    }

    expect(await staleAttempt, isFalse);
    expect(provider.isConnecting, isTrue);
    expect(provider.connectionAttemptFailed, isFalse);

    secondConnection.complete(null);
    expect(await currentAttempt, isTrue);
    await pumpEventQueue();

    expect(scans, 1);
    expect(provider.presentationConnectedDevice?.id, necklace.id);
    expect(provider.presentationIsConnected, isTrue);
    expect(provider.isConnecting, isFalse);
    expect(provider.connectionAttemptFailed, isFalse);
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

  test('automatic reconnect keeps retrying after a timed-out scan and ignores its late result', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final firstScan = Completer<BtDevice?>();
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      scanConnector: () {
        scanCalls++;
        return scanCalls == 1 ? firstScan.future : Future<BtDevice?>.value();
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
      connectionAttemptTimeout: const Duration(milliseconds: 10),
      automaticallyReconnectOnReady: false,
    );
    addTearDown(provider.dispose);

    await provider.periodicConnect('test timed-out automatic reconnect');
    for (var attempt = 0; attempt < 100 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, 3);
    expect(provider.automaticReconnectAttempts, 3);
    expect(provider.automaticReconnectExhausted, isTrue);
    expect(provider.isConnecting, isFalse);

    firstScan.complete(necklace);
    await pumpEventQueue();

    expect(provider.presentationIsConnected, isFalse);
    expect(provider.presentationConnectedDevice, isNull);
    expect(scanCalls, 3, reason: 'the late first scan remains fenced and cannot restart the exhausted loop');
  });

  test('automatic reconnect fences a native connected callback that resolves after its attempt times out', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final firstScan = Completer<BtDevice?>();
    final resolution = Completer<BtDevice?>();
    final resolverEntered = Completer<void>();
    final capture = _RecordingCaptureProvider();
    var scanCalls = 0;
    final provider = DeviceProvider(
      deviceService: service,
      connectionResolver: (_) {
        if (!resolverEntered.isCompleted) resolverEntered.complete();
        return resolution.future;
      },
      scanConnector: () {
        scanCalls++;
        return scanCalls == 1 ? firstScan.future : Future<BtDevice?>.value();
      },
      reconnectionInterval: const Duration(milliseconds: 2),
      maxAutomaticReconnectAttempts: 3,
      connectionAttemptTimeout: const Duration(milliseconds: 200),
      automaticallyReconnectOnReady: false,
    )..setProviders(capture);
    addTearDown(provider.dispose);
    addTearDown(capture.dispose);

    await provider.periodicConnect('test timed-out native callback');
    service.publishConnection(necklace.id, DeviceConnectionState.connected, connectionGeneration: 1);
    await resolverEntered.future.timeout(const Duration(seconds: 1));

    for (var attempt = 0; attempt < 200 && !provider.automaticReconnectExhausted; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }

    expect(scanCalls, 3);
    expect(provider.automaticReconnectExhausted, isTrue);
    expect(provider.isConnecting, isFalse);

    resolution.complete(necklace);
    firstScan.complete();
    await pumpEventQueue();

    expect(capture.deviceStarts, 0);
    expect(provider.presentationIsConnected, isFalse);
    expect(provider.presentationConnectedDevice, isNull);
    expect(scanCalls, 3, reason: 'the late native callback cannot revive an exhausted reconnect attempt');
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

  test('connected callback does not hammer transcription after a definitive capture failure', () async {
    final service = _FakeDeviceService(DeviceServiceStatus.ready);
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    await bindRememberedDeviceForCurrentTestAuthority(necklace);
    final capture = _RecordingCaptureProvider(
      failuresBeforeStart: 3,
      forcedDiagnosticFailure: CaptureDiagnosticFailure.transcriptionUnavailable,
    );
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

    expect(capture.deviceStarts, 1);
    expect(capture.recordingState, RecordingState.error);
    expect(provider.presentationIsConnected, isTrue);
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
