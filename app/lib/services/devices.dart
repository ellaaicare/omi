import 'dart:async';

import 'package:collection/collection.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/services/devices/device_connection.dart';
import 'package:omi/services/devices/discovery/apple_watch_discoverer.dart';
import 'package:omi/services/devices/discovery/bluetooth_discoverer.dart';
import 'package:omi/services/devices/discovery/device_discoverer.dart';
import 'package:omi/services/devices/errors.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/mutex.dart';

abstract class IDeviceService {
  void start();
  Future<void> stop();
  Future<void> discover({String? desirableDeviceId, int timeout = 5});

  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false});

  void subscribe(IDeviceServiceSubsciption subscription, Object context);
  void unsubscribe(Object context);

  DateTime? getFirstConnectedAt();

  // WiFi sync support - pause BLE reconnection during WiFi transfer
  void setWifiSyncInProgress(bool value);
  Future<void> disconnectDevice();
}

enum DeviceServiceStatus {
  init,
  ready,
  scanning,
  stop,
}

enum DeviceConnectionState {
  connected,
  disconnected,
}

/// Feature flags for Omi device capabilities
/// Must match the firmware definitions in features.h
class OmiFeatures {
  static const int speaker = 1 << 0;
  static const int accelerometer = 1 << 1;
  static const int button = 1 << 2;
  static const int battery = 1 << 3;
  static const int usb = 1 << 4;
  static const int haptic = 1 << 5;
  static const int offlineStorage = 1 << 6;
  static const int ledDimming = 1 << 7;
  static const int micGain = 1 << 8;
  static const int wifi = 1 << 9;
}

abstract class IDeviceServiceSubsciption {
  void onDevices(List<BtDevice> devices);
  void onStatusChanged(DeviceServiceStatus status);
  void onDeviceConnectionStateChanged(
    String deviceId,
    DeviceConnectionState state, {
    int? connectionGeneration,
  });
}

typedef DeviceConnectionCreator = DeviceConnection? Function(BtDevice device);

class DeviceService implements IDeviceService {
  DeviceService({List<DeviceDiscoverer>? discoverers, DeviceConnectionCreator? connectionCreator})
      : _discoverers = discoverers ?? [BluetoothDeviceDiscoverer(), AppleWatchDiscoverer()],
        _connectionCreator = connectionCreator ?? DeviceConnectionFactory.create;

  DeviceServiceStatus _status = DeviceServiceStatus.init;
  List<BtDevice> _devices = [];

  final List<DeviceDiscoverer> _discoverers;
  final DeviceConnectionCreator _connectionCreator;

  final Map<Object, IDeviceServiceSubsciption> _subscriptions = {};

  DeviceConnection? _connection;
  int _operationGeneration = 0;
  int _connectionGeneration = 0;
  List<BtDevice> get devices => _devices;

  DeviceServiceStatus get status => _status;

  DateTime? _firstConnectedAt;

  @override
  Future<void> discover({
    String? desirableDeviceId,
    int timeout = 5,
  }) async {
    Logger.debug("Device discovering...");
    if (_status != DeviceServiceStatus.ready) {
      logCommonErrorMessage("Device service is not ready, may busying or stop");
      return;
    }
    final operationGeneration = _operationGeneration;

    bool isCurrentDiscovery() => operationGeneration == _operationGeneration && _status != DeviceServiceStatus.stop;

    _status = DeviceServiceStatus.scanning;

    try {
      final discoveredDevices = <BtDevice>[];

      final supportedDiscoverers = _discoverers.where((d) => d.isSupported).toList();
      final discoveryFutures = supportedDiscoverers.map((d) async {
        try {
          final result = await d.discover(timeout: timeout);
          if (!isCurrentDiscovery()) return <BtDevice>[];
          return result.devices;
        } catch (e, st) {
          Logger.debug('Discovery failed for ${d.name}: $e');
          Logger.debug('$st');
          return <BtDevice>[];
        }
      });

      // Wait for all discoveries to complete
      final results = await Future.wait(discoveryFutures);
      if (!isCurrentDiscovery()) return;

      // Combine all discovered devices
      for (final devices in results) {
        discoveredDevices.addAll(devices);
      }

      if (!isCurrentDiscovery()) return;
      _devices = discoveredDevices;
      if (!isCurrentDiscovery()) return;
      onDevices(devices);

      if (desirableDeviceId != null && desirableDeviceId.isNotEmpty) {
        if (!isCurrentDiscovery()) return;
        await ensureConnection(desirableDeviceId, force: true);
        if (!isCurrentDiscovery()) return;
      }
    } finally {
      if (isCurrentDiscovery()) _status = DeviceServiceStatus.ready;
    }
  }

  Future<void> _connectToDevice(String id) async {
    // Drop existing connection first
    if (_connection?.status == DeviceConnectionState.connected) {
      await _connection?.disconnect();
    }
    _connection = null;

    var device = _devices.firstWhereOrNull((f) => f.id == id);

    // If device not in discovered list, try to get it from SharedPreferences
    // This allows background reconnection without scanning
    if (device == null) {
      Logger.debug("Device not in discovered list, checking stored device");
      device = _getStoredDevice(id);
      if (device != null) {
        Logger.debug("Using stored device for direct reconnection: ${device.name}");
        // Add to devices list so it's available for future connections
        if (!_devices.any((d) => d.id == device!.id)) {
          _devices.add(device);
        }
      } else {
        Logger.debug("No stored device available for $id");
        return;
      }
    }

    final connection = _connectionCreator(device);
    _connection = connection;
    if (connection != null) {
      final connectionGeneration = ++_connectionGeneration;
      await connection.connect(
        onConnectionStateChanged: (deviceId, state) {
          // A BLE transport can finish a callback after a later connection to
          // the same device id exists. Do not let the old session reach clients.
          if (connectionGeneration != _connectionGeneration || !identical(connection, _connection)) return;
          onDeviceConnectionStateChanged(deviceId, state, connectionGeneration: connectionGeneration);
        },
      );
    } else {
      Logger.debug("Failed to create device connection for ${device.id}");
    }
  }

  @override
  void subscribe(IDeviceServiceSubsciption subscription, Object context) {
    _subscriptions.remove(context.hashCode);
    _subscriptions.putIfAbsent(context.hashCode, () => subscription);

    // Retains
    subscription.onDevices(_devices);
    subscription.onStatusChanged(_status);
  }

  @override
  void unsubscribe(Object context) {
    _subscriptions.remove(context.hashCode);
  }

  @override
  void start() {
    _operationGeneration++;
    _status = DeviceServiceStatus.ready;
    onStatusChanged(_status);

    // TODO: Start watchdog to discover automatically, re-connect automatically
  }

  @override
  Future<void> stop() async {
    _operationGeneration++;
    _status = DeviceServiceStatus.stop;
    onStatusChanged(_status);

    // Stop all discoverers to prevent resource leaks and battery drain
    await Future.wait(_discoverers.map((discoverer) => discoverer.stop()));

    await disconnectDevice();

    _devices.clear();
  }

  void onStatusChanged(DeviceServiceStatus status) {
    for (var s in _subscriptions.values) {
      s.onStatusChanged(status);
    }
  }

  void onDeviceConnectionStateChanged(
    String deviceId,
    DeviceConnectionState state, {
    int? connectionGeneration,
  }) {
    Logger.debug("device connection state changed...$deviceId...$state");
    DebugLogManager.logEvent('device_connection_state', {
      'device_id': deviceId,
      'state': state.name,
    });
    for (var s in _subscriptions.values) {
      s.onDeviceConnectionStateChanged(deviceId, state, connectionGeneration: connectionGeneration);
    }
  }

  void onDevices(List<BtDevice> devices) {
    for (var s in _subscriptions.values) {
      s.onDevices(devices);
    }
  }

  // Warn: Should use a better solution to prevent race conditions
  final Mutex _mutex = Mutex();
  @override
  Future<DeviceConnection?> ensureConnection(String deviceId, {bool force = false}) async {
    final operationGeneration = _operationGeneration;
    await _mutex.acquire();
    try {
      if (_status == DeviceServiceStatus.stop || operationGeneration != _operationGeneration) return null;
      Logger.debug("ensureConnection ${_connection?.device.id} ${_connection?.status} $force");

      // Not force
      if (!force && _connection != null) {
        if (_connection?.device.id != deviceId || _connection?.status != DeviceConnectionState.connected) {
          return null;
        }

        // Connected
        return _connection;
      }

      // Force
      if (deviceId == _connection?.device.id && _connection?.status == DeviceConnectionState.connected) {
        return _connection;
      }

      // Connect
      try {
        await _connectToDevice(deviceId);
      } on DeviceConnectionException catch (e) {
        Logger.debug(e.cause);
        return null;
      }

      if (_status == DeviceServiceStatus.stop || operationGeneration != _operationGeneration) {
        await _connection?.disconnect();
        _connection = null;
        return null;
      }

      _firstConnectedAt ??= DateTime.now();
      return _connection;
    } finally {
      _mutex.release();
    }
  }

  @override
  DateTime? getFirstConnectedAt() {
    return _firstConnectedAt;
  }

  // Helper method to get stored device from SharedPreferences
  BtDevice? _getStoredDevice(String id) {
    try {
      final storedDevice = SharedPreferencesUtil().btDevice;
      if (storedDevice.id == id && storedDevice.id.isNotEmpty) {
        return storedDevice;
      }
    } catch (e) {
      Logger.debug('Error getting stored device: $e');
    }
    return null;
  }

  bool _isWifiSyncInProgress = false;
  bool get isWifiSyncInProgress => _isWifiSyncInProgress;

  @override
  void setWifiSyncInProgress(bool value) {
    _isWifiSyncInProgress = value;
    Logger.debug("DeviceService: WiFi sync in progress: $value");
  }

  @override
  Future<void> disconnectDevice() async {
    await _mutex.acquire();
    try {
      if (_connection != null) {
        Logger.debug("DeviceService: Disconnecting device...");
        await _connection?.disconnect();
        _connection = null;
        _connectionGeneration++;
      }
    } finally {
      _mutex.release();
    }
  }
}
