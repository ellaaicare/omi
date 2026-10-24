import 'package:omi/ella/upstream_capture/ella_native_capture_authorization.dart';
import 'package:flutter/widgets.dart';
import 'package:omi/upstream_capture/services/devices/transports/native_ble_transport.dart';
import 'package:omi/ella/capture_host/ella_capture_protocol_trace.dart';
import 'dart:async';
import 'dart:io';
import 'package:omi/services/capture/capture_operation_cancellation.dart';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:permission_handler/permission_handler.dart';

import 'package:omi/backend/http/api/conversations.dart' as ella_api;
import 'package:omi/backend/preferences.dart' as ella_preferences;
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_capture_memory_bridge.dart';
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
import 'package:omi/upstream_capture/services/devices.dart' as necklace_devices;
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';
import 'package:omi/upstream_capture/services/wals.dart';
import 'package:omi/utils/audio/foreground.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

enum EllaCaptureStartOutcome { started, consentRequired, unavailable }

/// Content-free projection of the exact returned finalization, never a save retry.
enum EllaCaptureFinishOutcome { discarded, failed, saved, processing }

String ellaCaptureFinishMessage(EllaCaptureFinishOutcome? outcome) => switch (outcome) {
  EllaCaptureFinishOutcome.discarded => 'Not saved as a memory: this recording was discarded. Your transcript was not submitted again.',
  EllaCaptureFinishOutcome.failed => 'Memory processing failed. Check the recording in Journal.',
  EllaCaptureFinishOutcome.saved => 'Memory created. Open Journal to read it.',
  EllaCaptureFinishOutcome.processing => 'Memory is processing. Check Journal for the result.',
  null => 'Memory requested. Check Journal for the result.',
};

/// Content-free identity for a current capture's presentation and Finish action.
/// Socket readiness is deliberately absent: retained words survive recovery.
class EllaCaptureSessionView {
  const EllaCaptureSessionView({
    required this.uid,
    required this.bindingEpoch,
    required this.recordingId,
    required this.windowId,
    required this.source,
    required this.canFinish,
  });

  final String uid;
  final int bindingEpoch;
  final String recordingId;
  final int windowId;
  final String source;
  final bool canFinish;

  bool matches(EllaCaptureSessionView? other) =>
      other != null &&
      uid == other.uid &&
      bindingEpoch == other.bindingEpoch &&
      recordingId == other.recordingId &&
      windowId == other.windowId &&
      source == other.source;
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
class EllaUpstreamCaptureRuntime with WidgetsBindingObserver implements necklace_devices.IDeviceServiceSubsciption {
  late final memoryBridge = EllaCaptureMemoryBridge(
    captureAuthority: authority,
    loader: _memoryLoader,
    accountAuthority: _memoryAccountAuthority,
    retryDelays: _memoryRetryDelays,
  );

  EllaUpstreamCaptureRuntime({
    required this.authority,
    EllaNativeCaptureAuthorization? nativeAuthorization,
    EllaCaptureProtocolSocket? Function()? activeProtocolSocket,
    EllaCaptureFinalizationRequest? finalizationRequest,
    EllaCaptureMemoryLoader? memoryLoader,
    AccountCommitAuthority? Function()? memoryAccountAuthority,
    List<Duration> memoryRetryDelays = const [
      Duration(seconds: 2),
      Duration(seconds: 4),
      Duration(seconds: 8),
      Duration(seconds: 16),
      Duration(seconds: 32),
      Duration(seconds: 64),
    ],
    @visibleForTesting Future<CaptureProvider> Function()? bootForTesting,
    @visibleForTesting Future<DeviceConnection?> Function(String deviceId)? connectDeviceForTesting,
    @visibleForTesting Future<void> Function(String deviceId)? disconnectDeviceForTesting,
    @visibleForTesting Future<DeviceConnection?> Function(String deviceId, {required bool force})? ensureDeviceConnectionForTesting,
    @visibleForTesting Listenable? deliveryChanges,
    @visibleForTesting WhisperDeliveryScope? Function()? deliveryScope,
  })  : _nativeAuthorization = nativeAuthorization ?? EllaNativeCaptureAuthorization(),
        _memoryLoader = memoryLoader,
        _memoryAccountAuthority = memoryAccountAuthority,
        _memoryRetryDelays = memoryRetryDelays,
        _activeProtocolSocket = activeProtocolSocket,
        _finalizationRequest = finalizationRequest,
        _bootForTesting = bootForTesting,
        _connectDeviceForTesting = connectDeviceForTesting,
        _ensureDeviceConnectionForTesting = ensureDeviceConnectionForTesting,
        _disconnectDeviceForTesting = disconnectDeviceForTesting,
        _deliveryChangesForTesting = deliveryChanges,
        _deliveryScopeForTesting = deliveryScope;

  final EllaNativeCaptureAuthorization _nativeAuthorization;
  final Listenable? _deliveryChangesForTesting;
  final WhisperDeliveryScope? Function()? _deliveryScopeForTesting;
  Listenable? _deliveryChanges;
  Map<String, String> _observedDeliveryQuery = const {};
  int _deliveryRevision = 0;
  bool _deliveryRefreshRequested = false;
  Future<void>? _deliveryRefresh;
  bool _disposed = false;
  bool get isDisposed => _disposed;

  /// Only the ready, exact-account delivery scope may enter a capture URL.
  Map<String, String> get captureDeliveryQuery {
    if (_disposed || !authority.hasCurrentAuthority) return const {};
    final scope = _deliveryScopeForTesting != null
        ? _deliveryScopeForTesting!()
        : whisperDeliveryProtocol1
            ? WhisperDeliveryRuntime.shared.client.scope
            : null;
    return scope?.uid == authority.boundUid ? scope!.captureQuery : const {};
  }

  @visibleForTesting
  Future<void> get pendingDeliveryRefresh async {
    while (true) {
      final pending = _deliveryRefresh;
      if (pending == null) return;
      await pending;
    }
  }

  void _observeDelivery() {
    if (_disposed) return;
    _deliveryChanges?.removeListener(_onDeliveryChanged);
    _deliveryChanges = _deliveryChangesForTesting ?? (whisperDeliveryProtocol1 ? WhisperDeliveryRuntime.shared : null);
    _observedDeliveryQuery = Map.unmodifiable(captureDeliveryQuery);
    _deliveryChanges?.addListener(_onDeliveryChanged);
  }

  void _onDeliveryChanged() {
    if (_disposed) return;
    final query = captureDeliveryQuery;
    if (mapEquals(query, _observedDeliveryQuery)) return;
    _observedDeliveryQuery = Map.unmodifiable(query);
    _deliveryRevision++;
    _protocolTrace.record(_protocolTrace.activeAttempt, CaptureTraceEvent.deliveryChanged,
        ownerCurrent: authority.hasCurrentAuthority,
        deliveryCurrent: false,
        paused: _provider?.isLiveCapturePaused,
        deliveryOptIn: query.isNotEmpty);
    // Revoke stale transport traffic before the serialized upstream action can
    // yield. Drain remains available under the unchanged capture authority.
    _provider?.noteTranscriptionTransportRetiring();
    (_activeProtocolSocket?.call() ?? _protocolSocket)?.retireDeliveryBinding();
    _deliveryRefreshRequested = true;
    _scheduleDeliveryRefresh();
  }

  void _onCaptureDeliveryChanged() {
    _onDeliveryChanged();
    _scheduleDeliveryRefresh();
  _scheduleDefaultNecklaceRecording();
  }

  void _scheduleDeliveryRefresh() {
    if (_disposed ||
        !_deliveryRefreshRequested ||
        _deliveryRefresh != null ||
        finishPending.value ||
        _finalizationSocket != null) {
      return;
    }
    final capture = _provider;
    final session = captureSession;
    if (capture == null || session == null || !session.canFinish || capture.isPhoneMicBatchRecording) return;
    final socket = _activeProtocolSocket?.call() ?? _protocolSocket;
    if (socket?.state == SocketServiceState.connected && mapEquals(socket!.deliveryQuery, captureDeliveryQuery)) {
      _deliveryRefreshRequested = false;
      return;
    }
    final completion = Completer<void>();
    _deliveryRefresh = completion.future;
    bool current() => !_disposed && identical(capture, _provider) && isCurrentCaptureSession(session);
    () async {
      try {
        // Coalesce changes made in one turn; never open a replacement alongside
        // the predecessor. The upstream action drains/closes it before opening.
        await Future<void>.delayed(Duration.zero);
        while (_deliveryRefreshRequested && current() && !finishPending.value && _finalizationSocket == null) {
          final revision = _deliveryRevision;
          _deliveryRefreshRequested = false;
          _protocolTrace.record(_protocolTrace.activeAttempt, CaptureTraceEvent.rebindBegin,
              ownerCurrent: current(), paused: capture.isLiveCapturePaused);
          await capture.onTranscriptionSettingsChanged();
          if (current())
            _protocolTrace.record(_protocolTrace.activeAttempt, CaptureTraceEvent.rebindEnd,
                ownerCurrent: true, paused: capture.isLiveCapturePaused);
          if (!current()) break;
          final replacement = _activeProtocolSocket?.call() ?? _protocolSocket;
          if (replacement?.state == SocketServiceState.connected &&
              mapEquals(replacement!.deliveryQuery, captureDeliveryQuery)) {
            _deliveryRefreshRequested = false;
          } else if (revision != _deliveryRevision) {
            _deliveryRefreshRequested = true;
          }
        }
      } catch (error) {
        if (current()) {
          _protocolTrace.record(_protocolTrace.activeAttempt, CaptureTraceEvent.rebindFailure, ownerCurrent: true);
          protocolUnavailable.value = true;
        }
        // Normal upstream recovery owns retries. Do not start another recorder
        // or spin on failed/offline admission.
        Logger.debug('[EllaUpstreamCapture] delivery rebind unavailable: ${error.runtimeType}');
      } finally {
        _deliveryRefresh = null;
        completion.complete();
      }
      if (current() && _deliveryRefreshRequested) _scheduleDeliveryRefresh();
    }();
  }

  /// Runtime lifetime cleanup; account release itself remains independently
  /// reusable for a later exact-account binding.
  void dispose() {
    if (_disposed) return;
    // Terminal runtime disposal denies native sinks without issuing a new
    // upstream capture command into an already-held transport teardown.
    final nativeDenial = _nativeAuthorization.deny();
    _teardown = Future.wait<void>([_teardown, nativeDenial]).then<void>((_) {});
    unawaited(_teardown.catchError((Object error) {
      Logger.debug('[EllaUpstreamCapture] disposed native admission unconfirmed: ${error.runtimeType}');
    }));
    _disposed = true;
    _defaultDeviceService?.unsubscribe(this);
    _defaultDeviceService = null;
    finishPending.removeListener(_scheduleDefaultNecklaceRecording);
    if (_healthObserving) WidgetsBinding.instance.removeObserver(this);
    _healthTransport = null;
    _healthBindingEpoch = null;
    _socketOpenGeneration++;
    _deliveryRevision++;
    _deliveryRefreshRequested = false;
    final socket = _activeProtocolSocket?.call() ?? _protocolSocket;
    socket?.retireCaptureOrigin();
    unawaited(socket?.stop(reason: 'capture runtime disposed').catchError((Object error) {
      Logger.debug('[EllaUpstreamCapture] disposed socket teardown: ${error.runtimeType}');
    }));
    _deliveryChanges?.removeListener(_onDeliveryChanged);
    _provider?.removeListener(_onCaptureDeliveryChanged);
    finishPending.removeListener(_scheduleDeliveryRefresh);
    unawaited(_revocationSubscription?.cancel());
  }

  static EllaUpstreamCaptureRuntime? _instance;

  static EllaUpstreamCaptureRuntime get instance =>
      _instance ??= EllaUpstreamCaptureRuntime(authority: EllaCaptureAuthority());

  static bool _upstreamEnvInitialized = false;
  static final EllaUpstreamServicesBootstrap _servicesBootstrap = EllaUpstreamServicesBootstrap();

  NativeBleTransport? _healthTransport;
  int? _healthBindingEpoch;
  String _healthLifecycle = 'unknown';
  bool _healthObserving = false;
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _healthLifecycle = state.name;
    if (state == AppLifecycleState.resumed) {
      _requestDefaultNecklaceRecording();
    }
  }

  bool _defaultRecordingRequested = true;
  int _defaultRecordingIntent = 0;

  void _requestDefaultNecklaceRecording() {
    _defaultRecordingRequested = true;
    _scheduleDefaultNecklaceRecording();
  }

  void _retireDefaultNecklaceRecording() {
    _defaultRecordingIntent++;
    _defaultRecordingRequested = false;
  }
  Future<void>? _defaultRecordingFuture;
  necklace_devices.DeviceService? _defaultDeviceService;
  Future<EllaCaptureStartOutcome>? _necklaceConnectFuture;
  DeviceConnection? _defaultRecordingConnection;
  String? _connectingNecklaceUid, _connectingNecklaceId;
  int? _connectingNecklaceEpoch, _connectingNecklaceIntent;

  BtDevice? _rememberedNecklace(String uid) {
    // Reuse the existing app's remembered-device owner fence. An unbound or
    // foreign legacy btDevice is never imported into automatic capture.
    try {
      final preferences = ella_preferences.SharedPreferencesUtil();
      if (preferences.uid.trim() != uid ||
          preferences.btDeviceOwnerBinding.trim() != uid)
        return null;
      final selected = preferences.btDevice;
      if (selected.id.isEmpty) return null;
      return BtDevice.fromJson(selected.toJson());
    } catch (_) {
      return null;
    }
  }

  void _listenForDefaultNecklaceRecording(
    necklace_devices.DeviceService service,
  ) {
    if (identical(service, _defaultDeviceService)) return;
    _defaultDeviceService?.unsubscribe(this);
    _defaultDeviceService = service;
    service.subscribe(this, this);
  }

  @override
  void onDevices(List<BtDevice> devices) {}
  @override
  void onStatusChanged(necklace_devices.DeviceServiceStatus status) {}
  @override
  void onDeviceConnectionStateChanged(
    String deviceId,
    necklace_devices.DeviceConnectionState state,
  ) {
    final uid = authority.boundUid;
    if (state != necklace_devices.DeviceConnectionState.connected ||
        uid == null ||
        !authority.hasCurrentAuthority)
      return;
    final device = _rememberedNecklace(uid);
    if (device?.id != deviceId) return;
    // An in-flight authorized Connect already consumes this transition. It
    // must not leave a second wake that later undoes an explicit Stop.
    if (_disconnectFuture != null ||
        (_necklaceConnectFuture != null &&
            _connectingNecklaceUid == uid &&
            _connectingNecklaceId == deviceId)) return;
    _requestDefaultNecklaceRecording();
  }

  // A single lifecycle wake uses the existing upstream connection/coordinator.
  // It never scans, selects an unknown device, changes consent, or loops on failure.
  void _scheduleDefaultNecklaceRecording() {
    if (_disposed ||
        !_defaultRecordingRequested ||
        _defaultRecordingFuture != null ||
        _necklaceConnectFuture != null ||
        finishPending.value ||
        !authority.hasCurrentAuthority)
      return;
    final uid = authority.boundUid;
    final capture = provider;
    if (uid == null || capture == null || !authority.isCurrentOwner(uid))
      return;
    final remembered = _rememberedNecklace(uid);
    if (remembered == null) {
      _defaultRecordingRequested = false;
      return;
    }
    final epoch = authority.bindingEpoch;
    final intent = _defaultRecordingIntent;
    bool current() =>
        !_disposed &&
        _defaultRecordingIntent == intent &&
        _rememberedNecklace(uid)?.id == remembered.id &&
        _rememberedNecklace(uid)?.type == remembered.type &&
        identical(capture, provider) &&
        authority.hasCurrentAuthority &&
        authority.isCurrentOwner(uid) &&
        authority.boundUid == uid &&
        authority.bindingEpoch == epoch;
    _defaultRecordingRequested = false;
    final done = Completer<void>();
    _defaultRecordingFuture = done.future;
    () async {
      try {
        if (!current()) return;
        await connectNecklace(uid, remembered,
          explicitSelection: false, lifecycleIsCurrent: current);
      } catch (error) {
        Logger.debug(
          '[EllaUpstreamCapture] default necklace start unavailable: ${error.runtimeType}',
        );
      } finally {
        if (identical(_defaultRecordingFuture, done.future))
          _defaultRecordingFuture = null;
        done.complete();
        if (_defaultRecordingRequested) _scheduleDefaultNecklaceRecording();
      }
    }();
  }

  @visibleForTesting
  Future<void> get pendingDefaultRecordingForTesting =>
      _defaultRecordingFuture ?? Future<void>.value();

  @visibleForTesting
  Future<void> requestDefaultNecklaceRecordingForTesting() async {
    _requestDefaultNecklaceRecording();
    await _defaultRecordingFuture;
  }

  final EllaCaptureProtocolTrace _protocolTrace = EllaCaptureProtocolTrace();
  final EllaCaptureAuthority authority;
  EllaCaptureDiagnosticsSnapshot get captureDiagnostics {
    final provider = _provider;
    if (provider == null) return const EllaCaptureDiagnosticsSnapshot.uninitialized();
    final socket = _activeProtocolSocket?.call() ?? _protocolSocket;
    final owner = _diagnosticOwner;
    final ownedCurrent = owner != null &&
        identical(owner.socket, socket) &&
        owner.openGeneration == _socketOpenGeneration &&
        owner.uid == authority.boundUid &&
        owner.bindingEpoch == authority.bindingEpoch &&
        owner.profile == _currentDiagnosticProfile();
    if (owner != null && !ownedCurrent) _diagnosticOwner = null;
    final current = authority.hasCurrentAuthority &&
        (socket == null || socket.hasOriginAuthority) &&
        (owner == null ? !_diagnosticTracked && _activeProtocolSocket != null : ownedCurrent);
    return EllaCaptureDiagnosticsSnapshot.upstream(
      ready: current && socket?.state == SocketServiceState.connected,
      receivedBytes: current ? provider.lifetimeBleBytesReceived : null,
      sentBytes: current ? provider.lifetimeWsSocketBytesSent : null,
      lastFailure: current ? socket?.lastAdmissionFailure : null,
      lastAttempt: current ? socket?.lastAttempt : null,
      protocolTrace: current ? _protocolTrace.snapshot : const [],
      audioHealth: current &&
              provider.liveCaptureSource != 'phone' &&
              _healthBindingEpoch == authority.bindingEpoch &&
              _healthTransport?.deviceId == provider.recordingDevice?.id
          ? Map.unmodifiable({
              ...?_healthTransport?.audioHealth,
              'app_lifecycle': _healthLifecycle,
              'capture_policy': upstream.SharedPreferencesUtil().capturePolicyDiagnostic,
              'capture_expected': provider.liveCaptureSource != null && !provider.isLiveCapturePaused,
              'socket_ready': socket?.state == SocketServiceState.connected,
              'socket_submitted_bytes': provider.lifetimeWsSocketBytesSent,
              'durable_audio_queue': 'not_observed',
              'server_audio_ack': 'not_observed',
            })
          : null,
    );
  }

  ({String uid, String profile, int generation, int presentationRevision})? _currentDiagnosticProfile() {
    try {
      final preferences = ella_preferences.SharedPreferencesUtil();
      final uid = authority.boundUid;
      final profile = preferences.aiConsentProfileBindingId;
      if (uid == null || uid.isEmpty || preferences.uid != uid || profile.isEmpty) return null;
      return (
        uid: uid,
        profile: profile,
        generation: preferences.aiConsentAuthorityGeneration,
        presentationRevision: preferences.aiConsentPresentationRevision,
      );
    } catch (_) {
      return null;
    }
  }

  void _trackDiagnosticSocket(EllaCaptureProtocolSocket socket, int openGeneration) {
    _diagnosticTracked = true;
    final profile = _currentDiagnosticProfile();
    _diagnosticOwner = profile == null
        ? null
        : (
            socket: socket,
            openGeneration: openGeneration,
            uid: profile.uid,
            bindingEpoch: authority.bindingEpoch,
            profile: profile,
          );
  }

  ({EllaCaptureProtocolSocket? reused, int openGeneration}) _beginSocketOpen(
    EllaCaptureProtocolSocket? previous, {
    required bool force,
    required BleAudioCodec codec,
    required int sampleRate,
    required String? clientConversationId,
  }) {
    final openGeneration = ++_socketOpenGeneration;
    if (_disposed) return (reused: null, openGeneration: openGeneration);
    if (previous != null &&
        previous.hasOriginAuthority &&
        previous.state == SocketServiceState.connected &&
        !force &&
        previous.codec == codec &&
        previous.sampleRate == sampleRate &&
        mapEquals(previous.deliveryQuery, captureDeliveryQuery) &&
        previous.clientConversationId == clientConversationId) {
      final owner = _diagnosticOwner;
      if (owner != null &&
          identical(owner.socket, previous) &&
          owner.openGeneration == openGeneration - 1 &&
          owner.uid == authority.boundUid &&
          owner.bindingEpoch == authority.bindingEpoch &&
          owner.profile == _currentDiagnosticProfile() &&
          authority.hasCurrentAuthority &&
          previous.hasOriginAuthority) {
        _diagnosticOwner = (
          socket: previous,
          openGeneration: openGeneration,
          uid: owner.uid,
          bindingEpoch: owner.bindingEpoch,
          profile: owner.profile,
        );
      }
      return (reused: previous, openGeneration: openGeneration);
    }
    return (reused: null, openGeneration: openGeneration);
  }

  @visibleForTesting
  EllaCaptureProtocolSocket? reuseProtocolSocketForTesting(
    EllaCaptureProtocolSocket? previous, {
    required bool force,
    required BleAudioCodec codec,
    required int sampleRate,
    required String? clientConversationId,
  }) =>
      _beginSocketOpen(
        previous,
        force: force,
        codec: codec,
        sampleRate: sampleRate,
        clientConversationId: clientConversationId,
      ).reused;

  @visibleForTesting
  int get socketOpenGenerationForTesting => _socketOpenGeneration;

  @visibleForTesting
  void trackDiagnosticSocketForTesting(EllaCaptureProtocolSocket socket) =>
      _trackDiagnosticSocket(socket, _socketOpenGeneration);

  final EllaCaptureMemoryLoader? _memoryLoader;
  final AccountCommitAuthority? Function()? _memoryAccountAuthority;
  final List<Duration> _memoryRetryDelays;
  final EllaCaptureProtocolSocket? Function()? _activeProtocolSocket;
  final EllaCaptureFinalizationRequest? _finalizationRequest;
  final Future<CaptureProvider> Function()? _bootForTesting;
  final Future<DeviceConnection?> Function(String deviceId)? _connectDeviceForTesting;
  final Future<DeviceConnection?> Function(String deviceId, {required bool force})? _ensureDeviceConnectionForTesting;
  final Future<void> Function(String deviceId)? _disconnectDeviceForTesting;
  Future<void>? _disconnectFuture;
  final _protocolFinalizations = Expando<Future<CreateConversationResponse?>>();
  CaptureProvider? _provider;
  EllaCaptureProtocolSocket? _protocolSocket;
  EllaCaptureProtocolSocket? _finalizationSocket;
  bool _diagnosticTracked = false;
  ({
    EllaCaptureProtocolSocket socket,
    int openGeneration,
    String uid,
    int bindingEpoch,
    ({String uid, String profile, int generation, int presentationRevision}) profile,
  })? _diagnosticOwner;
  int _socketOpenGeneration = 0;
  Future<CaptureProvider>? _boot;
  StreamSubscription<EllaCaptureRevocation>? _revocationSubscription;
  final ValueNotifier<EllaCaptureRevocation?> lastRevocation = ValueNotifier<EllaCaptureRevocation?>(null);
  final ValueNotifier<bool> protocolUnavailable = ValueNotifier<bool>(false);

  CaptureProvider? get provider => _provider;
  String? get boundOwnerId => authority.boundUid;

  final ValueNotifier<bool> finishPending = ValueNotifier<bool>(false);
  EllaCaptureSessionView? _finishingSession;
  EllaCaptureSessionView? _finishedSession;
  EllaCaptureSessionView? _finishOutcomeSession;
  EllaCaptureFinishOutcome? _finishOutcome;

  EllaCaptureFinishOutcome? finishOutcomeFor(EllaCaptureSessionView session) =>
      authority.hasCurrentAuthority && authority.boundUid == session.uid &&
          authority.bindingEpoch == session.bindingEpoch && session.matches(_finishOutcomeSession)
      ? _finishOutcome : null;

  Future<void>? _finishSessionFuture;
  Future<void>? _finishConversationFuture;
  EllaCaptureSessionView? _finishConversationSession;

  EllaCaptureSessionView? get captureSession {
    if (_disposed) return null;
    final currentProvider = provider;
    final uid = authority.boundUid;
    if (!authority.hasCurrentAuthority || currentProvider == null || uid == null) return null;
    final recordingId = currentProvider.activeRecordingId;
    final source = currentProvider.liveCaptureSource;
    if (recordingId == null || source == null) return null;
    return EllaCaptureSessionView(
      uid: uid,
      bindingEpoch: authority.bindingEpoch,
      recordingId: recordingId,
      windowId: currentProvider.captureWindowGeneration,
      source: source,
      canFinish: currentProvider.isPhoneMicBatchRecording || currentProvider.activeCaptureSessionId != null,
    );
  }

  bool isCurrentCaptureSession(EllaCaptureSessionView session) => session.matches(captureSession);

  bool _pauseChangePending = false;
  CaptureOperationCancellation? _resumeCancellation;
  EllaCaptureSessionView? _resumeSession;
  void cancelPendingCaptureChange() => _resumeCancellation?.cancel();
  bool get pauseChangePending => _pauseChangePending;

  bool isCapturePaused(EllaCaptureSessionView session) =>
      isCurrentCaptureSession(session) &&
      (session.source == 'phone' ? provider?.isLiveCapturePaused == true : provider?.isPaused == true);

  bool canPauseCapture(EllaCaptureSessionView session) {
    if (session.source != 'phone') {
      return isCurrentCaptureSession(session) && provider != null && !provider!.isPaused;
    }
    return !_pauseChangePending &&
        !finishPending.value &&
        _finishedSession?.matches(session) != true &&
        isCurrentCaptureSession(session) &&
        provider?.canPauseLiveCapture == true;
  }

  bool canResumeCapture(EllaCaptureSessionView session) {
    if (session.source != 'phone') return isCapturePaused(session);
    return !_pauseChangePending &&
        !finishPending.value &&
        _finishedSession?.matches(session) != true &&
        isCapturePaused(session);
  }

  /// Uses the official policy for pendants and committed live state for phone capture.
  /// No boot, account rebind, capture start or Whisper operation occurs here.
  Future<void> setCapturePaused(EllaCaptureSessionView session,
      {required bool paused, bool Function()? isCurrent}) async {
    if (paused) _retireDefaultNecklaceRecording();
    final capture = provider;
    if (session.source != 'phone') {
      // Match the official pendant control: dispatch the upstream Pause/Resume,
      // with only the current account/session fence required by Ella security.
      // No phone-start timeout, local cancellation mute, Finish/UI busy gate, or
      // phase reinterpretation is introduced around the pendant operation.
      bool current() => identical(capture, provider) && isCurrentCaptureSession(session) && (isCurrent?.call() ?? true);
      if (capture == null || !current()) throw StateError('capture authority changed');
      final traceAttempt = _protocolTrace.activeAttempt;
      _protocolTrace.record(traceAttempt, paused ? CaptureTraceEvent.pauseRequested : CaptureTraceEvent.resumeRequested,
          ownerCurrent: current(), paused: capture.isPaused);
      if (paused) {
        await capture.pauseCapture(isCurrent: current);
      } else {
        await capture.resumeCapture(isCurrent: current);
      }
      _protocolTrace.record(traceAttempt, paused ? CaptureTraceEvent.pauseCommitted : CaptureTraceEvent.resumeCommitted,
          ownerCurrent: current(), paused: capture.isPaused);
      return;
    }
    final cancellation = paused ? null : CaptureOperationCancellation();
    bool originCurrent() =>
        identical(capture, provider) && isCurrentCaptureSession(session) && (isCurrent?.call() ?? true);
    bool current() => cancellation?.isCancelled != true && originCurrent();
    if (capture == null || !current() || (paused ? !canPauseCapture(session) : !canResumeCapture(session))) {
      throw StateError('capture pause unavailable');
    }
    final traceAttempt = _protocolTrace.activeAttempt;
    _protocolTrace.record(traceAttempt, paused ? CaptureTraceEvent.pauseRequested : CaptureTraceEvent.resumeRequested,
        ownerCurrent: current(), paused: capture.isLiveCapturePaused);
    _pauseChangePending = true;
    _resumeCancellation = cancellation;
    _resumeSession = cancellation == null ? null : session;
    try {
      if (paused) {
        await capture.pauseCapture(isCurrent: current);
      } else {
        await capture.resumeCapture(isCurrent: current, cancellation: cancellation, canCancel: originCurrent);
      }
      if (!current() || (paused ? !capture.isLiveCapturePaused : !capture.canPauseLiveCapture)) {
        throw StateError('capture pause not confirmed');
      }
      _protocolTrace.record(traceAttempt, paused ? CaptureTraceEvent.pauseCommitted : CaptureTraceEvent.resumeCommitted,
          ownerCurrent: current(), paused: capture.isLiveCapturePaused);
    } finally {
      _pauseChangePending = false;
      cancellation?.dispose();
      if (identical(_resumeCancellation, cancellation)) {
        _resumeCancellation = null;
        _resumeSession = null;
      }
    }
  }

  bool isFinishingFor(String uid) =>
      finishPending.value &&
      _finishingSession?.uid == uid &&
      _finishingSession?.bindingEpoch == authority.bindingEpoch &&
      authority.hasCurrentAuthority;

  bool canFinishSession(EllaCaptureSessionView session) =>
      isCurrentCaptureSession(session) &&
      captureSession?.canFinish == true &&
      !finishPending.value &&
      !session.matches(_finishedSession);

  /// Join only this session's in-flight Finish. Never queue a second finalization
  /// behind a pending request, including after a dock remount.
  Future<void> finishCaptureSession(EllaCaptureSessionView session, {bool stopDeviceIngress = false}) {
    if (!isCurrentCaptureSession(session)) return Future<void>.value();
    final pending = _finishSessionFuture;
    if (pending != null) return session.matches(_finishingSession) ? pending : Future<void>.value();
    if (!canFinishSession(session)) return Future<void>.value();
    final completion = Completer<void>();
    _finishingSession = session;
    _finishSessionFuture = completion.future;
    finishPending.value = true;
    () async {
      try {
        // A listener notified above may retire the owner synchronously.
        if (isCurrentCaptureSession(session)) {
          // Once submitted, an error may follow partial durable side effects.
          // Keep the fence; a new owner/window must never be an implicit retry.
          _finishedSession = session;
          await finishConversation(
              isCurrent: () => isCurrentCaptureSession(session), stopDeviceIngress: stopDeviceIngress);
        }
        completion.complete();
      } catch (error, stack) {
        completion.completeError(error, stack);
      } finally {
        _finishingSession = null;
        _finishSessionFuture = null;
        finishPending.value = false;
      }
    }();
    return completion.future;
  }

  /// Boots the vendored stack once; later calls join the same future.
  Future<CaptureProvider> ensureBooted() {
    if (_disposed) return Future.error(StateError('capture runtime disposed'));
    final existing = _boot;
    if (existing != null) return existing;
    final attempt = _bootForTesting?.call() ?? _bootProduction();
    _boot = attempt;
    attempt.then(
      (_) {},
      onError: (Object _, StackTrace __) {
        if (identical(_boot, attempt)) _boot = null;
      },
    );
    return attempt;
  }

  Future<CaptureProvider> _bootProduction() async {
    if (!_healthObserving) {
      _healthObserving = true;
      WidgetsBinding.instance.addObserver(this);
      _healthLifecycle = WidgetsBinding.instance.lifecycleState?.name ?? 'unknown';
    }
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
    _listenForDefaultNecklaceRecording(services.device);
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
          if (_disposed || _finalizationSocket != null) return null;
          final previous = _protocolSocket;
          final opening = _beginSocketOpen(
            previous,
            force: force,
            codec: codec,
            sampleRate: sampleRate,
            clientConversationId: clientConversationId,
          );
          final openGeneration = opening.openGeneration;
          if (opening.reused != null) {
            _protocolTrace.record(_protocolTrace.activeAttempt, CaptureTraceEvent.socketReused,
                ownerCurrent: authority.hasCurrentAuthority, socketGeneration: openGeneration);
            return opening.reused;
          }
          if (previous != null) {
            final previousTraceAttempt = _protocolTrace.activeAttempt;
            _protocolTrace.record(previousTraceAttempt, CaptureTraceEvent.rebindOldStopBegin,
                socketGeneration: openGeneration);
            await previous.stop(reason: 'capture socket replaced');
            if (_disposed || openGeneration != _socketOpenGeneration) return null;
            _protocolTrace.record(previousTraceAttempt, CaptureTraceEvent.rebindOldStopEnd,
                socketGeneration: openGeneration);
          }
          final traceAttempt =
              _protocolTrace.begin(socketGeneration: openGeneration, paused: _provider?.isLiveCapturePaused);
          final originUid = authority.boundUid;
          final originEpoch = authority.bindingEpoch;
          final deliveryQuery = Map<String, String>.unmodifiable(captureDeliveryQuery);
          protocolUnavailable.value = false;
          late final EllaCaptureProtocolSocket socket;
          bool isCurrent() =>
              !_disposed &&
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
            deliveryQuery: deliveryQuery,
            hasCurrentDeliveryBinding: () => mapEquals(deliveryQuery, captureDeliveryQuery),
            onAdmissionFailure: (reason, closeCode) => _onCaptureProtocolFailure(socket, reason, closeCode),
            diagnosticTrace: _protocolTrace,
            diagnosticAttempt: traceAttempt,
          );
          _protocolSocket = socket;
          _trackDiagnosticSocket(socket, openGeneration);
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
          await provider.stopStreamRecording(reason: 'capture_protocol_unavailable', resumeHandedOffPendant: false);
        } else {
          await provider.stopStreamDeviceRecording();
        }
      } catch (error) {
        Logger.debug('[EllaUpstreamCapture] protocol failure teardown: ${error.runtimeType}');
      }
    }());
  }

  Future<CreateConversationResponse?> _processCaptureProtocolConversation() {
    final socket = _finalizationSocket ?? _activeProtocolSocket?.call() ?? _protocolSocket;
    if (socket == null) {
      return authority.hasCurrentAuthority
          ? Future.error(StateError('capture finalization not available'))
          : Future.value(null);
    }
    // A failed/unknown request may already have committed. Join the exact socket's
    // original result; another Finish/Disconnect must never submit it again.
    return _protocolFinalizations[socket] ??= () async {
      final exactAuthority = _CaptureProtocolAccountAuthority(socket, authority);
      final ownerAuthority = _CaptureRecoveryAuthority(this);
      final submittedSession = _finishingSession;
      final result = await finalizeEllaCaptureProtocolConversation(
        socket: socket,
        exactAuthority: exactAuthority,
        request: _finalizationRequest ?? ella_api.processInProgressConversation,
      );
      if (result == null && ownerAuthority.isExactCurrent()) {
        throw StateError('capture finalization not confirmed');
      }
      final conversation = result?.conversation;
      if (submittedSession != null && conversation != null && ownerAuthority.isExactCurrent()) {
        _finishOutcomeSession = submittedSession;
        _finishOutcome = conversation.discarded ? EllaCaptureFinishOutcome.discarded
            : conversation.status == ConversationStatus.failed ? EllaCaptureFinishOutcome.failed
            : conversation.status == ConversationStatus.completed &&
                conversation.structured.overview.trim().isNotEmpty
                ? EllaCaptureFinishOutcome.saved : EllaCaptureFinishOutcome.processing;
      }
      return result;
    }();
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
    if (_disposed) throw StateError('capture runtime disposed');
    final gatedDeviceConnection = ellaGatedDeviceConnectionLoader((deviceId) async {
      final epoch = authority.bindingEpoch;
      final uid = authority.boundUid;
      final connection = await wiring.ensureDeviceConnection(deviceId);
      // Diagnostics are optional: a custom connection may not expose a native
      // transport. Observation must never turn a usable connection into failure.
      NativeBleTransport? healthTransport;
      try {
        final transport = connection?.transport;
        if (transport is NativeBleTransport) healthTransport = transport;
      } catch (_) {
        // Keep coverage explicitly absent; do not touch the capture path.
      }
      if (!_disposed && authority.hasCurrentAuthority && authority.bindingEpoch == epoch && authority.boundUid == uid) {
        _healthTransport = healthTransport;
        _healthBindingEpoch = epoch;
      }
      return connection;
    }, authority);
    final gatedSocket = ellaGatedConversationSocketOpen(wiring.openConversationSocket, authority);
    wiring.wal.getSyncs().phone.recoveryAuthorityProvider = () => _CaptureRecoveryAuthority(this);
    late final CaptureProvider provider;
    wiring.wal.getSyncs().phone.captureProvenanceProvider = () {
      if (_disposed || !authority.hasCurrentAuthority) return null;
      final socket = _activeProtocolSocket?.call() ?? _protocolSocket;
      final protocol = socket?.captureAuthority;
      return {
        'account_id': authority.boundUid,
        'binding_epoch': authority.bindingEpoch,
        'source_recording_id': provider.activeRecordingId,
        'capture_session_id': provider.activeCaptureSessionId,
        'source_kind': provider.liveCaptureSource,
        'socket_generation': _socketOpenGeneration,
        'protocol_generation': protocol?.generation,
        'protocol_conversation_id': protocol?.conversationId,
        'socket_admitted': socket?.state == SocketServiceState.connected,
      };
    };
    provider = CaptureProvider(
      externalActions: memoryBridge,
      walService: wiring.wal,
      phoneMicRecorder: EllaGatedMicRecorderService(wiring.phoneMic, authority),
      phoneMicBatchSupported: wiring.batchSupported ?? (Platform.isIOS || Platform.isAndroid),
      authBoundary: wiring.auth ?? CaptureAuthBoundary.production,
      connectivity: wiring.connectivity ?? CaptureConnectivityBoundary.production(),
      now: wiring.now ?? DateTime.now,
      scheduling: wiring.scheduling ?? const WallClockCaptureScheduling(),
      preferences: wiring.preferences ?? upstream.SharedPreferencesUtil(),
      bleListeners: wiring.ble ?? const BleBridgeCaptureListeners(),
      openSocket: (
          {required codec,
          required sampleRate,
          required language,
          required force,
          source,
          clientConversationId,
          customSttConfig,
          geolocation}) async {
        if (_disposed) return null;
        final socket = await gatedSocket(
            codec: codec,
            sampleRate: sampleRate,
            language: language,
            force: force,
            source: source,
            clientConversationId: clientConversationId,
            customSttConfig: customSttConfig,
            geolocation: geolocation);
        if (_disposed) {
          await socket?.stop(reason: 'capture runtime disposed during open');
          return null;
        }
        return socket;
      },
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
    _provider?.removeListener(_onCaptureDeliveryChanged);
    _provider = provider;
    _observeDelivery();
    provider.addListener(_onCaptureDeliveryChanged);
    finishPending.removeListener(_scheduleDeliveryRefresh);
    finishPending.addListener(_scheduleDeliveryRefresh);
    finishPending.removeListener(_scheduleDefaultNecklaceRecording);
    finishPending.addListener(_scheduleDefaultNecklaceRecording);
    unawaited(_revocationSubscription?.cancel());
    _revocationSubscription = authority.revocations.listen(_onRevoked);
    return provider;
  }

  /// Binds current consent/account authority without changing upstream user mute.
  Future<({int? epoch, bool consentRequired})> _bindAccountEpoch(String uid, {
    bool Function()? beforeBindIsCurrent,
    void Function(int epoch)? onOwnBinding,
  }) async {
    const unavailable = (epoch: null, consentRequired: false);
    if (_disposed) return unavailable;
    final previousEpoch = authority.bindingEpoch;
    try {
      await _teardown;
    } catch (_) {
      return unavailable;
    }
    if (_disposed || authority.bindingEpoch != previousEpoch || !authority.isCurrentOwner(uid) ||
        !(beforeBindIsCurrent?.call() ?? true)) return unavailable;
    if (!authority.bind(uid)) {
      return (epoch: null, consentRequired: authority.bindingEpoch == previousEpoch && authority.isCurrentOwner(uid));
    }
    final boundEpoch = authority.bindingEpoch;
    // Synchronous acknowledgement of this invocation's own bind, before any
    // native await. A later/external rebind can never update the caller's fence.
    onOwnBinding?.call(boundEpoch);
    if (boundEpoch != previousEpoch) _protocolTrace.clear();
    bool originIsCurrent() =>
        authority.bindingEpoch == boundEpoch &&
        authority.isCurrentOwner(uid) &&
        authority.boundUid == uid &&
        authority.hasCurrentAuthority;
    try {
      if (!await _nativeAuthorization.bind(uid, boundEpoch, isCurrent: originIsCurrent)) {
        if (originIsCurrent()) authority.release();
        return unavailable;
      }
    } catch (error) {
      if (originIsCurrent()) authority.release();
      Logger.debug('[EllaUpstreamCapture] native account admission unconfirmed: ${error.runtimeType}');
      return unavailable;
    }
    return originIsCurrent() ? (epoch: boundEpoch, consentRequired: false) : unavailable;
  }

  Future<bool> bindAccount(String uid) async {
    final bound =(await _bindAccountEpoch(uid)).epoch != null;

  if (bound) _scheduleDefaultNecklaceRecording();
    return bound;
  }

  Future<void> _teardown = Future<void>.value();
  bool _nativeRetirementFailed = false;

  /// Completes when the teardown ordered by the latest revocation finished.
  Future<void> get pendingTeardown => _teardown;

  void _onRevoked(EllaCaptureRevocation revocation) {
    _protocolTrace.clear();
    _deliveryRevision++;
    _deliveryRefreshRequested = false;
    _diagnosticOwner = null;
    memoryBridge.cancel();
    lastRevocation.value = revocation;
    // Frames are already refused synchronously by the gate; this only stops
    // upstream capture through its public API, off the frame callback.
    // Invalidate an in-flight allow immediately, even while an earlier Stop drains.
    final nativeDenial =
        _nativeAuthorization.deny().then<Object?>((_) => null, onError: (Object error, StackTrace _) => error);
    _teardown = _teardown.then((_) => _stopUpstreamCapture(revocation, nativeDenial),
        onError: (Object _, StackTrace __) => _stopUpstreamCapture(revocation, nativeDenial));
    unawaited(_teardown.catchError((Object error) {
      Logger.debug('[EllaUpstreamCapture] native retirement unconfirmed: ${error.runtimeType}');
    }));
  }

  Future<void> _stopUpstreamCapture(EllaCaptureRevocation revocation, Future<Object?> nativeDenial) async {
    final provider = _provider;
    Object? retirementError = await nativeDenial;
    if (provider == null) {
      _nativeRetirementFailed = retirementError != null;
      if (retirementError != null) throw retirementError;
      return;
    }
    try {
      await provider.stopStreamRecording(reason: 'ella_capture_authority_revoked');
    } catch (error) {
      retirementError ??= error;
      Logger.debug('[EllaUpstreamCapture] phone stop after revocation: ${error.runtimeType}');
    }
    try {
      await provider.stopStreamDeviceRecording();
    } catch (error) {
      retirementError ??= error;
      Logger.debug('[EllaUpstreamCapture] device stop after revocation: ${error.runtimeType}');
    }
    if (revocation.reason == EllaCaptureRevocationReason.accountSwitch ||
        revocation.reason == EllaCaptureRevocationReason.released) {
      // Upstream's own account-retirement fence for live segments and WALs.
      provider.clearUserData();
    }
    _nativeRetirementFailed = retirementError != null;
    if (retirementError != null) throw retirementError;
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
    final admission = await _bindAccountEpoch(uid);
    final boundEpoch = admission.epoch;
    if (boundEpoch == null) {
      return admission.consentRequired ? EllaCaptureStartOutcome.consentRequired : EllaCaptureStartOutcome.unavailable;
    }
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

  Future<void> stopCaptureEscape() async {
    _retireDefaultNecklaceRecording();
    final target = _resumeSession ?? captureSession;
    if (target == null || !isCurrentCaptureSession(target)) return;
    cancelPendingCaptureChange();
    if (target.source == 'phone') {
      await stopPhoneCapture();
    } else {
      await provider?.stopStreamDeviceRecording(cleanDevice: false).timeout(const Duration(seconds: 15));
    }
  }

  Future<void> stopPhoneCapture() async {
    final cancellingResume = _resumeCancellation != null;
    cancelPendingCaptureChange();
    final provider = _provider;
    if (provider == null) return;
    await provider.stopStreamRecording(resumeHandedOffPendant: !cancellingResume).timeout(const Duration(seconds: 15));
  }

  /// Upstream's single "finish the live capture" action (processes the conversation).
  Future<void> finishConversation({bool Function()? isCurrent, bool stopDeviceIngress = false}) {
    final provider = _provider;
    if (provider == null) return Future.value();
    final session = captureSession;
    final pending = _finishConversationFuture;
    if (pending != null) {
      final original = _finishConversationSession;
      if (session != null &&
          original != null &&
          session.uid == original.uid &&
          session.bindingEpoch == original.bindingEpoch &&
          session.recordingId == original.recordingId) return pending;
      return Future.error(StateError('another capture finalization is pending'));
    }
    final finishing = _activeProtocolSocket?.call() ?? _protocolSocket;
    final completion = Completer<void>();
    _finishConversationFuture = completion.future;
    _finishConversationSession = session;
    _finalizationSocket = finishing;
    () async {
      try {
        await provider.finishCapture(isCurrent: isCurrent, stopDeviceIngress: stopDeviceIngress);
      } finally {
        if (identical(_finalizationSocket, finishing)) _finalizationSocket = null;
        if (identical(_finishConversationFuture, completion.future)) {
          _finishConversationFuture = null;
          _finishConversationSession = null;
        }
      }
    }()
        .then(completion.complete, onError: completion.completeError);
    return completion.future;
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
  Future<EllaCaptureStartOutcome> connectNecklace(String uid, BtDevice device, {
    bool explicitSelection = true,
    bool Function()? lifecycleIsCurrent,
  }) {
    final pending = _necklaceConnectFuture;
    if (pending != null) {
      return _connectingNecklaceUid == uid && _connectingNecklaceId == device.id &&
              _connectingNecklaceEpoch == authority.bindingEpoch &&
              _connectingNecklaceIntent == _defaultRecordingIntent &&
              (lifecycleIsCurrent?.call() ?? true)
          ? pending
          : Future.value(EllaCaptureStartOutcome.unavailable);
    }
    if (explicitSelection) _retireDefaultNecklaceRecording();
    var originEpoch = authority.bindingEpoch;
    final originIntent = _defaultRecordingIntent;
    bool originCurrent() => !_disposed &&
        _defaultRecordingIntent == originIntent &&
        authority.isCurrentOwner(uid) &&
        authority.bindingEpoch == originEpoch &&
        (lifecycleIsCurrent?.call() ?? true);
    if (!originCurrent()) return Future.value(EllaCaptureStartOutcome.unavailable);
    _connectingNecklaceUid = uid;
    _connectingNecklaceId = device.id;
    _connectingNecklaceEpoch = originEpoch;
    _connectingNecklaceIntent = originIntent;
    final done = Completer<EllaCaptureStartOutcome>();
    _necklaceConnectFuture = done.future;
    () async {
      try {
        done.complete(
          await _connectNecklaceOnce(
            uid,
            device,
            originIsCurrent: originCurrent,
            onOwnBinding: (epoch) {
              originEpoch = epoch;
              _connectingNecklaceEpoch = epoch;
            },
          ),
        );
      } catch (error, stack) {
        done.completeError(error, stack);
      } finally {
        if (identical(_necklaceConnectFuture, done.future)) {
          _necklaceConnectFuture = null;
          _connectingNecklaceUid = null;
          _connectingNecklaceId = null;
          _connectingNecklaceEpoch = null;
          _connectingNecklaceIntent = null;
          // A genuinely later foreground event may run once after this start;
          // the connected notification generated by this start is consumed.
          if (_defaultRecordingRequested) _scheduleDefaultNecklaceRecording();
        }
      }
    }();
    return done.future;
  }

  Future<EllaCaptureStartOutcome> _connectNecklaceOnce(
    String uid,
    BtDevice device, {
    required bool Function() originIsCurrent,
    required void Function(int epoch) onOwnBinding,
  }) async {
    // An explicit new Connect may follow cleanup, never overtake it.
    final disconnect = _disconnectFuture;
    if (disconnect != null) await disconnect;
    if (!originIsCurrent()) return EllaCaptureStartOutcome.unavailable;
    final originEpoch = authority.bindingEpoch;
    final provider = await ensureBooted();
    if (!originIsCurrent() || !authority.isCurrentOwner(uid) || authority.bindingEpoch != originEpoch) {
      return EllaCaptureStartOutcome.unavailable;
    }
    final admission = await _bindAccountEpoch(uid,
      beforeBindIsCurrent: originIsCurrent, onOwnBinding: onOwnBinding);
    final boundEpoch = admission.epoch;
    if (boundEpoch == null) {
      return admission.consentRequired ? EllaCaptureStartOutcome.consentRequired : EllaCaptureStartOutcome.unavailable;
    }
    bool admittedOriginIsCurrent() =>
        originIsCurrent() && authority.bindingEpoch == boundEpoch &&
        authority.isCurrentOwner(uid) &&
        authority.boundUid == uid &&
        authority.hasCurrentAuthority;
    if (!admittedOriginIsCurrent()) {
      return EllaCaptureStartOutcome.unavailable;
    }
    // Both explicit Connect and an owner-bound foreground wake are deliberate
    // start intents. Upstream returns an already connected transport unchanged;
    // force is required to open a missing/disconnected remembered transport.
    final connection = await (_ensureDeviceConnectionForTesting?.call(device.id, force: true) ??
        _connectDeviceForTesting?.call(device.id) ??
        ServiceManager.instance().device.ensureConnection(device.id, force: true));
    if (connection == null) return EllaCaptureStartOutcome.unavailable;
    if (!admittedOriginIsCurrent()) {
      return EllaCaptureStartOutcome.unavailable;
    }
    upstream.SharedPreferencesUtil().btDevice = device;
    await provider.ensureDeviceRecordingDefault(device: device,
      isCurrent: admittedOriginIsCurrent,
      refreshDeviceStream: !identical(connection, _defaultRecordingConnection),
    );
    if (!admittedOriginIsCurrent()) return EllaCaptureStartOutcome.unavailable;
    _defaultRecordingConnection = connection;
    // The existing authoritative DeviceProvider persists its selected-device
    // metadata and owner binding; do not create a second identity store here.
    return admittedOriginIsCurrent() ? EllaCaptureStartOutcome.started : EllaCaptureStartOutcome.unavailable;
  }

  Future<void> disconnectNecklace({String? deviceId}) {
    _retireDefaultNecklaceRecording();
    final pending = _disconnectFuture;
    if (pending != null) return pending;
    final capture = _provider;
    if (capture == null) return Future.value();
    final target = deviceId ?? capture.recordingDevice?.id ?? upstream.SharedPreferencesUtil().btDevice.id;
    final uid = authority.boundUid;
    final epoch = authority.bindingEpoch;
    final session = captureSession;
    final recordingId = capture.activeRecordingId;
    bool ownerCurrent() =>
        !_disposed &&
        identical(_provider, capture) &&
        authority.hasCurrentAuthority &&
        authority.boundUid == uid &&
        authority.bindingEpoch == epoch;
    bool ownsTarget() =>
        ownerCurrent() && capture.recordingDevice?.id == target && capture.activeRecordingId == recordingId;
    final completion = Completer<void>();
    _disconnectFuture = completion.future;
    () async {
      if (!ownsTarget()) return;
      // A phone Finish can publish its resumed pendant before its own future
      // settles. Serialize that distinct recording before admitting this stop;
      // a foreign pending Finish must never stand in for this pendant's Finish.
      final priorSessionFinish = _finishSessionFuture;
      final priorConversationFinish = _finishConversationFuture;
      final finishing = _finishingSession ?? _finishConversationSession;
      if (session != null &&
          finishing != null &&
          finishing.recordingId != session.recordingId &&
          (priorSessionFinish != null || priorConversationFinish != null)) {
        await (priorSessionFinish ?? priorConversationFinish!);
        // Do not adopt a replacement producer, window, device or account that
        // appeared while the prior transaction was settling.
        if (!ownsTarget() || !isCurrentCaptureSession(session)) return;
      }
      try {
        if (session != null && session.source != 'phone') {
          // Finish can have reset its window while its exact request is still
          // pending. Join by recording identity, not the mutable next window.
          final finishing = _finishingSession;
          if (_finishSessionFuture != null &&
              finishing?.uid == session.uid &&
              finishing?.bindingEpoch == session.bindingEpoch &&
              finishing?.recordingId == session.recordingId) {
            await _finishSessionFuture;
          } else {
            await finishCaptureSession(session, stopDeviceIngress: true);
          }
        }
      } finally {
        try {
          // The coordinator rechecks this guard when the queued stop begins.
          // A retired owner or replacement producer is never stopped here.
          if (ownsTarget()) await capture.stopStreamDeviceRecording(cleanDevice: true, isCurrent: ownsTarget);
        } finally {
          if (ownerCurrent() &&
              target.isNotEmpty &&
              (capture.recordingDevice == null ||
                  (capture.recordingDevice?.id == target &&
                      (capture.activeRecordingId == null || capture.activeRecordingId == recordingId)))) {
            await (_disconnectDeviceForTesting?.call(target) ??
                ServiceManager.instance().device.disconnectDevice(target));
          }
        }
      }
    }()
        .then(completion.complete, onError: completion.completeError)
        .whenComplete(() {
      if (identical(_disconnectFuture, completion.future)) _disconnectFuture = null;
    });
    return completion.future;
  }

  /// Revokes the bound session (sign-out / account transition from the legacy host).
  Future<void> releaseAccount() async {
    if (!authority.isBound && _nativeRetirementFailed) {
      // An explicit retirement retry needs a fresh native denial/cleanup receipt,
      // not an assumed acknowledgement or a reset of the user's mute preference.
      _onRevoked(const EllaCaptureRevocation(uid: null, reason: EllaCaptureRevocationReason.released));
    } else {
      authority.release();
    }
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

class _CaptureRecoveryAuthority implements ExactAccountAuthorityVerifier {
  _CaptureRecoveryAuthority(this.runtime)
      : uid = runtime.authority.boundUid ?? '',
        epoch = runtime.authority.bindingEpoch;
  final EllaUpstreamCaptureRuntime runtime;
  @override
  final String uid;
  final int epoch;
  @override
  bool isExactCurrent() =>
      uid.isNotEmpty &&
      !runtime.isDisposed &&
      runtime.authority.boundUid == uid &&
      runtime.authority.bindingEpoch == epoch &&
      runtime.authority.hasCurrentAuthority;
}
