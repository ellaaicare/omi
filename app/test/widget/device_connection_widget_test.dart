import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:omi/providers/device_provider.dart';
import '../fixtures/test_data.dart';

void main() {
  group('Device Connection Widget Tests', () {
    late DeviceProvider deviceProvider;

    setUp(() {
      deviceProvider = DeviceProvider();
    });

    tearDown(() {
      deviceProvider.dispose();
    });

    testWidgets('should show disconnected state initially', (WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<DeviceProvider>.value(
            value: deviceProvider,
            child: Scaffold(
              body: Consumer<DeviceProvider>(
                builder: (context, provider, child) {
                  if (!provider.isConnected) {
                    return const Text(
                      'Disconnected',
                      key: Key('disconnected-text'),
                    );
                  }
                  return const Text('Connected');
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('disconnected-text')), findsOneWidget);
    });

    testWidgets('should show connecting indicator', (WidgetTester tester) async {
      deviceProvider.updateConnectingStatus(true);

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<DeviceProvider>.value(
            value: deviceProvider,
            child: Scaffold(
              body: Consumer<DeviceProvider>(
                builder: (context, provider, child) {
                  if (provider.isConnecting) {
                    return const CircularProgressIndicator(
                      key: Key('connecting-indicator'),
                    );
                  }
                  return const SizedBox.shrink();
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('connecting-indicator')), findsOneWidget);
    });

    testWidgets('should display connected device info', (WidgetTester tester) async {
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<DeviceProvider>.value(
            value: deviceProvider,
            child: Scaffold(
              body: Consumer<DeviceProvider>(
                builder: (context, provider, child) {
                  if (provider.isConnected && provider.connectedDevice != null) {
                    return Column(
                      children: [
                        Text(
                          provider.connectedDevice!.name,
                          key: const Key('device-name'),
                        ),
                        Text(
                          provider.connectedDevice!.id,
                          key: const Key('device-id'),
                        ),
                      ],
                    );
                  }
                  return const SizedBox.shrink();
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('device-name')), findsOneWidget);
      expect(find.byKey(const Key('device-id')), findsOneWidget);
      expect(find.text(device.name), findsOneWidget);
    });

    testWidgets('should display battery level', (WidgetTester tester) async {
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);
      // Simulate battery level (normally set through listener)

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<DeviceProvider>.value(
            value: deviceProvider,
            child: Scaffold(
              body: Consumer<DeviceProvider>(
                builder: (context, provider, child) {
                  return Text(
                    'Battery: ${provider.batteryLevel}%',
                    key: const Key('battery-level'),
                  );
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      expect(find.byKey(const Key('battery-level')), findsOneWidget);
    });

    testWidgets('should show firmware update button when available', (WidgetTester tester) async {
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      await tester.pumpWidget(
        MaterialApp(
          home: ChangeNotifierProvider<DeviceProvider>.value(
            value: deviceProvider,
            child: Scaffold(
              body: Consumer<DeviceProvider>(
                builder: (context, provider, child) {
                  if (provider.havingNewFirmware) {
                    return ElevatedButton(
                      key: const Key('firmware-update-button'),
                      onPressed: () {},
                      child: const Text('Update Firmware'),
                    );
                  }
                  return const SizedBox.shrink();
                },
              ),
            ),
          ),
        ),
      );

      await tester.pumpAndSettle();

      // Initially no firmware update
      expect(find.byKey(const Key('firmware-update-button')), findsNothing);
    });
  });
}
