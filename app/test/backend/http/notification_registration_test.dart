import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/notifications.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _MutableAuthority implements AccountCommitAuthority {
  _MutableAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isCurrent() => current;

  @override
  bool isExactCurrent() => current;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init();

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
  });

  test('failed token registration persists retry-required and never reports ready', () async {
    final result = await saveFcmTokenServer(
      token: 'opaque-test-token',
      timeZone: 'UTC',
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(body, contains('opaque-test-token'));
        return http.Response('{"detail":"provider_unavailable"}', 503);
      },
    );

    expect(result.isReady, isFalse);
    expect(result.status.state, FcmRegistrationState.retryRequired);
    await SharedPreferencesUtil.init();
    expect(FcmRegistrationStatus.load('uid-a').state, FcmRegistrationState.retryRequired);
    expect(FcmRegistrationStatus.load('uid-a').isReady, isFalse);
  });

  test('only a successful owner-bound response becomes notification-ready', () async {
    final result = await saveFcmTokenServer(
      token: 'opaque-test-token',
      timeZone: 'UTC',
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async {
        expect(exactAuthority.isExactCurrent(), isTrue);
        return http.Response('{}', 200);
      },
    );

    expect(result.failure, isNull);
    expect(result.isReady, isTrue);
    expect(FcmRegistrationStatus.load('uid-a').isReady, isTrue);
  });

  test('authority drift during registration cannot report notification-ready', () async {
    final authority = _MutableAuthority('uid-a');
    final result = await saveFcmTokenServer(
      token: 'opaque-test-token',
      timeZone: 'UTC',
      authorityProvider: () => authority,
      transport: ({required url, required body, required expectedAuthenticatedUid, required exactAuthority}) async {
        authority.current = false;
        return http.Response('{}', 200);
      },
    );

    expect(result.isReady, isFalse);
    expect(result.failure?.kind, ClientApiFailureKind.accountChanged);
    expect(FcmRegistrationStatus.load('uid-a').state, FcmRegistrationState.retryRequired);
  });
}
