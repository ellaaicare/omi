import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';

import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_provider_utilities/flutter_provider_utilities.dart';
import 'package:permission_handler/permission_handler.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/main.dart';
import 'package:omi/providers/base_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/notifications.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/analytics/analytics_manager.dart';
import 'package:omi/utils/audio/foreground.dart';
import 'package:omi/utils/bluetooth/bluetooth_adapter.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_service.dart';

enum DeviceSelectionOutcome { connected, consentRequired, unavailable, cancelled }

class OnboardingProvider extends BaseProvider with MessageNotifierMixin implements IDeviceServiceSubsciption {
  OnboardingProvider({IDeviceService? deviceService}) : _deviceServiceOverride = deviceService {
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(_cancelDiscoveryForAuthorityChange);
  }

  final IDeviceService? _deviceServiceOverride;
  IDeviceService get _deviceService => _deviceServiceOverride ?? ServiceManager.instance().device;

  DeviceProvider? deviceProvider;
  bool isClicked = false;
  bool isConnected = false;
  int batteryPercentage = -1;
  String deviceName = '';
  DeviceType? deviceType;
  String deviceId = '';
  String? connectingToDeviceId;
  List<BtDevice> deviceList = [];
  Timer? _didNotMakeItTimer;
  bool enableInstructions = false;
  Map<String, BtDevice> foundDevicesMap = {};
  Timer? _discoveryTimer;
  Object? _pickerOwner;
  int _scanEpoch = 0;
  bool _disposed = false;
  bool _discoveryActive = false;
  bool discoveryFailed = false;
  String _scanUid = '';
  int _scanAuthorityGeneration = 0;
  bool Function()? _canScan;
  IDeviceService? _scanService;
  IDeviceService? _pickerService;
  _PickerDiscoverySubscription? _scanSubscription;
  DeviceServiceStatus _scanServiceStatus = DeviceServiceStatus.init;
  Future<void>? _initialScan;
  Future<void>? _scanInFlight;
  Future<void>? _scanCleanup;
  Future<void>? _permissionRequest;
  Map<String, BtDevice>? _passDevices;

  //----------------- Onboarding Permissions -----------------
  bool hasBluetoothPermission = false;
  bool hasLocationPermission = false;
  bool hasNotificationPermission = false;
  bool hasBackgroundPermission = false; // Android only
  bool hasMicrophonePermission = false;
  bool hasScreenCapturePermission = false; // macOS/Windows only
  bool hasAccessibilityPermission = false; // macOS only
  bool isLoading = false;

  // Method channel for macOS/Windows permissions
  static const MethodChannel _screenCaptureChannel = MethodChannel('screenCapturePlatform');

  Future updatePermissions() async {
    if (PlatformService.isDesktop) {
      try {
        // Use macOS-specific permission checking
        String bluetoothStatus = await _screenCaptureChannel.invokeMethod('checkBluetoothPermission');
        hasBluetoothPermission = bluetoothStatus == 'granted';

        String locationStatus = await _screenCaptureChannel.invokeMethod('checkLocationPermission');
        hasLocationPermission = locationStatus == 'granted';

        // Use macOS-specific notification permission checking
        String notificationStatus = await _screenCaptureChannel.invokeMethod('checkNotificationPermission');
        hasNotificationPermission = notificationStatus == 'granted' || notificationStatus == 'provisional';

        // Add microphone permission checking
        String microphoneStatus = await _screenCaptureChannel.invokeMethod('checkMicrophonePermission');
        hasMicrophonePermission = microphoneStatus == 'granted';

        // Add screen capture permission checking
        String screenCaptureStatus = await _screenCaptureChannel.invokeMethod('checkScreenCapturePermission');
        hasScreenCapturePermission = screenCaptureStatus == 'granted';

        // Add accessibility permission checking
        String accessibilityStatus = await _screenCaptureChannel.invokeMethod('checkAccessibilityPermission');
        hasAccessibilityPermission = accessibilityStatus == 'granted';

        Logger.debug(
          'Permissions update - Mic: $microphoneStatus, Screen: $screenCaptureStatus, Accessibility: $accessibilityStatus',
        );
      } catch (e) {
        Logger.debug('Error updating permissions on macOS: $e');
        // Fallback to standard permission checking
        hasBluetoothPermission = await Permission.bluetooth.isGranted;
        hasLocationPermission = await Permission.location.isGranted;
        hasNotificationPermission = await Permission.notification.isGranted;
        hasMicrophonePermission = await Permission.microphone.isGranted;
        // Screen capture permission not available through permission_handler on macOS
        hasScreenCapturePermission = false;
        hasAccessibilityPermission = false;
      }
    } else {
      // Mobile platforms (iOS/Android)
      hasBluetoothPermission = await Permission.bluetooth.isGranted;
      hasLocationPermission = await Permission.location.isGranted;
      hasNotificationPermission = await Permission.notification.isGranted;
      hasMicrophonePermission = await Permission.microphone.isGranted;
      // Screen capture and accessibility permissions not relevant on mobile platforms for this use case
      hasScreenCapturePermission = false;
      hasAccessibilityPermission = false;
    }

    SharedPreferencesUtil().notificationsEnabled = hasNotificationPermission;
    SharedPreferencesUtil().locationEnabled = hasLocationPermission;
    notifyListeners();
  }

  void setLoading(bool value) {
    isLoading = value;
    notifyListeners();
  }

  void updateBluetoothPermission(bool value) {
    if (_disposed) return;
    hasBluetoothPermission = value;
    notifyListeners();
  }

  void updateLocationPermission(bool value) {
    hasLocationPermission = value;
    SharedPreferencesUtil().locationEnabled = value;
    AnalyticsManager().setUserAttribute('Location Enabled', SharedPreferencesUtil().locationEnabled);
    notifyListeners();
  }

  void updateNotificationPermission(bool value) {
    hasNotificationPermission = value;
    SharedPreferencesUtil().notificationsEnabled = value;
    AnalyticsManager().setUserAttribute('Notifications Enabled', SharedPreferencesUtil().notificationsEnabled);
    notifyListeners();
  }

  void updateBackgroundPermission(bool value) {
    hasBackgroundPermission = value;
    AnalyticsManager().setUserAttribute('Background Permission Enabled', hasBackgroundPermission);
    notifyListeners();
  }

  void updateMicrophonePermission(bool value) {
    hasMicrophonePermission = value;
    notifyListeners();
  }

  void updateScreenCapturePermission(bool value) {
    hasScreenCapturePermission = value;
    notifyListeners();
  }

  void updateAccessibilityPermission(bool value) {
    hasAccessibilityPermission = value;
    notifyListeners();
  }

  Future askForBluetoothPermissions() async {
    if (!PlatformService.isWindows) {
      FlutterBluePlus.setLogLevel(LogLevel.info, color: true);
    }

    if (PlatformService.isDesktop) {
      try {
        String bluetoothStatus = await _screenCaptureChannel.invokeMethod('checkBluetoothPermission');
        if (bluetoothStatus == 'granted') {
          updateBluetoothPermission(true);
          return;
        }

        if (bluetoothStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestBluetoothPermission');
          updateBluetoothPermission(granted);
          if (!granted) {
            AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.onboardingBluetoothRequired ??
                  'Bluetooth permission is required to connect to your device.',
            );
          }
        } else if (bluetoothStatus == 'denied' || bluetoothStatus == 'restricted') {
          updateBluetoothPermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingBluetoothDeniedSystemPrefs ??
                'Bluetooth permission denied. Please grant permission in System Preferences.',
          );
        } else {
          updateBluetoothPermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingBluetoothStatusCheckPrefs(bluetoothStatus) ??
                'Bluetooth permission status: $bluetoothStatus. Please check System Preferences.',
          );
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Bluetooth permission on macOS: $e');
        AppSnackbar.showSnackbarError(
          MyApp.navigatorKey.currentContext?.l10n.onboardingFailedCheckBluetooth('$e') ??
              'Failed to check Bluetooth permission: $e',
        );
        updateBluetoothPermission(false);
      }
    } else if (Platform.isIOS) {
      PermissionStatus bleStatus = await Permission.bluetooth.request();
      Logger.debug('bleStatus: $bleStatus');
      updateBluetoothPermission(bleStatus.isGranted);
    } else {
      if (Platform.isAndroid) {
        if (!(await BluetoothAdapter.isSupported) ||
            FlutterBluePlus.adapterStateNow != BluetoothAdapterStateHelper.on) {
          try {
            await FlutterBluePlus.turnOn();
          } catch (e) {
            if (e is FlutterBluePlusException) {
              if (e.code == 11) {
                //  onShowDialog();
              }
            }
          }
        }
      }
      PermissionStatus bleScanStatus = await Permission.bluetoothScan.request();
      PermissionStatus bleConnectStatus = await Permission.bluetoothConnect.request();
      // PermissionStatus locationStatus = await Permission.location.request();
      updateBluetoothPermission(bleConnectStatus.isGranted && bleScanStatus.isGranted);
    }
    if (!_disposed) notifyListeners();
  }

  Future askForNotificationPermissions() async {
    if (PlatformService.isDesktop) {
      try {
        String notificationStatus = await _screenCaptureChannel.invokeMethod('checkNotificationPermission');
        Logger.debug('notificationStatus: $notificationStatus');
        if (notificationStatus == 'granted') {
          updateNotificationPermission(true);
          return;
        }

        if (notificationStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestNotificationPermission');
          updateNotificationPermission(granted);
          if (!granted) {
            AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.onboardingNotificationDeniedSystemPrefs ??
                  'Notification permission denied. Please grant permission in System Preferences.',
            );
          }
        } else if (notificationStatus == 'denied') {
          updateNotificationPermission(false);
          // Request permission which will redirect to settings if denied
          await _screenCaptureChannel.invokeMethod('requestNotificationPermission');
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingNotificationDeniedNotifications ??
                'Notification permission denied. Please grant permission in System Preferences > Notifications.',
          );
        } else if (notificationStatus == 'provisional') {
          updateNotificationPermission(true); // Provisional permissions are still functional
          Logger.debug('Notification permission is provisional - notifications will be delivered quietly');
        } else {
          updateNotificationPermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingNotificationStatusCheckPrefs(notificationStatus) ??
                'Notification permission status: $notificationStatus. Please check System Preferences.',
          );
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Notification permission on macOS: $e');
        AppSnackbar.showSnackbarError(
          MyApp.navigatorKey.currentContext?.l10n.onboardingFailedCheckNotification('$e') ??
              'Failed to check Notification permission: $e',
        );
        updateNotificationPermission(false);
      }
    } else {
      // Existing logic for iOS/Android
      var isAllowed = await NotificationService.instance.requestNotificationPermissions();
      updateNotificationPermission(isAllowed);
    }
    notifyListeners();
  }

  Future askForBackgroundPermissions() async {
    await FlutterForegroundTask.requestIgnoreBatteryOptimization();
    var isAllowed = await ForegroundUtil().isIgnoringBatteryOptimizations;
    updateBackgroundPermission(isAllowed);
    notifyListeners();
  }

  Future<(bool, PermissionStatus)> askForLocationPermissions() async {
    if (PlatformService.isDesktop) {
      try {
        String locationStatus = await _screenCaptureChannel.invokeMethod('checkLocationPermission');
        Logger.debug('locationStatus: $locationStatus');
        if (locationStatus == 'granted') {
          updateLocationPermission(true);
          return (true, PermissionStatus.granted);
        }

        if (locationStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestLocationPermission');
          updateLocationPermission(granted);
          Logger.debug('undetermined location permission granted: $granted');
          return (true, granted ? PermissionStatus.granted : PermissionStatus.denied);
        } else if (locationStatus == 'denied' || locationStatus == 'restricted') {
          updateLocationPermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingLocationGrantInSettings ??
                'Please grant location permission in Settings > Privacy & Security > Location Services',
          );
          return (true, PermissionStatus.permanentlyDenied);
        } else {
          updateLocationPermission(false);
          return (true, PermissionStatus.denied);
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Location permission on macOS: $e');
        updateLocationPermission(false);
        return (false, PermissionStatus.denied);
      }
    } else {
      // Existing logic for iOS/Android
      if (await Permission.location.serviceStatus.isDisabled) {
        Logger.debug('Location service is disabled');
        return (false, PermissionStatus.permanentlyDenied);
      } else {
        var res = await Permission.locationWhenInUse.request();
        return (true, res);
      }
    }
  }

  Future<bool> alwaysAllowLocation() async {
    if (PlatformService.isDesktop) {
      try {
        String locationStatus = await _screenCaptureChannel.invokeMethod('checkLocationPermission');
        bool granted = locationStatus == 'granted';
        updateLocationPermission(granted);
        return granted;
      } catch (e) {
        Logger.debug('Error checking location permission on macOS: $e');
        updateLocationPermission(false);
        return false;
      }
    } else {
      PermissionStatus locationStatus = await Permission.locationAlways.request();
      Logger.debug('alwaysAllowLocation permission status: $locationStatus');
      updateLocationPermission(locationStatus.isGranted);
      return locationStatus.isGranted;
    }
  }

  Future askForMicrophonePermissions() async {
    if (PlatformService.isDesktop) {
      try {
        String microphoneStatus = await _screenCaptureChannel.invokeMethod('checkMicrophonePermission');
        Logger.debug('microphoneStatus: $microphoneStatus');
        if (microphoneStatus == 'granted') {
          updateMicrophonePermission(true);
          return true;
        }

        if (microphoneStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestMicrophonePermission');
          updateMicrophonePermission(granted);
          if (!granted) {
            AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.onboardingMicrophoneRequired ??
                  'Microphone permission is required for recording.',
            );
          }
          return granted;
        } else if (microphoneStatus == 'denied' || microphoneStatus == 'restricted') {
          updateMicrophonePermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingMicrophoneDenied ??
                'Microphone permission denied. Please grant permission in System Preferences > Privacy & Security > Microphone.',
          );
          return false;
        } else {
          updateMicrophonePermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingMicrophoneStatusCheckPrefs(microphoneStatus) ??
                'Microphone permission status: $microphoneStatus. Please check System Preferences.',
          );
          return false;
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Microphone permission on macOS: $e');
        AppSnackbar.showSnackbarError(
          MyApp.navigatorKey.currentContext?.l10n.onboardingFailedCheckMicrophone('$e') ??
              'Failed to check Microphone permission: $e',
        );
        updateMicrophonePermission(false);
        return false;
      }
    } else {
      // Existing logic for iOS/Android
      PermissionStatus micStatus = await Permission.microphone.request();
      Logger.debug('micStatus: $micStatus');
      updateMicrophonePermission(micStatus.isGranted);
      return micStatus.isGranted;
    }
  }

  Future askForScreenCapturePermissions() async {
    if (PlatformService.isDesktop) {
      try {
        String screenCaptureStatus = await _screenCaptureChannel.invokeMethod('checkScreenCapturePermission');
        Logger.debug('screenCaptureStatus: $screenCaptureStatus');
        if (screenCaptureStatus == 'granted') {
          updateScreenCapturePermission(true);
          return true;
        }

        if (screenCaptureStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestScreenCapturePermission');
          updateScreenCapturePermission(granted);
          if (!granted) {
            AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.onboardingScreenCaptureRequired ??
                  'Screen capture permission is required for system audio recording.',
            );
          }
          return granted;
        } else if (screenCaptureStatus == 'denied') {
          updateScreenCapturePermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingScreenCaptureDenied ??
                'Screen capture permission denied. Please grant permission in System Preferences > Privacy & Security > Screen Recording.',
          );
          return false;
        } else {
          updateScreenCapturePermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingScreenCaptureStatusCheckPrefs(screenCaptureStatus) ??
                'Screen capture permission status: $screenCaptureStatus. Please check System Preferences.',
          );
          return false;
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Screen Capture permission on macOS: $e');
        AppSnackbar.showSnackbarError(
          MyApp.navigatorKey.currentContext?.l10n.onboardingFailedCheckScreenCapture('$e') ??
              'Failed to check Screen Capture permission: $e',
        );
        updateScreenCapturePermission(false);
        return false;
      }
    } else {
      // Screen capture not relevant on mobile for this use case
      updateScreenCapturePermission(false);
      return false;
    }
  }

  Future askForAccessibilityPermissions() async {
    if (PlatformService.isDesktop) {
      try {
        String accessibilityStatus = await _screenCaptureChannel.invokeMethod('checkAccessibilityPermission');
        Logger.debug('accessibilityStatus: $accessibilityStatus');
        if (accessibilityStatus == 'granted') {
          updateAccessibilityPermission(true);
          return true;
        }

        if (accessibilityStatus == 'undetermined') {
          bool granted = await _screenCaptureChannel.invokeMethod('requestAccessibilityPermission');
          updateAccessibilityPermission(granted);
          if (!granted) {
            AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.onboardingAccessibilityRequired ??
                  'Accessibility permission is required for detecting browser meetings.',
            );
          }
          return granted;
        } else {
          updateAccessibilityPermission(false);
          AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.onboardingAccessibilityStatusCheckPrefs(accessibilityStatus) ??
                'Accessibility permission status: $accessibilityStatus. Please check System Preferences.',
          );
          return false;
        }
      } catch (e) {
        Logger.debug('Error checking/requesting Accessibility permission on macOS: $e');
        AppSnackbar.showSnackbarError(
          MyApp.navigatorKey.currentContext?.l10n.onboardingFailedCheckAccessibility('$e') ??
              'Failed to check Accessibility permission: $e',
        );
        updateAccessibilityPermission(false);
        return false;
      }
    } else {
      // Accessibility not relevant on mobile for this use case
      updateAccessibilityPermission(false);
      return false;
    }
  }
  //----------------- Onboarding Permissions -----------------

  void setDeviceProvider(DeviceProvider provider) {
    if (identical(deviceProvider, provider)) return;
    deviceProvider?.removeListener(_handleDeviceProviderChanged);
    deviceProvider = provider;
    provider.addListener(_handleDeviceProviderChanged);
    _reconcileDevicePresentation(notify: false);
  }

  void _handleDeviceProviderChanged() => _reconcileDevicePresentation(notify: true);

  void _reconcileDevicePresentation({required bool notify}) {
    final provider = deviceProvider;
    final connectedDevice = provider?.presentationConnectedDevice;
    final connected = provider?.presentationIsConnected == true && connectedDevice != null;
    final presentedDevice = connected ? connectedDevice : null;
    final nextDeviceId = presentedDevice?.id ?? '';
    final nextDeviceName = presentedDevice?.name ?? '';
    final nextDeviceType = presentedDevice?.type;
    final nextBatteryPercentage = connected ? provider!.presentationBatteryLevel : -1;
    final changed = isConnected != connected ||
        deviceId != nextDeviceId ||
        deviceName != nextDeviceName ||
        deviceType != nextDeviceType ||
        batteryPercentage != nextBatteryPercentage;

    isConnected = connected;
    deviceId = nextDeviceId;
    deviceName = nextDeviceName;
    deviceType = nextDeviceType;
    batteryPercentage = nextBatteryPercentage;
    if (changed && notify) notifyListeners();
  }

  // Method to handle taps on devices
  Future<DeviceSelectionOutcome> handleTap(
      {required BtDevice device, required bool isFromOnboarding, VoidCallback? goNext}) async {
    if (_disposed || isClicked) return DeviceSelectionOutcome.cancelled;
    final uid = SharedPreferencesUtil().uid;
    final authorityGeneration = SharedPreferencesUtil().aiConsentAuthorityGeneration;
    final pause = pauseDeviceDiscovery();
    final selectionEpoch = _scanEpoch;
    bool isCurrentSelection() =>
        !_disposed &&
        selectionEpoch == _scanEpoch &&
        uid == SharedPreferencesUtil().uid &&
        authorityGeneration == SharedPreferencesUtil().aiConsentAuthorityGeneration;
    try {
      isClicked = true;
      connectingToDeviceId = device.id;
      notifyListeners();
      await pause;
      if (!isCurrentSelection()) return DeviceSelectionOutcome.cancelled;
      final connected = await deviceProvider!.connectDeviceForCurrentUser(device);
      if (!isCurrentSelection()) return DeviceSelectionOutcome.cancelled;
      if (!connected) {
        if (deviceProvider!.lastConnectionConsentRequired) {
          isClicked = false;
          connectingToDeviceId = null;
          notifyListeners();
          return DeviceSelectionOutcome.consentRequired;
        }
        throw StateError('Connected device was unavailable after pairing');
      }
      Logger.debug('Connected to device: ${device.name}');
      deviceId = device.id;
      deviceName = device.name;
      deviceType = device.type;
      var connectedDevice = deviceProvider!.connectedDevice;
      batteryPercentage = deviceProvider!.batteryLevel;
      isConnected = connectedDevice?.id == device.id && deviceProvider!.presentationIsConnected;
      isClicked = false;
      connectingToDeviceId = null; // Reset the connecting device
      notifyListeners();
      await Future.delayed(const Duration(seconds: 2));
      if (!isCurrentSelection()) return DeviceSelectionOutcome.cancelled;
      if (!isConnected || connectedDevice == null) throw StateError('Connected device was unavailable after pairing');
      SharedPreferencesUtil().deviceName = connectedDevice.name;
      foundDevicesMap.clear();
      deviceList.clear();
      if (isFromOnboarding) {
        goNext!();
      } else {
        notifyInfo('DEVICE_CONNECTED');
      }
      return DeviceSelectionOutcome.connected;
    } catch (e) {
      if (!isCurrentSelection()) return DeviceSelectionOutcome.cancelled;
      Logger.debug('Error connecting to device: $e');
      isClicked = false; // Allow clicks again after finishing the operation
      connectingToDeviceId = null; // Reset the connecting device
      final activeDevice = deviceProvider!.presentationConnectedDevice;
      if (activeDevice != null && deviceProvider!.presentationIsConnected) {
        isConnected = true;
        deviceId = activeDevice.id;
        deviceName = activeDevice.name;
        deviceType = activeDevice.type;
        batteryPercentage = deviceProvider!.batteryLevel;
      } else {
        isConnected = false;
      }
      notifyListeners();
      return DeviceSelectionOutcome.unavailable;
    }
  }

  void deviceAlreadyUnpaired() {
    batteryPercentage = -1;
    isConnected = false;
    deviceName = '';
    deviceType = null;
    deviceId = '';
    notifyListeners();
  }

  Future<void> scanDevices({required VoidCallback onShowDialog, Object? owner, bool Function()? canScan}) {
    if (_disposed) return Future.value();
    final pickerOwner = owner ?? this;
    if (_discoveryActive && identical(_pickerOwner, pickerOwner)) {
      return _scanService == null ? (_initialScan ?? Future.value()) : _runDiscoveryPass(_scanEpoch);
    }
    final cleanup = cancelDeviceDiscovery();
    _pickerOwner = pickerOwner;
    _discoveryActive = true;
    _scanUid = SharedPreferencesUtil().uid;
    _scanAuthorityGeneration = SharedPreferencesUtil().aiConsentAuthorityGeneration;
    _canScan = canScan;
    final epoch = _scanEpoch;
    foundDevicesMap.clear();
    deviceList = [];
    enableInstructions = false;
    discoveryFailed = false;
    notifyListeners();
    return _initialScan = _startPickerDiscovery(epoch, cleanup, onShowDialog);
  }

  bool _isScanCurrent(int epoch) =>
      !_disposed &&
      _discoveryActive &&
      epoch == _scanEpoch &&
      _scanUid == SharedPreferencesUtil().uid &&
      _scanAuthorityGeneration == SharedPreferencesUtil().aiConsentAuthorityGeneration &&
      (_canScan?.call() ?? true);

  Future<void> _startPickerDiscovery(int epoch, Future<void> cleanup, VoidCallback onShowDialog) async {
    try {
      await cleanup;
      await _scanInFlight;
      if (!_isScanCurrent(epoch)) return;
      await deviceProvider?.prepareForExplicitDeviceSelection();
      if (!_isScanCurrent(epoch)) return;
      if (SharedPreferencesUtil().btDevice.id.isEmpty) deviceAlreadyUnpaired();
      if (!hasBluetoothPermission) {
        final request = _permissionRequest ??= askForBluetoothPermissions().then<void>((_) {});
        try {
          await request;
        } finally {
          if (identical(_permissionRequest, request)) _permissionRequest = null;
        }
        if (!_isScanCurrent(epoch)) return;
        if (!hasBluetoothPermission) {
          unawaited(pauseDeviceDiscovery());
          discoveryFailed = true;
          enableInstructions = true;
          notifyListeners();
          onShowDialog();
          return;
        }
      }
      if (!_isScanCurrent(epoch)) return;
      _didNotMakeItTimer = Timer(const Duration(seconds: 10), () {
        if (!_isScanCurrent(epoch)) return;
        enableInstructions = true;
        notifyListeners();
      });
      final service = _pickerService = _scanService = _deviceService;
      _scanServiceStatus = DeviceServiceStatus.ready;
      final subscription = _scanSubscription = _PickerDiscoverySubscription(this, epoch);
      service.subscribe(subscription, subscription);
      if (!_isScanCurrent(epoch)) return;
      // Upstream's onboarding cadence, without its saved-device auto-connect.
      _discoveryTimer = Timer.periodic(const Duration(seconds: 10), (_) {
        if (!_isScanCurrent(epoch)) {
          unawaited(cancelDeviceDiscovery());
          return;
        }
        unawaited(_runDiscoveryPass(epoch));
      });
      await _runDiscoveryPass(epoch);
    } catch (error) {
      if (!_isScanCurrent(epoch)) return;
      unawaited(pauseDeviceDiscovery());
      discoveryFailed = true;
      enableInstructions = true;
      Logger.debug('Picker preparation failed: ${error.runtimeType}');
      notifyListeners();
    }
  }

  Future<void> _runDiscoveryPass(int epoch) async {
    if (!_isScanCurrent(epoch) || _scanServiceStatus != DeviceServiceStatus.ready) return;
    if (_scanInFlight != null) return _scanInFlight;
    discoveryFailed = false;
    final passDevices = _passDevices = <String, BtDevice>{};
    final service = _scanService!;
    final completion = Completer<void>();
    _scanInFlight = completion.future;
    try {
      await service.discover();
      if (!_isScanCurrent(epoch)) return;
      _publishPickerDevices(passDevices);
    } catch (error) {
      if (_isScanCurrent(epoch)) {
        Logger.debug('Picker discovery failed: ${error.runtimeType}');
        unawaited(pauseDeviceDiscovery());
        discoveryFailed = true;
        enableInstructions = true;
        notifyListeners();
      }
    } finally {
      if (identical(_passDevices, passDevices)) _passDevices = null;
      if (identical(_scanInFlight, completion.future)) _scanInFlight = null;
      completion.complete();
    }
  }

  Future<void> pauseDeviceDiscovery() => _stopPickerDiscovery(releaseOwner: false);

  bool isDiscoveringFor(Object owner) => _discoveryActive && identical(owner, _pickerOwner);

  Object? discoveryLeaseFor(Object owner) => isDiscoveringFor(owner) ? _scanSubscription : null;

  Future<void> cancelDeviceDiscovery({Object? owner}) {
    if (owner != null && !identical(owner, _pickerOwner)) return Future.value();
    return _stopPickerDiscovery(releaseOwner: true);
  }

  Future<void> _stopPickerDiscovery({required bool releaseOwner}) {
    _scanEpoch++;
    _discoveryActive = false;
    _discoveryTimer?.cancel();
    _discoveryTimer = null;
    _didNotMakeItTimer?.cancel();
    _didNotMakeItTimer = null;
    final service = _scanService ?? _pickerService;
    final subscription = _scanSubscription;
    _scanService = null;
    _scanSubscription = null;
    _passDevices = null;
    _initialScan = null;
    _canScan = null;
    if (subscription != null) service?.unsubscribe(subscription);
    if (releaseOwner) {
      _pickerOwner = null;
      _pickerService = null;
      isClicked = false;
      connectingToDeviceId = null;
    }
    if (service == null) return _scanCleanup ?? Future.value();
    return _scanCleanup = service.cancelPendingConnection().catchError((Object error) {
      Logger.debug('Picker cancellation failed: ${error.runtimeType}');
    });
  }

  void _cancelDiscoveryForAuthorityChange() {
    if (_disposed || _pickerOwner == null) return;
    unawaited(cancelDeviceDiscovery());
    foundDevicesMap.clear();
    deviceList = [];
    discoveryFailed = true;
    enableInstructions = true;
    notifyListeners();
  }

  void _publishPickerDevices(Map<String, BtDevice> devices) {
    foundDevicesMap = Map.of(devices);
    deviceList = devices.values.toList();
    if (deviceList.isNotEmpty) _didNotMakeItTimer?.cancel();
    notifyListeners();
  }

  @override
  void dispose() {
    unawaited(cancelDeviceDiscovery());
    _disposed = true;
    SharedPreferencesUtil.aiConsentAuthorityChanges.removeListener(_cancelDiscoveryForAuthorityChange);
    deviceProvider?.removeListener(_handleDeviceProviderChanged);
    super.dispose();
  }

  @override
  void onDeviceConnectionStateChanged(String deviceId, DeviceConnectionState state, {int? connectionGeneration}) {
    // TODO: implement onDeviceConnectionStateChanged
  }

  @override
  void onDevices(List<BtDevice> devices) {
    if (!_isScanCurrent(_scanEpoch)) return;
    for (final device in devices) {
      if (device.id.isNotEmpty) _passDevices?[device.id] = device;
    }
  }

  @override
  void onStatusChanged(DeviceServiceStatus status) {
    _scanServiceStatus = status;
    if (status == DeviceServiceStatus.stop) _cancelDiscoveryForAuthorityChange();
  }
}

class _PickerDiscoverySubscription implements IDeviceServiceSubsciption {
  const _PickerDiscoverySubscription(this.provider, this.epoch);

  final OnboardingProvider provider;
  final int epoch;

  @override
  void onDevices(List<BtDevice> devices) {
    if (provider._isScanCurrent(epoch)) provider.onDevices(devices);
  }

  @override
  void onStatusChanged(DeviceServiceStatus status) {
    if (provider._isScanCurrent(epoch)) provider.onStatusChanged(status);
  }

  @override
  void onDeviceConnectionStateChanged(String deviceId, DeviceConnectionState state, {int? connectionGeneration}) {}
}
