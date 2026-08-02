import 'dart:io';

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

  test('WAL diagnostics contain no stable owner namespace, identifier, or account path', () {
    final sources = [
      'lib/services/wals/local_wal_sync.dart',
      'lib/services/wals/sdcard_wal_sync.dart',
      'lib/services/wals/flash_page_wal_sync.dart',
      'lib/utils/wal_file_manager.dart',
      'lib/utils/audio_player_utils.dart',
      'lib/providers/sync_provider.dart',
    ];
    final loggerInvocation = RegExp(r'Logger\.(?:debug|info|warn|error)\(.*?\);', dotAll: true);
    final forbidden = [
      'storageNamespace',
      'wal.id',
      'wal.filePath',
      r'$filePath',
      '_accountsDirectory',
      'profileBindingId',
      'consentReceiptId',
      'owner.uid',
    ];

    for (final relativePath in sources) {
      final source = File('${_appRoot().path}/$relativePath').readAsStringSync();
      for (final match in loggerInvocation.allMatches(source)) {
        for (final value in forbidden) {
          expect(match.group(0), isNot(contains(value)), reason: '$relativePath diagnostic exposed $value');
        }
      }
    }
  });

  test('every direct Firebase identity mutation is behind unconditional quiescence', () {
    final lib = Directory('${_appRoot().path}/lib');
    final mutation =
        RegExp(r'FirebaseAuth\.instance\.(?:signOut|signInWithCredential|signInAnonymously|signInWithCustomToken)');
    final mutationFiles = lib
        .listSync(recursive: true)
        .whereType<File>()
        .where((file) => file.path.endsWith('.dart') && mutation.hasMatch(file.readAsStringSync()))
        .map((file) => file.path.substring(lib.path.length + 1))
        .toSet();
    expect(mutationFiles, {'services/auth_service.dart'});

    final auth = File('${lib.path}/services/auth_service.dart').readAsStringSync();
    expect(auth, contains('Future<T> runIdentityTransition<T>'));
    expect(auth.indexOf('stopForAccountTransition()'), lessThan(auth.indexOf('return mutation();')));
    expect(auth, contains('Future<void> signOut() => runIdentityTransition(FirebaseAuth.instance.signOut)'));
    expect(auth, contains('replaceIdentityWithCredential'));

    final provider = File('${lib.path}/providers/auth_provider.dart').readAsStringSync();
    expect(provider, contains('runIdentityTransition(_auth.signInAnonymously)'));

    final isolation = File('${lib.path}/ella/services/ella_account_isolation_service.dart').readAsStringSync();
    expect(isolation, contains('await _stopRegisteredCaptureProducers()'));
    expect(
      isolation.indexOf('stopCaptureForAccountTransition()'),
      lessThan(isolation.indexOf('stopCapture?.call()')),
    );
    expect(isolation.indexOf('quiesceForAccountTransition()'), lessThan(isolation.indexOf('stopCapture?.call()')));

    final services = File('${lib.path}/services/services.dart').readAsStringSync();
    final suspend = services.substring(
      services.indexOf('Future<void> suspendForAccountTransition()'),
      services.indexOf('Future<void> stopCaptureForAccountTransition()'),
    );
    for (final requiredStop in [
      'await _socket.stop()',
      'await _wal.stop()',
      'await _mic.stop()',
      'await _device.stop()',
      'await _systemAudio.stop()'
    ]) {
      expect(suspend, contains(requiredStop));
    }

    final capture = File('${lib.path}/providers/capture_provider.dart').readAsStringSync();
    expect(capture, contains('registerCaptureProducer(stopForAccountTransition)'));
    expect(capture, contains('ownerAtCapture: captureAuthority.owner'));
    expect(capture, isNot(contains('ownerAtCapture: null')));
  });
}

Directory _appRoot() {
  final current = Directory.current;
  if (File('${current.path}/pubspec.yaml').existsSync()) return current;
  final nested = Directory('${current.path}/app');
  if (File('${nested.path}/pubspec.yaml').existsSync()) return nested;
  throw StateError('Unable to locate Flutter app root from ${current.path}');
}
