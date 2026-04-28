import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:record/record.dart';
import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/logger.dart';

/// JSON event from the V2V proxy WebSocket.
class V2VEvent {
  final String type;
  final String? text;

  V2VEvent({required this.type, this.text});
}

/// Voice-to-voice WebSocket client for half-duplex PCM16 audio streaming.
///
/// Uses `record` package for mic input and `just_audio` for WAV playback.
/// Mic is muted during playback to prevent echo→VAD interruption on Grok's side.
class V2VClient {
  WebSocketChannel? _channel;
  AudioRecorder? _recorder;
  StreamSubscription? _micSub;
  StreamSubscription? _wsSub;
  bool _isConnected = false;
  bool _isPlaying = false;
  bool _micMuted = false;

  /// PCM buffer for accumulating audio chunks before WAV playback
  final BytesBuilder _pcmBuffer = BytesBuilder(copy: false);
  int _chunkCount = 0;

  /// Low-latency PCM stream player for provider-native V2V audio.
  final FlutterSoundPlayer _streamPlayer = FlutterSoundPlayer();
  bool _streamPlayerOpen = false;
  bool _streamPlaybackStarted = false;
  Future<void> _streamFeedFuture = Future.value();

  /// Callback for JSON events (transcripts, errors, etc.)
  final void Function(V2VEvent event)? onEvent;

  /// Callback for connection state changes.
  final void Function(bool connected)? onConnectionChanged;

  V2VClient({this.onEvent, this.onConnectionChanged});

  bool get isConnected => _isConnected;

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

  static String? sessionVoiceMode(String provider) => switch (normalizeProvider(provider)) {
        'openclaw-direct' => 'openclaw-direct-v1',
        'openai-native-realtime' => 'openai-native-realtime-v1',
        'gemini-native-live' => 'gemini-native-live-v1',
        _ => null,
      };

  /// Start a V2V session: get session token, connect WebSocket, start audio.
  Future<bool> connect({required String provider}) async {
    provider = normalizeProvider(provider);
    final uid = SharedPreferencesUtil().uid;
    if (uid.isEmpty) {
      Logger.debug('[V2V] No uid, cannot connect');
      return false;
    }

    // 1. Get session token from backend
    final sessionData = await _createSession(uid, provider);
    if (sessionData == null) return false;

    final token = sessionData['session_token'] as String? ?? '';
    final endpoint = sessionData['voice_endpoint'] as String? ?? '';
    if (token.isEmpty || endpoint.isEmpty) {
      Logger.debug('[V2V] Invalid session data');
      return false;
    }

    // 2. Configure iOS audio session for playAndRecord with Bluetooth + speaker routing
    await _configureAudioSession();

    // 3. Connect WebSocket
    final wsUrl = _withSessionToken(endpoint, token);
    Logger.debug('[V2V] Connecting to WebSocket for provider=$provider...');

    try {
      _channel = IOWebSocketChannel.connect(
        Uri.parse(wsUrl),
        pingInterval: const Duration(seconds: 30),
      );

      _isConnected = true;
      onConnectionChanged?.call(true);

      // 3. Listen for messages from proxy
      _wsSub = _channel!.stream.listen(
        _handleMessage,
        onError: (error) {
          Logger.error('[V2V] WebSocket error: $error');
          disconnect();
        },
        onDone: () {
          Logger.debug('[V2V] WebSocket closed');
          _isConnected = false;
          onConnectionChanged?.call(false);
        },
      );

      // 4. Start recording mic audio and streaming to WebSocket
      await _startMicStream();

      return true;
    } catch (e) {
      Logger.error('[V2V] WebSocket connect failed: $e');
      return false;
    }
  }

  /// Disconnect and clean up all resources.
  Future<void> disconnect() async {
    _isConnected = false;
    onConnectionChanged?.call(false);

    _wsSub?.cancel();
    _wsSub = null;

    try {
      await _channel?.sink.close();
    } catch (_) {}
    _channel = null;

    await _stopMicStream();
    try {
      await _stopStreamingPlayback();
      if (_streamPlayerOpen) {
        await _streamPlayer.closePlayer();
        _streamPlayerOpen = false;
      }
    } catch (_) {}
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    if (_isPlaying) {
      try {
        await _stopStreamingPlayback();
      } catch (_) {}
      _isPlaying = false;
    }
    _micMuted = false;
    _pcmBuffer.clear();
    _chunkCount = 0;
  }

  // --- Audio session ---

  Future<void> _configureAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(AudioSessionConfiguration(
      avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
      avAudioSessionCategoryOptions: AVAudioSessionCategoryOptions.defaultToSpeaker |
          AVAudioSessionCategoryOptions.allowBluetooth |
          AVAudioSessionCategoryOptions.allowBluetoothA2dp |
          AVAudioSessionCategoryOptions.allowAirPlay,
      avAudioSessionMode: AVAudioSessionMode.defaultMode,
      avAudioSessionRouteSharingPolicy: AVAudioSessionRouteSharingPolicy.defaultPolicy,
      avAudioSessionSetActiveOptions: AVAudioSessionSetActiveOptions.none,
    ));
    await session.setActive(true);
    Logger.debug('[V2V] Audio session: playAndRecord + defaultToSpeaker + BT + AirPlay');
  }

  // --- Session management ---

  Future<Map<String, dynamic>?> _createSession(String uid, String provider) async {
    try {
      provider = normalizeProvider(provider);
      final voiceMode = sessionVoiceMode(provider);
      final requestBody = {
        'uid': uid,
        'provider': provider,
        if (voiceMode != null) 'voice_mode': voiceMode,
      };

      final response = await makeApiCall(
        url: '${Env.apiBaseUrl}v1/voice/session',
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(requestBody),
        method: 'POST',
        timeout: const Duration(seconds: 10),
      );

      if (response == null || response.statusCode != 200) {
        Logger.debug('[V2V] Session create failed: ${response?.statusCode}');
        return null;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      Logger.debug('[V2V] Session created: provider=$provider voice_mode=${voiceMode ?? "default"}');
      return data;
    } catch (e) {
      Logger.error('[V2V] Session create error: $e');
      return null;
    }
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

  static bool _isAudioDoneEvent(String type) {
    final normalized = type.toLowerCase();
    return normalized == 'audio_done' ||
        normalized == 'response.audio.done' ||
        normalized == 'output_audio.done' ||
        normalized == 'turn_complete' ||
        normalized == 'response.done';
  }

  // --- Mic recording (PCM16, 24kHz, mono) using `record` package ---

  Future<void> _startMicStream() async {
    try {
      _recorder = AudioRecorder();

      final hasPermission = await _recorder!.hasPermission();
      if (!hasPermission) {
        Logger.error('[V2V] Mic permission denied');
        onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic permission denied'));
        return;
      }

      final stream = await _recorder!.startStream(const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 24000,
        numChannels: 1,
        autoGain: true,
        echoCancel: true,
        noiseSuppress: true,
      ));

      _micSub = stream.listen((data) {
        if (_isConnected && _channel != null && !_micMuted) {
          _channel!.sink.add(data);
        }
      });

      _micMuted = false;
      Logger.debug('[V2V] Mic recording started at 24kHz');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic active'));
    } catch (e) {
      Logger.error('[V2V] Mic start failed: $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic error: $e'));
    }
  }

  Future<void> _stopMicStream() async {
    _micSub?.cancel();
    _micSub = null;
    try {
      await _recorder?.stop();
      await _recorder?.dispose();
    } catch (_) {}
    _recorder = null;
  }

  // --- Low-latency PCM streaming playback ---

  /// Stream incoming PCM16 audio chunk to the platform player.
  void _streamAudioChunk(Uint8List pcmData) {
    if (pcmData.isEmpty) return;

    _chunkCount++;
    _pcmBuffer.add(pcmData);

    _streamFeedFuture = _streamFeedFuture.then((_) async {
      await _ensureStreamingPlaybackStarted();
      _streamPlayer.uint8ListSink?.add(pcmData);
    }).catchError((error) {
      Logger.error('[V2V] Stream playback feed error: $error');
      onEvent?.call(V2VEvent(type: 'error', text: 'Audio stream error: $error'));
    });

    // Mute mic on first chunk to avoid echo/VAD self-interrupt.
    if (!_micMuted) {
      _micMuted = true;
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Streaming response audio'));
      Logger.debug('[V2V] First audio chunk, streaming playback started, mic muted');
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
      sampleRate: 24000,
      numChannels: 1,
      bufferSize: 4096,
    );
    Logger.debug('[V2V] PCM stream player started');
  }

  Future<void> _stopStreamingPlayback() async {
    if (!_streamPlaybackStarted) return;
    try {
      await _streamPlayer.stopPlayer();
    } catch (_) {}
    _streamPlaybackStarted = false;
  }

  /// Called on audio_done — wait for queued PCM to drain, then return to listening.
  Future<void> _finishPlayback() async {
    final pcmBytes = _pcmBuffer.toBytes();
    final stats = '$_chunkCount chunks, ${(pcmBytes.length / 1024).toStringAsFixed(1)}KB';
    Logger.debug('[V2V] Audio stream done: $stats');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Finishing audio: $stats'));

    _pcmBuffer.clear();
    _chunkCount = 0;

    if (pcmBytes.isEmpty) {
      Logger.debug('[V2V] No audio data to play');
      await _onPlaybackComplete();
      return;
    }

    try {
      await _streamFeedFuture.timeout(const Duration(seconds: 5));
      await Future.delayed(const Duration(milliseconds: 250));
      await _onPlaybackComplete();
    } catch (e) {
      Logger.error('[V2V] Stream playback error: $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Stream play error: $e'));
      _isPlaying = false;
      _micMuted = false;
      onEvent?.call(V2VEvent(type: 'playback_complete'));
    }
  }

  /// Called when streaming playback finishes.
  Future<void> _onPlaybackComplete() async {
    if (!_isPlaying && !_micMuted) return; // Already handled
    Logger.debug('[V2V] Playback complete, restarting mic');
    await _stopStreamingPlayback();
    _isPlaying = false;
    _micMuted = false;

    onEvent?.call(V2VEvent(type: 'playback_complete'));
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
          onEvent?.call(V2VEvent(type: 'transcript', text: text));
          return;
        }

        if (_isAudioDoneEvent(type)) {
          _finishPlayback();
          onEvent?.call(V2VEvent(type: 'audio_done'));
          return;
        }

        switch (type) {
          case 'speech_started':
            interruptPlayback();
            onEvent?.call(V2VEvent(type: 'speech_started'));
            break;
          case 'function_calling':
          case 'function_executed':
            onEvent?.call(V2VEvent(type: type, text: text ?? json.toString()));
            break;
          case 'session_end':
            onEvent?.call(V2VEvent(type: 'session_end', text: text));
            disconnect();
            break;
          case 'error':
            Logger.error('[V2V] Server error: ${json['message'] ?? text}');
            onEvent?.call(V2VEvent(type: 'error', text: json['message'] as String? ?? text));
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
