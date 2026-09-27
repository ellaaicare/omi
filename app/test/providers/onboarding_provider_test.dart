import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';

class _NoopDeviceService implements IDeviceService {
  _NoopDeviceService({this.discoveredDevices = const [], this.calls});

  final List<BtDevice> discoveredDevices;
  final List<String>? calls;
  final Map<Object, IDeviceServiceSubsciption> _subscriptions = {};
  int ensureConnectionCalls = 0;

  @override
  void start() {}

  @override
  Future<void> stop() async {}

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    calls?.add('discover');
    for (final subscriber in _subscriptions.values.toList()) {
      subscriber.onDevices(discoveredDevices);
    }
  }

  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    ensureConnectionCalls++;
    return null;
  }

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {
    _subscriptions[context] = subscription;
    subscription.onStatusChanged(DeviceServiceStatus.ready);
  }

  @override
  void unsubscribe(Object context) => _subscriptions.remove(context);

  @override
  DateTime? getFirstConnectedAt() => null;

  @override
  void setWifiSyncInProgress(bool value) {}

  @override
  Future<void> cancelPendingConnection() async {}

  @override
  Future<void> disconnectDevice() async {}
}

class _FailedTargetConnectDeviceProvider extends DeviceProvider {
  _FailedTargetConnectDeviceProvider()
      : super(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);

  @override
  Future<bool> connectDeviceForCurrentUser(BtDevice device, {bool requireFreshSession = false}) async => false;
}

class _OrderedScanDeviceProvider extends DeviceProvider {
  _OrderedScanDeviceProvider(IDeviceService deviceService, this.calls)
      : super(deviceService: deviceService, automaticallyReconnectOnReady: false);

  final List<String> calls;

  @override
  Future<void> prepareForExplicitDeviceSelection() async {
    calls.add('prepare');
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(const {});
  });

  test('failed target pairing preserves another live device', () async {
    final necklaceA = BtDevice(name: 'Ella A', id: 'necklace-a', type: DeviceType.omi, rssi: -30);
    final necklaceB = BtDevice(name: 'Ella B', id: 'necklace-b', type: DeviceType.omi, rssi: -30);
    final device = _FailedTargetConnectDeviceProvider()
      ..connectedDevice = necklaceA
      ..pairedDevice = necklaceA
      ..setIsConnected(true);
    final onboarding = OnboardingProvider()
      ..setDeviceProvider(device)
      ..deviceList = [necklaceB]
      ..foundDevicesMap = {necklaceB.id: necklaceB};
    addTearDown(device.dispose);

    await onboarding.handleTap(device: necklaceB, isFromOnboarding: false);

    expect(device.presentationIsConnected, isTrue);
    expect(device.presentationConnectedDevice?.id, necklaceA.id);
    expect(device.presentationPairedDevice?.id, necklaceA.id);
    expect(onboarding.isConnected, isTrue);
    expect(onboarding.deviceId, necklaceA.id);
    expect(onboarding.isClicked, isFalse);
  });

  test('phone source picker discovers without connecting before a user tap', () async {
    await SharedPreferencesUtil.init();
    await SharedPreferencesUtil().saveEllaCaptureSource('phone');
    addTearDown(() => SharedPreferencesUtil().saveEllaCaptureSource(''));
    final calls = <String>[];
    final necklace = BtDevice(name: 'Friend', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final service = _NoopDeviceService(discoveredDevices: [necklace], calls: calls);
    final device = _OrderedScanDeviceProvider(service, calls);
    final onboarding = OnboardingProvider(deviceService: service)
      ..setDeviceProvider(device)
      ..hasBluetoothPermission = true;
    addTearDown(device.dispose);
    addTearDown(onboarding.dispose);

    await onboarding.scanDevices(onShowDialog: () {});

    expect(calls, ['prepare', 'discover']);
    expect(onboarding.deviceList.map((device) => device.id), [necklace.id]);
    expect(service.ensureConnectionCalls, 0);
    expect(device.presentationIsConnected, isFalse);
  });
}
