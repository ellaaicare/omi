import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:flutter_soloud/flutter_soloud.dart';
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

/// Voice-to-voice WebSocket client for full-duplex PCM16 audio streaming.
///
/// Uses `record` package for mic input and `flutter_soloud` for real-time
/// PCM16 playback — both can run simultaneously on iOS with the
/// `playAndRecord` audio session category.
class V2VClient {
  WebSocketChannel? _channel;
  AudioRecorder? _recorder;
  StreamSubscription? _micSub;
  StreamSubscription? _wsSub;
  bool _isConnected = false;
  bool _isPlaying = false;
  bool _micMuted = false;

  /// SoLoud engine + stream source for real-time PCM playback.
  AudioSource? _streamSource;
  SoundHandle? _playHandle;
  int _chunkCount = 0;
  int _totalBytes = 0;

  /// Callback for JSON events (transcripts, errors, etc.)
  final void Function(V2VEvent event)? onEvent;

  /// Callback for connection state changes.
  final void Function(bool connected)? onConnectionChanged;

  V2VClient({this.onEvent, this.onConnectionChanged});

  bool get isConnected => _isConnected;

  /// Start a V2V session: get session token, connect WebSocket, start audio.
  Future<bool> connect({required String provider}) async {
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

    // 2. Initialize SoLoud BEFORE audio session — SoLoud uses miniaudio which
    //    defers to the existing AVAudioSession (noAudioSessionActivate=true).
    await _initSoLoud();

    // 3. Configure iOS audio session for full-duplex with defaultToSpeaker
    //    (voiceChat mode routes to earpiece by default — we need the main speaker)
    await _configureAudioSession();

    // 4. Connect WebSocket
    final wsUrl = '$endpoint&token=$token';
    Logger.debug('[V2V] Connecting to WebSocket...');

    try {
      _channel = IOWebSocketChannel.connect(
        Uri.parse(wsUrl),
        pingInterval: const Duration(seconds: 30),
      );

      _isConnected = true;
      onConnectionChanged?.call(true);

      // 5. Listen for messages from proxy
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

      // 6. Start recording mic audio and streaming to WebSocket
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
    await _stopSoLoud();
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    if (_isPlaying && _playHandle != null) {
      try {
        SoLoud.instance.stop(_playHandle!);
      } catch (_) {}
      _isPlaying = false;
      _playHandle = null;
    }
    _micMuted = false;
    _chunkCount = 0;
    _totalBytes = 0;
    // Reset the stream buffer for the next response
    if (_streamSource != null) {
      try {
        SoLoud.instance.resetBufferStream(_streamSource!);
      } catch (_) {}
    }
  }

  // --- Audio session ---

  Future<void> _configureAudioSession() async {
    final session = await AudioSession.instance;
    await session.configure(AudioSessionConfiguration(
      avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
      avAudioSessionCategoryOptions: AVAudioSessionCategoryOptions.defaultToSpeaker |
          AVAudioSessionCategoryOptions.allowBluetooth |
          AVAudioSessionCategoryOptions.allowBluetoothA2dp,
      avAudioSessionMode: AVAudioSessionMode.voiceChat,
      avAudioSessionRouteSharingPolicy: AVAudioSessionRouteSharingPolicy.defaultPolicy,
      avAudioSessionSetActiveOptions: AVAudioSessionSetActiveOptions.none,
    ));
    await session.setActive(true);
    Logger.debug('[V2V] Audio session configured: playAndRecord + defaultToSpeaker + voiceChat');
  }

  // --- Session management ---

  Future<Map<String, dynamic>?> _createSession(String uid, String provider) async {
    try {
      final response = await makeApiCall(
        url: '${Env.apiBaseUrl}v1/voice/session',
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'uid': uid, 'provider': provider}),
        method: 'POST',
        timeout: const Duration(seconds: 10),
      );

      if (response == null || response.statusCode != 200) {
        Logger.debug('[V2V] Session create failed: ${response?.statusCode}');
        return null;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      Logger.debug('[V2V] Session created: provider=$provider');
      return data;
    } catch (e) {
      Logger.error('[V2V] Session create error: $e');
      return null;
    }
  }

  // --- Mic recording (PCM16, 24kHz, mono) using `record` package ---

  Future<void> _startMicStream() async {
    _recorder = AudioRecorder();

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

    Logger.debug('[V2V] Mic recording started at 24kHz (record package)');
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

  // --- Audio playback using flutter_soloud ---

  Future<void> _initSoLoud() async {
    try {
      if (!SoLoud.instance.isInitialized) {
        await SoLoud.instance.init();
      }

      // Create a buffer stream source for real-time PCM16 feeding
      _streamSource = SoLoud.instance.setBufferStream(
        sampleRate: 24000,
        channels: Channels.mono,
        format: BufferType.s16le,
        bufferingTimeNeeds: 0.2, // 200ms buffer before playback starts
        bufferingType: BufferingType.released,
      );

      Logger.debug('[V2V] SoLoud initialized with PCM16 24kHz mono stream');
    } catch (e) {
      Logger.error('[V2V] SoLoud init error: $e');
    }
  }

  Future<void> _stopSoLoud() async {
    if (_playHandle != null) {
      try {
        SoLoud.instance.stop(_playHandle!);
      } catch (_) {}
      _playHandle = null;
    }
    if (_streamSource != null) {
      try {
        SoLoud.instance.disposeSource(_streamSource!);
      } catch (_) {}
      _streamSource = null;
    }
    _isPlaying = false;
    // Don't deinit SoLoud globally — other parts of the app may use it
  }

  /// Feed PCM16 audio chunk directly to SoLoud for real-time playback.
  Future<void> _playAudioChunk(Uint8List pcmData) async {
    if (_streamSource == null || pcmData.isEmpty) return;

    _chunkCount++;
    _totalBytes += pcmData.length;

    try {
      // Start playback on first chunk — mute mic to prevent echo→VAD→interruption
      if (!_isPlaying) {
        _isPlaying = true;
        _micMuted = true;
        _playHandle = await SoLoud.instance.play(_streamSource!);
        onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Audio stream started (mic muted)'));
        Logger.debug('[V2V] Started SoLoud stream playback, mic muted, handle=$_playHandle');
      }

      // Feed PCM16 data directly — SoLoud handles buffering internally
      SoLoud.instance.addAudioDataStream(_streamSource!, pcmData);

      if (_chunkCount % 20 == 0) {
        Logger.debug('[V2V] Fed $_chunkCount chunks, ${_totalBytes}B total');
      }
    } catch (e) {
      Logger.debug('[V2V] SoLoud feed error (chunk $_chunkCount): $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Feed error: $e'));
    }
  }

  /// Called on audio_done — mark the stream as ended so SoLoud plays remaining buffer.
  void _finishPlayback() {
    final stats = '$_chunkCount chunks, ${(_totalBytes / 1024).toStringAsFixed(1)}KB';
    Logger.debug('[V2V] Audio done: $stats');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Done: $stats'));

    if (_streamSource != null) {
      try {
        SoLoud.instance.setDataIsEnded(_streamSource!);
        Logger.debug('[V2V] Stream data ended, playing remaining buffer');
      } catch (e) {
        Logger.debug('[V2V] setDataIsEnded error: $e');
        onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'setDataIsEnded err: $e'));
      }
    }

    // Wait for remaining buffer to play, then reset for next response
    final waitMs = (_totalBytes > 0) ? 2000 : 500;
    Future.delayed(Duration(milliseconds: waitMs), () {
      _isPlaying = false;
      _micMuted = false;
      _playHandle = null;
      _chunkCount = 0;
      _totalBytes = 0;
      if (_streamSource != null) {
        try {
          SoLoud.instance.resetBufferStream(_streamSource!);
        } catch (_) {}
      }
      Logger.debug('[V2V] Playback complete, mic unmuted');
      onEvent?.call(V2VEvent(type: 'playback_complete'));
    });
  }

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      // Binary frame = raw PCM16 audio from proxy — play immediately
      // Fire-and-forget but _playAudioChunk handles its own errors
      _playAudioChunk(Uint8List.fromList(message));
      return;
    }

    if (message is String) {
      // JSON event
      Logger.debug('[V2V] JSON event: $message');
      try {
        final json = jsonDecode(message) as Map<String, dynamic>;
        final type = json['type'] as String? ?? 'unknown';
        final text = json['text'] as String? ?? json['transcript'] as String?;

        switch (type) {
          case 'user_transcript':
            onEvent?.call(V2VEvent(type: 'user_transcript', text: text));
            break;
          case 'transcript':
            onEvent?.call(V2VEvent(type: 'transcript', text: text));
            break;
          case 'audio_done':
            _finishPlayback();
            onEvent?.call(V2VEvent(type: 'audio_done'));
            break;
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
