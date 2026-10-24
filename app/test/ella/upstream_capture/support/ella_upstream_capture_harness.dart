import 'package:omi/ella/upstream_capture/ella_native_capture_authorization.dart';
import 'dart:async';
import 'dart:io';
import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter/foundation.dart';
import 'package:omi/ella/services/whisper_delivery_client.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:geolocator/geolocator.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart' as fork;
import 'package:omi/backend/schema/conversation.dart' as ella_schema;
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/upstream_capture/ella_capture_authority.dart';
import 'package:omi/ella/upstream_capture/ella_capture_memory_bridge.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/upstream_capture/ella_capture_protocol_socket.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';
import 'package:omi/upstream_capture/backend/preferences.dart' as upstream;
import 'package:omi/upstream_capture/backend/schema/bt_device/bt_device.dart';
import 'package:omi/upstream_capture/backend/schema/conversation.dart' as upstream_schema;
import 'package:omi/upstream_capture/env/env.dart' as upstream_env;
import 'package:omi/upstream_capture/gen/phone_mic_pigeon.g.dart';
import 'package:omi/upstream_capture/providers/capture_provider.dart';
import 'package:omi/upstream_capture/services/capture/capture_seams.dart';
import 'package:omi/upstream_capture/services/capture/capture_session_owner.dart';
import 'package:omi/upstream_capture/services/capture/conversation_location_capture.dart';
import 'package:omi/upstream_capture/services/capture/scenarios/native_event_vector.dart';
import 'package:omi/upstream_capture/services/capture/stt_mode_resolver.dart';
import 'package:omi/upstream_capture/services/mic/native_mic_recorder_service.dart';
import 'package:omi/upstream_capture/services/services.dart';
import 'package:omi/upstream_capture/services/devices/connectors/device_connection.dart';
import 'package:omi/upstream_capture/services/mic/mic_arbiter.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';
import 'package:omi/upstream_capture/services/sockets/transcription_service.dart';
import 'package:omi/upstream_capture/services/wals/recording_transfer_coordinator.dart';
import 'package:omi/upstream_capture/services/wals/sync_rate_limiter.dart';
import 'package:omi/upstream_capture/services/wals/wal.dart';
import 'package:omi/upstream_capture/services/wals/wal_interfaces.dart';
import 'package:omi/upstream_capture/services/wals/wal_service.dart';

// Upstream's own capture test fixtures (vendored, byte-identical except imports).
import '../../../upstream_capture/support/capture/capture_replay_world.dart'
    show FakePhoneMicHostApi, ScriptedPureSocket, ScriptedUploads;
import '../../../upstream_capture/support/capture/scripted_device_connection.dart';
import '../../../upstream_capture/support/capture/virtual_capture_time.dart';

const String accountA = 'uid-a';
const String accountB = 'uid-b';

/// Protocol receipts only; no backend or physical recording is contacted.
class WhisperProtocolTransport extends ScriptedPureSocket {
  WhisperProtocolTransport(this.number, {this.beforeConnect, this.beforeDrain, this.beforeReady});
  final int number;
  final Future<void> Function()? beforeConnect;
  final Future<void> Function()? beforeDrain;
  final Future<void> Function()? beforeReady;
  Map<String, dynamic> get authority => {
        'protocol_version': 2,
        'conversation_id': 'synthetic-conversation-$number',
        'generation': 'synthetic-generation-$number',
        'owner_token': 'synthetic-owner-$number',
      };
  @override
  Future<bool> connect() async {
    await beforeConnect?.call();
    final connected = await super.connect();
    if (connected)
      Timer.run(() async {
        await beforeReady?.call();
        onMessage(jsonEncode({
          'type': 'service_status',
          'status': 'capture_protocol_ready',
          ...authority,
        }));
      });
    return connected;
  }

  @override
  void send(dynamic message) {
    super.send(message);
    if (message is String && (jsonDecode(message) as Map)['type'] == 'capture_drain') {
      () async {
        await beforeDrain?.call();
        onMessage(jsonEncode({'type': 'service_status', 'status': 'capture_protocol_drained', ...authority}));
      }();
    }
  }
}

/// Grants a fork (Ella) consent authority for [uid] in the shared prefs, exactly
/// like the existing AiConsentActiveSessionLease tests do.
void grantEllaConsent(fork.SharedPreferencesUtil preferences, String uid) {
  preferences.uid = uid;
  preferences.verifiedPersonaId = 'persona-$uid';
  preferences.acceptAiConsent(
    receiptId: 'aicr_receipt-$uid',
    uid: uid,
    profileBindingId: 'profile-binding-$uid',
    serverDecidedAt: '2026-07-27T00:00:00Z',
  );
  preferences.markAiConsentServerVerified(
    uid: uid,
    receiptId: 'aicr_receipt-$uid',
    policyVersion: fork.SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: fork.SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-binding-$uid',
    scopeVersion: fork.SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: fork.SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

class _NoBleListeners implements CaptureBleListeners {
  const _NoBleListeners();

  @override
  void addBatchRecordingFinalizedListener(void Function(String) callback) {}

  @override
  void removeBatchRecordingFinalizedListener(void Function(String) callback) {}
}

class _HarnessEnvFields implements upstream_env.EnvFields {
  const _HarnessEnvFields();

  @override
  String? get apiBaseUrl => 'http://127.0.0.1:0/';

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

bool _upstreamEnvInitialized = false;

/// The REAL upstream capture graph (CaptureProvider/CaptureController,
/// NativeMicRecorderService, TranscriptSegmentSocketService, WalService,
/// RecordingTransferCoordinator) composed by [EllaUpstreamCaptureRuntime.compose]
/// with the Ella per-frame gates, over upstream's scripted test transports.
class EllaUpstreamCaptureHarness {
  EllaUpstreamCaptureHarness._(this.tempDir);

  static final DateTime defaultStart = DateTime.utc(2026, 3, 1, 12, 0, 0);

  final Directory tempDir;
  late final VirtualClock clock;
  late final ManualScheduler scheduler;
  late final ScriptedUploads uploads;
  BleAudioCodec diagnosticPendantCodec = BleAudioCodec.pcm16;
  Future<SyncJobFetch> Function(String)? jobFetcher;
  late final FakePhoneMicHostApi hostApi;
  late final NativeMicRecorderService mic;
  late final WalService wal;
  late final RecordingTransferCoordinator coordinator;
  late final StreamController<bool> connectivity;
  late final EllaCaptureAuthority authority;
  late final EllaUpstreamCaptureRuntime runtime;
  late final CaptureProvider provider;
  late final fork.SharedPreferencesUtil ellaPreferences;
  final List<AiConsentActiveSessionLease> leases = [];
  final List<({String? source, ScriptedPureSocket transport, TranscriptSegmentSocketService service})> sockets = [];
  final List<Map<String, String>> socketDeliveryQueries = [];
  Listenable? deliveryChanges;
  WhisperDeliveryScope? Function()? deliveryScope;
  Completer<void>? holdNextSocketStart;
  Completer<void>? nextSocketStartEntered;
  Completer<void>? holdNextDrain;
  Completer<void>? nextDrainEntered;
  Completer<void>? holdNextReady;
  Completer<void>? nextReadyEntered;
  Completer<void>? holdNextAuth;
  Completer<void>? nextAuthEntered;

  ScriptedDeviceConnection? deviceConnection;
  String authenticatedUid = accountA;
  bool nativeCaptureAllowed = false;
  bool malformedNativeDenial = false;
  bool malformedNativeAllow = false;
  String? nativeCaptureOperation;
  int nativeCaptureRevision = 0;
  Completer<void>? holdNextNativeAllow;
  Completer<void>? nextNativeAllowEntered;
  late final nativeAuthorization = EllaNativeCaptureAuthorization(invoke: (method, arguments) async {
    if (method == 'getRevision') return nativeCaptureRevision;
    if (arguments['allowed'] == true) {
      final hold = holdNextNativeAllow;
      final entered = nextNativeAllowEntered;
      holdNextNativeAllow = null;
      nextNativeAllowEntered = null;
      entered?.complete();
      await hold?.future;
    }
    final operation = arguments['operation'] as String;
    if (arguments['allowed'] == false && nativeCaptureOperation != operation) {
      return {'revision': nativeCaptureRevision, 'allowed': false, 'operation': operation, 'ownedDenied': true};
    }
    final next = arguments['revision'] as int;
    if (next <= nativeCaptureRevision) throw StateError('stale native command');
    nativeCaptureRevision = next;
    nativeCaptureAllowed = arguments['allowed'] as bool;
    nativeCaptureOperation = nativeCaptureAllowed ? operation : null;
    if (nativeCaptureAllowed ? malformedNativeAllow : malformedNativeDenial) return null;
    return {
      'revision': nativeCaptureRevision,
      'allowed': nativeCaptureAllowed,
      'operation': operation,
      'ownedDenied': !nativeCaptureAllowed
    };
  });
  bool connected = true;
  bool protocolV2 = false;
  EllaCaptureProtocolSocket? Function()? diagnosticSocket;
  EllaCaptureMemoryLoader? memoryLoader;
  AccountCommitAuthority? Function()? memoryAccountAuthority;
  List<Duration>? memoryRetryDelays;
  String? processedMemoryId;
  int processCalls = 0;
  int connectionAttempts = 0;
  Future<DeviceConnection?> Function(String, {required bool force})? ensureConnectionOverride;
  Future<void> Function()? beforeDeviceDisconnect;
  final List<String> disconnectedDevices = [];
  Object? finalizationError;
  Future<void> Function()? beforeFinalization;
  IMicRecorderService Function(NativeMicRecorderService)? phoneMicWrapper;
  Future<bool> Function()? permissionRequester;
  final List<({String conversationId, int protocolVersion, String generation, String ownerToken})>
      protocolFinalizations = [];

  static final BtDevice pendant = BtDevice(id: 'pendant-1', name: 'Omi', type: DeviceType.omi, rssi: -40);

  static Future<EllaUpstreamCaptureHarness> boot({
    required Directory tempDir,
    bool initiallyConnected = true,
    bool grantConsent = true,
    bool protocolV2 = false,
    EllaCaptureProtocolSocket? Function()? diagnosticSocket,
    EllaCaptureMemoryLoader? memoryLoader,
    AccountCommitAuthority? Function()? memoryAccountAuthority,
    List<Duration>? memoryRetryDelays,
    String? processedMemoryId,
    Listenable? deliveryChanges,
    WhisperDeliveryScope? Function()? deliveryScope,
    Future<SyncJobFetch> Function(String)? jobFetcher,
    IMicRecorderService Function(NativeMicRecorderService)? phoneMicWrapper,
    Future<bool> Function()? permissionRequester,
  }) async {
    TestWidgetsFlutterBinding.ensureInitialized();
    final harness = EllaUpstreamCaptureHarness._(tempDir);
    harness.connected = initiallyConnected;
    harness.protocolV2 = protocolV2;
    harness.diagnosticSocket = diagnosticSocket;
    harness.memoryLoader = memoryLoader;
    harness.memoryAccountAuthority = memoryAccountAuthority;
    harness.memoryRetryDelays = memoryRetryDelays;
    harness.processedMemoryId = processedMemoryId;
    harness.deliveryChanges = deliveryChanges;
    harness.deliveryScope = deliveryScope;
    harness.jobFetcher = jobFetcher;
    harness.phoneMicWrapper = phoneMicWrapper;
    harness.permissionRequester = permissionRequester;
    await harness._boot(grantConsent: grantConsent);
    return harness;
  }

  Future<void> _boot({required bool grantConsent}) async {
    EllaProvisioningAuthorityCoordinator.resetForTesting();
    SharedPreferences.setMockInitialValues({});
    await fork.SharedPreferencesUtil.init();
    ellaPreferences = fork.SharedPreferencesUtil();
    if (grantConsent) grantEllaConsent(ellaPreferences, accountA);
    await upstream.SharedPreferencesUtil.init();
    if (!_upstreamEnvInitialized) {
      upstream_env.Env.init(const _HarnessEnvFields());
      _upstreamEnvInitialized = true;
    }
    SttModeResolver.instance = SttModeResolver(flagReader: () => false);

    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (MethodCall call) async {
        if (call.method == 'getApplicationDocumentsDirectory') return tempDir.path;
        return null;
      },
    );

    clock = VirtualClock(defaultStart);
    scheduler = ManualScheduler(clock: clock);
    uploads = ScriptedUploads(clock);
    hostApi = FakePhoneMicHostApi();
    mic = NativeMicRecorderService(
      hostApi: hostApi,
      registerFlutterApi: false,
      now: clock.now,
      periodic: scheduler.periodic,
    );
    wal = WalService(
      phoneUploadGate: uploads.buildGate(),
      phoneNow: clock.now,
      phonePeriodic: scheduler.periodic,
      phoneJobStatusFetcher: (jobId) =>
          jobFetcher?.call(jobId) ?? Future.value(const SyncJobFetch(SyncJobFetchOutcome.notFound)),
    );
    wal.start();
    await wal.syncs.phone.walReady;
    connectivity = StreamController<bool>.broadcast();

    authority = EllaCaptureAuthority(
      authenticatedUid: () => authenticatedUid,
      sessionStartAllowed: (uid) =>
          AiConsentActiveSessionLease.authorityForSessionStart(preferences: ellaPreferences, expectedUid: uid) != null,
      leaseFactory: ({required String uid, required FutureOr<void> Function() onAuthorityLost}) {
        final lease = AiConsentActiveSessionLease(
          uid: uid,
          onAuthorityLost: onAuthorityLost,
          preferences: ellaPreferences,
          refreshAuthority: (uid, receiptId, decidedAt) async =>
              const AiConsentAuthorityRefreshResult(AiConsentAuthorityRefreshDisposition.verified),
          revalidateProvisioning: (uid, receiptId) => true,
        );
        leases.add(lease);
        return lease;
      },
    );
    runtime = EllaUpstreamCaptureRuntime(
      authority: authority,
      nativeAuthorization: nativeAuthorization,
      deliveryChanges: deliveryChanges,
      deliveryScope: deliveryScope,
      memoryLoader: memoryLoader,
      memoryAccountAuthority: memoryAccountAuthority,
      memoryRetryDelays: memoryRetryDelays ?? const [Duration(seconds: 2)],
      bootForTesting: () async => provider,
      ensureDeviceConnectionForTesting: (id, {required force}) async {
        final override = ensureConnectionOverride;
        if (override != null) return override(id, force: force);
        connectionAttempts++;
        return deviceConnection;
      },
      connectDeviceForTesting: (_) async {
        connectionAttempts++;
        return deviceConnection;
      },
      disconnectDeviceForTesting: (id) async {
        await beforeDeviceDisconnect?.call();
        disconnectedDevices.add(id);
      },
      activeProtocolSocket: diagnosticSocket ??
          () => !protocolV2 || sockets.isEmpty ? null : sockets.last.service as EllaCaptureProtocolSocket,
      finalizationRequest: ({
        required conversationId,
        required protocolVersion,
        required generation,
        required ownerToken,
        required transportLost,
        expectedAuthenticatedUid,
        exactAuthority,
      }) async {
        protocolFinalizations.add((
          conversationId: conversationId,
          protocolVersion: protocolVersion,
          generation: generation,
          ownerToken: ownerToken,
        ));
        await beforeFinalization?.call();
        if (finalizationError != null) throw finalizationError!;
        return ella_schema.CreateConversationResponse.fromJson({
          'messages': const [],
          'conversation': {
            'id': conversationId,
            'created_at': '2026-09-29T00:00:00Z',
            'structured': {'title': 'Moment', 'overview': '', 'emoji': '', 'category': 'other'},
          },
        });
      },
    );

    // Placeholder passes; the production configuration comes from the runtime.
    coordinator = RecordingTransferCoordinator(
      reconcile: () async {},
      discover: () async {},
      refreshPending: () async {},
      drain: () async => const RecordingTransferDrainResult.skipped(),
      autoUploadEnabled: () => false,
      clock: clock.now,
      scheduleCooldown: (delay, callback) => scheduler.once(delay, callback),
    );
    runtime.configureTransferCoordinator(
      coordinator,
      wal,
      connectivityChanges: connectivity.stream,
      initiallyConnected: connected,
    );

    provider = runtime.compose(
      EllaUpstreamCaptureWiring(
        wal: wal,
        phoneMic: phoneMicWrapper?.call(mic) ??
            ArbitratedMic(inner: mic, arbiter: MicArbiter(ownership: AudioCaptureOwnership()), owner: 'test-phone'),
        openConversationSocket: _openSocket,
        ensureDeviceConnection: (deviceId) async => deviceConnection,
        owner: CaptureSessionOwner(coordinator: coordinator, startForeground: () async {}, stopForeground: () async {}),
        batchSupported: true,
        auth: CaptureAuthBoundary(isSignedIn: () => true, refreshIdToken: () async => null),
        connectivity: CaptureConnectivityBoundary(
          initiallyConnected: connected,
          changes: connectivity.stream,
          isConnected: () => connected,
        ),
        now: clock.now,
        scheduling: scheduler,
        preferences: upstream.SharedPreferencesUtil(),
        ble: const _NoBleListeners(),
        location: ConversationLocationCapture(
          isLocationServiceEnabled: () async => false,
          checkPermission: () async => LocationPermission.denied,
          requestPermission: () async => LocationPermission.denied,
          now: clock.now,
        ),
        codec: (deviceId) async => diagnosticPendantCodec,
        microphonePermission: permissionRequester ?? () async => true,
        refreshConversation: (_) async {},
        processInProgressConversation: protocolV2
            ? null
            : () async {
                processCalls++;
                if (processedMemoryId != null) {
                  return upstream_schema.CreateConversationResponse.fromJson({
                    'messages': const [],
                    'conversation': {
                      'id': processedMemoryId,
                      'created_at': '2026-09-29T00:00:00Z',
                      'started_at': '2026-09-29T00:00:00Z',
                      'finished_at': '2026-09-29T00:10:00Z',
                      'status': 'completed',
                      'structured': {'title': 'Imported title', 'overview': '', 'emoji': '', 'category': 'other'},
                    },
                  });
                }
                return null;
              },
      ),
    );
  }

  Future<TranscriptSegmentSocketService?> _openSocket({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    customSttConfig,
    geolocation,
  }) async {
    if (deliveryScope != null && sockets.isNotEmpty) {
      await sockets.last.service.stop(reason: 'scripted production socket replacement');
    }
    final query = Map<String, String>.of(runtime.captureDeliveryQuery);
    final originUid = authority.boundUid;
    final originEpoch = authority.bindingEpoch;
    bool originCurrent() =>
        !runtime.isDisposed &&
        authority.hasCurrentAuthority &&
        authority.boundUid == originUid &&
        authority.bindingEpoch == originEpoch;
    final auth = holdNextAuth;
    final authEntered = nextAuthEntered;
    holdNextAuth = null;
    nextAuthEntered = null;
    final hold = holdNextSocketStart;
    final entered = nextSocketStartEntered;
    holdNextSocketStart = null;
    nextSocketStartEntered = null;
    final drain = holdNextDrain;
    final drainEntered = nextDrainEntered;
    holdNextDrain = null;
    nextDrainEntered = null;
    final ready = holdNextReady;
    final readyEntered = nextReadyEntered;
    holdNextReady = null;
    nextReadyEntered = null;
    final transport = protocolV2 && deliveryScope != null
        ? WhisperProtocolTransport(sockets.length, beforeConnect: () async {
            await ellaCaptureSocketHeaders(() async {
              authEntered?.complete();
              await auth?.future;
              return const <String, String>{};
            },
                hasOriginAuthority: originCurrent,
                hasCurrentDeliveryBinding: () => mapEquals(query, runtime.captureDeliveryQuery))();
            entered?.complete();
            await hold?.future;
          }, beforeDrain: () async {
            drainEntered?.complete();
            await drain?.future;
          }, beforeReady: () async {
            readyEntered?.complete();
            await ready?.future;
          })
        : ScriptedPureSocket();
    socketDeliveryQueries.add(query);
    transport.connectAllowed = () => connected;
    final service = protocolV2
        ? EllaCaptureProtocolSocket.withTransport(
            sampleRate,
            codec,
            language,
            transport,
            source: source,
            clientConversationId: clientConversationId,
            hasOriginAuthority: () =>
                !runtime.isDisposed &&
                authority.hasCurrentAuthority &&
                (deliveryScope == null || (authority.boundUid == originUid && authority.bindingEpoch == originEpoch)),
            deliveryQuery: query,
            hasCurrentDeliveryBinding: () => deliveryScope == null || mapEquals(query, runtime.captureDeliveryQuery),
          )
        : TranscriptSegmentSocketService.withSocket(
            sampleRate,
            codec,
            language,
            transport,
            source: source,
            clientConversationId: clientConversationId,
          );
    sockets.add((source: source, transport: transport, service: service));
    await service.start();
    if (service.state != SocketServiceState.connected) return null;
    return service;
  }

  ScriptedPureSocket? get socket => sockets.isEmpty ? null : sockets.last.transport;

  /// Every binary audio frame any transcription socket actually sent.
  int get audioFramesSent => sockets.fold(0, (sum, s) => sum + s.transport.sentBinary.length);

  Future<bool> bind([String? uid]) => runtime.bindAccount(uid ?? authenticatedUid);

  Future<int> startPhone() async {
    await provider.streamRecording();
    await settle();
    mic.onStateChanged(PhoneMicCaptureState.running, hostApi.lastStartSessionId!);
    await settle();
    return hostApi.lastStartSessionId!;
  }

  /// Deterministic native PCM frames through the REAL NativeMicRecorderService.
  void injectPhoneFrames(int count, {int? sessionId, int firstFrameIndex = 0}) {
    final session = sessionId ?? hostApi.lastStartSessionId!;
    for (var i = 0; i < count; i++) {
      mic.onAudioFrame(NativeEventVector.synthesizePcmFrame(firstFrameIndex + i), session);
    }
  }

  Future<ScriptedDeviceConnection> connectPendant() async {
    final link = ScriptedDeviceConnection();
    deviceConnection = link;
    await provider.streamDeviceRecording(device: pendant);
    await settle();
    return link;
  }

  void setConnected(bool value) {
    if (connected == value) return;
    connected = value;
    connectivity.add(value);
  }

  /// Switches the signed-in account the way Ella's account transition does:
  /// Firebase uid and the persisted prefs uid move to [uid].
  void switchAccount(String uid) {
    authenticatedUid = uid;
    ellaPreferences.uid = uid;
  }

  Future<void> settle({int maxTurns = 64}) async {
    for (var i = 0; i < maxTurns; i++) {
      await scheduler.waitForCallbackIo();
      await coordinator.waitUntilIdle();
      await pumpEventQueue();
      await runtime.pendingTeardown;
      await runtime.pendingDefaultRecordingForTesting;
      await scheduler.waitForCallbackIo();
      await coordinator.waitUntilIdle();
      if (!coordinator.hasInFlight && !scheduler.hasInFlightIo) return;
    }
    throw StateError('EllaUpstreamCaptureHarness.settle: work did not go idle');
  }

  Future<void> elapse(Duration duration) async {
    scheduler.elapse(duration);
    await settle();
  }

  Future<Map<WalStatus, int>> walCounts() async {
    final counts = <WalStatus, int>{};
    for (final w in await wal.syncs.phone.getAllWals()) {
      counts[w.status] = (counts[w.status] ?? 0) + 1;
    }
    return counts;
  }

  Future<void> dispose() async {
    await scheduler.waitForCallbackIo();
    await coordinator.waitUntilIdle();
    authority.release();
    await runtime.pendingTeardown;
    runtime.dispose();
    authority.dispose();
    for (final lease in leases) {
      lease.stop();
    }
    provider.dispose();
    coordinator.dispose();
    await connectivity.close();
    await wal.stop();
    SttModeResolver.debugResetInstance();
    SyncRateLimiter.instance.clear();
    EllaProvisioningAuthorityCoordinator.resetForTesting();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      null,
    );
  }
}
