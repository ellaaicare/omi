import 'dart:async';

// Test-only platform injection follows the repository's existing provider tests.
// ignore: depend_on_referenced_packages
import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as legacy_preferences;
import 'package:omi/ella/upstream_capture/ella_upstream_device_service_adapter.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/onboarding_provider.dart';
import 'package:omi/services/services.dart' as legacy_services;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart' as upstream_device;
import 'package:omi/upstream_capture/services/devices.dart' as upstream_service;
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart' as upstream_connection;
import 'package:omi/upstream_capture/services/devices/discovery/device_locator.dart' as upstream_locator;

class _OfflineConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async => [ConnectivityResult.none];

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

class _ProductionRouteUpstreamService extends upstream_service.DeviceService {
  _ProductionRouteUpstreamService(this.discovered);

  final List<upstream_device.BtDevice> discovered;
  int discoverCalls = 0;

  @override
  Future<void> discover({String? desirableDeviceId, int timeout = 5}) async {
    discoverCalls++;
    onDevices(discovered);
  }

  @override
  Future<void> stopDiscoverers() async {}

  @override
  upstream_connection.DeviceConnection? connectionFor(String deviceId) => null;
}

upstream_device.BtDevice _device(String name, String id, upstream_device.DeviceType type) {
  return upstream_device.BtDevice(
    name: name,
    id: id,
    type: type,
    rssi: -40,
    locator: upstream_locator.DeviceLocator.bluetooth(deviceId: id),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('flag-ON production provider graph routes ordinary Connect through upstream hardware authority', () async {
    ConnectivityPlatform.instance = _OfflineConnectivityPlatform();
    SharedPreferences.setMockInitialValues({
      'uid': 'owner-a',
      'btDeviceOwnerBinding': 'owner-a',
    });
    await legacy_preferences.SharedPreferencesUtil.init();

    final friend = _device('Friend', 'friend-1', upstream_device.DeviceType.friendPendant);
    final upstream = _ProductionRouteUpstreamService([
      _device('Compass', 'compass-1', upstream_device.DeviceType.fieldy),
      friend,
    ]);
    String? connectedOwner;
    upstream_device.BtDevice? connectedDevice;
    final adapter = EllaUpstreamDeviceServiceAdapter(
      serviceLoader: () async => upstream,
      connect: (ownerId, device) async {
        connectedOwner = ownerId;
        connectedDevice = device;
        return true;
      },
      disconnect: (_) async {},
      connectionOwner: () => connectedOwner,
    );
    await legacy_services.ServiceManager.init(deviceService: adapter);
    legacy_services.ServiceManager.instance().device.start();

    final deviceProvider = DeviceProvider(automaticallyReconnectOnReady: false);
    final onboarding = OnboardingProvider()
      ..setDeviceProvider(deviceProvider)
      ..hasBluetoothPermission = true;
    addTearDown(deviceProvider.dispose);
    addTearDown(onboarding.dispose);
    addTearDown(adapter.stop);

    await onboarding.scanDevices(onShowDialog: () {});
    expect(upstream.discoverCalls, 1);
    expect(onboarding.deviceList.map((device) => device.name), ['Compass', 'Friend']);

    await onboarding.handleTap(
      device: onboarding.deviceList.last,
      isFromOnboarding: false,
    );

    expect(connectedOwner, 'owner-a');
    expect(connectedDevice?.id, friend.id);
    expect(deviceProvider.presentationConnectedDevice?.id, friend.id);
  });
}
