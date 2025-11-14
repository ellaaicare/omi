import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import '../fixtures/test_data.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  group('Device Connection Integration Tests', () {
    late DeviceProvider deviceProvider;
    late CaptureProvider captureProvider;

    setUp(() {
      deviceProvider = DeviceProvider();
      captureProvider = CaptureProvider();
      deviceProvider.setProviders(captureProvider);
    });

    tearDown(() {
      deviceProvider.dispose();
      captureProvider.dispose();
    });

    testWidgets('Device connection flow', (WidgetTester tester) async {
      // Initial state: disconnected
      expect(deviceProvider.isConnected, false);
      expect(deviceProvider.connectedDevice, isNull);

      // Simulate device connection
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);

      // Verify device is set
      expect(deviceProvider.connectedDevice, isNotNull);
      expect(deviceProvider.connectedDevice?.id, device.id);
      expect(deviceProvider.pairedDevice, isNotNull);

      // Set connected state
      deviceProvider.setIsConnected(true);
      expect(deviceProvider.isConnected, true);

      await tester.pumpAndSettle();
    });

    testWidgets('Device disconnection flow', (WidgetTester tester) async {
      // Setup: connected device
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      expect(deviceProvider.isConnected, true);
      expect(deviceProvider.connectedDevice, isNotNull);

      // Simulate disconnection
      deviceProvider.onDeviceDisconnected();

      // Verify disconnected state
      expect(deviceProvider.connectedDevice, isNull);
      expect(deviceProvider.isConnected, false);

      await tester.pumpAndSettle();
    });

    testWidgets('Device reconnection flow', (WidgetTester tester) async {
      // Setup: previously connected device
      final device = TestDevices.createTestDevice();

      // First connection
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);
      expect(deviceProvider.isConnected, true);

      // Disconnect
      deviceProvider.onDeviceDisconnected();
      expect(deviceProvider.isConnected, false);

      // Reconnect
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);
      expect(deviceProvider.isConnected, true);
      expect(deviceProvider.connectedDevice?.id, device.id);

      await tester.pumpAndSettle();
    });

    testWidgets('Battery level monitoring', (WidgetTester tester) async {
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      // Initial battery level should be unknown
      expect(deviceProvider.batteryLevel, -1);

      await tester.pumpAndSettle();
    });

    testWidgets('Multiple device switching', (WidgetTester tester) async {
      // Connect first device
      final device1 = TestDevices.createTestDevice(
        id: 'device-1',
        name: 'Omi Device 1',
      );
      await deviceProvider.setConnectedDevice(device1);
      deviceProvider.setIsConnected(true);

      expect(deviceProvider.connectedDevice?.id, 'device-1');

      // Disconnect first device
      deviceProvider.onDeviceDisconnected();

      // Connect second device
      final device2 = TestDevices.createTestDevice(
        id: 'device-2',
        name: 'Omi Device 2',
      );
      await deviceProvider.setConnectedDevice(device2);
      deviceProvider.setIsConnected(true);

      expect(deviceProvider.connectedDevice?.id, 'device-2');

      await tester.pumpAndSettle();
    });
  });
}
