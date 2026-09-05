import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/env/env.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/services/sockets/on_device_apple_provider.dart';
import 'package:omi/services/sockets/on_device_whisper_provider.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/services/sockets/transcription_service.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

export 'package:omi/utils/audio/audio_transcoder.dart';
export 'package:omi/services/sockets/composite_transcription_socket.dart';
export 'package:omi/services/sockets/pure_polling.dart';
export 'package:omi/services/sockets/pure_streaming_stt.dart';
export 'package:omi/models/stt_response_schema.dart';
export 'package:omi/models/stt_result.dart';
export 'package:omi/services/sockets/transcription_polling_service.dart';

abstract interface class ITransctiptSegmentSocketServiceListener {
  void onMessageEventReceived(MessageEvent event);

  void onSegmentReceived(List<TranscriptSegment> segments);

  void onError(Object err);

  void onConnected();

  void onClosed([int? closeCode]);
}

class SpeechProfileTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  SpeechProfileTranscriptSegmentSocketService.create(super.sampleRate, super.codec, super.language,
      {super.source, super.customSttMode, super.onboardingMode})
      : super.create(includeSpeechProfile: false);
}

class ConversationTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  ConversationTranscriptSegmentSocketService.create(super.sampleRate, super.codec, super.language,
      {super.source, super.customSttMode})
      : super.create(includeSpeechProfile: true);
}

class CustomSttTranscriptSegmentSocketService extends TranscriptSegmentSocketService {
  CustomSttTranscriptSegmentSocketService.create(super.sampleRate, super.codec, super.language, {super.source})
      : super.create(includeSpeechProfile: true, customSttMode: true);
}

enum SocketServiceState {
  connected,
  disconnected,
}

enum TranscriptSocketStartFailure {
  consentUnavailable,
  transportUnavailable,
  captureProtocolTimeout,
}

class CaptureProtocolAuthority {
  const CaptureProtocolAuthority({
    required this.protocolVersion,
    required this.conversationId,
    required this.generation,
    required this.ownerToken,
  });

  final int protocolVersion;
  final String conversationId;
  final String generation;
  final String ownerToken;

  Map<String, dynamic> toDrainJson() => {
        'type': 'capture_drain',
        'protocol_version': protocolVersion,
        'conversation_id': conversationId,
        'generation': generation,
        'owner_token': ownerToken,
      };

  bool matches(MessageServiceStatusEvent event) =>
      event.protocolVersion == protocolVersion &&
      event.conversationId == conversationId &&
      event.generation == generation &&
      event.ownerToken == ownerToken;
}

class TranscriptSegmentSocketService implements IPureSocketListener {
  static const _captureProtocolVersion = 2;
  static const _defaultCaptureProtocolTimeout = Duration(seconds: 8);

  late IPureSocket _socket;
  final Map<Object, ITransctiptSegmentSocketServiceListener> _listeners = {};
  AiConsentActiveSessionLease? _aiConsentLease;
  final bool _requiresCaptureProtocol;
  final Duration _captureProtocolTimeout;
  bool _captureProtocolReady = false;
  Completer<bool>? _captureProtocolReadyCompleter;
  Completer<bool>? _captureDrainCompleter;
  CaptureProtocolAuthority? _captureAuthority;
  CaptureProtocolAuthority? _pendingDrainAuthority;
  final Set<String> _retiredCaptureConversationIds = <String>{};
  TranscriptSocketStartFailure? _lastStartFailure;

  CaptureProtocolAuthority? get captureAuthority => _captureAuthority;
  TranscriptSocketStartFailure? get lastStartFailure => _lastStartFailure;

  /// Access to the underlying socket (for composite service creation)
  IPureSocket get socket => _socket;

  SocketServiceState get state =>
      _socket.status == PureSocketStatus.connected && (!_requiresCaptureProtocol || _captureProtocolReady)
          ? SocketServiceState.connected
          : SocketServiceState.disconnected;

  int sampleRate;
  BleAudioCodec codec;
  String language;
  bool includeSpeechProfile;
  String? source;
  bool customSttMode;
  String? sttConfigId;

  bool onboardingMode;

  TranscriptSegmentSocketService.create(
    this.sampleRate,
    this.codec,
    this.language, {
    this.includeSpeechProfile = false,
    this.source,
    this.customSttMode = false,
    this.sttConfigId,
    this.onboardingMode = false,
  })  : _requiresCaptureProtocol = true,
        _captureProtocolTimeout = _defaultCaptureProtocolTimeout {
    var params = '?language=$language&sample_rate=$sampleRate&codec=$codec&uid=${SharedPreferencesUtil().uid}'
        '&include_speech_profile=$includeSpeechProfile&stt_service=${SharedPreferencesUtil().transcriptionModel}'
        '&conversation_timeout=${SharedPreferencesUtil().conversationSilenceDuration}'
        '&capture_protocol=$_captureProtocolVersion';

    if (source != null && source!.isNotEmpty) {
      params += '&source=${Uri.encodeComponent(source!)}';
    }

    if (customSttMode) {
      params += '&custom_stt=enabled';
    }

    if (onboardingMode) {
      params += '&onboarding=enabled';
    }

    // Enable server-side speaker auto-assignment (backward compatibility flag)
    params += '&speaker_auto_assign=enabled';

    String url =
        Env.apiBaseUrl!.replaceFirst('https://', 'wss://').replaceFirst('http://', 'ws://') + 'v4/listen$params';

    _socket = PureSocket(url);
    _socket.setListener(this);
  }

  TranscriptSegmentSocketService.withSocket(
    this.sampleRate,
    this.codec,
    this.language,
    IPureSocket socket, {
    this.includeSpeechProfile = false,
    this.source,
    this.customSttMode = false,
    this.sttConfigId,
    this.onboardingMode = false,
    bool requireCaptureProtocol = false,
    Duration captureProtocolTimeout = _defaultCaptureProtocolTimeout,
  })  : _requiresCaptureProtocol = requireCaptureProtocol,
        _captureProtocolTimeout = captureProtocolTimeout {
    _socket = socket;
    _socket.setListener(this);
  }

  void subscribe(Object context, ITransctiptSegmentSocketServiceListener listener) {
    _listeners.remove(context.hashCode);
    _listeners.putIfAbsent(context.hashCode, () => listener);
  }

  void unsubscribe(Object context) {
    _listeners.remove(context.hashCode);
  }

  Future<void> start() async {
    _lastStartFailure = null;
    if (!SharedPreferencesUtil().aiConsentAccepted) {
      _lastStartFailure = TranscriptSocketStartFailure.consentUnavailable;
      return;
    }
    if (_requiresCaptureProtocol) {
      _captureProtocolReady = false;
      _captureAuthority = null;
      _pendingDrainAuthority = null;
      _retiredCaptureConversationIds.clear();
      _captureProtocolReadyCompleter = Completer<bool>();
    }
    bool ok;
    try {
      ok = await _socket.connect();
    } catch (_) {
      _lastStartFailure = TranscriptSocketStartFailure.transportUnavailable;
      rethrow;
    }
    if (!ok) {
      _lastStartFailure = TranscriptSocketStartFailure.transportUnavailable;
      Logger.debug("Can not connect to websocket");
      await DebugLogManager.logWarning('transcription_socket_connect_failed', {
        'sample_rate': sampleRate,
        'codec': codec.toString(),
        'language': language,
      });
      return;
    }
    if (_requiresCaptureProtocol) {
      final ready = await _captureProtocolReadyCompleter!.future.timeout(
        _captureProtocolTimeout,
        onTimeout: () => false,
      );
      if (!ready) {
        _lastStartFailure = TranscriptSocketStartFailure.captureProtocolTimeout;
        await _socket.stop();
        Logger.debug('Capture websocket did not establish protocol authority');
        return;
      }
    }
    final uid = SharedPreferencesUtil().uid;
    _aiConsentLease = AiConsentActiveSessionLease(
      uid: uid,
      onAuthorityLost: () async {
        final listeners = _listeners.values.toList(growable: false);
        for (final listener in listeners) {
          listener.onError(const AiConsentAuthorityLostException());
        }
        await stop(reason: 'AI consent authority lost');
      },
    )..start();
  }

  Future stop({String? reason}) async {
    final consentLease = _aiConsentLease;
    _aiConsentLease = null;
    consentLease?.stop();
    if (_requiresCaptureProtocol && _captureProtocolReady && _socket.status == PureSocketStatus.connected) {
      final transport = _socket;
      if (transport is CompositeTranscriptionSocket) {
        // A custom provider may publish its only final segment from stop().
        // Close just that provider while keeping the exact-owner backend
        // socket open, so the tail is queued before capture_drain asks the
        // server to acknowledge durable persistence.
        await transport.stopPrimaryForDrain();
      }
      try {
        final authority = _captureAuthority;
        if (authority == null) throw StateError('Capture websocket authority tuple is missing');
        _pendingDrainAuthority = authority;
        _captureDrainCompleter = Completer<bool>();
        _socket.send(jsonEncode(authority.toDrainJson()));
        final drained = await _captureDrainCompleter!.future.timeout(
          _captureProtocolTimeout,
          onTimeout: () => false,
        );
        if (!drained) {
          // The server may have committed the durable drained fence and lost
          // the acknowledgement with the socket. Closing the transport is
          // safe; the caller must reconcile through the idempotent exact-tuple
          // finalization POST instead of treating this ambiguity as failure.
          Logger.debug('Capture drain acknowledgement was ambiguous; reconciling by exact finalization tuple');
        }
      } catch (error) {
        // Send/transport failure is likewise ambiguous. The exact tuple held
        // by the finalization operation is the authority for reconciliation.
        Logger.debug('Capture drain transport became ambiguous: $error');
      }
    }
    _captureProtocolReady = false;
    _pendingDrainAuthority = null;
    await _socket.stop();
    _listeners.clear();

    if (reason != null) {
      Logger.debug(reason);
      await DebugLogManager.logInfo('transcription_socket_stopped', {'reason': reason});
    }
  }

  Future send(dynamic message) async {
    if (!SharedPreferencesUtil().aiConsentAccepted || state != SocketServiceState.connected) return;
    _socket.send(message);
    return;
  }

  Future sendText(String message) async {
    if (!SharedPreferencesUtil().aiConsentAccepted || state != SocketServiceState.connected) return;
    _socket.send(message);
    return;
  }

  @override
  void onClosed([int? closeCode]) {
    _captureProtocolReady = false;
    if (!(_captureProtocolReadyCompleter?.isCompleted ?? true)) _captureProtocolReadyCompleter!.complete(false);
    if (!(_captureDrainCompleter?.isCompleted ?? true)) _captureDrainCompleter!.complete(false);
    final consentLease = _aiConsentLease;
    _aiConsentLease = null;
    consentLease?.stop();
    _listeners.forEach((k, v) {
      v.onClosed(closeCode);
    });
    DebugLogManager.logEvent('transcription_socket_closed', {
      'close_code': closeCode ?? -1,
    });
  }

  @override
  void onError(Object err, StackTrace trace) {
    _captureProtocolReady = false;
    if (!(_captureProtocolReadyCompleter?.isCompleted ?? true)) _captureProtocolReadyCompleter!.complete(false);
    if (!(_captureDrainCompleter?.isCompleted ?? true)) _captureDrainCompleter!.complete(false);
    final consentLease = _aiConsentLease;
    _aiConsentLease = null;
    consentLease?.stop();
    _listeners.forEach((k, v) {
      v.onError(err);
    });
    DebugLogManager.logWarning('transcription_socket_error', {'error_type': err.runtimeType.toString()});
  }

  @override
  void onMessage(event) {
    // Decode json
    dynamic jsonEvent;
    try {
      jsonEvent = jsonDecode(event);
    } on FormatException catch (e) {
      Logger.debug('Transcription socket parse error (${e.runtimeType})');
      DebugLogManager.logWarning('transcription_socket_parse_error', {'error_type': e.runtimeType.toString()});
    }
    if (jsonEvent == null) {
      Logger.debug('Can not decode transcription socket message');
      return;
    }

    // Transcript segments
    if (jsonEvent is List) {
      var segments = jsonEvent;
      if (segments.isEmpty) {
        return;
      }
      _listeners.forEach((k, v) {
        v.onSegmentReceived(segments.map((e) => TranscriptSegment.fromJson(e)).toList());
      });
      return;
    }

    // Message event
    if (jsonEvent.containsKey("type")) {
      var event = MessageEvent.fromJson(jsonEvent);
      if (event is MessageServiceStatusEvent) {
        if (event.status == 'capture_protocol_ready') {
          final conversationId = (event.conversationId ?? '').trim();
          final generation = (event.generation ?? '').trim();
          final ownerToken = (event.ownerToken ?? '').trim();
          final valid = event.protocolVersion == _captureProtocolVersion &&
              conversationId.isNotEmpty &&
              generation.isNotEmpty &&
              ownerToken.isNotEmpty;
          if (valid) {
            final wasReady = _captureProtocolReady;
            final nextAuthority = CaptureProtocolAuthority(
              protocolVersion: event.protocolVersion!,
              conversationId: conversationId,
              generation: generation,
              ownerToken: ownerToken,
            );
            final currentAuthority = _captureAuthority;
            final isInitial = currentAuthority == null;
            final isIdempotent = currentAuthority?.matches(event) ?? false;
            final isSuccessor = currentAuthority != null &&
                currentAuthority.protocolVersion == nextAuthority.protocolVersion &&
                currentAuthority.generation == nextAuthority.generation &&
                currentAuthority.ownerToken == nextAuthority.ownerToken &&
                currentAuthority.conversationId != nextAuthority.conversationId &&
                !_retiredCaptureConversationIds.contains(nextAuthority.conversationId);
            if (!isInitial && !isIdempotent && !isSuccessor) {
              // Fail this stale/mixed-owner message closed without revoking a
              // newer valid authority already held by the live socket.
              return;
            }
            if (isSuccessor) {
              _retiredCaptureConversationIds.add(currentAuthority.conversationId);
            }
            _captureAuthority = nextAuthority;
            _captureProtocolReady = true;
            if (!(_captureProtocolReadyCompleter?.isCompleted ?? true)) _captureProtocolReadyCompleter!.complete(true);
            if (!wasReady) _notifyConnected();
          } else if (!(_captureProtocolReadyCompleter?.isCompleted ?? true)) {
            _captureProtocolReadyCompleter!.complete(false);
          }
        } else if (event.status == 'capture_protocol_drained') {
          final expected = _pendingDrainAuthority;
          final matches = expected != null && expected.matches(event);
          if (matches && !(_captureDrainCompleter?.isCompleted ?? true)) {
            _captureDrainCompleter!.complete(true);
          }
        }
      }
      _listeners.forEach((k, v) {
        v.onMessageEventReceived(event);
      });
      return;
    }

    Logger.debug(event.toString());
    DebugLogManager.logInfo('transcription_socket_unhandled_message: ${event.toString()}');
  }

  @override
  void onConnected() {
    if (_requiresCaptureProtocol) return;
    _notifyConnected();
  }

  void _notifyConnected() {
    _listeners.forEach((k, v) {
      v.onConnected();
    });
    DebugLogManager.logEvent('transcription_socket_connected', {
      'sample_rate': sampleRate,
      'codec': codec.toString(),
      'language': language,
      'include_speech_profile': includeSpeechProfile,
    });
  }
}

class TranscriptSocketServiceFactory {
  TranscriptSocketServiceFactory._();

  /// Codecs supported by custom STT providers
  static const List<BleAudioCodec> _customSttSupportedCodecs = [
    BleAudioCodec.pcm8,
    BleAudioCodec.pcm16,
    BleAudioCodec.opus,
    BleAudioCodec.opusFS320,
  ];

  /// Check if a codec is supported for custom STT
  static bool isCodecSupportedForCustomStt(BleAudioCodec codec) {
    return _customSttSupportedCodecs.contains(codec);
  }

  /// Create default Omi transcription service
  static TranscriptSegmentSocketService createDefault(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    bool includeSpeechProfile = true,
    String? source,
    String? sttConfigId,
  }) {
    return TranscriptSegmentSocketService.create(
      sampleRate,
      codec,
      language,
      includeSpeechProfile: includeSpeechProfile,
      source: source,
      sttConfigId: sttConfigId ?? 'omi:default',
    );
  }

  /// Create speech profile transcription service
  static TranscriptSegmentSocketService createSpeechProfile(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    String? source,
  }) {
    return SpeechProfileTranscriptSegmentSocketService.create(
      sampleRate,
      codec,
      language,
      source: source,
    );
  }

  /// Main entry point: Create transcription service from CustomSttConfig
  /// Uses config.isLive to decide between streaming and polling sockets
  static TranscriptSegmentSocketService createFromCustomConfig(
    int sampleRate,
    BleAudioCodec codec,
    String language,
    CustomSttConfig config, {
    String? source,
  }) {
    if (!config.isEnabled) {
      return createDefault(sampleRate, codec, language, source: source);
    }

    final sttConfigId = config.sttConfigId;
    final effectiveLang = config.effectiveLanguage;
    final effectiveModel = config.effectiveModel;
    Logger.debug(
        "[STTFactory] Creating socket: provider=${config.provider.name}, isLive=${config.isLive}, lang=$effectiveLang, model=$effectiveModel");

    // Create primary socket based on isLive/isPolling
    final primarySocket = config.isLive
        ? _createStreamingSocket(sampleRate, codec, config)
        : _createPollingSocket(sampleRate, codec, config);

    // Wrap with composite service (primary STT + Omi backend)
    return _createCompositeService(
      sampleRate,
      codec,
      effectiveLang,
      primarySocket: primarySocket,
      source: source,
      sttConfigId: sttConfigId,
      sttProvider: config.provider.name,
    );
  }

  /// Create streaming WebSocket for live STT
  static IPureSocket _createStreamingSocket(
    int sampleRate,
    BleAudioCodec codec,
    CustomSttConfig config,
  ) {
    final transcoder = AudioTranscoderFactory.createToRawPcm(
      sourceCodec: codec,
      sampleRate: sampleRate,
    );

    // Special case: Gemini Live has unique protocol (setup message, base64 audio)
    if (config.provider == SttProvider.geminiLive) {
      return GeminiStreamingSttSocket(
        apiKey: config.apiKey ?? '',
        model:
            config.effectiveModel.isNotEmpty ? config.effectiveModel : 'gemini-2.5-flash-native-audio-preview-12-2025',
        language: config.effectiveLanguage,
        sampleRate: sampleRate,
        transcoder: transcoder,
      );
    }

    // Deepgram Live and other streaming providers
    final requestConfig = config.requestConfig;
    final url = requestConfig['url'] ?? config.effectiveUrl;
    final headers =
        requestConfig['headers'] != null ? Map<String, String>.from(requestConfig['headers']) : (config.headers ?? {});
    final params =
        requestConfig['params'] != null ? Map<String, String>.from(requestConfig['params']) : (config.params ?? {});

    // Build WebSocket URL with query params
    final wsUrl = _buildUrlWithParams(url, params);

    return PureStreamingSttSocket(
      config: StreamingSttConfig.schemaBased(
        wsUrl: wsUrl,
        schema: config.schema,
        headers: headers,
        transcoder: transcoder,
        serviceId: config.provider.name,
        sendKeepAlive: config.provider == SttProvider.deepgramLive,
        keepAliveInterval: const Duration(seconds: 8),
      ),
    );
  }

  /// Create polling HTTP socket for batch STT
  static IPureSocket _createPollingSocket(
    int sampleRate,
    BleAudioCodec codec,
    CustomSttConfig config,
  ) {
    final transcoder = AudioTranscoderFactory.createToWav(
      sourceCodec: codec,
      sampleRate: sampleRate,
    );

    final requestConfig = config.requestConfig;
    final url = requestConfig['url'] ?? config.effectiveUrl;
    final headers =
        requestConfig['headers'] != null ? Map<String, String>.from(requestConfig['headers']) : (config.headers ?? {});
    final params =
        requestConfig['params'] != null ? Map<String, String>.from(requestConfig['params']) : (config.params ?? {});
    final audioFieldName = requestConfig['audio_field_name'] ?? config.audioFieldName ?? 'file';
    final requestType = config.effectiveRequestType;

    // Build URL with query params for raw_binary type
    final effectiveUrl = requestType == SttRequestType.rawBinary ? _buildUrlWithParams(url, params) : url;

    // Special handling for On-Device Whisper
    if (config.provider == SttProvider.onDeviceWhisper) {
      // Use Native iOS Speech Recognition on iOS
      if (Platform.isIOS) {
        return PurePollingSocket(
          config: AudioPollingConfig(
            bufferDuration: const Duration(seconds: 5),
            minBufferSizeBytes: sampleRate * 2,
            serviceId: config.provider.name,
            transcoder: transcoder,
          ),
          sttProvider: OnDeviceAppleProvider(
            language: config.language ?? 'en',
          ),
        );
      }

      if (config.url == null || config.url!.isEmpty) {
        throw ArgumentError("[STTFactory] OnDeviceWhisper selected but no model path provided.");
      }
      return PurePollingSocket(
        config: AudioPollingConfig(
          bufferDuration: const Duration(seconds: 5),
          minBufferSizeBytes: sampleRate * 2,
          serviceId: config.provider.name,
          transcoder: transcoder,
        ),
        sttProvider: OnDeviceWhisperProvider(
          modelPath: config.url ?? '',
          language: config.language ?? 'en',
        ),
      );
    }

    return PurePollingSocket(
      config: AudioPollingConfig(
        bufferDuration: const Duration(seconds: 5),
        minBufferSizeBytes: sampleRate * 2,
        serviceId: config.provider.name,
        transcoder: transcoder,
      ),
      sttProvider: SchemaBasedSttProvider(
        apiUrl: effectiveUrl,
        schema: config.schema,
        defaultHeaders: headers,
        defaultFields: requestType == SttRequestType.rawBinary ? {} : params,
        audioFieldName: audioFieldName,
        requestType: requestType,
      ),
    );
  }

  /// Build URL with query parameters
  static String _buildUrlWithParams(String baseUrl, Map<String, String> params) {
    if (params.isEmpty) return baseUrl;
    final uri = Uri.parse(baseUrl);
    final newUri = uri.replace(queryParameters: {...uri.queryParameters, ...params});
    return newUri.toString();
  }

  /// Create composite service: primary STT socket + Omi backend for conversation processing
  static TranscriptSegmentSocketService _createCompositeService(
    int sampleRate,
    BleAudioCodec codec,
    String language, {
    required IPureSocket primarySocket,
    String? source,
    String? sttConfigId,
    String? sttProvider,
  }) {
    final secondaryService = CustomSttTranscriptSegmentSocketService.create(
      sampleRate,
      codec,
      language,
      source: source,
    );
    final compositeSocket = CompositeTranscriptionSocket(
      primarySocket: primarySocket,
      secondarySocket: secondaryService.socket,
      sttProvider: sttProvider,
    );
    return TranscriptSegmentSocketService.withSocket(
      sampleRate,
      codec,
      language,
      compositeSocket,
      source: source,
      customSttMode: true,
      sttConfigId: sttConfigId,
    );
  }
}
