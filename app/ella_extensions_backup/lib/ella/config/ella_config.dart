/// Ella Configuration
///
/// Centralized configuration for all Ella extensions.
/// Loaded from SharedPreferences with sensible defaults.
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Ella-specific configuration
///
/// Singleton pattern for consistent config access.
class EllaConfig {
  static final EllaConfig _instance = EllaConfig._internal();
  factory EllaConfig() => _instance;
  EllaConfig._internal();

  SharedPreferences? _prefs;

  // ============================================
  // WAKE WORD CONFIGURATION
  // ============================================

  /// Wake words to detect (lowercase)
  List<String> get wakeWords =>
      _prefs?.getStringList('ella_wake_words') ??
      ['hey ella', 'hi ella', 'hello ella', 'ella'];

  set wakeWords(List<String> value) =>
      _prefs?.setStringList('ella_wake_words', value);

  /// Enable wake word detection
  bool get wakeWordEnabled => _prefs?.getBool('ella_wake_word_enabled') ?? true;

  set wakeWordEnabled(bool value) =>
      _prefs?.setBool('ella_wake_word_enabled', value);

  /// Auto-start voice call on wake word detection
  bool get autoStartCallOnWakeWord =>
      _prefs?.getBool('ella_auto_start_call') ?? true;

  set autoStartCallOnWakeWord(bool value) =>
      _prefs?.setBool('ella_auto_start_call', value);

  /// Wake word debounce duration in seconds
  int get wakeWordDebounceSec =>
      _prefs?.getInt('ella_wake_word_debounce_sec') ?? 3;

  set wakeWordDebounceSec(int value) =>
      _prefs?.setInt('ella_wake_word_debounce_sec', value);

  // ============================================
  // VOICE V2V CONFIGURATION
  // ============================================

  /// Voice pipeline mode: 'default' (Pipecat) or 'grok_v2v' (~500ms)
  String get voicePipelineMode =>
      _prefs?.getString('ella_voice_pipeline_mode') ?? 'grok_v2v';

  set voicePipelineMode(String value) =>
      _prefs?.setString('ella_voice_pipeline_mode', value);

  /// Voice WebSocket base URL
  String get voiceWsBaseUrl =>
      _prefs?.getString('ella_voice_ws_base_url') ??
      'wss://api.ella-ai-care.com';

  set voiceWsBaseUrl(String value) =>
      _prefs?.setString('ella_voice_ws_base_url', value);

  /// Enable voice mode
  bool get voiceModeEnabled =>
      _prefs?.getBool('ella_voice_mode_enabled') ?? true;

  set voiceModeEnabled(bool value) =>
      _prefs?.setBool('ella_voice_mode_enabled', value);

  // ============================================
  // TTS CONFIGURATION
  // ============================================

  /// TTS API base URL
  String get ttsApiBaseUrl =>
      _prefs?.getString('ella_tts_api_base_url') ??
      'https://api.ella-ai-care.com';

  set ttsApiBaseUrl(String value) =>
      _prefs?.setString('ella_tts_api_base_url', value);

  /// Default TTS voice
  String get ttsDefaultVoice =>
      _prefs?.getString('ella_tts_default_voice') ?? 'nova';

  set ttsDefaultVoice(String value) =>
      _prefs?.setString('ella_tts_default_voice', value);

  /// Enable TTS caching
  bool get ttsCachingEnabled =>
      _prefs?.getBool('ella_tts_caching_enabled') ?? true;

  set ttsCachingEnabled(bool value) =>
      _prefs?.setBool('ella_tts_caching_enabled', value);

  /// Fallback to native TTS on API failure
  bool get ttsFallbackToNative =>
      _prefs?.getBool('ella_tts_fallback_native') ?? true;

  set ttsFallbackToNative(bool value) =>
      _prefs?.setBool('ella_tts_fallback_native', value);

  // ============================================
  // AUDIO PUSH CONFIGURATION
  // ============================================

  /// Enable audio in push notifications
  bool get audioPushEnabled =>
      _prefs?.getBool('ella_audio_push_enabled') ?? true;

  set audioPushEnabled(bool value) =>
      _prefs?.setBool('ella_audio_push_enabled', value);

  // ============================================
  // LIFECYCLE
  // ============================================

  /// Load configuration from SharedPreferences
  Future<void> load() async {
    _prefs = await SharedPreferences.getInstance();
    debugPrint('[EllaConfig] Configuration loaded');
  }

  /// Export config as JSON (for debugging)
  Map<String, dynamic> toJson() {
    return {
      'wakeWord': {
        'enabled': wakeWordEnabled,
        'words': wakeWords,
        'autoStartCall': autoStartCallOnWakeWord,
        'debounceSec': wakeWordDebounceSec,
      },
      'voiceV2V': {
        'enabled': voiceModeEnabled,
        'pipelineMode': voicePipelineMode,
        'wsBaseUrl': voiceWsBaseUrl,
      },
      'tts': {
        'apiBaseUrl': ttsApiBaseUrl,
        'defaultVoice': ttsDefaultVoice,
        'cachingEnabled': ttsCachingEnabled,
        'fallbackToNative': ttsFallbackToNative,
      },
      'audioPush': {
        'enabled': audioPushEnabled,
      },
    };
  }

  /// Reset all Ella config to defaults
  Future<void> resetToDefaults() async {
    final keys = _prefs?.getKeys().where((k) => k.startsWith('ella_')) ?? [];
    for (final key in keys) {
      await _prefs?.remove(key);
    }
    debugPrint('[EllaConfig] Reset to defaults');
  }
}
