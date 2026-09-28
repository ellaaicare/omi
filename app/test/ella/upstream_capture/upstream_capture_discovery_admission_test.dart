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
import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/devices/bluetooth_readiness.dart';
import 'package:omi/upstream_capture/services/devices/discovery/native_bluetooth_discoverer.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';

class _FakeBleHostApi extends BleHostApi {
  _FakeBleHostApi({this.startScanHandler, this.stopScanHandler});

  final Future<void> Function()? startScanHandler;
  final Future<void> Function()? stopScanHandler;
  int startScanCalls = 0;
  int stopScanCalls = 0;
  List<String>? lastServiceUuids;

  @override
  Future<void> startScan(int timeoutSeconds, List<String> serviceUuids) async {
    startScanCalls++;
    lastServiceUuids = serviceUuids;
    await startScanHandler?.call();
  }

  @override
  Future<void> stopScan() async {
    stopScanCalls++;
    await stopScanHandler?.call();
  }
}

BluetoothReadiness _readyBluetooth() => BluetoothReadiness(
      readState: () async => 'on',
      permissionState: (_) async => null,
      observeBridge: false,
    );

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

    // ellaaicare/ella-ai#1280 RUN-010 / #1287: `OmiBleManager.didDiscover` already falls
    // back from the advertisement local name to `peripheral.name` (the OS's cached GAP
    // name) via `OmiBleDiscoveryNaming.discoveredName` — confirmed identical to upstream.
    // The admission classifier below must accept that fallback name exactly like an
    // advertised local name; this only exercises the classifier (the two hasAdvName /
    // hasPeripheralName booleans are diagnostics-only and don't affect admission).
    test('admits a necklace whose name came only from peripheral.name, not the advertisement', () {
      final peripheral = BlePeripheral(
        uuid: '0000-aaaa-0005',
        name: 'Friend',
        rssi: -62,
        serviceUuids: [],
        hasAdvertisedLocalName: false,
        hasPeripheralName: true,
      );

      expect(NativeBluetoothDiscoverer.isOmi(peripheral), isTrue);
      expect(NativeBluetoothDiscoverer.isSupportedPeripheral(peripheral), isTrue);

      final device = NativeBluetoothDiscoverer.peripheralToDevice(peripheral);
      expect(device.type, DeviceType.omi);
      expect(device.name, 'Friend');
    });
  });

  group('NativeBluetoothDiscoverer host boundary', () {
    late void Function(BlePeripheral peripheral)? previousCallback;
    late BluetoothReadiness readiness;

    setUp(() {
      previousCallback = BleBridge.instance.peripheralDiscoveredCallback;
      readiness = _readyBluetooth();
    });

    tearDown(() {
      BleBridge.instance.peripheralDiscoveredCallback = previousCallback;
      readiness.dispose();
    });

    test('returns an empty result and restores the callback when startScan fails', () async {
      final host = _FakeBleHostApi(
        startScanHandler: () => Future<void>.error(PlatformException(code: 'channel-error')),
      );
      void sentinel(BlePeripheral _) {}
      BleBridge.instance.peripheralDiscoveredCallback = sentinel;
      final discoverer = NativeBluetoothDiscoverer(hostApi: host, bluetoothReadiness: readiness);

      final result = await discoverer.discover(timeout: 0);

      expect(result.devices, isEmpty);
      expect(host.startScanCalls, 1);
      expect(host.stopScanCalls, 0);
      expect(BleBridge.instance.peripheralDiscoveredCallback, same(sentinel));
    });

    test('awaits delayed start and stop before restoring the callback', () async {
      final startCompleter = Completer<void>();
      final stopCompleter = Completer<void>();
      final host = _FakeBleHostApi(
        startScanHandler: () => startCompleter.future,
        stopScanHandler: () => stopCompleter.future,
      );
      void sentinel(BlePeripheral _) {}
      BleBridge.instance.peripheralDiscoveredCallback = sentinel;
      final discoverer = NativeBluetoothDiscoverer(hostApi: host, bluetoothReadiness: readiness);

      var completed = false;
      final discovery = discoverer.discover(timeout: 0).whenComplete(() => completed = true);
      await Future<void>.delayed(Duration.zero);

      expect(host.startScanCalls, 1);
      expect(host.stopScanCalls, 0);
      expect(completed, isFalse);
      expect(BleBridge.instance.peripheralDiscoveredCallback, isNot(same(sentinel)));

      startCompleter.complete();
      for (var attempt = 0; attempt < 10 && host.stopScanCalls == 0; attempt++) {
        await Future<void>.delayed(Duration.zero);
      }

      expect(host.stopScanCalls, 1);
      expect(completed, isFalse);
      expect(BleBridge.instance.peripheralDiscoveredCallback, isNot(same(sentinel)));

      stopCompleter.complete();
      final result = await discovery;

      expect(result.devices, isEmpty);
      expect(completed, isTrue);
      expect(BleBridge.instance.peripheralDiscoveredCallback, same(sentinel));
    });

    // ellaaicare/ella-ai#1287: verified the current serviceUuids: [] call site is already
    // unfiltered — CoreBluetooth's scanForPeripherals(withServices:) only reports
    // peripherals advertising an exact match, and production necklaces don't reliably
    // advertise the Omi/Friend service UUID. This guards against a regression that would
    // pass a non-empty filter and silently drop those necklaces again before the
    // name/UUID admission classifier above ever sees them.
    test('requests an unfiltered scan (no serviceUuids filter)', () async {
      final host = _FakeBleHostApi();
      final discoverer = NativeBluetoothDiscoverer(hostApi: host, bluetoothReadiness: _readyBluetooth());

      await discoverer.discover(timeout: 0);

      expect(host.startScanCalls, 1);
      expect(host.lastServiceUuids, isEmpty);
    });

    test('handles stopScan failure and restores the callback', () async {
      final host = _FakeBleHostApi(
        stopScanHandler: () => Future<void>.error(PlatformException(code: 'channel-error')),
      );
      void sentinel(BlePeripheral _) {}
      BleBridge.instance.peripheralDiscoveredCallback = sentinel;
      final discoverer = NativeBluetoothDiscoverer(hostApi: host, bluetoothReadiness: readiness);

      final result = await discoverer.discover(timeout: 0);

      expect(result.devices, isEmpty);
      expect(host.startScanCalls, 1);
      expect(host.stopScanCalls, 1);
      expect(BleBridge.instance.peripheralDiscoveredCallback, same(sentinel));
    });
  });
}
