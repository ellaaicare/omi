import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/caregiver_api.dart';
import 'package:omi/env/env.dart';

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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() => Env.init(_TestEnv()));

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
  });

  test('caregiver list uses first-party bearer route without caller UID', () async {
    final result = await getCaregivers(
      transport: (
          {required url,
          required method,
          required body,
          required expectedAuthenticatedUid,
          required exactAuthority}) async {
        expect(url, 'https://api.ella-ai-care.com/v1/ella/caregivers');
        expect(method, 'GET');
        expect(body, isEmpty);
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(exactAuthority.isExactCurrent(), isTrue);
        return http.Response(jsonEncode({'caregivers': []}), 200);
      },
    );

    expect(result.isSuccess, isTrue);
    expect(result.value, isEmpty);
  });

  test('caregiver failures are not represented as an empty list', () async {
    final result = await getCaregivers(
      transport: (
              {required url,
              required method,
              required body,
              required expectedAuthenticatedUid,
              required exactAuthority}) async =>
          http.Response('{"detail":"upgrade_required"}', 426),
    );

    expect(result.isFailure, isTrue);
    expect(result.value, isNull);
    expect(result.failure?.kind, ClientApiFailureKind.updateRequired);
  });

  test('no emergency contact is successful data, not a failed read', () async {
    final result = await getEmergencyContactId(
      transport: (
              {required url,
              required method,
              required body,
              required expectedAuthenticatedUid,
              required exactAuthority}) async =>
          http.Response('{"caregiver_id":null}', 200),
    );

    expect(result.isSuccess, isTrue);
    expect(result.value, isNull);
  });

  test('multi-step emergency contact creation cannot continue after account drift', () async {
    var calls = 0;
    await expectLater(
      createEmergencyContact(
        name: 'Family member',
        phone: '+15555550100',
        email: 'family@example.test',
        relationship: 'Family',
        transport: (
            {required url,
            required method,
            required body,
            required expectedAuthenticatedUid,
            required exactAuthority}) async {
          calls++;
          SharedPreferencesUtil().uid = 'uid-b';
          return http.Response('{"caregiver_id":"caregiver-a","invite_code":"code-a"}', 201);
        },
      ),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.accountChanged),
      ),
    );

    expect(calls, 1);
  });
}
