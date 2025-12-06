import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle, ByteData;
import 'package:just_audio/just_audio.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:path_provider/path_provider.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Voice Mode V2 States (simplified - server handles turn detection)
enum VoiceModeV2State {
  inactive,    // Not connected
  connecting,  // WebSocket connecting
  active,      // Connected, sending audio, ready for conversation
  speaking,    // Playing TTS response from server
}

/// Voice Mode V2 Service
///
/// Connects to Pipecat /v2/voice endpoint for real-time voice conversations.
/// Server-side VAD handles turn detection - client just sends/receives audio.
///
/// Protocol:
/// - Send: Raw PCM16 audio at 16kHz
/// - Receive: TTS audio chunks
/// - VAD handles turn-taking automatically
class VoiceModeV2Service extends ChangeNotifier {
  static final VoiceModeV2Service _instance = VoiceModeV2Service._internal();
  factory VoiceModeV2Service() => _instance;
  VoiceModeV2Service._internal();

  // State
  VoiceModeV2State _state = VoiceModeV2State.inactive;
  VoiceModeV2State get state => _state;
  bool get isActive => _state != VoiceModeV2State.inactive;

  // WebSocket
  WebSocketChannel? _channel;
  StreamSubscription? _subscription;

  // Audio playback - TTS comes as 24kHz PCM16 mono
  static const int _ttsOutputSampleRate = 24000;
  static const int _ttsBitsPerSample = 16;
  static const int _ttsChannels = 1;

  final AudioPlayer _audioPlayer = AudioPlayer();

  // Accumulate TTS chunks with debounce for complete playback
  final BytesBuilder _ttsBuffer = BytesBuilder();
  Timer? _ttsPlaybackDebounce;
  int _ttsChunksReceived = 0;

  // Callbacks for UI
  VoidCallback? onSessionStarted;
  VoidCallback? onSessionEnded;
  Function(String)? onError;

  /// Start voice mode session
  Future<bool> start() async {
    if (_state != VoiceModeV2State.inactive) {
      debugPrint('VoiceModeV2: Already active, ignoring start');
      return false;
    }

    _state = VoiceModeV2State.connecting;
    notifyListeners();

    try {
      final uid = SharedPreferencesUtil().uid;
      final sessionId = DateTime.now().millisecondsSinceEpoch.toString();
      final wsUrl = '${Env.apiBaseUrl!.replaceFirst('https://', 'wss://').replaceFirst('http://', 'ws://')}v2/voice?uid=$uid&session_id=$sessionId';

      debugPrint('VoiceModeV2: Connecting to $wsUrl');

      _channel = WebSocketChannel.connect(Uri.parse(wsUrl));

      // Wait for connection
      await _channel!.ready;

      debugPrint('VoiceModeV2: Connected!');

      _subscription = _channel!.stream.listen(
        _onMessage,
        onError: _onError,
        onDone: _onDone,
      );

      _state = VoiceModeV2State.active;
      _audioBytesSent = 0;
      _audioChunksSent = 0;
      _lastAudioLogTime = null;
      notifyListeners();
      onSessionStarted?.call();

      return true;
    } catch (e) {
      debugPrint('VoiceModeV2: Connection failed: $e');
      _state = VoiceModeV2State.inactive;
      notifyListeners();
      onError?.call('Connection failed: $e');
      return false;
    }
  }

  /// Stop voice mode session - robust cleanup with timeout protection
  Future<void> stop() async {
    if (_state == VoiceModeV2State.inactive) {
      debugPrint('VoiceModeV2: Already inactive, nothing to stop');
      return;
    }

    debugPrint('VoiceModeV2: Stopping session - sent $_audioChunksSent chunks, $_audioBytesSent bytes');

    // Set state to inactive FIRST to prevent re-entry issues
    _state = VoiceModeV2State.inactive;
    notifyListeners();

    // Cancel subscription immediately
    _subscription?.cancel();
    _subscription = null;

    // Clear TTS buffer
    _ttsPlaybackDebounce?.cancel();
    _ttsPlaybackDebounce = null;
    _ttsBuffer.clear();
    _ttsChunksReceived = 0;

    // Stop audio player with timeout
    try {
      await _audioPlayer.stop().timeout(
        const Duration(milliseconds: 500),
        onTimeout: () {
          debugPrint('VoiceModeV2: Audio player stop timed out, continuing');
        },
      );
    } catch (e) {
      debugPrint('VoiceModeV2: Audio player stop error: $e');
    }

    // Close WebSocket with timeout
    try {
      final channel = _channel;
      _channel = null; // Clear reference immediately
      if (channel != null) {
        await channel.sink.close().timeout(
          const Duration(milliseconds: 500),
          onTimeout: () {
            debugPrint('VoiceModeV2: WebSocket close timed out, continuing');
          },
        );
      }
    } catch (e) {
      debugPrint('VoiceModeV2: WebSocket close error: $e');
    }

    debugPrint('VoiceModeV2: Session stopped, ready for restart');
    onSessionEnded?.call();
  }

  /// Force reset state - use if stuck
  void forceReset() {
    debugPrint('VoiceModeV2: Force reset');
    _subscription?.cancel();
    _subscription = null;
    _channel = null;
    _ttsPlaybackDebounce?.cancel();
    _ttsBuffer.clear();
    _ttsChunksReceived = 0;
    _state = VoiceModeV2State.inactive;
    notifyListeners();
  }

  // Audio stats tracking
  int _audioBytesSent = 0;
  int _audioChunksSent = 0;
  DateTime? _lastAudioLogTime;

  /// Send audio data to server
  /// Call this continuously with PCM16 16kHz audio chunks
  void sendAudio(Uint8List audioData) {
    // ONLY send audio when active (listening to user)
    // Do NOT send during 'speaking' state - prevents feedback loop
    // where AI's TTS output is picked up by mic and sent back!
    if (_state != VoiceModeV2State.active) {
      return;
    }

    if (_channel == null) {
      debugPrint('VoiceModeV2: sendAudio skipped - channel is null');
      return;
    }

    _channel!.sink.add(audioData);
    _audioBytesSent += audioData.length;
    _audioChunksSent++;

    // Log every second to avoid spam
    final now = DateTime.now();
    if (_lastAudioLogTime == null || now.difference(_lastAudioLogTime!).inSeconds >= 1) {
      debugPrint('VoiceModeV2: Sent $_audioChunksSent chunks, $_audioBytesSent bytes total');
      _lastAudioLogTime = now;
    }
  }

  /// Handle incoming WebSocket message
  void _onMessage(dynamic message) {
    if (message is Uint8List) {
      // Binary data = TTS audio chunk
      debugPrint('VoiceModeV2: Received audio chunk (${message.length} bytes)');
      _queueAudio(message);
    } else if (message is String) {
      // Text = control message (JSON)
      debugPrint('VoiceModeV2: Received message: $message');
      _handleControlMessage(message);
    }
  }

  /// Handle control messages from server
  void _handleControlMessage(String message) {
    // TODO: Parse JSON control messages
    // Expected events:
    // - session_start: Session initialized
    // - speaking_start: Server is sending TTS
    // - speaking_end: TTS finished
    // - session_end: Session terminated
    // - error: Error occurred
  }

  /// Queue audio for playback - accumulates TTS chunks with debounce
  void _queueAudio(Uint8List audioData) {
    // Accumulate TTS chunks
    _ttsBuffer.add(audioData);
    _ttsChunksReceived++;

    if (_state != VoiceModeV2State.speaking) {
      _state = VoiceModeV2State.speaking;
      notifyListeners();
    }

    // Debounce: wait 200ms after last chunk before playing
    // This allows all TTS chunks to arrive before playback
    _ttsPlaybackDebounce?.cancel();
    _ttsPlaybackDebounce = Timer(const Duration(milliseconds: 200), () {
      _playAccumulatedTTS();
    });
  }

  /// Play accumulated TTS audio
  Future<void> _playAccumulatedTTS() async {
    if (_ttsBuffer.isEmpty) return;

    final pcmData = _ttsBuffer.toBytes();
    _ttsBuffer.clear();

    final chunksPlayed = _ttsChunksReceived;
    _ttsChunksReceived = 0;

    debugPrint('VoiceModeV2: Playing TTS - $chunksPlayed chunks, ${pcmData.length} bytes (${(pcmData.length / (_ttsOutputSampleRate * 2)).toStringAsFixed(2)}s)');

    try {
      // Create WAV file from PCM data and play
      final wavFile = await _createWavFile(pcmData);
      await _audioPlayer.setFilePath(wavFile.path);
      await _audioPlayer.play();

      // Wait for playback to complete
      await _audioPlayer.playerStateStream.firstWhere(
        (state) => state.processingState == ProcessingState.completed,
      );

      debugPrint('VoiceModeV2: TTS playback completed');
    } catch (e) {
      debugPrint('VoiceModeV2: Playback error: $e');
    }

    // Return to active state after playback
    if (_state == VoiceModeV2State.speaking) {
      _state = VoiceModeV2State.active;
      notifyListeners();
    }
  }

  /// Create WAV file from raw PCM16 data
  Future<File> _createWavFile(Uint8List pcmData) async {
    final tempDir = await getTemporaryDirectory();
    final wavPath = '${tempDir.path}/v2_tts_${DateTime.now().millisecondsSinceEpoch}.wav';
    final wavFile = File(wavPath);

    // Build WAV header
    final wavHeader = _buildWavHeader(pcmData.length);

    // Combine header + PCM data
    final wavData = BytesBuilder();
    wavData.add(wavHeader);
    wavData.add(pcmData);

    await wavFile.writeAsBytes(wavData.toBytes());

    debugPrint('VoiceModeV2: Created WAV file: $wavPath (${wavData.length} bytes)');

    return wavFile;
  }

  /// Build WAV header for PCM16 audio
  Uint8List _buildWavHeader(int pcmDataLength) {
    final header = ByteData(44);

    // RIFF header
    header.setUint8(0, 0x52); // 'R'
    header.setUint8(1, 0x49); // 'I'
    header.setUint8(2, 0x46); // 'F'
    header.setUint8(3, 0x46); // 'F'
    header.setUint32(4, 36 + pcmDataLength, Endian.little); // File size - 8
    header.setUint8(8, 0x57);  // 'W'
    header.setUint8(9, 0x41);  // 'A'
    header.setUint8(10, 0x56); // 'V'
    header.setUint8(11, 0x45); // 'E'

    // fmt chunk
    header.setUint8(12, 0x66); // 'f'
    header.setUint8(13, 0x6D); // 'm'
    header.setUint8(14, 0x74); // 't'
    header.setUint8(15, 0x20); // ' '
    header.setUint32(16, 16, Endian.little); // Subchunk1Size (16 for PCM)
    header.setUint16(20, 1, Endian.little);  // AudioFormat (1 = PCM)
    header.setUint16(22, _ttsChannels, Endian.little); // NumChannels
    header.setUint32(24, _ttsOutputSampleRate, Endian.little); // SampleRate (24kHz)
    header.setUint32(28, _ttsOutputSampleRate * _ttsChannels * (_ttsBitsPerSample ~/ 8), Endian.little); // ByteRate
    header.setUint16(32, _ttsChannels * (_ttsBitsPerSample ~/ 8), Endian.little); // BlockAlign
    header.setUint16(34, _ttsBitsPerSample, Endian.little); // BitsPerSample

    // data chunk
    header.setUint8(36, 0x64); // 'd'
    header.setUint8(37, 0x61); // 'a'
    header.setUint8(38, 0x74); // 't'
    header.setUint8(39, 0x61); // 'a'
    header.setUint32(40, pcmDataLength, Endian.little); // Subchunk2Size

    return header.buffer.asUint8List();
  }

  /// Handle WebSocket error
  void _onError(Object error) {
    debugPrint('VoiceModeV2: WebSocket error: $error');
    onError?.call('Connection error: $error');
    stop();
  }

  /// Handle WebSocket close
  void _onDone() {
    debugPrint('VoiceModeV2: WebSocket closed');
    if (_state != VoiceModeV2State.inactive) {
      stop();
    }
  }

  /// Test V2 voice mode with bundled audio file
  /// This sends a pre-recorded speech sample to test the full pipeline
  Future<bool> runTest() async {
    debugPrint('VoiceModeV2: Starting test with bundled audio...');

    // Start session if not already active
    if (_state == VoiceModeV2State.inactive) {
      final success = await start();
      if (!success) {
        debugPrint('VoiceModeV2: Test failed - could not connect');
        return false;
      }
    }

    try {
      // Load bundled test audio
      final ByteData audioData = await rootBundle.load('assets/audio/test_voice_mode.pcm');
      final Uint8List audioBytes = audioData.buffer.asUint8List();
      debugPrint('VoiceModeV2: Loaded test audio: ${audioBytes.length} bytes');

      // Send audio at real-time pace (100ms chunks)
      const chunkSize = 3200; // 100ms at 16kHz PCM16
      int chunksSent = 0;

      debugPrint('VoiceModeV2: Sending test audio at real-time pace...');
      for (int i = 0; i < audioBytes.length; i += chunkSize) {
        final end = (i + chunkSize < audioBytes.length) ? i + chunkSize : audioBytes.length;
        final chunk = audioBytes.sublist(i, end);
        sendAudio(Uint8List.fromList(chunk));
        chunksSent++;
        await Future.delayed(const Duration(milliseconds: 100));
      }

      // Send 3 seconds of silence to trigger VAD
      debugPrint('VoiceModeV2: Sending 3s silence to trigger VAD...');
      final silence = Uint8List(chunkSize);
      for (int i = 0; i < 30; i++) {
        sendAudio(silence);
        await Future.delayed(const Duration(milliseconds: 100));
      }

      debugPrint('VoiceModeV2: Test audio sent ($chunksSent chunks + 3s silence)');
      debugPrint('VoiceModeV2: Waiting for TTS response...');

      // Wait up to 15 seconds for response
      final startTime = DateTime.now();
      while (_state == VoiceModeV2State.active &&
             DateTime.now().difference(startTime).inSeconds < 15) {
        await Future.delayed(const Duration(milliseconds: 100));
      }

      // Check if we got a response
      if (_state == VoiceModeV2State.speaking) {
        debugPrint('VoiceModeV2: Test SUCCESS - received TTS response!');

        // Wait for playback to complete
        while (_state == VoiceModeV2State.speaking) {
          await Future.delayed(const Duration(milliseconds: 100));
        }

        debugPrint('VoiceModeV2: Test complete - TTS playback finished');
        return true;
      } else {
        debugPrint('VoiceModeV2: Test FAILED - no TTS response received');
        return false;
      }
    } catch (e) {
      debugPrint('VoiceModeV2: Test error: $e');
      return false;
    } finally {
      await stop();
    }
  }

  /// Dispose resources
  @override
  void dispose() {
    _ttsPlaybackDebounce?.cancel();
    stop();
    _audioPlayer.dispose();
    super.dispose();
  }
}
