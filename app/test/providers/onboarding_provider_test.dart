import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/device_connection.dart';

class _NoopDeviceService implements IDeviceService {
  @override
  void start() {}

  @override
  Future<void> stop() async {}

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {}

  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async => null;

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {
    subscription.onStatusChanged(DeviceServiceStatus.ready);
  }

  @override
  void unsubscribe(Object context) {}

  @override
  DateTime? getFirstConnectedAt() => null;

  @override
  void setWifiSyncInProgress(bool value) {}

  @override
  Future<void> disconnectDevice() async {}
}

class _FailedTargetConnectDeviceProvider extends DeviceProvider {
  _FailedTargetConnectDeviceProvider()
      : super(deviceService: _NoopDeviceService(), automaticallyReconnectOnReady: false);

  @override
  Future<bool> connectDeviceForCurrentUser(BtDevice device) async => false;
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
}
