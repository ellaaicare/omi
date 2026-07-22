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
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);

    preferences.declineAiConsent();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentAcceptedAt, isEmpty);
    expect(preferences.aiConsentContractVersion, isEmpty);
  });

  test('legacy and stale processor consent receipts fail closed', () async {
    SharedPreferences.setMockInitialValues({
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentReceiptId': 'legacy-receipt',
      'aiConsentReceiptUid': 'uid-a',
    });
    await SharedPreferencesUtil.init();

    final preferences = SharedPreferencesUtil();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isFalse);

    await preferences.saveString('aiConsentContractVersion', 'voice-ai-processors-v1');
    expect(preferences.aiConsentAccepted, isFalse);

    preferences.acceptAiConsent(receiptId: 'current-receipt', uid: 'uid-a');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isTrue);
  });
}
