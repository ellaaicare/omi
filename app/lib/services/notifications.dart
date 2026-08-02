import 'dart:isolate';
import 'dart:ui';

import 'package:flutter/material.dart';

import 'package:awesome_notifications/awesome_notifications.dart';

import 'package:omi/main.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/services/notifications/daily_reflection_notification.dart';
import 'package:omi/services/notifications/ella_notification_handler.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/logger.dart';

// Re-export the main notification service for backward compatibility
// All notification functionality is now handled by the platform-aware service

export 'package:omi/services/notifications/notification_service.dart';

typedef NotificationNavigationDispatcher = void Function(String navigateTo, String? autoMessage);

class NotificationUtil {
  static ReceivePort? receivePort;

  @visibleForTesting
  static NotificationNavigationDispatcher? debugNavigationDispatcher;

  static Future<void> initializeNotificationsEventListeners() async {
    // Only after at least the action method is set, the notification events are delivered
    AwesomeNotifications().setListeners(onActionReceivedMethod: NotificationUtil.onActionReceivedMethod);
  }

  static Future<void> initializeIsolateReceivePort() async {
    receivePort = ReceivePort('Notification action port in main isolate');
    receivePort!.listen((serializedData) {
      final receivedAction = ReceivedAction().fromMap(serializedData);
      onActionReceivedMethodImpl(receivedAction);
    });

    // This initialization only happens on main isolate
    IsolateNameServer.registerPortWithName(receivePort!.sendPort, 'notification_action_port');
  }

  /// Use this method to detect when the user taps on a notification or action button
  @pragma("vm:entry-point")
  static Future<void> onActionReceivedMethod(ReceivedAction receivedAction) async {
    if (receivePort != null) {
      await onActionReceivedMethodImpl(receivedAction);
    } else {
      Logger.debug('Notification action received before the main isolate port was initialized');
      SendPort? sendPort = IsolateNameServer.lookupPortByName('notification_action_port');

      if (sendPort != null) {
        Logger.debug('Redirecting notification action to the main isolate');
        dynamic serializedData = receivedAction.toMap();
        sendPort.send(serializedData);
      }
    }
  }

  static Future<void> onActionReceivedMethodImpl(ReceivedAction receivedAction) async {
    if (receivedAction.payload == null || receivedAction.payload!.isEmpty) {
      return;
    }
    await _handleAppLinkOrDeepLink(receivedAction.payload!);
  }

  static Future<void> _handleAppLinkOrDeepLink(Map<String, dynamic> payload) async {
    // Always ensure that all plugins was initialized
    // TODO: for what?
    WidgetsFlutterBinding.ensureInitialized();

    if (EllaNotificationHandler.isGuardianPayload(payload) && !allowsGuardianSurface()) {
      Logger.debug('Guardian notification denied by build policy');
      return;
    }

    String? navigateTo;
    if (payload.containsKey('navigate_to')) {
      navigateTo = payload['navigate_to'];
    }
    final allowedNavigateTo = allowedEllaNavigationRoute(navigateTo);
    if (allowedNavigateTo == null) {
      Logger.debug('Notification navigation route denied');
      return;
    }

    // Play audio if this is an Ella notification with audio
    if (EllaNotificationHandler.isEllaAudioPayload(payload)) {
      EllaNotificationHandler.playAudio(payload['ella_audio_url'] as String);
    }

    // Check if this is a daily reflection notification
    String? autoMessage;
    if (DailyReflectionNotification.isReflectionPayload(payload)) {
      autoMessage = DailyReflectionNotification.reflectionMessage;
    }

    final dispatcher = debugNavigationDispatcher;
    if (dispatcher != null) {
      dispatcher(allowedNavigateTo, autoMessage);
      return;
    }

    MyApp.navigatorKey.currentState?.pushReplacement(
      MaterialPageRoute(
        builder: (context) => HomePageWrapper(navigateToRoute: allowedNavigateTo, autoMessage: autoMessage),
      ),
    );
  }

  static Future<void> triggerFallNotification() async {
    if (!allowsGuardianSurface()) return;
    final allowed = await AwesomeNotifications().isNotificationAllowed();
    if (!allowed) return;

    final ctx = MyApp.navigatorKey.currentContext;
    await AwesomeNotifications().createNotification(
      content: NotificationContent(
        id: 6,
        channelKey: 'channel',
        actionType: ActionType.Default,
        title: ctx?.l10n.fallNotificationTitle ?? 'Ouch',
        body: ctx?.l10n.fallNotificationBody ?? 'Did you fall?',
        wakeUpScreen: true,
      ),
    );
  }
}
