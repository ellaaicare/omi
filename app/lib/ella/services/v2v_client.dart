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
/// Uses `record` package for mic input and FlutterSound for PCM playback.
/// The mic recorder is suspended during playback so provider VAD cannot hear
/// Ella's own response audio or post-turn background noise.
class V2VClient {
  static const int _pcmSampleRate = 24000;
  static const int _pcmBytesPerSample = 2;
  static const int _pcmChannels = 1;
  static const Duration _postPlaybackMicCooldown = Duration(seconds: 2);

  static V2VClient? _activeClient;

  WebSocketChannel? _channel;
  AudioRecorder? _recorder;
  StreamSubscription? _micSub;
  StreamSubscription? _wsSub;
  bool _isConnected = false;
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
    if (_activeClient != null && _activeClient != this) {
      Logger.debug('[V2V] Closing existing active V2V client before provider=$provider connect');
      await _activeClient!.disconnect();
    }
    _activeClient = this;

    if (_isConnected || _channel != null || _recorder != null) {
      Logger.debug('[V2V] connect() called with existing session state, disconnecting first');
      await disconnect();
      _activeClient = this;
    }

    final uid = SharedPreferencesUtil().uid;
    if (uid.isEmpty) {
      Logger.debug('[V2V] No uid, cannot connect');
      if (_activeClient == this) _activeClient = null;
      return false;
    }

    // 1. Get session token from backend
    final sessionData = await _createSession(uid, provider);
    if (sessionData == null) {
      if (_activeClient == this) _activeClient = null;
      return false;
    }

    final token = sessionData['session_token'] as String? ?? '';
    final endpoint = sessionData['voice_endpoint'] as String? ?? '';
    if (token.isEmpty || endpoint.isEmpty) {
      Logger.debug('[V2V] Invalid session data');
      if (_activeClient == this) _activeClient = null;
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
      await disconnect();
      return false;
    }
  }

  /// Disconnect and clean up all resources.
  Future<void> disconnect() async {
    _isConnected = false;
    onConnectionChanged?.call(false);
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
    _micGateFuture = Future.value();
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
        normalized == 'turn_complete' ||
        normalized == 'response.done';
  }

  // --- Mic recording (PCM16, 24kHz, mono) using `record` package ---

  Future<void> _startMicStream() async {
    try {
      if (_recorder != null || _micSub != null) {
        Logger.debug('[V2V] Mic stream already active, not starting duplicate recorder');
        return;
      }

      _recorder = AudioRecorder();

      final hasPermission = await _recorder!.hasPermission();
      if (!hasPermission) {
        Logger.error('[V2V] Mic permission denied');
        onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic permission denied'));
        await _recorder?.dispose();
        _recorder = null;
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

      _micChunksSent = 0;
      _micBytesSent = 0;
      _micSub = stream.listen((data) {
        if (_isConnected && _channel != null && !_micMuted && !_isPlaying && !_micSuspendedForPlayback) {
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
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic active'));
    } catch (e) {
      Logger.error('[V2V] Mic start failed: $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic error: $e'));
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
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Mic gate cooldown'));
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
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Streaming response audio'));
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
      onEvent?.call(V2VEvent(type: 'playback_complete'));
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
          _suspendMicForPlayback(type);
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
            if (_isPlaying || _micMuted || _micSuspendedForPlayback || _streamPlaybackStarted) {
              Logger.debug('[V2V] Ignoring speech_started while playback gate is active');
              onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Ignoring speech_started during response'));
              break;
            }
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
