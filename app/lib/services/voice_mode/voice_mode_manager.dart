import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';

/// Voice Mode States
enum VoiceModeState {
  inactive,      // Not in voice mode
  listening,     // Recording user speech
  transcribing,  // Processing user speech
  thinking,      // Waiting for Ella's response
  speaking,      // Playing Ella's audio response
}

/// Trigger types for voice mode
enum VoiceModeTrigger {
  button,    // User pressed the button
  wakeWord,  // Wake word detected
}

/// Voice Mode Manager
///
/// Coordinates voice conversations with Ella AI.
/// Handles state machine, audio capture routing, streaming playback,
/// and wake word detection.
class VoiceModeManager extends ChangeNotifier {
  static final VoiceModeManager _instance = VoiceModeManager._internal();
  factory VoiceModeManager() => _instance;
  VoiceModeManager._internal();

  // Current state
  VoiceModeState _state = VoiceModeState.inactive;
  VoiceModeState get state => _state;
  bool get isActive => _state != VoiceModeState.inactive;

  // Session info
  String? _sessionId;
  String? get sessionId => _sessionId;

  // Current transcript (for UI display)
  String _currentTranscript = '';
  String get currentTranscript => _currentTranscript;

  // Ella's response text (for UI display)
  String _responseText = '';
  String get responseText => _responseText;

  // Wake word detection
  bool _wakeWordEnabled = true;
  bool get wakeWordEnabled => _wakeWordEnabled;
  double _wakeWordConfidence = 0.0;

  // Wake word phrases (can be updated from backend config)
  final List<String> _wakeWordPhrases = [
    'hey ella',
    'hi ella',
    'okay ella',
    'hey ala',  // Common misrecognition
    'hi ala',
  ];

  // Audio playback for streaming response
  final AudioPlayer _audioPlayer = AudioPlayer();
  final List<_AudioChunk> _audioQueue = [];
  bool _isPlayingQueue = false;
  int _audioSequence = 0;

  // Wake word sound
  final AudioPlayer _wakeWordSoundPlayer = AudioPlayer();

  // Callbacks for WebSocket integration
  Function(Map<String, dynamic>)? onSendWebSocketEvent;

  // Timeout handling
  Timer? _silenceTimer;
  static const Duration silenceTimeout = Duration(seconds: 3);
  static const Duration sessionTimeout = Duration(seconds: 120);
  Timer? _sessionTimer;

  /// Initialize voice mode manager
  Future<void> initialize() async {
    // Pre-load wake word sound for instant playback
    // TODO: Add actual sound asset
    debugPrint('VoiceModeManager initialized');
  }

  /// Start voice mode (button triggered)
  Future<void> startFromButton() async {
    await _startVoiceMode(VoiceModeTrigger.button);
  }

  /// Start voice mode (wake word triggered)
  Future<void> startFromWakeWord(double confidence) async {
    _wakeWordConfidence = confidence;
    await _playWakeWordSound();
    await _startVoiceMode(VoiceModeTrigger.wakeWord);
  }

  /// Internal start voice mode
  Future<void> _startVoiceMode(VoiceModeTrigger trigger) async {
    if (isActive) {
      debugPrint('VoiceModeManager: Already active, ignoring start');
      return;
    }

    debugPrint('VoiceModeManager: Starting voice mode (trigger: $trigger)');

    _state = VoiceModeState.listening;
    _currentTranscript = '';
    _responseText = '';
    _audioSequence = 0;
    _audioQueue.clear();
    notifyListeners();

    // Send start event to backend
    _sendEvent({
      'event': 'voice_mode_start',
      'trigger': trigger == VoiceModeTrigger.button ? 'button' : 'wake_word',
      if (trigger == VoiceModeTrigger.wakeWord) 'wake_word_confidence': _wakeWordConfidence,
    });

    // Start session timeout
    _sessionTimer?.cancel();
    _sessionTimer = Timer(sessionTimeout, () {
      debugPrint('VoiceModeManager: Session timeout');
      stop(reason: 'session_timeout');
    });
  }

  /// Stop voice mode
  Future<void> stop({String reason = 'user_request'}) async {
    if (!isActive) return;

    debugPrint('VoiceModeManager: Stopping voice mode (reason: $reason)');

    // Send stop event to backend
    _sendEvent({
      'event': 'voice_mode_stop',
      'reason': reason,
    });

    await _cleanup();
  }

  /// Cleanup and reset state
  Future<void> _cleanup() async {
    _silenceTimer?.cancel();
    _sessionTimer?.cancel();
    _audioQueue.clear();
    _isPlayingQueue = false;

    await _audioPlayer.stop();

    _state = VoiceModeState.inactive;
    _sessionId = null;
    _currentTranscript = '';
    _responseText = '';
    notifyListeners();
  }

  /// Toggle voice mode (for button)
  Future<void> toggle() async {
    if (isActive) {
      await stop();
    } else {
      await startFromButton();
    }
  }

  /// Check transcript for wake word
  /// Returns true if wake word detected
  bool checkForWakeWord(String transcript) {
    if (!_wakeWordEnabled || isActive) return false;

    final lowerTranscript = transcript.toLowerCase().trim();

    for (final phrase in _wakeWordPhrases) {
      if (lowerTranscript.contains(phrase)) {
        debugPrint('VoiceModeManager: Wake word detected: "$phrase" in "$transcript"');

        // Calculate rough confidence based on transcript length
        // Shorter = more confident it's just the wake word
        final confidence = lowerTranscript.length < 15 ? 0.95 : 0.8;

        // Trigger voice mode
        startFromWakeWord(confidence);
        return true;
      }
    }

    return false;
  }

  /// Play wake word acknowledgment sound
  Future<void> _playWakeWordSound() async {
    try {
      // TODO: Use actual asset sound file
      // For now, use a simple system sound or skip
      debugPrint('VoiceModeManager: Playing wake word sound');
      // await _wakeWordSoundPlayer.play(AssetSource('sounds/wake_word_bong.mp3'));
    } catch (e) {
      debugPrint('VoiceModeManager: Wake word sound error: $e');
    }
  }

  /// Handle user speech final (end of utterance)
  void onUserSpeechFinal(String transcript) {
    if (_state != VoiceModeState.listening) return;

    _currentTranscript = transcript;
    _state = VoiceModeState.transcribing;
    notifyListeners();

    // Reset silence timer
    _silenceTimer?.cancel();
  }

  /// Handle interim transcript update
  void onTranscriptUpdate(String transcript) {
    if (_state != VoiceModeState.listening) return;

    _currentTranscript = transcript;
    notifyListeners();

    // Reset silence timer on activity
    _resetSilenceTimer();
  }

  /// Reset silence timer
  void _resetSilenceTimer() {
    _silenceTimer?.cancel();
    _silenceTimer = Timer(silenceTimeout, () {
      debugPrint('VoiceModeManager: Silence timeout - ending utterance');
      if (_state == VoiceModeState.listening && _currentTranscript.isNotEmpty) {
        // Signal end of user speech
        _sendEvent({
          'event': 'voice_audio',
          'data': '',  // Empty = end marker
          'sequence': _audioSequence++,
          'is_final': true,
        });
        _state = VoiceModeState.transcribing;
        notifyListeners();
      }
    });
  }

  // ============================================
  // Backend Event Handlers
  // ============================================

  /// Handle voice_mode_active event from backend
  void handleVoiceModeActive(Map<String, dynamic> data) {
    _sessionId = data['session_id'] as String?;
    debugPrint('VoiceModeManager: Session active: $_sessionId');

    // Update timeout if backend specifies
    final timeoutSeconds = data['timeout_seconds'] as int?;
    if (timeoutSeconds != null) {
      _sessionTimer?.cancel();
      _sessionTimer = Timer(Duration(seconds: timeoutSeconds), () {
        stop(reason: 'session_timeout');
      });
    }
  }

  /// Handle voice_transcription event from backend
  void handleVoiceTranscription(Map<String, dynamic> data) {
    final text = data['text'] as String? ?? '';
    final isFinal = data['is_final'] as bool? ?? false;

    _currentTranscript = text;

    if (isFinal) {
      _state = VoiceModeState.thinking;
    }

    notifyListeners();
  }

  /// Handle voice_status event from backend
  void handleVoiceStatus(Map<String, dynamic> data) {
    final status = data['status'] as String? ?? '';

    switch (status) {
      case 'listening':
        _state = VoiceModeState.listening;
        break;
      case 'transcribing':
        _state = VoiceModeState.transcribing;
        break;
      case 'thinking':
        _state = VoiceModeState.thinking;
        break;
      case 'speaking':
        _state = VoiceModeState.speaking;
        break;
    }

    notifyListeners();
  }

  /// Handle voice_response_audio event from backend
  void handleVoiceResponseAudio(Map<String, dynamic> data) {
    final audioData = data['data'] as String?;
    final sequence = data['sequence'] as int? ?? 0;
    final format = data['format'] as String? ?? 'mp3';
    final sampleRate = data['sample_rate'] as int? ?? 24000;

    if (audioData == null || audioData.isEmpty) return;

    debugPrint('VoiceModeManager: Received audio chunk $sequence (format: $format, rate: $sampleRate)');

    // Queue audio for playback
    _audioQueue.add(_AudioChunk(
      data: audioData,
      sequence: sequence,
      format: format,
      sampleRate: sampleRate,
    ));

    // Start playing if not already
    if (!_isPlayingQueue) {
      _state = VoiceModeState.speaking;
      notifyListeners();
      _playAudioQueue();
    }
  }

  /// Handle voice_response_complete event from backend
  void handleVoiceResponseComplete(Map<String, dynamic> data) {
    _responseText = data['text'] as String? ?? '';
    final durationMs = data['duration_ms'] as int? ?? 0;

    debugPrint('VoiceModeManager: Response complete (${durationMs}ms): $_responseText');

    // Wait for audio queue to finish, then go back to listening
    // or end session based on backend instruction
  }

  /// Handle voice_mode_ended event from backend
  void handleVoiceModeEnded(Map<String, dynamic> data) {
    final reason = data['reason'] as String? ?? 'unknown';
    debugPrint('VoiceModeManager: Backend ended session: $reason');
    _cleanup();
  }

  /// Handle voice_error event from backend
  void handleVoiceError(Map<String, dynamic> data) {
    final code = data['code'] as String? ?? 'unknown';
    final message = data['message'] as String? ?? 'Unknown error';

    debugPrint('VoiceModeManager: Error [$code]: $message');

    // TODO: Show error to user
    // For now, just stop voice mode
    _cleanup();
  }

  // ============================================
  // Audio Playback
  // ============================================

  /// Play queued audio chunks
  Future<void> _playAudioQueue() async {
    _isPlayingQueue = true;

    while (_audioQueue.isNotEmpty) {
      final chunk = _audioQueue.removeAt(0);

      try {
        await _playChunk(chunk);
      } catch (e) {
        debugPrint('VoiceModeManager: Audio playback error: $e');
      }
    }

    _isPlayingQueue = false;

    // After all audio played, go back to listening (multi-turn) or end
    if (_state == VoiceModeState.speaking) {
      _state = VoiceModeState.listening;
      notifyListeners();
      _resetSilenceTimer();
    }
  }

  /// Play a single audio chunk based on its format
  Future<void> _playChunk(_AudioChunk chunk) async {
    // Check if data is a URL
    if (chunk.data.startsWith('http://') || chunk.data.startsWith('https://')) {
      debugPrint('VoiceModeManager: Playing audio from URL');
      await _audioPlayer.setUrl(chunk.data);
      await _audioPlayer.play();
      // Wait for playback to complete
      await _audioPlayer.processingStateStream.firstWhere(
        (state) => state == ProcessingState.completed,
      );
      return;
    }

    // Decode base64 data
    final Uint8List bytes;
    try {
      bytes = base64Decode(chunk.data);
    } catch (e) {
      debugPrint('VoiceModeManager: Failed to decode base64 audio: $e');
      return;
    }

    debugPrint('VoiceModeManager: Playing ${bytes.length} bytes of ${chunk.format} audio');

    // Get temp directory for audio file
    final tempDir = await getTemporaryDirectory();
    final tempFile = File('${tempDir.path}/voice_chunk_${chunk.sequence}.${_getFileExtension(chunk.format)}');

    // Prepare audio data based on format
    Uint8List audioBytes;
    if (chunk.format == 'pcm16' || chunk.format == 'pcm') {
      // Wrap PCM16 data with WAV header so just_audio can play it
      audioBytes = _createWavFromPcm16(bytes, chunk.sampleRate);
    } else {
      // MP3, WAV, etc. - use as-is
      audioBytes = bytes;
    }

    // Write to temp file
    await tempFile.writeAsBytes(audioBytes);

    // Play the audio
    await _audioPlayer.setFilePath(tempFile.path);
    await _audioPlayer.play();

    // Wait for playback to complete
    await _audioPlayer.processingStateStream.firstWhere(
      (state) => state == ProcessingState.completed,
    );

    // Clean up temp file
    try {
      await tempFile.delete();
    } catch (_) {}
  }

  /// Create WAV file from raw PCM16 mono data
  Uint8List _createWavFromPcm16(Uint8List pcmData, int sampleRate) {
    const int channels = 1;
    const int bitsPerSample = 16;
    final int byteRate = sampleRate * channels * bitsPerSample ~/ 8;
    const int blockAlign = channels * bitsPerSample ~/ 8;
    final int dataSize = pcmData.length;
    final int fileSize = 36 + dataSize;

    final ByteData header = ByteData(44);

    // RIFF header
    header.setUint8(0, 0x52); // 'R'
    header.setUint8(1, 0x49); // 'I'
    header.setUint8(2, 0x46); // 'F'
    header.setUint8(3, 0x46); // 'F'
    header.setUint32(4, fileSize, Endian.little);
    header.setUint8(8, 0x57);  // 'W'
    header.setUint8(9, 0x41);  // 'A'
    header.setUint8(10, 0x56); // 'V'
    header.setUint8(11, 0x45); // 'E'

    // fmt subchunk
    header.setUint8(12, 0x66); // 'f'
    header.setUint8(13, 0x6D); // 'm'
    header.setUint8(14, 0x74); // 't'
    header.setUint8(15, 0x20); // ' '
    header.setUint32(16, 16, Endian.little); // Subchunk1Size (16 for PCM)
    header.setUint16(20, 1, Endian.little);  // AudioFormat (1 = PCM)
    header.setUint16(22, channels, Endian.little);
    header.setUint32(24, sampleRate, Endian.little);
    header.setUint32(28, byteRate, Endian.little);
    header.setUint16(32, blockAlign, Endian.little);
    header.setUint16(34, bitsPerSample, Endian.little);

    // data subchunk
    header.setUint8(36, 0x64); // 'd'
    header.setUint8(37, 0x61); // 'a'
    header.setUint8(38, 0x74); // 't'
    header.setUint8(39, 0x61); // 'a'
    header.setUint32(40, dataSize, Endian.little);

    // Combine header and PCM data
    final result = Uint8List(44 + dataSize);
    result.setRange(0, 44, header.buffer.asUint8List());
    result.setRange(44, 44 + dataSize, pcmData);

    return result;
  }

  /// Get file extension for audio format
  String _getFileExtension(String format) {
    switch (format.toLowerCase()) {
      case 'mp3':
        return 'mp3';
      case 'wav':
        return 'wav';
      case 'pcm16':
      case 'pcm':
        return 'wav'; // We'll wrap it with WAV header
      case 'aac':
        return 'aac';
      case 'ogg':
        return 'ogg';
      default:
        return 'mp3';
    }
  }

  // ============================================
  // WebSocket Integration
  // ============================================

  /// Send event to backend via WebSocket
  void _sendEvent(Map<String, dynamic> event) {
    if (onSendWebSocketEvent != null) {
      onSendWebSocketEvent!(event);
    } else {
      debugPrint('VoiceModeManager: No WebSocket callback set, event not sent: $event');
    }
  }

  /// Route incoming WebSocket event
  void handleWebSocketEvent(String eventType, Map<String, dynamic> data) {
    switch (eventType) {
      case 'voice_mode_active':
        handleVoiceModeActive(data);
        break;
      case 'voice_transcription':
        handleVoiceTranscription(data);
        break;
      case 'voice_status':
        handleVoiceStatus(data);
        break;
      case 'voice_response_audio':
        handleVoiceResponseAudio(data);
        break;
      case 'voice_response_complete':
        handleVoiceResponseComplete(data);
        break;
      case 'voice_mode_ended':
        handleVoiceModeEnded(data);
        break;
      case 'voice_error':
        handleVoiceError(data);
        break;
    }
  }

  // ============================================
  // Configuration
  // ============================================

  /// Enable/disable wake word detection
  void setWakeWordEnabled(bool enabled) {
    _wakeWordEnabled = enabled;
    notifyListeners();
  }

  /// Update wake word phrases from backend config
  void updateWakeWordPhrases(List<String> phrases) {
    _wakeWordPhrases.clear();
    _wakeWordPhrases.addAll(phrases.map((p) => p.toLowerCase()));
    debugPrint('VoiceModeManager: Updated wake phrases: $_wakeWordPhrases');
  }

  @override
  void dispose() {
    _silenceTimer?.cancel();
    _sessionTimer?.cancel();
    _audioPlayer.dispose();
    _wakeWordSoundPlayer.dispose();
    super.dispose();
  }
}

/// Helper class for audio chunks with metadata
class _AudioChunk {
  final String data;
  final int sequence;
  final String format;
  final int sampleRate;

  _AudioChunk({
    required this.data,
    required this.sequence,
    required this.format,
    required this.sampleRate,
  });
}
