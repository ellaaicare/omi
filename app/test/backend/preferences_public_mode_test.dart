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
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );

    expect(preferences.aiConsentAccepted, isFalse);
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');

    expect(preferences.aiConsentAccepted, isTrue);
    expect(DateTime.tryParse(preferences.aiConsentAcceptedAt), isNotNull);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(preferences.aiConsentProcessorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(preferences.aiConsentClientVersion, '1.0.528+804');
    expect(preferences.aiConsentLocale, 'en-US');
    expect(preferences.aiConsentProfileBindingId, 'profile-binding-a');
    expect(preferences.aiConsentScopeVersion, SharedPreferencesUtil.currentAiConsentScopeVersion);
    expect(preferences.aiConsentScopeHash, SharedPreferencesUtil.currentAiConsentScopeHash);
    expect(preferences.aiConsentServerDecidedAt, '2026-07-27T00:00:00Z');

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
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_current-receipt');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.hasAccountBoundAiConsent('uid-a'), isTrue);
  });

  test('existing v7 account requires v8 before any managed-cloud AI action', () async {
    SharedPreferences.setMockInitialValues({
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentContractVersion': 'ai-data-processors-v7',
      'aiConsentProcessorSetHash': 'sha256:dd84e4a9da1166cff66e5de55c2570d0496a2c89d46ca431530e993758616296',
      'aiConsentReceiptId': 'aicr_v7-receipt-a',
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

  test('deferred v8 remains inactive until a server-verified account and profile grant', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';

    preferences.deferAiConsent();
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.isCurrentAiConsentDeferred, isTrue);

    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    expect(preferences.aiConsentAccepted, isFalse);
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.isCurrentAiConsentDeferred, isFalse);
    expect(preferences.aiConsentContractVersion, 'ai-data-processors-v8');
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
      'aiConsentProfileBindingId': 'profile-binding-a',
      'aiConsentScopeVersion': SharedPreferencesUtil.currentAiConsentScopeVersion,
      'aiConsentScopeHash': SharedPreferencesUtil.currentAiConsentScopeHash,
      'aiConsentServerDecidedAt': '2026-07-27T00:00:00Z',
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('expired server verification fails closed without deleting the cached receipt', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    _markServerVerified(
      preferences,
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      verifiedAt: DateTime.now().subtract(SharedPreferencesUtil.aiConsentServerVerificationTtl),
    );

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, 'aicr_receipt-a');
  });

  test('provider, profile, or Photon scope drift fails closed without deleting the cached receipt', () async {
    SharedPreferences.setMockInitialValues({
      'uid': 'uid-a',
      'aiConsentAccepted': true,
      'aiConsentContractVersion': SharedPreferencesUtil.currentAiConsentContractVersion,
      'aiConsentProcessorSetHash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      'aiConsentReceiptId': 'aicr_receipt-a',
      'aiConsentReceiptUid': 'uid-a',
      'aiConsentProfileBindingId': 'profile-binding-a',
      'aiConsentScopeVersion': SharedPreferencesUtil.currentAiConsentScopeVersion,
      'aiConsentScopeHash': 'sha256:stale-scope',
      'aiConsentServerDecidedAt': '2026-07-27T00:00:00Z',
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
    expect(SharedPreferencesUtil().aiConsentReceiptId, 'aicr_receipt-a');
  });

  test('profile selection change invalidates ephemeral authority until the server re-verifies it', () {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    _markServerVerified(preferences, uid: 'uid-a', receiptId: 'aicr_receipt-a');
    expect(preferences.aiConsentAccepted, isTrue);

    preferences.verifiedPersonaId = 'persona-b';

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
    profileBindingId: 'profile-binding-a',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    verifiedAt: verifiedAt,
  );
}
