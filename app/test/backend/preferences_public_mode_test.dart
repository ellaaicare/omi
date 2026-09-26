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

  test('existing v8 account requires v9 before any managed-cloud AI or illustration action', () async {
    SharedPreferences.setMockInitialValues({
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentContractVersion': 'ai-data-processors-v8',
      'aiConsentProcessorSetHash': 'sha256:dd84e4a9da1166cff66e5de55c2570d0496a2c89d46ca431530e993758616296',
      'aiConsentReceiptId': 'aicr_v8-receipt-a',
      'aiConsentReceiptUid': 'uid-a',
      'aiConsentProfileBindingId': 'profile-binding-a',
      'aiConsentScopeVersion': 'managed-cloud-internal-pilot-v1',
      'aiConsentScopeHash': 'sha256:727b1db818ce79090a02279f1cc6d15dfc3d65a58592b13fbed53ad048c38a30',
      'aiConsentServerDecidedAt': '2026-07-27T00:00:00Z',
    });
    await SharedPreferencesUtil.init();

    final preferences = SharedPreferencesUtil();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.hasPriorAccountBoundAiConsent('uid-a'), isTrue);
    expect(preferences.hasPriorAccountBoundAiConsent('uid-b'), isFalse);
  });

  test('receipt-less acceptance clears stale authority and remains fail closed', () async {
    SharedPreferences.setMockInitialValues({
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentContractVersion': 'voice-ai-processors-v1',
      'aiConsentReceiptId': 'ios-private-cloud-sync:voice-ai-processors-v1:stale-receipt',
      'aiConsentReceiptUid': 'uid-a',
    });
    await SharedPreferencesUtil.init();

    final preferences = SharedPreferencesUtil();
    preferences.acceptAiConsent();

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
    expect(preferences.aiConsentReceiptUid, isEmpty);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isFalse);
  });

  test('account switch invalidates otherwise current processor consent', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');
    expect(preferences.aiConsentAccepted, isTrue);

    preferences.uid = 'uid-b';

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.persistedAiConsentReceiptIdForCurrentAccount, isEmpty);
    expect(preferences.hasAccountBoundAiConsent('uid-b'), isFalse);
  });

  test('internal pilot authority is English-only without changing normal locale authority', () async {
    SharedPreferences.setMockInitialValues({'app_locale': 'es'});
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');

    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.hasCurrentAiConsentAuthority(enforceEnglishPilotLocale: true), isFalse);

    await preferences.saveString('app_locale', 'en');
    expect(preferences.hasCurrentAiConsentAuthority(enforceEnglishPilotLocale: true), isTrue);
  });
}

void _markServerVerified(
  SharedPreferencesUtil preferences, {
  required String uid,
  required String receiptId,
  DateTime? verifiedAt,
}) {
  preferences.markAiConsentServerVerified(
    uid: uid,
    receiptId: receiptId,
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-binding-a',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    verifiedAt: verifiedAt,
  );
}
