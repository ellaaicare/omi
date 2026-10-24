import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/logger.dart';

enum WhisperStartupStage {
  authority,
  authenticationToken,
  authenticationReceipt,
  presentationPreference,
  stateReconciliation,
  nativeStop,
  stateRead,
  stateValidation,
  preferencesPersist,
  deviceApi,
  deviceValidation,
  sessionApi,
  sessionValidation,
  pendingStop,
  nativeActivate,
  ready,
}

enum WhisperStartupCode {
  none,
  missingAuthority,
  authorityChanged,
  userChanged,
  authTimeMissing,
  rejected,
  timeout,
  invalidState,
  invalidResponse,
  authError,
  tokenExpired,
  networkUnavailable,
  userDisabled,
  nativeInvalid,
  nativeOwner,
  nativeMedia,
  nativeChannel,
  nativeError,
  unknown,
  httpFailure,
  noResponse,
  protocolDisabled,
  apiUnconfigured,
}

enum WhisperStartupResult { begin, ok, failed }

/// Only enum values and a bounded HTTP status can enter this diagnostic stream.
/// Never stringify exceptions, native details, request/response fields or identity.
class WhisperStartupDiagnostics {
  @visibleForTesting
  static void Function(String)? testSink;

  static WhisperStartupCode codeFor(Object error) {
    if (error is TimeoutException) return WhisperStartupCode.timeout;
    if (error is PlatformException) {
      return switch (error.code) {
        'STRICT_INVALID' => WhisperStartupCode.nativeInvalid,
        'STRICT_OWNER' => WhisperStartupCode.nativeOwner,
        'STRICT_MEDIA' => WhisperStartupCode.nativeMedia,
        'channel-error' => WhisperStartupCode.nativeChannel,
        _ => WhisperStartupCode.nativeError,
      };
    }
    if (error is FirebaseAuthException) {
      return switch (error.code) {
        'user-token-expired' || 'invalid-user-token' => WhisperStartupCode.tokenExpired,
        'network-request-failed' => WhisperStartupCode.networkUnavailable,
        'user-disabled' => WhisperStartupCode.userDisabled,
        _ => WhisperStartupCode.authError,
      };
    }
    if (error is FormatException) return WhisperStartupCode.invalidState;
    if (error is TypeError) return WhisperStartupCode.invalidResponse;
    if (error is StateError) return WhisperStartupCode.rejected;
    return WhisperStartupCode.unknown;
  }

  static void record(WhisperStartupStage stage, WhisperStartupResult result,
      {WhisperStartupCode code = WhisperStartupCode.none, int? httpStatus}) {
    final status = httpStatus != null && httpStatus >= 100 && httpStatus <= 599 ? ' http=$httpStatus' : '';
    final line = '[WhisperStartup] stage=${stage.name} result=${result.name} code=${code.name}$status';
    try {
      if (testSink != null) {
        testSink!(line);
      } else {
        Logger.info(line);
        unawaited(DebugLogManager.logInfo(line).catchError((_) {}));
      }
    } catch (_) {
      // Diagnostics never grant authority, mask a failure or prevent a Stop.
    }
  }

  static void failure(WhisperStartupStage stage, Object error) =>
      record(stage, WhisperStartupResult.failed, code: codeFor(error));
}
