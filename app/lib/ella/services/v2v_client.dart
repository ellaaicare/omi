import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_sound/flutter_sound.dart';
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
  final Uint8List? audio;

  V2VEvent({required this.type, this.text, this.audio});
}

/// Voice-to-voice WebSocket client for bidirectional PCM16 audio streaming.
///
/// Connects to the Grok Voice / Gemini Live proxy via WebSocket,
/// streams mic audio as raw PCM16 24kHz mono, and receives audio + JSON events.
class V2VClient {
  WebSocketChannel? _channel;
  FlutterSoundRecorder? _recorder;
  FlutterSoundPlayer? _player;
  StreamController<Uint8List>? _micController;
  StreamSubscription? _wsSub;
  bool _isConnected = false;
  bool _isPlaying = false;

  /// Pre-buffer audio to avoid underruns from tiny PCM chunks.
  final List<Uint8List> _audioBuffer = [];
  int _bufferedBytes = 0;
  static const int _preBufferBytes = 9600; // 200ms at 24kHz PCM16 mono
  bool _preBufferFilled = false;

  /// Callback for JSON events (transcripts, errors, etc.)
  final void Function(V2VEvent event)? onEvent;

  /// Callback for connection state changes.
  final void Function(bool connected)? onConnectionChanged;

  /// Callback for audio level from mic (0.0 - 1.0).
  final void Function(double level)? onAudioLevel;

  V2VClient({this.onEvent, this.onConnectionChanged, this.onAudioLevel});

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
      Logger.debug('[V2V] Invalid session data: token=${token.isNotEmpty}, endpoint=${endpoint.isNotEmpty}');
      return false;
    }

    // 2. Connect WebSocket
    final wsUrl = '$endpoint&token=$token';
    Logger.debug('[V2V] Connecting to WebSocket: ${endpoint.split('?').first}...');

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

      // 5. Open player for received audio
      await _initPlayer();

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
    await _stopPlayer();
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    if (_isPlaying && _player != null) {
      try {
        await _player!.stopPlayer();
      } catch (_) {}
      _isPlaying = false;
    }
    _preBufferFilled = false;
    _audioBuffer.clear();
    _bufferedBytes = 0;
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

  // --- Mic recording (PCM16, 24kHz, mono) ---

  Future<void> _startMicStream() async {
    _recorder = FlutterSoundRecorder();
    await _recorder!.openRecorder();

    _micController = StreamController<Uint8List>();
    _micController!.stream.listen((buffer) {
      if (_isConnected && _channel != null) {
        // Send raw PCM16 bytes directly to WebSocket
        _channel!.sink.add(buffer);
      }
    });

    await _recorder!.startRecorder(
      toStream: _micController!.sink,
      codec: Codec.pcm16,
      numChannels: 1,
      sampleRate: 24000,
      bufferSize: 4800, // 100ms at 24kHz mono 16-bit = 4800 bytes
    );

    Logger.debug('[V2V] Mic recording started at 24kHz');
  }

  Future<void> _stopMicStream() async {
    try {
      await _recorder?.stopRecorder();
      await _recorder?.closeRecorder();
    } catch (_) {}
    _recorder = null;

    try {
      await _micController?.close();
    } catch (_) {}
    _micController = null;
  }

  // --- Audio playback ---

  Future<void> _initPlayer() async {
    _player = FlutterSoundPlayer();
    await _player!.openPlayer();
    Logger.debug('[V2V] Audio player initialized');
  }

  Future<void> _stopPlayer() async {
    try {
      if (_isPlaying) await _player?.stopPlayer();
      await _player?.closePlayer();
    } catch (_) {}
    _player = null;
    _isPlaying = false;
  }

  Future<void> _playAudioChunk(Uint8List pcmData) async {
    if (_player == null || pcmData.isEmpty) return;

    try {
      if (!_isPlaying) {
        // Accumulate 200ms of audio before starting playback to avoid underruns
        if (!_preBufferFilled) {
          _audioBuffer.add(pcmData);
          _bufferedBytes += pcmData.length;
          if (_bufferedBytes < _preBufferBytes) return;
          _preBufferFilled = true;
        }
        _isPlaying = true;
        await _player!.startPlayerFromStream(
          codec: Codec.pcm16,
          numChannels: 1,
          sampleRate: 24000,
        );
        // Flush pre-buffer
        for (final chunk in _audioBuffer) {
          // ignore: deprecated_member_use
          _player!.foodSink?.add(FoodData(chunk));
        }
        _audioBuffer.clear();
        _bufferedBytes = 0;
      }
      // ignore: deprecated_member_use
      _player!.foodSink?.add(FoodData(pcmData));
    } catch (e) {
      Logger.debug('[V2V] Audio play error: $e');
    }
  }

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      // Binary frame = raw PCM16 audio from proxy
      final audioBytes = Uint8List.fromList(message);
      _playAudioChunk(audioBytes);
      onEvent?.call(V2VEvent(type: 'audio', audio: audioBytes));
      return;
    }

    if (message is String) {
      // JSON event
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
            // User started speaking — interrupt playback
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

  void _finishPlayback() async {
    if (_isPlaying && _player != null) {
      try {
        // Wait for buffered audio to drain before stopping
        await Future.delayed(const Duration(milliseconds: 500));
        if (!_isPlaying) return; // interrupted while waiting
        // ignore: deprecated_member_use
        _player!.foodSink?.add(FoodEvent(() {}));
        await _player!.stopPlayer();
      } catch (_) {}
      _isPlaying = false;
    }
    _preBufferFilled = false;
    _audioBuffer.clear();
    _bufferedBytes = 0;
  }
}
