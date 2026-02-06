import 'dart:async';
import 'dart:math';

import 'package:awesome_notifications/awesome_notifications.dart';
import 'package:just_audio/just_audio.dart';

import 'package:omi/main.dart';
import 'package:omi/utils/logger.dart';

/// Handler for Ella-specific FCM notifications with audio playback.
///
/// Supports FCM data payloads with:
///   type: "ella_notification"
///   audio_url: "https://storage.googleapis.com/..."
///   title: "Ella"
///   body: "Time for your morning check-in!"
///   navigate_to: "/chat"
class EllaNotificationHandler {
  static final _awesomeNotifications = AwesomeNotifications();
  static AudioPlayer? _audioPlayer;

  /// Handle an ella_notification FCM data message.
  ///
  /// When the app is in the foreground, plays audio immediately and shows a notification.
  /// When in the background, shows a notification; audio plays when the user taps it
  /// (handled by the notification tap handler in notifications.dart which reads the
  /// payload's audio_url and calls [playAudio]).
  static Future<void> handleEllaNotification(
    Map<String, dynamic> data,
    String channelKey, {
    bool isAppInForeground = false,
  }) async {
    final audioUrl = data['audio_url'] as String?;
    final title = data['title'] as String? ?? 'Ella';
    final body = data['body'] as String? ?? '';
    final navigateTo = data['navigate_to'] as String?;

    Logger.debug('[EllaNotification] Received: title=$title, audioUrl=${audioUrl != null ? "present" : "null"}, '
        'foreground=$isAppInForeground');

    // Show local notification
    await _showNotification(
      channelKey: channelKey,
      title: title,
      body: body,
      audioUrl: audioUrl,
      navigateTo: navigateTo,
    );

    // Auto-play audio when app is in foreground
    if (isAppInForeground && audioUrl != null && audioUrl.isNotEmpty) {
      await playAudio(audioUrl);
    }
  }

  /// Play audio from a URL using just_audio.
  ///
  /// Can be called from the foreground handler (auto-play) or from the
  /// notification tap handler (background tap-to-play).
  static Future<void> playAudio(String audioUrl) async {
    try {
      Logger.debug('[EllaNotification] Playing audio: $audioUrl');

      // Dispose previous player if any
      await _audioPlayer?.dispose();
      _audioPlayer = AudioPlayer();

      await _audioPlayer!.setUrl(audioUrl);
      await _audioPlayer!.play();

      // Clean up after playback completes
      _audioPlayer!.playerStateStream.listen((state) {
        if (state.processingState == ProcessingState.completed) {
          Logger.debug('[EllaNotification] Audio playback completed');
          _audioPlayer?.dispose();
          _audioPlayer = null;
        }
      });
    } catch (e) {
      Logger.debug('[EllaNotification] Error playing audio: $e');
      _audioPlayer?.dispose();
      _audioPlayer = null;
    }
  }

  /// Stop any currently playing Ella notification audio.
  static Future<void> stopAudio() async {
    await _audioPlayer?.stop();
    await _audioPlayer?.dispose();
    _audioPlayer = null;
  }

  /// Check if a notification payload is an Ella audio notification.
  /// Used by the notification tap handler to trigger audio playback.
  static bool isEllaAudioPayload(Map<String, dynamic> payload) {
    return payload['ella_audio_url'] != null && (payload['ella_audio_url'] as String).isNotEmpty;
  }

  static Future<void> _showNotification({
    required String channelKey,
    required String title,
    required String body,
    String? audioUrl,
    String? navigateTo,
  }) async {
    try {
      final notificationId = Random().nextInt(100000);

      final Map<String, String?> payload = {
        'navigate_to': navigateTo ?? '/chat',
      };

      // Include audio URL in payload so the tap handler can play it
      if (audioUrl != null && audioUrl.isNotEmpty) {
        payload['ella_audio_url'] = audioUrl;
      }

      await _awesomeNotifications.createNotification(
        content: NotificationContent(
          id: notificationId,
          channelKey: channelKey,
          title: title,
          body: body,
          payload: payload,
          notificationLayout: NotificationLayout.Default,
          wakeUpScreen: true,
          category: NotificationCategory.Message,
        ),
      );

      Logger.debug('[EllaNotification] Showed notification: $title');
    } catch (e) {
      Logger.debug('[EllaNotification] Error showing notification: $e');
    }
  }
}
