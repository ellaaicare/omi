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

  test('persisted AI consent is not authority until the server grant is verified', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';

    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      clientVersion: '1.0.528+804',
      locale: 'en-US',
    );

    expect(preferences.aiConsentAccepted, isFalse);
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');

    expect(preferences.aiConsentAccepted, isTrue);
    expect(DateTime.tryParse(preferences.aiConsentAcceptedAt), isNotNull);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(preferences.aiConsentProcessorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(preferences.aiConsentClientVersion, '1.0.528+804');
    expect(preferences.aiConsentLocale, 'en-US');

    preferences.declineAiConsent();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentAcceptedAt, isEmpty);
    expect(preferences.aiConsentContractVersion, isEmpty);
    expect(preferences.aiConsentProcessorSetHash, isEmpty);
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
    preferences.uid = 'uid-a';
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isFalse);

    await preferences.saveString('aiConsentContractVersion', 'voice-ai-processors-v1');
    expect(preferences.aiConsentAccepted, isFalse);

    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}current-receipt',
      uid: 'uid-a',
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_current-receipt');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isTrue);
  });

  test('existing v4 account requires v5 before any AI action', () async {
    SharedPreferences.setMockInitialValues({
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentContractVersion': 'ai-data-processors-v4',
      'aiConsentReceiptId': 'ios-ai-consent:ai-data-processors-v4:receipt-a',
      'aiConsentReceiptUid': 'uid-a',
    });
    await SharedPreferencesUtil.init();

    final preferences = SharedPreferencesUtil();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.hasPriorAccountBoundAiConsent('uid-a'), isTrue);
    expect(preferences.hasPriorAccountBoundAiConsent('uid-b'), isFalse);
  });

  test('deferred v5 remains inactive until a server-verified account grant', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';

    preferences.deferAiConsent();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.isCurrentAiConsentDeferred, isTrue);

    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
    );
    expect(preferences.aiConsentAccepted, isFalse);
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.isCurrentAiConsentDeferred, isFalse);
    expect(preferences.aiConsentContractVersion, 'ai-data-processors-v5');
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
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');
    expect(preferences.aiConsentAccepted, isTrue);

    preferences.uid = 'uid-b';

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.hasAccountBoundAiConsent('uid-b'), isFalse);
  });

  test('processor hash change fails closed even when version and receipt look current', () async {
    SharedPreferences.setMockInitialValues({
      'uid': 'uid-a',
      'aiConsentAccepted': true,
      'aiConsentContractVersion': SharedPreferencesUtil.currentAiConsentContractVersion,
      'aiConsentProcessorSetHash': 'sha256:stale',
      'aiConsentReceiptId': '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      'aiConsentReceiptUid': 'uid-a',
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('expired server verification fails closed without deleting the cached receipt', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(receiptId: 'aicr_receipt-a', uid: 'uid-a');
    _markServerVerified(
      preferences,
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      verifiedAt: DateTime.now().subtract(SharedPreferencesUtil.aiConsentServerVerificationTtl),
    );

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, 'aicr_receipt-a');
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
    verifiedAt: verifiedAt,
  );
}
