import 'package:flutter_tts/flutter_tts.dart';
import 'dart:io';
import 'package:flutter/services.dart';

/// Ella TTS Service - Text-to-Speech with automatic Bluetooth routing
///
/// Features:
/// - Native iOS implementation for iOS 26+ (better voice support)
/// - Flutter TTS fallback for older iOS or other platforms
/// - Automatically routes audio to connected Bluetooth headsets (AirPods, etc.)
/// - Falls back to phone speaker if no Bluetooth audio device connected
/// - Supports voice selection and customization
/// - NO CONFIGURATION NEEDED - iOS handles routing automatically!
class EllaTtsService {
  static final EllaTtsService _instance = EllaTtsService._internal();
  factory EllaTtsService() => _instance;

  // Native iOS TTS (for iOS 26+)
  static const MethodChannel _nativeChannel = MethodChannel('ella.ai/native_tts');

  // Flutter TTS fallback (kept for compatibility)
  final FlutterTts _flutterTts = FlutterTts();

  bool _isInitialized = false;
  bool _useNative = false; // Will be set to true on iOS
  String? _selectedVoiceId; // Store selected voice ID for native TTS

  EllaTtsService._internal();

  /// Initialize TTS engine with premium voice quality
  Future<void> initialize() async {
    if (_isInitialized) return;

    try {
      if (Platform.isIOS) {
        // Try native iOS TTS first (works with iOS 26)
        try {
          await _nativeChannel.invokeMethod('initialize');
          _useNative = true;
          print('✅ Using Native iOS TTS (iOS 26 compatible)');
        } catch (e) {
          print('⚠️ Native TTS unavailable, falling back to Flutter TTS: $e');
          _useNative = false;
          await _initializeFlutterTts();
        }
      } else {
        // Android or other platforms use Flutter TTS
        await _initializeFlutterTts();
      }

      _isInitialized = true;
    } catch (e) {
      print('⚠️ EllaTtsService initialization error: $e');
      _isInitialized = true; // Continue even if initialization fails
    }
  }

  /// Initialize Flutter TTS (fallback method)
  Future<void> _initializeFlutterTts() async {
    await _flutterTts.setLanguage("en-US");
    await _flutterTts.setSharedInstance(true);

    // Try to set enhanced voice (may not work on all devices)
    try {
      await _flutterTts.setVoice({"name": "com.apple.voice.enhanced.en-US.Samantha", "locale": "en-US"});
      print('✅ Flutter TTS using enhanced voice');
    } catch (e) {
      print('⚠️ Enhanced voice not available, using default');
    }

    await _flutterTts.setSpeechRate(0.52);
    await _flutterTts.setVolume(1.0);
    await _flutterTts.setPitch(1.0);
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
      if (_useNative) {
        // Use native iOS TTS with selected voice
        print('🎤 Speaking with voice ID: $_selectedVoiceId');
        await _nativeChannel.invokeMethod('speak', {
          'text': text,
          'voiceId': _selectedVoiceId, // Pass the selected voice ID
          'rate': 1.0, // Normal speed (was 0.52 - too slow)
          'pitch': 1.0,
        });
      } else {
        // Use Flutter TTS fallback
        await _flutterTts.speak(text);
      }
    } catch (e) {
      print('EllaTtsService speak error: $e');
    }
  }

  /// Stop speaking immediately
  Future<void> stop() async {
    try {
      if (_useNative) {
        await _nativeChannel.invokeMethod('stop');
      } else {
        await _flutterTts.stop();
      }
    } catch (e) {
      print('EllaTtsService stop error: $e');
    }
  }

  /// Pause speaking (can be resumed)
  Future<void> pause() async {
    try {
      if (_useNative) {
        await _nativeChannel.invokeMethod('pause');
      } else {
        await _flutterTts.pause();
      }
    } catch (e) {
      print('EllaTtsService pause error: $e');
    }
  }

  /// Check if TTS is currently speaking
  Future<bool> isSpeaking() async {
    try {
      // Not implemented for native yet
      return false;
    } catch (e) {
      return false;
    }
  }

  /// Get available voices
  Future<List<Map<String, String>>> getVoices() async {
    await initialize();

    print('🔍 getVoices: _useNative = $_useNative');

    try {
      if (_useNative) {
        // Get voices from native iOS
        print('📱 Calling native iOS getVoices...');
        final List<dynamic>? voices = await _nativeChannel.invokeMethod('getVoices');
        print('📱 Native iOS returned ${voices?.length ?? 0} voices');

        if (voices == null || voices.isEmpty) {
          print('⚠️ No voices returned from native iOS');
          return [];
        }

        final mappedVoices = voices.map((voice) {
          return {
            'id': voice['id']?.toString() ?? '',
            'name': voice['name']?.toString() ?? '',
            'locale': voice['language']?.toString() ?? '',
            'quality': voice['quality']?.toString() ?? 'default',
          };
        }).toList();

        print('✅ Loaded ${mappedVoices.length} iOS voices');
        return mappedVoices;
      } else {
        // Get voices from Flutter TTS
        print('📱 Using Flutter TTS fallback...');
        final dynamic voicesRaw = await _flutterTts.getVoices;
        print('📱 Flutter TTS returned: $voicesRaw');

        if (voicesRaw == null) {
          print('⚠️ No voices available from Flutter TTS');
          return [];
        }

        final List<dynamic> voices = voicesRaw as List<dynamic>;
        print('📱 Cast to ${voices.length} voices');

        if (voices.isEmpty) {
          print('⚠️ No voices in list from Flutter TTS');
          return [];
        }

        final mappedVoices = voices.map((voice) {
          final voiceMap = voice as Map<Object?, Object?>;
          return {
            'id': voiceMap['name']?.toString() ?? '',
            'name': voiceMap['name']?.toString() ?? '',
            'locale': voiceMap['locale']?.toString() ?? '',
            'quality': 'default',
          };
        }).toList();

        print('✅ Loaded ${mappedVoices.length} Flutter TTS voices');
        return mappedVoices;
      }
    } catch (e) {
      print('❌ EllaTtsService getVoices error: $e');
      return [];
    }
  }

  /// Set voice by ID (native) or name (flutter_tts)
  Future<void> setVoice(String voiceId, String locale) async {
    try {
      if (_useNative) {
        // Store the selected voice ID for native iOS TTS
        _selectedVoiceId = voiceId;
        print('✅ Voice selected: $voiceId (will be used in next speak() call)');
      } else {
        await _flutterTts.setVoice({"name": voiceId, "locale": locale});
        print('✅ Voice changed to: $voiceId');
      }
    } catch (e) {
      print('EllaTtsService setVoice error: $e');
    }
  }

  /// Set speech rate (0.0 = very slow, 1.0 = very fast)
  Future<void> setSpeechRate(double rate) async {
    try {
      if (!_useNative) {
        await _flutterTts.setSpeechRate(rate.clamp(0.0, 1.0));
      }
    } catch (e) {
      print('EllaTtsService setSpeechRate error: $e');
    }
  }

  /// Set pitch (0.5 = low, 1.0 = normal, 2.0 = high)
  Future<void> setPitch(double pitch) async {
    try {
      if (!_useNative) {
        await _flutterTts.setPitch(pitch.clamp(0.5, 2.0));
      }
    } catch (e) {
      print('EllaTtsService setPitch error: $e');
    }
  }

  /// Set volume (0.0 = silent, 1.0 = full)
  Future<void> setVolume(double volume) async {
    try {
      if (!_useNative) {
        await _flutterTts.setVolume(volume.clamp(0.0, 1.0));
      }
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
