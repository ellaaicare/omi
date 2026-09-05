import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/http/api/device.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/main.dart';
import 'package:omi/ella/services/diagnostics/ella_diagnostic_event.dart';
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
typedef DevicePreferenceWriter = Future<void> Function(BtDevice device);

class DeviceProvider extends ChangeNotifier with WidgetsBindingObserver implements IDeviceServiceSubsciption {
  DeviceProvider({
    IDeviceService? deviceService,
    DeviceConnectionResolver? connectionResolver,
    DeviceScanConnector? scanConnector,
    @visibleForTesting DeviceStorageListResolver? storageListResolver,
    @visibleForTesting Duration reconnectionInterval = const Duration(seconds: 15),
    @visibleForTesting int maxAutomaticReconnectAttempts = 3,
    @visibleForTesting Duration automaticReconnectCooldown = const Duration(minutes: 1),
    @visibleForTesting Duration deviceCaptureRetryDelay = const Duration(milliseconds: 500),
    @visibleForTesting int maxDeviceCaptureStartAttempts = 3,
    @visibleForTesting DevicePreferenceWriter? rememberedDeviceWriter,
    @visibleForTesting bool automaticallyReconnectOnReady = true,
  })  : _deviceService = deviceService ?? ServiceManager.instance().device,
        _connectionResolver = connectionResolver,
        _scanConnector = scanConnector,
        _storageListResolver = storageListResolver,
        _reconnectionInterval = reconnectionInterval,
        _maxAutomaticReconnectAttempts = maxAutomaticReconnectAttempts,
        _automaticReconnectCooldown = automaticReconnectCooldown,
        _deviceCaptureRetryDelay = deviceCaptureRetryDelay,
        _maxDeviceCaptureStartAttempts = maxDeviceCaptureStartAttempts,
        _rememberedDeviceWriter = rememberedDeviceWriter ?? SharedPreferencesUtil().btDeviceSet,
        _automaticallyReconnectOnReady = automaticallyReconnectOnReady {
    _lastDeviceOwnerBinding = _rememberedDeviceOwnerBinding();
    _requiresExplicitDeviceSelectionAfterAuthorityChange = _lastDeviceOwnerBinding == null;
    WidgetsBinding.instance.addObserver(this);
    _deviceService.subscribe(this, this);
    _accountAuthorityChanges.addListener(_handleAccountAuthorityChanged);
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
  final Duration _automaticReconnectCooldown;
  final Duration _deviceCaptureRetryDelay;
  final int _maxDeviceCaptureStartAttempts;
  final DevicePreferenceWriter _rememberedDeviceWriter;
  final bool _automaticallyReconnectOnReady;
  final ValueListenable<int> _accountAuthorityChanges = SharedPreferencesUtil.aiConsentAuthorityChanges;
  int _automaticReconnectAttempts = 0;
  bool _automaticReconnectExhausted = false;
  DateTime? _automaticReconnectCooldownUntil;

  int get automaticReconnectAttempts => _automaticReconnectAttempts;
  bool get automaticReconnectExhausted => _automaticReconnectExhausted;

  bool _havingNewFirmware = false;
  bool get havingNewFirmware => _havingNewFirmware && pairedDevice != null && isConnected;

  BtDevice? get presentationConnectedDevice =>
      SharedPreferencesUtil().demoMode ? _demoDevice : _nonEmptyPresentationDevice(connectedDevice);

  BtDevice? get presentationPairedDevice =>
      SharedPreferencesUtil().demoMode ? _demoDevice : _nonEmptyPresentationDevice(pairedDevice);

  BtDevice? _nonEmptyPresentationDevice(BtDevice? device) => device?.id.isNotEmpty == true ? device : null;

  bool get presentationIsConnected =>
      SharedPreferencesUtil().demoMode || (isConnected && connectedDevice?.id.isNotEmpty == true);

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
  int _rememberedDeviceAuthorityGeneration = 0;
  int _authorityReconciliationGeneration = 0;
  Future<void> _rememberedDeviceWriteTail = Future<void>.value();
  bool _deviceServiceReady = false;
  Future<void>? _captureTeardown;
  int? _deferredDeviceCaptureGeneration;
  String? _deferredDeviceCaptureId;
  Future<void>? _deferredDeviceCaptureStart;
  // Native BLE callbacks carry only a device id, never Firebase authority.
  // Keep them fenced until a device is owner-bound or deliberately selected.
  bool _requiresExplicitDeviceSelectionAfterAuthorityChange = true;
  bool _authorityReconciliationPending = false;
  String? _lastDeviceOwnerBinding;
  int? _activeDeviceConnectionSession;
  final Map<int, EllaDiagnosticCaptureTrace> _diagnosticTraceByOperationGeneration =
      <int, EllaDiagnosticCaptureTrace>{};
  final Set<int> _diagnosticResolutionStartedByOperationGeneration = <int>{};
  bool _disposed = false;

  void Function(BtDevice device)? onDeviceConnected;

  bool _isDeviceOperationCurrent(int generation) =>
      !_disposed && _deviceServiceReady && _captureTeardown == null && generation == _deviceOperationGeneration;

  bool _isCurrentDeviceConnectionSession(int connectionGeneration) =>
      !_disposed && connectionGeneration == _activeDeviceConnectionSession;

  String? _rememberedDeviceOwnerBinding() {
    final preferences = SharedPreferencesUtil();
    final uid = preferences.uid.trim();
    final profileBindingId = preferences.aiConsentProfileBindingId.trim();
    if (uid.isEmpty || profileBindingId.isEmpty) return null;
    return '$uid\u001f$profileBindingId';
  }

  BtDevice? _rememberedDeviceForCurrentAuthority() {
    final preferences = SharedPreferencesUtil();
    final ownerBinding = _rememberedDeviceOwnerBinding();
    if (ownerBinding == null) return null;
    final device = preferences.btDevice;
    if (device.id.isEmpty || preferences.btDeviceOwnerBinding != ownerBinding) return null;
    return device;
  }

  /// A non-empty record written before owner binding existed. It is intentionally
  /// not treated as paired: Home may offer one explicit confirmation, but no
  /// reconnect or capture can begin until that confirmation binds it to the
  /// current Firebase UID/profile.
  BtDevice? get legacyUntrustedDeviceCandidate {
    final preferences = SharedPreferencesUtil();
    if (_rememberedDeviceOwnerBinding() == null) return null;
    final device = preferences.btDevice;
    if (device.id.isEmpty || preferences.btDeviceOwnerBinding.trim().isNotEmpty) return null;
    return device;
  }

  bool _isLegacyCandidateCurrent({
    required BtDevice candidate,
    required String ownerBinding,
    required int authorityGeneration,
  }) {
    final preferences = SharedPreferencesUtil();
    return !_disposed &&
        _deviceServiceReady &&
        authorityGeneration == _rememberedDeviceAuthorityGeneration &&
        ownerBinding == _rememberedDeviceOwnerBinding() &&
        preferences.btDevice.id == candidate.id &&
        preferences.btDeviceOwnerBinding.trim().isEmpty;
  }

  bool _isCurrentOwnerBoundDevice(String deviceId) {
    final boundDevice = _rememberedDeviceForCurrentAuthority();
    return boundDevice != null && boundDevice.id == deviceId;
  }

  void _handleAccountAuthorityChanged() {
    // The notifier can fire immediately before a replacement UID/profile is
    // persisted. Fence callbacks now, then reconcile settled preferences.
    _rememberedDeviceAuthorityGeneration++;
    _deviceOperationGeneration++;
    _requiresExplicitDeviceSelectionAfterAuthorityChange = true;
    _connectDebouncer.cancel();
    _authorityReconciliationPending = true;
    final reconciliationGeneration = ++_authorityReconciliationGeneration;
    unawaited(_reconcileAccountAuthorityChange(reconciliationGeneration));
  }

  Future<void> _reconcileAccountAuthorityChange(int reconciliationGeneration) async {
    // Preference writes update the in-memory store synchronously. Yielding one
    // microtask lets the profile write share this authority transition without
    // leaving a timer alive after a widget/provider is disposed.
    await Future<void>.microtask(() {});
    if (_disposed || reconciliationGeneration != _authorityReconciliationGeneration) return;

    final settledOwnerBinding = _rememberedDeviceOwnerBinding();
    final ownerChanged = settledOwnerBinding != _lastDeviceOwnerBinding;
    _authorityReconciliationPending = false;

    if (!ownerChanged && settledOwnerBinding != null) {
      // A consent receipt refresh for the same account/profile must preserve a
      // healthy necklace rather than require a Settings round-trip.
      _requiresExplicitDeviceSelectionAfterAuthorityChange = false;
      pairedDevice ??= _rememberedDeviceForCurrentAuthority();
      notifyListeners();
      if (!isConnected && pairedDevice != null) {
        unawaited(resumeKnownDeviceConnection(reason: 'same-owner authority refresh'));
      }
      return;
    }

    // A new owner cannot inherit capture from the preceding authority. Only a
    // fresh, owner-bound reconnect started after this point can clear the fence.
    _lastDeviceOwnerBinding = settledOwnerBinding;
    _activeDeviceConnectionSession = null;
    _diagnosticTraceByOperationGeneration.clear();
    _diagnosticResolutionStartedByOperationGeneration.clear();
    final disconnectedDeviceId = connectedDevice?.id ?? pairedDevice?.id ?? captureProvider?.recordingDevice?.id;
    _reconnectionTimer?.cancel();
    _disconnectNotificationTimer?.cancel();
    _disconnectDebouncer.cancel();
    _bleBatteryLevelListener?.cancel();
    _bleBatteryLevelListener = null;
    _clearDeferredDeviceCapture();
    connectedDevice = null;
    pairedDevice = _rememberedDeviceForCurrentAuthority();
    isConnected = false;
    isConnecting = false;
    isDeviceStorageSupport = false;
    batteryLevel = -1;
    _automaticReconnectAttempts = 0;
    _automaticReconnectExhausted = false;
    _automaticReconnectCooldownUntil = null;
    final operationGeneration = _deviceOperationGeneration;
    final teardown = _teardownCaptureForDevice(disconnectedDeviceId);
    notifyListeners();

    if (settledOwnerBinding != null && pairedDevice != null) {
      await teardown;
      if (!_isDeviceOperationCurrent(operationGeneration)) return;
      unawaited(_resumeBoundDeviceAfterTeardown(operationGeneration));
    }
  }

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
    await periodicConnect('device service resumed', boundDeviceOnly: true, operationGeneration: generation);
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
    if (device != null) {
      await _persistRememberedDevice(device, operationGeneration: operationGeneration);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    }
    await getDeviceInfo(operationGeneration: operationGeneration);
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    Logger.debug('setConnectedDevice: $device');
    notifyListeners();
  }

  /// Commits a device selected by the current user after the scanner has
  /// resolved it. This is the only post-authority-change path allowed to
  /// restart necklace capture; unsolicited BLE callbacks remain fenced.
  Future<void> confirmConnectedDeviceForCurrentAuthority(BtDevice device) async {
    if (!_deviceServiceReady) {
      await setConnectedDevice(device);
      return;
    }
    final generation = ++_deviceOperationGeneration;
    await _onDeviceConnected(device, generation, explicitlyAuthorized: true);
  }

  Future<void> _persistRememberedDevice(BtDevice device, {int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    final authorityGeneration = _rememberedDeviceAuthorityGeneration;
    final ownerBinding = _rememberedDeviceOwnerBinding();
    final expectedDeviceId = device.id;
    if (expectedDeviceId.isEmpty || ownerBinding == null) return;
    final previousWrite = _rememberedDeviceWriteTail;
    late final Future<void> write;
    write = (() async {
      await previousWrite.catchError((_) {});
      if (!_isRememberedDeviceWriteCurrent(
        authorityGeneration: authorityGeneration,
        ownerBinding: ownerBinding,
        operationGeneration: operationGeneration,
        deviceId: expectedDeviceId,
      )) {
        return;
      }
      await _rememberedDeviceWriter(device);
      if (_isRememberedDeviceWriteCurrent(
        authorityGeneration: authorityGeneration,
        ownerBinding: ownerBinding,
        operationGeneration: operationGeneration,
        deviceId: expectedDeviceId,
      )) {
        await SharedPreferencesUtil().btDeviceOwnerBindingSet(ownerBinding);
        return;
      }
      // The serialized tail guarantees a newer account/device write runs only
      // after this stale binding is removed.
      if (SharedPreferencesUtil().btDevice.id == expectedDeviceId) {
        await _rememberedDeviceWriter(BtDevice.empty());
        await SharedPreferencesUtil().btDeviceOwnerBindingSet('');
      }
    })();
    _rememberedDeviceWriteTail = write;
    await write;
  }

  bool _isRememberedDeviceWriteCurrent({
    required int authorityGeneration,
    required String ownerBinding,
    required int? operationGeneration,
    required String deviceId,
  }) {
    if (authorityGeneration != _rememberedDeviceAuthorityGeneration ||
        ownerBinding != _rememberedDeviceOwnerBinding()) {
      return false;
    }
    if (operationGeneration == null || operationGeneration == _deviceOperationGeneration) return true;
    return pairedDevice?.id == deviceId || connectedDevice?.id == deviceId;
  }

  Future getDeviceInfo({int? operationGeneration}) async {
    if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    if (connectedDevice?.id.isNotEmpty == true) {
      if (pairedDevice?.firmwareRevision != null && pairedDevice?.firmwareRevision != 'Unknown') {
        return;
      }
      var connection = await _deviceService.ensureConnection(connectedDevice!.id);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
      final info = await connectedDevice?.getDeviceInfo(connection);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
      pairedDevice = info;
      await _persistRememberedDevice(pairedDevice!, operationGeneration: operationGeneration);
      if (operationGeneration != null && !_isDeviceOperationCurrent(operationGeneration)) return;
    } else {
      final rememberedDevice = _rememberedDeviceForCurrentAuthority();
      if (rememberedDevice == null) {
        // An empty sentinel is storage hygiene, not a paired necklace. Keep
        // the presentation state null so Home never offers a fictitious
        // reconnect path after logout or an authority change.
        pairedDevice = null;
      } else {
        pairedDevice = rememberedDevice;
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
    var deviceId = _rememberedDeviceForCurrentAuthority()?.id ?? '';
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

  Future periodicConnect(String printer, {bool boundDeviceOnly = false, int? operationGeneration}) async {
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    _reconnectionTimer?.cancel();
    _automaticReconnectAttempts = 0;
    _automaticReconnectExhausted = false;
    _automaticReconnectCooldownUntil = null;
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
      if (boundDeviceOnly && _rememberedDeviceForCurrentAuthority() == null) {
        t.cancel();
        return;
      }
      Logger.debug("isConnected: $isConnected, isConnecting: $isConnecting, connectedDevice: $connectedDevice");
      if (!isConnected) {
        if (isConnecting) {
          return;
        }
        if (_automaticReconnectAttempts >= _maxAutomaticReconnectAttempts) {
          final cooldownUntil = _automaticReconnectCooldownUntil;
          if (cooldownUntil != null && cooldownUntil.isAfter(DateTime.now())) return;
          _automaticReconnectAttempts = 0;
          _automaticReconnectExhausted = false;
          _automaticReconnectCooldownUntil = null;
          notifyListeners();
        }
        _automaticReconnectAttempts++;
        try {
          await scanAndConnectToDevice(operationGeneration: generation, startCaptureWhenConnected: boundDeviceOnly);
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
          isConnecting = false;
          _automaticReconnectExhausted = true;
          _automaticReconnectCooldownUntil = DateTime.now().add(_automaticReconnectCooldown);
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

    final pairedDeviceId = _rememberedDeviceForCurrentAuthority()?.id ?? '';
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

  Future scanAndConnectToDevice({int? operationGeneration, bool startCaptureWhenConnected = false}) async {
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    EllaDiagnosticCaptureTrace? diagnosticTrace;
    if (startCaptureWhenConnected) {
      final rememberedDevice = _rememberedDeviceForCurrentAuthority();
      diagnosticTrace = _diagnosticTraceByOperationGeneration[generation];
      if (diagnosticTrace == null) {
        diagnosticTrace =
            rememberedDevice == null ? null : captureProvider?.beginDeviceDiagnosticTrace(rememberedDevice);
      } else if (_diagnosticResolutionStartedByOperationGeneration.contains(generation)) {
        diagnosticTrace = diagnosticTrace.nextAttempt();
      }
      if (diagnosticTrace != null) {
        _diagnosticTraceByOperationGeneration[generation] = diagnosticTrace;
        _diagnosticResolutionStartedByOperationGeneration.add(generation);
        unawaited(
          diagnosticTrace.emit(
            layer: EllaDiagnosticLayer.bleTransport,
            eventName: 'peripheral_resolution',
            outcome: EllaDiagnosticOutcome.started,
            retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
            expectedNextEvent: 'peripheral_connected',
            deadlineMs: 15000,
            safeCounters: <String, int>{'retry_number': diagnosticTrace.retryNumber},
          ),
        );
      }
    }
    updateConnectingStatus(true);
    if (isConnected) {
      if (connectedDevice == null) {
        final resolvedDevice = await _getConnectedDevice();
        if (!_isDeviceOperationCurrent(generation)) return;
        if (resolvedDevice == null) {
          if (diagnosticTrace != null) {
            unawaited(
              diagnosticTrace.emit(
                layer: EllaDiagnosticLayer.bleTransport,
                eventName: 'peripheral_resolution',
                outcome: EllaDiagnosticOutcome.failed,
                retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
                failureCode: EllaDiagnosticFailureCode.rememberedDeviceNotResolved,
                safeCounters: <String, int>{'retry_number': diagnosticTrace.retryNumber},
              ),
            );
          }
          updateConnectingStatus(false);
          return;
        }
        if (startCaptureWhenConnected) {
          if (!_isCurrentOwnerBoundDevice(resolvedDevice.id)) return;
          await _onDeviceConnected(resolvedDevice, generation, explicitlyAuthorized: true);
        } else {
          connectedDevice = resolvedDevice;
          await SharedPreferencesUtil().saveString('deviceName', connectedDevice!.name);
        }
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
    BtDevice? device;
    try {
      device = await (_scanConnector?.call() ?? _scanConnectDevice(generation));
    } catch (_) {
      if (diagnosticTrace != null) {
        unawaited(
          diagnosticTrace.emit(
            layer: EllaDiagnosticLayer.bleTransport,
            eventName: 'peripheral_resolution',
            outcome: EllaDiagnosticOutcome.failed,
            retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
            failureCode: EllaDiagnosticFailureCode.peripheralConnectTimeout,
            safeCounters: <String, int>{'retry_number': diagnosticTrace.retryNumber},
          ),
        );
      }
      rethrow;
    }
    if (!_isDeviceOperationCurrent(generation)) return;
    Logger.debug('inside scanAndConnectToDevice $device in device_provider');
    if (device != null) {
      var cDevice = await _resolveConnectedDevice(device.id) ?? device;
      if (!_isDeviceOperationCurrent(generation)) return;
      if (startCaptureWhenConnected) {
        if (!_isCurrentOwnerBoundDevice(cDevice.id)) return;
        await _onDeviceConnected(cDevice, generation, explicitlyAuthorized: true);
      } else {
        await setConnectedDevice(cDevice, operationGeneration: generation);
      }
      if (!_isDeviceOperationCurrent(generation)) return;
      await setisDeviceStorageSupport(operationGeneration: generation);
      if (!_isDeviceOperationCurrent(generation)) return;
      await SharedPreferencesUtil().saveString('deviceName', cDevice.name);
      if (!_isDeviceOperationCurrent(generation)) return;
      MixpanelManager().deviceConnected();
      setIsConnected(true);
      Logger.debug('device is not null $cDevice');
    } else if (diagnosticTrace != null) {
      unawaited(
        diagnosticTrace.emit(
          layer: EllaDiagnosticLayer.bleTransport,
          eventName: 'peripheral_resolution',
          outcome: EllaDiagnosticOutcome.failed,
          retryClass: EllaDiagnosticRetryClass.boundedAutomatic,
          failureCode: EllaDiagnosticFailureCode.peripheralConnectTimeout,
          safeCounters: <String, int>{'retry_number': diagnosticTrace.retryNumber},
        ),
      );
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
      _automaticReconnectCooldownUntil = null;
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _authorityReconciliationGeneration++;
    _authorityReconciliationPending = false;
    _activeDeviceConnectionSession = null;
    _diagnosticTraceByOperationGeneration.clear();
    _diagnosticResolutionStartedByOperationGeneration.clear();
    WidgetsBinding.instance.removeObserver(this);
    _accountAuthorityChanges.removeListener(_handleAccountAuthorityChanged);
    captureProvider?.removeListener(_onCaptureProviderChanged);
    _clearDeferredDeviceCapture();
    _bleBatteryLevelListener?.cancel();
    _reconnectionTimer?.cancel();
    _disconnectDebouncer.cancel();
    _connectDebouncer.cancel();
    _deviceService.unsubscribe(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      unawaited(resumeKnownDeviceConnection(reason: 'app resumed'));
    }
  }

  Future<void> resumeKnownDeviceConnection({required String reason}) async {
    if (!_deviceServiceReady || isConnected) return;
    final stored = _rememberedDeviceForCurrentAuthority();
    if (stored == null) return;
    await _captureTeardown;
    if (!_deviceServiceReady || isConnected) return;
    final generation = ++_deviceOperationGeneration;
    pairedDevice = stored;
    _automaticReconnectCooldownUntil = null;
    await periodicConnect(reason, boundDeviceOnly: true, operationGeneration: generation);
  }

  /// Reconnects the exact account/profile-bound necklace and waits until its
  /// capture transport is active. Unlike the background watchdog, this is a
  /// user-initiated operation and also repairs a connected BLE session whose
  /// audio capture failed to start.
  Future<bool> reconnectKnownDeviceForCapture({required String reason}) async {
    if (!_deviceServiceReady) return false;
    final stored = _rememberedDeviceForCurrentAuthority();
    if (stored == null) return false;

    await _captureTeardown;
    if (!_deviceServiceReady || !_isCurrentOwnerBoundDevice(stored.id)) return false;

    final generation = ++_deviceOperationGeneration;
    _diagnosticTraceByOperationGeneration.clear();
    _diagnosticResolutionStartedByOperationGeneration.clear();
    final diagnosticTrace = captureProvider?.beginDeviceDiagnosticTrace(stored);
    if (diagnosticTrace != null) _diagnosticTraceByOperationGeneration[generation] = diagnosticTrace;
    pairedDevice = stored;
    _automaticReconnectCooldownUntil = null;
    _automaticReconnectExhausted = false;
    updateConnectingStatus(true);

    try {
      final activeDevice = connectedDevice;
      if (isConnected && activeDevice != null) {
        if (activeDevice.id != stored.id) return false;
        await _onDeviceConnected(
          activeDevice,
          generation,
          explicitlyAuthorized: true,
          diagnosticTrace: diagnosticTrace,
        );
      } else {
        await scanAndConnectToDevice(operationGeneration: generation, startCaptureWhenConnected: true);
      }
    } catch (error) {
      Logger.debug('User-initiated necklace reconnect failed ($reason): $error');
    } finally {
      if (_isDeviceOperationCurrent(generation)) updateConnectingStatus(false);
    }

    if (!_isDeviceOperationCurrent(generation) || !_isCurrentOwnerBoundDevice(stored.id)) return false;
    final captureReady = isConnected &&
        connectedDevice?.id == stored.id &&
        captureProvider?.recordingState == RecordingState.deviceRecord;
    if (!captureReady && !isConnected) {
      unawaited(periodicConnect('$reason follow-up', boundDeviceOnly: true, operationGeneration: generation));
    }
    return captureReady;
  }

  /// Commits the one explicit Home confirmation for a device saved by builds
  /// that predate owner binding. This is deliberately separate from normal
  /// resume so a new account can never inherit or capture through a legacy
  /// device record without the current person's action.
  Future<bool> confirmLegacyNecklaceForCurrentAuthority({required String reason}) async {
    if (!_deviceServiceReady || isConnected) return false;
    final ownerBinding = _rememberedDeviceOwnerBinding();
    final candidate = legacyUntrustedDeviceCandidate;
    final authorityGeneration = _rememberedDeviceAuthorityGeneration;
    if (ownerBinding == null || candidate == null) return false;
    if (!_isLegacyCandidateCurrent(
      candidate: candidate,
      ownerBinding: ownerBinding,
      authorityGeneration: authorityGeneration,
    )) {
      return false;
    }

    final generation = ++_deviceOperationGeneration;
    await _persistRememberedDevice(candidate, operationGeneration: generation);
    if (!_isDeviceOperationCurrent(generation) || !_isCurrentOwnerBoundDevice(candidate.id)) return false;

    // Only after a durable exact-owner binding may this device enter normal
    // reconnect/capture handling.
    pairedDevice = candidate;
    _requiresExplicitDeviceSelectionAfterAuthorityChange = false;
    _automaticReconnectCooldownUntil = null;
    notifyListeners();
    await periodicConnect(reason, boundDeviceOnly: true, operationGeneration: generation);
    return true;
  }

  Future<void> onDeviceDisconnected({int? operationGeneration, String? deviceId, int? connectionGeneration}) async {
    if (connectionGeneration != null && !_isCurrentDeviceConnectionSession(connectionGeneration)) return;
    final generation = operationGeneration ?? _deviceOperationGeneration;
    if (!_isDeviceOperationCurrent(generation)) return;
    Logger.debug('onDisconnected inside: $connectedDevice');
    final disconnectedDeviceId = deviceId ?? connectedDevice?.id ?? pairedDevice?.id;
    _activeDeviceConnectionSession = null;
    _deviceOperationGeneration++;
    _havingNewFirmware = false;
    connectedDevice = null;
    _clearDeferredDeviceCapture();
    pairedDevice = _rememberedDeviceForCurrentAuthority();
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
      currentFirmware: device.firmwareRevision,
      latestFirmwareDetails: latestFirmwareDetails,
    );
    return (message, hasUpdate, version, latestFirmwareDetails);
  }

  Future<void> _onDeviceConnected(
    BtDevice device,
    int operationGeneration, {
    bool explicitlyAuthorized = false,
    EllaDiagnosticCaptureTrace? diagnosticTrace,
  }) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    if (_rememberedDeviceOwnerBinding() == null) return;
    if (!explicitlyAuthorized &&
        (_authorityReconciliationPending ||
            _requiresExplicitDeviceSelectionAfterAuthorityChange ||
            !_isCurrentOwnerBoundDevice(device.id))) {
      return;
    }
    _requiresExplicitDeviceSelectionAfterAuthorityChange = false;
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
    await _persistRememberedDevice(device, operationGeneration: operationGeneration);
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    final capture = captureProvider;
    capture?.updateRecordingDevice(device);
    final activeDiagnosticTrace = diagnosticTrace ?? _diagnosticTraceByOperationGeneration[operationGeneration];
    if (activeDiagnosticTrace != null) {
      unawaited(
        activeDiagnosticTrace.emit(
          layer: EllaDiagnosticLayer.bleTransport,
          eventName: 'peripheral_connected',
          outcome: EllaDiagnosticOutcome.succeeded,
          retryClass: EllaDiagnosticRetryClass.never,
          expectedNextEvent: 'capture_authority_current',
          deadlineMs: 8000,
          firmware: device.firmwareRevision,
        ),
      );
    }

    try {
      // Capture is the critical post-connect path. Metadata, storage, and
      // battery probes are useful but must never prevent necklace recording.
      if (capture?.phoneCaptureOwnsMobileAudio == true) {
        _deferDeviceCaptureUntilPhoneReleases(device, operationGeneration);
      } else {
        await _startDeviceCaptureWithRetry(device, operationGeneration, diagnosticTrace: activeDiagnosticTrace);
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

  Future<void> _runConnectedSetupStep(String name, int operationGeneration, Future<void> Function() step) async {
    if (!_isDeviceOperationCurrent(operationGeneration)) return;
    try {
      await step();
    } catch (error) {
      Logger.debug('Connected device $name setup failed without suppressing capture: $error');
    }
  }

  Future<bool> _startDeviceCaptureWithRetry(
    BtDevice device,
    int operationGeneration, {
    EllaDiagnosticCaptureTrace? diagnosticTrace,
  }) async {
    final capture = captureProvider;
    if (capture == null) return false;
    var activeDiagnosticTrace = diagnosticTrace ?? _diagnosticTraceByOperationGeneration[operationGeneration];
    for (var attempt = 1; attempt <= _maxDeviceCaptureStartAttempts; attempt++) {
      if (!_isDeviceOperationCurrent(operationGeneration)) return false;
      if (capture.phoneCaptureOwnsMobileAudio) {
        _deferDeviceCaptureUntilPhoneReleases(device, operationGeneration);
        return false;
      }
      if (attempt > 1) {
        activeDiagnosticTrace = activeDiagnosticTrace?.nextAttempt();
        if (activeDiagnosticTrace != null) {
          _diagnosticTraceByOperationGeneration[operationGeneration] = activeDiagnosticTrace;
        }
      }
      try {
        await capture.streamDeviceRecording(device: device, diagnosticTrace: activeDiagnosticTrace);
      } catch (error) {
        Logger.debug('Necklace capture start attempt $attempt failed: $error');
      }
      if (!_isDeviceOperationCurrent(operationGeneration)) return false;
      if (capture.recordingState == RecordingState.deviceRecord) return true;
      if (capture.phoneCaptureOwnsMobileAudio) {
        _deferDeviceCaptureUntilPhoneReleases(device, operationGeneration);
        return false;
      }
      if (attempt < _maxDeviceCaptureStartAttempts) {
        await Future<void>.delayed(_deviceCaptureRetryDelay);
      }
    }
    Logger.debug(
      'Necklace transport is connected but capture did not become ready after '
      '$_maxDeviceCaptureStartAttempts attempts',
    );
    return false;
  }

  Future<void> _handleDeviceConnected(String deviceId, int operationGeneration, int connectionGeneration) async {
    if (!_isDeviceOperationCurrent(operationGeneration) || !_isCurrentDeviceConnectionSession(connectionGeneration)) {
      return;
    }
    if (_authorityReconciliationPending ||
        _requiresExplicitDeviceSelectionAfterAuthorityChange ||
        !_isCurrentOwnerBoundDevice(deviceId)) {
      return;
    }
    final device = await _resolveConnectedDevice(deviceId);
    if (device == null ||
        !_isDeviceOperationCurrent(operationGeneration) ||
        !_isCurrentDeviceConnectionSession(connectionGeneration) ||
        _authorityReconciliationPending ||
        !_isCurrentOwnerBoundDevice(device.id)) {
      return;
    }
    var diagnosticTrace = _diagnosticTraceByOperationGeneration[operationGeneration];
    diagnosticTrace ??= captureProvider?.beginDeviceDiagnosticTrace(device);
    if (diagnosticTrace != null) {
      _diagnosticTraceByOperationGeneration[operationGeneration] = diagnosticTrace;
    }
    await _onDeviceConnected(device, operationGeneration, diagnosticTrace: diagnosticTrace);
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

    while (!_disposed && retryCount < maxRetries) {
      try {
        var (message, hasUpdate, version, firmwareDetails) = await shouldUpdateFirmware();
        if (_disposed) return false;
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
        if (_disposed) return false;
        retryCount++;
        Logger.debug('Error checking firmware update (attempt $retryCount): $e');

        if (retryCount == maxRetries) {
          Logger.debug('Max retries reached, giving up');
          _havingNewFirmware = false;
          if (!_disposed) notifyListeners();
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
                builder: (context) =>
                    OmiGlassOtaUpdate(device: pairedDevice, latestFirmwareDetails: _latestOmiGlassFirmwareDetails),
              ),
            );
          } else {
            Navigator.of(context).push(MaterialPageRoute(builder: (context) => FirmwareUpdate(device: pairedDevice)));
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
  void onDeviceConnectionStateChanged(String deviceId, DeviceConnectionState state, {int? connectionGeneration}) async {
    Logger.debug("provider > device connection state changed...$deviceId...$state...${connectedDevice?.id}");
    switch (state) {
      case DeviceConnectionState.connected:
        _disconnectDebouncer.cancel();
        // Service callbacks carry no Firebase identity. Only accept a callback
        // after the current account explicitly starts a new scan. This fences
        // an account-A BLE event delivered after account B becomes current.
        if (connectionGeneration == null ||
            _authorityReconciliationPending ||
            _requiresExplicitDeviceSelectionAfterAuthorityChange ||
            !_isCurrentOwnerBoundDevice(deviceId)) {
          return;
        }
        final generation = _deviceOperationGeneration;
        if (!_isDeviceOperationCurrent(generation)) return;
        _activeDeviceConnectionSession = connectionGeneration;
        _connectDebouncer.run(() => _handleDeviceConnected(deviceId, generation, connectionGeneration));
        break;
      case DeviceConnectionState.disconnected:
        _connectDebouncer.cancel();
        // Native callbacks from an older connection session must not tear down
        // a current owner's later reconnect to the same physical necklace.
        if (connectionGeneration == null || !_isCurrentDeviceConnectionSession(connectionGeneration)) return;
        // Check if this is the paired device or currently connected device
        // Coz connectedDevice and pairedDevice are the same but connectedDevice becomes null after disconnect
        if (deviceId == connectedDevice?.id || deviceId == pairedDevice?.id) {
          final generation = _deviceOperationGeneration;
          if (!_isDeviceOperationCurrent(generation)) return;
          _disconnectDebouncer.run(
            () => onDeviceDisconnected(
              operationGeneration: generation,
              deviceId: deviceId,
              connectionGeneration: connectionGeneration,
            ),
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
        _activeDeviceConnectionSession = null;
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
        pairedDevice = _rememberedDeviceForCurrentAuthority();
        notifyListeners();
        if (pairedDevice != null && _automaticallyReconnectOnReady) {
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
