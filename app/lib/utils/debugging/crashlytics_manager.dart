import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'package:crypto/crypto.dart';
import 'package:firebase_crashlytics/firebase_crashlytics.dart';

import 'package:omi/utils/debugging/crash_reporter.dart';
import 'package:omi/utils/log_redaction.dart';
import 'package:omi/utils/platform/platform_service.dart';

class CrashlyticsManager implements CrashReporter {
  static final CrashlyticsManager _instance = CrashlyticsManager._internal();
  static CrashlyticsManager get instance => _instance;

  CrashlyticsManager._internal();

  factory CrashlyticsManager() {
    return _instance;
  }

  static Future<void> init() async {
    // Disable Crashlytics collection in debug mode
    if (kDebugMode) {
      await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(false);
    } else {
      await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(true);
    }
  }

  @override
  void identifyUser(String userId) {
    PlatformService.executeIfSupported(true, () async {
      await FirebaseCrashlytics.instance.setUserIdentifier(pseudonymousCrashUserId(userId));
    });
  }

  @override
  void logInfo(String message) {
    PlatformService.executeIfSupported(true, () => FirebaseCrashlytics.instance.log(redactSensitiveLogText(message)));
  }

  @override
  void logError(String message) {
    PlatformService.executeIfSupported(
      true,
      () => FirebaseCrashlytics.instance.log(redactSensitiveLogText('ERROR: $message')),
    );
  }

  @override
  void logWarn(String message) {
    PlatformService.executeIfSupported(
      true,
      () => FirebaseCrashlytics.instance.log(redactSensitiveLogText('WARN: $message')),
    );
  }

  @override
  void logDebug(String message) {
    PlatformService.executeIfSupported(
      true,
      () => FirebaseCrashlytics.instance.log(redactSensitiveLogText('DEBUG: $message')),
    );
  }

  @override
  void logVerbose(String message) {
    PlatformService.executeIfSupported(
      true,
      () => FirebaseCrashlytics.instance.log(redactSensitiveLogText('VERBOSE: $message')),
    );
  }

  @override
  void setUserAttribute(String key, String value) {
    if (_isPersonalCrashKey(key)) return;
    PlatformService.executeIfSupported(
      true,
      () => FirebaseCrashlytics.instance.setCustomKey(key, redactSensitiveLogText(value)),
    );
  }

  @override
  void setEnabled(bool isEnabled) {
    PlatformService.executeIfSupported(true, () async {
      await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(isEnabled);
    });
  }

  @override
  Future<void> reportCrash(Object exception, StackTrace stackTrace, {Map<String, String>? userAttributes}) async {
    await PlatformService.executeIfSupportedAsync(true, () async {
      if (userAttributes != null) {
        for (final entry in userAttributes.entries) {
          if (_isPersonalCrashKey(entry.key)) continue;
          final value = entry.key.toLowerCase().contains('url')
              ? redactUrlForLogs(entry.value)
              : redactSensitiveLogText(entry.value);
          await FirebaseCrashlytics.instance.setCustomKey(entry.key, value);
        }
      }
      await FirebaseCrashlytics.instance.recordError(
        Exception(redactedCrashExceptionMessage(exception)),
        stackTrace,
      );
    });
  }

  @override
  NavigatorObserver? getNavigatorObserver() {
    return null;
  }

  @override
  bool get isSupported => true;
}

@visibleForTesting
String pseudonymousCrashUserId(String userId) {
  if (userId.trim().isEmpty) return '';
  return sha256.convert(utf8.encode('ella-crash-v1:${userId.trim()}')).toString();
}

String redactedCrashExceptionMessage(Object exception) => redactSensitiveLogText(exception.toString());

bool _isPersonalCrashKey(String key) {
  final normalized = key.toLowerCase().replaceAll(RegExp('[^a-z0-9]+'), '_');
  return normalized.contains('email') ||
      normalized.contains('name') ||
      normalized == 'uid' ||
      normalized.contains('user_id') ||
      normalized.contains('token') ||
      normalized.contains('authorization');
}
