import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:collection/collection.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_provider_utilities/flutter_provider_utilities.dart';
import 'package:geolocator/geolocator.dart';
import 'package:permission_handler/permission_handler.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/geolocation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ai_consent_coordinator.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/providers/calendar_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/providers/usage_provider.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/sockets/transcription_service.dart';
import 'package:omi/services/wals.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/alerts/app_snackbar.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/image/image_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_service.dart';
import 'package:omi/main.dart';

import 'package:omi/backend/schema/message_event.dart'
    show
        MessageEvent,
        MessageServiceStatusEvent,
        ConversationProcessingStartedEvent,
        ConversationEvent,
        LastConversationEvent,
        SpeakerLabelSuggestionEvent,
        TranslationEvent,
        PhotoProcessingEvent,
        PhotoDescribedEvent,
        FreemiumThresholdReachedEvent,
        SegmentsDeletedEvent;

enum PhoneCaptureStartResult {
  started,
  consentUnavailable,
  accountNotReady,
  microphonePermissionDenied,
  transcriptionUnavailable,
  recorderUnavailable,
  cancelled,
}

@visibleForTesting
class PhoneCaptureStartProof {
  final Completer<void> _firstAudioFrame = Completer<void>();

  bool acceptFrame(List<int> bytes) {
    if (bytes.isEmpty) return false;
    if (!_firstAudioFrame.isCompleted) _firstAudioFrame.complete();
    return true;
  }

  Future<void> waitForAudio({Duration timeout = const Duration(seconds: 5)}) =>
      _firstAudioFrame.future.timeout(timeout);
}

@visibleForTesting
class DeviceCaptureStartProof {
  final Completer<void> _firstTransmittedAudioFrame = Completer<void>();

  bool acceptTransmittedFrame(List<int> bytes) {
    if (bytes.isEmpty) return false;
    if (!_firstTransmittedAudioFrame.isCompleted) {
      _firstTransmittedAudioFrame.complete();
    }
    return true;
  }

  Future<void> waitForTransmittedAudio({Duration timeout = const Duration(seconds: 5)}) =>
      _firstTransmittedAudioFrame.future.timeout(timeout);
}

@visibleForTesting
Future<bool> ensureCaptureConsentAuthority({
  required bool Function() hasCurrentConsent,
  required String Function() authenticatedUid,
  required String Function() persistedConsentReceiptId,
  required Future<bool> Function(String uid) refreshAuthority,
}) async {
  if (hasCurrentConsent()) return true;
  final uid = authenticatedUid().trim();
  final priorReceiptId = persistedConsentReceiptId().trim();
  if (uid.isEmpty || priorReceiptId.isEmpty || !await refreshAuthority(uid)) return false;
  return hasCurrentConsent() && persistedConsentReceiptId().trim() == priorReceiptId;
}

typedef InProgressConversationFetchCall = Future<List<ServerConversation>> Function({
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});
typedef InProgressConversationProcessCall = Future<CreateConversationResponse?> Function({
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});
typedef ActiveWalAuthorityProvider = ActiveWalAuthority? Function();
typedef CaptureGeolocationSender = Future<bool> Function({
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});
typedef CaptureConsentAuthorityEnsurer = Future<bool> Function();
typedef DeviceCaptureStarter = Future<bool> Function();
typedef PhoneCaptureStarter = Future<PhoneCaptureStartResult> Function();

Future<List<ServerConversation>> _fetchInProgressConversation({
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    getConversations(
      statuses: [ConversationStatus.in_progress],
      limit: 1,
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

Future<CreateConversationResponse?> _processInProgressConversation({
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
}) =>
    processInProgressConversation(
      expectedAuthenticatedUid: expectedAuthenticatedUid,
      exactAuthority: exactAuthority,
    );

@visibleForTesting
class CaptureFinalizationOperation implements ExactAccountAuthorityVerifier {
  CaptureFinalizationOperation({
    required EllaAccountCommitLease accountLease,
    required this.captureGeneration,
    required int Function() currentCaptureGeneration,
  })  : _accountLease = accountLease,
        _currentCaptureGeneration = currentCaptureGeneration;

  final EllaAccountCommitLease _accountLease;
  final int captureGeneration;
  final int Function() _currentCaptureGeneration;

  bool get isCurrent => _accountLease.isExactCurrent() && captureGeneration == _currentCaptureGeneration();

  @override
  String get uid => _accountLease.uid;

  @override
  bool isExactCurrent() => isCurrent;

  void close() => _accountLease.close();
}

@visibleForTesting
class CaptureGeolocationOperation implements ExactAccountAuthorityVerifier {
  CaptureGeolocationOperation({
    required ActiveWalAuthority captureAuthority,
    required this.captureGeneration,
    required int Function() currentCaptureGeneration,
  })  : _captureAuthority = captureAuthority,
        _currentCaptureGeneration = currentCaptureGeneration;

  final ActiveWalAuthority _captureAuthority;
  final int captureGeneration;
  final int Function() _currentCaptureGeneration;

  @override
  String get uid => _captureAuthority.uid;

  @override
  bool isExactCurrent() => captureGeneration == _currentCaptureGeneration() && _captureAuthority.isExactCurrent();
}

class CaptureProvider extends ChangeNotifier
    with MessageNotifierMixin, WidgetsBindingObserver
    implements ITransctiptSegmentSocketServiceListener {
  ConversationProvider? conversationProvider;
  MessageProvider? messageProvider;
  PeopleProvider? peopleProvider;
  UsageProvider? usageProvider;
  CalendarProvider? calendarProvider;

  // Cache refresh for backend-created persons
  Future<void>? _peopleRefreshFuture;

  TranscriptSegmentSocketService? _socket;
  Timer? _keepAliveTimer;
  DateTime? _keepAliveLastExecutedAt;
  bool _replacingTranscriptionSocket = false;

  // Method channel for system audio permissions
  static late MethodChannel _screenCaptureChannel;
  static late MethodChannel _controlBarChannel;

  IWalService get _wal => ServiceManager.instance().wal;

  bool _isWalSupported = false;

  bool get isWalSupported => _isWalSupported;

  StreamSubscription<bool>? _connectionStateListener;
  bool _isConnected = ConnectivityService().isConnected;

  get isConnected => _isConnected;

  String? microphoneName;
  double microphoneLevel = 0.0;
  double systemAudioLevel = 0.0;

  bool _isAutoReconnecting = false;
  bool get isAutoReconnecting => _isAutoReconnecting;

  bool get outOfCredits => usageProvider?.isOutOfCredits ?? false;

  // Freemium: Threshold notification state
  bool _freemiumThresholdReached = false;
  int _freemiumRemainingSeconds = 0;
  bool _freemiumRequiresUserAction = false;

  bool get freemiumThresholdReached => _freemiumThresholdReached;
  int get freemiumRemainingSeconds => _freemiumRemainingSeconds;

  /// Whether user needs to take action (e.g., setup on-device STT)
  bool get freemiumRequiresUserAction => _freemiumRequiresUserAction;

  Timer? _reconnectTimer;
  int _reconnectCountdown = 5;
  int get reconnectCountdown => _reconnectCountdown;

  Timer? _recordingTimer;
  int _recordingDuration = 0; // in seconds

  int _getRecordingDuration() => _recordingDuration;

  List<MessageEvent> _transcriptionServiceStatuses = [];
  List<MessageEvent> get transcriptionServiceStatuses => _transcriptionServiceStatuses;

  List<int> _systemAudioBuffer = [];
  bool _systemAudioCaching = true;
  Future<PhoneCaptureStartResult>? _micStartFuture;
  Future<bool>? _systemAudioStartFuture;
  ActiveWalAuthority? _systemAudioCaptureAuthority;
  Timer? _systemAudioCacheTimer;
  int _captureGeneration = 0;
  final ActiveAccountAuthorityProvider _activeAccountAuthority;
  final ActiveWalAuthorityProvider _activeWalAuthority;
  final CaptureGeolocationSender? _geolocationSender;
  final Set<Future<bool>> _captureGeolocationFutures = <Future<bool>>{};
  final CaptureConsentAuthorityEnsurer? _captureConsentAuthorityEnsurer;
  final DeviceCaptureStarter? _deviceCaptureStarter;
  final PhoneCaptureStarter? _phoneCaptureStarter;
  final Duration _captureAuthorityWaitTimeout;
  final Duration _captureAuthorityPollInterval;
  final InProgressConversationFetchCall _inProgressConversationFetch;
  final InProgressConversationProcessCall _inProgressConversationProcess;

  bool _isCaptureCurrent(int generation, ActiveWalAuthority authority) =>
      generation == _captureGeneration && authority.isCurrent();

  bool _isLoadingInProgressConversation = false;

  // BLE streaming metrics
  int _blesBytesReceived = 0;
  int _wsSocketBytesSent = 0;
  double _bleReceiveRateKbps = 0.0;
  double _wsSendRateKbps = 0.0;
  DateTime? _metricsLastCalculated;
  Timer? _metricsTimer;
  // Reference count for metrics listeners to handle multiple consumers safely.
  // Each widget that needs metrics calls addMetricsListener() in initState
  // and removeMetricsListener() in dispose. This prevents one widget's dispose
  // from disabling metrics for other widgets that still need them.
  int _metricsListenersCount = 0;

  double get bleReceiveRateKbps => _bleReceiveRateKbps;
  double get wsSendRateKbps => _wsSendRateKbps;

  @visibleForTesting
  bool get hasActiveKeepAliveTimerForTesting => _keepAliveTimer?.isActive ?? false;

  /// Call this in initState of a widget that needs BLE/WS metrics
  void addMetricsListener() {
    _metricsListenersCount++;
    if (_metricsListenersCount == 1) {
      notifyListeners(); // Initial update for the first listener
    }
  }

  /// Call this in dispose of a widget that uses BLE/WS metrics
  void removeMetricsListener() {
    if (_metricsListenersCount > 0) {
      _metricsListenersCount--;
    }
  }

  bool get _metricsNotifyEnabled => _metricsListenersCount > 0;

  /// Check if any segment has a personId not in local cache.
  /// Uses Set difference for O(N+M) complexity instead of O(N*M).
  bool _hasMissingPerson(List<TranscriptSegment> segments) {
    final cachedIds = SharedPreferencesUtil().cachedPeople.map((p) => p.id).toSet();
    final segmentPersonIds = segments.map((s) => s.personId).whereType<String>().toSet();
    return segmentPersonIds.difference(cachedIds).isNotEmpty;
  }

  CaptureProvider({
    ActiveAccountAuthorityProvider activeAccountAuthority = WalOwnerAuthority.activeAccount,
    ActiveWalAuthorityProvider? activeWalAuthority,
    CaptureGeolocationSender? geolocationSender,
    CaptureConsentAuthorityEnsurer? captureConsentAuthorityEnsurer,
    DeviceCaptureStarter? deviceCaptureStarter,
    PhoneCaptureStarter? phoneCaptureStarter,
    @visibleForTesting Duration captureAuthorityWaitTimeout = const Duration(seconds: 3),
    @visibleForTesting Duration captureAuthorityPollInterval = const Duration(milliseconds: 100),
    @visibleForTesting Duration deviceAudioFrameTimeout = const Duration(seconds: 5),
    InProgressConversationFetchCall inProgressConversationFetch = _fetchInProgressConversation,
    InProgressConversationProcessCall inProgressConversationProcess = _processInProgressConversation,
  })  : _activeAccountAuthority = activeAccountAuthority,
        _activeWalAuthority = activeWalAuthority ?? WalOwnerAuthority.active,
        _geolocationSender = geolocationSender,
        _captureConsentAuthorityEnsurer = captureConsentAuthorityEnsurer,
        _deviceCaptureStarter = deviceCaptureStarter,
        _phoneCaptureStarter = phoneCaptureStarter,
        _captureAuthorityWaitTimeout = captureAuthorityWaitTimeout,
        _captureAuthorityPollInterval = captureAuthorityPollInterval,
        _deviceAudioFrameTimeout = deviceAudioFrameTimeout,
        _inProgressConversationFetch = inProgressConversationFetch,
        _inProgressConversationProcess = inProgressConversationProcess {
    _accountIsolationProducerToken = EllaAccountIsolationService.registerCaptureProducer(stopForAccountTransition);
    _connectionStateListener = ConnectivityService().onConnectionChange.listen((bool isConnected) {
      onConnectionStateChanged(isConnected);
    });

    if (PlatformService.isDesktop) {
      _screenCaptureChannel = const MethodChannel('screenCapturePlatform');
      _controlBarChannel = const MethodChannel('com.omi/floating_control_bar');

      _initializeAppLifecycleListener();

      WidgetsBinding.instance.addPostFrameCallback((_) {
        _controlBarChannel.setMethodCallHandler(_handleFloatingControlBarMethodCall);
        ServiceManager.instance().systemAudio.setOnRecordingStartedFromNub(_handleRecordingStartedFromNub);
        ServiceManager.instance().systemAudio.setIsRecordingPausedCallback(() => _isPaused);
      });
    }
  }

  void _initializeAppLifecycleListener() {
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    super.didChangeAppLifecycleState(state);

    DebugLogManager.logEvent('app_lifecycle_changed', {
      'state': state.name,
      'recording_state': recordingState.name,
      'has_device': _recordingDevice != null,
      'socket_connected': _socket?.state == SocketServiceState.connected,
    });

    if (state == AppLifecycleState.resumed) {
      _handleAppResumed();
    }
  }

  void _handleAppResumed() async {
    if (!PlatformService.isDesktop || !_shouldAutoResumeAfterWake) return;

    try {
      final nativeRecording = await _screenCaptureChannel.invokeMethod('isRecording') ?? false;

      if (!nativeRecording && recordingState != RecordingState.stop) {
        updateRecordingState(RecordingState.stop);
        await _socket?.stop(reason: 'native recording stopped during sleep');
      }

      if (!nativeRecording && recordingState == RecordingState.stop) {
        await Future.delayed(const Duration(seconds: 2));
        await streamSystemAudioRecording();
      }
    } catch (e) {
      Logger.debug('[AutoRecord] Resume error: $e');
    }
  }

  void updateProviderInstances(ConversationProvider? cp, MessageProvider? mp, PeopleProvider? pp, UsageProvider? up) {
    conversationProvider = cp;
    messageProvider = mp;
    peopleProvider = pp;
    usageProvider = up;

    notifyListeners();
  }

  void reset() {
    _captureGeneration++;
    _conversation = null;
    segments = [];
    photos = [];
    _segmentsPhotosVersion = 0;
    suggestionsBySegmentId = {};
    taggingSegmentIds = [];
    hasTranscripts = false;
    _transcriptionServiceStatuses = [];
    _recordingDuration = 0;
    _freemiumThresholdReached = false;
    _freemiumRemainingSeconds = 0;
    _freemiumRequiresUserAction = false;
    notifyListeners();
  }

  BtDevice? _recordingDevice;

  String? _getConversationSourceFromDevice() {
    if (_recordingDevice == null) {
      return null;
    }
    switch (_recordingDevice!.type) {
      case DeviceType.friendPendant:
        return 'friend_com';
      case DeviceType.omi:
        return 'omi';
      case DeviceType.openglass:
        return 'openglass';
      case DeviceType.fieldy:
        return 'fieldy';
      case DeviceType.bee:
        return 'bee';
      case DeviceType.plaud:
        return 'plaud';
      case DeviceType.frame:
        return 'frame';
      case DeviceType.appleWatch:
        return 'apple_watch';
      case DeviceType.limitless:
        return 'limitless';
    }
  }

  ServerConversation? _conversation;
  List<TranscriptSegment> segments = [];
  List<ConversationPhoto> photos = [];
  // Version counter for segments/photos content changes. Incremented on in-place mutations
  // (e.g., translation updates, photo description changes) to signal UI rebuilds when
  // list length and last-text remain unchanged.
  int _segmentsPhotosVersion = 0;
  int get segmentsPhotosVersion => _segmentsPhotosVersion;
  Map<String, SpeakerLabelSuggestionEvent> suggestionsBySegmentId = {};
  List<String> taggingSegmentIds = [];

  bool hasTranscripts = false;

  StreamSubscription? _bleBytesStream;
  StreamSubscription? _blePhotoStream;
  final Duration _deviceAudioFrameTimeout;
  Timer? _deviceAudioFrameWatchdog;

  get bleBytesStream => _bleBytesStream;

  StreamSubscription? _bleButtonStream;
  DateTime? _voiceCommandSession;
  List<List<int>> _commandBytes = [];
  bool _isProcessingButtonEvent = false; // Guard to prevent overlapping button operations
  Timer? _voiceCommandTimeoutTimer; // 30s auto-end timer for voice questions
  bool _voiceSessionStartedByLegacyLongPress =
      false; // Track if session was started by legacy long press (3) vs new toggle (1), TODO: remove this flag later

  StreamSubscription? _storageStream;
  late final Object _accountIsolationProducerToken;

  get storageStream => _storageStream;

  RecordingState recordingState = RecordingState.stop;

  bool _isPaused = false;
  bool get isPaused => _isPaused;

  // Flag to star the conversation when it ends
  bool _starOngoingConversation = false;
  bool get isConversationMarkedForStarring => _starOngoingConversation;

  void markConversationForStarring() {
    _starOngoingConversation = true;
    notifyListeners();
  }

  void unmarkConversationForStarring() {
    _starOngoingConversation = false;
    notifyListeners();
  }

  // Session-based auto-resume flag
  // Always true on app start, set to false only when user manually stops/pauses
  bool _shouldAutoResumeAfterWake = true;
  bool get shouldAutoResumeAfterWake => _shouldAutoResumeAfterWake;

  bool _transcriptServiceReady = false;

  bool get transcriptServiceReady => _transcriptServiceReady && _isConnected;

  // having a connected device or using the phone's mic for recording
  bool get recordingDeviceServiceReady =>
      _recordingDevice != null ||
      recordingState == RecordingState.record ||
      recordingState == RecordingState.systemAudioRecord;

  bool get havingRecordingDevice => _recordingDevice != null;

  bool get hasCapturableContent =>
      segments.any((segment) => segment.text.trim().isNotEmpty) ||
      photos.any((photo) => !photo.discarded && photo.base64.trim().isNotEmpty);

  BtDevice? get recordingDevice => _recordingDevice;

  void setHasTranscripts(bool value) {
    hasTranscripts = value;
    notifyListeners();
  }

  void setConversationCreating(bool value) {
    Logger.debug('set Conversation creating $value');
    // ConversationCreating = value;
    notifyListeners();
  }

  void _updateRecordingDevice(BtDevice? device) {
    Logger.debug('connected device changed from ${_recordingDevice?.id} to ${device?.id}');
    _recordingDevice = device;
    notifyListeners();
  }

  void updateRecordingDevice(BtDevice? device) {
    _updateRecordingDevice(device);
  }

  /// Fails the active necklace capture closed when its physical BLE transport
  /// disconnects. Merely clearing [_recordingDevice] leaves the transcription
  /// socket and `deviceRecord` presentation alive, which can create empty
  /// conversations indefinitely after the necklace is powered off.
  Future<bool> handleRecordingDeviceDisconnected(String deviceId) async {
    if (_recordingDevice?.id != deviceId) return false;

    _captureGeneration++;
    _deviceAudioFrameWatchdog?.cancel();
    _deviceAudioFrameWatchdog = null;
    _keepAliveTimer?.cancel();
    _keepAliveTimer = null;

    final state = recordingState;
    _updateRecordingDevice(null);
    if (state == RecordingState.initialising || state == RecordingState.deviceRecord) {
      updateRecordingState(RecordingState.error);
    }

    await _closeBleStream(stopCamera: false);
    await _socket?.stop(reason: 'necklace transport disconnected');
    return true;
  }

  void _armDeviceAudioFrameWatchdog(String deviceId, int generation) {
    _deviceAudioFrameWatchdog?.cancel();
    _deviceAudioFrameWatchdog = Timer(_deviceAudioFrameTimeout, () {
      if (generation != _captureGeneration ||
          _recordingDevice?.id != deviceId ||
          recordingState != RecordingState.deviceRecord) {
        return;
      }
      Logger.error('Necklace capture stopped because BLE audio frames ceased');
      unawaited(handleRecordingDeviceDisconnected(deviceId));
    });
  }

  @visibleForTesting
  void armDeviceAudioFrameWatchdogForTesting() {
    final deviceId = _recordingDevice?.id;
    if (deviceId == null) return;
    _armDeviceAudioFrameWatchdog(deviceId, _captureGeneration);
  }

  Future _resetStateVariables() async {
    segments = [];
    photos = [];
    hasTranscripts = false;
    suggestionsBySegmentId = {};
    _conversation = null;
    taggingSegmentIds = [];
    notifyListeners();
  }

  Future<void> onRecordProfileSettingChanged() async {
    await _resetState();
  }

  /// Called when transcription settings are changed (e.g., custom STT provider)
  /// This resets the socket connection to use the new configuration
  Future<void> onTranscriptionSettingsChanged() async {
    Logger.debug("Transcription settings changed, refreshing socket connection...");

    _replacingTranscriptionSocket = true;
    try {
      // Handle device recording
      if (_recordingDevice != null) {
        await _socket?.stop(reason: 'transcription settings changed');
        BleAudioCodec codec = await _getAudioCodec(_recordingDevice!.id);
        await _initiateWebsocket(
          audioCodec: codec,
          force: true,
          source: _getConversationSourceFromDevice(),
        );
        return;
      }

      // Handle phone mic recording
      if (recordingState == RecordingState.record) {
        await _socket?.stop(reason: 'transcription settings changed');
        await _initiateWebsocket(
          audioCodec: BleAudioCodec.pcm16,
          sampleRate: 16000,
          force: true,
          source: ConversationSource.phone.name,
        );
        return;
      }

      // Handle system audio recording (desktop)
      if (recordingState == RecordingState.systemAudioRecord) {
        await _socket?.stop(reason: 'transcription settings changed');
        await _initiateWebsocket(
          audioCodec: BleAudioCodec.pcm16,
          sampleRate: 16000,
          force: true,
          source: ConversationSource.desktop.name,
        );
        return;
      }
    } finally {
      _replacingTranscriptionSocket = false;
    }
  }

  Future<void> changeAudioRecordProfile({
    required BleAudioCodec audioCodec,
    int? sampleRate,
    int? channels,
    bool? isPcm,
    String? source,
  }) async {
    await _resetState();
    await _initiateWebsocket(
        audioCodec: audioCodec, sampleRate: sampleRate, channels: channels, isPcm: isPcm, source: source);
  }

  Future<void> _initiateWebsocket({
    required BleAudioCodec audioCodec,
    int? sampleRate,
    int? channels,
    bool? isPcm,
    bool force = false,
    String? source,
  }) async {
    if (!SharedPreferencesUtil().aiConsentAccepted) {
      _transcriptServiceReady = false;
      return;
    }
    Logger.debug('initiateWebsocket in capture_provider');

    BleAudioCodec codec = audioCodec;
    sampleRate ??= mapCodecToSampleRate(codec);
    channels ??= (codec == BleAudioCodec.pcm16 || codec == BleAudioCodec.pcm8) ? 1 : 2;

    Logger.debug('is ws null: ${_socket == null}');
    Logger.debug('Initiating WebSocket with: codec=$codec, sampleRate=$sampleRate, channels=$channels, isPcm=$isPcm');

    // Get language and custom STT config
    String language =
        SharedPreferencesUtil().hasSetPrimaryLanguage ? SharedPreferencesUtil().userPrimaryLanguage : "multi";
    final customSttConfig = SharedPreferencesUtil().customSttConfig;

    Logger.debug('Custom STT enabled: ${customSttConfig.isEnabled}, provider: ${customSttConfig.provider}');

    // Check codec compatibility for custom STT - fallback to default if incompatible
    CustomSttConfig? effectiveConfig = customSttConfig.isEnabled ? customSttConfig : null;
    if (effectiveConfig != null && !TranscriptSocketServiceFactory.isCodecSupportedForCustomStt(codec)) {
      Logger.debug('[CustomSTT] Codec $codec not supported, falling back to Omi');
      effectiveConfig = null;
    }

    // Connect to the transcript socket
    _socket = await ServiceManager.instance().socket.conversation(
          codec: codec,
          sampleRate: sampleRate,
          language: language,
          force: force,
          source: source,
          customSttConfig: effectiveConfig,
        );
    if (_socket == null) {
      _startKeepAliveServices();
      Logger.debug("Can not create new conversation socket");
      return;
    }
    _socket?.subscribe(this, this);
    _transcriptServiceReady = true;

    unawaited(refreshInProgressConversations());

    notifyListeners();
  }

  void _processVoiceCommandBytes(String deviceId, List<List<int>> data) async {
    if (!SharedPreferencesUtil().aiConsentAccepted) return;
    if (data.isEmpty) {
      Logger.debug("voice frames is empty");
      return;
    }

    BleAudioCodec codec = await _getAudioCodec(_recordingDevice!.id);
    if (messageProvider != null) {
      await messageProvider?.sendVoiceMessageStreamToServer(
        data,
        onFirstChunkRecived: () {
          _playSpeakerHaptic(deviceId, 2);
        },
        codec: codec,
      );
    }
  }

  // Start a 15s timeout timer for voice commands - auto-ends if user forgets to tap again
  void _startVoiceCommandTimeout(String deviceId) {
    _voiceCommandTimeoutTimer?.cancel();
    _voiceCommandTimeoutTimer = Timer(const Duration(seconds: 15), () {
      debugPrint("Voice command timeout - auto-ending session after 15s");
      if (_voiceCommandSession != null) {
        _endVoiceCommandSession(deviceId);
      }
    });
  }

  // End voice command session and process the collected audio
  void _endVoiceCommandSession(String deviceId) {
    _voiceCommandTimeoutTimer?.cancel();
    _voiceCommandTimeoutTimer = null;
    _voiceCommandSession = null;
    _voiceSessionStartedByLegacyLongPress = false; // Reset flag
    var data = List<List<int>>.from(_commandBytes);
    _commandBytes = [];
    _processVoiceCommandBytes(deviceId, data);
  }

  Future streamButton(String deviceId) async {
    final generation = _captureGeneration;
    Logger.debug('streamButton in capture_provider');
    await _bleButtonStream?.cancel();
    final subscription = await _getBleButtonListener(deviceId, onButtonReceived: (List<int> value) {
      if (generation != _captureGeneration) return;
      final snapshot = List<int>.from(value);
      if (snapshot.isEmpty || snapshot.length < 4) return;
      var buttonState = ByteData.view(Uint8List.fromList(snapshot.sublist(0, 4).reversed.toList()).buffer).getUint32(0);
      Logger.debug("device button $buttonState");

      // double tap
      if (buttonState == 2) {
        Logger.debug("Double tap detected");

        // Guard: ignore if already processing a button event
        if (_isProcessingButtonEvent) {
          Logger.debug("Double tap: already processing, ignoring");
          return;
        }

        int doubleTapAction = SharedPreferencesUtil().doubleTapAction;

        if (doubleTapAction == 1) {
          // Pause/resume recording
          Logger.debug("Double tap: toggling pause/mute");
          _isProcessingButtonEvent = true;
          if (_isPaused) {
            MixpanelManager().omiDoubleTap(feature: 'unmute');
            resumeDeviceRecording().then((_) {
              _isProcessingButtonEvent = false;
            }).catchError((e) {
              Logger.debug("Error resuming device recording: $e");
              _isProcessingButtonEvent = false;
            });
          } else {
            MixpanelManager().omiDoubleTap(feature: 'mute');
            pauseDeviceRecording().then((_) {
              _isProcessingButtonEvent = false;
            }).catchError((e) {
              Logger.debug("Error pausing device recording: $e");
              _isProcessingButtonEvent = false;
            });
          }
        } else if (doubleTapAction == 2) {
          // Star ongoing conversation (doesn't end it)
          Logger.debug("Double tap: marking conversation for starring");
          if (!_starOngoingConversation) {
            markConversationForStarring();
            MixpanelManager().omiDoubleTap(feature: 'star_conversation');
            // Haptic feedback to confirm
            HapticFeedback.mediumImpact();
          } else {
            // Toggle off if already marked
            unmarkConversationForStarring();
            MixpanelManager().omiDoubleTap(feature: 'unstar_conversation');
            HapticFeedback.lightImpact();
          }
        } else {
          // End conversation and process (default)
          Logger.debug("Double tap: processing conversation");
          MixpanelManager().omiDoubleTap(feature: 'process_conversation');
          forceProcessingCurrentConversation();
        }
        return;
      }

      // Single tap (buttonState == 1) - toggle voice question mode
      // Tap once to start, tap again to end
      if (buttonState == 1) {
        debugPrint("Single tap detected");
        if (_voiceCommandSession == null) {
          // Start voice question session (new toggle mode)
          debugPrint("Starting voice question session (toggle mode)");
          _voiceCommandSession = DateTime.now();
          _commandBytes = [];
          _voiceSessionStartedByLegacyLongPress = false; // New toggle mode
          _startVoiceCommandTimeout(deviceId);
          _playSpeakerHaptic(deviceId, 1);
        } else if (!_voiceSessionStartedByLegacyLongPress) {
          // Only end on second tap if session was started by toggle mode (not legacy)
          debugPrint("Ending voice question session (toggle mode)");
          _endVoiceCommandSession(deviceId);
        }
        return;
      }

      // Legacy support: start long press (for voice commands) - older firmware
      if (buttonState == 3 && _voiceCommandSession == null) {
        debugPrint("Legacy: Long press start detected");
        _voiceCommandSession = DateTime.now();
        _commandBytes = [];
        _voiceSessionStartedByLegacyLongPress = true; // Legacy hold-to-talk mode
        _startVoiceCommandTimeout(deviceId);
        _playSpeakerHaptic(deviceId, 1);
      }

      // Legacy support: release (end voice command) - older firmware
      // Only end on release if session was started by legacy long press (buttonState 3)
      if (buttonState == 5 && _voiceCommandSession != null && _voiceSessionStartedByLegacyLongPress) {
        debugPrint("Legacy: Release detected - ending voice command");
        _endVoiceCommandSession(deviceId);
      }
    });
    if (generation != _captureGeneration) {
      await subscription?.cancel();
      return;
    }
    _bleButtonStream = subscription;
  }

  Future<StreamSubscription?> streamAudioToWs(
    String deviceId,
    BleAudioCodec codec, {
    DeviceCaptureStartProof? startProof,
  }) async {
    if (!SharedPreferencesUtil().aiConsentAccepted) {
      await _bleBytesStream?.cancel();
      _bleBytesStream = null;
      return null;
    }
    final generation = _captureGeneration;
    final captureAuthority = _activeWalAuthority();
    if (captureAuthority == null || !_isCaptureCurrent(generation, captureAuthority)) {
      _bleBytesStream = null;
      return null;
    }
    Logger.debug('streamAudioToWs in capture_provider');
    await _bleBytesStream?.cancel();
    _startMetricsTracking();
    final subscription = await _getBleAudioBytesListener(deviceId, onAudioBytesReceived: (List<int> value) {
      final snapshot = List<int>.from(value);
      if (snapshot.isEmpty || snapshot.length < 3) return;

      // Keep late frames bound to the capture-start authority. Unknown/stale
      // frames are retained for quarantine and never inherit the next account.
      if (!_isCaptureCurrent(generation, captureAuthority)) {
        _wal.getSyncs().phone.onByteStream(snapshot, ownerAtCapture: captureAuthority.owner);
        return;
      }

      // Track bytes received from BLE
      _blesBytesReceived += snapshot.length;

      // Command button triggered
      bool voiceCommandSupported = _recordingDevice != null
          ? (_recordingDevice?.type == DeviceType.omi || _recordingDevice?.type == DeviceType.openglass)
          : false;
      if (_voiceCommandSession != null && voiceCommandSupported) {
        _commandBytes.add(snapshot.sublist(3));
      }

      // Local storage syncs
      var checkWalSupported =
          (_recordingDevice?.type == DeviceType.omi || _recordingDevice?.type == DeviceType.openglass) &&
              codec.isOpusSupported() &&
              (_socket?.state != SocketServiceState.connected || SharedPreferencesUtil().unlimitedLocalStorageEnabled);
      if (checkWalSupported != _isWalSupported) {
        setIsWalSupported(checkWalSupported);
      }
      if (_isWalSupported) {
        _wal.getSyncs().phone.onByteStream(snapshot, ownerAtCapture: captureAuthority.owner);
      }

      // Send WS
      if (_socket?.state == SocketServiceState.connected) {
        if (!SharedPreferencesUtil().aiConsentAccepted) return;
        final paddingLeft =
            (_recordingDevice?.type == DeviceType.omi || _recordingDevice?.type == DeviceType.openglass) ? 3 : 0;
        final trimmedValue = paddingLeft > 0 ? value.sublist(paddingLeft) : value;
        _socket?.send(trimmedValue);
        startProof?.acceptTransmittedFrame(trimmedValue);
        _armDeviceAudioFrameWatchdog(deviceId, generation);

        // Track bytes sent to websocket
        _wsSocketBytesSent += trimmedValue.length;

        // Mark as synced
        if (_isWalSupported) {
          _wal.getSyncs().phone.onBytesSync(value);
        }
      }
    });
    if (!_isCaptureCurrent(generation, captureAuthority)) {
      await subscription?.cancel();
      return null;
    }
    _bleBytesStream = subscription;
    notifyListeners();
    return subscription;
  }

  Future<void> _resetState() async {
    Logger.debug('resetState');
    await _cleanupCurrentState();

    // Always try to stream audio if a device is present
    await _ensureDeviceSocketConnection();
    await _initiateDeviceAudioStreaming();

    // Additionally, stream photos if the device supports it
    if (_recordingDevice != null) {
      var connection = await ServiceManager.instance().device.ensureConnection(_recordingDevice!.id);
      if (connection != null && await connection.hasPhotoStreamingCharacteristic()) {
        await _initiateDevicePhotoStreaming();
      }
    }

    notifyListeners();
  }

  Future<bool> _startDeviceCaptureTransport() async {
    await _resetState();
    return recordingState == RecordingState.deviceRecord;
  }

  Future _cleanupCurrentState() async {
    await _closeBleStream();
    notifyListeners();
  }

  Future<BleAudioCodec> _getAudioCodec(String deviceId) async {
    var connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null) {
      return BleAudioCodec.pcm8;
    }
    return connection.getAudioCodec();
  }

  Future<bool> _playSpeakerHaptic(String deviceId, int level) async {
    var connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null) {
      return false;
    }
    return connection.performPlayToSpeakerHaptic(level);
  }

  Future<StreamSubscription?> _getBleAudioBytesListener(
    String deviceId, {
    required void Function(List<int>) onAudioBytesReceived,
  }) async {
    var connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null) {
      return Future.value(null);
    }
    return connection.getBleAudioBytesListener(onAudioBytesReceived: onAudioBytesReceived);
  }

  Future<StreamSubscription?> _getBleButtonListener(
    String deviceId, {
    required void Function(List<int>) onButtonReceived,
  }) async {
    var connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null) {
      return Future.value(null);
    }
    return connection.getBleButtonListener(onButtonReceived: onButtonReceived);
  }

  Future<void> _ensureDeviceSocketConnection() async {
    if (_recordingDevice == null) {
      return;
    }
    BleAudioCodec codec = await _getAudioCodec(_recordingDevice!.id);
    var language =
        SharedPreferencesUtil().hasSetPrimaryLanguage ? SharedPreferencesUtil().userPrimaryLanguage : "multi";
    final customSttConfig = SharedPreferencesUtil().customSttConfig;
    final sttConfigId = customSttConfig.sttConfigId;

    if (language != _socket?.language ||
        codec != _socket?.codec ||
        _socket?.state != SocketServiceState.connected ||
        _socket?.sttConfigId != sttConfigId) {
      await _initiateWebsocket(audioCodec: codec, force: true, source: _getConversationSourceFromDevice());
    }
  }

  Future<bool> _initiateDeviceAudioStreaming() async {
    final device = _recordingDevice;
    if (device == null) {
      return false;
    }
    final deviceId = device.id;
    if (deviceId.isEmpty) {
      return false;
    }
    final generation = _captureGeneration;
    final captureAuthority = _activeWalAuthority();
    if (captureAuthority == null || !_isCaptureCurrent(generation, captureAuthority)) {
      return false;
    }
    final connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null || !_isCaptureCurrent(generation, captureAuthority)) {
      return false;
    }
    final codec = await _getAudioCodec(deviceId);
    await _wal.getSyncs().phone.onAudioCodecChanged(codec);

    // Set device info for WAL creation
    final pd = await device.getDeviceInfo(connection);
    final deviceModel = pd.modelNumber.isNotEmpty ? pd.modelNumber : "Omi";
    _wal.getSyncs().phone.setDeviceInfo(deviceId, deviceModel);

    await streamButton(deviceId);
    final startProof = DeviceCaptureStartProof();
    final subscription = await streamAudioToWs(deviceId, codec, startProof: startProof);
    if (subscription == null || !_isCaptureCurrent(generation, captureAuthority)) {
      return false;
    }
    try {
      await startProof.waitForTransmittedAudio();
    } catch (error) {
      Logger.error('Necklace did not transmit an audio frame to transcription: $error');
      await subscription.cancel();
      if (identical(_bleBytesStream, subscription)) {
        _bleBytesStream = null;
      }
      return false;
    }
    if (!_isCaptureCurrent(generation, captureAuthority) || _socket?.state != SocketServiceState.connected) {
      await subscription.cancel();
      if (identical(_bleBytesStream, subscription)) {
        _bleBytesStream = null;
      }
      return false;
    }

    // Recording is visible only after a non-empty frame reaches the live
    // transcription socket under the same exact account/capture authority.
    updateRecordingState(RecordingState.deviceRecord);
    notifyListeners();
    return true;
  }

  Future<void> _initiateDevicePhotoStreaming() async {
    if (!SharedPreferencesUtil().aiConsentAccepted) return;
    if (_recordingDevice == null) return;
    final generation = _captureGeneration;
    final captureAuthority = _activeWalAuthority();
    if (captureAuthority == null || !_isCaptureCurrent(generation, captureAuthority)) return;
    final deviceId = _recordingDevice!.id;
    var connection = await ServiceManager.instance().device.ensureConnection(deviceId);
    if (connection == null || !_isCaptureCurrent(generation, captureAuthority)) return;

    await connection.performCameraStartPhotoController();
    if (!_isCaptureCurrent(generation, captureAuthority)) return;
    final subscription = await connection.performGetImageListener(onImageReceived: (orientedImage) async {
      if (!_isCaptureCurrent(generation, captureAuthority)) return;
      final rotatedImageBytes = rotateImage(orientedImage);
      final String tempId = 'temp_img_${DateTime.now().millisecondsSinceEpoch}';
      final String base64Image = base64Encode(rotatedImageBytes);

      // Add placeholder to UI for immediate feedback
      photos.add(ConversationPhoto(id: tempId, base64: base64Image, createdAt: DateTime.now()));
      photos = List.from(photos);
      notifyListeners();

      // Chunking Logic
      const int chunkSize = 8192; // 8KB chunks
      final totalChunks = (base64Image.length / chunkSize).ceil();

      for (int i = 0; i < totalChunks; i++) {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        final start = i * chunkSize;
        final end = (start + chunkSize > base64Image.length) ? base64Image.length : start + chunkSize;
        final chunk = base64Image.substring(start, end);

        final payload = jsonEncode({
          'type': 'image_chunk',
          'id': tempId,
          'index': i,
          'total': totalChunks,
          'data': chunk,
        });

        if (_socket?.state == SocketServiceState.connected) {
          _socket?.send(payload); // Send the JSON string
        }
        await Future.delayed(const Duration(milliseconds: 20)); // Small delay to prevent flooding
      }
    });
    if (!_isCaptureCurrent(generation, captureAuthority)) {
      await subscription?.cancel();
      return;
    }
    _blePhotoStream = subscription;
    notifyListeners();
  }

  void clearTranscripts() {
    segments = [];
    hasTranscripts = false;
    notifyListeners();
  }

  void _startMetricsTracking() {
    _blesBytesReceived = 0;
    _wsSocketBytesSent = 0;
    _bleReceiveRateKbps = 0.0;
    _wsSendRateKbps = 0.0;
    _metricsLastCalculated = DateTime.now();

    _metricsTimer?.cancel();
    _metricsTimer = Timer.periodic(const Duration(seconds: 5), (timer) {
      _calculateMetricsRates();
    });
  }

  void _calculateMetricsRates() {
    final now = DateTime.now();
    if (_metricsLastCalculated == null) {
      _metricsLastCalculated = now;
      return;
    }

    final elapsedSeconds = now.difference(_metricsLastCalculated!).inMilliseconds / 1000.0;
    if (elapsedSeconds > 0) {
      // Calculate kbps (kilobits per second)
      _bleReceiveRateKbps = (_blesBytesReceived * 8) / (elapsedSeconds * 1000);
      _wsSendRateKbps = (_wsSocketBytesSent * 8) / (elapsedSeconds * 1000);

      // Reset counters for next interval
      _blesBytesReceived = 0;
      _wsSocketBytesSent = 0;
      _metricsLastCalculated = now;

      // Only notify listeners when UI actually needs these metrics to reduce battery drain
      if (_metricsNotifyEnabled) {
        notifyListeners();
      }
    }
  }

  void _stopMetricsTracking() {
    _metricsTimer?.cancel();
    _metricsTimer = null;
    _blesBytesReceived = 0;
    _wsSocketBytesSent = 0;
    _bleReceiveRateKbps = 0.0;
    _wsSendRateKbps = 0.0;
    _metricsLastCalculated = null;
    notifyListeners();
  }

  /// Triggers a metrics calculation for testing.
  /// This allows verifying that notifyListeners is gated by _metricsNotifyEnabled.
  @visibleForTesting
  void calculateMetricsForTesting() {
    // Initialize metrics tracking state if not already done
    _metricsLastCalculated ??= DateTime.now().subtract(const Duration(seconds: 10));
    _calculateMetricsRates();
  }

  Future<void> _closeBleStream({bool stopCamera = true}) async {
    _deviceAudioFrameWatchdog?.cancel();
    _deviceAudioFrameWatchdog = null;
    final bytesStream = _bleBytesStream;
    final photoStream = _blePhotoStream;
    final buttonStream = _bleButtonStream;
    _bleBytesStream = null;
    _blePhotoStream = null;
    _bleButtonStream = null;
    await bytesStream?.cancel();
    await photoStream?.cancel();
    await buttonStream?.cancel();
    _stopMetricsTracking();
    if (stopCamera && _recordingDevice != null) {
      var connection = await ServiceManager.instance().device.ensureConnection(_recordingDevice!.id);
      if (connection != null && await connection.hasPhotoStreamingCharacteristic()) {
        await connection.performCameraStopPhotoController();
      }
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _captureGeneration++;
    EllaAccountIsolationService.unregisterCaptureProducer(_accountIsolationProducerToken);
    _bleBytesStream?.cancel();
    _blePhotoStream?.cancel();
    _socket?.unsubscribe(this);
    _keepAliveTimer?.cancel();
    _connectionStateListener?.cancel();
    _recordingTimer?.cancel();
    _metricsTimer?.cancel();
    _deviceAudioFrameWatchdog?.cancel();
    _peopleRefreshFuture = null; // Clear in-flight tracker

    // Remove lifecycle observer
    if (PlatformService.isDesktop) {
      WidgetsBinding.instance.removeObserver(this);
    }

    super.dispose();
  }

  void updateRecordingState(RecordingState state) {
    recordingState = state;
    notifyListeners();
    _broadcastRecordingState();
  }

  /// Sends current geolocation to backend if location services are enabled and permission is granted.
  /// Every protected await is fenced to the exact capture operation that
  /// produced the recording proof.
  Future<bool> _sendCurrentGeolocation({
    required String expectedAuthenticatedUid,
    required ExactAccountAuthorityVerifier exactAuthority,
  }) async {
    bool isCurrent() =>
        expectedAuthenticatedUid.isNotEmpty &&
        exactAuthority.uid == expectedAuthenticatedUid &&
        exactAuthority.isExactCurrent();

    try {
      if (!isCurrent()) return false;
      if (!await Geolocator.isLocationServiceEnabled()) {
        Logger.log('Location service is not enabled, skipping geolocation update');
        return false;
      }
      if (!isCurrent()) return false;

      final permission = await Geolocator.checkPermission();
      if (!isCurrent()) return false;
      if (permission == LocationPermission.denied || permission == LocationPermission.deniedForever) {
        Logger.log('Location permission not granted, skipping geolocation update');
        return false;
      }

      final position = await Geolocator.getCurrentPosition();
      if (!isCurrent()) return false;
      final geolocation = Geolocation(
        latitude: position.latitude,
        longitude: position.longitude,
        altitude: position.altitude,
        accuracy: position.accuracy,
        time: position.timestamp.toUtc(),
      );

      final accepted = await updateUserGeolocation(
        geolocation: geolocation,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
      );
      return accepted && isCurrent();
    } catch (e) {
      Logger.error('Error sending geolocation: $e');
      return false;
    }
  }

  Future<bool> _sendCaptureGeolocation({
    required String expectedAuthenticatedUid,
    required ExactAccountAuthorityVerifier exactAuthority,
  }) async {
    if (expectedAuthenticatedUid.isEmpty ||
        exactAuthority.uid != expectedAuthenticatedUid ||
        !exactAuthority.isExactCurrent()) {
      return false;
    }
    final accepted = await (_geolocationSender?.call(
          expectedAuthenticatedUid: expectedAuthenticatedUid,
          exactAuthority: exactAuthority,
        ) ??
        _sendCurrentGeolocation(
          expectedAuthenticatedUid: expectedAuthenticatedUid,
          exactAuthority: exactAuthority,
        ));
    return accepted && exactAuthority.isExactCurrent();
  }

  Future<bool> _queueCaptureGeolocation(int generation, ActiveWalAuthority captureAuthority) {
    final operation = CaptureGeolocationOperation(
      captureAuthority: captureAuthority,
      captureGeneration: generation,
      currentCaptureGeneration: () => _captureGeneration,
    );
    late final Future<bool> tracked;
    tracked = _sendCaptureGeolocation(
      expectedAuthenticatedUid: operation.uid,
      exactAuthority: operation,
    ).catchError((Object error, StackTrace stackTrace) {
      Logger.debug('Capture geolocation was rejected: $error');
      return false;
    }).whenComplete(() => _captureGeolocationFutures.remove(tracked));
    _captureGeolocationFutures.add(tracked);
    return tracked;
  }

  @visibleForTesting
  Future<List<bool>> waitForCaptureGeolocationForTesting() =>
      Future.wait(_captureGeolocationFutures.toList(growable: false));

  Future<bool> _ensureCurrentCaptureConsentAuthority() =>
      _captureConsentAuthorityEnsurer?.call() ??
      ensureCaptureConsentAuthority(
        hasCurrentConsent: () => SharedPreferencesUtil().aiConsentAccepted,
        authenticatedUid: () => WalOwnerAuthority.authenticatedUid,
        persistedConsentReceiptId: () => SharedPreferencesUtil().persistedAiConsentReceiptIdForCurrentAccount,
        refreshAuthority: (uid) => EllaAiConsentService().refreshServerAuthority(uid: uid),
      );

  Future<PhoneCaptureStartResult> streamRecording() {
    final activeStart = _micStartFuture;
    if (activeStart != null) return activeStart;

    late final Future<PhoneCaptureStartResult> trackedStart;
    trackedStart = _streamRecording().whenComplete(() {
      if (identical(_micStartFuture, trackedStart)) _micStartFuture = null;
    });
    _micStartFuture = trackedStart;
    return trackedStart;
  }

  Future<PhoneCaptureStartResult> _streamRecording() async {
    final generation = _captureGeneration;
    final consentCurrent = await _ensureCurrentCaptureConsentAuthority();
    if (!consentCurrent || generation != _captureGeneration) return PhoneCaptureStartResult.consentUnavailable;
    final captureAuthority = await _waitForCaptureAuthority(generation);
    if (generation != _captureGeneration) return PhoneCaptureStartResult.cancelled;
    if (captureAuthority == null) return PhoneCaptureStartResult.accountNotReady;
    final phoneCaptureStarter = _phoneCaptureStarter;
    if (phoneCaptureStarter != null) {
      final result = await phoneCaptureStarter();
      if (!_isCaptureCurrent(generation, captureAuthority)) return PhoneCaptureStartResult.cancelled;
      if (result == PhoneCaptureStartResult.started) {
        updateRecordingState(RecordingState.record);
        unawaited(_queueCaptureGeolocation(generation, captureAuthority));
      } else {
        updateRecordingState(RecordingState.stop);
      }
      return result;
    }
    updateRecordingState(RecordingState.initialising);
    PermissionStatus microphonePermission;
    try {
      microphonePermission = await Permission.microphone.request();
    } catch (error) {
      Logger.error('Phone microphone permission could not be checked: $error');
      updateRecordingState(RecordingState.stop);
      return PhoneCaptureStartResult.recorderUnavailable;
    }
    if (!_isCaptureCurrent(generation, captureAuthority)) {
      updateRecordingState(RecordingState.stop);
      return PhoneCaptureStartResult.cancelled;
    }
    if (!microphonePermission.isGranted) {
      updateRecordingState(RecordingState.stop);
      return PhoneCaptureStartResult.microphonePermissionDenied;
    }

    // prepare
    try {
      await changeAudioRecordProfile(audioCodec: BleAudioCodec.pcm16, sampleRate: 16000);
    } catch (error) {
      Logger.error('Phone transcription could not start: $error');
      _transcriptServiceReady = false;
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'phone capture transcript unavailable');
      return PhoneCaptureStartResult.transcriptionUnavailable;
    }
    if (!_isCaptureCurrent(generation, captureAuthority)) {
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'phone capture authority changed');
      return PhoneCaptureStartResult.cancelled;
    }
    if (_socket == null || _socket?.state != SocketServiceState.connected) {
      _transcriptServiceReady = false;
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'phone capture transcript unavailable');
      return PhoneCaptureStartResult.transcriptionUnavailable;
    }

    // A native start receipt proves the recorder is physically running. The
    // first non-empty frame additionally proves that audio reached this
    // account-scoped transcription boundary before Home reports success.
    final startProof = PhoneCaptureStartProof();
    try {
      await ServiceManager.instance().mic.start(onByteReceived: (bytes) {
        if (_isCaptureCurrent(generation, captureAuthority) &&
            _socket?.state == SocketServiceState.connected &&
            startProof.acceptFrame(bytes)) {
          _socket?.send(bytes);
        }
      }, onRecording: () {
        // The isolate receipt is authoritative for physical start. Keep the UI
        // initialising until the first frame also reaches transcription.
      }, onStop: () {
        if (_isCaptureCurrent(generation, captureAuthority)) updateRecordingState(RecordingState.stop);
      }, onInitializing: () {
        if (_isCaptureCurrent(generation, captureAuthority)) updateRecordingState(RecordingState.initialising);
      });
      await startProof.waitForAudio();
    } catch (error) {
      Logger.error('Phone microphone did not transmit an audio frame to transcription: $error');
      updateRecordingState(RecordingState.stop);
      try {
        await ServiceManager.instance().mic.stop();
      } catch (stopError) {
        Logger.error('Phone microphone cleanup failed after start failure: $stopError');
      }
      await _socket?.stop(reason: 'phone recorder did not produce audio');
      return PhoneCaptureStartResult.recorderUnavailable;
    }
    if (!_isCaptureCurrent(generation, captureAuthority)) {
      await ServiceManager.instance().mic.stop();
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'phone capture authority changed');
      return PhoneCaptureStartResult.cancelled;
    }
    if (_socket == null || _socket?.state != SocketServiceState.connected) {
      await ServiceManager.instance().mic.stop();
      _transcriptServiceReady = false;
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'phone capture transcript disconnected');
      return PhoneCaptureStartResult.transcriptionUnavailable;
    }
    updateRecordingState(RecordingState.record);
    // Capture has actually started; do not send location for failed attempts.
    unawaited(_queueCaptureGeolocation(generation, captureAuthority));
    return PhoneCaptureStartResult.started;
  }

  Future<ActiveWalAuthority?> _waitForCaptureAuthority(int generation) async {
    final deadline = DateTime.now().add(_captureAuthorityWaitTimeout);
    while (generation == _captureGeneration) {
      final authority = _activeWalAuthority();
      if (authority != null && _isCaptureCurrent(generation, authority)) return authority;
      if (!DateTime.now().isBefore(deadline)) return null;
      await Future<void>.delayed(_captureAuthorityPollInterval);
    }
    return null;
  }

  stopStreamRecording() async {
    await _cleanupCurrentState();
    await ServiceManager.instance().mic.stop();
    updateRecordingState(RecordingState.stop);
    await _socket?.stop(reason: 'stop stream recording');
  }

  Future<void> stopForAccountTransition() async {
    _captureGeneration++;
    final pendingGeolocation = _captureGeolocationFutures.toList(growable: false);
    final pendingGeolocationStops = pendingGeolocation.map(
      (future) => future.timeout(const Duration(seconds: 5), onTimeout: () => false).then<void>((_) {}),
    );
    final micStart = _micStartFuture;
    final systemAudioStart = _systemAudioStartFuture;
    _voiceCommandTimeoutTimer?.cancel();
    _voiceCommandTimeoutTimer = null;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _keepAliveTimer?.cancel();
    _keepAliveTimer = null;
    _recordingTimer?.cancel();
    _recordingTimer = null;
    _systemAudioCacheTimer?.cancel();
    _systemAudioCacheTimer = null;
    _commandBytes = [];
    _voiceCommandSession = null;
    _systemAudioBuffer.clear();
    await _closeBleStream(stopCamera: false);
    if (ServiceManager.isInitialized) {
      final stops = <Future<void>>[ServiceManager.instance().mic.stop()];
      if (PlatformService.isDesktop) {
        stops.add(ServiceManager.instance().systemAudio.stopAndClearCallbacks());
      }
      await Future.wait([
        if (micStart != null) micStart,
        if (systemAudioStart != null) systemAudioStart.then<void>((_) {}),
        ...pendingGeolocationStops,
        ...stops,
      ]);
    } else {
      await Future.wait(pendingGeolocationStops);
    }
    _systemAudioCaptureAuthority = null;
    await _socket?.stop(reason: 'account transition');
    reset();
    updateRecordingState(RecordingState.stop);
  }

  Future streamDeviceRecording({BtDevice? device}) async {
    final generation = _captureGeneration;
    if (recordingState == RecordingState.error) {
      _keepAliveTimer?.cancel();
      _keepAliveTimer = null;
      await _closeBleStream();
      await _socket?.stop(reason: 'retry necklace capture after transport failure');
      if (generation != _captureGeneration) return;
      updateRecordingState(RecordingState.stop);
    }
    final consentCurrent = await _ensureCurrentCaptureConsentAuthority();
    if (!consentCurrent || generation != _captureGeneration) {
      if (generation == _captureGeneration) {
        updateRecordingState(RecordingState.error);
      }
      return;
    }
    final captureAuthority = _activeWalAuthority();
    if (captureAuthority == null || !_isCaptureCurrent(generation, captureAuthority)) {
      if (generation == _captureGeneration) updateRecordingState(RecordingState.error);
      return;
    }
    Logger.debug("streamDeviceRecording $device");
    if (device != null) _updateRecordingDevice(device);
    updateRecordingState(RecordingState.initialising);

    bool wasPaused = _isPaused;

    await _resetStateVariables();
    final started = await (_deviceCaptureStarter?.call() ?? _startDeviceCaptureTransport());
    if (!_isCaptureCurrent(generation, captureAuthority)) return;
    if (!started || recordingState != RecordingState.deviceRecord) {
      updateRecordingState(RecordingState.error);
      await _socket?.stop(reason: 'necklace recorder did not produce audio');
      return;
    }

    // Location is protected capture context. Emit it only after a non-empty
    // necklace frame reached a connected transcription socket under the same
    // exact account/capture authority.
    unawaited(_queueCaptureGeolocation(generation, captureAuthority));

    if (wasPaused) {
      await pauseDeviceRecording();
    }
  }

  Future stopStreamDeviceRecording({bool cleanDevice = false}) async {
    await _cleanupCurrentState();
    if (cleanDevice) {
      _updateRecordingDevice(null);
    }
    updateRecordingState(RecordingState.stop);
    await _socket?.stop(reason: 'stop stream device recording');
  }

  Future<bool> streamSystemAudioRecording() {
    final activeStart = _systemAudioStartFuture;
    if (activeStart != null) return activeStart;

    late final Future<bool> trackedStart;
    trackedStart = _streamSystemAudioRecording().whenComplete(() {
      if (identical(_systemAudioStartFuture, trackedStart)) {
        _systemAudioStartFuture = null;
      }
    });
    _systemAudioStartFuture = trackedStart;
    return trackedStart;
  }

  Future<bool> _streamSystemAudioRecording() async {
    if (!PlatformService.isDesktop) {
      notifyError('System audio recording is only available on macOS and Windows.');
      return false;
    }

    final generation = _captureGeneration;
    if (!SharedPreferencesUtil().aiConsentAccepted) {
      final context = MyApp.navigatorKey.currentContext;
      if (context == null || !await AiConsentCoordinator.ensure(context)) return false;
    }

    // User wants to record - enable auto-resume after wake
    _shouldAutoResumeAfterWake = true;

    updateRecordingState(RecordingState.initialising);

    _systemAudioBuffer = [];
    _systemAudioCaching = true;
    _systemAudioCaptureAuthority = _activeWalAuthority();
    final captureAuthority = _systemAudioCaptureAuthority;
    if (captureAuthority == null || !_isCaptureCurrent(generation, captureAuthority)) return false;
    _systemAudioCacheTimer?.cancel();
    _systemAudioCacheTimer = Timer(const Duration(seconds: 3), () {
      if (!_isCaptureCurrent(generation, captureAuthority)) return;
      _systemAudioCaching = false;
      _flushSystemAudioBuffer();
    });

    bool permissionsGranted = await _checkAndRequestSystemAudioPermissions();
    if (permissionsGranted && _isCaptureCurrent(generation, captureAuthority)) {
      await _startSystemAudioCapture(generation, captureAuthority);
      return _isCaptureCurrent(generation, captureAuthority) && recordingState == RecordingState.systemAudioRecord;
    } else {
      updateRecordingState(RecordingState.stop);
      return false;
    }
  }

  Future<void> _startSystemAudioCapture(int generation, ActiveWalAuthority captureAuthority) async {
    await changeAudioRecordProfile(audioCodec: BleAudioCodec.pcm16, sampleRate: 16000);
    if (!_isCaptureCurrent(generation, captureAuthority)) return;

    await ServiceManager.instance().systemAudio.start(
      onFormatReceived: (Map<String, dynamic> format) async {
        // This callback is for information only, no action needed.
      },
      onByteReceived: (bytes) {
        if (_isCaptureCurrent(generation, captureAuthority)) _processSystemAudioByteReceived(bytes);
      },
      onRecording: () {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        updateRecordingState(RecordingState.systemAudioRecord);
        _startRecordingTimer();
        Logger.debug('System audio recording started successfully.');
      },
      onStop: () {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        if (_isPaused) {
          updateRecordingState(RecordingState.pause);
        } else {
          updateRecordingState(RecordingState.stop);
        }
        _socket?.stop(reason: 'system audio stream ended from native');
      },
      onError: (error) {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('System audio capture error: $error');
        AppSnackbar.showSnackbarError(MyApp.navigatorKey.currentContext?.l10n.captureRecordingError(error) ??
            'An error occurred during recording: $error');
        updateRecordingState(RecordingState.stop);
      },
      onSystemWillSleep: (wasRecording) {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('System will sleep - was recording: $wasRecording');
      },
      onSystemDidWake: (nativeIsRecording) async {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('[SystemWake] Native recording: $nativeIsRecording, Flutter state: $recordingState');

        if (!nativeIsRecording && recordingState == RecordingState.systemAudioRecord) {
          // Native stopped, sync Flutter state
          updateRecordingState(RecordingState.stop);

          // Auto-resume based on session flag (was recording before sleep?)
          if (_shouldAutoResumeAfterWake) {
            Logger.debug('[SystemWake] Auto-resuming recording (was recording before sleep)...');
            await Future.delayed(const Duration(seconds: 2));
            await streamSystemAudioRecording();
          } else {
            Logger.debug('[SystemWake] Not auto-resuming (user manually stopped)');
          }
        }
      },
      onScreenDidLock: (wasRecording) {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('Screen locked - was recording: $wasRecording');
      },
      onScreenDidUnlock: () {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('Screen unlocked');
      },
      onDisplaySetupInvalid: (reason) {
        if (!_isCaptureCurrent(generation, captureAuthority)) return;
        Logger.debug('Display setup invalid: $reason');
        if (recordingState == RecordingState.systemAudioRecord) {
          updateRecordingState(RecordingState.stop);
          AppSnackbar.showSnackbarError(
              MyApp.navigatorKey.currentContext?.l10n.captureRecordingStoppedDisplayIssue(reason) ??
                  'Recording stopped: $reason. You may need to reconnect external displays or restart recording.');
        }
      },
      onMicrophoneDeviceChanged: () {
        if (_isCaptureCurrent(generation, captureAuthority)) _onMicrophoneDeviceChanged();
      },
      onMicrophoneStatus: (deviceName, micLevel, systemAudioLevel) {
        if (_isCaptureCurrent(generation, captureAuthority)) {
          _onMicrophoneStatus(deviceName, micLevel, systemAudioLevel);
        }
      },
      onStoppedAutomatically: () {
        if (_isCaptureCurrent(generation, captureAuthority)) _handleRecordingStoppedAutomatically();
      },
    );
  }

  Future<bool> _checkAndRequestSystemAudioPermissions() async {
    final micStatus = await _screenCaptureChannel.invokeMethod('checkMicrophonePermission');

    if (micStatus != 'granted') {
      if (micStatus == 'undetermined' || micStatus == 'unavailable') {
        final granted = await _screenCaptureChannel.invokeMethod('requestMicrophonePermission');
        if (!granted) {
          AppSnackbar.showSnackbarError(MyApp.navigatorKey.currentContext?.l10n.captureMicrophonePermissionRequired ??
              'Microphone permission required');
          return false;
        }
      } else if (micStatus == 'denied') {
        AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.captureMicrophonePermissionInSystemPreferences ??
                'Grant microphone permission in System Preferences');
        return false;
      }
    }

    final screenStatus = await _screenCaptureChannel.invokeMethod('checkScreenCapturePermission');

    if (screenStatus != 'granted') {
      final granted = await _screenCaptureChannel.invokeMethod('requestScreenCapturePermission');
      if (!granted) {
        AppSnackbar.showSnackbarError(
            MyApp.navigatorKey.currentContext?.l10n.captureScreenRecordingPermissionRequired ??
                'Screen recording permission required');
        return false;
      }
    }
    return true;
  }

  Future<void> _onMicrophoneDeviceChanged() async {
    final nativeRecording = await _screenCaptureChannel.invokeMethod('isRecording') ?? false;
    if (!nativeRecording) return;

    _isAutoReconnecting = true;
    _reconnectCountdown = 5;
    notifyListeners();

    await pauseSystemAudioRecording(isAuto: true);

    _reconnectTimer?.cancel();
    _reconnectTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (_reconnectCountdown > 1) {
        _reconnectCountdown--;
        notifyListeners();
      } else {
        _reconnectTimer?.cancel();
        _reconnectTimer = null;
        if (_isAutoReconnecting) {
          resumeSystemAudioRecording().then((_) {
            _isAutoReconnecting = false;
            notifyListeners();
          });
        }
      }
    });
  }

  void _onMicrophoneStatus(String deviceName, double micLevel, double systemAudioLevel) {
    final bool needsUpdate = microphoneName != deviceName ||
        (microphoneLevel - micLevel).abs() > 0.001 ||
        (this.systemAudioLevel - systemAudioLevel).abs() > 0.001;

    if (needsUpdate) {
      microphoneName = deviceName;
      microphoneLevel = micLevel;
      this.systemAudioLevel = systemAudioLevel;
      notifyListeners();
    }
  }

  void _flushSystemAudioBuffer() {
    if (_systemAudioCaptureAuthority?.isCurrent() == true && _socket?.state == SocketServiceState.connected) {
      while (_systemAudioBuffer.length >= 320) {
        final chunk = _systemAudioBuffer.sublist(0, 320);
        _socket?.send(chunk);
        _systemAudioBuffer.removeRange(0, 320);
      }
    }
  }

  Future<void> stopSystemAudioRecording() async {
    if (!PlatformService.isDesktop) return;

    // User manually stopped - don't auto-resume after wake
    _shouldAutoResumeAfterWake = false;

    _isAutoReconnecting = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;

    await ServiceManager.instance().systemAudio.stop();
    _isPaused = false;
    _stopRecordingTimer();
    await _socket?.stop(reason: 'manual stop');
    await _cleanupCurrentState();

    // Tell native to reset recording source since user explicitly stopped
    _screenCaptureChannel.invokeMethod('resetRecordingSource');
  }

  Future<void> pauseSystemAudioRecording({bool isAuto = false}) async {
    if (!PlatformService.isDesktop) return;

    if (!isAuto) {
      // User manually paused - don't auto-resume after wake
      _shouldAutoResumeAfterWake = false;
      _isAutoReconnecting = false;
      _reconnectTimer?.cancel();
      _reconnectTimer = null;
    }

    ServiceManager.instance().systemAudio.stop();
    _isPaused = true;
    // Don't reset duration - just pause the timer
    _pauseRecordingTimer();
    notifyListeners();
    _broadcastRecordingState();
  }

  Future<void> resumeSystemAudioRecording() async {
    if (!PlatformService.isDesktop) return;

    // User wants to resume - enable auto-resume after wake
    _shouldAutoResumeAfterWake = true;
    _isPaused = false;

    // Preserve the current duration before starting
    final preservedDuration = _recordingDuration;
    await streamSystemAudioRecording();
    // Restore duration after streamSystemAudioRecording may have reset it
    _recordingDuration = preservedDuration;
    _broadcastRecordingState();
  }

  Future<void> _handleFloatingControlBarMethodCall(MethodCall call) async {
    if (!PlatformService.isDesktop) return;

    switch (call.method) {
      case 'togglePauseResume':
        if (isPaused) {
          await resumeSystemAudioRecording();
        } else if (recordingState == RecordingState.systemAudioRecord) {
          await pauseSystemAudioRecording();
        } else {
          await streamSystemAudioRecording();
        }
        break;
      case 'requestCurrentState':
        // Control bar is requesting current state (e.g., when it becomes visible)
        _broadcastRecordingState();
        break;
      default:
        Logger.debug('FloatingControlBarChannel: Unhandled method ${call.method}');
    }
  }

  Future<void> _handleRecordingStoppedAutomatically() async {
    Logger.debug('CaptureProvider: Recording stopped automatically (meeting ended)');
    // Don't auto-resume after this - meeting is over
    _shouldAutoResumeAfterWake = false;

    // Stop the Flutter-side recording state
    if (PlatformService.isDesktop) {
      _isAutoReconnecting = false;
      _reconnectTimer?.cancel();
      _reconnectTimer = null;
      _isPaused = false;
      _stopRecordingTimer();
      updateRecordingState(RecordingState.stop);
      await _socket?.stop(reason: 'meeting ended - auto stop');
      await _cleanupCurrentState();
    }

    await forceProcessingCurrentConversation();
  }

  Future<void> _handleRecordingStartedFromNub() async {
    Logger.debug('CaptureProvider: Recording started from nub - stopping any existing recording and starting fresh');

    // Reset all recording state to ensure clean start
    _isPaused = false;
    _stopRecordingTimer();

    // Stop any existing recording and CLEAR CALLBACKS immediately
    ServiceManager.instance().systemAudio.stopAndClearCallbacks();
    await _socket?.stop(reason: 'nub start - reset');

    // Reset state to stop and broadcast immediately so control bar shows correct state
    recordingState = RecordingState.stop;
    notifyListeners();
    _broadcastRecordingState();

    // Small delay to ensure native stop completes before starting new recording
    await Future.delayed(const Duration(milliseconds: 300));

    // Start fresh recording
    await streamSystemAudioRecording();
  }

  @override
  void onClosed([int? closeCode]) {
    _transcriptionServiceStatuses = [];
    _transcriptServiceReady = false;

    if (closeCode == 4002) {
      usageProvider?.markAsOutOfCreditsAndRefresh();
    }

    if (_replacingTranscriptionSocket) {
      notifyListeners();
      return;
    }
    if (_failActiveMobileCaptureAfterSocketLoss('transcription socket closed')) {
      return;
    }

    notifyListeners();
    if (recordingState == RecordingState.error) {
      _keepAliveTimer?.cancel();
      return;
    }
    if (!SharedPreferencesUtil().aiConsentAccepted) {
      _keepAliveTimer?.cancel();
      return;
    }
    _startKeepAliveServices();
  }

  void _startKeepAliveServices() {
    _keepAliveTimer?.cancel();
    if (recordingState == RecordingState.error) {
      _keepAliveTimer = null;
      return;
    }
    _keepAliveTimer = Timer.periodic(const Duration(seconds: 15), (t) async {
      if (recordingState == RecordingState.error) {
        t.cancel();
        return;
      }
      Logger.debug("[Provider] keep alive");
      // rate 1/15s
      if (_keepAliveLastExecutedAt != null &&
          DateTime.now().subtract(const Duration(seconds: 15)).isBefore(_keepAliveLastExecutedAt!)) {
        Logger.debug("[Provider] keep alive - hitting rate limits 1/15s");
        return;
      }

      _keepAliveLastExecutedAt = DateTime.now();
      if (!recordingDeviceServiceReady || _socket?.state == SocketServiceState.connected) {
        t.cancel();
        return;
      }

      if (_recordingDevice != null) {
        BleAudioCodec codec = await _getAudioCodec(_recordingDevice!.id);
        await _initiateWebsocket(audioCodec: codec, source: _getConversationSourceFromDevice());
        return;
      }
      if (recordingState == RecordingState.record) {
        await _initiateWebsocket(
            audioCodec: BleAudioCodec.pcm16, sampleRate: 16000, source: ConversationSource.phone.name);
        return;
      }
      if (recordingState == RecordingState.systemAudioRecord && PlatformService.isDesktop) {
        Logger.debug("System audio socket disconnected, reconnecting...");
        await _initiateWebsocket(
            audioCodec: BleAudioCodec.pcm16, sampleRate: 16000, source: ConversationSource.desktop.name);
        return;
      }
    });
  }

  @override
  void onError(Object err) {
    _transcriptionServiceStatuses = [];
    _transcriptServiceReady = false;

    if (err is AiConsentAuthorityLostException) {
      _keepAliveTimer?.cancel();
      _shouldAutoResumeAfterWake = false;
      ServiceManager.instance().mic.stop();
      if (recordingState == RecordingState.systemAudioRecord && PlatformService.isDesktop) {
        ServiceManager.instance().systemAudio.stopAndClearCallbacks();
      }
      unawaited(_closeBleStream());
      updateRecordingState(RecordingState.stop);
      AppSnackbar.showSnackbarError(MyApp.navigatorKey.currentContext?.l10n.aiConsentActiveAudioStopped ??
          'AI permission could not be verified. Recording stopped.');
      return;
    }

    if (_failActiveMobileCaptureAfterSocketLoss('transcription socket error: $err')) {
      return;
    }

    if (err.toString().contains('Failed to find any displays or windows to capture')) {
      if (recordingState == RecordingState.systemAudioRecord) {
        AppSnackbar.showSnackbarError(MyApp.navigatorKey.currentContext?.l10n.captureDisplayDetectionFailed ??
            'Display detection failed. Recording stopped.');
        updateRecordingState(RecordingState.stop);
      }
    }

    notifyListeners();
    if (recordingState == RecordingState.error) {
      _keepAliveTimer?.cancel();
      return;
    }
    _startKeepAliveServices();
  }

  bool _failActiveMobileCaptureAfterSocketLoss(String reason) {
    final state = recordingState;
    if (state != RecordingState.record && state != RecordingState.deviceRecord) {
      return false;
    }
    Logger.error('Capture stopped because $reason');
    _keepAliveTimer?.cancel();
    _keepAliveTimer = null;
    if (state == RecordingState.record && ServiceManager.isInitialized) {
      unawaited(ServiceManager.instance().mic.stop());
    }
    if (state == RecordingState.deviceRecord) {
      unawaited(_closeBleStream());
    }
    updateRecordingState(RecordingState.error);
    return true;
  }

  @override
  void onConnected() {
    _transcriptServiceReady = true;
    notifyListeners();
  }

  CaptureFinalizationOperation? _beginFinalizationOperation() {
    final accountLease = EllaAccountCommitBarrier.begin(
      authorityProvider: _activeAccountAuthority,
      onInvalidated: reset,
    );
    if (accountLease == null) return null;
    return CaptureFinalizationOperation(
      accountLease: accountLease,
      captureGeneration: _captureGeneration,
      currentCaptureGeneration: () => _captureGeneration,
    );
  }

  Future<bool> refreshInProgressConversations() async {
    final operation = _beginFinalizationOperation();
    if (operation == null) return false;
    try {
      return await _loadInProgressConversation(operation);
    } on ExactAccountAuthorityChangedException {
      return false;
    } finally {
      operation.close();
    }
  }

  /// Gives the transcription service a short, bounded window to publish its
  /// final segment after microphone/socket shutdown. Empty placeholder
  /// segments never qualify, so Home cannot create a blank processing memory.
  Future<bool> awaitFinalCapturableContent({
    int maxAttempts = 3,
    Duration retryDelay = const Duration(milliseconds: 250),
  }) async {
    final operation = _beginFinalizationOperation();
    if (operation == null) return false;
    try {
      return await _awaitFinalCapturableContent(
        operation,
        maxAttempts: maxAttempts,
        retryDelay: retryDelay,
      );
    } finally {
      operation.close();
    }
  }

  Future<bool> _awaitFinalCapturableContent(
    CaptureFinalizationOperation operation, {
    required int maxAttempts,
    required Duration retryDelay,
  }) async {
    for (var attempt = 0; attempt < maxAttempts; attempt++) {
      try {
        if (!await _loadInProgressConversation(operation)) return false;
      } on ExactAccountAuthorityChangedException {
        return false;
      } catch (_) {
        // A final refresh failure must not turn an empty capture into a blank
        // server memory. Keep the retry bounded and report no content.
      }
      if (!operation.isCurrent) return false;
      if (hasCapturableContent) return true;
      if (attempt + 1 < maxAttempts && retryDelay > Duration.zero) {
        await Future<void>.delayed(retryDelay);
        if (!operation.isCurrent) return false;
      }
    }
    return false;
  }

  Future<bool> _loadInProgressConversation(CaptureFinalizationOperation operation) async {
    if (!operation.isCurrent) return false;
    final convos = await _inProgressConversationFetch(
      expectedAuthenticatedUid: operation.uid,
      exactAuthority: operation,
    );
    if (!operation.isCurrent) return false;
    _conversation = convos.isNotEmpty ? convos.first : null;
    if (_conversation != null) {
      segments = _conversation!.transcriptSegments;
      // Merge server photos with locally-captured temp photos to avoid losing
      // photos that haven't been processed server-side yet.
      final serverPhotos = _conversation!.photos;
      final localTempPhotos = photos.where((p) => p.id.startsWith('temp_img_')).toList();
      final serverPhotoIds = serverPhotos.map((p) => p.id).toSet();
      // Keep local temp photos that aren't already on the server
      final mergedPhotos = List<ConversationPhoto>.from(serverPhotos);
      for (final local in localTempPhotos) {
        if (!serverPhotoIds.contains(local.id)) {
          mergedPhotos.add(local);
        }
      }
      photos = mergedPhotos;
    } else {
      segments = [];
      photos = [];
    }
    _segmentsPhotosVersion++; // Bump version so Selector rebuilds
    setHasTranscripts(segments.isNotEmpty);
    notifyListeners();
    return true;
  }

  @override
  void onMessageEventReceived(MessageEvent event) {
    if (event is ConversationProcessingStartedEvent) {
      conversationProvider!.addProcessingConversation(event.memory);
      _resetStateVariables();
      return;
    }

    if (event is ConversationEvent) {
      event.memory.isNew = true;
      conversationProvider!.removeProcessingConversation(event.memory.id);
      _processConversationCreated(event.memory, event.messages.cast<ServerMessage>());
      return;
    }

    if (event is LastConversationEvent) {
      _handleLastConvoEvent(event.memoryId);
      return;
    }

    if (event is SpeakerLabelSuggestionEvent) {
      _handleSpeakerLabelSuggestionEvent(event);
      return;
    }

    if (event is TranslationEvent) {
      _handleTranslationEvent(event.segments);
      return;
    }

    if (event is SegmentsDeletedEvent) {
      _handleSegmentsDeletedEvent(event);
      return;
    }

    if (event is MessageServiceStatusEvent) {
      // Handle freemium threshold event via status field
      if (event.status == 'freemium_threshold_reached') {
        // Parse as FreemiumThresholdReachedEvent for consistent handling
        final thresholdEvent = FreemiumThresholdReachedEvent.fromJson({
          'status_text': event.statusText,
        });
        _handleFreemiumThresholdReached(thresholdEvent);
        return;
      }

      _transcriptionServiceStatuses.add(event);
      _transcriptionServiceStatuses = List.from(_transcriptionServiceStatuses);
      notifyListeners();
      return;
    }

    if (event is FreemiumThresholdReachedEvent) {
      _handleFreemiumThresholdReached(event);
      return;
    }

    if (event is PhotoProcessingEvent) {
      final tempId = event.tempId;
      final permanentId = event.photoId;
      final photoIndex = photos.indexWhere((p) => p.id == tempId);
      if (photoIndex != -1) {
        photos[photoIndex].id = permanentId;
        _segmentsPhotosVersion++;
        notifyListeners();
      }
      return;
    }

    if (event is PhotoDescribedEvent) {
      final photoId = event.photoId;
      final description = event.description;
      final discarded = event.discarded;
      final photoIndex = photos.indexWhere((p) => p.id == photoId);
      if (photoIndex != -1) {
        photos[photoIndex].description = description;
        photos[photoIndex].discarded = discarded;
        _segmentsPhotosVersion++;
        notifyListeners();
      }
      return;
    }
  }

  /// Performs one authoritative final transcript read and processing request
  /// under the same account lease and capture generation.
  Future<bool> finalizeCurrentConversation({
    int maxTranscriptAttempts = 3,
    Duration transcriptRetryDelay = const Duration(milliseconds: 250),
  }) async {
    final operation = _beginFinalizationOperation();
    if (operation == null) return false;
    try {
      final hasContent = await _awaitFinalCapturableContent(
        operation,
        maxAttempts: maxTranscriptAttempts,
        retryDelay: transcriptRetryDelay,
      );
      if (!hasContent || !operation.isCurrent) return false;
      return await forceProcessingCurrentConversation(operation: operation);
    } finally {
      operation.close();
    }
  }

  Future<bool> forceProcessingCurrentConversation({CaptureFinalizationOperation? operation}) async {
    final activeOperation = operation ?? _beginFinalizationOperation();
    if (activeOperation == null) return false;
    final ownsOperation = operation == null;
    final conversations = conversationProvider;
    if (conversations == null) {
      if (ownsOperation) activeOperation.close();
      return false;
    }
    try {
      if (!activeOperation.isCurrent) return false;
      await _resetStateVariables();
      if (!activeOperation.isCurrent) return false;
      conversations.addProcessingConversation(
        ServerConversation(
          id: '0',
          createdAt: DateTime.now(),
          structured: Structured('', ''),
          status: ConversationStatus.processing,
        ),
      );
      final result = await _inProgressConversationProcess(
        expectedAuthenticatedUid: activeOperation.uid,
        exactAuthority: activeOperation,
      );
      if (!activeOperation.isCurrent) return false;
      if (result == null || result.conversation == null) {
        conversations.removeProcessingConversation('0');
        return false;
      }
      conversations.removeProcessingConversation('0');
      result.conversation!.isNew = true;
      return await _processConversationCreated(
        result.conversation,
        result.messages,
        operation: activeOperation,
      );
    } on ExactAccountAuthorityChangedException {
      return false;
    } finally {
      if (ownsOperation) activeOperation.close();
    }
  }

  Future<bool> _processConversationCreated(
    ServerConversation? conversation,
    List<ServerMessage> messages, {
    CaptureFinalizationOperation? operation,
  }) async {
    if (conversation == null || operation?.isCurrent == false) return false;

    // Star the conversation if it was marked for starring
    if (_starOngoingConversation) {
      Logger.debug("Conversation was marked for starring, applying star");
      conversation.starred = true;
      // Call API to star the conversation
      await setConversationStarred(
        conversation.id,
        true,
        expectedAuthenticatedUid: operation?.uid,
        exactAuthority: operation,
      );
      if (operation?.isCurrent == false) return false;
      _starOngoingConversation = false;
    }

    if (operation?.isCurrent == false) return false;
    conversationProvider?.upsertConversation(conversation);
    if (operation?.isCurrent == false) return false;
    MixpanelManager().conversationCreated(conversation);
    return true;
  }

  Future<void> _handleLastConvoEvent(String memoryId) async {
    bool conversationExists =
        conversationProvider?.conversations.any((conversation) => conversation.id == memoryId) ?? false;
    if (conversationExists) {
      return;
    }
    ServerConversation? conversation = await getConversationById(memoryId);
    if (conversation != null) {
      Logger.debug("Adding last conversation to conversations: $memoryId");
      conversationProvider?.upsertConversation(conversation);
    } else {
      Logger.debug("Failed to fetch last conversation: $memoryId");
    }
  }

  void _handleTranslationEvent(List<TranscriptSegment> translatedSegments) {
    try {
      if (translatedSegments.isEmpty) return;

      Logger.debug("Received ${translatedSegments.length} translated segments");

      // Update the segments with the translated ones
      var remainSegments = TranscriptSegment.updateSegments(segments, translatedSegments);
      if (remainSegments.isNotEmpty) {
        Logger.debug("Adding ${remainSegments.length} new translated segments");
      }

      _segmentsPhotosVersion++;
      notifyListeners();
    } catch (e) {
      Logger.debug("Error handling translation event: $e");
    }
  }

  void _handleSegmentsDeletedEvent(SegmentsDeletedEvent event) {
    if (event.segmentIds.isEmpty) return;

    segments.removeWhere((segment) => event.segmentIds.contains(segment.id));
    suggestionsBySegmentId.removeWhere((key, value) => event.segmentIds.contains(key));
    taggingSegmentIds.removeWhere((id) => event.segmentIds.contains(id));
    hasTranscripts = segments.isNotEmpty;
    _segmentsPhotosVersion++;
    notifyListeners();
  }

  void _handleSpeakerLabelSuggestionEvent(SpeakerLabelSuggestionEvent event) {
    // Tagging
    if (taggingSegmentIds.contains(event.segmentId)) {
      return;
    }
    // If segment already exists, check if it's assigned. If so, ignore suggestion.
    var segment = segments.firstWhereOrNull((s) => s.id == event.segmentId);
    if (segment != null && segment.id.isNotEmpty && (segment.personId != null || segment.isUser)) {
      return;
    }

    // Add backend-created person to local cache for UI display (backward compatibility)
    final isUser = event.personId == 'user';
    if (!isUser && event.personId.isNotEmpty && SharedPreferencesUtil().getPersonById(event.personId) == null) {
      SharedPreferencesUtil().addCachedPerson(
        Person(
          id: event.personId,
          name: event.personName,
          createdAt: DateTime.now(),
          updatedAt: DateTime.now(),
        ),
      );
    }

    // Auto-apply assignment if backend provided personId (speaker_auto_assign=enabled)
    if (event.personId.isNotEmpty) {
      for (var seg in segments) {
        if (seg.speakerId == event.speakerId) {
          seg.isUser = isUser;
          seg.personId = isUser ? null : event.personId;
        }
      }
      _segmentsPhotosVersion++; // Trigger UI rebuild after auto-apply
    }
    notifyListeners();
  }

  Future<void> assignSpeakerToConversation(
      int speakerId, String personId, String personName, List<String> segmentIds) async {
    if (segmentIds.isEmpty) return;

    taggingSegmentIds = List.from(segmentIds);
    notifyListeners();

    try {
      String finalPersonId = personId;

      // Create person if new (old app path - calls idempotent API)
      if (finalPersonId.isEmpty) {
        Person? newPerson = await peopleProvider?.createPersonProvider(personName);
        if (newPerson != null) {
          finalPersonId = newPerson.id;
        }
      }

      // Add person to local cache if not exists (backward compatibility for old apps)
      if (finalPersonId.isNotEmpty &&
          finalPersonId != 'user' &&
          SharedPreferencesUtil().getPersonById(finalPersonId) == null) {
        SharedPreferencesUtil().addCachedPerson(
          Person(
            id: finalPersonId,
            name: personName,
            createdAt: DateTime.now(),
            updatedAt: DateTime.now(),
          ),
        );
      }

      // Find conversation id
      if (_conversation == null) return;

      final isAssigningToUser = finalPersonId == 'user';

      // Update all segments with this speakerId for UI consistency
      for (var segment in segments) {
        if (segment.speakerId == speakerId) {
          segment.isUser = isAssigningToUser;
          segment.personId = isAssigningToUser ? null : finalPersonId;
        }
      }
      _segmentsPhotosVersion++; // Bump version so Selector rebuilds

      // Persist change
      await assignBulkConversationTranscriptSegments(
        _conversation!.id,
        segmentIds,
        isUser: isAssigningToUser,
        personId: isAssigningToUser ? null : finalPersonId,
      );

      // Notify backend session
      if (_socket?.state == SocketServiceState.connected) {
        final payload = jsonEncode({
          'type': 'speaker_assigned',
          'speaker_id': speakerId,
          'person_id': finalPersonId,
          'person_name': personName,
          'segment_ids': segmentIds,
        });
        _socket?.send(payload);
      }

      // Remove all suggestions for this speakerId
      suggestionsBySegmentId.removeWhere((key, value) => value.speakerId == speakerId);
    } finally {
      taggingSegmentIds = [];
      notifyListeners();
    }
  }

  @override
  void onSegmentReceived(List<TranscriptSegment> newSegments) {
    _processNewSegmentReceived(newSegments);
  }

  void _processNewSegmentReceived(List<TranscriptSegment> newSegments) async {
    if (newSegments.isEmpty) return;
    final captureGeneration = _captureGeneration;

    if (segments.isEmpty && !_isLoadingInProgressConversation) {
      _isLoadingInProgressConversation = true;
      if (!PlatformService.isDesktop) {
        FlutterForegroundTask.sendDataToTask(jsonEncode({'location': true}));
      }
      try {
        await refreshInProgressConversations();
      } finally {
        _isLoadingInProgressConversation = false;
      }
    }
    if (captureGeneration != _captureGeneration) return;

    final remainSegments = TranscriptSegment.updateSegments(segments, newSegments);
    segments.addAll(remainSegments);

    // Refresh people cache if we see unknown personIds (backend-created persons)
    // Check all newSegments, not just remainSegments, to catch updates to existing segments
    if (_peopleRefreshFuture == null && _hasMissingPerson(newSegments)) {
      _peopleRefreshFuture = peopleProvider?.setPeople().whenComplete(() {
        _peopleRefreshFuture = null;
      });
    }

    _segmentsPhotosVersion++; // Bump version so Selector rebuilds
    hasTranscripts = true;
    notifyListeners();
  }

  void onConnectionStateChanged(bool isConnected) {
    _isConnected = isConnected;
    notifyListeners();
  }

  // ============== Freemium: Threshold Notification ==============

  /// Handle freemium threshold reached: Notify user based on required action
  void _handleFreemiumThresholdReached(FreemiumThresholdReachedEvent event) {
    if (_freemiumThresholdReached) return;

    _freemiumThresholdReached = true;
    _freemiumRemainingSeconds = event.remainingSeconds;
    _freemiumRequiresUserAction = event.requiresUserAction;

    Logger.debug('[Freemium] Threshold reached - ${event.remainingSeconds} seconds remaining');
    Logger.debug('[Freemium] Action required: ${event.action.name}, requires user action: ${event.requiresUserAction}');

    if (event.requiresUserAction) {
      Logger.debug('[Freemium] User should setup on-device transcription in Settings > Transcription');
    } else {
      Logger.debug('[Freemium] No user action required - backend will handle fallback');
    }

    // Update usage provider to reflect approaching limit
    usageProvider?.refreshSubscription();

    notifyListeners();
  }

  /// Callback for external components to reset their freemium session state
  VoidCallback? onFreemiumSessionReset;

  /// Reset freemium threshold state (e.g., when credits reset or on new session)
  void resetFreemiumThresholdState() {
    _freemiumThresholdReached = false;
    _freemiumRemainingSeconds = 0;
    _freemiumRequiresUserAction = false;
    // Notify external handlers (e.g., FreemiumSwitchHandler)
    onFreemiumSessionReset?.call();
    notifyListeners();
  }

  /// Check if credits were restored and reset threshold state
  Future<void> checkCreditsAndResetThresholdIfNeeded() async {
    await usageProvider?.fetchSubscription();
    if (usageProvider?.isOutOfCredits == false && _freemiumThresholdReached) {
      Logger.debug('[Freemium] Credits restored! Resetting threshold state.');
      resetFreemiumThresholdState();
    }
  }

  void setIsWalSupported(bool value) {
    _isWalSupported = value;
    notifyListeners();
  }

  void _processSystemAudioByteReceived(Uint8List bytes) {
    if (_systemAudioCaptureAuthority?.isCurrent() != true) return;
    _systemAudioBuffer.addAll(bytes);
    if (!_systemAudioCaching) {
      _flushSystemAudioBuffer();
    }
  }

  void _broadcastRecordingState() {
    if (!PlatformService.isDesktop) return;

    final stateData = {
      'isRecording':
          recordingState == RecordingState.systemAudioRecord || recordingState == RecordingState.deviceRecord,
      'isPaused': _isPaused,
      'duration': _getRecordingDuration(),
      'isInitialising': recordingState == RecordingState.initialising,
    };

    _controlBarChannel.invokeMethod('updateRecordingState', stateData);
  }

  void _startRecordingTimer() {
    _recordingDuration = 0;
    _recordingTimer?.cancel();
    _recordingTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (recordingState == RecordingState.systemAudioRecord || recordingState == RecordingState.deviceRecord) {
        _recordingDuration++;
        _broadcastRecordingState();
      }
    });
  }

  void _pauseRecordingTimer() {
    // Stop the timer but preserve the current duration
    _recordingTimer?.cancel();
    _recordingTimer = null;
    // Don't reset _recordingDuration here
  }

  void _stopRecordingTimer() {
    _recordingTimer?.cancel();
    _recordingTimer = null;
    _recordingDuration = 0;
  }

  Future<void> pauseDeviceRecording() async {
    if (_recordingDevice == null) return;

    // Pause the BLE stream but keep the device connection
    await _bleBytesStream?.cancel();
    _isPaused = true;
    updateRecordingState(RecordingState.pause);
    notifyListeners();
  }

  Future<void> resumeDeviceRecording() async {
    if (_recordingDevice == null) return;
    _isPaused = false;
    updateRecordingState(RecordingState.initialising);
    final started = await _initiateDeviceAudioStreaming();
    if (!started) {
      updateRecordingState(RecordingState.error);
    }
    notifyListeners();
  }
}
