/// Voice-to-Voice (V2V) Plugin
///
/// Real-time bidirectional voice conversations via WebSocket.
/// Connects to Grok V2V backend for low-latency (~500ms) voice AI.
///
/// This is a SKELETON - port implementation from standalone Ella app.
///
/// Protocol:
/// - Send: Raw PCM16 audio at 16kHz
/// - Receive: TTS audio chunks (PCM16 24kHz)
/// - Server handles VAD and turn-taking
///
/// Example usage:
/// ```dart
/// final plugin = VoiceV2VPlugin();
/// await plugin.initialize();
///
/// plugin.onCallStarted = () => print('Call started');
/// plugin.onCallEnded = () => print('Call ended');
///
/// await plugin.startCall();
/// plugin.sendAudio(audioBytes);
/// await plugin.endCall();
/// ```
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../base_plugin.dart';
import '../../config/ella_config.dart';

/// Voice call state
enum VoiceV2VState {
  /// Not connected
  inactive,

  /// WebSocket connecting
  connecting,

  /// Connected, ready to send/receive audio
  active,

  /// Playing TTS response
  speaking,
}

/// Voice-to-Voice plugin for real-time AI conversations
class VoiceV2VPlugin extends EllaPlugin {
  @override
  String get name => 'VoiceV2V';

  @override
  String get version => '1.0.0';

  // State
  VoiceV2VState _state = VoiceV2VState.inactive;
  VoiceV2VState get state => _state;
  bool get isActive => _state != VoiceV2VState.inactive;
  bool get isSpeaking => _state == VoiceV2VState.speaking;

  // WebSocket
  WebSocketChannel? _channel;
  StreamSubscription? _subscription;

  // Audio playback
  final AudioPlayer _audioPlayer = AudioPlayer();

  // TTS buffer (accumulate chunks before playback)
  final BytesBuilder _ttsBuffer = BytesBuilder();
  Timer? _ttsPlaybackDebounce;
  int _ttsChunksReceived = 0;

  // Audio format constants
  static const int _ttsOutputSampleRate = 24000;
  static const int _ttsBitsPerSample = 16;
  static const int _ttsChannels = 1;

  // Stats
  int _audioBytesSent = 0;
  int _audioChunksSent = 0;
  DateTime? _callStartTime;

  // Callbacks
  VoidCallback? onCallStarted;
  VoidCallback? onCallEnded;
  VoidCallback? onSpeakingStarted;
  VoidCallback? onSpeakingEnded;
  Function(String)? onError;
  Function(String)? onTranscript; // User's speech transcribed

  // Native channel for mic audio
  static const _channel_native = MethodChannel('com.ella.voice_v2v');

  @override
  Future<void> initialize() async {
    // Setup native method channel
    _channel_native.setMethodCallHandler(_handleNativeCall);
    debugPrint('[VoiceV2V] Initialized');
  }

  @override
  Future<void> dispose() async {
    await endCall();
    await _audioPlayer.dispose();
  }

  /// Handle calls from native iOS code
  Future<dynamic> _handleNativeCall(MethodCall call) async {
    switch (call.method) {
      case 'onAudioData':
        // Native mic audio data
        final data = call.arguments['data'] as Uint8List?;
        if (data != null) {
          sendAudio(data);
        }
        return null;

      default:
        return null;
    }
  }

  /// Start a voice call
  ///
  /// Connects to the voice WebSocket and begins the session.
  /// Returns true if connection successful.
  Future<bool> startCall({String? pipelineMode, String? uid}) async {
    if (_state != VoiceV2VState.inactive) {
      debugPrint('[VoiceV2V] Already active, ignoring start');
      return false;
    }

    _state = VoiceV2VState.connecting;

    try {
      // Build WebSocket URL
      final mode = pipelineMode ?? EllaConfig().voicePipelineMode;
      final userId = uid ?? 'anonymous'; // TODO: Get from auth
      final sessionId = DateTime.now().millisecondsSinceEpoch.toString();
      final baseUrl = EllaConfig().voiceWsBaseUrl;

      final wsUrl = '$baseUrl/v2/voice?uid=$userId&session_id=$sessionId&pipeline_mode=$mode';

      debugPrint('[VoiceV2V] Connecting with pipeline_mode=$mode');
      debugPrint('[VoiceV2V] URL: $wsUrl');

      _channel = WebSocketChannel.connect(Uri.parse(wsUrl));
      await _channel!.ready;

      debugPrint('[VoiceV2V] Connected!');

      // Listen for messages
      _subscription = _channel!.stream.listen(
        _onMessage,
        onError: _onError,
        onDone: _onDone,
      );

      // Reset stats
      _audioBytesSent = 0;
      _audioChunksSent = 0;
      _callStartTime = DateTime.now();

      _state = VoiceV2VState.active;
      onCallStarted?.call();

      // Start native mic capture
      await _startMicCapture();

      return true;
    } catch (e) {
      debugPrint('[VoiceV2V] Connection failed: $e');
      _state = VoiceV2VState.inactive;
      onError?.call('Connection failed: $e');
      return false;
    }
  }

  /// End the voice call
  Future<void> endCall() async {
    if (_state == VoiceV2VState.inactive) return;

    final duration = _callStartTime != null
        ? DateTime.now().difference(_callStartTime!).inSeconds
        : 0;

    debugPrint('[VoiceV2V] Ending call - duration: ${duration}s, sent $_audioChunksSent chunks, $_audioBytesSent bytes');

    // Set state first to prevent re-entry
    _state = VoiceV2VState.inactive;

    // Stop mic capture
    await _stopMicCapture();

    // Cancel subscription
    _subscription?.cancel();
    _subscription = null;

    // Clear TTS buffer
    _ttsPlaybackDebounce?.cancel();
    _ttsBuffer.clear();
    _ttsChunksReceived = 0;

    // Stop audio player
    try {
      await _audioPlayer.stop();
    } catch (e) {
      debugPrint('[VoiceV2V] Audio player stop error: $e');
    }

    // Close WebSocket
    try {
      final channel = _channel;
      _channel = null;
      await channel?.sink.close();
    } catch (e) {
      debugPrint('[VoiceV2V] WebSocket close error: $e');
    }

    debugPrint('[VoiceV2V] Call ended');
    onCallEnded?.call();
  }

  /// Send audio data to server
  ///
  /// Call continuously with PCM16 16kHz audio chunks.
  /// Audio is NOT sent during 'speaking' state to prevent feedback.
  void sendAudio(Uint8List audioData) {
    // Only send when active (not during TTS playback)
    if (_state != VoiceV2VState.active) return;
    if (_channel == null) return;

    _channel!.sink.add(audioData);
    _audioBytesSent += audioData.length;
    _audioChunksSent++;
  }

  /// Handle incoming WebSocket message
  void _onMessage(dynamic message) {
    if (message is Uint8List) {
      // Binary = TTS audio chunk
      debugPrint('[VoiceV2V] Received TTS chunk (${message.length} bytes)');
      _queueTtsAudio(message);
    } else if (message is String) {
      // Text = control message (JSON)
      debugPrint('[VoiceV2V] Received message: $message');
      _handleControlMessage(message);
    }
  }

  /// Handle control messages from server
  void _handleControlMessage(String message) {
    // TODO: Parse JSON control messages
    // Expected events:
    // - session_start: Session initialized
    // - speaking_start: Server sending TTS
    // - speaking_end: TTS finished
    // - transcript: User's speech transcribed
    // - error: Error occurred

    try {
      // For now, just log
      debugPrint('[VoiceV2V] Control: $message');
    } catch (e) {
      debugPrint('[VoiceV2V] Error parsing control message: $e');
    }
  }

  /// Queue TTS audio for playback
  void _queueTtsAudio(Uint8List audioData) {
    _ttsBuffer.add(audioData);
    _ttsChunksReceived++;

    if (_state != VoiceV2VState.speaking) {
      _state = VoiceV2VState.speaking;
      onSpeakingStarted?.call();
    }

    // Debounce: wait 200ms after last chunk before playing
    _ttsPlaybackDebounce?.cancel();
    _ttsPlaybackDebounce = Timer(const Duration(milliseconds: 200), () {
      _playAccumulatedTts();
    });
  }

  /// Play accumulated TTS audio
  Future<void> _playAccumulatedTts() async {
    if (_ttsBuffer.isEmpty) return;

    final pcmData = _ttsBuffer.toBytes();
    _ttsBuffer.clear();

    final chunksPlayed = _ttsChunksReceived;
    _ttsChunksReceived = 0;

    final duration = pcmData.length / (_ttsOutputSampleRate * 2);
    debugPrint('[VoiceV2V] Playing TTS - $chunksPlayed chunks, ${pcmData.length} bytes (${duration.toStringAsFixed(2)}s)');

    try {
      final wavFile = await _createWavFile(pcmData);
      await _audioPlayer.setFilePath(wavFile.path);
      await _audioPlayer.play();

      // Wait for playback to complete
      await _audioPlayer.playerStateStream.firstWhere(
        (state) => state.processingState == ProcessingState.completed,
      );

      debugPrint('[VoiceV2V] TTS playback completed');
    } catch (e) {
      debugPrint('[VoiceV2V] Playback error: $e');
    }

    // Return to active state
    if (_state == VoiceV2VState.speaking) {
      _state = VoiceV2VState.active;
      onSpeakingEnded?.call();
    }
  }

  /// Create WAV file from PCM data
  Future<File> _createWavFile(Uint8List pcmData) async {
    final tempDir = await getTemporaryDirectory();
    final wavPath = '${tempDir.path}/v2v_tts_${DateTime.now().millisecondsSinceEpoch}.wav';
    final wavFile = File(wavPath);

    final wavHeader = _buildWavHeader(pcmData.length);
    final wavData = BytesBuilder();
    wavData.add(wavHeader);
    wavData.add(pcmData);

    await wavFile.writeAsBytes(wavData.toBytes());
    return wavFile;
  }

  /// Build WAV header
  Uint8List _buildWavHeader(int pcmDataLength) {
    final header = ByteData(44);

    // RIFF header
    header.setUint8(0, 0x52); // R
    header.setUint8(1, 0x49); // I
    header.setUint8(2, 0x46); // F
    header.setUint8(3, 0x46); // F
    header.setUint32(4, 36 + pcmDataLength, Endian.little);
    header.setUint8(8, 0x57);  // W
    header.setUint8(9, 0x41);  // A
    header.setUint8(10, 0x56); // V
    header.setUint8(11, 0x45); // E

    // fmt chunk
    header.setUint8(12, 0x66); // f
    header.setUint8(13, 0x6D); // m
    header.setUint8(14, 0x74); // t
    header.setUint8(15, 0x20); // space
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little); // PCM
    header.setUint16(22, _ttsChannels, Endian.little);
    header.setUint32(24, _ttsOutputSampleRate, Endian.little);
    header.setUint32(28, _ttsOutputSampleRate * _ttsChannels * (_ttsBitsPerSample ~/ 8), Endian.little);
    header.setUint16(32, _ttsChannels * (_ttsBitsPerSample ~/ 8), Endian.little);
    header.setUint16(34, _ttsBitsPerSample, Endian.little);

    // data chunk
    header.setUint8(36, 0x64); // d
    header.setUint8(37, 0x61); // a
    header.setUint8(38, 0x74); // t
    header.setUint8(39, 0x61); // a
    header.setUint32(40, pcmDataLength, Endian.little);

    return header.buffer.asUint8List();
  }

  /// Start native mic capture
  Future<void> _startMicCapture() async {
    try {
      await _channel_native.invokeMethod('startMicCapture');
      debugPrint('[VoiceV2V] Mic capture started');
    } catch (e) {
      debugPrint('[VoiceV2V] Failed to start mic capture: $e');
    }
  }

  /// Stop native mic capture
  Future<void> _stopMicCapture() async {
    try {
      await _channel_native.invokeMethod('stopMicCapture');
      debugPrint('[VoiceV2V] Mic capture stopped');
    } catch (e) {
      debugPrint('[VoiceV2V] Failed to stop mic capture: $e');
    }
  }

  void _onError(Object error) {
    debugPrint('[VoiceV2V] WebSocket error: $error');
    onError?.call('Connection error: $error');
    endCall();
  }

  void _onDone() {
    debugPrint('[VoiceV2V] WebSocket closed');
    if (_state != VoiceV2VState.inactive) {
      endCall();
    }
  }

  @override
  Map<String, dynamic> getStatus() {
    return {
      ...super.getStatus(),
      'state': _state.name,
      'isActive': isActive,
      'isSpeaking': isSpeaking,
      'audioBytesSent': _audioBytesSent,
      'audioChunksSent': _audioChunksSent,
      'callStartTime': _callStartTime?.toIso8601String(),
    };
  }

  // ============================================
  // TODO: PORT FROM STANDALONE ELLA APP
  // ============================================
  //
  // The following should be ported from the standalone Ella app:
  //
  // 1. Native iOS mic capture
  //    - AVAudioEngine setup
  //    - Bluetooth audio routing
  //    - PCM16 16kHz format
  //
  // 2. Control message parsing
  //    - Session events
  //    - Transcript delivery
  //    - Error handling
  //
  // 3. Advanced features
  //    - Interrupt handling (stop TTS on user speech)
  //    - Audio ducking
  //    - Call quality metrics
  //
  // See: [standalone Ella app]/GrokVoiceSession.swift
  // ============================================
}
