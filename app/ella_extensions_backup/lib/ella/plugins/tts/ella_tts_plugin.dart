/// Ella TTS Plugin
///
/// Text-to-Speech with modular provider architecture.
/// Supports backend TTS API with fallback to native iOS TTS.
///
/// Provider pattern allows easy extension:
/// - EllaBackendProvider: Our backend API (default)
/// - NativeTTSProvider: iOS AVSpeechSynthesizer fallback
/// - Future: OpenAI direct, ElevenLabs, etc.
///
/// Example usage:
/// ```dart
/// final plugin = EllaTtsPlugin();
/// await plugin.initialize();
///
/// // Speak with default provider
/// await plugin.speak('Hello, how are you?');
///
/// // Speak with specific voice
/// await plugin.speak('Time for medication', voice: 'shimmer');
///
/// // Switch provider
/// plugin.setProvider(NativeTTSProvider());
/// ```
import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:just_audio/just_audio.dart';

import '../base_plugin.dart';
import '../../config/ella_config.dart';

/// TTS Provider interface
///
/// Implement this to add new TTS backends.
abstract class TTSProvider {
  String get name;

  /// Generate audio URL for text
  /// Returns URL to audio file (MP3/WAV)
  Future<String> generateAudioUrl(String text, {String? voice});

  /// Get available voices
  Future<List<String>> getVoices();
}

/// Ella Backend TTS Provider
///
/// Uses our backend API with Redis caching.
class EllaBackendTTSProvider implements TTSProvider {
  @override
  String get name => 'EllaBackend';

  @override
  Future<String> generateAudioUrl(String text, {String? voice}) async {
    final config = EllaConfig();
    final baseUrl = config.ttsApiBaseUrl;
    final defaultVoice = voice ?? config.ttsDefaultVoice;

    final body = {
      'text': text,
      'voice': defaultVoice,
      'model': 'hd',
    };

    // Add cache key if caching enabled
    if (config.ttsCachingEnabled) {
      body['cache_key'] = '${text.hashCode}_$defaultVoice';
    }

    debugPrint('[EllaBackendTTS] Generating audio for: "${text.substring(0, text.length > 50 ? 50 : text.length)}..."');

    final response = await http.post(
      Uri.parse('$baseUrl/api/v1/tts/generate'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode(body),
    );

    if (response.statusCode != 200) {
      throw Exception('TTS API error: ${response.statusCode} - ${response.body}');
    }

    final data = jsonDecode(response.body);
    final audioUrl = data['audio_url'] as String;
    final cached = data['cached'] as bool? ?? false;

    debugPrint('[EllaBackendTTS] Generated audio (cached: $cached): $audioUrl');

    return audioUrl;
  }

  @override
  Future<List<String>> getVoices() async {
    final baseUrl = EllaConfig().ttsApiBaseUrl;

    try {
      final response = await http.get(
        Uri.parse('$baseUrl/api/v1/tts/voices'),
      );

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return List<String>.from(data['voices'] ?? []);
      }
    } catch (e) {
      debugPrint('[EllaBackendTTS] Failed to get voices: $e');
    }

    // Return default voices
    return ['nova', 'shimmer', 'alloy', 'echo', 'fable', 'onyx'];
  }
}

/// Native iOS TTS Provider
///
/// Uses iOS AVSpeechSynthesizer as fallback.
class NativeTTSProvider implements TTSProvider {
  static const _channel = MethodChannel('com.ella.native_tts');

  @override
  String get name => 'NativeiOS';

  @override
  Future<String> generateAudioUrl(String text, {String? voice}) async {
    // Native TTS doesn't generate URLs - it speaks directly
    // This will be handled specially in EllaTtsPlugin
    throw UnsupportedError('Native TTS does not generate audio URLs');
  }

  @override
  Future<List<String>> getVoices() async {
    // iOS voices
    return ['Samantha', 'Alex', 'Victoria', 'Daniel'];
  }

  /// Speak text directly using native TTS
  Future<void> speakDirect(String text, {String? voice}) async {
    try {
      await _channel.invokeMethod('speak', {
        'text': text,
        'voice': voice,
      });
    } catch (e) {
      debugPrint('[NativeTTS] Error: $e');
      rethrow;
    }
  }

  /// Stop current speech
  Future<void> stop() async {
    try {
      await _channel.invokeMethod('stop');
    } catch (e) {
      debugPrint('[NativeTTS] Stop error: $e');
    }
  }
}

/// Ella TTS Plugin
///
/// Manages TTS with provider pattern and automatic fallback.
class EllaTtsPlugin extends EllaPlugin {
  @override
  String get name => 'EllaTTS';

  @override
  String get version => '1.0.0';

  // Current provider
  TTSProvider _provider = EllaBackendTTSProvider();
  final NativeTTSProvider _nativeProvider = NativeTTSProvider();

  // Audio player for URL-based playback
  final AudioPlayer _audioPlayer = AudioPlayer();

  // State
  bool _isSpeaking = false;
  bool get isSpeaking => _isSpeaking;

  // Callbacks
  VoidCallback? onSpeakingStarted;
  VoidCallback? onSpeakingEnded;
  Function(String)? onError;

  @override
  Future<void> initialize() async {
    debugPrint('[EllaTTS] Initialized with provider: ${_provider.name}');
  }

  @override
  Future<void> dispose() async {
    await stop();
    await _audioPlayer.dispose();
  }

  /// Speak text using current provider
  ///
  /// Falls back to native TTS if primary provider fails.
  Future<void> speak(String text, {String? voice}) async {
    if (text.isEmpty) return;

    _isSpeaking = true;
    onSpeakingStarted?.call();

    try {
      if (_provider is NativeTTSProvider) {
        // Use native directly
        await (_provider as NativeTTSProvider).speakDirect(text, voice: voice);
      } else {
        // Get audio URL and play
        final audioUrl = await _provider.generateAudioUrl(text, voice: voice);
        await _playAudioUrl(audioUrl);
      }
    } catch (e) {
      debugPrint('[EllaTTS] Provider ${_provider.name} failed: $e');

      // Fallback to native if enabled
      if (EllaConfig().ttsFallbackToNative && _provider is! NativeTTSProvider) {
        debugPrint('[EllaTTS] Falling back to native TTS');
        try {
          await _nativeProvider.speakDirect(text, voice: voice);
        } catch (e2) {
          debugPrint('[EllaTTS] Native fallback also failed: $e2');
          onError?.call('TTS failed: $e');
        }
      } else {
        onError?.call('TTS failed: $e');
      }
    } finally {
      _isSpeaking = false;
      onSpeakingEnded?.call();
    }
  }

  /// Play audio from URL
  Future<void> _playAudioUrl(String url) async {
    debugPrint('[EllaTTS] Playing audio: $url');

    await _audioPlayer.setUrl(url);
    await _audioPlayer.play();

    // Wait for completion
    await _audioPlayer.playerStateStream.firstWhere(
      (state) => state.processingState == ProcessingState.completed,
    );
  }

  /// Stop current speech
  Future<void> stop() async {
    try {
      await _audioPlayer.stop();
      await _nativeProvider.stop();
    } catch (e) {
      debugPrint('[EllaTTS] Stop error: $e');
    }
    _isSpeaking = false;
  }

  /// Set TTS provider
  void setProvider(TTSProvider provider) {
    _provider = provider;
    debugPrint('[EllaTTS] Provider changed to: ${provider.name}');
  }

  /// Get current provider name
  String get currentProvider => _provider.name;

  /// Get available voices from current provider
  Future<List<String>> getVoices() async {
    return _provider.getVoices();
  }

  @override
  Map<String, dynamic> getStatus() {
    return {
      ...super.getStatus(),
      'provider': _provider.name,
      'isSpeaking': _isSpeaking,
      'fallbackEnabled': EllaConfig().ttsFallbackToNative,
    };
  }
}
