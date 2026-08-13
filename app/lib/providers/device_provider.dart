import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/http/api/device.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/main.dart';
import 'package:omi/pages/home/firmware_update.dart';
import 'package:omi/pages/home/omiglass_ota_update.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/device.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/other/debouncer.dart';
import 'package:omi/utils/platform/platform_manager.dart';
import 'package:omi/widgets/confirmation_dialog.dart';

typedef DeviceConnectionResolver = Future<BtDevice?> Function(String deviceId);
typedef DeviceScanConnector = Future<BtDevice?> Function();
typedef DeviceStorageListResolver = Future<List<int>> Function(String deviceId);

class DeviceProvider extends ChangeNotifier implements IDeviceServiceSubsciption {
  DeviceProvider({
    IDeviceService? deviceService,
    DeviceConnectionResolver? connectionResolver,
    DeviceScanConnector? scanConnector,
    @visibleForTesting DeviceStorageListResolver? storageListResolver,
    @visibleForTesting Duration reconnectionInterval = const Duration(seconds: 15),
    @visibleForTesting int maxAutomaticReconnectAttempts = 3,
    @visibleForTesting Duration deviceCaptureRetryDelay = const Duration(milliseconds: 500),
    @visibleForTesting int maxDeviceCaptureStartAttempts = 3,
  })  : _deviceService = deviceService ?? ServiceManager.instance().device,
        _connectionResolver = connectionResolver,
        _scanConnector = scanConnector,
        _storageListResolver = storageListResolver,
        _reconnectionInterval = reconnectionInterval,
        _maxAutomaticReconnectAttempts = maxAutomaticReconnectAttempts,
        _deviceCaptureRetryDelay = deviceCaptureRetryDelay,
        _maxDeviceCaptureStartAttempts = maxDeviceCaptureStartAttempts {
    _deviceService.subscribe(this, this);
  }

  final IDeviceService _deviceService;
  final DeviceConnectionResolver? _connectionResolver;
  final DeviceScanConnector? _scanConnector;
  final DeviceStorageListResolver? _storageListResolver;
  CaptureProvider? captureProvider;

  bool isConnecting = false;
  bool isConnected = false;
  bool isDeviceStorageSupport = false;
  BtDevice? connectedDevice;
  BtDevice? pairedDevice;
  StreamSubscription<List<int>>? _bleBatteryLevelListener;
  int batteryLevel = -1;
  int _lastNotifiedBatteryLevel = -1;
  DateTime? _lastBatteryNotifyTime;
  bool _hasLowBatteryAlerted = false;
  Timer? _reconnectionTimer;
  DateTime? _reconnectAt;
  final Duration _reconnectionInterval;
  final int _maxAutomaticReconnectAttempts;
  final Duration _deviceCaptureRetryDelay;
  final int _maxDeviceCaptureStartAttempts;
  int _automaticReconnectAttempts = 0;
  bool _automaticReconnectExhausted = false;

  int get automaticReconnectAttempts => _automaticReconnectAttempts;
  bool get automaticReconnectExhausted => _automaticReconnectExhausted;

  bool _havingNewFirmware = false;
  bool get havingNewFirmware => _havingNewFirmware && pairedDevice != null && isConnected;

  BtDevice? get presentationConnectedDevice => SharedPreferencesUtil().demoMode ? _demoDevice : connectedDevice;

  BtDevice? get presentationPairedDevice => SharedPreferencesUtil().demoMode ? _demoDevice : pairedDevice;

  bool get presentationIsConnected => SharedPreferencesUtil().demoMode ? true : isConnected;

  int get presentationBatteryLevel => SharedPreferencesUtil().demoMode ? 96 : batteryLevel;

  static final BtDevice _demoDevice = BtDevice(
    name: 'Ella',
    id: 'demo-device',
    type: DeviceType.omi,
    rssi: 0,
    modelNumber: 'Demo',
    firmwareRevision: 'Demo',
  );

  // Track firmware update state to prevent showing dialog during updates
  bool _isFirmwareUpdateInProgress = false;
  bool get isFirmwareUpdateInProgress => _isFirmwareUpdateInProgress;

  // Current and latest firmware versions for UI display
  String get currentFirmwareVersion => pairedDevice?.firmwareRevision ?? 'Unknown';
  String _latestFirmwareVersion = '';
  String get latestFirmwareVersion => _latestFirmwareVersion;

  // OmiGlass firmware update details from GitHub releases
  Map<String, dynamic> _latestOmiGlassFirmwareDetails = {};
  Map<String, dynamic> get latestOmiGlassFirmwareDetails => _latestOmiGlassFirmwareDetails;

  Timer? _disconnectNotificationTimer;
  final Debouncer _disconnectDebouncer = Debouncer(delay: const Duration(milliseconds: 500));
  final Debouncer _connectDebouncer = Debouncer(delay: const Duration(milliseconds: 100));
  int _deviceOperationGeneration = 0;
  bool _deviceServiceReady = false;
  Future<void>? _captureTeardown;
  int? _deferredDeviceCaptureGeneration;
  String? _deferredDeviceCaptureId;
  Future<void>? _deferredDeviceCaptureStart;

  void Function(BtDevice device)? onDeviceConnected;

  bool _isDeviceOperationCurrent(int generation) =>
      _deviceServiceReady && _captureTeardown == null && generation == _deviceOperationGeneration;

  Future<void> _teardownCaptureForDevice(String? deviceId) {
    final priorTeardown = _captureTeardown;
    final capture = captureProvider;
    late final Future<void> teardown;
    teardown = (() async {
      await priorTeardown;
      if (deviceId == null || deviceId.isEmpty || capture == null) return;
      await capture.handleRecordingDeviceDisconnected(deviceId);
    })()
        .whenComplete(() {
      if (identical(_captureTeardown, teardown)) _captureTeardown = null;
    });
    _captureTeardown = teardown;
    return teardown;
  }

  Future<void> _resumeBoundDeviceAfterTeardown(int generation) async {
    await _captureTeardown;
    if (!_isDeviceOperationCurrent(generation) || pairedDevice == null) return;
    await periodicConnect(
      'device service resumed',
      boundDeviceOnly: true,
      operationGeneration: generation,
    );
  }

  Future<BtDevice?> _resolveConnectedDevice(String deviceId) async {
    final resolver = _connectionResolver;
    if (resolver != null) return resolver(deviceId);
    return (await _deviceService.ensureConnection(deviceId))?.device;
  }

  void setProviders(CaptureProvider provider) {
    if (identical(captureProvider, provider)) return;
    captureProvider?.removeListener(_onCaptureProviderChanged);
    captureProvider = provider;
    provider.addListener(_onCaptureProviderChanged);
    _onCaptureProviderChanged();
    notifyListeners();
  }

  void _deferDeviceCaptureUntilPhoneReleases(BtDevice device, int operationGeneration) {
    _deferredDeviceCaptureGeneration = operationGeneration;
    _deferredDeviceCaptureId = device.id;
    _onCaptureProviderChanged();
  }

  void _clearDeferredDeviceCapture() {
    _deferredDeviceCaptureGeneration = null;
    _deferredDeviceCaptureId = null;
  }

  void _onCaptureProviderChanged() {
    if (_deferredDeviceCaptureStart != null) return;
    final capture = captureProvider;
    final generation = _deferredDeviceCaptureGeneration;
    final deviceId = _deferredDeviceCaptureId;
    final device = connectedDevice;
    if (capture == null ||
        capture.phoneCaptureOwnsMobileAudio ||
        generation == null ||
        deviceId == null ||
        device?.id != deviceId ||
        !_isDeviceOperationCurrent(generation)) {
      return;
    }

    _clearDeferredDeviceCapture();
    late final Future<void> resume;
    resume = _startDeviceCaptureWithRetry(device!, generation).then<void>((_) {}).whenComplete(() {
      if (identical(_deferredDeviceCaptureStart, resume)) {
        _deferredDeviceCaptureStart = null;
        _onCaptureProviderChanged();
      }
    });
    _deferredDeviceCaptureStart = resume;
  }

  Future<void> setConnectedDevice(BtDevice? device, {int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    connectedDevice = device;
    pairedDevice = device;
    await getDeviceInfo(operationGeneration: operationGeneration);
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    Logger.debug('setConnectedDevice: $device');
    notifyListeners();
  }

  Future getDeviceInfo({int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    if (connectedDevice != null) {
      if (pairedDevice?.firmwareRevision != null && pairedDevice?.firmwareRevision != 'Unknown') {
        return;
      }
      var connection = await _deviceService.ensureConnection(connectedDevice!.id);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
      final info = await connectedDevice?.getDeviceInfo(connection);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
      pairedDevice = info;
      await SharedPreferencesUtil().btDeviceSet(pairedDevice!);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    } else {
      if (SharedPreferencesUtil().btDevice.id.isEmpty) {
        pairedDevice = BtDevice.empty();
      } else {
        pairedDevice = SharedPreferencesUtil().btDevice;
      }
    }
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    notifyListeners();
  }

  // TODO: thinh, use connection directly
  Future _bleDisconnectDevice(BtDevice btDevice) async {
    var connection = await _deviceService.ensureConnection(btDevice.id);
    if (connection == null) {
      return Future.value(null);
    }
    return await connection.disconnect();
  }

  Future<int> _retrieveBatteryLevel(String deviceId) async {
    var connection = await _deviceService.ensureConnection(deviceId);
    if (connection == null) {
      return -1;
    }
    return connection.retrieveBatteryLevel();
  }

  Future<StreamSubscription<List<int>>?> _getBleBatteryLevelListener(
    String deviceId, {
    void Function(int)? onBatteryLevelChange,
  }) async {
    {
      var connection = await _deviceService.ensureConnection(deviceId);
      if (connection == null) {
        return Future.value(null);
      }
      return connection.getBleBatteryLevelListener(onBatteryLevelChange: onBatteryLevelChange);
    }
  }

  Future<List<int>> _getStorageList(String deviceId) async {
    final resolver = _storageListResolver;
    if (resolver != null) return resolver(deviceId);
    var connection = await _deviceService.ensureConnection(deviceId);
    if (connection == null) {
      return [];
    }
    return connection.getStorageList();
  }

  Future<BtDevice?> _getConnectedDevice() async {
    var deviceId = SharedPreferencesUtil().btDevice.id;
    if (deviceId.isEmpty) {
      return null;
    }
    var connection = await _deviceService.ensureConnection(deviceId);
    return connection?.device;
  }

  initiateBleBatteryListener({int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    if (connectedDevice == null) {
      return;
    }
    _bleBatteryLevelListener?.cancel();
    final listener = await _getBleBatteryLevelListener(
      connectedDevice!.id,
      onBatteryLevelChange: (int value) {
        if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
        batteryLevel = value;
        if (batteryLevel < 20 && !_hasLowBatteryAlerted) {
          _hasLowBatteryAlerted = true;
          final ctx = MyApp.navigatorKey.currentContext;
          NotificationService.instance.createNotification(
            title: ctx?.l10n.lowBatteryAlertTitle ?? "Low Battery Alert",
            body: ctx?.l10n.lowBatteryAlertBody ?? "Your device is running low on battery. Time for a recharge! 🔋",
          );
        } else if (batteryLevel > 20) {
          _hasLowBatteryAlerted = true;
        }
        // Throttle notifyListeners to reduce battery drain from excessive UI rebuilds
        // Only notify when: first reading, >=5% change, 15min elapsed, or crosses 20% threshold
        final delta = (_lastNotifiedBatteryLevel - value).abs();
        final elapsed = _lastBatteryNotifyTime == null
            ? const Duration(minutes: 999)
            : DateTime.now().difference(_lastBatteryNotifyTime!);
        final crossedLowBatteryThreshold =
            (value < 20 && _lastNotifiedBatteryLevel >= 20) || (value >= 20 && _lastNotifiedBatteryLevel < 20);
        final shouldNotify =
            _lastNotifiedBatteryLevel == -1 || delta >= 5 || elapsed.inMinutes >= 15 || crossedLowBatteryThreshold;
        if (shouldNotify) {
          _lastNotifiedBatteryLevel = value;
          _lastBatteryNotifyTime = DateTime.now();
          notifyListeners();
        }
      },
    );
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) {
      await listener?.cancel();
      return;
    }
    _bleBatteryLevelListener = listener;
    notifyListeners();
  }

  /// Updates battery level with throttling logic. Returns true if notifyListeners was called.
  /// This method is exposed for testing the throttling behavior.
  @visibleForTesting
  bool updateBatteryLevelForTesting(int value, {DateTime? now}) {
    batteryLevel = value;
    final currentTime = now ?? DateTime.now();

    // Throttle notifyListeners to reduce battery drain from excessive UI rebuilds
    // Only notify when: first reading, >=5% change, 15min elapsed, or crosses 20% threshold
    final delta = (_lastNotifiedBatteryLevel - value).abs();
    final elapsed =
        _lastBatteryNotifyTime == null ? const Duration(minutes: 999) : currentTime.difference(_lastBatteryNotifyTime!);
    final crossedLowBatteryThreshold =
        (value < 20 && _lastNotifiedBatteryLevel >= 20) || (value >= 20 && _lastNotifiedBatteryLevel < 20);
    final shouldNotify =
        _lastNotifiedBatteryLevel == -1 || delta >= 5 || elapsed.inMinutes >= 15 || crossedLowBatteryThreshold;
    if (shouldNotify) {
      _lastNotifiedBatteryLevel = value;
      _lastBatteryNotifyTime = currentTime;
      notifyListeners();
      return true;
    }
    return false;
  }

  /// Resets battery throttling state for testing.
  @visibleForTesting
  void resetBatteryThrottlingForTesting() {
    _lastNotifiedBatteryLevel = -1;
    _lastBatteryNotifyTime = null;
  }

  Future periodicConnect(
    String printer, {
    bool boundDeviceOnly = false,
    int? operationGeneration,
  }) async {
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    _reconnectionTimer?.cancel();
    _automaticReconnectAttempts = 0;
    _automaticReconnectExhausted = false;
    scan(t) async {
      if (!_isDeviceOperationCurrent(generation)) {
        t.cancel();
        return;
      }
      debugPrint("Periodic connect triggered at ${DateTime.now()}");

      final deviceService = _deviceService;
      if (deviceService is DeviceService && deviceService.isWifiSyncInProgress) {
        debugPrint("Skipping BLE reconnect - WiFi sync in progress");
        return;
      }
      if (_reconnectAt != null && _reconnectAt!.isAfter(DateTime.now())) {
        return;
      }
      if (boundDeviceOnly && SharedPreferencesUtil().btDevice.id.isEmpty) {
        t.cancel();
        return;
      }
      Logger.debug("isConnected: $isConnected, isConnecting: $isConnecting, connectedDevice: $connectedDevice");
      if (!isConnected) {
        if (isConnecting) {
          return;
        }
        if (_automaticReconnectAttempts >= _maxAutomaticReconnectAttempts) {
          t.cancel();
          isConnecting = false;
          _automaticReconnectExhausted = true;
          notifyListeners();
          return;
        }
        _automaticReconnectAttempts++;
        try {
          await scanAndConnectToDevice(operationGeneration: generation);
        } catch (error) {
          Logger.debug('Automatic BLE reconnect failed: $error');
          if (_isDeviceOperationCurrent(generation)) {
            if (!isConnected) {
              connectedDevice = null;
              isDeviceStorageSupport = false;
            }
            updateConnectingStatus(false);
          }
        }
        if (!_isDeviceOperationCurrent(generation)) return;
        if (!isConnected && _automaticReconnectAttempts >= _maxAutomaticReconnectAttempts) {
          t.cancel();
          isConnecting = false;
          _automaticReconnectExhausted = true;
          notifyListeners();
        }
      } else {
        t.cancel();
      }
    }

    _reconnectionTimer = Timer.periodic(_reconnectionInterval, scan);
    scan(_reconnectionTimer);
  }

  Future<BtDevice?> _scanConnectDevice(int operationGeneration) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return null;
    var device = await _getConnectedDevice();
    if (!_isDeviceOperationCurrent(operationGeneration)) return null;
    if (device != null) {
      return device;
    }

    final pairedDeviceId = SharedPreferencesUtil().btDevice.id;
    if (pairedDeviceId.isNotEmpty) {
      try {
        Logger.debug('Attempting direct reconnection to paired device: $pairedDeviceId');
        await _deviceService.ensureConnection(pairedDeviceId, force: true);
        if (!_isDeviceOperationCurrent(operationGeneration)) return null;

        // Check if connection succeeded
        await Future.delayed(const Duration(seconds: 2));
        if (!_isDeviceOperationCurrent(operationGeneration)) return null;
        device = await _getConnectedDevice();
        if (!_isDeviceOperationCurrent(operationGeneration)) return null;
        if (device != null) {
          Logger.debug('Direct reconnection successful');
          return device;
        }
      } catch (e) {
        Logger.debug('Direct reconnection failed: $e');
      }
    }

    await _deviceService.discover(desirableDeviceId: pairedDeviceId);
    if (!_isDeviceOperationCurrent(operationGeneration)) return null;

    // Waiting for the device connected (if any)
    await Future.delayed(const Duration(seconds: 2));
    if (!_isDeviceOperationCurrent(operationGeneration)) return null;
    if (connectedDevice != null) {
      return connectedDevice;
    }
    return null;
  }

  Future scanAndConnectToDevice({int? operationGeneration}) async {
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    updateConnectingStatus(true);
    if (isConnected) {
      if (connectedDevice == null) {
        final resolvedDevice = await _getConnectedDevice();
        if (!_isDeviceOperationCurrent(generation)) return;
        if (resolvedDevice == null) {
          updateConnectingStatus(false);
          return;
        }
        connectedDevice = resolvedDevice;
        await SharedPreferencesUtil().saveString('deviceName', connectedDevice!.name);
        if (!_isDeviceOperationCurrent(generation)) return;
        MixpanelManager().deviceConnected();
      }

      if (!_isDeviceOperationCurrent(generation)) return;
      setIsConnected(true);
      updateConnectingStatus(false);
      notifyListeners();
      return;
    }

    // else
    var device = await (_scanConnector?.call() ?? _scanConnectDevice(generation));
    if (!_isDeviceOperationCurrent(generation)) return;
    Logger.debug('inside scanAndConnectToDevice $device in device_provider');
    if (device != null) {
      var cDevice = await _resolveConnectedDevice(device.id) ?? device;
      if (!_isDeviceOperationCurrent(generation)) return;
      await setConnectedDevice(cDevice, operationGeneration: generation);
      if (!_isDeviceOperationCurrent(generation)) return;
      await setisDeviceStorageSupport(operationGeneration: generation);
      if (!_isDeviceOperationCurrent(generation)) return;
      await SharedPreferencesUtil().saveString('deviceName', cDevice.name);
      if (!_isDeviceOperationCurrent(generation)) return;
      MixpanelManager().deviceConnected();
      setIsConnected(true);
      Logger.debug('device is not null $cDevice');
    }
    if (!_isDeviceOperationCurrent(generation)) return;
    updateConnectingStatus(false);

    notifyListeners();
  }

  void updateConnectingStatus(bool value) {
    isConnecting = value;
    notifyListeners();
  }

  void setIsConnected(bool value) {
    isConnected = value;
    if (isConnected) {
      _reconnectionTimer?.cancel();
      _automaticReconnectAttempts = 0;
      _automaticReconnectExhausted = false;
    }
    notifyListeners();
  }

  @override
  void dispose() {
    captureProvider?.removeListener(_onCaptureProviderChanged);
    _clearDeferredDeviceCapture();
    _bleBatteryLevelListener?.cancel();
    _reconnectionTimer?.cancel();
    _disconnectDebouncer.cancel();
    _connectDebouncer.cancel();
    _deviceService.unsubscribe(this);
    super.dispose();
  }

  Future<void> onDeviceDisconnected({int? operationGeneration, String? deviceId}) async {
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    Logger.debug('onDisconnected inside: $connectedDevice');
    final disconnectedDeviceId = deviceId ?? connectedDevice?.id ?? pairedDevice?.id;
    _deviceOperationGeneration++;
    _havingNewFirmware = false;
    connectedDevice = null;
    _clearDeferredDeviceCapture();
    final storedDevice = SharedPreferencesUtil().btDevice;
    pairedDevice = storedDevice.id.isEmpty ? null : storedDevice;
    isConnected = false;
    isConnecting = false;
    isDeviceStorageSupport = false;
    notifyListeners();

    final captureTeardown = _teardownCaptureForDevice(disconnectedDeviceId);

    // Wals
    ServiceManager.instance().wal.getSyncs().sdcard.setDevice(null);
    ServiceManager.instance().wal.getSyncs().flashPage.setDevice(null);

    PlatformManager.instance.crashReporter.logInfo('Omi Device Disconnected');
    _disconnectNotificationTimer?.cancel();
    _disconnectNotificationTimer = Timer(const Duration(seconds: 30), () {
      final ctx = MyApp.navigatorKey.currentContext;
      NotificationService.instance.createNotification(
        title: ctx?.l10n.deviceDisconnectedNotificationTitle ?? 'Your Omi Device Disconnected',
        body: ctx?.l10n.deviceDisconnectedNotificationBody ?? 'Please reconnect to continue using your Omi.',
      );
    });
    MixpanelManager().deviceDisconnected();

    await captureTeardown;
    if (!_deviceServiceReady) return;
    final reconnectGeneration = ++_deviceOperationGeneration;

    // Retired 1s to prevent the race condition made by standby power of ble device
    Future.delayed(const Duration(seconds: 1), () {
      if (_isDeviceOperationCurrent(reconnectGeneration)) {
        periodicConnect('coming from onDisconnect', operationGeneration: reconnectGeneration);
      }
    });
  }

  Future<(String, bool, String, Map)> shouldUpdateFirmware() async {
    if (pairedDevice == null || connectedDevice == null) {
      return ('No paired device is connected', false, '', {});
    }

    var device = pairedDevice!;
    var latestFirmwareDetails = await getLatestFirmwareVersion(
      deviceModelNumber: device.modelNumber,
      firmwareRevision: device.firmwareRevision,
      hardwareRevision: device.hardwareRevision,
      manufacturerName: device.manufacturerName,
    );

    var (message, hasUpdate, version) = await DeviceUtils.shouldUpdateFirmware(
        currentFirmware: device.firmwareRevision, latestFirmwareDetails: latestFirmwareDetails);
    return (message, hasUpdate, version, latestFirmwareDetails);
  }

  Future<void> _onDeviceConnected(BtDevice device, int operationGeneration) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    Logger.debug('_onConnected inside: $connectedDevice');
    _disconnectNotificationTimer?.cancel();
    try {
      NotificationService.instance.clearNotification(1);
    } catch (error) {
      Logger.debug('Could not clear the stale disconnect notification: $error');
    }
    // The transport callback is the connection authority. Publish the device
    // and connected bit in one synchronous turn before any setup await so a
    // failed overlapping scan cannot clear half of the committed state.
    connectedDevice = device;
    pairedDevice = device;
    setIsConnected(true);
    final capture = captureProvider;
    capture?.updateRecordingDevice(device);

    try {
      // Capture is the critical post-connect path. Metadata, storage, and
      // battery probes are useful but must never prevent necklace recording.
      if (capture?.phoneCaptureOwnsMobileAudio == true) {
        _deferDeviceCaptureUntilPhoneReleases(device, operationGeneration);
      } else {
        await _startDeviceCaptureWithRetry(device, operationGeneration);
      }
      if (!_isDeviceOperationCurrent(operationGeneration)) return;

      await _runConnectedSetupStep(
        'device info',
        operationGeneration,
        () => getDeviceInfo(operationGeneration: operationGeneration),
      );
      await _runConnectedSetupStep(
        'storage support',
        operationGeneration,
        () => setisDeviceStorageSupport(operationGeneration: operationGeneration),
      );
      await _runConnectedSetupStep('initial battery', operationGeneration, () async {
        final currentLevel = await _retrieveBatteryLevel(device.id);
        if (!_isDeviceOperationCurrent(operationGeneration)) return;
        if (currentLevel != -1) batteryLevel = currentLevel;
      });
      await _runConnectedSetupStep(
        'battery listener',
        operationGeneration,
        () => initiateBleBatteryListener(operationGeneration: operationGeneration),
      );
      if (batteryLevel != -1 && batteryLevel < 20) _hasLowBatteryAlerted = false;
      await _runConnectedSetupStep(
        'device name persistence',
        operationGeneration,
        () => SharedPreferencesUtil().saveString('deviceName', device.name),
      );
      if (!_isDeviceOperationCurrent(operationGeneration)) return;
    } finally {
      if (_isDeviceOperationCurrent(operationGeneration)) updateConnectingStatus(false);
    }

    // Wals
    ServiceManager.instance().wal.getSyncs().sdcard.setDevice(device);
    ServiceManager.instance().wal.getSyncs().flashPage.setDevice(device);

    notifyListeners();

    // Check firmware updates
    _checkFirmwareUpdates(operationGeneration: operationGeneration);

    onDeviceConnected?.call(device);
  }

  Future<void> _runConnectedSetupStep(
    String name,
    int operationGeneration,
    Future<void> Function() step,
  ) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    try {
      await step();
    } catch (error) {
      Logger.debug('Connected device $name setup failed without suppressing capture: $error');
    }
  }

  Future<bool> _startDeviceCaptureWithRetry(BtDevice device, int operationGeneration) async {
    final capture = captureProvider;
    if (capture == null) return false;
    for (var attempt = 1; attempt <= _maxDeviceCaptureStartAttempts; attempt++) {
      if (!_isDeviceOperationCurrent(operationGeneration)) return false;
      try {
        await capture.streamDeviceRecording(device: device);
      } catch (error) {
        Logger.debug('Necklace capture start attempt $attempt failed: $error');
      }
      if (!_isDeviceOperationCurrent(operationGeneration)) return false;
      if (capture.recordingState == RecordingState.deviceRecord) return true;
      if (attempt < _maxDeviceCaptureStartAttempts) {
        await Future<void>.delayed(_deviceCaptureRetryDelay);
      }
    }
    Logger.debug('Necklace transport is connected but capture did not become ready after '
        '$_maxDeviceCaptureStartAttempts attempts');
    return false;
  }

  Future<void> _handleDeviceConnected(String deviceId, int operationGeneration) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    final device = await _resolveConnectedDevice(deviceId);
    if (device == null || !_isDeviceOperationCurrent(operationGeneration)) return;
    await _onDeviceConnected(device, operationGeneration);
  }

  void _checkFirmwareUpdates({int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    if (_isFirmwareUpdateInProgress) {
      return;
    }

    await checkFirmwareUpdates();
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;

    // Show firmware update dialog if needed
    if (_havingNewFirmware) {
      // Use a small delay to ensure the UI is ready
      Future.delayed(const Duration(milliseconds: 500), () {
        if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
        final context = MyApp.navigatorKey.currentContext;
        if (context != null) {
          showFirmwareUpdateDialog(context);
        }
      });
    }
  }

  bool get _isOmiGlassDevice {
    if (pairedDevice == null) return false;
    if (pairedDevice!.type == DeviceType.openglass) return true;
    final name = pairedDevice!.name.toLowerCase();
    return name.contains('openglass') || name.contains('omiglass') || name.contains('glass');
  }

  Future checkFirmwareUpdates() async {
    int retryCount = 0;
    const maxRetries = 3;
    const retryDelay = Duration(seconds: 3);

    while (retryCount < maxRetries) {
      try {
        var (message, hasUpdate, version, firmwareDetails) = await shouldUpdateFirmware();
        _havingNewFirmware = hasUpdate;
        _latestFirmwareVersion = version.isNotEmpty ? version : message;

        // For OmiGlass devices, populate the firmware details for the OTA UI
        if (_isOmiGlassDevice && firmwareDetails.isNotEmpty) {
          // Map backend response to OmiGlass OTA UI expected format
          final versionStr = firmwareDetails['version']?.toString() ?? '';
          final cleanVersion = versionStr.startsWith('v') ? versionStr.substring(1) : versionStr;
          final changelog = firmwareDetails['changelog'];
          final changelogStr = changelog is List ? changelog.join('\n') : (changelog?.toString() ?? '');

          _latestOmiGlassFirmwareDetails = {
            'version': cleanVersion,
            'download_url': firmwareDetails['zip_url'] ?? '',
            'changelog': changelogStr,
          };
        }

        notifyListeners();
        return hasUpdate;
      } catch (e) {
        retryCount++;
        Logger.debug('Error checking firmware update (attempt $retryCount): $e');

        if (retryCount == maxRetries) {
          Logger.debug('Max retries reached, giving up');
          _havingNewFirmware = false;
          notifyListeners();
          break;
        }

        await Future.delayed(retryDelay);
      }
    }
    return;
  }

  // Track if user is currently viewing a firmware update page
  bool _isOnFirmwareUpdatePage = false;
  void setOnFirmwareUpdatePage(bool value) {
    _isOnFirmwareUpdatePage = value;
  }

  void showFirmwareUpdateDialog(BuildContext context) {
    if (!_havingNewFirmware ||
        !SharedPreferencesUtil().showFirmwareUpdateDialog ||
        _isFirmwareUpdateInProgress ||
        _isOnFirmwareUpdatePage) {
      return;
    }

    showDialog(
      context: context,
      builder: (context) => ConfirmationDialog(
        title: context.l10n.firmwareUpdateAvailable,
        description: context.l10n.firmwareUpdateAvailableDescription(_latestFirmwareVersion),
        confirmText: context.l10n.update,
        cancelText: context.l10n.later,
        onConfirm: () {
          Navigator.of(context).pop();
          setFirmwareUpdateInProgress(true);
          if (_isOmiGlassDevice) {
            Navigator.of(context).push(
              MaterialPageRoute(
                builder: (context) => OmiGlassOtaUpdate(
                  device: pairedDevice,
                  latestFirmwareDetails: _latestOmiGlassFirmwareDetails,
                ),
              ),
            );
          } else {
            Navigator.of(context).push(
              MaterialPageRoute(
                builder: (context) => FirmwareUpdate(device: pairedDevice),
              ),
            );
          }
        },
        onCancel: () {
          Navigator.of(context).pop();
        },
      ),
    );
  }

  Future setisDeviceStorageSupport({int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    if (connectedDevice == null) {
      isDeviceStorageSupport = false;
    } else {
      var storageFiles = await _getStorageList(connectedDevice!.id);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
      isDeviceStorageSupport = storageFiles.isNotEmpty;
    }
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    notifyListeners();
  }

  @override
  void onDeviceConnectionStateChanged(String deviceId, DeviceConnectionState state) async {
    Logger.debug("provider > device connection state changed...$deviceId...$state...${connectedDevice?.id}");
    switch (state) {
      case DeviceConnectionState.connected:
        _disconnectDebouncer.cancel();
        final generation = _deviceOperationGeneration;
        if (!_isDeviceOperationCurrent(generation)) return;
        _connectDebouncer.run(() => _handleDeviceConnected(deviceId, generation));
        break;
      case DeviceConnectionState.disconnected:
        _connectDebouncer.cancel();
        // Check if this is the paired device or currently connected device
        // Coz connectedDevice and pairedDevice are the same but connectedDevice becomes null after disconnect
        if (deviceId == connectedDevice?.id || deviceId == pairedDevice?.id) {
          final generation = _deviceOperationGeneration;
          if (!_isDeviceOperationCurrent(generation)) return;
          _disconnectDebouncer.run(
            () => onDeviceDisconnected(operationGeneration: generation, deviceId: deviceId),
          );
        }
        break;
    }
  }

  @override
  void onDevices(List<BtDevice> devices) async {}

  @override
  void onStatusChanged(DeviceServiceStatus status) {
    switch (status) {
      case DeviceServiceStatus.stop:
        final disconnectedDeviceId = connectedDevice?.id ?? pairedDevice?.id ?? captureProvider?.recordingDevice?.id;
        _deviceOperationGeneration++;
        _deviceServiceReady = false;
        _clearDeferredDeviceCapture();
        _reconnectionTimer?.cancel();
        _disconnectDebouncer.cancel();
        _connectDebouncer.cancel();
        _bleBatteryLevelListener?.cancel();
        _bleBatteryLevelListener = null;
        connectedDevice = null;
        pairedDevice = null;
        isConnected = false;
        isConnecting = false;
        isDeviceStorageSupport = false;
        batteryLevel = -1;
        unawaited(_teardownCaptureForDevice(disconnectedDeviceId));
        notifyListeners();
        break;
      case DeviceServiceStatus.ready:
        final generation = ++_deviceOperationGeneration;
        _deviceServiceReady = true;
        final stored = SharedPreferencesUtil().btDevice;
        pairedDevice = stored.id.isEmpty ? null : stored;
        notifyListeners();
        if (pairedDevice != null) {
          unawaited(_resumeBoundDeviceAfterTeardown(generation));
        }
        break;
      case DeviceServiceStatus.init:
        _deviceServiceReady = false;
        break;
      case DeviceServiceStatus.scanning:
        break;
    }
  }

  prepareDFU() {
    if (connectedDevice == null) {
      return;
    }
    _bleDisconnectDevice(connectedDevice!);
    _reconnectAt = DateTime.now().add(Duration(seconds: 30));
  }

  // Reset firmware update state when update completes or fails
  void resetFirmwareUpdateState() {
    _isFirmwareUpdateInProgress = false;
    notifyListeners();
  }

  // Set firmware update state when starting an update
  void setFirmwareUpdateInProgress(bool inProgress) {
    _isFirmwareUpdateInProgress = inProgress;
    notifyListeners();
  }
}
