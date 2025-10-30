import 'package:flutter_tts/flutter_tts.dart';
import 'dart:io';

/// Ella TTS Service - Text-to-Speech with automatic Bluetooth routing
///
/// Features:
/// - Automatically routes audio to connected Bluetooth headsets (AirPods, etc.)
/// - Falls back to phone speaker if no Bluetooth audio device connected
/// - Supports voice selection and customization
/// - NO CONFIGURATION NEEDED - iOS handles routing automatically!
class EllaTtsService {
  static final EllaTtsService _instance = EllaTtsService._internal();
  factory EllaTtsService() => _instance;

  final FlutterTts _flutterTts = FlutterTts();
  bool _isInitialized = false;

  EllaTtsService._internal();

  /// Initialize TTS engine
  Future<void> initialize() async {
    if (_isInitialized) return;

    try {
      // Configure TTS settings
      await _flutterTts.setLanguage("en-US");
      await _flutterTts.setSpeechRate(0.5); // Normal speed (0.0-1.0)
      await _flutterTts.setVolume(1.0); // Full volume (0.0-1.0)
      await _flutterTts.setPitch(1.0); // Normal pitch (0.5-2.0)

      // iOS-specific: Audio automatically routes to Bluetooth if available!
      if (Platform.isIOS) {
        await _flutterTts.setSharedInstance(true);
        // iOS AVAudioSession will handle routing to:
        // - AirPods (if connected)
        // - Bluetooth headset (if connected)
        // - Phone speaker (fallback)
      }

      _isInitialized = true;
    } catch (e) {
      print('EllaTtsService initialization error: $e');
      rethrow;
    }
  }

  /// Speak text - automatically routes to Bluetooth headset if connected
  ///
  /// This is SMART ROUTING:
  /// 1. If AirPods/Bluetooth headset connected → audio goes there
  /// 2. If no Bluetooth → audio goes to phone speaker
  /// 3. If necklace has speaker (future) → can route there too
  Future<void> speak(String text) async {
    await initialize();

    if (text.isEmpty) return;

    try {
      await _flutterTts.speak(text);
    } catch (e) {
      print('EllaTtsService speak error: $e');
    }
  }

  /// Stop speaking immediately
  Future<void> stop() async {
    try {
      await _flutterTts.stop();
    } catch (e) {
      print('EllaTtsService stop error: $e');
    }
  }

  /// Pause speaking (can be resumed)
  Future<void> pause() async {
    try {
      await _flutterTts.pause();
    } catch (e) {
      print('EllaTtsService pause error: $e');
    }
  }

  /// Check if TTS is currently speaking
  Future<bool> isSpeaking() async {
    try {
      // Note: This may not be available on all platforms
      return false; // Placeholder
    } catch (e) {
      return false;
    }
  }

  /// Get available voices
  Future<List<dynamic>> getVoices() async {
    await initialize();
    try {
      return await _flutterTts.getVoices ?? [];
    } catch (e) {
      print('EllaTtsService getVoices error: $e');
      return [];
    }
  }

  /// Set voice by name (e.g., "com.apple.ttsbundle.Samantha-compact")
  Future<void> setVoice(Map<String, String> voice) async {
    try {
      await _flutterTts.setVoice(voice);
    } catch (e) {
      print('EllaTtsService setVoice error: $e');
    }
  }

  /// Set speech rate (0.0 = very slow, 1.0 = very fast)
  Future<void> setSpeechRate(double rate) async {
    try {
      await _flutterTts.setSpeechRate(rate.clamp(0.0, 1.0));
    } catch (e) {
      print('EllaTtsService setSpeechRate error: $e');
    }
  }

  /// Set pitch (0.5 = low, 1.0 = normal, 2.0 = high)
  Future<void> setPitch(double pitch) async {
    try {
      await _flutterTts.setPitch(pitch.clamp(0.5, 2.0));
    } catch (e) {
      print('EllaTtsService setPitch error: $e');
    }
  }

  /// Set volume (0.0 = silent, 1.0 = full)
  Future<void> setVolume(double volume) async {
    try {
      await _flutterTts.setVolume(volume.clamp(0.0, 1.0));
    } catch (e) {
      print('EllaTtsService setVolume error: $e');
    }
  }

  /// Sample health notification messages for testing
  static const Map<String, String> sampleMessages = {
    'medication': 'Reminder: It\'s time to take your blood pressure medication. Please take one pill with water.',
    'appointment': 'You have a doctor\'s appointment tomorrow at 2 PM with Dr. Smith. Don\'t forget to bring your insurance card.',
    'message': 'New message from your healthcare provider. Please check your Ella app when convenient.',
    'activity': 'Congratulations! You\'ve reached your daily step goal of 10,000 steps. Keep up the great work!',
    'checkin': 'Good morning! How are you feeling today? Remember to log your symptoms in the Ella app.',
    'welcome': 'Hello, this is Ella AI Care. I\'m your personal health companion, here to help you manage your wellness journey.',
  };
}
