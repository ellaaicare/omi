import 'package:omi/ella/services/app_output_admission.dart';
import 'package:omi/ella/services/whisper_delivery_runtime.dart';
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:flutter/foundation.dart';
import 'package:omi/ella/services/admitted_audio_player.dart';
import 'package:just_audio/just_audio.dart' as sdk;
import 'package:record/record.dart';
import 'package:web_socket_channel/io.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/ella/services/ella_voice_audio_route.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/v2v_turn_reconciler.dart';
import 'package:omi/ella/services/grok_provider_readiness.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/services/mic/audio_capture_ownership.dart';

abstract interface class V2VMicrophoneRecorder {
  Future<void> useEllaManagedAudioSession();
  Future<bool> hasPermission();
  Future<Stream<Uint8List>> startStream(RecordConfig config);
  Future<String?> stop();
  Future<bool> isRecording();
  Future<void> dispose();
}

final class RecordV2VMicrophoneRecorder implements V2VMicrophoneRecorder {
  RecordV2VMicrophoneRecorder() : _recorder = AudioRecorder();

  final AudioRecorder _recorder;

  @override
  Future<void> useEllaManagedAudioSession() async {
    await _recorder.ios?.manageAudioSession(false);
  }

  @override
  Future<bool> hasPermission() => _recorder.hasPermission();

  @override
  Future<Stream<Uint8List>> startStream(RecordConfig config) => _recorder.startStream(config);

  @override
  Future<String?> stop() => _recorder.stop();

  @override
  Future<bool> isRecording() => _recorder.isRecording();

  @override
  Future<void> dispose() => _recorder.dispose();
}

/// Exact physical output receipts; local playing flags do not establish silence.
abstract interface class V2VOutputPlayer {
  Future<void> setVolume(double volume);
  Future<void> setFilePath(String path);
  Future<void> play();
  Future<bool> stopAndConfirm();
  Future<bool> disposeAndConfirm();
  bool get reusableAfterStop;
}

final class JustAudioV2VOutputPlayer implements V2VOutputPlayer {
  JustAudioV2VOutputPlayer({@visibleForTesting sdk.AudioPlayer? player}) : _player = player ?? AudioPlayer();
  final sdk.AudioPlayer _player;
  Future<bool>? _disposal;
  bool _disposedConfirmed = false;
  bool _disposalFailed = false;
  @override
  bool get reusableAfterStop => false;
  @override
  Future<void> setVolume(double volume) => _player.setVolume(volume);
  @override
  Future<void> setFilePath(String path) async {
    await _player.setFilePath(path);
  }

  @override
  Future<void> play() => _player.play();
  @override
  Future<bool> stopAndConfirm() => disposeAndConfirm();
  @override
  Future<bool> disposeAndConfirm() {
    if (_disposedConfirmed) return Future.value(true);
    // Stock dispose marks its local object disposed before native acknowledgement.
    // After failure a stock retry is a no-op, not evidence of physical silence.
    if (_disposalFailed) return Future.value(false);
    return _disposal ??= (() async {
      try {
        await _player.dispose();
        _disposedConfirmed = true;
        return true;
      } catch (_) {
        _disposalFailed = true;
        rethrow;
      }
    })();
  }
}

class _V2VOutputLifetime {
  _V2VOutputLifetime(this.lease) : outputPermit = AppOutputAdmission.shared.reserveOutput('v2v-session');
  final AppOutputPermit outputPermit;
  final AudioCaptureLease lease;
  V2VOutputPlayer? player;
  Future<void>? setup;
  Future<void>? teardown;
  bool retiring = false;
  bool stopping = false;
  Future<void>? playerStop;
  bool failed = false;
  bool routeAttempted = false;
  bool routeReleased = false;
  Future<void>? injectedPlayback;
}

enum V2VSessionScopeKind { memory, dailyCard }

@immutable
class V2VSessionScope {
  const V2VSessionScope.memory({required this.conversationId, this.expectedActiveSummaryVersionId})
      : assert(conversationId != ''),
        kind = V2VSessionScopeKind.memory,
        cardId = '',
        expectedVersion = 0;

  const V2VSessionScope.dailyCard({required this.cardId, required this.expectedVersion})
      : assert(cardId != ''),
        assert(expectedVersion > 0),
        kind = V2VSessionScopeKind.dailyCard,
        conversationId = '',
        expectedActiveSummaryVersionId = null;

  final V2VSessionScopeKind kind;
  final String conversationId;
  final String? expectedActiveSummaryVersionId;
  final String cardId;
  final int expectedVersion;

  Map<String, dynamic> toJson() => switch (kind) {
        V2VSessionScopeKind.memory => {
            'kind': kind.name,
            'conversation_id': conversationId,
            if (expectedActiveSummaryVersionId?.isNotEmpty == true)
              'expected_active_summary_version_id': expectedActiveSummaryVersionId,
          },
        V2VSessionScopeKind.dailyCard => {'kind': 'daily_card', 'card_id': cardId, 'expected_version': expectedVersion},
      };

  V2VSessionScope withExpectedActiveSummaryVersionId(String? value) =>
      V2VSessionScope.memory(conversationId: conversationId, expectedActiveSummaryVersionId: value);
}

@immutable
class V2VResolvedSessionScope {
  const V2VResolvedSessionScope({
    required this.kind,
    this.conversationId = '',
    this.activeSummaryVersionId = '',
    this.cardId = '',
    this.cardVersion = 0,
    this.canReinterpret = false,
  });

  final V2VSessionScopeKind kind;
  final String conversationId;
  final String activeSummaryVersionId;
  final String cardId;
  final int cardVersion;
  final bool canReinterpret;

  static V2VResolvedSessionScope? tryParse(Object? value) {
    if (value is! Map) return null;
    final kind = value['kind']?.toString();
    final canReinterpret = value['can_reinterpret'];
    if (canReinterpret is! bool) return null;
    if (kind == V2VSessionScopeKind.memory.name) {
      final conversationId = value['conversation_id']?.toString().trim() ?? '';
      final activeSummaryVersionId = value['active_summary_version_id']?.toString().trim() ?? '';
      if (conversationId.isEmpty || activeSummaryVersionId.isEmpty) return null;
      return V2VResolvedSessionScope(
        kind: V2VSessionScopeKind.memory,
        conversationId: conversationId,
        activeSummaryVersionId: activeSummaryVersionId,
        canReinterpret: canReinterpret,
      );
    }
    if (kind == 'daily_card') {
      final cardId = value['card_id']?.toString().trim() ?? '';
      final cardVersion = value['card_version'];
      if (cardId.isEmpty || cardVersion is! num || cardVersion.toInt() < 1) return null;
      return V2VResolvedSessionScope(
        kind: V2VSessionScopeKind.dailyCard,
        conversationId: value['conversation_id']?.toString().trim() ?? '',
        activeSummaryVersionId: value['active_summary_version_id']?.toString().trim() ?? '',
        cardId: cardId,
        cardVersion: cardVersion.toInt(),
        canReinterpret: canReinterpret,
      );
    }
    return null;
  }

  bool matches(V2VSessionScope requested) => switch (kind) {
        V2VSessionScopeKind.memory => requested.kind == kind && conversationId == requested.conversationId,
        V2VSessionScopeKind.dailyCard =>
          requested.kind == kind && cardId == requested.cardId && cardVersion == requested.expectedVersion,
      };
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
    this.terminalTurn,
  });

  final String type;
  final String? text;
  final EllaVoicePolicyReason? policyReason;
  final DateTime? resetsAt;
  final MemoryReinterpretationEvent? memoryReinterpretation;
  final String? quotaState;
  final EllaQuota? quota;
  final bool turnBoundary;
  final V2VTerminalTranscriptTurn? terminalTurn;
}

enum V2VConnectionStage {
  consent,
  identity,
  providerRegistry,
  session,
  audioSession,
  websocket,
  microphone,
  providerReady,
  connected
}

enum V2VProtectedEgressBoundary { providerRegistry, session, websocket, microphoneCapture, microphoneFrame }

typedef V2VBeforeTransportActivation = Future<bool> Function(String sessionId);
typedef V2VTerminalTurnDurableHandler = Future<bool> Function(V2VTerminalTranscriptTurn turn);

class V2VWebSocketTransport {
  const V2VWebSocketTransport({
    required this.ready,
    required this.stream,
    required this.send,
    required this.close,
  });

  final Future<void> ready;
  final Stream<dynamic> stream;
  final void Function(dynamic data) send;
  final Future<void> Function() close;
}

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

  bool get shouldRefreshSessionScope => stage == V2VConnectionStage.session && errorCode == 'voice_session_scope_stale';

  @Deprecated('Use shouldRefreshSessionScope for both memory and daily-card scopes.')
  bool get shouldRefreshMemoryScope => shouldRefreshSessionScope;
  EllaVoicePolicyReason? get policyReason => parseEllaVoicePolicyReason(errorCode);
  bool get isPolicyDenial => policyReason != null;
  bool get isPermanentScopedFailure => const {
        'ai_consent_required',
        'consent_authority_lost',
        'invalid_session_contract',
        'invalid_session_scope',
        'memory_scope_provider_unsupported',
        'missing_authenticated_identity',
        'provider_mismatch',
        'unsupported_provider',
        'voice_session_scope_stale',
        'voice_session_scope_unavailable',
      }.contains(errorCode);

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
/// Uses `record` for mic input and `just_audio` for deterministic WAV playback.
/// The mic recorder is suspended during playback so provider VAD cannot hear
/// Ella's own response audio or post-turn background noise.
class V2VClient {
  static const int _pcmSampleRate = 24000;
  static const int _pcmBytesPerSample = 2;
  static const int _pcmChannels = 1;
  static const Duration _playbackFinishQuietPeriod = Duration(milliseconds: 900);

  static V2VClient? _activeClient;

  static Future<void> disconnectActiveForAccountTransition() async {
    final active = _activeClient;
    if (active != null) await active.disconnect();
    _activeClient = null;
  }

  V2VWebSocketTransport? _channel;
  V2VMicrophoneRecorder? _recorder;
  AudioCaptureLease? _audioCallLease;
  AudioCaptureTransport? _micTransport;
  StreamSubscription? _micSub;
  StreamSubscription? _wsSub;
  AiConsentActiveSessionLease? _aiConsentLease;
  AiConsentAuthoritySnapshot? _activeConsentAuthority;
  bool Function()? _sessionShouldContinue;
  bool _handlingAuthorityLoss = false;
  bool _isConnected = false;
  GrokProviderReadiness? _providerReadiness;
  bool _requiresProviderReady = false;
  bool _providerAcknowledged = false;
  bool _connectionAnnounced = false;
  bool _isPlaying = false;
  bool _micMuted = false;
  bool _micSuspendedForPlayback = false;
  int _micChunksSent = 0;
  int _micBytesSent = 0;
  Future<void> _micGateFuture = Future.value();
  Object? _micGateFailure;
  Future<void> _terminalTurnTail = Future.value();
  String _connectedSessionId = '';

  /// PCM buffer for accumulating audio chunks before WAV playback
  final BytesBuilder _pcmBuffer = BytesBuilder(copy: false);
  int _chunkCount = 0;

  _V2VOutputLifetime? _outputLifetime;
  final Map<AudioCaptureLease, _V2VOutputLifetime> _outputLifetimes = {};
  Future<void> _playbackFuture = Future.value();
  int _playbackGeneration = 0;
  Timer? _finishPlaybackTimer;
  bool _finishingPlayback = false;

  /// Callback for JSON events (transcripts, errors, etc.)
  final void Function(V2VEvent event)? onEvent;

  /// Returns true only after the exact terminal turn is durably owned by the app.
  final V2VTerminalTurnDurableHandler? onTerminalTurnDurable;

  /// Callback for connection state changes.
  final void Function(bool connected)? onConnectionChanged;

  final Future<void> Function(V2VProtectedEgressBoundary boundary)? _beforeProtectedEgress;
  final void Function(V2VProtectedEgressBoundary boundary)? _onProtectedEgress;
  final Future<bool> Function()? _microphoneStarter;
  final V2VMicrophoneRecorder Function() _microphoneRecorderFactory;
  final Future<bool> Function() _interactiveOutputEnforcer;
  final Future<bool> Function() _playbackOutputEnforcer;
  final Future<void> Function(Uint8List wavBytes)? _wavPlayback;
  final bool Function()? _liveChannelForTesting;
  final Future<V2VConnectionReceipt?> Function(String provider)? _providerRegistryValidator;
  final Future<Map<String, dynamic>> Function(String uid, String provider, V2VSessionScope? sessionScope)?
      _sessionCreator;
  final Future<void> Function()? _audioSessionConfigurator;
  final Future<void> Function()? _audioSessionReleaser;
  final Future<bool> Function()? _audioSessionReleaseReceipt;
  final V2VOutputPlayer Function() _outputPlayerFactory;
  final Future<bool> Function()? _injectedPlaybackStopReceipt;
  final V2VWebSocketTransport Function(Uri uri)? _webSocketConnector;
  final Duration _playbackMicCooldown;
  final Duration _providerReadinessTimeout;

  V2VClient({
    this.onEvent,
    this.onTerminalTurnDurable,
    this.onConnectionChanged,
    @visibleForTesting Future<void> Function(V2VProtectedEgressBoundary boundary)? beforeProtectedEgress,
    @visibleForTesting void Function(V2VProtectedEgressBoundary boundary)? onProtectedEgress,
    @visibleForTesting Future<bool> Function()? microphoneStarter,
    @visibleForTesting V2VMicrophoneRecorder Function()? microphoneRecorderFactory,
    @visibleForTesting Future<bool> Function()? audibleOutputEnforcer,
    @visibleForTesting Future<bool> Function()? playbackOutputEnforcer,
    @visibleForTesting Future<void> Function(Uint8List wavBytes)? wavPlayback,
    @visibleForTesting bool Function()? liveChannelForTesting,
    @visibleForTesting Future<V2VConnectionReceipt?> Function(String provider)? providerRegistryValidator,
    @visibleForTesting
    Future<Map<String, dynamic>> Function(String uid, String provider, V2VSessionScope? sessionScope)? sessionCreator,
    @visibleForTesting Future<void> Function()? audioSessionConfigurator,
    @visibleForTesting Future<void> Function()? audioSessionReleaser,
    @visibleForTesting Future<bool> Function()? audioSessionReleaseReceipt,
    @visibleForTesting V2VOutputPlayer Function()? outputPlayerFactory,
    @visibleForTesting Future<bool> Function()? injectedPlaybackStopReceipt,
    @visibleForTesting V2VWebSocketTransport Function(Uri uri)? webSocketConnector,
    @visibleForTesting Duration playbackMicCooldown = const Duration(seconds: 2),
    @visibleForTesting Duration providerReadinessTimeout = const Duration(seconds: 16),
  })  : _beforeProtectedEgress = beforeProtectedEgress,
        _onProtectedEgress = onProtectedEgress,
        _microphoneStarter = microphoneStarter,
        _microphoneRecorderFactory = microphoneRecorderFactory ?? RecordV2VMicrophoneRecorder.new,
        _interactiveOutputEnforcer = audibleOutputEnforcer ??
            (() => EllaVoiceAudioRoute.ensureAudibleOutput(usage: EllaVoiceAudioUsage.interactive)),
        _playbackOutputEnforcer = playbackOutputEnforcer ??
            audibleOutputEnforcer ??
            (() => EllaVoiceAudioRoute.ensureAudibleOutput(usage: EllaVoiceAudioUsage.playback)),
        _wavPlayback = wavPlayback,
        _liveChannelForTesting = liveChannelForTesting,
        _providerRegistryValidator = providerRegistryValidator,
        _sessionCreator = sessionCreator,
        _audioSessionConfigurator = audioSessionConfigurator,
        _audioSessionReleaser = audioSessionReleaser,
        _audioSessionReleaseReceipt = audioSessionReleaseReceipt,
        _outputPlayerFactory = outputPlayerFactory ?? JustAudioV2VOutputPlayer.new,
        _injectedPlaybackStopReceipt = injectedPlaybackStopReceipt,
        _webSocketConnector = webSocketConnector,
        _playbackMicCooldown = playbackMicCooldown,
        _providerReadinessTimeout = providerReadinessTimeout;

  bool get isConnected => _isConnected;

  @visibleForTesting
  int get micChunksSentForTesting => _micChunksSent;

  @visibleForTesting
  bool get hasActiveConsentLeaseForTesting => _aiConsentLease?.isActive == true;

  @visibleForTesting
  bool get micMutedForTesting => _micMuted;

  @visibleForTesting
  bool get micSuspendedForPlaybackForTesting => _micSuspendedForPlayback;

  @visibleForTesting
  void markConnectedForTesting() => _isConnected = true;

  @visibleForTesting
  void bufferAudioChunkForTesting(Uint8List pcmData) => _bufferAudioChunk(pcmData);

  @visibleForTesting
  Future<void> waitForPlaybackForTesting() => _playbackFuture;

  @visibleForTesting
  Future<void> finishPlaybackForTesting() => _finishPlayback(reason: 'test');

  @visibleForTesting
  void handleMessageForTesting(dynamic message) => _handleMessage(message);

  @visibleForTesting
  Future<void> settleTerminalTurnsForTesting() => _terminalTurnTail;

  V2VConnectionReceipt? get lastConnectionReceipt => _lastConnectionReceipt;

  V2VConnectionReceipt? _lastConnectionReceipt;
  Future<void>? _disconnectFuture;

  bool _hasCurrentSessionAuthority() {
    final authority = _activeConsentAuthority;
    return authority != null &&
        _audioCallLease?.isCurrent == true &&
        authority.isCurrent() &&
        (_sessionShouldContinue == null || _sessionShouldContinue!.call());
  }

  Future<bool> _authorizeProtectedEgress(V2VProtectedEgressBoundary boundary) async {
    await _beforeProtectedEgress?.call(boundary);
    if (!_hasCurrentSessionAuthority()) return false;
    _onProtectedEgress?.call(boundary);
    return true;
  }

  void _reportUnconfirmedOutput() {
    // Diagnostics are observers, never part of a physical teardown receipt.
    // Neither a logger nor an application callback may strand the exact owner.
    try {
      Logger.error('[V2V] Output teardown unconfirmed; exact owner retained');
    } catch (_) {}
    try {
      onEvent?.call(const V2VEvent(type: 'error', text: 'Voice audio playback failed'));
    } catch (_) {}
  }

  void _disconnectAfterEvent() {
    unawaited(disconnect().catchError((Object _) => _reportUnconfirmedOutput()));
  }

  Future<void> _handleRuntimeAuthorityLoss() async {
    if (_handlingAuthorityLoss) return;
    _handlingAuthorityLoss = true;
    onEvent?.call(const V2VEvent(type: 'consent_authority_lost'));
    try {
      await disconnect();
    } catch (_) {
      _reportUnconfirmedOutput();
    } finally {
      _handlingAuthorityLoss = false;
    }
  }

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
      if (canonicalProvider == 'grok-voice') 'ready_protocol': 1,
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
  static bool registrySupportsGrokReady(Object? data) {
    if (data is! Map || data['providers'] is! List) return false;
    return (data['providers'] as List).whereType<Map>().any((p) =>
        p['id'] == 'grok-voice' &&
        p['available'] == true &&
        p['type'] == 'v2v' &&
        p['ready_protocols'] is List &&
        (p['ready_protocols'] as List).any((v) => v is int && v == 1));
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
  bool _reserveAudioCall(AiConsentAuthoritySnapshot authority, bool Function()? shouldContinue) {
    final existing = _audioCallLease;
    if (existing != null && existing.isOwner) return existing.isCurrent;
    _audioCallLease = AudioCaptureOwnership.shared.reserve(
      source: 'v2v-call',
      hasAuthority: () => authority.isCurrent() && (shouldContinue == null || shouldContinue()),
    );
    if (_audioCallLease != null) _registerOutputLifetime(_audioCallLease!);
    return _audioCallLease != null;
  }

  Future<V2VConnectionReceipt> connect({
    required String provider,
    V2VSessionScope? sessionScope,
    bool Function()? shouldContinue,
    V2VBeforeTransportActivation? beforeTransportActivation,
  }) {
    final authority = AiConsentActiveSessionLease.authorityForSessionStart(expectedUid: SharedPreferencesUtil().uid);
    Future<void>? precedingCleanup;
    if (authority != null) {
      final retiringClient = _disconnectFuture != null ? this : _activeClient;
      final retiringLease = retiringClient?._audioCallLease;
      precedingCleanup = retiringClient?._disconnectFuture;
      if (precedingCleanup != null && retiringLease?.isOwner == true) {
        _audioCallLease = retiringLease!.reserveSuccessor(
          source: 'v2v-call',
          hasAuthority: () => authority.isCurrent() && (shouldContinue == null || shouldContinue()),
        );
      } else if (_audioCallLease?.isOwner == true || !_reserveAudioCall(authority, shouldContinue)) {
        return Future.value(_completeReceipt(V2VConnectionReceipt(
          connected: false,
          provider: normalizeProvider(provider),
          stage: V2VConnectionStage.identity,
          errorCode: 'audio_capture_busy',
        )));
      }
      if (_audioCallLease == null) {
        return Future.value(_completeReceipt(V2VConnectionReceipt(
          connected: false,
          provider: normalizeProvider(provider),
          stage: V2VConnectionStage.identity,
          errorCode: 'audio_capture_busy',
        )));
      }
    }
    final reservation = _audioCallLease;
    if (reservation != null) _registerOutputLifetime(reservation);
    Future<V2VConnectionReceipt> startAfterAdmission() async {
      if (reservation != null && !await reservation.admitted) {
        return V2VConnectionReceipt(
            connected: false,
            provider: normalizeProvider(provider),
            stage: V2VConnectionStage.identity,
            errorCode: 'audio_capture_cancelled');
      }
      if (precedingCleanup != null) await precedingCleanup;
      return _connectOwned(
          provider: provider,
          sessionScope: sessionScope,
          shouldContinue: shouldContinue,
          beforeTransportActivation: beforeTransportActivation);
    }

    return startAfterAdmission().then((receipt) async {
      if (!receipt.connected && reservation != null) {
        await reservation.stop();
        if (identical(_audioCallLease, reservation)) _audioCallLease = null;
      }
      return receipt;
    }, onError: (Object error, StackTrace stack) async {
      await reservation?.stop();
      if (identical(_audioCallLease, reservation)) _audioCallLease = null;
      Error.throwWithStackTrace(error, stack);
    });
  }

  Future<V2VConnectionReceipt> _connectOwned({
    required String provider,
    V2VSessionScope? sessionScope,
    bool Function()? shouldContinue,
    V2VBeforeTransportActivation? beforeTransportActivation,
  }) async {
    final pendingDisconnect = _disconnectFuture;
    if (pendingDisconnect != null) await pendingDisconnect;
    provider = normalizeProvider(provider);
    _lastConnectionReceipt = null;

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

    final authority = AiConsentActiveSessionLease.authorityForSessionStart(expectedUid: uid);
    if (authority == null) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.consent,
          errorCode: 'ai_consent_required',
        ),
      );
    }
    _activeConsentAuthority = authority;
    _sessionShouldContinue = () => (shouldContinue == null || shouldContinue()) && authority.isCurrent();

    Future<V2VConnectionReceipt> stopForAuthorityLoss({String voiceMode = ''}) async {
      await disconnect();
      return _completeReceipt(
        V2VConnectionReceipt(
          connected: false,
          provider: provider,
          voiceMode: voiceMode,
          stage: V2VConnectionStage.consent,
          errorCode: 'consent_authority_lost',
        ),
      );
    }

    Future<V2VConnectionReceipt?> stopIfStartupInvalid(V2VConnectionStage stage, {String voiceMode = ''}) async {
      final cancellation = await cancelIfRequested(stage, voiceMode: voiceMode);
      if (cancellation != null) return cancellation;
      if (!authority.isCurrent()) return stopForAuthorityLoss(voiceMode: voiceMode);
      return null;
    }

    if (!await _authorizeProtectedEgress(V2VProtectedEgressBoundary.providerRegistry)) {
      final invalid = await stopIfStartupInvalid(V2VConnectionStage.providerRegistry);
      if (invalid != null) return invalid;
      return stopForAuthorityLoss();
    }
    final registryFailure = await (_providerRegistryValidator?.call(provider) ?? _validateProviderRegistry(provider));
    final registryInvalid = await stopIfStartupInvalid(V2VConnectionStage.providerRegistry);
    if (registryInvalid != null) return registryInvalid;
    if (registryFailure != null) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(registryFailure);
    }

    // 1. Get session token from backend
    if (!await _authorizeProtectedEgress(V2VProtectedEgressBoundary.session)) {
      final invalid = await stopIfStartupInvalid(
        V2VConnectionStage.session,
        voiceMode: sessionVoiceMode(provider, memoryScoped: sessionScope != null) ?? '',
      );
      if (invalid != null) return invalid;
      return stopForAuthorityLoss();
    }
    final sessionResult = _sessionCreator == null
        ? await _createSession(uid, provider, sessionScope)
        : _V2VSessionResult(
            data: await _sessionCreator!(uid, provider, sessionScope),
            receipt: V2VConnectionReceipt(
              connected: false,
              provider: provider,
              stage: V2VConnectionStage.session,
            ),
          );
    final sessionInvalid = await stopIfStartupInvalid(
      V2VConnectionStage.session,
      voiceMode: sessionVoiceMode(provider, memoryScoped: sessionScope != null) ?? '',
    );
    if (sessionInvalid != null) return sessionInvalid;
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
    final requiresReady = provider == 'grok-voice';
    Uri? issuedEndpoint;
    try {
      issuedEndpoint = Uri.parse(endpoint);
    } catch (_) {}
    final issuedProtocols = issuedEndpoint?.queryParametersAll['ready_protocol'];
    if (requiresReady &&
        (sessionData['ready_protocol'] is! int ||
            sessionData['ready_protocol'] != 1 ||
            issuedProtocols == null ||
            issuedProtocols.length != 1 ||
            issuedProtocols.single != '1')) {
      if (_activeClient == this) _activeClient = null;
      return _completeReceipt(V2VConnectionReceipt(
          connected: false,
          provider: provider,
          stage: V2VConnectionStage.session,
          errorCode: 'provider_ready_protocol_unsupported'));
    }

    if (token.isEmpty || endpoint.isEmpty || sessionId.isEmpty) {
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
      await _withOutputSetup(() => _audioSessionConfigurator?.call() ?? _configureAudioSession());
      final invalid = await stopIfStartupInvalid(V2VConnectionStage.audioSession, voiceMode: confirmedVoiceMode);
      if (invalid != null) return invalid;
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
      if (!await _authorizeProtectedEgress(V2VProtectedEgressBoundary.websocket)) {
        final invalid = await stopIfStartupInvalid(V2VConnectionStage.websocket, voiceMode: confirmedVoiceMode);
        if (invalid != null) return invalid;
        return stopForAuthorityLoss(voiceMode: confirmedVoiceMode);
      }
      _channel = _webSocketConnector?.call(Uri.parse(wsUrl)) ?? _connectWebSocket(Uri.parse(wsUrl));
      final connectedChannel = _channel!;

      await connectedChannel.ready.timeout(const Duration(seconds: 12));
      final websocketInvalid = await stopIfStartupInvalid(V2VConnectionStage.websocket, voiceMode: confirmedVoiceMode);
      if (websocketInvalid != null) return websocketInvalid;

      _requiresProviderReady = requiresReady;
      _providerAcknowledged = !requiresReady;
      final readiness = requiresReady
          ? GrokProviderReadiness(
              timeout: _providerReadinessTimeout,
              sessionId: sessionId,
              isCurrent: () => identical(_channel, connectedChannel) && _hasCurrentSessionAuthority())
          : null;
      _providerReadiness = readiness;
      GrokProviderReadyResult? readyResult;
      var listenerAttached = false;
      final micStarted = await activateV2VTransportInOrder(
        armSession: () async {
          if (beforeTransportActivation != null && !await beforeTransportActivation(sessionId)) return false;
          return _hasCurrentSessionAuthority();
        },
        attachListener: () {
          // Registering the listener can synchronously release buffered proxy frames. The
          // caller's session/outbox must therefore be armed before this point.
          listenerAttached = true;
          _isConnected = !requiresReady;
          _connectedSessionId = sessionId;
          _wsSub = connectedChannel.stream.listen(
            (message) {
              if (identical(_channel, connectedChannel) && _hasCurrentSessionAuthority()) _handleMessage(message);
            },
            onError: (error) {
              if (!identical(_channel, connectedChannel)) return;
              readiness?.fail('provider_startup_failed');
              Logger.error('[V2V] WebSocket error: ${error.runtimeType}');
              _disconnectAfterEvent();
            },
            onDone: () {
              if (!identical(_channel, connectedChannel)) return;
              readiness?.fail('provider_startup_client_closed');
              Logger.debug('[V2V] WebSocket closed');
              _disconnectAfterEvent();
            },
          );
        },
        startMicrophone: () async {
          if (readiness != null) {
            await readiness.result;
            readyResult = readiness.outcome;
            if (!readyResult!.ready || !identical(_channel, connectedChannel) || !_hasCurrentSessionAuthority()) {
              return false;
            }
            _providerAcknowledged = true;
          }
          final continuation = _sessionShouldContinue;
          if (continuation == null) return false;
          final started = await _startAuthorizedMicrophone(authority: authority, shouldContinue: continuation);
          if (readiness != null) readyResult = readiness.outcome;
          if (requiresReady &&
              readyResult?.ready == true &&
              started &&
              identical(_channel, connectedChannel) &&
              _hasCurrentSessionAuthority()) {
            _isConnected = true;
          }
          return started;
        },
      );
      if (readiness != null) readyResult = readiness.outcome;
      if (readyResult != null && !readyResult!.ready) {
        await disconnect();
        return _completeReceipt(V2VConnectionReceipt(
            connected: false,
            provider: provider,
            voiceMode: confirmedVoiceMode,
            stage: V2VConnectionStage.providerReady,
            httpStatus: readyResult!.httpStatus,
            errorCode: readyResult!.errorCode));
      }
      final microphoneInvalid = await stopIfStartupInvalid(
        V2VConnectionStage.microphone,
        voiceMode: confirmedVoiceMode,
      );
      if (microphoneInvalid != null) return microphoneInvalid;
      if (!listenerAttached || !micStarted || !_isConnected) {
        await disconnect();
        return _completeReceipt(
          V2VConnectionReceipt(
            connected: false,
            provider: provider,
            voiceMode: confirmedVoiceMode,
            stage: !listenerAttached || micStarted ? V2VConnectionStage.websocket : V2VConnectionStage.microphone,
            errorCode: !listenerAttached
                ? 'session_activation_failed'
                : micStarted
                    ? 'websocket_closed'
                    : 'microphone_unavailable',
          ),
        );
      }
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
  Future<void> disconnect() {
    final pending = _disconnectFuture;
    if (pending != null) return pending;
    final cleanup = Completer<void>();
    _disconnectFuture = cleanup.future;
    Future<void> runCleanup() async {
      try {
        await _disconnectResources();
        cleanup.complete();
      } catch (error, stackTrace) {
        cleanup.completeError(error, stackTrace);
      } finally {
        if (_activeClient == this && _audioCallLease?.isOwner != true) _activeClient = null;
        _disconnectFuture = null;
      }
    }

    unawaited(runCleanup());
    return cleanup.future;
  }

  Future<void> _disconnectResources() async {
    final reservation = _audioCallLease;
    _providerReadiness?.fail('connect_cancelled');
    _providerReadiness = null;
    _providerAcknowledged = false;
    _requiresProviderReady = false;
    final consentLease = _aiConsentLease;
    _aiConsentLease = null;
    consentLease?.stop();
    _activeConsentAuthority = null;
    _sessionShouldContinue = null;
    final shouldAnnounceDisconnect = _connectionAnnounced;
    _isConnected = false;
    _connectedSessionId = '';
    _connectionAnnounced = false;
    if (shouldAnnounceDisconnect) onConnectionChanged?.call(false);
    await _wsSub?.cancel();
    _wsSub = null;

    try {
      await _channel?.close();
    } catch (_) {}
    _channel = null;

    await _stopMicStream(reason: 'disconnect');
    if (reservation != null) {
      await _teardownOutputLifetime(_lifetimeFor(reservation));
    } else if (_outputLifetime != null) {
      await _teardownOutputLifetime(_outputLifetime!);
    } else if (_audioSessionReleaser != null) {
      await _audioSessionReleaser!(); // Existing no-route fixture, no production effect.
    }
    await reservation?.stop();
    if (identical(_audioCallLease, reservation)) _audioCallLease = null;
    if (identical(_outputLifetime?.lease, reservation)) _outputLifetime = null;
    _outputLifetimes.remove(reservation);
    _resetTurnState();
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    _playbackGeneration++;
    _finishPlaybackTimer?.cancel();
    _finishPlaybackTimer = null;
    final shouldResumeMic = _micSuspendedForPlayback;
    await _stopAudioPlayback();
    _isPlaying = false;
    _pcmBuffer.clear();
    _chunkCount = 0;
    if (shouldResumeMic) {
      await _resumeMicAfterPlayback();
    } else {
      _micMuted = false;
    }
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
      // Keep the future observed, but do not turn an ambiguous physical stop
      // into permission to play or restart another microphone transport.
      _micGateFailure = error;
      Logger.error('[V2V] Mic gate close failed: ${error.runtimeType}');
      onEvent?.call(const V2VEvent(type: 'error', text: 'Voice microphone teardown failed'));
    });
  }

  void _resetTurnState() {
    _playbackGeneration++;
    _isPlaying = false;
    _micMuted = false;
    _micSuspendedForPlayback = false;
    _pcmBuffer.clear();
    _chunkCount = 0;
    _micChunksSent = 0;
    _micBytesSent = 0;
    _playbackFuture = Future.value();
    _finishPlaybackTimer?.cancel();
    _finishPlaybackTimer = null;
    _finishingPlayback = false;
    _micGateFuture = Future.value();
    _micGateFailure = null;
  }

  // --- Audio session ---

  V2VWebSocketTransport _connectWebSocket(Uri uri) {
    final channel = IOWebSocketChannel.connect(uri, pingInterval: const Duration(seconds: 30));
    return V2VWebSocketTransport(
      ready: channel.ready,
      stream: channel.stream,
      send: channel.sink.add,
      close: () async {
        await channel.sink.close();
      },
    );
  }

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
    if (!await _interactiveOutputEnforcer()) {
      throw StateError('Ella voice output route could not be made audible');
    }
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
        if (provider == 'grok-voice') {
          return V2VConnectionReceipt(
              connected: false,
              provider: provider,
              stage: V2VConnectionStage.providerRegistry,
              httpStatus: response?.statusCode,
              errorCode: 'registry_unavailable');
        }
        return null;
      }

      final decodedRegistry = jsonDecode(response.body);
      if (provider == 'grok-voice' && !registrySupportsGrokReady(decodedRegistry)) {
        return V2VConnectionReceipt(
            connected: false,
            provider: provider,
            stage: V2VConnectionStage.providerRegistry,
            httpStatus: response.statusCode,
            errorCode: 'provider_ready_protocol_unsupported');
      }
      final providers = availableSessionProviders(decodedRegistry);
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
      if (provider == 'grok-voice') {
        return V2VConnectionReceipt(
            connected: false,
            provider: provider,
            stage: V2VConnectionStage.providerRegistry,
            errorCode: 'registry_unavailable');
      }
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
    return type.toLowerCase() == 'user_transcript';
  }

  static bool _isAssistantTranscriptEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'transcript';
  }

  @visibleForTesting
  static bool treatsAsAssistantTranscriptEvent(String type) {
    return _isAssistantTranscriptEvent(type);
  }

  static bool _isAudioDoneEvent(String type) {
    return type.toLowerCase() == 'audio_done';
  }

  @visibleForTesting
  static bool treatsAsAudioDoneEvent(String type) {
    return _isAudioDoneEvent(type);
  }

  // --- Mic recording (PCM16, 24kHz, mono) using `record` package ---

  @visibleForTesting
  Future<bool> startAuthorizedMicrophoneForTesting({
    required AiConsentAuthoritySnapshot authority,
    required bool Function() shouldContinue,
  }) =>
      _startAuthorizedMicrophone(authority: authority, shouldContinue: shouldContinue);

  Future<bool> _startAuthorizedMicrophone({
    required AiConsentAuthoritySnapshot authority,
    required bool Function() shouldContinue,
  }) async {
    if (_outputLifetime?.failed == true || (_outputLifetime?.retiring == true || _outputLifetime?.stopping == true))
      return false;
    if (!_reserveAudioCall(authority, shouldContinue)) return false;
    _activeConsentAuthority = authority;
    _sessionShouldContinue = shouldContinue;
    _aiConsentLease ??= AiConsentActiveSessionLease(
      uid: authority.uid,
      authority: authority,
      onAuthorityLost: _handleRuntimeAuthorityLoss,
    )..start();

    if (_aiConsentLease?.isActive != true ||
        !await _authorizeProtectedEgress(V2VProtectedEgressBoundary.microphoneCapture)) {
      _aiConsentLease?.stop();
      _aiConsentLease = null;
      return false;
    }

    final started = await (_microphoneStarter?.call() ?? _startMicStream());
    if (!started || !_hasCurrentSessionAuthority()) {
      await _stopMicStream(reason: 'consent_authority_lost_during_start');
      _aiConsentLease?.stop();
      _aiConsentLease = null;
      return false;
    }
    _micMuted = false;
    _micSuspendedForPlayback = false;
    return true;
  }

  Future<bool> _startMicStream() async {
    AudioCaptureTransport? transport;
    V2VMicrophoneRecorder? recorder;
    try {
      if (!_hasCurrentSessionAuthority() || _aiConsentLease?.isActive != true) return false;
      if (_recorder != null || _micSub != null) return _micTransport?.isCurrent == true;
      transport = _audioCallLease!.createTransport();
      _micTransport = transport;
      recorder = _microphoneRecorderFactory();
      _recorder = recorder;
      await recorder.useEllaManagedAudioSession();
      if (!transport.isCurrent || !identical(_recorder, recorder)) {
        await _stopMicStream(reason: 'cancelled_before_permission', expectedTransport: transport);
        return false;
      }
      final hasPermission = whisperDeliveryProtocol1
          ? await WhisperDeliveryRuntime.shared.withoutWhisperOutput(() => recorder!.hasPermission())
          : await recorder.hasPermission();
      if (!hasPermission || !transport.isCurrent || !_hasCurrentSessionAuthority()) {
        await _stopMicStream(reason: 'permission_or_authority_unavailable', expectedTransport: transport);
        return false;
      }
      late final Stream<Uint8List> stream;
      StreamSubscription? ownedSubscription;
      final ownedRecorder = recorder;
      final ownedTransport = transport;
      await transport.start(
        start: () async {
          stream = await ownedRecorder.startStream(
            const RecordConfig(
              encoder: AudioEncoder.pcm16bits,
              sampleRate: 24000,
              numChannels: 1,
              autoGain: true,
              echoCancel: true,
              noiseSuppress: true,
              iosConfig: IosRecordConfig(
                categoryOptions: [
                  IosAudioCategoryOption.defaultToSpeaker,
                  IosAudioCategoryOption.allowBluetooth,
                  IosAudioCategoryOption.allowBluetoothA2DP,
                  IosAudioCategoryOption.allowAirPlay,
                ],
              ),
            ),
          );
        },
        stopAndConfirm: () async {
          await ownedSubscription?.cancel();
          await ownedRecorder.stop();
          if (await ownedRecorder.isRecording()) throw StateError('V2V microphone stop not confirmed');
          await ownedRecorder.dispose();
        },
      );
      if (!transport.isCurrent || !_hasCurrentSessionAuthority() || !await _ensureInteractiveOutput()) {
        await _stopMicStream(reason: 'post_start_admission_unavailable', expectedTransport: transport);
        return false;
      }
      if (!transport.isCurrent || !_hasCurrentSessionAuthority()) {
        await _stopMicStream(reason: 'late_route_verification', expectedTransport: transport);
        return false;
      }
      _micChunksSent = 0;
      _micBytesSent = 0;
      ownedSubscription = stream.listen((data) {
        if (!ownedTransport.isCurrent || !_hasCurrentSessionAuthority() || _aiConsentLease?.isActive != true) return;
        if (_isConnected && _channel != null && !_micMuted && !_isPlaying && !_micSuspendedForPlayback) {
          _onProtectedEgress?.call(V2VProtectedEgressBoundary.microphoneFrame);
          _channel!.send(data);
          _micChunksSent++;
          _micBytesSent += data.length;
        }
      });
      _micSub = ownedSubscription;
      _micMuted = false;
      _micSuspendedForPlayback = false;
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic active'));
      return true;
    } catch (error) {
      if (transport != null) {
        await _stopMicStream(reason: 'start_failed', expectedTransport: transport);
      }
      Logger.error('[V2V] Mic start failed: ${error.runtimeType}');
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic unavailable'));
      return false;
    }
  }

  Future<void> _stopMicStream({required String reason, AudioCaptureTransport? expectedTransport}) async {
    final transport = expectedTransport ?? _micTransport;
    if (expectedTransport != null && !identical(_micTransport, expectedTransport)) return;
    final recorder = _recorder;
    if (transport != null) {
      await transport.stop();
      // Before native start there is no physical recording, but the owned
      // recorder object may already exist while its permission dialog is held.
      if (!transport.hasAttemptedStart) await recorder?.dispose();
    }
    if (identical(_micTransport, transport)) {
      _micTransport = null;
      _micSub = null;
      if (identical(_recorder, recorder)) _recorder = null;
    }
  }

  Future<void> _resumeMicAfterPlayback() async {
    if (_outputLifetime?.failed == true || (_outputLifetime?.retiring == true || _outputLifetime?.stopping == true))
      return;
    if (!_micSuspendedForPlayback) {
      _micMuted = false;
      return;
    }

    Logger.debug('[V2V] Mic gate cooldown: ${_playbackMicCooldown.inMilliseconds}ms');
    onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Mic gate cooldown'));
    await Future.delayed(_playbackMicCooldown);
    await _micGateFuture;
    if (_micGateFailure != null) return;

    if (!_hasLiveSessionTransport) {
      Logger.debug('[V2V] Mic gate remains closed: session disconnected');
      _micSuspendedForPlayback = false;
      _micMuted = false;
      return;
    }
    if (!_hasCurrentSessionAuthority() || _aiConsentLease?.isActive != true) {
      await _handleRuntimeAuthorityLoss();
      return;
    }

    if (_isPlaying) {
      Logger.debug('[V2V] Mic gate remains closed: playback still active');
      return;
    }

    Logger.debug('[V2V] Mic gate reopening after playback cooldown');
    final authority = _activeConsentAuthority;
    final shouldContinue = _sessionShouldContinue;
    if (authority == null || shouldContinue == null) {
      await _handleRuntimeAuthorityLoss();
      return;
    }
    if (!await _ensureInteractiveOutput()) {
      Logger.error('[V2V] Interactive audio route could not be restored after playback');
      await disconnect();
      return;
    }
    final resumed = await _startAuthorizedMicrophone(authority: authority, shouldContinue: shouldContinue);
    if (!resumed) await disconnect();
  }

  bool get _hasLiveSessionTransport => _isConnected && (_channel != null || _liveChannelForTesting?.call() == true);

  // --- PCM16 playback ---

  /// Buffer provider PCM16 until its explicit completion marker arrives.
  void _bufferAudioChunk(Uint8List pcmData) {
    if (pcmData.isEmpty ||
        !_hasLiveSessionTransport ||
        _outputLifetime?.failed == true ||
        (_outputLifetime?.retiring == true || _outputLifetime?.stopping == true) ||
        !_hasCurrentSessionAuthority()) return;

    // A binary frame proves that this response has audio. Transcript events do
    // not: some provider turns are intentionally text-only.
    _suspendMicForPlayback('audio_chunk');
    _deferPendingPlaybackFinish('audio_chunk');
    _chunkCount++;
    _pcmBuffer.add(pcmData);

    if (_chunkCount == 1) {
      onEvent?.call(const V2VEvent(type: 'v2v_debug', text: 'Buffering response audio'));
      Logger.debug('[V2V] First audio chunk, playback gate active');
    }

    if (_chunkCount % 20 == 0) {
      Logger.debug('[V2V] Buffered $_chunkCount chunks, ${_pcmBuffer.length}B');
    }
  }

  void _registerOutputLifetime(AudioCaptureLease lease) {
    final existing = _outputLifetimes[lease];
    if (existing != null) {
      _outputLifetime = existing;
      return;
    }
    final lifetime = _V2VOutputLifetime(lease);
    _outputLifetimes[lease] = lifetime;
    _outputLifetime = lifetime;
    lease.addStopAndConfirm(() => _teardownOutputLifetime(lifetime));
  }

  _V2VOutputLifetime _lifetimeFor(AudioCaptureLease lease) {
    final lifetime = _outputLifetimes[lease];
    if (lifetime == null) throw StateError('V2V output lifetime unavailable');
    return lifetime;
  }

  Future<T> _withOutputSetup<T>(Future<T> Function() action) async {
    final lifetime = _outputLifetime;
    if (lifetime == null || !lifetime.lease.isCurrent || lifetime.retiring || lifetime.stopping || lifetime.failed) {
      throw StateError('V2V output owner unavailable');
    }
    final receipt = Completer<void>();
    final preceding = lifetime.setup;
    lifetime.setup = receipt.future;
    try {
      if (preceding != null) {
        try {
          await preceding;
        } catch (_) {}
      }
      if (!lifetime.lease.isCurrent || lifetime.retiring || lifetime.stopping || lifetime.failed)
        throw StateError('V2V output setup retired');
      lifetime.routeAttempted = true;
      lifetime.routeReleased = false;
      return await action();
    } finally {
      receipt.complete();
      if (identical(lifetime.setup, receipt.future)) lifetime.setup = null;
    }
  }

  Future<bool> _ensureInteractiveOutput() => _withOutputSetup(_interactiveOutputEnforcer);

  Future<void> _releaseOutputRoute(_V2VOutputLifetime lifetime) async {
    if (!lifetime.routeAttempted || lifetime.routeReleased) return;
    final injectedReceipt = _audioSessionReleaseReceipt;
    if (injectedReceipt != null) {
      if (!await injectedReceipt()) throw StateError('V2V audio route release not confirmed');
    } else if (_audioSessionReleaser != null) {
      await _audioSessionReleaser!();
    } else if (!await EllaVoiceAudioRoute.release(usage: EllaVoiceAudioUsage.interactive)) {
      throw StateError('V2V audio route release not confirmed');
    }
    lifetime.routeReleased = true;
  }

  Future<void> _teardownOutputLifetime(_V2VOutputLifetime lifetime) {
    final pending = lifetime.teardown;
    if (pending != null) return pending;
    lifetime.retiring = true;
    _playbackGeneration++;
    _finishPlaybackTimer?.cancel();
    _finishPlaybackTimer = null;
    final receipt = Completer<void>();
    lifetime.teardown = receipt.future;
    () async {
      await lifetime.setup;
      await _stopOutputPlayer(lifetime, dispose: true);
      await _releaseOutputRoute(lifetime);
      lifetime.failed = false;
      lifetime.outputPermit.releaseAfterConfirmedTeardown();
    }()
        .then(receipt.complete, onError: (Object error, StackTrace stack) {
      lifetime.failed = true;
      receipt.completeError(error, stack);
      _reportUnconfirmedOutput();
    }).whenComplete(() => lifetime.teardown = null);
    return receipt.future;
  }

  Future<void> _stopOutputPlayer(_V2VOutputLifetime lifetime, {required bool dispose}) async {
    final pending = lifetime.playerStop;
    if (pending != null) {
      await pending;
      if (dispose) await _stopOutputPlayer(lifetime, dispose: true);
      return;
    }
    final receipt = Completer<void>();
    lifetime.playerStop = receipt.future;
    try {
      await _stopOutputPlayerOwned(lifetime, dispose: dispose);
      receipt.complete();
    } catch (error, stack) {
      receipt.completeError(error, stack);
    } finally {
      lifetime.playerStop = null;
    }
    return receipt.future;
  }

  Future<void> _stopOutputPlayerOwned(_V2VOutputLifetime lifetime, {required bool dispose}) async {
    final injected = lifetime.injectedPlayback;
    if (injected != null) {
      final confirmation = _injectedPlaybackStopReceipt;
      if (confirmation != null) {
        if (!await confirmation()) throw StateError('V2V injected output stop not confirmed');
      } else {
        try {
          await injected;
        } catch (_) {}
      }
      lifetime.injectedPlayback = null;
    }
    final player = lifetime.player;
    if (player == null) return;
    if (!await player.stopAndConfirm()) throw StateError('V2V output stop not confirmed');
    if (dispose && !await player.disposeAndConfirm()) throw StateError('V2V output disposal not confirmed');
    if (dispose || !player.reusableAfterStop) lifetime.player = null;
  }

  Future<void> _stopAudioPlayback({bool dispose = false}) async {
    final lifetime = _outputLifetime;
    if (lifetime == null) return;
    lifetime.stopping = true;
    try {
      await lifetime.setup;
      await _stopOutputPlayer(lifetime, dispose: dispose);
      lifetime.failed = false;
    } catch (_) {
      lifetime.failed = true;
      _micMuted = true;
      _micSuspendedForPlayback = true;
      _reportUnconfirmedOutput();
      rethrow;
    } finally {
      lifetime.stopping = false;
    }
  }

  Future<void> _playWav(Uint8List wavBytes, int generation) async {
    final lifetime = _outputLifetime;
    if (lifetime == null || lifetime.failed || lifetime.retiring || lifetime.stopping)
      throw StateError('V2V output unavailable');
    final injectedPlayback = _wavPlayback;
    if (injectedPlayback != null) {
      if (generation != _playbackGeneration || !lifetime.lease.isCurrent) return;
      final playing = Future<void>.sync(() => injectedPlayback(wavBytes));
      lifetime.injectedPlayback = playing;
      await playing;
      if (identical(lifetime.injectedPlayback, playing)) lifetime.injectedPlayback = null;
      return;
    }
    final path = '${Directory.systemTemp.path}/ella_v2v_${DateTime.now().microsecondsSinceEpoch}_$generation.wav';
    final file = File(path);
    try {
      await _withOutputSetup(() async {
        await file.writeAsBytes(wavBytes, flush: true);
        if (!lifetime.lease.isCurrent || lifetime.retiring || lifetime.stopping || generation != _playbackGeneration)
          return;
        final player = lifetime.player ??= _outputPlayerFactory();
        await player.setVolume(1.0);
        if (!lifetime.lease.isCurrent || lifetime.retiring || lifetime.stopping || generation != _playbackGeneration)
          return;
        await player.setFilePath(path);
      });
      if (generation != _playbackGeneration ||
          !_hasLiveSessionTransport ||
          !lifetime.lease.isCurrent ||
          lifetime.retiring ||
          lifetime.stopping) return;
      await lifetime.player?.play();
    } finally {
      try {
        await file.delete();
      } catch (_) {}
    }
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

  /// Called after audio/output completion quiets. A complete WAV source gives
  /// just_audio a deterministic duration and an awaitable completion boundary.
  Future<void> _finishPlayback({required String reason}) async {
    if (_outputLifetime?.failed == true || (_outputLifetime?.retiring == true || _outputLifetime?.stopping == true))
      return;
    if (_finishingPlayback) {
      Logger.debug('[V2V] Playback finish already in progress, ignoring reason=$reason');
      return;
    }
    _finishingPlayback = true;
    _finishPlaybackTimer?.cancel();
    _finishPlaybackTimer = null;
    final generation = ++_playbackGeneration;
    final pcmBytes = _pcmBuffer.toBytes();
    final stats = '$_chunkCount chunks, ${(pcmBytes.length / 1024).toStringAsFixed(1)}KB';
    Logger.debug('[V2V] Audio buffer done: reason=$reason $stats');

    _pcmBuffer.clear();
    _chunkCount = 0;

    if (pcmBytes.isEmpty) {
      Logger.debug('[V2V] No audio data to play');
      if (_micSuspendedForPlayback) await _resumeMicAfterPlayback();
      onEvent?.call(const V2VEvent(type: 'playback_complete'));
      _finishingPlayback = false;
      return;
    }

    try {
      await _micGateFuture;
      if (_micGateFailure != null) return;
      if (generation != _playbackGeneration || !_hasLiveSessionTransport) return;
      if (!_hasCurrentSessionAuthority() || _aiConsentLease?.isActive != true) {
        await _handleRuntimeAuthorityLoss();
        return;
      }
      if (!await _withOutputSetup(_playbackOutputEnforcer)) {
        throw StateError('Ella voice playback route could not be made audible');
      }

      final wavBytes = _buildWav(Uint8List.fromList(pcmBytes));
      final totalAudioMs = (pcmBytes.length / (_pcmSampleRate * _pcmBytesPerSample * _pcmChannels) * 1000).ceil();
      final timeoutMs = (totalAudioMs + 8000).clamp(8000, 180000);
      Logger.debug('[V2V] WAV ready: ${wavBytes.length}B, ${totalAudioMs}ms');
      _isPlaying = true;
      _playbackFuture = _playWav(wavBytes, generation).timeout(Duration(milliseconds: timeoutMs));
      await _playbackFuture;
      if (generation != _playbackGeneration) return;
      await _onPlaybackComplete(generation);
    } catch (error) {
      if (generation != _playbackGeneration || _outputLifetime?.failed == true) return;
      Logger.error('[V2V] WAV playback error: ${error.runtimeType}');
      onEvent?.call(const V2VEvent(type: 'error', text: 'Voice audio playback failed'));
      try {
        await _stopAudioPlayback();
      } catch (_) {
        return;
      }
      _isPlaying = false;
      await _resumeMicAfterPlayback();
      onEvent?.call(const V2VEvent(type: 'playback_complete'));
    } finally {
      _finishingPlayback = false;
    }
  }

  Future<void> _onPlaybackComplete(int generation) async {
    if (generation != _playbackGeneration) return;
    if (!_isPlaying && !_micMuted) return; // Already handled
    Logger.debug('[V2V] Playback complete, restarting mic');
    await _stopAudioPlayback();
    _isPlaying = false;
    await _resumeMicAfterPlayback();

    onEvent?.call(const V2VEvent(type: 'playback_complete'));
  }

  static Uint8List _buildWav(Uint8List pcmData) {
    const bitsPerSample = 16;
    const bytesPerSample = bitsPerSample ~/ 8;
    const byteRate = _pcmSampleRate * _pcmChannels * bytesPerSample;
    const blockAlign = _pcmChannels * bytesPerSample;
    final header = ByteData(44)
      ..setUint32(0, 0x46464952, Endian.little)
      ..setUint32(4, 36 + pcmData.length, Endian.little)
      ..setUint32(8, 0x45564157, Endian.little)
      ..setUint32(12, 0x20746d66, Endian.little)
      ..setUint32(16, 16, Endian.little)
      ..setUint16(20, 1, Endian.little)
      ..setUint16(22, _pcmChannels, Endian.little)
      ..setUint32(24, _pcmSampleRate, Endian.little)
      ..setUint32(28, byteRate, Endian.little)
      ..setUint16(32, blockAlign, Endian.little)
      ..setUint16(34, bitsPerSample, Endian.little)
      ..setUint32(36, 0x61746164, Endian.little)
      ..setUint32(40, pcmData.length, Endian.little);
    final wav = Uint8List(44 + pcmData.length)..setRange(0, 44, header.buffer.asUint8List());
    wav.setRange(44, wav.length, pcmData);
    return wav;
  }

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      if (_requiresProviderReady && (!_providerAcknowledged || !_isConnected)) return;
      // Binary frame = raw PCM16 audio from proxy.
      _bufferAudioChunk(Uint8List.fromList(message));
      return;
    }

    if (message is String) {
      // JSON event
      try {
        final json = jsonDecode(message) as Map<String, dynamic>;
        final type = json['type'] as String? ?? 'unknown';
        final text = _eventText(json);
        if (_requiresProviderReady && (type == 'error' || type == 'session_end')) {
          final policyReason = _eventPolicyReason(json);
          final legacyCode = safeErrorCode(jsonEncode(json), fallback: 'provider_startup_failed');
          final consentDenied = const {'ai_consent_required', 'consent_authority_lost'}.contains(legacyCode);
          if (policyReason != null) {
            _providerReadiness?.fail(ellaVoicePolicyReasonCode(policyReason));
            onEvent?.call(V2VEvent(type: type, policyReason: policyReason, resetsAt: _eventResetsAt(json)));
          } else if (consentDenied) {
            _providerReadiness?.fail(legacyCode);
            onEvent?.call(const V2VEvent(type: 'consent_authority_lost'));
          } else {
            _providerReadiness?.handle(json);
          }
          final failure = _providerReadiness?.outcome;
          _completeReceipt(V2VConnectionReceipt(
              connected: false,
              provider: 'grok-voice',
              stage: V2VConnectionStage.providerReady,
              errorCode: failure?.errorCode ?? 'provider_startup_failed',
              httpStatus: failure?.httpStatus));
          _disconnectAfterEvent();
          return;
        }
        if (_requiresProviderReady && (!_providerAcknowledged || !_isConnected)) {
          _providerReadiness?.handle(json);
          return;
        }
        if (_requiresProviderReady && type == 'provider_ready') return;

        if (type == 'transcript_turn') {
          final terminalTurn = V2VTerminalTranscriptTurn.tryParse(json);
          if (terminalTurn == null) {
            Logger.debug('[V2V] Ignoring invalid terminal transcript contract');
            return;
          }
          final durableHandler = onTerminalTurnDurable;
          if (durableHandler == null) {
            onEvent?.call(V2VEvent(type: 'transcript_turn', terminalTurn: terminalTurn));
            return;
          }
          _terminalTurnTail = _terminalTurnTail.then((_) async {
            var durablyOwned = false;
            try {
              durablyOwned = await durableHandler(terminalTurn);
            } catch (_) {
              Logger.error('[V2V] Terminal transcript durable handoff failed');
            }
            if (!durablyOwned) return;
            onEvent?.call(V2VEvent(type: 'transcript_turn', terminalTurn: terminalTurn));
            final channel = _channel;
            if (!_isConnected ||
                channel == null ||
                terminalTurn.sessionId != _connectedSessionId ||
                !_hasCurrentSessionAuthority()) {
              return;
            }
            channel.send(
              jsonEncode({
                'type': 'transcript_turn_ack',
                'contract_version': 1,
                'session_id': terminalTurn.sessionId,
                'turn_id': terminalTurn.turnId,
              }),
            );
          }).catchError((_) {
            Logger.error('[V2V] Terminal transcript acknowledgment failed');
          });
          return;
        }

        if (_isUserTranscriptEvent(type)) {
          onEvent?.call(V2VEvent(type: 'user_transcript', text: text));
          return;
        }

        if (_isAssistantTranscriptEvent(type)) {
          onEvent?.call(V2VEvent(type: 'transcript', text: text));
          return;
        }

        if (_isAudioDoneEvent(type)) {
          _scheduleFinishPlayback(type);
          onEvent?.call(const V2VEvent(type: 'audio_done'));
          return;
        }

        switch (type) {
          case 'speech_started':
            if (_isPlaying || _micMuted || _micSuspendedForPlayback) {
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
            _disconnectAfterEvent();
            break;
          case 'error':
            Logger.error('[V2V] Server error event');
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
            Logger.debug('[V2V] Unknown event received');
            onEvent?.call(V2VEvent(type: type, text: text));
        }
      } catch (e) {
        Logger.debug('[V2V] Failed to parse JSON event: ${e.runtimeType}');
      }
    }
  }
}
