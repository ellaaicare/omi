import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:just_audio/just_audio.dart';

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
  final List<String> _audioQueue = [];
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

    if (audioData == null || audioData.isEmpty) return;

    debugPrint('VoiceModeManager: Received audio chunk $sequence');

    // Queue audio for playback
    _audioQueue.add(audioData);

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
      final audioData = _audioQueue.removeAt(0);

      try {
        // Decode base64 and play
        final bytes = base64Decode(audioData);

        // For PCM16, we need to convert or use a different player
        // AudioPlayer expects encoded formats (mp3, wav with header, etc.)
        // TODO: Use AVAudioEngine on iOS for raw PCM playback

        // Temporary: Skip raw PCM, wait for proper implementation
        debugPrint('VoiceModeManager: Would play ${bytes.length} bytes of audio');

        // Simulate playback time
        await Future.delayed(const Duration(milliseconds: 100));

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
