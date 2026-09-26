import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';

class _FakeTransport extends EllaAiConsentTransport {
  _FakeTransport({this.policy, this.submitResult, this.fetchResult, this.fetchCompleter});

  AiConsentPolicy? policy;
  AiConsentSubmitResult? submitResult;
  AiConsentFetchResult? fetchResult;
  Completer<AiConsentFetchResult>? fetchCompleter;
  int submitCalls = 0;

  @override
  Future<AiConsentPolicy?> fetchPolicy() async => policy;

  @override
  Future<AiConsentStatus?> fetchStatus() async => null;

  @override
  Future<AiConsentFetchResult> fetchStatusWithDetails() async {
    final completer = fetchCompleter;
    if (completer != null) {
      return completer.future;
    }
    return fetchResult ?? const AiConsentFetchResult();
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) async => (await submitWithDetails(submission)).status;

  @override
  Future<AiConsentSubmitResult> submitWithDetails(AiConsentSubmission submission) async {
    submitCalls++;
    return submitResult ?? const AiConsentSubmitResult();
  }
}

AiConsentStatus _currentGrantStatus(String uid) => AiConsentStatus(
      subjectUid: uid,
      authorized: true,
      policy: AiConsentPolicy.bundled,
      decision: 'granted',
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      appVersion: '1.0.0',
      buildNumber: '1',
      locale: 'en-US',
      profileBindingId: 'binding-1',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
      serverDecidedAt: DateTime.utc(2026, 8, 7),
    );

EllaAiConsentService _service(_FakeTransport transport) => EllaAiConsentService(
      transport: transport,
      pilotLocaleRestricted: false,
      requestIdFactory: () => 'request-1',
      clientVersionFactory: () => '1.0.0+1',
      localeFactory: () => 'en-US',
      appLocaleFactory: () => 'en',
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const uid = 'uid-a';

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = uid;
  });

  test('unreachable server maps to a network failure and never persists consent', () async {
    final transport = _FakeTransport(policy: AiConsentPolicy.bundled, submitResult: const AiConsentSubmitResult());
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.accepted, isFalse);
    expect(outcome.failureKind, AiConsentGrantFailureKind.network);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('503 grant rejection surfaces server-unavailable with the backend code', () async {
    final transport = _FakeTransport(
      policy: AiConsentPolicy.bundled,
      submitResult: const AiConsentSubmitResult(
        httpStatus: 503,
        errorCode: 'managed_cloud_consent_authority_unavailable',
      ),
    );
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.failureKind, AiConsentGrantFailureKind.serverUnavailable);
    expect(outcome.supportCode, 'managed_cloud_consent_authority_unavailable');
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('409 policy mismatch maps to the policy-mismatch failure kind', () async {
    final transport = _FakeTransport(
      policy: AiConsentPolicy.bundled,
      submitResult: const AiConsentSubmitResult(httpStatus: 409, errorCode: 'ai_consent_policy_mismatch'),
    );
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.failureKind, AiConsentGrantFailureKind.policyMismatch);
    expect(outcome.supportCode, 'ai_consent_policy_mismatch');
  });

  test('non-200 without a body code falls back to an http support code', () async {
    final transport = _FakeTransport(
      policy: AiConsentPolicy.bundled,
      submitResult: const AiConsentSubmitResult(httpStatus: 500),
    );
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.failureKind, AiConsentGrantFailureKind.serverUnavailable);
    expect(outcome.supportCode, 'http_500');
  });

  test('missing policy response uses the matching bundled policy for the authoritative submit', () async {
    final transport = _FakeTransport(
      policy: null,
      submitResult: AiConsentSubmitResult(httpStatus: 200, status: _currentGrantStatus(uid)),
    );
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.accepted, isTrue);
    expect(transport.submitCalls, 1);
    expect(SharedPreferencesUtil().aiConsentReceiptId, outcome.receiptId);
  });

  test('200 with a non-current grant is rejected without persisting authority', () async {
    final transport = _FakeTransport(
      policy: AiConsentPolicy.bundled,
      submitResult: const AiConsentSubmitResult(httpStatus: 200),
    );
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.failureKind, AiConsentGrantFailureKind.rejected);
    expect(outcome.supportCode, 'consent_grant_not_current');
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('current verified grant is accepted and persists the receipt', () async {
    final transport = _FakeTransport(policy: AiConsentPolicy.bundled);
    transport.submitResult = AiConsentSubmitResult(httpStatus: 200, status: _currentGrantStatus(uid));
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.accepted, isTrue);
    expect(outcome.receiptId, '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1');
    expect(SharedPreferencesUtil().aiConsentAccepted, isTrue);
    expect(SharedPreferencesUtil().aiConsentReceiptId, outcome.receiptId);
  });

  test('legacy grantCurrentConsent keeps returning only the receipt id', () async {
    final transport = _FakeTransport(policy: AiConsentPolicy.bundled);
    transport.submitResult = AiConsentSubmitResult(httpStatus: 200, status: _currentGrantStatus(uid));

    expect(
      await _service(transport).grantCurrentConsent(uid: uid),
      '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
    );
  });

  test('active refresh treats backend-unavailable 503 as retryable and preserves acceptance', () async {
    final preferences = SharedPreferencesUtil();
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      uid: uid,
      profileBindingId: 'binding-1',
      serverDecidedAt: '2026-08-07T00:00:00Z',
    );
    final transport = _FakeTransport(
      fetchResult: const AiConsentFetchResult(
        httpStatus: 503,
        errorCode: 'ai_consent_authority_unavailable',
        authorityState: 'unavailable',
        retryable: true,
      ),
    );

    final result = await _service(transport).refreshActiveSessionAuthority(
      uid: uid,
      expectedReceiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      expectedServerDecidedAt: DateTime.utc(2026, 8, 7),
    );

    expect(result.disposition, AiConsentAuthorityRefreshDisposition.retryable);
    expect(result.supportCode, 'ai_consent_authority_unavailable');
    expect(preferences.getBool('aiConsentAccepted', defaultValue: false), isTrue);
    expect(preferences.aiConsentReceiptId, '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1');
  });

  test('active refresh accepts the same receipt across bundled policy drift', () async {
    final preferences = SharedPreferencesUtil();
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      uid: uid,
      profileBindingId: 'binding-1',
      serverDecidedAt: '2026-08-07T00:00:00Z',
    );
    await preferences.saveString('aiConsentContractVersion', 'client-before-backend-deploy');
    final transport = _FakeTransport(
      fetchResult: AiConsentFetchResult(httpStatus: 200, status: _currentGrantStatus(uid)),
    );

    final result = await _service(transport).refreshActiveSessionAuthority(
      uid: uid,
      expectedReceiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      expectedServerDecidedAt: DateTime.utc(2026, 8, 7),
    );

    expect(result.disposition, AiConsentAuthorityRefreshDisposition.verified);
    expect(preferences.aiConsentAccepted, isTrue);
  });

  test('active refresh applies an explicit revoked state immediately', () async {
    final preferences = SharedPreferencesUtil();
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      uid: uid,
      profileBindingId: 'binding-1',
      serverDecidedAt: '2026-08-07T00:00:00Z',
    );
    final transport = _FakeTransport(
      fetchResult: const AiConsentFetchResult(httpStatus: 200, authorityState: 'revoked'),
    );

    final result = await _service(transport).refreshActiveSessionAuthority(
      uid: uid,
      expectedReceiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1',
      expectedServerDecidedAt: DateTime.utc(2026, 8, 7),
    );

    expect(result.disposition, AiConsentAuthorityRefreshDisposition.revoked);
    expect(preferences.aiConsentAccepted, isFalse);
  });

  test('stale terminal refresh cannot erase a newer same-account grant', () async {
    final preferences = SharedPreferencesUtil();
    const firstReceipt = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-1';
    const replacementReceipt = '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-2';
    preferences.acceptAiConsent(
      receiptId: firstReceipt,
      uid: uid,
      profileBindingId: 'binding-1',
      serverDecidedAt: '2026-08-07T00:00:00Z',
    );
    final fetchCompleter = Completer<AiConsentFetchResult>();
    final transport = _FakeTransport(fetchCompleter: fetchCompleter);

    final refresh = _service(transport).refreshActiveSessionAuthority(
      uid: uid,
      expectedReceiptId: firstReceipt,
      expectedServerDecidedAt: DateTime.utc(2026, 8, 7),
    );
    await Future<void>.delayed(Duration.zero);
    preferences.acceptAiConsent(
      receiptId: replacementReceipt,
      uid: uid,
      profileBindingId: 'binding-1',
      serverDecidedAt: '2026-08-07T00:01:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: uid,
      receiptId: replacementReceipt,
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'binding-1',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    fetchCompleter.complete(const AiConsentFetchResult(httpStatus: 200, authorityState: 'revoked'));

    final result = await refresh;
    expect(result.disposition, AiConsentAuthorityRefreshDisposition.retryable);
    expect(result.supportCode, 'authority_superseded');
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentReceiptId, replacementReceipt);
  });
}
