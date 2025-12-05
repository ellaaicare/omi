import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/services/asr/on_device_asr_service.dart' as asr;

/// Detects voice commands to answer or decline incoming calls
class VoiceAnswerDetector {
  // Default phrases (can be customized in settings)
  static const List<String> defaultAnswerPhrases = [
    'answer',
    'pick up',
    'yes',
    'hello',
    'accept',
    'hey',
    'hi',
  ];

  static const List<String> defaultDeclinePhrases = [
    'decline',
    'voicemail',
    'no',
    'busy',
    'later',
    'not now',
    'ignore',
    'reject',
  ];

  // Current phrases (loaded from settings)
  List<String> _answerPhrases = defaultAnswerPhrases;
  List<String> _declinePhrases = defaultDeclinePhrases;

  // ASR service
  final asr.OnDeviceASRService _asrService = asr.OnDeviceASRService();
  StreamSubscription? _transcriptSubscription;

  // Callbacks
  VoidCallback? _onAnswer;
  VoidCallback? _onDecline;

  // State
  bool _isListening = false;
  bool get isListening => _isListening;

  // Debounce to prevent multiple triggers
  bool _actionTaken = false;

  /// Load custom phrases from settings
  void loadCustomPhrases() {
    final customAnswer = SharedPreferencesUtil().getString('answer_phrases');
    final customDecline = SharedPreferencesUtil().getString('decline_phrases');

    if (customAnswer != null && customAnswer.isNotEmpty) {
      _answerPhrases = customAnswer.split(',').map((p) => p.trim().toLowerCase()).toList();
    }

    if (customDecline != null && customDecline.isNotEmpty) {
      _declinePhrases = customDecline.split(',').map((p) => p.trim().toLowerCase()).toList();
    }

    debugPrint('VoiceAnswerDetector: Answer phrases: $_answerPhrases');
    debugPrint('VoiceAnswerDetector: Decline phrases: $_declinePhrases');
  }

  /// Start listening for answer/decline commands
  Future<void> startListening({
    required VoidCallback onAnswer,
    required VoidCallback onDecline,
  }) async {
    if (_isListening) {
      debugPrint('VoiceAnswerDetector: Already listening');
      return;
    }

    _onAnswer = onAnswer;
    _onDecline = onDecline;
    _actionTaken = false;

    // Load custom phrases
    loadCustomPhrases();

    debugPrint('VoiceAnswerDetector: Starting voice detection');

    // Request authorization
    final authorized = await _asrService.requestAuthorization();
    if (!authorized) {
      debugPrint('VoiceAnswerDetector: ASR not authorized');
      return;
    }

    // Subscribe to transcripts
    _transcriptSubscription = _asrService.transcriptStream.listen((segment) {
      if (_actionTaken) return;

      final text = segment.text.toLowerCase().trim();
      if (text.isEmpty) return;

      debugPrint('VoiceAnswerDetector: Heard: "$text"');

      // Check for answer phrases
      for (final phrase in _answerPhrases) {
        if (text.contains(phrase)) {
          debugPrint('VoiceAnswerDetector: ANSWER detected ("$phrase" in "$text")');
          _actionTaken = true;
          _onAnswer?.call();
          stopListening();
          return;
        }
      }

      // Check for decline phrases
      for (final phrase in _declinePhrases) {
        if (text.contains(phrase)) {
          debugPrint('VoiceAnswerDetector: DECLINE detected ("$phrase" in "$text")');
          _actionTaken = true;
          _onDecline?.call();
          stopListening();
          return;
        }
      }
    });

    // Start ASR
    try {
      await _asrService.startTranscription();
      _isListening = true;
      debugPrint('VoiceAnswerDetector: Now listening for commands');
    } catch (e) {
      debugPrint('VoiceAnswerDetector: Failed to start ASR: $e');
      _transcriptSubscription?.cancel();
    }
  }

  /// Stop listening
  Future<void> stopListening() async {
    if (!_isListening) return;

    debugPrint('VoiceAnswerDetector: Stopping voice detection');

    _isListening = false;
    _transcriptSubscription?.cancel();
    _transcriptSubscription = null;

    try {
      await _asrService.stopTranscription();
    } catch (e) {
      debugPrint('VoiceAnswerDetector: Error stopping ASR: $e');
    }
  }

  /// Dispose resources
  void dispose() {
    stopListening();
  }
}
