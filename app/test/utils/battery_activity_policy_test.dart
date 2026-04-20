import 'package:flutter_test/flutter_test.dart';

import 'package:omi/utils/battery_activity_policy.dart';

void main() {
  group('BatteryActivityPolicy.shouldStartPeriodicBleReconnect', () {
    test('blocks idle reconnect without a saved device', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: false,
          boundDeviceOnly: false,
          pairingActive: false,
          guardianActive: false,
          isConnected: false,
          hasConnectedDevice: false,
        ),
        false,
      );
    });

    test('blocks bound reconnect without a saved device', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: false,
          boundDeviceOnly: true,
          pairingActive: false,
          guardianActive: false,
          isConnected: false,
          hasConnectedDevice: false,
        ),
        false,
      );
    });

    test('allows pairing reconnect without a saved device', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: false,
          boundDeviceOnly: false,
          pairingActive: true,
          guardianActive: false,
          isConnected: false,
          hasConnectedDevice: false,
        ),
        true,
      );
    });

    test('allows guardian reconnect without a saved device', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: false,
          boundDeviceOnly: false,
          pairingActive: false,
          guardianActive: true,
          isConnected: false,
          hasConnectedDevice: false,
        ),
        true,
      );
    });

    test('allows idle reconnect with a saved device', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: true,
          boundDeviceOnly: false,
          pairingActive: false,
          guardianActive: false,
          isConnected: false,
          hasConnectedDevice: false,
        ),
        true,
      );
    });

    test('blocks reconnect while already connected', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: true,
          boundDeviceOnly: false,
          pairingActive: false,
          guardianActive: false,
          isConnected: true,
          hasConnectedDevice: false,
        ),
        false,
      );
    });

    test('blocks reconnect while a connected device is present', () {
      expect(
        BatteryActivityPolicy.shouldStartPeriodicBleReconnect(
          hasSavedDevice: true,
          boundDeviceOnly: false,
          pairingActive: false,
          guardianActive: false,
          isConnected: false,
          hasConnectedDevice: true,
        ),
        false,
      );
    });
  });
}
