import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:omi/ella/services/effective_voice_settings_diagnostics.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid);
  @override
  final String uid;
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

Map<String, Object> _payload(String uid) => {
      'uid': uid,
      'private_extra': 'never expose response extras',
      'effective_voice_settings': {
        'voice_mode': 'grok-voice',
        'one_shot_tts_provider': 'xai-tts',
        'one_shot_tts_candidates': ['xai-tts', 'kokoro', 'elevenlabs'],
        'provider_type': 'v2v',
        'fallback_used': false,
        'fallback_reason': 'private free text must be excluded',
      },
    };

void main() {
  if (!isEffectiveVoiceDiagnosticsEnabled) {
    test('normal release diagnostics are inert before even reading authority', () async {
      var authorityReads = 0;
      var requests = 0;
      final api = EffectiveVoiceSettingsDiagnostics(
        authorityProvider: () {
          authorityReads++;
          return _Authority('private-owner');
        },
        transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
          requests++;
          return http.Response(jsonEncode(_payload('private-owner')), 200);
        },
      );
      expect((await api.read()).failure, EffectiveVoiceDiagnosticsFailure.disabled);
      expect(authorityReads, 0);
      expect(requests, 0);
    });
    return;
  }

  test('explicit read binds exact authority and exposes only hash and normalized allowlist', () async {
    final owner = _Authority('private owner exact UTF8');
    var requests = 0;
    final api = EffectiveVoiceSettingsDiagnostics(
      baseUrl: 'https://read-only.invalid/',
      authorityProvider: () => owner,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
        requests++;
        expect(url, 'https://read-only.invalid/v1/ella/settings/effective');
        expect(expectedAuthenticatedUid, owner.uid);
        expect(identical(exactAuthority, owner), isTrue);
        return http.Response(jsonEncode(_payload(owner.uid)), 200);
      },
    );
    expect(requests, 0);
    final result = await api.read();
    expect(requests, 1);
    expect(result.failure, isNull);
    expect(result.httpStatus, 200);
    final fields = result.snapshot!.fields;
    expect(fields.keys.toSet(), {
      'account_sha256',
      'http_status',
      'voice_mode',
      'one_shot_tts_provider',
      'one_shot_tts_candidates',
      'provider_type',
      'fallback_used'
    });
    expect(fields['account_sha256'], sha256.convert(utf8.encode(owner.uid)).toString());
    expect(jsonEncode(fields), isNot(contains(owner.uid)));
    expect(jsonEncode(fields), isNot(contains('private')));
    expect(() => fields['uid'] = owner.uid, throwsUnsupportedError);
    expect(() => (fields['one_shot_tts_candidates'] as List).add('secret'), throwsUnsupportedError);
    owner.current = false;
    expect(result.snapshot!.isCurrent, isFalse);
  });

  test('missing or retired authority sends nothing', () async {
    var requests = 0;
    final owner = _Authority('owner')..current = false;
    for (final authority in [null, owner]) {
      final result = await EffectiveVoiceSettingsDiagnostics(
        baseUrl: 'https://read-only.invalid/',
        authorityProvider: () => authority,
        transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async {
          requests++;
          return null;
        },
      ).read();
      expect(result.failure, EffectiveVoiceDiagnosticsFailure.authorityUnavailable);
    }
    expect(requests, 0);
  });

  test('late response after account or consent authority retirement is discarded', () async {
    final owner = _Authority('owner-a');
    final response = Completer<http.Response?>();
    final resultFuture = EffectiveVoiceSettingsDiagnostics(
      baseUrl: 'https://read-only.invalid/',
      authorityProvider: () => owner,
      transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) => response.future,
    ).read();
    owner.current = false;
    response.complete(http.Response(jsonEncode(_payload(owner.uid)), 200));
    final result = await resultFuture;
    expect(result.snapshot, isNull);
    expect(result.failure, EffectiveVoiceDiagnosticsFailure.authorityChanged);
  });

  for (final invalid in [
    'wrong_subject',
    'unknown_mode',
    'unknown_candidate',
    'bad_boolean',
    'malformed',
    'oversized'
  ]) {
    test('$invalid response never exposes raw subject or free text', () async {
      final owner = _Authority('private-owner-a');
      final data = _payload(invalid == 'wrong_subject' ? 'private-owner-b' : owner.uid);
      final voice = data['effective_voice_settings'] as Map<String, Object>;
      if (invalid == 'unknown_mode') voice['voice_mode'] = 'Bearer private-token';
      if (invalid == 'unknown_candidate') voice['one_shot_tts_candidates'] = ['https://private.invalid'];
      if (invalid == 'bad_boolean') voice['fallback_used'] = 'private free text';
      final body = invalid == 'malformed'
          ? '{secret'
          : invalid == 'oversized'
              ? 'x' * 65537
              : jsonEncode(data);
      final result = await EffectiveVoiceSettingsDiagnostics(
        baseUrl: 'https://read-only.invalid/',
        authorityProvider: () => owner,
        transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async =>
            http.Response(body, 200),
      ).read();
      expect(result.snapshot, isNull);
      expect(result.failure, EffectiveVoiceDiagnosticsFailure.invalidResponse);
    });
  }

  for (final status in [401, 403, 500]) {
    test('HTTP$status has no parsed payload or synthesized settings', () async {
      final result = await EffectiveVoiceSettingsDiagnostics(
        baseUrl: 'https://read-only.invalid/',
        authorityProvider: () => _Authority('private-owner'),
        transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) async =>
            http.Response('Bearer private-token private-owner', status),
      ).read();
      expect(result.snapshot, isNull);
      expect(result.failure, EffectiveVoiceDiagnosticsFailure.http);
      expect(result.httpStatus, status);
    });
  }
}
