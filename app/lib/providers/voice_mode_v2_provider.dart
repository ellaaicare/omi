import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/services/voice_mode_v2/voice_mode_v2_service.dart';

/// Provider for Voice Mode V2 (Pipecat)
///
/// Wraps the VoiceModeV2Service for use with Provider pattern.
/// Connects to wss://api.ella-ai-care.com/v2/voice for server-side VAD.
class VoiceModeV2Provider extends ChangeNotifier {
  final VoiceModeV2Service _service = VoiceModeV2Service();

  VoiceModeV2Provider() {
    _service.addListener(_onServiceChange);
    _service.onSessionStarted = _onSessionStarted;
    _service.onSessionEnded = _onSessionEnded;
    _service.onError = _onError;
  }

  // Delegate state to service
  VoiceModeV2State get state => _service.state;
  bool get isActive => _service.isActive;
  bool get isConnecting => _service.state == VoiceModeV2State.connecting;
  bool get isSpeaking => _service.state == VoiceModeV2State.speaking;

  // Error state
  String? _lastError;
  String? get lastError => _lastError;

  // Session tracking
  DateTime? _sessionStartTime;
  Duration get sessionDuration => _sessionStartTime != null
      ? DateTime.now().difference(_sessionStartTime!)
      : Duration.zero;

  /// Check if V2 voice mode is enabled in preferences
  static bool get isEnabled {
    return SharedPreferencesUtil().voiceModeV2Enabled;
  }

  /// Enable/disable V2 voice mode
  static void setEnabled(bool enabled) {
    SharedPreferencesUtil().voiceModeV2Enabled = enabled;
  }

  /// Start voice mode session
  Future<bool> start() async {
    _lastError = null;
    notifyListeners();

    final success = await _service.start();
    if (!success && _lastError == null) {
      _lastError = 'Failed to connect to voice server';
    }
    return success;
  }

  /// Stop voice mode session
  Future<void> stop() async {
    await _service.stop();
  }

  /// Send audio data to server
  /// Call this with PCM16 16kHz audio chunks from audio capture
  void sendAudio(Uint8List audioData) {
    _service.sendAudio(audioData);
  }

  void _onServiceChange() {
    notifyListeners();
  }

  void _onSessionStarted() {
    _sessionStartTime = DateTime.now();
    debugPrint('VoiceModeV2Provider: Session started');
  }

  void _onSessionEnded() {
    _sessionStartTime = null;
    debugPrint('VoiceModeV2Provider: Session ended');
  }

  void _onError(String error) {
    _lastError = error;
    debugPrint('VoiceModeV2Provider: Error - $error');
    notifyListeners();
  }

  @override
  void dispose() {
    _service.removeListener(_onServiceChange);
    _service.onSessionStarted = null;
    _service.onSessionEnded = null;
    _service.onError = null;
    super.dispose();
  }
}
