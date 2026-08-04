import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/models/guardian_mode.dart';
import 'package:omi/ella/services/guardian_mode_api.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella-ai-care.com/';

  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isExactCurrent() => current;
}

void main() {
  setUpAll(() => Env.init(_TestEnv()));

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
  });

  test('mode read uses authenticated first-party route and exact account authority', () async {
    final authority = _Authority('uid-a');
    var calls = 0;

    final result = await getGuardianMode(
      guardianAllowed: true,
      exactAuthority: authority,
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        calls++;
        expect(url, 'https://api.ella-ai-care.com/v1/ella/guardian/mode');
        expect(method, 'GET');
        expect(body, isEmpty);
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority, same(authority));
        return http.Response(
          jsonEncode({
            'success': true,
            'currentMode': 'OFF',
            'override': null,
            'features': <String>[],
          }),
          200,
        );
      },
    );

    expect(calls, 1);
    expect(result.isSuccess, isTrue);
    expect(result.value?.twoTierState?.isOff, isTrue);
  });

  test('mode write carries no caller-selected uid and rejects stale authority before transport', () async {
    final authority = _Authority('uid-a');
    Map<String, dynamic>? requestBody;

    final enabled = await setGuardianModeTwoTier(
      const GuardianModeState(features: ['ACTIVE_SUPPORT']),
      guardianAllowed: true,
      exactAuthority: authority,
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        expect(method, 'PUT');
        expect(expectedAuthenticatedUid, 'uid-a');
        requestBody = jsonDecode(body) as Map<String, dynamic>;
        return http.Response('{"success":true,"currentMode":"ACTIVE_SUPPORT"}', 200);
      },
    );

    expect(enabled.isSuccess, isTrue);
    expect(requestBody, {
      'override': null,
      'features': ['ACTIVE_SUPPORT']
    });
    expect(requestBody, isNot(contains('uid')));

    authority.current = false;
    var staleTransportCalls = 0;
    final staleResult = await setGuardianModeTwoTier(
      const GuardianModeState(),
      guardianAllowed: true,
      exactAuthority: authority,
      transport: ({
        required url,
        required method,
        required body,
        required expectedAuthenticatedUid,
        required exactAuthority,
      }) async {
        staleTransportCalls++;
        return http.Response('{}', 200);
      },
    );

    expect(staleResult.isFailure, isTrue);
    expect(staleTransportCalls, 0);
  });
}
