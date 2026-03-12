import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_sound/flutter_sound.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
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
  AudioPlayer? _audioPlayer;
  StreamController<Uint8List>? _micController;
  StreamSubscription? _wsSub;
  StreamSubscription? _playerSub;
  bool _isConnected = false;
  bool _isPlaying = false;
  bool _micMuted = false;
  String? _cachedTempDir;

  /// Accumulate all PCM audio chunks for the current response, then play as WAV.
  final BytesBuilder _audioAccumulator = BytesBuilder(copy: false);

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
    _audioAccumulator.clear();
    if (_isPlaying && _audioPlayer != null) {
      try {
        await _audioPlayer!.stop();
      } catch (_) {}
      _isPlaying = false;
    }
    _micMuted = false;
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
      if (_isConnected && _channel != null && !_micMuted) {
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
    _audioPlayer = AudioPlayer();
    _playerSub = _audioPlayer!.playerStateStream.listen((state) {
      if (state.processingState == ProcessingState.completed && _isPlaying) {
        _isPlaying = false;
        _micMuted = false;
        onEvent?.call(V2VEvent(type: 'playback_complete'));
      }
    });
    _cachedTempDir ??= (await getTemporaryDirectory()).path;
    Logger.debug('[V2V] Audio player initialized');
  }

  Future<void> _stopPlayer() async {
    _playerSub?.cancel();
    _playerSub = null;
    try {
      await _audioPlayer?.stop();
      await _audioPlayer?.dispose();
    } catch (_) {}
    _audioPlayer = null;
    _isPlaying = false;
  }

  /// Accumulate PCM chunk — actual playback happens in _finishPlayback on audio_done.
  void _bufferAudioChunk(Uint8List pcmData) {
    if (pcmData.isEmpty) return;
    if (_audioAccumulator.isEmpty) {
      // First chunk of a new response — mute mic
      _micMuted = true;
    }
    _audioAccumulator.add(pcmData);
  }

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      // Binary frame = raw PCM16 audio from proxy — buffer it
      final bytes = Uint8List.fromList(message);
      _bufferAudioChunk(bytes);
      Logger.debug('[V2V] Buffered ${bytes.length}b audio, total: ${_audioAccumulator.length}b');
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

  /// Write accumulated PCM to WAV file and play it with just_audio.
  void _finishPlayback() async {
    final pcmData = _audioAccumulator.takeBytes();
    Logger.debug('[V2V] _finishPlayback: ${pcmData.length}b PCM, player=${_audioPlayer != null}, dir=$_cachedTempDir');
    if (pcmData.isEmpty || _audioPlayer == null) {
      Logger.debug('[V2V] _finishPlayback: skipping — empty=${pcmData.isEmpty}, player=${_audioPlayer == null}');
      _micMuted = false;
      return;
    }

    try {
      // Wrap PCM16 in WAV header
      final wavBytes = _pcmToWav(pcmData, sampleRate: 24000);
      final ts = DateTime.now().millisecondsSinceEpoch;
      final path = '$_cachedTempDir/ella_v2v_$ts.wav';
      await File(path).writeAsBytes(wavBytes, flush: true);
      final fileSize = await File(path).length();

      Logger.debug('[V2V] WAV written: $fileSize bytes → $path');
      _isPlaying = true;
      await _audioPlayer!.setFilePath(path);
      Logger.debug('[V2V] Audio duration: ${_audioPlayer!.duration}');
      await _audioPlayer!.play();
      Logger.debug('[V2V] play() called');
      // _playerSub listener handles completion → unmutes mic
    } catch (e, st) {
      Logger.error('[V2V] Playback error: $e\n$st');
      _isPlaying = false;
      _micMuted = false;
    }
  }

  /// Wrap raw PCM16 mono data in a WAV container.
  static Uint8List _pcmToWav(Uint8List pcmData, {int sampleRate = 24000, int channels = 1, int bitsPerSample = 16}) {
    final dataSize = pcmData.length;
    final fileSize = 36 + dataSize;
    final byteRate = sampleRate * channels * (bitsPerSample ~/ 8);
    final blockAlign = channels * (bitsPerSample ~/ 8);

    final header = ByteData(44);
    // RIFF header
    header.setUint8(0, 0x52); // R
    header.setUint8(1, 0x49); // I
    header.setUint8(2, 0x46); // F
    header.setUint8(3, 0x46); // F
    header.setUint32(4, fileSize, Endian.little);
    header.setUint8(8, 0x57); // W
    header.setUint8(9, 0x41); // A
    header.setUint8(10, 0x56); // V
    header.setUint8(11, 0x45); // E
    // fmt chunk
    header.setUint8(12, 0x66); // f
    header.setUint8(13, 0x6D); // m
    header.setUint8(14, 0x74); // t
    header.setUint8(15, 0x20); // (space)
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little); // PCM
    header.setUint16(22, channels, Endian.little);
    header.setUint32(24, sampleRate, Endian.little);
    header.setUint32(28, byteRate, Endian.little);
    header.setUint16(32, blockAlign, Endian.little);
    header.setUint16(34, bitsPerSample, Endian.little);
    // data chunk
    header.setUint8(36, 0x64); // d
    header.setUint8(37, 0x61); // a
    header.setUint8(38, 0x74); // t
    header.setUint8(39, 0x61); // a
    header.setUint32(40, dataSize, Endian.little);

    final wav = Uint8List(44 + dataSize);
    wav.setRange(0, 44, header.buffer.asUint8List());
    wav.setRange(44, 44 + dataSize, pcmData);
    return wav;
  }
}
