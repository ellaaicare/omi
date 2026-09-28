// ellaaicare/ella-ai#1280 RUN-009: with the upstream-capture flag ON, native BLE discovery
// listed no necklace on two phones and two necklace generations. iOS `OmiBleManager.didDiscover`
// forwards the scan-time name and the advertised service UUIDs unfiltered; the drop happened in
// this file's admission classifier, which only recognized Omi necklaces by an advertised service
// UUID. Production necklaces advertise as bare 'Friend' or 'Omi' with NO service UUID, so every
// one of them was silently rejected before a GATT connection was ever attempted.
//
// Confirmed identical in BasedHardware/omi at a74e4cfca376a7c8212687a23d9354e7e755671d (main) —
// upstream carries the same bug — so this is patched here in upstream style (see
// UPSTREAM_PATCHES.md) rather than re-vendored from a fix that doesn't exist yet.
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/devices/discovery/native_bluetooth_discoverer.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';

void main() {
  group('NativeBluetoothDiscoverer necklace admission (ellaaicare/ella-ai#1280 RUN-009)', () {
    test('admits a production-shaped Friend necklace with no advertised service UUIDs', () {
      final peripheral = BlePeripheral(
        uuid: '0000-aaaa-0001',
        name: 'Friend',
        rssi: -58,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isFriendPendant(peripheral), isFalse);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);

      final device = NativeBluetoothDiscoverer.peripheralToDevice(peripheral);
      expect(device.type, DeviceType.omi);
      expect(device.name, 'Friend');
      expect(device.id, '0000-aaaa-0001');
    });

    test('admits a production-shaped Omi necklace with no advertised service UUIDs', () {
      final peripheral = BlePeripheral(
        uuid: '0000-aaaa-0002',
        name: 'Omi',
        rssi: -61,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);

      final device = NativeBluetoothDiscoverer.peripheralToDevice(peripheral);
      expect(device.type, DeviceType.omi);
      expect(device.name, 'Omi');
    });

    test('admits an Omi-prefixed name variant with no advertised service UUIDs', () {
      final peripheral = BlePeripheral(
        uuid: '0000-aaaa-0003',
        name: 'Omi-4F21',
        rssi: -66,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.peripheralToDevice(peripheral).type, DeviceType.omi);
    });

    test('admits a candidate that advertises the Omi service UUID regardless of name', () {
      final peripheral = BlePeripheral(
        uuid: '0000-aaaa-0004',
        name: 'Custom Wearable',
        rssi: -70,
        serviceUuids: [omiServiceUuid],
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.peripheralToDevice(peripheral).type, DeviceType.omi);
    });

    test('does not misidentify an unrelated peripheral with no UUIDs as a necklace', () {
      final peripheral = BlePeripheral(
        uuid: '0000-bbbb-0001',
        name: 'JBL Speaker',
        rssi: -50,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isFalse);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isFalse);
    });

    test('keeps the friend_-prefixed Friend Pendant as its own distinct product', () {
      final peripheral = BlePeripheral(
        uuid: '0000-cccc-0001',
        name: 'friend_A1B2',
        rssi: -55,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isFriendPendant(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isFalse);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.peripheralToDevice(peripheral).type, DeviceType.friendPendant);
    });

    test('does not conflict with other supported peripherals', () {
      final beeDevice = BlePeripheral(
        uuid: '0000-dddd-0001',
        name: 'Bee Device',
        rssi: -50,
        serviceUuids: [],
      );

      expect(NativeBluetoothDiscoverer.isOmi(beeDevice), isFalse);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(beeDevice), isTrue);
      expect(NativeBluetoothDiscoverer.peripheralToDevice(beeDevice).type, DeviceType.bee);
    });
  });
}
