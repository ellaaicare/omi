import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ella_chat_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('chat sends no protected text without current account processor consent', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    expect(preferences.aiConsentAccepted, isTrue);
    preferences.uid = 'uid-b';

    await expectLater(
      sendEllaChatStream('private message').toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.consentRequired),
      ),
    );
  });
}
