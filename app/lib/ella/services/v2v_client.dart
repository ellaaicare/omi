import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:record/record.dart';
import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

enum V2VSessionScopeKind { memory }

@immutable
class V2VSessionScope {
  const V2VSessionScope.memory({required this.conversationId, this.expectedActiveSummaryVersionId})
      : kind = V2VSessionScopeKind.memory;

  final V2VSessionScopeKind kind;
  final String conversationId;
  final String? expectedActiveSummaryVersionId;

  Map<String, dynamic> toJson() => {
        'kind': kind.name,
        'conversation_id': conversationId,
        if (expectedActiveSummaryVersionId?.isNotEmpty == true)
          'expected_active_summary_version_id': expectedActiveSummaryVersionId,
      };

  V2VSessionScope withExpectedActiveSummaryVersionId(String? value) =>
      V2VSessionScope.memory(conversationId: conversationId, expectedActiveSummaryVersionId: value);
}

@immutable
class V2VResolvedSessionScope {
  const V2VResolvedSessionScope({
    required this.kind,
    required this.conversationId,
    required this.activeSummaryVersionId,
    required this.canReinterpret,
  });

  final V2VSessionScopeKind kind;
  final String conversationId;
  final String activeSummaryVersionId;
  final bool canReinterpret;

  static V2VResolvedSessionScope? tryParse(Object? value) {
    if (value is! Map) return null;
    final kind = value['kind']?.toString();
    final conversationId = value['conversation_id']?.toString().trim() ?? '';
    final activeSummaryVersionId = value['active_summary_version_id']?.toString().trim() ?? '';
    final canReinterpret = value['can_reinterpret'];
    if (kind != V2VSessionScopeKind.memory.name ||
        conversationId.isEmpty ||
        activeSummaryVersionId.isEmpty ||
        canReinterpret is! bool) {
      return null;
    }
    return V2VResolvedSessionScope(
      kind: V2VSessionScopeKind.memory,
      conversationId: conversationId,
      activeSummaryVersionId: activeSummaryVersionId,
      canReinterpret: canReinterpret,
    );
  }

  bool matches(V2VSessionScope requested) => kind == requested.kind && conversationId == requested.conversationId;
}

@immutable
class MemoryReinterpretationEvent {
  const MemoryReinterpretationEvent({
    required this.state,
    required this.sessionId,
    required this.conversationId,
    required this.correctionId,
    this.traceId = '',
    this.status = '',
    this.pollAfter = const Duration(milliseconds: 750),
  });

  final String state;
  final String sessionId;
  final String conversationId;
  final String correctionId;
  final String traceId;
  final String status;
  final Duration pollAfter;

  static MemoryReinterpretationEvent? tryParse(Object? value) {
    if (value is! Map) return null;
    final state = value['state']?.toString().trim() ?? '';
    final sessionId = value['session_id']?.toString().trim() ?? '';
    final conversationId = value['conversation_id']?.toString().trim() ?? '';
    final correctionId = value['correction_id']?.toString().trim() ?? '';
    if (state.isEmpty || sessionId.isEmpty || conversationId.isEmpty || correctionId.isEmpty) {
      return null;
    }
    final rawPollAfter = value['poll_after_ms'];
    final pollAfterMs = rawPollAfter is num ? rawPollAfter.toInt().clamp(500, 5000) : 750;
    return MemoryReinterpretationEvent(
      state: state,
      sessionId: sessionId,
      conversationId: conversationId,
      correctionId: correctionId,
      traceId: value['trace_id']?.toString().trim() ?? '',
      status: value['status']?.toString().trim() ?? '',
      pollAfter: Duration(milliseconds: pollAfterMs),
    );
  }
}

/// JSON event from the V2V proxy WebSocket.
class V2VEvent {
  const V2VEvent({
    required this.type,
    this.text,
    this.policyReason,
    this.resetsAt,
    this.memoryReinterpretation,
    this.quotaState,
    this.quota,
    this.turnBoundary = false,
  });

  final String type;
  final String? text;
  final EllaVoicePolicyReason? policyReason;
  final DateTime? resetsAt;
  final MemoryReinterpretationEvent? memoryReinterpretation;
  final String? quotaState;
  final EllaQuota? quota;
  final bool turnBoundary;
}

enum V2VConnectionStage { consent, identity, providerRegistry, session, audioSession, websocket, microphone, connected }

/// Redacted connection result safe to show in UI and persist in debug logs.
/// It intentionally excludes session tokens, endpoint URLs, and response bodies.
class V2VConnectionReceipt {
  const V2VConnectionReceipt({
    required this.connected,
    required this.provider,
    required this.stage,
    this.voiceMode = '',
    this.sessionId = '',
    this.sessionScope,
    this.httpStatus,
    this.errorCode = '',
  });

  final bool connected;
  final String provider;
  final String voiceMode;
  final String sessionId;
  final V2VResolvedSessionScope? sessionScope;
  final V2VConnectionStage stage;
  final int? httpStatus;
  final String errorCode;

  bool get shouldRefreshMemoryScope => stage == V2VConnectionStage.session && errorCode == 'voice_session_scope_stale';
  EllaVoicePolicyReason? get policyReason => parseEllaVoicePolicyReason(errorCode);
  bool get isPolicyDenial => policyReason != null;

  String get safeDetail {
    final parts = <String>[if (httpStatus != null) 'HTTP $httpStatus', if (errorCode.isNotEmpty) errorCode];
    return parts.isEmpty ? stage.name : parts.join(' · ');
  }

  Map<String, Object?> toDebugFields() => {
        'connected': connected,
        'provider': provider,
        'voice_mode': voiceMode,
        'stage': stage.name,
        if (sessionId.isNotEmpty) 'session_id': sessionId,
        if (sessionScope != null) 'scope_kind': sessionScope!.kind.name,
        if (sessionScope != null) 'scope_conversation_id': sessionScope!.conversationId,
        if (httpStatus != null) 'http_status': httpStatus,
        if (errorCode.isNotEmpty) 'error_code': errorCode,
      };
}

class _V2VSessionResult {
  const _V2VSessionResult({this.data, required this.receipt});

  final Map<String, dynamic>? data;
  final V2VConnectionReceipt receipt;
}

/// Voice-to-voice WebSocket client for half-duplex PCM16 audio streaming.
///
/// Uses `record` package for mic input and FlutterSound for PCM playback.
/// The mic recorder is suspended during playback so provider VAD cannot hear
/// Ella's own response audio or post-turn background noise.
class V2VClient {
  static const int _pcmSampleRate = 24000;
  static const int _pcmBytesPerSample = 2;
  static const int _pcmChannels = 1;
  static const Duration _postPlaybackMicCooldown = Duration(seconds: 2);
  static const Duration _playbackFinishQuietPeriod = Duration(milliseconds: 900);

  static V2VClient? _activeClient;

  WebSocketChannel? _channel;
  AudioRecorder? _recorder;
  StreamSubscription? _micSub;
  StreamSubscription? _wsSub;
  AiConsentActiveSessionLease? _aiConsentLease;
  bool _isConnected = false;
  bool _connectionAnnounced = false;
  bool _isPlaying = false;
  bool _micMuted = false;
  bool _micSuspendedForPlayback = false;
  int _micChunksSent = 0;
  int _micBytesSent = 0;
  Future<void> _micGateFuture = Future.value();

  /// PCM buffer for accumulating audio chunks before WAV playback
  final BytesBuilder _pcmBuffer = BytesBuilder(copy: false);
  int _chunkCount = 0;

  /// Low-latency PCM stream player for provider-native V2V audio.
  final FlutterSoundPlayer _streamPlayer = FlutterSoundPlayer();
  bool _streamPlayerOpen = false;
  bool _streamPlaybackStarted = false;
  DateTime? _streamPlaybackStartedAt;
  Future<void> _streamFeedFuture = Future.value();
  Timer? _finishPlaybackTimer;
  bool _finishingPlayback = false;

  /// Callback for JSON events (transcripts, errors, etc.)
  final void Function(V2VEvent event)? onEvent;

  /// Callback for connection state changes.
  final void Function(bool connected)? onConnectionChanged;

  V2VClient({this.onEvent, this.onConnectionChanged});

  bool get isConnected => _isConnected;

  V2VConnectionReceipt? get lastConnectionReceipt => _lastConnectionReceipt;

  V2VConnectionReceipt? _lastConnectionReceipt;

  static String normalizeProvider(String provider) => switch (provider) {
        // Legacy values may remain in SharedPreferences after TestFlight upgrades.
        'gemini-live' => 'gemini-native-live',
        'openai-realtime' => 'openai-native-realtime',
        _ => provider,
      };

  static bool isSessionProvider(String provider) =>
      normalizeProvider(provider) == 'openclaw-direct' ||
      normalizeProvider(provider) == 'openai-native-realtime' ||
      normalizeProvider(provider) == 'grok-voice' ||
      normalizeProvider(provider) == 'gemini-native-live';

  static String? sessionVoiceMode(String provider, {bool memoryScoped = false}) =>
      switch (normalizeProvider(provider)) {
        'grok-voice' when memoryScoped => 'v4',
        'openclaw-direct' => 'openclaw-direct-v1',
        'openai-native-realtime' => 'openai-native-realtime-v1',
        'gemini-native-live' => 'gemini-native-live-v1',
        _ => null,
      };

  static bool isMemoryScopedProvider(String provider) {
    final normalized = normalizeProvider(provider);
    return normalized == 'grok-voice' || normalized == 'gemini-native-live';
  }

  static String providerDisplayName(String provider) => switch (normalizeProvider(provider)) {
        'grok-voice' => 'Grok Native Realtime',
        'gemini-native-live' => 'Gemini Native Live',
        'openai-native-realtime' => 'OpenAI Native Realtime',
        'openclaw-direct' => 'OpenClaw Direct',
        _ => provider,
      };

  static String resolveEffectiveProvider({required String provisionedProvider, required String selectedProvider}) {
    final provisioned = normalizeProvider(provisionedProvider.trim());
    return provisioned.isNotEmpty ? provisioned : normalizeProvider(selectedProvider.trim());
  }

  @visibleForTesting
  static Map<String, dynamic> buildSessionRequestBody({
    required String uid,
    required String provider,
    required bool includeUid,
    V2VSessionScope? sessionScope,
  }) {
    final canonicalProvider = normalizeProvider(provider);
    final voiceMode = sessionVoiceMode(canonicalProvider, memoryScoped: sessionScope != null);
    return {
      if (includeUid) 'uid': uid,
      'provider': canonicalProvider,
      if (voiceMode != null) 'voice_mode': voiceMode,
      if (sessionScope != null) 'session_scope': sessionScope.toJson(),
    };
  }

  @visibleForTesting
  static String sessionFailureCode({required int? statusCode, required String body, V2VSessionScope? sessionScope}) {
    if (sessionScope != null && statusCode == 404) {
      return 'voice_session_scope_unavailable';
    }
    final code = safeErrorCode(body, fallback: 'session_request_failed');
    if (sessionScope != null &&
        statusCode == 409 &&
        (code == 'voice_session_scope_stale' || code == 'voice_session_scope_version_unavailable')) {
      return 'voice_session_scope_stale';
    }
    return code;
  }

  @visibleForTesting
  static Set<String> availableSessionProviders(Object? responseJson) {
    if (responseJson is! Map) return const {};
    final providers = responseJson['providers'];
    if (providers is! List) return const {};

    return providers.whereType<Map>().where((item) {
      return item['type'] == 'v2v' && item['available'] == true;
    }).map((item) {
      return normalizeProvider(item['id']?.toString() ?? '');
    }).where((provider) {
      return provider.isNotEmpty;
    }).toSet();
  }

  @visibleForTesting
  static String safeErrorCode(String body, {String fallback = 'request_failed'}) {
    Object? decoded;
    try {
      decoded = jsonDecode(body);
    } catch (_) {
      decoded = null;
    }

    String? candidate;
    if (decoded is Map) {
      final detail = decoded['detail'];
      final error = decoded['error'];
      if (detail is Map) candidate = detail['code']?.toString();
      if (candidate == null && error is Map) candidate = error['code']?.toString();
      candidate ??= decoded['code']?.toString();
      if (candidate == null && detail is String && !detail.contains(' ') && !detail.contains('.')) candidate = detail;
      if (candidate == null && error is String && !error.contains(' ') && !error.contains('.')) candidate = error;
      final humanDetail = detail is String
          ? detail.toLowerCase()
          : error is String
              ? error.toLowerCase()
              : '';
      if (candidate == null && humanDetail.contains('missing api key')) candidate = 'provider_not_configured';
      if (candidate == null && humanDetail.contains('unknown v2v provider')) candidate = 'unknown_provider';
      if (candidate == null && humanDetail.contains('uid required')) candidate = 'uid_required';
    }

    if (candidate != null && candidate.length > 64) candidate = null;

    final normalized = (candidate ?? fallback)
        .toLowerCase()
        .replaceAll(RegExp('[^a-z0-9_-]+'), '_')
        .replaceAll(RegExp('_+'), '_')
        .replaceAll(RegExp('^_|_\$'), '');
    if (normalized.isEmpty) return fallback;
    return normalized.length <= 64 ? normalized : normalized.substring(0, 64);
  }

  /// Start a V2V session: get session token, connect WebSocket, start audio.
  Future<V2VConnectionReceipt> connect({
    required String provider,
    V2VSessionScope? sessionScope,
    bool Function()? shouldContinue,
  }) async {
    provider = normalizeProvider(provider);

    Future<V2VConnectionReceipt?> cancelIfRequested(V2VConnectionStage stage, {String voiceMode = ''}) async {
      if (shouldContinue == null || shouldContinue()) return null;
      Logger.debug('[V2V] Cancelling stale connect for provider=$provider stage=${stage.name}');
      await disconnect();
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: voiceMode,
          stage: stage,
          errorCode: 'connection_cancelled',
        ),
      );
    }

    final initialCancellation = await cancelIfRequested(V2VConnectionStage.providerRegistry);
    if (initialCancellation != null) return initialCancellation;

    if (_activeClient != null && _activeClient != this) {
      Logger.debug('[V2V] Closing existing active V2V client before provider=$provider connect');
      await _activeClient!.disconnect();
      final cancellation = await cancelIfRequested(V2VConnectionStage.providerRegistry);
      if (cancellation != null) return cancellation;
    }
    _activeClient = this;

    if (_isConnected || _channel != null || _recorder != null) {
      Logger.debug('[V2V] connect() called with existing session state, disconnecting first');
      await disconnect();
      final cancellation = await cancelIfRequested(V2VConnectionStage.providerRegistry);
      if (cancellation != null) return cancellation;
      _activeClient = this;
    }

    if (!SharedPreferencesUtil().aiConsentAccepted) {
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.consent,
          errorCode: 'ai_consent_required',
        ),
      );
    }

    if (!isSessionProvider(provider)) {
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.providerRegistry,
          errorCode: 'unsupported_provider',
        ),
      );
    }
    if (sessionScope != null && !isMemoryScopedProvider(provider)) {
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.providerRegistry,
          errorCode: 'memory_scope_provider_unsupported',
        ),
      );
    }

    final uid = SharedPreferencesUtil().uid;
    if (uid.isEmpty) {
      Logger.debug('[V2V] No uid, cannot connect');
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.identity,
          errorCode: 'missing_authenticated_identity',
        ),
      );
    }

    final registryFailure = await _validateProviderRegistry(provider);
    final registryCancellation = await cancelIfRequested(V2VConnectionStage.providerRegistry);
    if (registryCancellation != null) return registryCancellation;
    if (registryFailure != null) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(registryFailure);
    }

    // 1. Get session token from backend
    final sessionResult = await _createSession(uid, provider, sessionScope);
    final sessionCancellation = await cancelIfRequested(
      V2VConnectionStage.session,
      voiceMode: sessionVoiceMode(provider, memoryScoped: sessionScope != null) ?? '',
    );
    if (sessionCancellation != null) return sessionCancellation;
    final sessionData = sessionResult.data;
    if (sessionData == null) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(sessionResult.receipt);
    }

    final token = sessionData['session_token'] as String? ?? '';
    final endpoint = sessionData['voice_endpoint'] as String? ?? '';
    final confirmedProvider = normalizeProvider(sessionData['provider'] as String? ?? provider);
    final confirmedVoiceMode =
        sessionData['voice_mode'] as String? ?? sessionVoiceMode(provider, memoryScoped: sessionScope != null) ?? '';
    final sessionId = sessionData['session_id']?.toString().trim() ?? '';
    final confirmedScope = V2VResolvedSessionScope.tryParse(sessionData['session_scope']);
    if (token.isEmpty || endpoint.isEmpty) {
      Logger.debug('[V2V] Invalid session data');
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          stage: V2VConnectionStage.session,
          httpStatus: 200,
          errorCode: 'invalid_session_contract',
        ),
      );
    }
    if (confirmedProvider != provider) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          stage: V2VConnectionStage.session,
          httpStatus: 200,
          errorCode: 'provider_mismatch',
        ),
      );
    }
    if (sessionScope != null &&
        (sessionId.isEmpty || confirmedScope == null || !confirmedScope.matches(sessionScope))) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          stage: V2VConnectionStage.session,
          httpStatus: 200,
          errorCode: 'invalid_session_scope',
        ),
      );
    }

    // 2. Configure iOS audio session for playAndRecord with Bluetooth + speaker routing
    try {
      await _configureAudioSession();
      final cancellation = await cancelIfRequested(V2VConnectionStage.audioSession, voiceMode: confirmedVoiceMode);
      if (cancellation != null) return cancellation;
    } catch (error) {
      Logger.error('[V2V] Audio session setup failed for provider=$provider');
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          stage: V2VConnectionStage.audioSession,
          errorCode: _safeExceptionCode(error, fallback: 'audio_session_failed'),
        ),
      );
    }

    // 3. Connect WebSocket
    final wsUrl = _withSessionToken(endpoint, token);
    Logger.debug('[V2V] Connecting to WebSocket for provider=$provider...');

    try {
      _channel = IOWebSocketChannel.connect(Uri.parse(wsUrl), pingInterval: const Duration(seconds: 30));

      await _channel!.ready.timeout(const Duration(seconds: 12));
      final websocketCancellation = await cancelIfRequested(
        V2VConnectionStage.websocket,
        voiceMode: confirmedVoiceMode,
      );
      if (websocketCancellation != null) return websocketCancellation;

      // 3. Listen for messages from proxy
      _isConnected = true;
      _wsSub = _channel!.stream.listen(
        _handleMessage,
        onError: (error) {
          Logger.error('[V2V] WebSocket error: ${error.runtimeType}');
          disconnect();
        },
        onDone: () {
          Logger.debug('[V2V] WebSocket closed');
          _isConnected = false;
          if (_connectionAnnounced) {
            _connectionAnnounced = false;
            onConnectionChanged?.call(false);
          }
        },
      );

      // 4. Start recording mic audio and streaming to WebSocket
      final micStarted = await _startMicStream();
      final microphoneCancellation = await cancelIfRequested(
        V2VConnectionStage.microphone,
        voiceMode: confirmedVoiceMode,
      );
      if (microphoneCancellation != null) return microphoneCancellation;
      if (!micStarted || !_isConnected) {
        await disconnect();
        return _completeReceipt(
          V2VConnectionReceipt(
            connected: false,
            provider: provider,
            voiceMode: confirmedVoiceMode,
            stage: micStarted ? V2VConnectionStage.websocket : V2VConnectionStage.microphone,
            errorCode: micStarted ? 'websocket_closed' : 'microphone_unavailable',
          ),
        );
      }
      _aiConsentLease = AiConsentActiveSessionLease(
        uid: uid,
        onAuthorityLost: () async {
          onEvent?.call(const V2VEvent(type: 'consent_authority_lost'));
          await disconnect();
        },
      )..start();
      _connectionAnnounced = true;
      onConnectionChanged?.call(true);

      return _completeReceipt(
        V2VConnectionReceipt(
          connected: true,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          sessionId: sessionId,
          sessionScope: confirmedScope,
          stage: V2VConnectionStage.connected,
        ),
      );
    } catch (error) {
      Logger.error('[V2V] WebSocket handshake failed for provider=$provider');
      await disconnect();
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: confirmedVoiceMode,
          stage: V2VConnectionStage.websocket,
          errorCode: _safeExceptionCode(error, fallback: 'websocket_handshake_failed'),
        ),
      );
    }
  }

  /// Disconnect and clean up all resources.
  Future<void> disconnect() async {
    final consentLease = _aiConsentLease;
    _aiConsentLease = null;
    consentLease?.stop();
    final shouldAnnounceDisconnect = _connectionAnnounced;
    _isConnected = false;
    _connectionAnnounced = false;
    if (shouldAnnounceDisconnect) onConnectionChanged?.call(false);
    if (_activeClient == this) {
      _activeClient = null;
    }

    _wsSub?.cancel();
    _wsSub = null;

    try {
      await _channel?.sink.close();
    } catch (_) {}
    _channel = null;

    await _stopMicStream(reason: 'disconnect');
    try {
      await _stopStreamingPlayback();
      if (_streamPlayerOpen) {
        await _streamPlayer.closePlayer();
        _streamPlayerOpen = false;
      }
    } catch (_) {}
    _resetTurnState();
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    if (_isPlaying) {
      try {
        await _stopStreamingPlayback();
      } catch (_) {}
      _isPlaying = false;
    }
    _micSuspendedForPlayback = false;
    _micMuted = false;
    _pcmBuffer.clear();
    _chunkCount = 0;
    _streamPlaybackStartedAt = null;
  }

  void _suspendMicForPlayback(String reason) {
    if (_micMuted && _micSuspendedForPlayback) return;
    _micMuted = true;
    _micSuspendedForPlayback = true;
    Logger.debug('[V2V] Mic gate closed for playback: reason=$reason sent=$_micChunksSent chunks/$_micBytesSent bytes');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic gate closed: $reason'));

    _micGateFuture = _micGateFuture.then((_) async {
      await _stopMicStream(reason: 'playback_gate:$reason');
    }).catchError((error) {
      Logger.error('[V2V] Mic gate close failed: $error');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic gate close failed: $error'));
    });
  }

  void _resetTurnState() {
    _isPlaying = false;
    _micMuted = false;
    _micSuspendedForPlayback = false;
    _pcmBuffer.clear();
    _chunkCount = 0;
    _micChunksSent = 0;
    _micBytesSent = 0;
    _streamPlaybackStarted = false;
    _streamPlaybackStartedAt = null;
    _streamFeedFuture = Future.value();
    _finishPlaybackTimer?.cancel();
    _finishPlaybackTimer = null;
    _finishingPlayback = false;
    _micGateFuture = Future.value();
  }

  // --- Audio session ---

  Future<void> _configureAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(
      AudioSessionConfiguration(
        avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
        avAudioSessionCategoryOptions: AVAudioSessionCategoryOptions.defaultToSpeaker |
            AVAudioSessionCategoryOptions.allowBluetooth |
            AVAudioSessionCategoryOptions.allowBluetoothA2dp |
            AVAudioSessionCategoryOptions.allowAirPlay,
        avAudioSessionMode: AVAudioSessionMode.defaultMode,
        avAudioSessionRouteSharingPolicy: AVAudioSessionRouteSharingPolicy.defaultPolicy,
        avAudioSessionSetActiveOptions: AVAudioSessionSetActiveOptions.none,
      ),
    );
    await session.setActive(true);
    Logger.debug('[V2V] Audio session: playAndRecord + defaultToSpeaker + BT + AirPlay');
  }

  // --- Session management ---

  Future<V2VConnectionReceipt?> _validateProviderRegistry(String provider) async {
    try {
      final response = await makeApiCall(
        url: '${Env.apiBaseUrl}v1/voice/providers',
        headers: const {},
        body: '',
        method: 'GET',
        timeout: const Duration(seconds: 5),
        retries: 0,
      );
      if (response == null || response.statusCode != 200) {
        _logRegistryReceipt(provider, response?.statusCode, 'registry_unavailable');
        return null;
      }

      final providers = availableSessionProviders(jsonDecode(response.body));
      if (!providers.contains(provider)) {
        return V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.providerRegistry,
          httpStatus: response.statusCode,
          errorCode: 'provider_unavailable',
        );
      }
      _logRegistryReceipt(provider, response.statusCode, 'available');
    } catch (_) {
      _logRegistryReceipt(provider, null, 'registry_unavailable');
    }
    return null;
  }

  Future<_V2VSessionResult> _createSession(String uid, String provider, V2VSessionScope? sessionScope) async {
    try {
      provider = normalizeProvider(provider);
      final voiceMode = sessionVoiceMode(provider, memoryScoped: sessionScope != null);
      final requestBody = buildSessionRequestBody(
        uid: uid,
        provider: provider,
        includeUid: !isHermesProvisioningGateEnabled,
        sessionScope: sessionScope,
      );

      final response = await makeApiCall(
        url: '${Env.apiBaseUrl}v1/voice/session',
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(requestBody),
        method: 'POST',
        timeout: const Duration(seconds: 10),
      );

      if (response == null || response.statusCode != 200) {
        Logger.debug('[V2V] Session create failed: ${response?.statusCode}');
        return _V2VSessionResult(
          receipt: V2VConnectionReceipt(
            connected: false,
            provider: provider,
            voiceMode: voiceMode ?? '',
            stage: V2VConnectionStage.session,
            httpStatus: response?.statusCode,
            errorCode: response == null
                ? 'session_request_failed'
                : sessionFailureCode(statusCode: response.statusCode, body: response.body, sessionScope: sessionScope),
          ),
        );
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      Logger.debug('[V2V] Session created: provider=$provider voice_mode=${voiceMode ?? "default"}');
      return _V2VSessionResult(
        data: data,
        receipt: V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: data['voice_mode'] as String? ?? voiceMode ?? '',
          stage: V2VConnectionStage.session,
          sessionId: data['session_id']?.toString() ?? '',
          sessionScope: V2VResolvedSessionScope.tryParse(data['session_scope']),
          httpStatus: response.statusCode,
        ),
      );
    } catch (error) {
      Logger.error('[V2V] Session create error for provider=$provider');
      return _V2VSessionResult(
        receipt: V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: sessionVoiceMode(provider, memoryScoped: sessionScope != null) ?? '',
          stage: V2VConnectionStage.session,
          errorCode: _safeExceptionCode(error, fallback: 'session_request_failed'),
        ),
      );
    }
  }

  V2VConnectionReceipt _completeReceipt(V2VConnectionReceipt receipt) {
    if (!receipt.connected && !_isConnected && _channel == null && _activeClient == this) {
      _activeClient = null;
    }
    _lastConnectionReceipt = receipt;
    final fields = receipt.toDebugFields();
    Logger.debug('[V2V] Connection receipt: $fields');
    unawaited(DebugLogManager.logEvent('v2v_connection_receipt', fields));
    onEvent?.call(V2VEvent(type: 'connection_receipt', text: receipt.safeDetail));
    return receipt;
  }

  void _logRegistryReceipt(String provider, int? status, String result) {
    final fields = <String, Object?>{
      'provider': provider,
      'stage': V2VConnectionStage.providerRegistry.name,
      'result': result,
      if (status != null) 'http_status': status,
    };
    Logger.debug('[V2V] Provider registry receipt: $fields');
    unawaited(DebugLogManager.logEvent('v2v_provider_registry_receipt', fields));
  }

  static String _safeExceptionCode(Object error, {required String fallback}) {
    final raw = error.runtimeType.toString();
    return safeErrorCode('{"code":"$raw"}', fallback: fallback);
  }

  static String _withSessionToken(String endpoint, String token) {
    final uri = Uri.parse(endpoint);
    if (uri.queryParameters.containsKey('token')) {
      return endpoint;
    }
    final separator = uri.hasQuery ? '&' : '?';
    return '$endpoint${separator}token=$token';
  }

  static String? _eventText(Map<String, dynamic> json) {
    for (final key in ['text', 'transcript', 'delta', 'content', 'message']) {
      final value = json[key];
      if (value is String && value.isNotEmpty) return value;
    }

    final response = json['response'];
    if (response is Map<String, dynamic>) {
      return _eventText(response);
    }

    final item = json['item'];
    if (item is Map<String, dynamic>) {
      return _eventText(item);
    }

    return null;
  }

  static EllaVoicePolicyReason? _eventPolicyReason(Map<String, dynamic> json) {
    for (final key in ['reason', 'termination_reason', 'denial_reason', 'code', 'state']) {
      final reason = parseEllaVoicePolicyReason(json[key]);
      if (reason != null) return reason;
    }
    for (final key in ['detail', 'error', 'data', 'quota']) {
      final value = json[key];
      if (value is Map) {
        final reason = _eventPolicyReason(value.map((key, value) => MapEntry(key.toString(), value)));
        if (reason != null) return reason;
      }
    }
    return null;
  }

  @visibleForTesting
  static EllaVoicePolicyReason? policyReasonFromEvent(Map<String, dynamic> json) => _eventPolicyReason(json);

  @visibleForTesting
  static V2VEvent? quotaEventFromEvent(Map<String, dynamic> json) {
    if (json['type']?.toString() != 'quota_state') return null;
    final quotaValue = json['quota'];
    return V2VEvent(
      type: 'quota_state',
      quotaState: json['state']?.toString().trim().toLowerCase(),
      quota: quotaValue is Map ? EllaQuota.fromJson(quotaValue) : null,
      policyReason: _eventPolicyReason(json),
      resetsAt: _eventResetsAt(json),
      turnBoundary: json['turn_boundary'] == true,
    );
  }

  static DateTime? _eventResetsAt(Map<String, dynamic> json) {
    final direct = DateTime.tryParse(json['resets_at']?.toString() ?? '')?.toLocal();
    if (direct != null) return direct;
    for (final key in ['detail', 'error', 'data', 'quota']) {
      final value = json[key];
      if (value is Map) {
        final parsed = _eventResetsAt(value.map((key, value) => MapEntry(key.toString(), value)));
        if (parsed != null) return parsed;
      }
    }
    return null;
  }

  static bool _isUserTranscriptEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'user_transcript' ||
        normalized == 'input_transcript' ||
        normalized == 'input_audio_transcription.completed' ||
        normalized.contains('input_audio_transcription') ||
        (normalized.contains('user') && normalized.contains('transcript'));
  }

  static bool _isAssistantTranscriptEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'transcript' ||
        normalized == 'transcript_delta' ||
        normalized == 'assistant_transcript' ||
        normalized == 'output_transcript' ||
        normalized == 'response_text' ||
        normalized == 'response.audio_transcript.delta' ||
        normalized == 'response.audio_transcript.done' ||
        normalized == 'response.text.delta' ||
        normalized == 'response.text.done' ||
        normalized.contains('output_audio_transcription') ||
        (normalized.contains('assistant') && normalized.contains('transcript'));
  }

  @visibleForTesting
  static bool treatsAsAssistantTranscriptEvent(String type) {
    return _isAssistantTranscriptEvent(type);
  }

  static bool _isAudioDoneEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'audio_done' ||
        normalized == 'response.audio.done' ||
        normalized == 'output_audio.done' ||
        normalized == 'audio.done';
  }

  static bool _isResponseCompleteEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'turn_complete' || normalized == 'response.done';
  }

  @visibleForTesting
  static bool treatsAsAudioDoneEvent(String type) {
    return _isAudioDoneEvent(type);
  }

  @visibleForTesting
  static bool treatsAsResponseCompleteEvent(String type) {
    return _isResponseCompleteEvent(type);
  }

  // --- Mic recording (PCM16, 24kHz, mono) using `record` package ---

  Future<bool> _startMicStream() async {
    try {
      if (_recorder != null || _micSub != null) {
        Logger.debug('[V2V] Mic stream already active, not starting duplicate recorder');
        return true;
      }

      _recorder = AudioRecorder();

      final hasPermission = await _recorder!.hasPermission();
      if (!hasPermission) {
        Logger.error('[V2V] Mic permission denied');
        onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic permission denied'));
        await _recorder?.dispose();
        _recorder = null;
        return false;
      }

      final stream = await _recorder!.startStream(
        const RecordConfig(
          encoder: AudioEncoder.pcm16bits,
          sampleRate: 24000,
          numChannels: 1,
          autoGain: true,
          echoCancel: true,
          noiseSuppress: true,
        ),
      );

      _micChunksSent = 0;
      _micBytesSent = 0;
      _micSub = stream.listen((data) {
        if (_isConnected &&
            _channel != null &&
            !_micMuted &&
            !_isPlaying &&
            !_micSuspendedForPlayback &&
            SharedPreferencesUtil().aiConsentAccepted) {
          _channel!.sink.add(data);
          _micChunksSent++;
          _micBytesSent += data.length;
          if (_micChunksSent % 50 == 0) {
            Logger.debug('[V2V] Mic streamed $_micChunksSent chunks, $_micBytesSent bytes');
          }
        }
      });

      _micMuted = false;
      _micSuspendedForPlayback = false;
      Logger.debug('[V2V] Mic gate open: recording at 24kHz');
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic active'));
      return true;
    } catch (error) {
      Logger.error('[V2V] Mic start failed: ${error.runtimeType}');
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic unavailable'));
      return false;
    }
  }

  Future<void> _stopMicStream({required String reason}) async {
    final hadMic = _micSub != null || _recorder != null;
    if (hadMic) {
      Logger.debug('[V2V] Mic stream stopping: reason=$reason');
    }

    await _micSub?.cancel();
    _micSub = null;
    try {
      await _recorder?.stop();
      await _recorder?.dispose();
    } catch (_) {}
    _recorder = null;

    if (hadMic) {
      Logger.debug('[V2V] Mic stream stopped: reason=$reason');
    }
  }

  Future<void> _resumeMicAfterPlayback() async {
    if (!_micSuspendedForPlayback) {
      _micMuted = false;
      return;
    }

    Logger.debug('[V2V] Mic gate cooldown: ${_postPlaybackMicCooldown.inMilliseconds}ms');
    onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic gate cooldown'));
    await Future.delayed(_postPlaybackMicCooldown);
    await _micGateFuture;

    if (!_isConnected || _channel == null) {
      Logger.debug('[V2V] Mic gate remains closed: session disconnected');
      _micSuspendedForPlayback = false;
      _micMuted = false;
      return;
    }

    if (_isPlaying || _streamPlaybackStarted) {
      Logger.debug('[V2V] Mic gate remains closed: playback still active');
      return;
    }

    Logger.debug('[V2V] Mic gate reopening after playback cooldown');
    await _startMicStream();
  }

  // --- Low-latency PCM streaming playback ---

  /// Stream incoming PCM16 audio chunk to the platform player.
  void _streamAudioChunk(Uint8List pcmData) {
    if (pcmData.isEmpty) return;

    // Gate the microphone before enqueueing playback so provider VAD cannot
    // hear Ella's own response audio and interrupt the active turn.
    _suspendMicForPlayback('audio_chunk');
    _deferPendingPlaybackFinish('audio_chunk');
    _chunkCount++;
    _pcmBuffer.add(pcmData);

    _streamFeedFuture = _streamFeedFuture.then((_) async {
      await _ensureStreamingPlaybackStarted();
      _streamPlayer.uint8ListSink?.add(pcmData);
    }).catchError((error) {
      Logger.error('[V2V] Stream playback feed error: $error');
      onEvent?.call(V2VEvent(type: 'error', text: 'Audio stream error: $error'));
    });

    if (_chunkCount == 1) {
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Streaming response audio'));
      Logger.debug('[V2V] First audio chunk, streaming playback gate active');
    }

    if (_chunkCount % 20 == 0) {
      Logger.debug('[V2V] Streamed $_chunkCount chunks, ${_pcmBuffer.length}B');
    }
  }

  Future<void> _ensureStreamingPlaybackStarted() async {
    if (!_streamPlayerOpen) {
      await _streamPlayer.openPlayer();
      _streamPlayerOpen = true;
    }

    if (_streamPlaybackStarted) return;

    _isPlaying = true;
    _streamPlaybackStarted = true;
    await _streamPlayer.startPlayerFromStream(
      codec: Codec.pcm16,
      sampleRate: _pcmSampleRate,
      numChannels: _pcmChannels,
      bufferSize: 4096,
    );
    _streamPlaybackStartedAt = DateTime.now();
    Logger.debug('[V2V] PCM stream player started');
  }

  Future<void> _stopStreamingPlayback() async {
    if (!_streamPlaybackStarted) return;
    try {
      await _streamPlayer.stopPlayer();
    } catch (_) {}
    _streamPlaybackStarted = false;
  }

  void _scheduleFinishPlayback(String reason) {
    _finishPlaybackTimer?.cancel();
    Logger.debug('[V2V] Playback finish scheduled after quiet period: reason=$reason');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Playback finish pending: $reason'));
    _finishPlaybackTimer = Timer(_playbackFinishQuietPeriod, () {
      _finishPlaybackTimer = null;
      Future<void>(() async {
        await _finishPlayback(reason: reason);
      });
    });
  }

  void _deferPendingPlaybackFinish(String reason) {
    if (_finishPlaybackTimer == null) return;
    _scheduleFinishPlayback(reason);
  }

  /// Called after audio/output completion quiets — wait for queued PCM to drain,
  /// then return to listening.
  Future<void> _finishPlayback({required String reason}) async {
    if (_finishingPlayback) {
      Logger.debug('[V2V] Playback finish already in progress, ignoring reason=$reason');
      return;
    }
    _finishingPlayback = true;
    final pcmBytes = _pcmBuffer.toBytes();
    final stats = '$_chunkCount chunks, ${(pcmBytes.length / 1024).toStringAsFixed(1)}KB';
    Logger.debug('[V2V] Audio stream done: reason=$reason $stats');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Finishing audio: $stats'));

    _pcmBuffer.clear();
    _chunkCount = 0;

    if (pcmBytes.isEmpty) {
      Logger.debug('[V2V] No audio data to play');
      await _onPlaybackComplete();
      _finishingPlayback = false;
      return;
    }

    try {
      await _streamFeedFuture.timeout(const Duration(seconds: 5));

      // `audio_done` means the proxy finished sending bytes, not that the
      // native PCM player has drained its internal queue. Stop too early and
      // the user hears only the first words even though transcript is complete.
      final startedAt = _streamPlaybackStartedAt;
      final totalAudioMs = (pcmBytes.length / (_pcmSampleRate * _pcmBytesPerSample * _pcmChannels) * 1000).ceil();
      final elapsedMs = startedAt == null ? 0 : DateTime.now().difference(startedAt).inMilliseconds;
      final remainingMs = totalAudioMs - elapsedMs;
      final drainDelayMs = remainingMs > 0 ? remainingMs + 300 : 300;
      Logger.debug('[V2V] Waiting ${drainDelayMs}ms for PCM drain ($totalAudioMs ms audio, $elapsedMs ms elapsed)');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Playing audio: ${(totalAudioMs / 1000).toStringAsFixed(1)}s'));
      await Future.delayed(Duration(milliseconds: drainDelayMs.clamp(300, 30000)));
      await _onPlaybackComplete();
    } catch (e) {
      Logger.error('[V2V] Stream playback error: $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Stream play error: $e'));
      _isPlaying = false;
      _streamPlaybackStartedAt = null;
      await _resumeMicAfterPlayback();
      onEvent?.call(const V2VEvent(type: 'playback_complete'));
    } finally {
      _finishingPlayback = false;
    }
  }

  /// Called when streaming playback finishes.
  Future<void> _onPlaybackComplete() async {
    if (!_isPlaying && !_micMuted) return; // Already handled
    Logger.debug('[V2V] Playback complete, restarting mic');
    await _stopStreamingPlayback();
    _isPlaying = false;
    _streamPlaybackStartedAt = null;
    await _resumeMicAfterPlayback();

    onEvent?.call(const V2VEvent(type: 'playback_complete'));
  }

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      // Binary frame = raw PCM16 audio from proxy — stream directly to playback.
      _streamAudioChunk(Uint8List.fromList(message));
      return;
    }

    if (message is String) {
      // JSON event
      Logger.debug('[V2V] JSON event: $message');
      try {
        final json = jsonDecode(message) as Map<String, dynamic>;
        final type = json['type'] as String? ?? 'unknown';
        final text = _eventText(json);

        if (_isUserTranscriptEvent(type)) {
          onEvent?.call(V2VEvent(type: 'user_transcript', text: text));
          return;
        }

        if (_isAssistantTranscriptEvent(type)) {
          _suspendMicForPlayback(type);
          _deferPendingPlaybackFinish(type);
          onEvent?.call(V2VEvent(type: 'transcript', text: text));
          return;
        }

        if (_isAudioDoneEvent(type)) {
          _scheduleFinishPlayback(type);
          onEvent?.call(const V2VEvent(type: 'audio_done'));
          return;
        }

        if (_isResponseCompleteEvent(type)) {
          _scheduleFinishPlayback(type);
          return;
        }

        switch (type) {
          case 'speech_started':
            if (_isPlaying || _micMuted || _micSuspendedForPlayback || _streamPlaybackStarted) {
              Logger.debug('[V2V] Ignoring speech_started while playback gate is active');
              onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Ignoring speech_started during response'));
              break;
            }
            interruptPlayback();
            onEvent?.call(const V2VEvent(type: 'speech_started'));
            break;
          case 'function_calling':
          case 'function_executed':
            onEvent?.call(V2VEvent(type: type, text: text ?? json.toString()));
            break;
          case 'memory_reinterpretation':
            final memoryReinterpretation = MemoryReinterpretationEvent.tryParse(json);
            if (memoryReinterpretation != null) {
              onEvent?.call(V2VEvent(type: type, memoryReinterpretation: memoryReinterpretation));
            }
            break;
          case 'quota_state':
            final quotaEvent = quotaEventFromEvent(json);
            if (quotaEvent != null) onEvent?.call(quotaEvent);
            break;
          case 'session_end':
            onEvent?.call(
              V2VEvent(
                type: 'session_end',
                text: text,
                policyReason: _eventPolicyReason(json),
                resetsAt: _eventResetsAt(json),
              ),
            );
            disconnect();
            break;
          case 'error':
            Logger.error('[V2V] Server error: ${json['message'] ?? text}');
            onEvent?.call(
              V2VEvent(
                type: 'error',
                text: json['message'] as String? ?? text,
                policyReason: _eventPolicyReason(json),
                resetsAt: _eventResetsAt(json),
              ),
            );
            break;
          default:
            Logger.debug('[V2V] Unknown event: $type');
            onEvent?.call(V2VEvent(type: type, text: text));
        }
      } catch (e) {
        Logger.debug('[V2V] Failed to parse JSON event: $e');
      }
    }
  }
}
