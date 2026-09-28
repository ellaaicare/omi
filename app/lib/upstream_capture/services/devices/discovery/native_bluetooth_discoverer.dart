import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/devices/bluetooth_readiness.dart';
import 'package:omi/upstream_capture/services/devices/discovery/device_locator.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';
import 'package:omi/utils/logger.dart';
import 'device_discoverer.dart';

/// BLE discoverer backed by native platform APIs via Pigeon.
/// iOS: CoreBluetooth. Android: BluetoothLeScanner + CompanionDeviceManager.
class NativeBluetoothDiscoverer extends DeviceDiscoverer {
  NativeBluetoothDiscoverer({BleHostApi? hostApi, BluetoothReadiness? bluetoothReadiness})
      : _hostApi = hostApi ?? BleHostApi(),
        _bluetoothReadiness = bluetoothReadiness ?? BluetoothReadiness.instance;

  final BleHostApi _hostApi;
  final BluetoothReadiness _bluetoothReadiness;
  Timer? _timeoutTimer;
  Completer<void>? _scanCompleter;

  @override
  String get name => 'NativeBluetooth';

  @override
  bool get isSupported => true;

  @override
  Future<DeviceDiscoveryResult> discover({int timeout = 5}) async {
    if (!await _bluetoothReadiness.ensureReady(BluetoothUse.discovery)) {
      return const DeviceDiscoveryResult(devices: [], isBlocked: true);
    }
    final List<BlePeripheral> results = [];
    final completer = Completer<void>();
    _scanCompleter = completer;

    // Redacted discovery counters: counts only, never names/UUIDs.
    int seenCount = 0;
    int noNameCount = 0;

    final previousCallback = BleBridge.instance.peripheralDiscoveredCallback;

    BleBridge.instance.peripheralDiscoveredCallback = (BlePeripheral peripheral) {
      seenCount++;
      if (peripheral.name.isEmpty) {
        noNameCount++;
        return;
      }
      // Deduplicate by UUID
      results.removeWhere((p) => p.uuid == peripheral.uuid);
      results.add(peripheral);
    };

    var scanStarted = false;
    try {
      try {
        await _hostApi.startScan(timeout, []);
        scanStarted = true;

        _timeoutTimer?.cancel();
        _timeoutTimer = Timer(Duration(seconds: timeout), () {
          if (!completer.isCompleted) completer.complete();
        });
        await completer.future;
      } catch (error, stackTrace) {
        Logger.warning('NativeBluetoothDiscoverer: start scan error: $error');
        Logger.debug('$stackTrace');
        return const DeviceDiscoveryResult(devices: []);
      }
    } finally {
      _timeoutTimer?.cancel();
      _timeoutTimer = null;
      if (identical(_scanCompleter, completer)) {
        _scanCompleter = null;
      }
      if (scanStarted) {
        try {
          await _hostApi.stopScan();
        } catch (error, stackTrace) {
          Logger.warning('NativeBluetoothDiscoverer: stop scan error: $error');
          Logger.debug('$stackTrace');
        }
      }
      BleBridge.instance.peripheralDiscoveredCallback = previousCallback;
    }

    final devices = results.where(_isSupportedPeripheral).map(_peripheralToDevice).toList()
      ..sort((a, b) => b.rssi.compareTo(a.rssi));

    final noSignatureMatchCount = results.length - devices.length;
    Logger.debug('NativeBluetoothDiscoverer: seen=$seenCount admitted=${devices.length} '
        'rejected(no_name=$noNameCount, no_signature_match=$noSignatureMatchCount)');

    return DeviceDiscoveryResult(devices: devices);
  }

  @override
  Future<void> stop() async {
    _timeoutTimer?.cancel();
    _timeoutTimer = null;
    final completer = _scanCompleter;
    if (completer != null && !completer.isCompleted) {
      completer.complete();
    }
    try {
      await _hostApi.stopScan();
    } catch (e) {
      Logger.debug('NativeBluetoothDiscoverer: stop scan error: $e');
    }
  }

  // MARK: - Device type detection (mirrors BtDevice.isSupportedDevice without ScanResult)

  @visibleForTesting
  static bool isSupportedPeripheral(BlePeripheral p) => _isSupportedPeripheral(p);

  @visibleForTesting
  static bool isPlaud(BlePeripheral p) => _isPlaud(p);

  @visibleForTesting
  static bool isFriendPendant(BlePeripheral p) => _isFriendPendant(p);

  @visibleForTesting
  static bool isOmi(BlePeripheral p) => _isOmi(p);

  @visibleForTesting
  static BtDevice peripheralToDevice(BlePeripheral p) => _peripheralToDevice(p);

  static bool _isSupportedPeripheral(BlePeripheral p) {
    return _isBee(p) || _isPlaud(p) || _isFieldy(p) || _isFriendPendant(p) || _isLimitless(p) || _isOmi(p);
  }

  static bool _isBee(BlePeripheral p) {
    return p.name.toLowerCase().contains('bee');
  }

  static bool _isPlaud(BlePeripheral p) {
    final name = p.name.toLowerCase();
    return name.startsWith('plaud') || name.contains('notepin') || _hasService(p, plaudServiceUuid);
  }

  static bool _isFieldy(BlePeripheral p) {
    final name = p.name.toLowerCase();
    return name == 'compass' || name == 'fieldy' || _hasService(p, fieldyServiceUuid);
  }

  static bool _isFriendPendant(BlePeripheral p) {
    return p.name.toLowerCase().startsWith('friend_') || _hasService(p, friendPendantServiceUuid);
  }

  static bool _isLimitless(BlePeripheral p) {
    final name = p.name.toLowerCase();
    return name.contains('limitless') || name.contains('pendant') || _hasService(p, limitlessServiceUuid);
  }

  static bool _isOmi(BlePeripheral p) {
    // Necklaces can advertise as bare 'Friend' (the pre-rebrand name) or an
    // 'Omi'-prefixed local name with no service UUID in the advertisement packet.
    // 'friend_'-prefixed names remain the distinct Friend Pendant product,
    // matched by _isFriendPendant above.
    final name = p.name.toLowerCase();
    return name == 'friend' || name.startsWith('omi') || _hasService(p, omiServiceUuid);
  }

  static bool _hasService(BlePeripheral p, String serviceUuid) {
    final target = serviceUuid.toLowerCase();
    return p.serviceUuids.any((uuid) => uuid.toLowerCase() == target);
  }

  static BtDevice _peripheralToDevice(BlePeripheral p) {
    DeviceType type;
    if (_isBee(p)) {
      type = DeviceType.bee;
    } else if (_isPlaud(p)) {
      type = DeviceType.plaud;
    } else if (_isFieldy(p)) {
      type = DeviceType.fieldy;
    } else if (_isFriendPendant(p)) {
      type = DeviceType.friendPendant;
    } else if (_isLimitless(p)) {
      type = DeviceType.limitless;
    } else if (_isOmi(p)) {
      type = DeviceType.omi;
    } else {
      type = DeviceType.omi;
    }

    return BtDevice(
      name: p.name,
      id: p.uuid,
      type: type,
      rssi: p.rssi,
      locator: DeviceLocator.bluetooth(deviceId: p.uuid),
    );
  }
}
