import 'dart:convert';
import 'fictional_transcript_run.dart';

/// Finite, content-free internal QA outcomes. Never render exception messages,
/// account values, credentials, filesystem paths or native stack traces.
enum FictionalLocalLoadCode {
  inputTooLarge,
  jsonInvalid,
  jsonObjectRequired,
  planInvalid,
  authorityUnavailable,
  featureDisabled,
  authorityRetired,
  ownerMismatch,
  planExpired,
  apiOriginInvalid,
  applicationSupportUnavailable,
  attemptDirectoryUnavailable,
  runCreationFailed,
  loadedLocally,
}

class FictionalLocalLoadException implements Exception {
  const FictionalLocalLoadException(this.code);
  final FictionalLocalLoadCode code;
  @override
  String toString() => 'Fictional local check: ${code.name}';
}

class FictionalInputFingerprint {
  FictionalInputFingerprint(String input)
      : bytes = utf8.encode(input).length,
        sha256 = fictionalHash(input);
  final int bytes;
  final String sha256;
}

class FictionalLocalLoadDiagnostic {
  const FictionalLocalLoadDiagnostic(this.code, [this.input]);
  final FictionalLocalLoadCode code;
  final FictionalInputFingerprint? input;
  String get safeText => 'Local check: ${code.name}'
      '${input == null ? '' : '\nInput UTF-8 bytes: ${input!.bytes}\nInput SHA256: ${input!.sha256}'}';
}
