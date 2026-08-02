import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_legal_links.dart';
import 'package:omi/ella/services/ella_public_surface_policy.dart';
import 'package:omi/utils/debugging/crashlytics_manager.dart';
import 'package:omi/utils/log_redaction.dart';

void main() {
  test('Crashlytics identity is stable and pseudonymous', () {
    final first = pseudonymousCrashUserId('firebase-user-123');
    final second = pseudonymousCrashUserId('firebase-user-123');

    expect(first, second);
    expect(first, hasLength(64));
    expect(first, isNot(contains('firebase-user-123')));
    expect(pseudonymousCrashUserId(''), isEmpty);
  });

  test('URL, header, bearer, and JSON token values are redacted', () {
    final url = redactUrlForLogs(
      'wss://voice.example/session?token=secret-token&code=oauth-code&provider=grok#c=invite-secret',
    );
    expect(url, isNot(contains('secret-token')));
    expect(url, isNot(contains('oauth-code')));
    expect(url, isNot(contains('invite-secret')));
    expect(url, isNot(contains('#')));
    expect(url, contains('provider=grok'));

    final headers = redactHeadersForLogs({'Authorization': 'Bearer secret', 'X-Request-Id': 'safe-request-id'});
    expect(headers['Authorization'], redactedLogValue);
    expect(headers['X-Request-Id'], 'safe-request-id');

    final text = redactSensitiveLogText('Authorization: Bearer abc.def {\"access_token\":\"secret-json-token\"}');
    expect(text, isNot(contains('abc.def')));
    expect(text, isNot(contains('secret-json-token')));

    final crashMessage = redactedCrashExceptionMessage(
      Exception('socket failed: wss://voice.example/session?token=crash-token'),
    );
    expect(crashMessage, isNot(contains('crash-token')));
  });

  test('all Ella legal surfaces use the current canonical links', () {
    expect(EllaLegalLinks.privacy.toString(), 'https://ella-ai-care.com/privacy');
    expect(EllaLegalLinks.terms.toString(), 'https://ella-ai-care.com/terms');
    expect(EllaLegalLinks.privacy.path, isNot(startsWith('/legal/')));
    expect(EllaLegalLinks.terms.path, isNot(startsWith('/legal/')));
  });

  test('public builds reject inherited Omi surfaces', () {
    expect(allowsInheritedOmiSurface(isPublicBuild: true), isFalse);
    expect(allowsUnverifiedEllaSurface(isPublicBuild: true), isFalse);
  });

  test('non-public builds retain inherited Omi surfaces', () {
    expect(allowsInheritedOmiSurface(isPublicBuild: false), isTrue);
    expect(allowsUnverifiedEllaSurface(isPublicBuild: false), isTrue);
  });
}
