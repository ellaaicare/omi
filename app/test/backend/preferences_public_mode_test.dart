import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('public mode defaults to false and persists changes', () {
    final preferences = SharedPreferencesUtil();

    expect(preferences.publicMode, isFalse);

    preferences.publicMode = true;
    expect(preferences.publicMode, isTrue);

    preferences.publicMode = false;
    expect(preferences.publicMode, isFalse);
  });

  test('accepting AI consent persists acceptance and a timestamp', () {
    final preferences = SharedPreferencesUtil();

    preferences.acceptAiConsent();

    expect(preferences.aiConsentAccepted, isTrue);
    expect(DateTime.tryParse(preferences.aiConsentAcceptedAt), isNotNull);

    preferences.declineAiConsent();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentAcceptedAt, isEmpty);
  });
}
