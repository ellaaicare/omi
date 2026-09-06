import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'package:collection/collection.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import 'package:omi/services/devices/models.dart';
import 'package:omi/utils/bluetooth/bluetooth_adapter.dart';
import 'package:omi/utils/logger.dart';
import 'device_transport.dart';

const _audioLivenessWindow = Duration(seconds: 2);
const _notificationSetupTimeoutSeconds = 4;

@visibleForTesting
bool bleCharacteristicUsesFreshNotifications(String characteristicUuid) =>
    characteristicUuid.toLowerCase() == audioDataStreamCharacteristicUuid.toLowerCase();

@visibleForTesting
class BleAudioLivenessRecovery {
  BleAudioLivenessRecovery({this.window = _audioLivenessWindow});

  final Duration window;
  Timer? _timer;
  bool _recoveryUsed = false;

  void arm(Future<void> Function() recover) {
    _timer?.cancel();
    _recoveryUsed = false;
    _timer = Timer(window, () {
      if (_recoveryUsed) return;
      _recoveryUsed = true;
      unawaited(recover());
    });
  }

  void observedAudio() {
    _timer?.cancel();
    _timer = null;
  }

  void reset() {
    _timer?.cancel();
    _timer = null;
    _recoveryUsed = false;
  }

  void dispose() => reset();
}

class BleTransport extends DeviceTransport {
  final BluetoothDevice _bleDevice;
  final StreamController<DeviceTransportState> _connectionStateController;
  final Map<String, StreamController<List<int>>> _streamControllers = {};
  final Map<String, StreamSubscription> _characteristicSubscriptions = {};
  final Map<String, Future<bool>> _characteristicSetupOperations = {};
  final BleAudioLivenessRecovery _audioLivenessRecovery;

  List<BluetoothService> _services = [];
  DeviceTransportState _state = DeviceTransportState.disconnected;
  StreamSubscription<BluetoothConnectionState>? _bleConnectionSubscription;

  BleTransport(this._bleDevice, {@visibleForTesting BleAudioLivenessRecovery? audioLivenessRecovery})
      : _connectionStateController = StreamController<DeviceTransportState>.broadcast(),
        _audioLivenessRecovery = audioLivenessRecovery ?? BleAudioLivenessRecovery() {
    _bleConnectionSubscription = _bleDevice.connectionState.listen((state) {
      switch (state) {
        case BluetoothConnectionState.disconnected:
          _audioLivenessRecovery.reset();
          _updateState(DeviceTransportState.disconnected);
          break;
        case BluetoothConnectionState.connecting:
          _updateState(DeviceTransportState.connecting);
          break;
        case BluetoothConnectionState.connected:
          _updateState(DeviceTransportState.connected);
          break;
        case BluetoothConnectionState.disconnecting:
          _updateState(DeviceTransportState.disconnecting);
          break;
      }
    });
  }

  @override
  String get deviceId => _bleDevice.remoteId.str;

  @override
  Stream<DeviceTransportState> get connectionStateStream => _connectionStateController.stream;

  void _updateState(DeviceTransportState newState) {
    if (_state != newState) {
      _state = newState;
      _connectionStateController.add(_state);
    }
  }

  @override
  Future<void> connect() async {
    if (_state == DeviceTransportState.connected) {
      return;
    }

    _updateState(DeviceTransportState.connecting);

    try {
      // Wait for Bluetooth adapter to be ready
      await BluetoothAdapter.adapterState.where((val) => val == BluetoothAdapterStateHelper.on).first;

      // Connect to device
      await _bleDevice.connect(license: License.free);
      await _bleDevice.connectionState.where((val) => val == BluetoothConnectionState.connected).first;

      // Request larger MTU for better performance on Android
      if (Platform.isAndroid && _bleDevice.mtuNow < 512) {
        await _bleDevice.requestMtu(512);
      }

      // Discover services
      _services = await _bleDevice.discoverServices();

      _updateState(DeviceTransportState.connected);
    } catch (e) {
      _updateState(DeviceTransportState.disconnected);
      rethrow;
    }
  }

  @override
  Future<void> disconnect() async {
    if (_state == DeviceTransportState.disconnected) {
      return;
    }

    _updateState(DeviceTransportState.disconnecting);
    _audioLivenessRecovery.reset();

    try {
      for (final subscription in _characteristicSubscriptions.values) {
        await subscription.cancel();
      }
      _characteristicSubscriptions.clear();

      for (final controller in _streamControllers.values) {
        await controller.close();
      }
      _streamControllers.clear();

      await _bleDevice.disconnect();

      _updateState(DeviceTransportState.disconnected);
    } catch (e) {
      _updateState(DeviceTransportState.disconnected);
      rethrow;
    }
  }

  @override
  Future<bool> isConnected() async {
    return _bleDevice.isConnected;
  }

  @override
  Future<bool> ping() async {
    try {
      await _bleDevice.readRssi(timeout: 10);
      return true;
    } catch (e) {
      Logger.debug('BLE Transport ping failed: $e');
      return false;
    }
  }

  @override
  Stream<List<int>> getCharacteristicStream(String serviceUuid, String characteristicUuid) {
    final key = _characteristicKey(serviceUuid, characteristicUuid);
    final controller = _streamControllers.putIfAbsent(key, StreamController<List<int>>.broadcast);
    unawaited(_ensureCharacteristicListener(serviceUuid, characteristicUuid, key));
    return controller.stream;
  }

  @override
  Future<Stream<List<int>>?> getReadyCharacteristicStream(String serviceUuid, String characteristicUuid) async {
    final key = _characteristicKey(serviceUuid, characteristicUuid);
    final controller = _streamControllers.putIfAbsent(key, StreamController<List<int>>.broadcast);
    final ready = await _ensureCharacteristicListener(serviceUuid, characteristicUuid, key);
    if (!ready) return null;
    if (_isAudioCharacteristic(characteristicUuid)) {
      _audioLivenessRecovery.arm(() => _recoverSilentAudioSubscription(serviceUuid, characteristicUuid, key));
    }
    return controller.stream;
  }

  String _characteristicKey(String serviceUuid, String characteristicUuid) =>
      '${serviceUuid.toLowerCase()}:${characteristicUuid.toLowerCase()}';

  bool _isAudioCharacteristic(String characteristicUuid) => bleCharacteristicUsesFreshNotifications(characteristicUuid);

  Future<bool> _ensureCharacteristicListener(String serviceUuid, String characteristicUuid, String key) async {
    if (_characteristicSubscriptions.containsKey(key)) return true;
    final pending = _characteristicSetupOperations[key];
    if (pending != null) return pending;

    final operation = _setupCharacteristicListener(serviceUuid, characteristicUuid, key);
    _characteristicSetupOperations[key] = operation;
    try {
      return await operation;
    } finally {
      if (identical(_characteristicSetupOperations[key], operation)) {
        _characteristicSetupOperations.remove(key);
      }
    }
  }

  Future<bool> _setupCharacteristicListener(
    String serviceUuid,
    String characteristicUuid,
    String key, {
    bool force = false,
  }) async {
    try {
      if (force) {
        await _characteristicSubscriptions.remove(key)?.cancel();
      } else if (_characteristicSubscriptions.containsKey(key)) {
        return true;
      }
      final characteristic = await _getCharacteristic(serviceUuid, characteristicUuid);
      if (characteristic == null) {
        Logger.debug('BLE Transport: Characteristic not found: $serviceUuid:$characteristicUuid');
        return false;
      }
      if (_state != DeviceTransportState.connected) return false;

      if (force) {
        try {
          await characteristic.setNotifyValue(false, timeout: _notificationSetupTimeoutSeconds);
        } catch (error) {
          Logger.debug('BLE Transport: Could not clear stale notification state before retry: $error');
        }
      }

      final values =
          _isAudioCharacteristic(characteristicUuid) ? characteristic.onValueReceived : characteristic.lastValueStream;
      final subscription = values.listen(
        (value) {
          if (value.isNotEmpty && _isAudioCharacteristic(characteristicUuid)) {
            _audioLivenessRecovery.observedAudio();
          }
          if (_streamControllers[key] != null && !_streamControllers[key]!.isClosed) {
            _streamControllers[key]!.add(value);
          }
        },
        onError: (error) {
          Logger.debug('BLE Transport characteristic stream error: $error');
        },
      );

      try {
        await characteristic.setNotifyValue(true, timeout: _notificationSetupTimeoutSeconds);
      } catch (_) {
        await subscription.cancel();
        rethrow;
      }
      if (_state != DeviceTransportState.connected) {
        await subscription.cancel();
        return false;
      }

      _characteristicSubscriptions[key] = subscription;
      _bleDevice.cancelWhenDisconnected(subscription);
      return true;
    } catch (e) {
      Logger.debug('BLE Transport: Failed to setup characteristic listener: $e');
      return false;
    }
  }

  Future<void> _recoverSilentAudioSubscription(String serviceUuid, String characteristicUuid, String key) async {
    if (_state != DeviceTransportState.connected) return;
    Logger.debug('BLE Transport: Connected audio channel is silent; retrying its notification subscription once');
    final recovered = await _setupCharacteristicListener(serviceUuid, characteristicUuid, key, force: true);
    if (!recovered) {
      Logger.debug('BLE Transport: Silent audio notification subscription retry failed');
    }
  }

  @override
  Future<List<int>> readCharacteristic(String serviceUuid, String characteristicUuid) async {
    final characteristic = await _getCharacteristic(serviceUuid, characteristicUuid);
    if (characteristic == null) {
      return [];
    }

    try {
      return await characteristic.read();
    } catch (e) {
      Logger.debug('BLE Transport: Failed to read characteristic: $e');
      return [];
    }
  }

  @override
  Future<void> writeCharacteristic(String serviceUuid, String characteristicUuid, List<int> data) async {
    final characteristic = await _getCharacteristic(serviceUuid, characteristicUuid);
    if (characteristic == null) {
      throw Exception('Characteristic not found: $serviceUuid:$characteristicUuid');
    }

    try {
      // Use allowLongWrite when data exceeds the current MTU payload size.
      final needsLongWrite = data.length > (_bleDevice.mtuNow - 3);
      await characteristic.write(data, allowLongWrite: needsLongWrite);
    } catch (e) {
      Logger.debug('BLE Transport: Failed to write characteristic: $e');
      rethrow;
    }
  }

  Future<BluetoothCharacteristic?> _getCharacteristic(String serviceUuid, String characteristicUuid) async {
    final service = _services.firstWhereOrNull(
      (service) => service.uuid.str128.toLowerCase() == serviceUuid.toLowerCase(),
    );

    if (service == null) {
      return null;
    }

    return service.characteristics.firstWhereOrNull(
      (characteristic) => characteristic.uuid.str128.toLowerCase() == characteristicUuid.toLowerCase(),
    );
  }

  @override
  Future<void> dispose() async {
    _audioLivenessRecovery.dispose();
    await _bleConnectionSubscription?.cancel();

    for (final subscription in _characteristicSubscriptions.values) {
      await subscription.cancel();
    }
    _characteristicSubscriptions.clear();

    for (final controller in _streamControllers.values) {
      await controller.close();
    }
    _streamControllers.clear();

    await _connectionStateController.close();
  }
}
