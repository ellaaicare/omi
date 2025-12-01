import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
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

  // Audio playback
  final AudioPlayer _audioPlayer = AudioPlayer();
  final List<Uint8List> _audioQueue = [];
  bool _isPlayingQueue = false;

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

  /// Stop voice mode session
  Future<void> stop() async {
    debugPrint('VoiceModeV2: Stopping session - sent $_audioChunksSent chunks, $_audioBytesSent bytes');

    _subscription?.cancel();
    _subscription = null;

    await _channel?.sink.close();
    _channel = null;

    await _audioPlayer.stop();
    _audioQueue.clear();
    _isPlayingQueue = false;

    _state = VoiceModeV2State.inactive;
    notifyListeners();
    onSessionEnded?.call();
  }

  // Audio stats tracking
  int _audioBytesSent = 0;
  int _audioChunksSent = 0;
  DateTime? _lastAudioLogTime;

  /// Send audio data to server
  /// Call this continuously with PCM16 16kHz audio chunks
  void sendAudio(Uint8List audioData) {
    if (_state != VoiceModeV2State.active && _state != VoiceModeV2State.speaking) {
      debugPrint('VoiceModeV2: sendAudio skipped - state is $_state');
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

  /// Queue audio for playback
  void _queueAudio(Uint8List audioData) {
    _audioQueue.add(audioData);

    if (_state != VoiceModeV2State.speaking) {
      _state = VoiceModeV2State.speaking;
      notifyListeners();
    }

    if (!_isPlayingQueue) {
      _playAudioQueue();
    }
  }

  /// Play queued audio chunks
  Future<void> _playAudioQueue() async {
    _isPlayingQueue = true;

    while (_audioQueue.isNotEmpty) {
      final chunk = _audioQueue.removeAt(0);

      try {
        await _playChunk(chunk);
      } catch (e) {
        debugPrint('VoiceModeV2: Playback error: $e');
      }
    }

    _isPlayingQueue = false;

    // Return to active state after playback
    if (_state == VoiceModeV2State.speaking) {
      _state = VoiceModeV2State.active;
      notifyListeners();
    }
  }

  /// Play a single audio chunk
  Future<void> _playChunk(Uint8List chunk) async {
    // TODO: Determine audio format from server (PCM16 or MP3?)
    // For now, assume same format as v1 (may need adjustment)

    // Write to temp file and play
    // This is a simplified version - may need to buffer more
    debugPrint('VoiceModeV2: Playing chunk (${chunk.length} bytes)');

    // Placeholder - actual implementation depends on audio format from server
    // await _audioPlayer.setAudioSource(...)
    // await _audioPlayer.play()
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

  /// Dispose resources
  @override
  void dispose() {
    stop();
    _audioPlayer.dispose();
    super.dispose();
  }
}
