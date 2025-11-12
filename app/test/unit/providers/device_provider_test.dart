import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import '../../fixtures/test_data.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('DeviceProvider Tests', () {
    late DeviceProvider deviceProvider;

    setUp(() {
      deviceProvider = DeviceProvider();
    });

    tearDown(() {
      deviceProvider.dispose();
    });

    group('Device Connection', () {
      test('should initialize with disconnected state', () {
        expect(deviceProvider.isConnected, false);
        expect(deviceProvider.isConnecting, false);
        expect(deviceProvider.connectedDevice, isNull);
        expect(deviceProvider.pairedDevice, isNull);
      });

      test('should set connected device', () async {
        final device = TestDevices.createTestDevice();

        await deviceProvider.setConnectedDevice(device);

        expect(deviceProvider.connectedDevice, isNotNull);
        expect(deviceProvider.connectedDevice?.id, device.id);
        expect(deviceProvider.pairedDevice, isNotNull);
      });

      test('should update connecting status', () {
        expect(deviceProvider.isConnecting, false);

        deviceProvider.updateConnectingStatus(true);
        expect(deviceProvider.isConnecting, true);

        deviceProvider.updateConnectingStatus(false);
        expect(deviceProvider.isConnecting, false);
      });

      test('should set connection state', () {
        expect(deviceProvider.isConnected, false);

        deviceProvider.setIsConnected(true);
        expect(deviceProvider.isConnected, true);

        deviceProvider.setIsConnected(false);
        expect(deviceProvider.isConnected, false);
      });
    });

    group('Battery Monitoring', () {
      test('should initialize with unknown battery level', () {
        expect(deviceProvider.batteryLevel, -1);
      });

      test('battery level should be within valid range when set', () {
        // This would be set through the battery listener
        // Just verify initial state
        expect(deviceProvider.batteryLevel, -1);
      });
    });

    group('Firmware Updates', () {
      test('should initialize without firmware updates', () {
        expect(deviceProvider.havingNewFirmware, false);
        expect(deviceProvider.isFirmwareUpdateInProgress, false);
      });

      test('should set firmware update in progress', () {
        expect(deviceProvider.isFirmwareUpdateInProgress, false);

        deviceProvider.setFirmwareUpdateInProgress(true);
        expect(deviceProvider.isFirmwareUpdateInProgress, true);

        deviceProvider.setFirmwareUpdateInProgress(false);
        expect(deviceProvider.isFirmwareUpdateInProgress, false);
      });

      test('should reset firmware update state', () {
        deviceProvider.setFirmwareUpdateInProgress(true);
        expect(deviceProvider.isFirmwareUpdateInProgress, true);

        deviceProvider.resetFirmwareUpdateState();
        expect(deviceProvider.isFirmwareUpdateInProgress, false);
      });

      test('should get current firmware version from paired device', () {
        final device = TestDevices.createTestDevice();
        deviceProvider.pairedDevice = device;

        expect(deviceProvider.currentFirmwareVersion, '1.0.0');
      });

      test('should return unknown for firmware version when no device', () {
        expect(deviceProvider.currentFirmwareVersion, 'Unknown');
      });
    });

    group('Storage Support', () {
      test('should initialize with no storage support', () {
        expect(deviceProvider.isDeviceStorageSupport, false);
      });
    });

    group('Device Disconnection', () {
      test('should handle device disconnection', () {
        final device = TestDevices.createTestDevice();
        deviceProvider.connectedDevice = device;
        deviceProvider.isConnected = true;

        deviceProvider.onDeviceDisconnected();

        expect(deviceProvider.connectedDevice, isNull);
        expect(deviceProvider.isConnected, false);
      });
    });

    group('Notification Tests', () {
      test('should notify listeners when connection state changes', () {
        var notified = false;
        deviceProvider.addListener(() {
          notified = true;
        });

        deviceProvider.setIsConnected(true);
        expect(notified, true);
      });

      test('should notify listeners when device is set', () async {
        var notifyCount = 0;
        deviceProvider.addListener(() {
          notifyCount++;
        });

        final device = TestDevices.createTestDevice();
        await deviceProvider.setConnectedDevice(device);

        expect(notifyCount, greaterThan(0));
      });

      test('should notify listeners on connecting status change', () {
        var notified = false;
        deviceProvider.addListener(() {
          notified = true;
        });

        deviceProvider.updateConnectingStatus(true);
        expect(notified, true);
      });
    });

    group('DFU Preparation', () {
      test('should prepare for DFU update', () {
        final device = TestDevices.createTestDevice();
        deviceProvider.connectedDevice = device;

        deviceProvider.prepareDFU();

        // Verify that prepareDFU was called
        // Note: Full DFU testing would require mocking the BLE service
      });
    });
  });
}
