import 'package:flutter/material.dart';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:intercom_flutter/intercom_flutter.dart';
import 'package:talker_flutter/talker_flutter.dart';

import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/log_redaction.dart';

class CrashlyticsTalkerObserver extends TalkerObserver {
  CrashlyticsTalkerObserver();

  @override
  void onError(err) {
    FirebaseCrashlytics.instance.recordError(
      _redactedException(err.error),
      err.stackTrace,
      reason: _redactedMessage(err.message),
    );
  }

  @override
  void onException(err) {
    FirebaseCrashlytics.instance.recordError(
      _redactedException(err.exception),
      err.stackTrace,
      reason: _redactedMessage(err.message),
    );
  }
}

class Logger {
  final crashlyticsTalkerObserver = CrashlyticsTalkerObserver();
  late final talker = TalkerFlutter.init(observer: crashlyticsTalkerObserver);

  Logger._();

  static final Logger _instance = Logger._();

  static Logger get instance => _instance;

  static void log(dynamic message) {
    instance.talker.log(_redactedMessage(message));
  }

  static void error(dynamic message) {
    final safeMessage = _redactedMessage(message);
    instance.talker.error(safeMessage);
    DebugLogManager.logError(safeMessage);
  }

  static void warning(dynamic message) {
    final safeMessage = _redactedMessage(message);
    instance.talker.warning(safeMessage);
    DebugLogManager.logWarning(safeMessage);
  }

  static void info(dynamic message) {
    instance.talker.info(_redactedMessage(message));
  }

  static void debug(dynamic message) {
    instance.talker.debug(_redactedMessage(message));
  }

  static void handle(dynamic exception, StackTrace? stackTrace, {String? message}) {
    final safeException = _redactedException(exception);
    final safeMessage = _redactedMessage(message ?? 'An error occurred. Please try again later.');
    instance.talker.handle(safeException, stackTrace, safeMessage);
    DebugLogManager.logError(safeException, stackTrace, safeMessage);
  }
}

String _redactedMessage(dynamic value) => redactSensitiveLogText(value?.toString() ?? '');

Object _redactedException(dynamic value) => Exception(_redactedMessage(value));

class LoggerSnackbar extends StatelessWidget {
  final TalkerError? error;
  final TalkerException? exception;

  const LoggerSnackbar({super.key, this.error, this.exception}) : assert(error != null || exception != null);

  @override
  Widget build(BuildContext context) {
    final data = error ?? exception!;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.red,
        borderRadius: BorderRadius.circular(10),
      ),
      child: ListTile(
        contentPadding: const EdgeInsets.all(0),
        leading: const Icon(Icons.error_outline, color: Colors.white),
        title: Text(
          data.message ?? context.l10n.somethingWentWrongTryAgain,
          style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
        ),
        trailing: IconButton(
          icon: const Icon(Icons.share, color: Colors.white),
          onPressed: () async {
            // TODO: Have a custom form which can be prefilled with the error stack trace instead of opening the Gleap Homepage
            await Intercom.instance.displayMessenger();
          },
        ),
      ),
    );
  }
}
