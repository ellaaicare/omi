import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'package:collection/collection.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import 'package:omi/services/devices/models.dart';
import 'package:omi/utils/bluetooth/bluetooth_adapter.dart';
import 'package:omi/utils/logger.dart';
import 'device_transport.dart';

@visibleForTesting
const bleAudioLivenessWindow = Duration(milliseconds: 500);

@visibleForTesting
const bleNotificationEnableTimeoutSeconds = 2;

@visibleForTesting
const bleNotificationResetTimeoutSeconds = 1;

@visibleForTesting
const bleNotificationSetupRetryDelay = Duration(milliseconds: 100);

@visibleForTesting
abstract class BleNotificationEndpoint {
  Stream<List<int>> get freshValues;
  Stream<List<int>> get replayingValues;

  Future<void> setNotifyValue(bool enabled, {required int timeout});
}

class _FlutterBlueNotificationEndpoint implements BleNotificationEndpoint {
  _FlutterBlueNotificationEndpoint(this.characteristic);

  final BluetoothCharacteristic characteristic;

  @override
  Stream<List<int>> get freshValues => characteristic.onValueReceived;

  @override
  Stream<List<int>> get replayingValues => characteristic.lastValueStream;

  @override
  Future<void> setNotifyValue(bool enabled, {required int timeout}) =>
      characteristic.setNotifyValue(enabled, timeout: timeout);
}

@visibleForTesting
typedef BleNotificationEndpointResolver = Future<BleNotificationEndpoint?> Function(
  String serviceUuid,
  String characteristicUuid,
);

@visibleForTesting
bool bleCharacteristicUsesFreshNotifications(String characteristicUuid) =>
    characteristicUuid.toLowerCase() == audioDataStreamCharacteristicUuid.toLowerCase();

@visibleForTesting
class BleAudioLivenessRecovery {
  BleAudioLivenessRecovery({this.window = bleAudioLivenessWindow});

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
  final BleNotificationEndpointResolver? _notificationEndpointResolver;
  final bool Function()? _connectionProbe;
  final void Function(StreamSubscription<List<int>>)? _disconnectRegistrar;

  List<BluetoothService> _services = [];
  DeviceTransportState _state = DeviceTransportState.disconnected;
  StreamSubscription<BluetoothConnectionState>? _bleConnectionSubscription;
  Future<void> _characteristicTeardownOperation = Future<void>.value();
  int _connectionGeneration = 0;
  bool _disposed = false;

  BleTransport(
    this._bleDevice, {
    @visibleForTesting BleAudioLivenessRecovery? audioLivenessRecovery,
    @visibleForTesting BleNotificationEndpointResolver? notificationEndpointResolver,
    @visibleForTesting bool Function()? connectionProbe,
    @visibleForTesting void Function(StreamSubscription<List<int>>)? disconnectRegistrar,
    @visibleForTesting Stream<BluetoothConnectionState>? connectionStateStream,
  })  : _connectionStateController = StreamController<DeviceTransportState>.broadcast(),
        _audioLivenessRecovery = audioLivenessRecovery ?? BleAudioLivenessRecovery(),
        _notificationEndpointResolver = notificationEndpointResolver,
        _connectionProbe = connectionProbe,
        _disconnectRegistrar = disconnectRegistrar {
    _bleConnectionSubscription = (connectionStateStream ?? _bleDevice.connectionState).listen((state) {
      switch (state) {
        case BluetoothConnectionState.disconnected:
          _updateState(DeviceTransportState.disconnected);
          unawaited(_invalidateAndClearCharacteristicState());
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

    try {
      await _invalidateAndClearCharacteristicState();

      await _bleDevice.disconnect();
      await _characteristicTeardownOperation;

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
    final requestGeneration = _connectionGeneration;
    await _characteristicTeardownOperation;
    if (!_isSetupCurrent(requestGeneration)) return null;
    final key = _characteristicKey(serviceUuid, characteristicUuid);
    final controller = _streamControllers.putIfAbsent(key, StreamController<List<int>>.broadcast);
    var ready = await _ensureCharacteristicListener(serviceUuid, characteristicUuid, key);
    if (!ready && _isSetupCurrent(requestGeneration)) {
      await Future<void>.delayed(bleNotificationSetupRetryDelay);
      if (!_isSetupCurrent(requestGeneration)) return null;
      ready = await _ensureCharacteristicListener(serviceUuid, characteristicUuid, key, force: true);
    }
    if (!ready || !_isSetupCurrent(requestGeneration) || !identical(_streamControllers[key], controller)) return null;
    if (_isAudioCharacteristic(characteristicUuid)) {
      _audioLivenessRecovery.arm(() => _recoverSilentAudioSubscription(serviceUuid, characteristicUuid, key));
    }
    return controller.stream;
  }

  String _characteristicKey(String serviceUuid, String characteristicUuid) =>
      '${serviceUuid.toLowerCase()}:${characteristicUuid.toLowerCase()}';

  bool _isAudioCharacteristic(String characteristicUuid) => bleCharacteristicUsesFreshNotifications(characteristicUuid);

  bool get _isOperationConnected => _connectionProbe?.call() ?? _state == DeviceTransportState.connected;

  bool _isSetupCurrent(int generation) => !_disposed && _isOperationConnected && generation == _connectionGeneration;

  Future<bool> _ensureCharacteristicListener(
    String serviceUuid,
    String characteristicUuid,
    String key, {
    bool force = false,
    bool resetNotifications = false,
  }) async {
    await _characteristicTeardownOperation;
    if (_disposed || !_isOperationConnected) return false;
    if (!force && _characteristicSubscriptions.containsKey(key)) return true;
    final pending = _characteristicSetupOperations[key];
    if (pending != null) return pending;

    final operation = _setupCharacteristicListener(
      serviceUuid,
      characteristicUuid,
      key,
      force: force,
      resetNotifications: resetNotifications,
    );
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
    bool resetNotifications = false,
  }) async {
    final setupGeneration = _connectionGeneration;
    try {
      if (force) {
        await _characteristicSubscriptions.remove(key)?.cancel();
        if (!_isSetupCurrent(setupGeneration)) return false;
      } else if (_characteristicSubscriptions.containsKey(key)) {
        return true;
      }
      final endpoint = await _getNotificationEndpoint(serviceUuid, characteristicUuid);
      if (endpoint == null) {
        Logger.debug('BLE Transport: Characteristic not found: $serviceUuid:$characteristicUuid');
        return false;
      }
      if (!_isSetupCurrent(setupGeneration)) return false;

      if (resetNotifications) {
        try {
          await endpoint.setNotifyValue(false, timeout: bleNotificationResetTimeoutSeconds);
        } catch (error) {
          Logger.debug('BLE Transport: Could not clear stale notification state before retry: $error');
        }
        if (!_isSetupCurrent(setupGeneration)) return false;
      }

      final values = _isAudioCharacteristic(characteristicUuid) ? endpoint.freshValues : endpoint.replayingValues;
      final subscription = values.listen(
        (value) {
          if (!_isSetupCurrent(setupGeneration)) return;
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
        await endpoint.setNotifyValue(true, timeout: bleNotificationEnableTimeoutSeconds);
      } catch (_) {
        await subscription.cancel();
        rethrow;
      }
      if (!_isSetupCurrent(setupGeneration)) {
        await subscription.cancel();
        return false;
      }

      _characteristicSubscriptions[key] = subscription;
      final disconnectRegistrar = _disconnectRegistrar;
      if (disconnectRegistrar != null) {
        disconnectRegistrar(subscription);
      } else {
        _bleDevice.cancelWhenDisconnected(subscription);
      }
      return true;
    } catch (e) {
      Logger.debug('BLE Transport: Failed to setup characteristic listener: $e');
      return false;
    }
  }

  Future<void> _recoverSilentAudioSubscription(String serviceUuid, String characteristicUuid, String key) async {
    if (!_isOperationConnected) return;
    Logger.debug('BLE Transport: Connected audio channel is silent; retrying its notification subscription once');
    final recovered = await _ensureCharacteristicListener(
      serviceUuid,
      characteristicUuid,
      key,
      force: true,
      resetNotifications: true,
    );
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

  Future<BleNotificationEndpoint?> _getNotificationEndpoint(String serviceUuid, String characteristicUuid) async {
    final resolver = _notificationEndpointResolver;
    if (resolver != null) return resolver(serviceUuid, characteristicUuid);
    final characteristic = await _getCharacteristic(serviceUuid, characteristicUuid);
    return characteristic == null ? null : _FlutterBlueNotificationEndpoint(characteristic);
  }

  Future<void> _clearCharacteristicState() async {
    final subscriptions = _characteristicSubscriptions.values.toList(growable: false);
    _characteristicSubscriptions.clear();
    final controllers = _streamControllers.values.toList(growable: false);
    _streamControllers.clear();

    for (final subscription in subscriptions) {
      await subscription.cancel();
    }
    for (final controller in controllers) {
      if (!controller.isClosed) await controller.close();
    }
  }

  Future<void> _finishCharacteristicTeardown(
    Future<void> previousTeardown,
    List<Future<bool>> pendingSetups,
  ) async {
    await previousTeardown;
    await Future.wait(pendingSetups);
    await _clearCharacteristicState();
  }

  Future<void> _invalidateAndClearCharacteristicState() {
    _connectionGeneration++;
    _audioLivenessRecovery.reset();
    final operation = _finishCharacteristicTeardown(
      _characteristicTeardownOperation,
      _characteristicSetupOperations.values.toList(growable: false),
    );
    _characteristicTeardownOperation = operation;
    return operation;
  }

  @override
  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    _audioLivenessRecovery.dispose();
    await _bleConnectionSubscription?.cancel();
    await _invalidateAndClearCharacteristicState();

    await _connectionStateController.close();
  }
}
