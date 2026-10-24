import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

// Opt-in for an owner-approved local candidate. Normal Release builds are inert.
const bool isEffectiveVoiceDiagnosticsEnabled =
    bool.fromEnvironment('ELLA_EFFECTIVE_VOICE_DIAGNOSTICS', defaultValue: false);

typedef EffectiveVoiceDiagnosticsTransport = Future<http.Response?> Function({
  required String url,
  required String expectedAuthenticatedUid,
  required ExactAccountAuthorityVerifier exactAuthority,
});

enum EffectiveVoiceDiagnosticsFailure {
  disabled,
  authorityUnavailable,
  authorityChanged,
  transport,
  http,
  invalidResponse
}

class EffectiveVoiceDiagnosticsSnapshot {
  EffectiveVoiceDiagnosticsSnapshot._(this._authority, this.fields);

  final ExactAccountAuthorityVerifier _authority;
  final Map<String, Object> fields;
  bool get isCurrent => _authority.isExactCurrent();
}

class EffectiveVoiceDiagnosticsResult {
  const EffectiveVoiceDiagnosticsResult({this.snapshot, this.failure, this.httpStatus});

  final EffectiveVoiceDiagnosticsSnapshot? snapshot;
  final EffectiveVoiceDiagnosticsFailure? failure;
  final int? httpStatus;
}

class EffectiveVoiceSettingsDiagnostics {
  EffectiveVoiceSettingsDiagnostics({
    ExactAccountAuthorityVerifier? Function()? authorityProvider,
    EffectiveVoiceDiagnosticsTransport? transport,
    String? baseUrl,
  })  : _authorityProvider = authorityProvider ?? WalOwnerAuthority.active,
        _transport = transport ?? _defaultTransport,
        _baseUrl = baseUrl;

  final ExactAccountAuthorityVerifier? Function() _authorityProvider;
  final EffectiveVoiceDiagnosticsTransport _transport;
  final String? _baseUrl;

  static const _ttsProviders = {
    'elevenlabs',
    'fish-audio',
    'fish-audio-s1',
    'fish-audio-s2',
    'kokoro',
    'inworld',
    'xai-tts'
  };
  static const _voiceModes = {
    ..._ttsProviders,
    'openclaw-direct',
    'grok-voice',
    'gemini-native-live',
    'openai-native-realtime',
  };

  static Future<http.Response?> _defaultTransport({
    required String url,
    required String expectedAuthenticatedUid,
    required ExactAccountAuthorityVerifier exactAuthority,
  }) =>
      makeApiCall(
        url: url,
        method: 'GET',
        headers: const {'Content-Type': 'application/json'},
        body: '',
        timeout: const Duration(seconds: 10),
        enforceAbsoluteTimeout: true,
        retries: 0,
        retryOnUnauthorized: false,
        requireAuthCheck: true,
        expectedAuthenticatedUid: expectedAuthenticatedUid,
        exactAuthority: exactAuthority,
      );

  Future<EffectiveVoiceDiagnosticsResult> read() async {
    if (!isEffectiveVoiceDiagnosticsEnabled) {
      return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.disabled);
    }
    final authority = _authorityProvider();
    final base = _baseUrl ?? Env.apiBaseUrl;
    if (authority == null || authority.uid.isEmpty || !authority.isExactCurrent() || base == null || base.isEmpty) {
      return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.authorityUnavailable);
    }
    try {
      final response = await _transport(
        url: '${base}v1/ella/settings/effective',
        expectedAuthenticatedUid: authority.uid,
        exactAuthority: authority,
      );
      if (!authority.isExactCurrent()) {
        return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.authorityChanged);
      }
      final status = response?.statusCode;
      final safeStatus = status != null && status >= 100 && status <= 599 ? status : null;
      if (response == null) {
        return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.transport);
      }
      if (status != 200) {
        return EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.http, httpStatus: safeStatus);
      }
      if (response.bodyBytes.length > 65536) throw const FormatException();
      final data = jsonDecode(response.body);
      if (data is! Map<String, dynamic> || data['uid'] != authority.uid) throw const FormatException();
      final voice = data['effective_voice_settings'];
      if (voice is! Map<String, dynamic> ||
          !_voiceModes.contains(voice['voice_mode']) ||
          !_ttsProviders.contains(voice['one_shot_tts_provider']) ||
          !{'v2v', 'tts'}.contains(voice['provider_type']) ||
          voice['fallback_used'] is! bool) {
        throw const FormatException();
      }
      final candidates = voice['one_shot_tts_candidates'];
      if (candidates is! List ||
          candidates.isEmpty ||
          candidates.length > _ttsProviders.length ||
          candidates.any((item) => !_ttsProviders.contains(item))) {
        throw const FormatException();
      }
      final fields = Map<String, Object>.unmodifiable({
        'account_sha256': sha256.convert(utf8.encode(authority.uid)).toString(),
        'http_status': 200,
        'voice_mode': voice['voice_mode'] as String,
        'one_shot_tts_provider': voice['one_shot_tts_provider'] as String,
        'one_shot_tts_candidates': List<String>.unmodifiable(candidates.cast<String>()),
        'provider_type': voice['provider_type'] as String,
        'fallback_used': voice['fallback_used'] as bool,
      });
      if (!authority.isExactCurrent()) {
        return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.authorityChanged);
      }
      return EffectiveVoiceDiagnosticsResult(
          snapshot: EffectiveVoiceDiagnosticsSnapshot._(authority, fields), httpStatus: 200);
    } on FormatException {
      return const EffectiveVoiceDiagnosticsResult(failure: EffectiveVoiceDiagnosticsFailure.invalidResponse);
    } catch (_) {
      return EffectiveVoiceDiagnosticsResult(
        failure: authority.isExactCurrent()
            ? EffectiveVoiceDiagnosticsFailure.transport
            : EffectiveVoiceDiagnosticsFailure.authorityChanged,
      );
    }
  }
}
