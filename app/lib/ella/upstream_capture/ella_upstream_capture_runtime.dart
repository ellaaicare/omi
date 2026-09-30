import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:permission_handler/permission_handler.dart';

import 'package:omi/backend/http/api/conversations.dart' as ella_api;
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_finalization.dart';
import 'package:omi/ella/upstream_capture/ella_gated_capture_seams.dart';
import 'package:omi/ella/upstream_capture/ella_gated_device_connection.dart';
import 'package:omi/env/env.dart' as ella_env;
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/upstream_capture/backend/http/api/conversations.dart' as upstream_api;
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart';
import 'package:omi/upstream_capture/env/env.dart' as upstream_env;
import 'package:omi/upstream_capture/gen/pigeon_communicator.g.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/bridges/ble_bridge.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/services/capture/capture_session_owner.dart';
import 'package:omi/upstream_capture/services/capture/conversation_location_capture.dart';
import 'package:omi/upstream_capture/services/capture/local_segment_store.dart';
import 'package:omi/upstream_capture/services/capture/recording_lifecycle_telemetry.dart';
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';
import 'package:omi/upstream_capture/services/wals.dart';
import 'package:omi/utils/audio/foreground.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

enum EllaCaptureStartOutcome {
  started,
  consentRequired,
  unavailable,
}

/// Recovers the one upstream partial-init state: ServiceManager installs its
/// singleton before awaiting ConnectivityService.init().
class EllaUpstreamServicesBootstrap {
  EllaUpstreamServicesBootstrap({
    Future<void> Function()? initializeManager,
    bool Function()? managerExists,
    Future<void> Function()? initializeConnectivity,
  })  : _initializeManager = initializeManager ?? ServiceManager.init,
        _managerExists = managerExists ?? _productionManagerExists,
        _initializeConnectivity = initializeConnectivity ?? ConnectivityService().init;

  final Future<void> Function() _initializeManager;
  final bool Function() _managerExists;
  final Future<void> Function() _initializeConnectivity;
  bool _initialized = false;

  static bool _productionManagerExists() {
    try {
      ServiceManager.instance();
      return true;
    } catch (_) {
      return false;
    }
  }

  Future<void> ensureInitialized() async {
    if (_initialized) return;
    try {
      await _initializeManager();
    } catch (_) {
      if (!_managerExists()) rethrow;
      await _initializeConnectivity();
    }
    _initialized = true;
  }
}

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
    this.processInProgressConversation,
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

  /// Null in production (upstream's REST call). Deterministic tests inject it.
  final Future<CreateConversationResponse?> Function()? processInProgressConversation;
}

/// Converts the native Pigeon discovery-diagnostics snapshot into the
/// flag-OFF-safe DTO consumed by `DeviceDiagnosticsPage`. A function of
/// [BleHostApi] (not a bound method) so tests can exercise the exact same
/// mapping with a fake host, and production can register it with
/// `EllaCaptureHost.installNativeDiscoveryDiagnosticsLoader`.
Future<EllaNativeDiscoveryDiagnostics> loadNativeDiscoveryDiagnostics([BleHostApi? hostApi]) async {
  final native = await (hostApi ?? BleHostApi()).getNativeDiscoveryDiagnostics();
  return EllaNativeDiscoveryDiagnostics(
    lastStartScanCbState: native.lastStartScanCbState,
    scansStartedImmediately: native.scansStartedImmediately,
    scansQueued: native.scansQueued,
    queuedScansFired: native.queuedScansFired,
    didDiscoverCount: native.didDiscoverCount,
    flutterApiNilDropCount: native.flutterApiNilDropCount,
    nameArrivedLate: native.nameArrivedLate,
    retrievedConnectedCount: native.retrievedConnectedCount,
    retrievedKnownCount: native.retrievedKnownCount,
    restoredCount: native.restoredCount,
  );
}

/// Composition root of the flag-ON graph: boots the vendored upstream capture
/// stack exactly as upstream's own `main.dart` does (Env, SharedPreferences,
/// ServiceManager, BleFlutterApi -> BleBridge) and builds upstream's
/// CaptureProvider through upstream's own `composeCaptureProvider` with the
/// Ella-gated seams. It owns no capture state machine: every capture decision
/// (sources, sockets, WAL, sessions) stays in upstream's CaptureController /
/// CaptureCoordinator.
class EllaUpstreamCaptureRuntime {
  EllaUpstreamCaptureRuntime({
    required this.authority,
    EllaCaptureProtocolSocket? Function()? activeProtocolSocket,
    EllaCaptureFinalizationRequest? finalizationRequest,
    @visibleForTesting Future<CaptureProvider> Function()? bootForTesting,
    @visibleForTesting Future<DeviceConnection?> Function(String deviceId)? connectDeviceForTesting,
  })  : _activeProtocolSocket = activeProtocolSocket,
        _finalizationRequest = finalizationRequest,
        _bootForTesting = bootForTesting,
        _connectDeviceForTesting = connectDeviceForTesting;

  static EllaUpstreamCaptureRuntime? _instance;

  static EllaUpstreamCaptureRuntime get instance =>
      _instance ??= EllaUpstreamCaptureRuntime(authority: EllaCaptureAuthority());

  static bool _upstreamEnvInitialized = false;
  static final EllaUpstreamServicesBootstrap _servicesBootstrap = EllaUpstreamServicesBootstrap();

  final EllaCaptureAuthority authority;
  final EllaCaptureProtocolSocket? Function()? _activeProtocolSocket;
  final EllaCaptureFinalizationRequest? _finalizationRequest;
  final Future<CaptureProvider> Function()? _bootForTesting;
  final Future<DeviceConnection?> Function(String deviceId)? _connectDeviceForTesting;
  CaptureProvider? _provider;
  EllaCaptureProtocolSocket? _protocolSocket;
  EllaCaptureProtocolSocket? _finalizationSocket;
  int _socketOpenGeneration = 0;
  Future<CaptureProvider>? _boot;
  StreamSubscription<EllaCaptureRevocation>? _revocationSubscription;
  final ValueNotifier<EllaCaptureRevocation?> lastRevocation = ValueNotifier<EllaCaptureRevocation?>(null);
  final ValueNotifier<bool> protocolUnavailable = ValueNotifier<bool>(false);

  CaptureProvider? get provider => _provider;
  String? get boundOwnerId => authority.boundUid;

  /// Boots the vendored stack once; later calls join the same future.
  Future<CaptureProvider> ensureBooted() {
    final existing = _boot;
    if (existing != null) return existing;
    final attempt = _bootForTesting?.call() ?? _bootProduction();
    _boot = attempt;
    attempt.then((_) {}, onError: (Object _, StackTrace __) {
      if (identical(_boot, attempt)) _boot = null;
    });
    return attempt;
  }

  Future<CaptureProvider> _bootProduction() async {
    if (!_upstreamEnvInitialized) {
      upstream_env.Env.init(const EllaUpstreamEnvFields());
      _upstreamEnvInitialized = true;
    }
    await upstream.SharedPreferencesUtil.init(
      secureStorage: const EllaIsolatedSecureStorage(),
      mirrorNativeAuthToken: false,
    );
    await _servicesBootstrap.ensureInitialized();
    // Upstream main.dart registers the native BLE bridge here.
    BleFlutterApi.setUp(BleBridge.instance);
    DebugLogManager.recordBleFlutterApiSetUp();
    await ServiceManager.instance().start();

    final services = ServiceManager.instance();
    final wal = services.wal;
    configureTransferCoordinator(
      RecordingTransferCoordinator.instance,
      wal,
      connectivityChanges: ConnectivityService().onConnectionChange,
      initiallyConnected: ConnectivityService().isConnected,
    );
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
        }) async {
          if (_finalizationSocket != null) return null;
          final openGeneration = ++_socketOpenGeneration;
          final previous = _protocolSocket;
          if (previous != null) {
            if (previous.state == SocketServiceState.connected &&
                !force &&
                previous.codec == codec &&
                previous.sampleRate == sampleRate &&
                previous.clientConversationId == clientConversationId) {
              return previous;
            }
            await previous.stop(reason: 'capture socket replaced');
            if (openGeneration != _socketOpenGeneration) return null;
          }
          final originUid = authority.boundUid;
          final originEpoch = authority.bindingEpoch;
          protocolUnavailable.value = false;
          late final EllaCaptureProtocolSocket socket;
          bool isCurrent() =>
              originUid != null &&
              authority.boundUid == originUid &&
              authority.bindingEpoch == originEpoch &&
              authority.hasCurrentAuthority &&
              identical(_protocolSocket, socket);
          socket = createEllaCaptureProtocolSocket(
            codec: codec,
            sampleRate: sampleRate,
            language: language,
            source: source,
            clientConversationId: clientConversationId,
            customSttConfig: customSttConfig,
            geolocation: geolocation,
            hasOriginAuthority: isCurrent,
            onAdmissionFailure: (reason, closeCode) => _onCaptureProtocolFailure(socket, reason, closeCode),
          );
          _protocolSocket = socket;
          await socket.start();
          return openGeneration == _socketOpenGeneration && socket.state == SocketServiceState.connected
              ? socket
              : null;
        },
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
        processInProgressConversation: _processCaptureProtocolConversation,
      ),
    );
    return provider;
  }

  void _onCaptureProtocolFailure(EllaCaptureProtocolSocket socket, String reason, int? closeCode) {
    if (!identical(_protocolSocket, socket) || !socket.hasOriginAuthority) return;
    final provider = _provider;
    if (provider == null) return;
    protocolUnavailable.value = true;
    // A policy rejection must not churn the upstream retry loop. Network and
    // server failures remain under upstream's normal recovery ownership.
    if (!captureProtocolRejectionIsPermanent(reason, closeCode)) return;
    unawaited(() async {
      try {
        if (!identical(_protocolSocket, socket) || !socket.hasOriginAuthority) return;
        if (socket.source == ConversationSource.phone.name) {
          await provider.stopStreamRecording(
            reason: 'capture_protocol_unavailable',
            resumeHandedOffPendant: false,
          );
        } else {
          await provider.stopStreamDeviceRecording();
        }
      } catch (error) {
        Logger.debug('[EllaUpstreamCapture] protocol failure teardown: ${error.runtimeType}');
      }
    }());
  }

  Future<CreateConversationResponse?> _processCaptureProtocolConversation() async {
    final socket = _finalizationSocket ?? _activeProtocolSocket?.call() ?? _protocolSocket;
    if (socket == null) return null;
    _finalizationSocket = socket;
    try {
      final exactAuthority = _CaptureProtocolAccountAuthority(socket, authority);
      return await finalizeEllaCaptureProtocolConversation(
        socket: socket,
        exactAuthority: exactAuthority,
        request: _finalizationRequest ?? ella_api.processInProgressConversation,
      );
    } finally {
      if (identical(_finalizationSocket, socket)) _finalizationSocket = null;
    }
  }

  /// Builds upstream's CaptureProvider through its own constructor seams with
  /// every audio seam decorated by the Ella gate. Production and tests use this
  /// same path.
  ///
  /// This forwards exactly the seams upstream's `composeCaptureProvider(
  /// CaptureDependencies)` forwards (that helper is a pure forwarder into this
  /// same constructor), plus `processInProgressConversation`, which
  /// CaptureDependencies does not expose; production uses the exact protocol
  /// finalizer, while deterministic legacy tests can inject their own callback.
  CaptureProvider compose(EllaUpstreamCaptureWiring wiring) {
    final gatedDeviceConnection = ellaGatedDeviceConnectionLoader(wiring.ensureDeviceConnection, authority);
    final gatedSocket = ellaGatedConversationSocketOpen(wiring.openConversationSocket, authority);
    late final CaptureProvider provider;
    provider = CaptureProvider(
      walService: wiring.wal,
      phoneMicRecorder: EllaGatedMicRecorderService(wiring.phoneMic, authority),
      phoneMicBatchSupported: wiring.batchSupported ?? (Platform.isIOS || Platform.isAndroid),
      authBoundary: wiring.auth ?? CaptureAuthBoundary.production,
      connectivity: wiring.connectivity ?? CaptureConnectivityBoundary.production(),
      now: wiring.now ?? DateTime.now,
      scheduling: wiring.scheduling ?? const WallClockCaptureScheduling(),
      preferences: wiring.preferences ?? upstream.SharedPreferencesUtil(),
      bleListeners: wiring.ble ?? const BleBridgeCaptureListeners(),
      openSocket: gatedSocket,
      sessionOwner: wiring.owner,
      conversationLocationCapture: wiring.location ?? ConversationLocationCapture(),
      inProgressConversationLoader: () async {
        final refresh = wiring.refreshConversation;
        if (refresh != null) return refresh(provider);
        // Same as upstream CaptureController's default in-progress loader.
        final conversations = await upstream_api.getConversations(statuses: [ConversationStatus.in_progress], limit: 1);
        provider.applyInProgressConversation(conversations.isNotEmpty ? conversations.first : null);
      },
      audioCodecLoader: wiring.codec ??
          (deviceId) async {
            final connection = await gatedDeviceConnection(deviceId);
            if (connection == null) return BleAudioCodec.pcm8;
            return connection.getAudioCodec();
          },
      microphonePermissionRequester:
          wiring.microphonePermission ?? () async => (await Permission.microphone.request()).isGranted,
      recordingTelemetry: wiring.telemetry ?? RecordingLifecycleTelemetry(),
      localSegmentStore: wiring.localSegments ?? LocalSegmentStore.disabled(),
      deviceConnectionLoader: gatedDeviceConnection,
      processInProgressConversation: wiring.processInProgressConversation ?? _processCaptureProtocolConversation,
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

  /// Configures upstream's recording-transfer owner for the phone WAL the way
  /// upstream's SyncProvider does for local WALs (reconcile, drain via
  /// `syncAll`), with auto-upload admitted only while the bound account still
  /// holds current consent authority: uploading recorded audio is an emission.
  /// Production passes `RecordingTransferCoordinator.instance`; tests pass an
  /// isolated coordinator with a virtual clock.
  void configureTransferCoordinator(
    RecordingTransferCoordinator coordinator,
    IWalService wal, {
    required Stream<bool> connectivityChanges,
    required bool initiallyConnected,
  }) {
    final syncs = wal.getSyncs() as WalSyncs;
    final phone = syncs.phone;
    coordinator.configure(
      reconcile: () async => phone.reconcileUploadedWals(),
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
      autoUploadEnabled: () => authority.hasCurrentAuthority,
      connectivityChanges: connectivityChanges,
      initiallyConnected: initiallyConnected,
    );
  }

  // ---- Thin UI entry points: each one is a direct call into upstream's public
  // capture/device API, gated only by [bindAccount]. No capture state lives here.

  /// Phone mic: binds the signed-in account, then upstream `streamRecording()`.
  Future<EllaCaptureStartOutcome> startPhoneCapture(String uid) async {
    final originEpoch = authority.bindingEpoch;
    final provider = await ensureBooted();
    if (!authority.isCurrentOwner(uid) || authority.bindingEpoch != originEpoch) {
      return EllaCaptureStartOutcome.unavailable;
    }
    if (!await bindAccount(uid)) {
      return authority.isCurrentOwner(uid)
          ? EllaCaptureStartOutcome.consentRequired
          : EllaCaptureStartOutcome.unavailable;
    }
    final boundEpoch = authority.bindingEpoch;
    bool originIsCurrent() =>
        authority.bindingEpoch == boundEpoch &&
        authority.isCurrentOwner(uid) &&
        authority.boundUid == uid &&
        authority.hasCurrentAuthority;
    if (!originIsCurrent()) {
      return EllaCaptureStartOutcome.unavailable;
    }
    await provider.streamRecording();
    return originIsCurrent() ? EllaCaptureStartOutcome.started : EllaCaptureStartOutcome.unavailable;
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
    final finishing = _protocolSocket;
    _finalizationSocket = finishing;
    try {
      await provider.finishCapture();
    } finally {
      if (identical(_finalizationSocket, finishing)) _finalizationSocket = null;
    }
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
  Future<EllaCaptureStartOutcome> connectNecklace(String uid, BtDevice device) async {
    final originEpoch = authority.bindingEpoch;
    final provider = await ensureBooted();
    if (!authority.isCurrentOwner(uid) || authority.bindingEpoch != originEpoch) {
      return EllaCaptureStartOutcome.unavailable;
    }
    if (!await bindAccount(uid)) {
      return authority.isCurrentOwner(uid)
          ? EllaCaptureStartOutcome.consentRequired
          : EllaCaptureStartOutcome.unavailable;
    }
    final boundEpoch = authority.bindingEpoch;
    bool originIsCurrent() =>
        authority.bindingEpoch == boundEpoch &&
        authority.isCurrentOwner(uid) &&
        authority.boundUid == uid &&
        authority.hasCurrentAuthority;
    if (!originIsCurrent()) {
      return EllaCaptureStartOutcome.unavailable;
    }
    final connection = await (_connectDeviceForTesting?.call(device.id) ??
        ServiceManager.instance().device.ensureConnection(device.id, force: true));
    if (connection == null) return EllaCaptureStartOutcome.unavailable;
    if (!originIsCurrent()) {
      return EllaCaptureStartOutcome.unavailable;
    }
    upstream.SharedPreferencesUtil().btDevice = device;
    provider.updateRecordingDevice(device);
    await provider.streamDeviceRecording(device: device);
    return originIsCurrent() ? EllaCaptureStartOutcome.started : EllaCaptureStartOutcome.unavailable;
  }

  Future<void> disconnectNecklace({String? deviceId}) async {
    final provider = _provider;
    if (provider == null) return;
    final targetDeviceId = deviceId ?? upstream.SharedPreferencesUtil().btDevice.id;
    await provider.stopStreamDeviceRecording(cleanDevice: true);
    if (targetDeviceId.isNotEmpty) await ServiceManager.instance().device.disconnectDevice(targetDeviceId);
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

bool captureProtocolRejectionIsPermanent(String reason, int? closeCode) =>
    closeCode == 1008 || reason == 'invalid_capture_protocol_ready';

class _CaptureProtocolAccountAuthority implements ExactAccountAuthorityVerifier {
  _CaptureProtocolAccountAuthority(this._socket, this._capture)
      : uid = _capture.boundUid ?? '',
        _bindingEpoch = _capture.bindingEpoch;

  @override
  final String uid;
  final int _bindingEpoch;
  final EllaCaptureProtocolSocket _socket;
  final EllaCaptureAuthority _capture;

  @override
  bool isExactCurrent() =>
      uid.isNotEmpty &&
      _socket.hasOriginAuthority &&
      _capture.boundUid == uid &&
      _capture.bindingEpoch == _bindingEpoch &&
      _capture.hasCurrentAuthority;
}
