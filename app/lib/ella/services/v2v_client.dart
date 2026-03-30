import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:audio_session/audio_session.dart';
import 'package:just_audio/just_audio.dart';
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

  /// just_audio player for WAV playback
  final AudioPlayer _audioPlayer = AudioPlayer();

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

    // 2. Configure iOS audio session for playAndRecord with Bluetooth + speaker routing
    await _configureAudioSession();

    // 3. Connect WebSocket
    final wsUrl = '$endpoint&token=$token';
    Logger.debug('[V2V] Connecting to WebSocket...');

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

      // 5. Listen for playback completion
      _audioPlayer.playerStateStream.listen((state) {
        if (state.processingState == ProcessingState.completed && _isPlaying) {
          _onPlaybackComplete();
        }
      });

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
      await _audioPlayer.stop();
      await _audioPlayer.dispose();
    } catch (_) {}
  }

  /// Interrupt current playback (e.g., user started speaking).
  Future<void> interruptPlayback() async {
    if (_isPlaying) {
      try {
        await _audioPlayer.stop();
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

  // --- Audio playback using just_audio (WAV file) ---

  /// Buffer incoming PCM16 audio chunk.
  void _bufferAudioChunk(Uint8List pcmData) {
    if (pcmData.isEmpty) return;

    _chunkCount++;
    _pcmBuffer.add(pcmData);

    // Mute mic on first chunk
    if (!_micMuted) {
      _micMuted = true;
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Buffering audio (mic muted)'));
      Logger.debug('[V2V] First audio chunk, mic muted');
    }

    if (_chunkCount % 20 == 0) {
      Logger.debug('[V2V] Buffered $_chunkCount chunks, ${_pcmBuffer.length}B');
    }
  }

  /// Called on audio_done — write WAV and play with just_audio.
  Future<void> _finishPlayback() async {
    final pcmBytes = _pcmBuffer.toBytes();
    final stats = '$_chunkCount chunks, ${(pcmBytes.length / 1024).toStringAsFixed(1)}KB';
    Logger.debug('[V2V] Audio done: $stats');
    onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Playing: $stats'));

    _pcmBuffer.clear();
    _chunkCount = 0;

    if (pcmBytes.isEmpty) {
      Logger.debug('[V2V] No audio data to play');
      _micMuted = false;
      onEvent?.call(V2VEvent(type: 'playback_complete'));
      return;
    }

    // Stop mic recording before switching audio session to playback
    await _stopMicStream();

    // Write WAV file
    final wavPath = '${Directory.systemTemp.path}/v2v_response.wav';
    final wavBytes = _buildWav(Uint8List.fromList(pcmBytes), sampleRate: 24000, numChannels: 1, bitsPerSample: 16);
    await File(wavPath).writeAsBytes(wavBytes);
    final durationMs = (pcmBytes.length / (24000 * 2)) * 1000;
    Logger.debug('[V2V] WAV written: ${wavBytes.length}B, ${durationMs.toStringAsFixed(0)}ms');

    // Play with just_audio
    try {
      _isPlaying = true;
      await _audioPlayer.setVolume(1.0);
      await _audioPlayer.setFilePath(wavPath);
      await _audioPlayer.play();
      Logger.debug('[V2V] Playback started');

      // Safety timeout — if playerStateStream doesn't fire completion within
      // expected duration + 3s buffer, force restart mic anyway
      final timeoutMs = durationMs.toInt() + 3000;
      Future.delayed(Duration(milliseconds: timeoutMs), () {
        if (_isPlaying) {
          Logger.debug('[V2V] Playback safety timeout after ${timeoutMs}ms, forcing mic restart');
          _onPlaybackComplete();
        }
      });
    } catch (e) {
      Logger.error('[V2V] Playback error: $e');
      onEvent?.call(V2VEvent(type: 'v2v_debug', text: 'Play error: $e'));
      _isPlaying = false;
      _micMuted = false;
      // Restart mic even on error
      if (_isConnected) await _startMicStream();
      onEvent?.call(V2VEvent(type: 'playback_complete'));
    }
  }

  /// Called when just_audio finishes playing.
  Future<void> _onPlaybackComplete() async {
    if (!_isPlaying && !_micMuted) return; // Already handled
    Logger.debug('[V2V] Playback complete, restarting mic');
    _isPlaying = false;
    _micMuted = false;

    // Restart mic recording
    if (_isConnected) {
      await _startMicStream();
    }

    onEvent?.call(V2VEvent(type: 'playback_complete'));
  }

  /// Build a WAV file from raw PCM16 data.
  static Uint8List _buildWav(Uint8List pcmData, {int sampleRate = 24000, int numChannels = 1, int bitsPerSample = 16}) {
    final byteRate = sampleRate * numChannels * (bitsPerSample ~/ 8);
    final blockAlign = numChannels * (bitsPerSample ~/ 8);
    final dataSize = pcmData.length;
    final fileSize = 36 + dataSize;

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
    header.setUint32(16, 16, Endian.little); // chunk size
    header.setUint16(20, 1, Endian.little); // PCM format
    header.setUint16(22, numChannels, Endian.little);
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

  // --- WebSocket message handling ---

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      // Binary frame = raw PCM16 audio from proxy — buffer for WAV playback
      _bufferAudioChunk(Uint8List.fromList(message));
      return;
    }

    if (message is String) {
      // JSON event
      Logger.debug('[V2V] JSON event: $message');
      try {
        final json = jsonDecode(message) as Map<String, dynamic>;
        final type = json['type'] as String? ?? 'unknown';
        final text = json['text'] as String? ?? json['transcript'] as String?;

        // Handle legacy {"error": "..."} format sent by older proxy versions
        if (type == 'unknown') {
          final legacyError = json['error'] as String?;
          if (legacyError != null) {
            Logger.error('[V2V] Server error (legacy format): $legacyError');
            onEvent?.call(V2VEvent(type: 'error', text: legacyError));
            return;
          }
        }

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
