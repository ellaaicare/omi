import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';

class _FakeTransport extends EllaAiConsentTransport {
  _FakeTransport({this.policy, this.submitResult});

  AiConsentPolicy? policy;
  AiConsentSubmitResult? submitResult;
  int submitCalls = 0;

  @override
  Future<AiConsentPolicy?> fetchPolicy() async => policy;

  @override
  Future<AiConsentStatus?> fetchStatus() async => null;

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
      submitResult:
          const AiConsentSubmitResult(httpStatus: 503, errorCode: 'managed_cloud_consent_authority_unavailable'),
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

  test('missing policy blocks the submit and reports server-unavailable', () async {
    final transport = _FakeTransport(policy: null);
    final outcome = await _service(transport).grantCurrentConsentWithOutcome(uid: uid);

    expect(outcome.failureKind, AiConsentGrantFailureKind.serverUnavailable);
    expect(outcome.supportCode, 'consent_policy_unavailable');
    expect(transport.submitCalls, 0);
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
}
