import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:omi/upstream_capture/backend/preferences.dart';
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/devices/bluetooth_readiness.dart';
import 'package:omi/upstream_capture/services/devices/discovery/device_locator.dart';
import 'package:omi/upstream_capture/services/devices/models.dart';
import 'package:omi/utils/debug_log_manager.dart';
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

    void handleCandidate(BlePeripheral peripheral) {
      seenCount++;
      DebugLogManager.deviceCandidatesSeen++;
      DebugLogManager.recordDeviceDiagnostic(
        'NativeBluetoothDiscoverer: candidate hasAdvName=${peripheral.hasAdvertisedLocalName} '
        'hasPeripheralName=${peripheral.hasPeripheralName} uuidCount=${peripheral.serviceUuids.length} '
        'source=${peripheral.source} rssiBucket=${_rssiBucket(peripheral.rssi)}',
      );
      // ellaaicare/ella-ai#1287 RUN-018: a candidate with no name at all can still be
      // admitted when its source is itself trustworthy evidence — see _isAdmittedBySource.
      if (peripheral.name.isEmpty && !_isAdmittedBySource(peripheral)) {
        noNameCount++;
        DebugLogManager.recordCandidateRejected('no_name');
        return;
      }
      // Deduplicate by UUID
      results.removeWhere((p) => p.uuid == peripheral.uuid);
      results.add(peripheral);
    }

    BleBridge.instance.peripheralDiscoveredCallback = handleCandidate;

    var scanStarted = false;
    try {
      try {
        // ellaaicare/ella-ai#1287: this scan is already unfiltered (no serviceUuids) —
        // CoreBluetooth's scanForPeripherals(withServices:) only reports peripherals
        // advertising an exact match, and production necklaces don't reliably
        // advertise one. Passing [] keeps CoreBluetooth from filtering candidates
        // out before the name/UUID admission classifier below ever sees them.
        await _hostApi.startScan(timeout, []);
        scanStarted = true;
        DebugLogManager.deviceScansStarted++;
        final btState = await _hostApi.getBluetoothState().catchError((_) => 'unknown');
        DebugLogManager.recordDeviceDiagnostic(
          'NativeBluetoothDiscoverer: startScan btState=$btState serviceUuidFilterCount=0 timeoutSeconds=$timeout',
        );

        // ellaaicare/ella-ai#1287 RUN-018: a necklace already connected at the CoreBluetooth
        // level (or already paired on this device) stops advertising, so the scan above will
        // never surface it. Ask natively for those two additional sources up front — they
        // return immediately, no active scan involved.
        try {
          final retrieved = await _hostApi.retrieveConnectedAndKnownPeripherals([omiServiceUuid], _knownDeviceIds());
          for (final peripheral in retrieved) {
            handleCandidate(peripheral);
          }
        } catch (error, stackTrace) {
          Logger.warning('NativeBluetoothDiscoverer: retrieveConnectedAndKnownPeripherals error: $error');
          Logger.debug('$stackTrace');
        }

        _timeoutTimer?.cancel();
        _timeoutTimer = Timer(Duration(seconds: timeout), () {
          if (!completer.isCompleted) completer.complete();
        });
        await completer.future;
      } catch (error, stackTrace) {
        Logger.warning('NativeBluetoothDiscoverer: start scan error: $error');
        Logger.debug('$stackTrace');
        DebugLogManager.recordDeviceDiagnostic('NativeBluetoothDiscoverer: startScan error');
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
          DebugLogManager.deviceScansStopped++;
          DebugLogManager.recordDeviceDiagnostic('NativeBluetoothDiscoverer: stopScan');
        } catch (error, stackTrace) {
          Logger.warning('NativeBluetoothDiscoverer: stop scan error: $error');
          Logger.debug('$stackTrace');
          DebugLogManager.recordDeviceDiagnostic('NativeBluetoothDiscoverer: stopScan error');
        }
      }
      BleBridge.instance.peripheralDiscoveredCallback = previousCallback;
    }

    final devices = results.where(_isSupportedPeripheral).map(_peripheralToDevice).toList()
      ..sort((a, b) => b.rssi.compareTo(a.rssi));

    final noSignatureMatchCount = results.length - devices.length;
    if (noSignatureMatchCount > 0) {
      DebugLogManager.deviceCandidatesRejectedByReason['no_signature_match'] =
          (DebugLogManager.deviceCandidatesRejectedByReason['no_signature_match'] ?? 0) + noSignatureMatchCount;
    }
    DebugLogManager.deviceCandidatesAdmitted += devices.length;

    final summary = 'NativeBluetoothDiscoverer: seen=$seenCount admitted=${devices.length} '
        'rejected(no_name=$noNameCount, no_signature_match=$noSignatureMatchCount)';
    Logger.debug(summary);
    DebugLogManager.recordDeviceDiagnostic(summary);

    return DeviceDiscoveryResult(devices: devices);
  }

  /// Coarse 10dBm-wide RSSI bucket for redacted diagnostics — never the raw
  /// precise reading, which combined with other signals could help fingerprint
  /// a specific device/location.
  static String _rssiBucket(int rssi) {
    final lower = (rssi / 10).floor() * 10;
    return '$lower..${lower + 10}';
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

  @visibleForTesting
  static bool isAdmittedBySource(BlePeripheral p) => _isAdmittedBySource(p);

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
    return name == 'friend' || name.startsWith('omi') || _hasService(p, omiServiceUuid) || _isAdmittedBySource(p);
  }

  static bool _hasService(BlePeripheral p, String serviceUuid) {
    final target = serviceUuid.toLowerCase();
    return p.serviceUuids.any((uuid) => uuid.toLowerCase() == target);
  }

  /// ellaaicare/ella-ai#1287 RUN-018: none of the three capture-layer sources below
  /// involve an active scan, so none of them carry an advertised name — the name/UUID
  /// signature checks above never fire for them. Membership in a source that
  /// CoreBluetooth itself already vetted (already connected exposing the Omi service,
  /// or a saved/paired device id) stands in for the missing adv name.
  static bool _isAdmittedBySource(BlePeripheral p) {
    switch (p.source) {
      case 'retrievedConnected':
        // CoreBluetooth only returns this peripheral because it already exposes the
        // requested (Omi) service — that is itself the admission evidence.
        return true;
      case 'retrievedKnown':
        // CoreBluetooth only returns this peripheral because its id was passed in the
        // saved/paired device id list — that is itself the admission evidence.
        return true;
      case 'restored':
        // State restoration doesn't pre-filter by service the way retrievedConnected
        // does, so evaluate a restored peripheral the same way (a)/(b) would: either it
        // already exposes the Omi service, or its id is a saved/paired device.
        return _hasService(p, omiServiceUuid) || _isKnownDeviceId(p.uuid);
      default:
        return false;
    }
  }

  static bool _isKnownDeviceId(String uuid) {
    if (uuid.isEmpty) return false;
    if (SharedPreferencesUtil().btDevice.id == uuid) return true;
    return SharedPreferencesUtil().btDevices.any((device) => device.id == uuid);
  }

  /// Saved/paired device ids from the same persisted store the legacy reconnect path
  /// reads from (`SharedPreferencesUtil().btDevice` / `.btDevices`), passed to the
  /// native `retrievePeripherals(withIdentifiers:)` retrieval (source (b)).
  static List<String> _knownDeviceIds() {
    final ids = <String>{};
    final primaryId = SharedPreferencesUtil().btDevice.id;
    if (primaryId.isNotEmpty) ids.add(primaryId);
    for (final device in SharedPreferencesUtil().btDevices) {
      if (device.id.isNotEmpty) ids.add(device.id);
    }
    return ids.toList();
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
