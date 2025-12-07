import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';
import 'package:omi/services/voice_mode_v2/voice_mode_v2_service.dart';

/// Service for managing on-device heuristics (wake words, patterns)
/// Detects patterns in transcripts locally for low-latency response
class HeuristicsService extends ChangeNotifier {
  static final HeuristicsService _instance = HeuristicsService._internal();
  factory HeuristicsService() => _instance;
  HeuristicsService._internal();

  /// Audio player for chime sounds
  final AudioPlayer _audioPlayer = AudioPlayer();

  /// Current wake words (pulled from n8n or defaults)
  List<String> _wakeWords = [
    'hey ella',
    'hi ella',
    'hello ella',
    'ella',
  ];

  /// Auto-start voice call on wake word detection
  bool _autoStartCall = true;

  /// Callback when wake word is detected
  VoidCallback? onWakeWordDetected;

  /// Whether wake word detection is enabled
  bool _isEnabled = true;

  /// Debounce to prevent multiple triggers
  DateTime? _lastWakeWordTime;
  static const _debounceDuration = Duration(seconds: 3);

  /// Getters
  List<String> get wakeWords => List.unmodifiable(_wakeWords);
  bool get isEnabled => _isEnabled;
  bool get autoStartCall => _autoStartCall;

  /// Set auto-start call behavior
  void setAutoStartCall(bool enabled) {
    _autoStartCall = enabled;
    notifyListeners();
  }

  /// Enable/disable wake word detection
  void setEnabled(bool enabled) {
    _isEnabled = enabled;
    notifyListeners();
  }

  /// Update wake words (called when pulled from n8n)
  void updateWakeWords(List<String> words) {
    _wakeWords = words.map((w) => w.toLowerCase().trim()).toList();
    debugPrint('🎯 [Heuristics] Updated wake words: $_wakeWords');
    notifyListeners();
  }

  /// Scan transcript for wake words
  /// Returns true if wake word detected
  bool scanForWakeWord(String transcript) {
    if (!_isEnabled) return false;

    final normalizedText = transcript.toLowerCase().trim();

    for (final wakeWord in _wakeWords) {
      if (normalizedText.contains(wakeWord)) {
        // Debounce check
        final now = DateTime.now();
        if (_lastWakeWordTime != null &&
            now.difference(_lastWakeWordTime!) < _debounceDuration) {
          debugPrint('🎯 [Heuristics] Wake word debounced (too soon)');
          return false;
        }

        _lastWakeWordTime = now;
        debugPrint('🎯 [Heuristics] WAKE WORD DETECTED: "$wakeWord" in "$normalizedText"');

        // Play chime sound
        _playWakeChime();

        // Trigger callback
        onWakeWordDetected?.call();

        // Auto-start voice call if enabled
        if (_autoStartCall) {
          _autoStartVoiceCall();
        }

        return true;
      }
    }

    return false;
  }

  /// Auto-start voice call when wake word detected
  Future<void> _autoStartVoiceCall() async {
    debugPrint('📞 [Heuristics] Auto-starting voice call...');
    try {
      final v2Service = VoiceModeV2Service();

      // Don't start if already in a call
      if (v2Service.isActive) {
        debugPrint('📞 [Heuristics] Voice call already active, skipping');
        return;
      }

      // Start the V2 voice mode
      final success = await v2Service.start();
      if (success) {
        debugPrint('📞 [Heuristics] Voice call started successfully!');
        // Haptic feedback to confirm
        await HapticFeedback.heavyImpact();
      } else {
        debugPrint('❌ [Heuristics] Failed to start voice call');
      }
    } catch (e) {
      debugPrint('❌ [Heuristics] Error starting voice call: $e');
    }
  }

  /// Play a chime sound to indicate wake word detection
  Future<void> _playWakeChime() async {
    try {
      // Use iOS system sound (1057 = Tink sound, nice subtle chime)
      // Alternative system sounds: 1000-1036 for various tones
      await SystemSound.play(SystemSoundType.click);

      // Also trigger haptic feedback
      await HapticFeedback.mediumImpact();

      debugPrint('🔔 [Heuristics] Played wake chime');
    } catch (e) {
      debugPrint('❌ [Heuristics] Error playing chime: $e');
    }
  }

  /// Play a custom audio file (for future use with downloaded chimes)
  Future<void> playCustomChime(String assetPath) async {
    try {
      await _audioPlayer.setAsset(assetPath);
      await _audioPlayer.play();
      debugPrint('🔔 [Heuristics] Played custom chime: $assetPath');
    } catch (e) {
      debugPrint('❌ [Heuristics] Error playing custom chime: $e');
    }
  }

  /// Pull heuristics from n8n (future implementation)
  /// Called on app startup and when push notification received
  Future<void> pullFromN8n() async {
    // TODO: Implement n8n webhook call to pull heuristics
    // For now, use hardcoded defaults
    debugPrint('🎯 [Heuristics] Using hardcoded defaults (n8n pull not implemented yet)');
  }

  /// Dispose resources
  @override
  void dispose() {
    _audioPlayer.dispose();
    super.dispose();
  }
}
