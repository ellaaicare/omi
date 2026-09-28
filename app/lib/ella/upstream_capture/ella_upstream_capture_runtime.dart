import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:permission_handler/permission_handler.dart';

import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_gated_capture_seams.dart';
import 'package:omi/ella/upstream_capture/ella_gated_device_connection.dart';
import 'package:omi/env/env.dart' as ella_env;
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/upstream_capture/backend/http/api/conversations.dart' as upstream_api;
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/env/env.dart' as upstream_env;
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/capture/capture_composition.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/services/capture/capture_session_owner.dart';
import 'package:omi/upstream_capture/services/capture/conversation_location_capture.dart';
import 'package:omi/upstream_capture/services/capture/local_segment_store.dart';
import 'package:omi/upstream_capture/services/capture/recording_lifecycle_telemetry.dart';
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/upstream_capture/services/wals.dart';
import 'package:omi/utils/audio/foreground.dart';
import 'package:omi/utils/logger.dart';

/// Upstream [upstream_env.EnvFields] backed by the fork's already-initialized
/// Ella [ella_env.Env], so the vendored stack talks to the Ella backend with the
/// Ella flavor's configuration (no second source of endpoints or keys).
class EllaUpstreamEnvFields implements upstream_env.EnvFields {
  const EllaUpstreamEnvFields();

  @override
  String? get apiBaseUrl => ella_env.Env.apiBaseUrl;

  @override
  String? get posthogApiKey => null;

  @override
  String? get intercomAppId => null;

  @override
  String? get intercomIOSApiKey => null;

  @override
  String? get intercomAndroidApiKey => null;

  @override
  String? get googleClientId => null;

  @override
  String? get googleClientSecret => null;

  @override
  bool? get useWebAuth => null;

  @override
  bool? get useAuthCustomToken => null;
}

/// Secure storage handed to the vendored upstream SharedPreferencesUtil.
///
/// Upstream's preferences migrate `authToken` out of SharedPreferences into the
/// keychain and delete the prefs copy. The fork's legacy auth still reads that
/// prefs key, so the upstream copy must never migrate it: reads find nothing
/// and writes fail, which upstream treats as "migration not persisted — keep
/// the prefs copy and retry later". Upstream then serves the fork's token from
/// its in-memory cache without mutating any legacy state.
class EllaIsolatedSecureStorage extends FlutterSecureStorage {
  const EllaIsolatedSecureStorage();

  @override
  Future<String?> read({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async =>
      null;

  @override
  Future<void> write({
    required String key,
    required String? value,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {
    throw StateError('upstream secure storage is isolated in the Ella fork');
  }

  @override
  Future<void> delete({
    required String key,
    AppleOptions? iOptions,
    AndroidOptions? aOptions,
    LinuxOptions? lOptions,
    WebOptions? webOptions,
    AppleOptions? mOptions,
    WindowsOptions? wOptions,
  }) async {}
}

/// Collaborators of [EllaUpstreamCaptureRuntime.compose]; production values
/// come from the vendored upstream singletons, tests inject scripted ones.
class EllaUpstreamCaptureWiring {
  const EllaUpstreamCaptureWiring({
    required this.wal,
    required this.phoneMic,
    required this.openConversationSocket,
    required this.ensureDeviceConnection,
    required this.owner,
    this.batchSupported,
    this.auth,
    this.connectivity,
    this.now,
    this.scheduling,
    this.preferences,
    this.ble,
    this.location,
    this.localSegments,
    this.codec,
    this.microphonePermission,
    this.refreshConversation,
    this.telemetry,
  });

  final IWalService wal;
  final IMicRecorderService phoneMic;
  final CaptureConversationSocketOpen openConversationSocket;
  final Future<DeviceConnection?> Function(String deviceId) ensureDeviceConnection;
  final CaptureSessionOwner owner;
  final bool? batchSupported;
  final CaptureAuthBoundary? auth;
  final CaptureConnectivityBoundary? connectivity;
  final DateTime Function()? now;
  final CaptureScheduling? scheduling;
  final upstream.SharedPreferencesUtil? preferences;
  final CaptureBleListeners? ble;
  final ConversationLocationCapture? location;
  final LocalSegmentStore? localSegments;
  final Future<BleAudioCodec> Function(String deviceId)? codec;
  final Future<bool> Function()? microphonePermission;
  final Future<void> Function(CaptureProvider provider)? refreshConversation;
  final RecordingLifecycleTelemetry? telemetry;
}

/// Composition root of the flag-ON graph: boots the vendored upstream capture
/// stack exactly as upstream's own `main.dart` does (Env, SharedPreferences,
/// ServiceManager, BleFlutterApi -> BleBridge) and builds upstream's
/// CaptureProvider through upstream's own `composeCaptureProvider` with the
/// Ella-gated seams. It owns no capture state machine: every capture decision
/// (sources, sockets, WAL, sessions) stays in upstream's CaptureController /
/// CaptureCoordinator.
class EllaUpstreamCaptureRuntime {
  EllaUpstreamCaptureRuntime({required this.authority});

  static EllaUpstreamCaptureRuntime? _instance;

  static EllaUpstreamCaptureRuntime get instance =>
      _instance ??= EllaUpstreamCaptureRuntime(authority: EllaCaptureAuthority());

  static bool _upstreamEnvInitialized = false;
  static bool _upstreamServicesInitialized = false;

  final EllaCaptureAuthority authority;
  CaptureProvider? _provider;
  Future<CaptureProvider>? _boot;
  StreamSubscription<EllaCaptureRevocation>? _revocationSubscription;
  final ValueNotifier<EllaCaptureRevocation?> lastRevocation = ValueNotifier<EllaCaptureRevocation?>(null);

  CaptureProvider? get provider => _provider;

  /// Boots the vendored stack once; later calls join the same future.
  Future<CaptureProvider> ensureBooted() => _boot ??= _bootProduction();

  Future<CaptureProvider> _bootProduction() async {
    if (!_upstreamEnvInitialized) {
      upstream_env.Env.init(const EllaUpstreamEnvFields());
      _upstreamEnvInitialized = true;
    }
    await upstream.SharedPreferencesUtil.init(
      secureStorage: const EllaIsolatedSecureStorage(),
      mirrorNativeAuthToken: false,
    );
    if (!_upstreamServicesInitialized) {
      await ServiceManager.init();
      _upstreamServicesInitialized = true;
    }
    // Upstream main.dart registers the native BLE bridge here.
    BleFlutterApi.setUp(BleBridge.instance);
    await ServiceManager.instance().start();

    final services = ServiceManager.instance();
    final wal = services.wal;
    _configureTransferCoordinator(wal);
    final provider = compose(
      EllaUpstreamCaptureWiring(
        wal: wal,
        phoneMic: services.phoneMic,
        openConversationSocket: ({
          required BleAudioCodec codec,
          required int sampleRate,
          required String language,
          required bool force,
          String? source,
          String? clientConversationId,
          customSttConfig,
          geolocation,
        }) =>
            services.socket.conversation(
          codec: codec,
          sampleRate: sampleRate,
          language: language,
          force: force,
          source: source,
          clientConversationId: clientConversationId,
          customSttConfig: customSttConfig,
          geolocation: geolocation,
        ),
        ensureDeviceConnection: (deviceId) => services.device.ensureConnection(deviceId),
        owner: CaptureSessionOwner(
          coordinator: RecordingTransferCoordinator.instance,
          startForeground: () async {
            if (!Platform.isAndroid) return;
            await ForegroundUtil.initializeForegroundService();
            await ForegroundUtil.startForegroundTask();
          },
          stopForeground: ForegroundUtil.stopForegroundTask,
        ),
        localSegments: LocalSegmentStore.appSupport(),
      ),
    );
    return provider;
  }

  /// Builds upstream's CaptureProvider through upstream's
  /// [composeCaptureProvider] with every audio seam decorated by the Ella gate.
  /// Production and tests use this same path.
  CaptureProvider compose(EllaUpstreamCaptureWiring wiring) {
    final gatedDeviceConnection = ellaGatedDeviceConnectionLoader(wiring.ensureDeviceConnection, authority);
    final gatedSocket = ellaGatedConversationSocketOpen(wiring.openConversationSocket, authority);
    late final CaptureProvider provider;
    provider = composeCaptureProvider(
      CaptureDependencies(
        wal: wiring.wal,
        phoneMic: EllaGatedMicRecorderService(wiring.phoneMic, authority),
        batchSupported: wiring.batchSupported ?? (Platform.isIOS || Platform.isAndroid),
        auth: wiring.auth ?? CaptureAuthBoundary.production,
        connectivity: wiring.connectivity ?? CaptureConnectivityBoundary.production(),
        now: wiring.now ?? DateTime.now,
        scheduling: wiring.scheduling ?? const WallClockCaptureScheduling(),
        preferences: wiring.preferences ?? upstream.SharedPreferencesUtil(),
        ble: wiring.ble ?? const BleBridgeCaptureListeners(),
        openSocket: ({
          required BleAudioCodec codec,
          required int sampleRate,
          required String language,
          required bool force,
          String? source,
          String? clientConversationId,
          customSttConfig,
        }) =>
            gatedSocket(
          codec: codec,
          sampleRate: sampleRate,
          language: language,
          force: force,
          source: source,
          clientConversationId: clientConversationId,
          customSttConfig: customSttConfig,
        ),
        openConversationSocket: gatedSocket,
        owner: wiring.owner,
        location: wiring.location ?? ConversationLocationCapture(),
        localSegments: wiring.localSegments ?? LocalSegmentStore.disabled(),
        codec: wiring.codec ??
            (deviceId) async {
              final connection = await gatedDeviceConnection(deviceId);
              if (connection == null) return BleAudioCodec.pcm8;
              return connection.getAudioCodec();
            },
        microphonePermission:
            wiring.microphonePermission ?? () async => (await Permission.microphone.request()).isGranted,
        refreshConversation: () async {
          final refresh = wiring.refreshConversation;
          if (refresh != null) return refresh(provider);
          // Same as upstream CaptureController's default in-progress loader.
          final conversations =
              await upstream_api.getConversations(statuses: [ConversationStatus.in_progress], limit: 1);
          provider.applyInProgressConversation(conversations.isNotEmpty ? conversations.first : null);
        },
        telemetry: wiring.telemetry ?? RecordingLifecycleTelemetry(),
        ensureDeviceConnection: gatedDeviceConnection,
      ),
    );
    _provider = provider;
    unawaited(_revocationSubscription?.cancel());
    _revocationSubscription = authority.revocations.listen(_onRevoked);
    return provider;
  }

  /// Binds the capture session to [uid] (fresh consent lease + generation) and
  /// releases upstream's capture-policy latch if a previous revocation set it.
  Future<bool> bindAccount(String uid) async {
    final bound = authority.bind(uid);
    if (!bound) return false;
    try {
      final preferences = upstream.SharedPreferencesUtil();
      if (preferences.capturePolicy.muted && _mutedByRevocation) {
        await preferences.setCaptureMuted(false);
      }
      _mutedByRevocation = false;
    } catch (error) {
      Logger.debug('[EllaUpstreamCapture] capture policy unmute failed: ${error.runtimeType}');
    }
    return authority.hasCurrentAuthority;
  }

  bool _mutedByRevocation = false;
  Future<void> _teardown = Future<void>.value();

  /// Completes when the teardown ordered by the latest revocation finished.
  Future<void> get pendingTeardown => _teardown;

  void _onRevoked(EllaCaptureRevocation revocation) {
    lastRevocation.value = revocation;
    // Frames are already refused synchronously by the gate; this only stops
    // upstream capture through its public API, off the frame callback.
    _teardown = _teardown.then((_) => _stopUpstreamCapture(revocation));
    unawaited(_teardown);
  }

  Future<void> _stopUpstreamCapture(EllaCaptureRevocation revocation) async {
    final provider = _provider;
    // Native latch: upstream's native batch writers and every Dart admission
    // check (`_admitsCapture`) honour the capture policy, so muting it stops
    // audio that never crosses into Dart (Transcribe Later files).
    try {
      _mutedByRevocation = true;
      await upstream.SharedPreferencesUtil().setCaptureMuted(true);
    } catch (error) {
      Logger.debug('[EllaUpstreamCapture] capture policy mute failed: ${error.runtimeType}');
    }
    if (provider == null) return;
    try {
      await provider.stopStreamRecording(reason: 'ella_capture_authority_revoked');
    } catch (error) {
      Logger.debug('[EllaUpstreamCapture] phone stop after revocation: ${error.runtimeType}');
    }
    try {
      await provider.stopStreamDeviceRecording();
    } catch (error) {
      Logger.debug('[EllaUpstreamCapture] device stop after revocation: ${error.runtimeType}');
    }
    if (revocation.reason == EllaCaptureRevocationReason.accountSwitch ||
        revocation.reason == EllaCaptureRevocationReason.released) {
      // Upstream's own account-retirement fence for live segments and WALs.
      provider.clearUserData();
    }
  }

  void _configureTransferCoordinator(IWalService wal) {
    final phone = wal.getSyncs().phone as LocalWalSync;
    RecordingTransferCoordinator.instance.configure(
      reconcile: () async => (wal.getSyncs() as WalSyncs).phone.reconcileUploadedWals(),
      discover: () async {},
      refreshPending: () async {},
      drain: () async {
        final response = await phone.syncAll();
        final failed = (response?.localUploadFailures ?? 0) + (response?.localUploadPermanentFailures ?? 0) > 0;
        final needsReconciliation = (await phone.getAllWals()).any((w) => w.status == WalStatus.uploaded);
        return RecordingTransferDrainResult(
          attempted: response != null,
          failed: failed,
          needsReconciliation: needsReconciliation,
        );
      },
      // Uploading recorded audio is an emission too: only while the bound
      // account still holds current consent authority.
      autoUploadEnabled: () => authority.hasCurrentAuthority,
      connectivityChanges: ConnectivityService().onConnectionChange,
      initiallyConnected: ConnectivityService().isConnected,
    );
  }

  // ---- Thin UI entry points: each one is a direct call into upstream's public
  // capture/device API, gated only by [bindAccount]. No capture state lives here.

  /// Phone mic: binds the signed-in account, then upstream `streamRecording()`.
  Future<bool> startPhoneCapture(String uid) async {
    final provider = await ensureBooted();
    if (!await bindAccount(uid)) return false;
    await provider.streamRecording();
    return true;
  }

  Future<void> stopPhoneCapture() async {
    final provider = _provider;
    if (provider == null) return;
    await provider.stopStreamRecording();
  }

  /// Upstream's single "finish the live capture" action (processes the conversation).
  Future<void> finishConversation() async {
    final provider = _provider;
    if (provider == null) return;
    await provider.finishCapture();
  }

  /// Manual necklace discovery through upstream's DeviceService.
  Future<List<BtDevice>> discoverNecklaces({int timeoutSeconds = 5}) async {
    await ensureBooted();
    final devices = ServiceManager.instance().device;
    await devices.discover(timeout: timeoutSeconds);
    return List<BtDevice>.of(devices.devices);
  }

  /// Manual (user-initiated) necklace connect, mirroring what upstream's
  /// DeviceProvider does on connect: force-connect through DeviceService,
  /// remember the device, hand it to capture, and start device recording.
  /// There is intentionally no Ella auto-connect / reconnect loop.
  Future<bool> connectNecklace(String uid, BtDevice device) async {
    final provider = await ensureBooted();
    if (!await bindAccount(uid)) return false;
    final connection = await ServiceManager.instance().device.ensureConnection(device.id, force: true);
    if (connection == null) return false;
    upstream.SharedPreferencesUtil().btDevice = device;
    provider.updateRecordingDevice(device);
    await provider.streamDeviceRecording(device: device);
    return true;
  }

  Future<void> disconnectNecklace() async {
    final provider = _provider;
    if (provider == null) return;
    final deviceId = upstream.SharedPreferencesUtil().btDevice.id;
    await provider.stopStreamDeviceRecording(cleanDevice: true);
    if (deviceId.isNotEmpty) await ServiceManager.instance().device.disconnectDevice(deviceId);
  }

  /// Revokes the bound session (sign-out / account transition from the legacy host).
  Future<void> releaseAccount() async {
    authority.release();
    await _teardown;
  }

  @visibleForTesting
  static void resetForTesting() {
    _instance = null;
  }
}
