/// Wake Word Detection Plugin
///
/// Detects wake words in transcribed text and triggers voice calls.
///
/// This is a SKELETON - port implementation from standalone Ella app.
///
/// Key integration points:
/// - [onTranscriptReceived]: Called by EllaExtensions when transcript arrives
/// - [onWakeWordDetected]: Callback when wake word is detected
///
/// Example usage:
/// ```dart
/// final plugin = WakeWordPlugin();
/// await plugin.initialize();
///
/// plugin.onWakeWordDetected = () {
///   print('Wake word detected! Starting call...');
///   voicePlugin.startCall();
/// };
///
/// // Feed transcripts
/// plugin.onTranscriptReceived('Hey Ella, how are you?');
/// ```
import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../base_plugin.dart';
import '../../config/ella_config.dart';

/// Wake word detection plugin
///
/// Scans incoming transcripts for wake words and triggers callbacks.
class WakeWordPlugin extends EllaPlugin {
  @override
  String get name => 'WakeWord';

  @override
  String get version => '1.0.0';

  // State
  bool _enabled = true;
  bool _inCall = false;
  DateTime? _lastTrigger;

  // Method channel for native iOS ASR (if using on-device)
  static const _channel = MethodChannel('com.ella.wake_word');

  // Callbacks
  /// Called when a wake word is detected
  VoidCallback? onWakeWordDetected;

  /// Called with the detected wake word text
  Function(String detectedWord)? onWakeWordDetectedWithText;

  @override
  Future<void> initialize() async {
    _enabled = EllaConfig().wakeWordEnabled;

    // Setup native method channel handlers (for on-device ASR)
    _channel.setMethodCallHandler(_handleNativeCall);

    debugPrint('[WakeWord] Initialized with wake words: ${EllaConfig().wakeWords}');
  }

  @override
  Future<void> dispose() async {
    _enabled = false;
  }

  /// Handle calls from native iOS code
  Future<dynamic> _handleNativeCall(MethodCall call) async {
    switch (call.method) {
      case 'onTranscript':
        // Native ASR sent a transcript
        final text = call.arguments['text'] as String?;
        if (text != null && text.isNotEmpty) {
          onTranscriptReceived(text);
        }
        return null;

      case 'onWakeWordDetected':
        // Native layer detected wake word directly
        final word = call.arguments['word'] as String?;
        _handleDetection(word ?? 'ella');
        return null;

      default:
        return null;
    }
  }

  /// Process incoming transcript for wake word detection
  ///
  /// Called automatically by EllaExtensions when transcripts arrive.
  @override
  void onTranscriptReceived(String text) {
    if (!_enabled || _inCall) return;

    final normalized = text.toLowerCase().trim();
    final wakeWords = EllaConfig().wakeWords;

    for (final wakeWord in wakeWords) {
      if (normalized.contains(wakeWord)) {
        _handleDetection(wakeWord);
        return; // Only trigger once per transcript
      }
    }
  }

  /// Handle wake word detection
  void _handleDetection(String detectedWord) {
    // Debounce check
    final debounceDuration = Duration(seconds: EllaConfig().wakeWordDebounceSec);
    if (_lastTrigger != null &&
        DateTime.now().difference(_lastTrigger!) < debounceDuration) {
      debugPrint('[WakeWord] Debounced (within ${debounceDuration.inSeconds}s)');
      return;
    }

    _lastTrigger = DateTime.now();

    debugPrint('🎯 [WakeWord] DETECTED: "$detectedWord"');

    // Play chime sound
    _playChime();

    // Trigger callbacks
    onWakeWordDetectedWithText?.call(detectedWord);
    onWakeWordDetected?.call();
  }

  /// Play wake word chime
  Future<void> _playChime() async {
    // TODO: Port chime playback from standalone Ella app
    // Could use just_audio or native audio
    try {
      await _channel.invokeMethod('playChime');
    } catch (e) {
      debugPrint('[WakeWord] Chime playback error: $e');
    }
  }

  /// Set whether currently in a call (disables detection during calls)
  void setInCall(bool value) {
    _inCall = value;
    debugPrint('[WakeWord] In call: $value');
  }

  /// Enable/disable wake word detection
  void setEnabled(bool value) {
    _enabled = value;
    EllaConfig().wakeWordEnabled = value;
    debugPrint('[WakeWord] Enabled: $value');
  }

  /// Check if wake word detection is active
  bool get isActive => _enabled && !_inCall;

  @override
  Map<String, dynamic> getStatus() {
    return {
      ...super.getStatus(),
      'enabled': _enabled,
      'inCall': _inCall,
      'isActive': isActive,
      'wakeWords': EllaConfig().wakeWords,
      'lastTrigger': _lastTrigger?.toIso8601String(),
    };
  }

  // ============================================
  // TODO: PORT FROM STANDALONE ELLA APP
  // ============================================
  //
  // The following features should be ported from the standalone Ella app:
  //
  // 1. Native iOS ASR integration (SFSpeechRecognizer)
  //    - Continuous listening mode
  //    - Bluetooth audio support
  //    - Background operation
  //
  // 2. Chime sound playback
  //    - Load from assets
  //    - Play through current audio route
  //
  // 3. Advanced wake word detection
  //    - Fuzzy matching for variations
  //    - Confidence thresholds
  //    - Multiple language support
  //
  // See: [standalone Ella app]/WakeWordDetector.swift
  // ============================================
}
