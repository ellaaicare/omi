/// Audio Push Plugin
///
/// Handles audio playback from push notifications.
/// When a push notification contains audio, this plugin plays it.
///
/// This is a SKELETON - port implementation from current fork's AppDelegate.
///
/// Example push payload:
/// ```json
/// {
///   "aps": { "alert": "Ella", "sound": "default" },
///   "data": {
///     "type": "audio_message",
///     "audio_url": "https://storage.googleapis.com/.../message.mp3",
///     "text": "Time to take your medication"
///   }
/// }
/// ```
import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';

import '../base_plugin.dart';
import '../../config/ella_config.dart';

/// Audio Push Plugin
///
/// Receives audio URLs from push notifications and plays them.
class AudioPushPlugin extends EllaPlugin {
  @override
  String get name => 'AudioPush';

  @override
  String get version => '1.0.0';

  // Method channel for native push handling
  static const _channel = MethodChannel('com.ella.audio_push');

  // Audio player
  final AudioPlayer _audioPlayer = AudioPlayer();

  // State
  bool _isPlaying = false;
  bool get isPlaying => _isPlaying;

  // Callbacks
  VoidCallback? onAudioStarted;
  VoidCallback? onAudioEnded;
  Function(String)? onError;

  @override
  Future<void> initialize() async {
    // Setup method channel handler for push notifications
    _channel.setMethodCallHandler(_handleNativeCall);
    debugPrint('[AudioPush] Initialized');
  }

  @override
  Future<void> dispose() async {
    await _audioPlayer.dispose();
  }

  /// Handle calls from native iOS (push notification received)
  Future<dynamic> _handleNativeCall(MethodCall call) async {
    switch (call.method) {
      case 'playAudioFromPush':
        final audioUrl = call.arguments['audio_url'] as String?;
        final text = call.arguments['text'] as String?;
        if (audioUrl != null) {
          await playAudio(audioUrl, text: text);
        }
        return null;

      case 'stopAudio':
        await stop();
        return null;

      default:
        return null;
    }
  }

  /// Play audio from URL
  ///
  /// Called when push notification contains audio.
  Future<void> playAudio(String audioUrl, {String? text}) async {
    if (!EllaConfig().audioPushEnabled) {
      debugPrint('[AudioPush] Audio push disabled, skipping');
      return;
    }

    debugPrint('[AudioPush] Playing audio: $audioUrl');
    if (text != null) {
      debugPrint('[AudioPush] Text: $text');
    }

    _isPlaying = true;
    onAudioStarted?.call();

    try {
      await _audioPlayer.setUrl(audioUrl);
      await _audioPlayer.play();

      // Wait for completion
      await _audioPlayer.playerStateStream.firstWhere(
        (state) => state.processingState == ProcessingState.completed,
      );

      debugPrint('[AudioPush] Playback completed');
    } catch (e) {
      debugPrint('[AudioPush] Playback error: $e');
      onError?.call('Audio playback failed: $e');
    } finally {
      _isPlaying = false;
      onAudioEnded?.call();
    }
  }

  /// Stop current audio playback
  Future<void> stop() async {
    try {
      await _audioPlayer.stop();
    } catch (e) {
      debugPrint('[AudioPush] Stop error: $e');
    }
    _isPlaying = false;
  }

  /// Process push notification data
  ///
  /// Call this from your push notification handler.
  Future<bool> handlePushNotification(Map<String, dynamic> data) async {
    final type = data['type'] as String?;

    if (type == 'audio_message' || type == 'speak_tts') {
      final audioUrl = data['audio_url'] as String?;
      final text = data['text'] as String?;

      if (audioUrl != null) {
        await playAudio(audioUrl, text: text);
        return true;
      }
    }

    return false;
  }

  @override
  Map<String, dynamic> getStatus() {
    return {
      ...super.getStatus(),
      'enabled': EllaConfig().audioPushEnabled,
      'isPlaying': _isPlaying,
    };
  }

  // ============================================
  // TODO: PORT FROM CURRENT FORK
  // ============================================
  //
  // Port the following from current AppDelegate.swift:
  //
  // 1. Background audio session setup
  //    - AVAudioSession configuration
  //    - Background modes capability
  //
  // 2. Push notification audio handling
  //    - Notification extension for background playback
  //    - Audio interruption handling
  //
  // 3. Bluetooth audio routing
  //    - Play through connected headset
  //    - Handle audio route changes
  //
  // See: ios/Runner/AppDelegate.swift (audio sections)
  // ============================================
}
