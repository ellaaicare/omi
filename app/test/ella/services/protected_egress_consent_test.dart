import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/geolocation.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('protected API boundaries fail closed without current server consent', () async {
    final file = File('${Directory.systemTemp.path}/ai-consent-protected-payload');

    await expectLater(_expectConsentFailure(sendMessageStreamServer('private chat')), completes);
    await expectLater(_expectConsentFailure(sendEllaMessageStream('private Ella chat')), completes);
    await expectLater(_expectConsentFailure(sendVoiceMessageStreamServer([file])), completes);
    await expectLater(
      uploadFilesServer([file]),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.consentRequired),
      ),
    );
    expect(await sendStorageToBackend(file, '2026-07-27T00:00:00Z'), isEmpty);
    expect(await updateUserGeolocation(geolocation: Geolocation(latitude: 1, longitude: 2)), isFalse);
    expect(
      await submitConversationCorrection(conversationId: 'conversation-a', correctionText: 'private correction'),
      isFalse,
    );
    expect(await testConversationPrompt('private prompt', 'conversation-a'), isEmpty);
    expect(await retryConversationProcessing('conversation-a', 'request-a'), isNull);
    expect(await reProcessConversationServer('conversation-a'), isNull);
  });

  test('a cached v7 receipt sends no protected data under managed-cloud v8', () async {
    SharedPreferences.setMockInitialValues({
      'uid': 'uid-a',
      'aiConsentAccepted': true,
      'aiConsentContractVersion': 'ai-data-processors-v7',
      'aiConsentProcessorSetHash': 'sha256:dd84e4a9da1166cff66e5de55c2570d0496a2c89d46ca431530e993758616296',
      'aiConsentReceiptId': 'aicr_v7-receipt',
      'aiConsentReceiptUid': 'uid-a',
      'aiConsentProfileBindingId': 'profile-binding-a',
      'aiConsentScopeVersion': 'managed-cloud-internal-pilot-v1',
      'aiConsentScopeHash': 'sha256:727b1db818ce79090a02279f1cc6d15dfc3d65a58592b13fbed53ad048c38a30',
      'aiConsentServerDecidedAt': '2026-07-27T00:00:00Z',
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
    await _expectConsentFailure(sendEllaMessageStream('must not leave device'));
    await _expectConsentFailure(sendMessageStreamServer('must not leave device'));
  });

  test('revocation immediately blocks protected text, audio, and location egress', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    expect(preferences.aiConsentAccepted, isTrue);

    preferences.declineAiConsent();

    final file = File('${Directory.systemTemp.path}/revoked-v8-audio');
    await _expectConsentFailure(sendEllaMessageStream('must not leave device'));
    await _expectConsentFailure(sendVoiceMessageStreamServer([file]));
    expect(await updateUserGeolocation(geolocation: Geolocation(latitude: 1, longitude: 2)), isFalse);
  });

  test('delayed location response is rejected after exact account authority changes', () async {
    final preferences = SharedPreferencesUtil()..uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_receipt-a',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_receipt-a',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    final authority = _MutableExactAuthority('uid-a');
    final entered = Completer<void>();
    final response = Completer<http.Response?>();

    final update = updateUserGeolocation(
      geolocation: Geolocation(latitude: 1, longitude: 2),
      expectedAuthenticatedUid: 'uid-a',
      exactAuthority: authority,
      transport: ({required body, expectedAuthenticatedUid, exactAuthority}) {
        expect(expectedAuthenticatedUid, 'uid-a');
        expect(identical(exactAuthority, authority), isTrue);
        entered.complete();
        return response.future;
      },
    );
    await entered.future;
    authority.current = false;
    response.complete(http.Response('', 200));

    expect(await update, isFalse);
  });
}

class _MutableExactAuthority implements ExactAccountAuthorityVerifier {
  _MutableExactAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isExactCurrent() => current;
}

Future<void> _expectConsentFailure(Stream<Object?> stream) async {
  await expectLater(
    stream.toList(),
    throwsA(
      isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.consentRequired),
    ),
  );
}
