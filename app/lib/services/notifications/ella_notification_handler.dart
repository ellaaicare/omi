import 'dart:async';
import 'dart:math';

import 'package:awesome_notifications/awesome_notifications.dart';
import 'package:flutter/services.dart';
import 'package:just_audio/just_audio.dart';

import 'package:omi/ella/services/ella_public_surface_policy.dart';
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
  static const guardianNotificationGroupKey = 'ella.guardian.notifications';
  static const _guardianModeChannel = MethodChannel('com.ellaaicare.ella/guardian_mode');
  static final _awesomeNotifications = AwesomeNotifications();
  static AudioPlayer? _audioPlayer;
  static int _audioGeneration = 0;

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
    final guardianPayload = isGuardianPayload(data);
    if (guardianPayload && !allowsGuardianCareSurface()) return;
    final audioUrl = data['audio_url'] as String?;
    final title = data['title'] as String? ?? 'Ella';
    final body = data['body'] as String? ?? '';
    final navigateTo = data['navigate_to'] as String?;

    Logger.debug(
      '[EllaNotification] Received: title=$title, audioUrl=${audioUrl != null ? "present" : "null"}, '
      'foreground=$isAppInForeground',
    );

    // Show local notification
    await _showNotification(
      channelKey: channelKey,
      title: title,
      body: body,
      audioUrl: audioUrl,
      navigateTo: navigateTo,
      guardianPayload: guardianPayload,
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
    final generation = ++_audioGeneration;
    final previousPlayer = _audioPlayer;
    final player = AudioPlayer();
    _audioPlayer = player;
    try {
      Logger.debug('[EllaNotification] Playing audio: $audioUrl');

      await previousPlayer?.dispose();
      await player.setUrl(audioUrl);
      if (generation != _audioGeneration || !identical(_audioPlayer, player)) {
        await player.dispose();
        return;
      }

      await player.play();

      // Clean up after playback completes
      player.playerStateStream.listen((state) {
        if (state.processingState == ProcessingState.completed && identical(_audioPlayer, player)) {
          Logger.debug('[EllaNotification] Audio playback completed');
          player.dispose();
          _audioPlayer = null;
        }
      });
    } catch (e) {
      Logger.debug('[EllaNotification] Error playing audio: $e');
      await player.dispose();
      if (identical(_audioPlayer, player)) _audioPlayer = null;
    }
  }

  /// Stop any currently playing Ella notification audio.
  static Future<void> stopAudio() async {
    _audioGeneration += 1;
    final player = _audioPlayer;
    _audioPlayer = null;
    await player?.stop();
    await player?.dispose();
  }

  static Future<void> clearGuardianNotificationResidue({
    Future<void> Function(String groupKey)? cancelDelivered,
    Future<void> Function(String groupKey)? cancelPending,
    Future<void> Function()? clearNative,
  }) async {
    try {
      await (cancelDelivered ?? _awesomeNotifications.cancelNotificationsByGroupKey)(guardianNotificationGroupKey);
      await (cancelPending ?? _awesomeNotifications.cancelSchedulesByGroupKey)(guardianNotificationGroupKey);
    } catch (e) {
      Logger.debug('[EllaNotification] Unable to clear grouped notification residue: $e');
    }

    try {
      if (clearNative != null) {
        await clearNative();
      } else {
        await _guardianModeChannel.invokeMethod<void>('clearNotificationResidue');
      }
    } catch (e) {
      Logger.debug('[EllaNotification] Unable to clear native notification residue: $e');
    }
  }

  static Future<void> stopAndClearGuardianResidue() async {
    await stopAudio();
    await clearGuardianNotificationResidue();
  }

  /// Check if a notification payload is an Ella audio notification.
  /// Used by the notification tap handler to trigger audio playback.
  static bool isEllaAudioPayload(Map<String, dynamic> payload) {
    return payload['ella_audio_url'] != null && (payload['ella_audio_url'] as String).isNotEmpty;
  }

  static bool isGuardianPayload(Map<String, dynamic> payload) {
    if (payload['ella_guardian_audio'] == 'true' || payload['ella_guardian_audio'] == true) return true;
    final type = payload['type']?.toString().trim().toLowerCase() ?? '';
    if (const {
      'ella_notification',
      'ella_emergency_confirmation',
      'guardian_notification',
      'guardian_alert',
      'emergency',
    }.contains(type)) {
      return true;
    }
    if (payload['urgency']?.toString().trim().toLowerCase() == 'emergency') return true;
    final values = [
      payload['type'],
      payload['subtype'],
      payload['notification_type'],
      payload['trigger_type'],
      payload['guardian_mode'],
      payload['category'],
      payload['source'],
      payload['navigate_to'],
    ].whereType<Object>().join(' ').toLowerCase();
    return values.contains('guardian') ||
        values.contains('whisper') ||
        values.contains('wake_word') ||
        values.contains('caregiver') ||
        values.contains('emergency');
  }

  static Future<void> _showNotification({
    required String channelKey,
    required String title,
    required String body,
    String? audioUrl,
    String? navigateTo,
    bool guardianPayload = false,
  }) async {
    try {
      final notificationId = Random().nextInt(100000);

      final Map<String, String?> payload = {'navigate_to': navigateTo ?? '/chat'};

      // Include audio URL in payload so the tap handler can play it
      if (audioUrl != null && audioUrl.isNotEmpty) {
        payload['ella_audio_url'] = audioUrl;
      }
      if (guardianPayload) payload['ella_guardian_audio'] = 'true';

      await _awesomeNotifications.createNotification(
        content: NotificationContent(
          id: notificationId,
          channelKey: channelKey,
          groupKey: guardianPayload ? guardianNotificationGroupKey : null,
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
