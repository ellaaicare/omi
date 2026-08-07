import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/ai_consent_policy.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('authenticated provisioning gate is enabled by default', () {
    expect(isHermesProvisioningGateEnabled, isTrue);
  });

  test('ensure request contains client context but no caller identity', () {
    final context = EllaProvisioningRequestContext(
      appVersion: '1.0.524+800',
      locale: 'en-US',
      timezone: 'America/Los_Angeles',
      clientRequestId: 'request-1',
      consentReceiptId: 'consent-1',
    );

    final payload = context.toJson();

    expect(payload['target_schema_version'], ellaProvisioningTargetSchema);
    expect(payload['client_request_id'], 'request-1');
    expect(payload['consent_receipt_id'], 'consent-1');
    expect(payload['client'], {
      'platform': 'ios',
      'app_version': '1.0.524+800',
      'locale': 'en-US',
      'timezone': 'America/Los_Angeles',
    });
    expect(payload.containsKey('uid'), isFalse);
    expect(payload.containsKey('email'), isFalse);
    expect(payload.containsKey('profile_id'), isFalse);
    expect(payload.containsKey('runtime_provider'), isFalse);
    expect(payload.containsKey('runtime_mode'), isFalse);
    expect(payload.containsKey('endpoint'), isFalse);
    expect(payload.toString(), isNot(contains('token')));
  });

  test('exact backend ready receipt requires positive numeric binding revision', () {
    final ready = EllaProvisioningReceipt.fromJson({
      'job_id': '11111111-1111-1111-1111-111111111111',
      'state': 'ready',
      'stage': 'ready',
      'retryable': false,
      'retry_after_ms': null,
      'support_code': 'ELLA-11111111',
      'target_schema_version': 'hermes-user-v1',
      'binding_state': 'active',
      'binding_revision': 2,
      'effective_policy_revision': 'frontier-v1:ella-voice-v1',
    });
    final incomplete = EllaProvisioningReceipt.fromJson({
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 0,
      'effective_policy_revision': 'frontier-v1:ella-voice-v1',
    });

    expect(ready.isOperational, isTrue);
    expect(ready.bindingRevision, 2);
    expect(incomplete.isOperational, isFalse);
  });

  test('public route receipt distinguishes an isolated cloud pilot from retained compatibility', () {
    final cloud = EllaProvisioningReceipt.fromJson({
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 4,
      'effective_policy_revision': 'cloud-policy-v1',
      'runtime_provider': 'hermes_cloud',
      'runtime_status': 'internal_canary',
    });
    final retained = EllaProvisioningReceipt.fromJson({
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 1,
      'effective_policy_revision': 'retained-compatibility-v1',
      'compatibility_mode': 'retained',
    });

    expect(cloud.isOperational, isTrue);
    expect(cloud.isCloudRuntime, isTrue);
    expect(cloud.runtimeStatus, 'internal_canary');
    expect(cloud.compatibilityMode, isEmpty);
    expect(cloud.effectiveVoiceMode, isEmpty);

    expect(retained.isOperational, isTrue);
    expect(retained.isRetainedCompatibility, isTrue);
    expect(retained.runtimeProvider, isEmpty);
    expect(retained.effectiveVoiceMode, isEmpty);
  });

  test('status request uses the same canonical schema as ensure', () {
    final ensureUri = Uri.parse(buildEllaProvisioningEnsureUrl('https://api.example.test/'));
    final uri = Uri.parse(buildEllaProvisioningStatusUrl('https://api.example.test/'));

    expect(ensureUri.path, '/v1/ella/onboarding/ensure');
    expect(ensureUri.host, 'api.example.test');
    expect(uri.path, '/v1/ella/onboarding/status');
    expect(uri.queryParameters, {'target_schema_version': 'hermes-user-v1'});
  });

  test('FastAPI detail error is exposed as a blocked receipt code', () {
    final receipt = EllaProvisioningReceipt.fromJson({
      'detail': {'code': 'provisioning_disabled'},
    });
    final stringDetail = EllaProvisioningReceipt.fromJson({'detail': 'auth_required'});

    expect(receipt.state, EllaProvisioningState.blocked);
    expect(receipt.errorCode, 'provisioning_disabled');
    expect(receipt.isOperational, isFalse);
    expect(stringDetail.errorCode, 'auth_required');
  });

  test('receipt carrying gateway credentials fails closed and is not cacheable authority', () {
    final receipt = EllaProvisioningReceipt.fromJson({
      'state': 'ready',
      'binding_state': 'active',
      'policy_revision': 'policy-1',
      'gatewayUrl': 'https://example.invalid',
      'accessToken': 'must-not-reach-the-client',
    });

    expect(receipt.state, EllaProvisioningState.blocked);
    expect(receipt.errorCode, 'unsafe_response_contract');
    expect(receipt.isOperational, isFalse);
    expect(receipt.toCacheJson().toString(), isNot(contains('must-not-reach-the-client')));
  });

  test('account switch clears legacy authority, consent, demo state, settings, and receipts', () async {
    SharedPreferences.setMockInitialValues({
      'ellaProvisioningAccountUid': 'uid-a',
      'ellaProvisioningReceipt:uid-a': '{"state":"ready"}',
      'ellaProvisioningReceipt:uid-b': '{"state":"ready"}',
      'ellaGatewayUrl': 'https://gateway.invalid',
      'ellaGatewayToken': 'secret',
      'ellaResolvedEndpoint': '{"token":"secret"}',
      'devTtsProvider': 'grok-voice',
      'ellaSettingsVoiceModeDirty': true,
      'aiConsentAccepted': true,
      'aiConsentAcceptedAt': '2026-01-01T00:00:00Z',
      'aiConsentReceiptId': '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}consent-a',
      'aiConsentReceiptUid': 'uid-a',
      'demoMode': true,
      'publicMode': true,
      'cachedMessages': ['{"id":"old-user-message"}'],
    });
    await SharedPreferencesUtil.init();
    final preferences = SharedPreferencesUtil();

    await preferences.prepareEllaProvisioningAccount('uid-b');

    expect(preferences.getString('ellaProvisioningAccountUid'), 'uid-b');
    expect(preferences.getEllaProvisioningReceipt('uid-a'), isNull);
    expect(preferences.getEllaProvisioningReceipt('uid-b'), isNull);
    expect(preferences.ellaGatewayUrl, isEmpty);
    expect(preferences.ellaGatewayToken, isEmpty);
    expect(preferences.getString('ellaResolvedEndpoint'), isEmpty);
    expect(preferences.getString('devTtsProvider'), isEmpty);
    expect(preferences.getBool('ellaSettingsVoiceModeDirty'), isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
    expect(preferences.aiConsentReceiptUid, isEmpty);
    expect(preferences.demoMode, isFalse);
    expect(preferences.publicMode, isFalse);
    expect(preferences.getStringList('cachedMessages'), isEmpty);
  });

  test(
    'same retained account preserves current consent and legacy preferences without making them authority',
    () async {
      SharedPreferences.setMockInitialValues({
        'ellaProvisioningAccountUid': 'uid-a',
        'ellaGatewayUrl': 'https://gateway.invalid',
        'ellaGatewayToken': 'secret',
        'ellaKey': 'legacy-key',
        'aiConsentAccepted': true,
        'aiConsentReceiptId': '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}consent-a',
        'aiConsentReceiptUid': 'uid-a',
        'aiConsentContractVersion': SharedPreferencesUtil.currentAiConsentContractVersion,
        'aiConsentProcessorSetHash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
        'aiConsentProfileBindingId': 'profile-binding-a',
        'aiConsentScopeVersion': SharedPreferencesUtil.currentAiConsentScopeVersion,
        'aiConsentScopeHash': SharedPreferencesUtil.currentAiConsentScopeHash,
        'aiConsentServerDecidedAt': '2026-07-27T00:00:00Z',
        'uid': 'uid-a',
      });
      await SharedPreferencesUtil.init();
      final preferences = SharedPreferencesUtil();
      preferences.markAiConsentServerVerified(
        uid: 'uid-a',
        receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}consent-a',
        policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
        processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
        profileBindingId: 'profile-binding-a',
        scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
        scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
      );

      await preferences.prepareEllaProvisioningAccount('uid-a');

      expect(preferences.ellaGatewayUrl, 'https://gateway.invalid');
      expect(preferences.ellaGatewayToken, 'secret');
      expect(preferences.ellaKey, 'legacy-key');
      expect(preferences.aiConsentAccepted, isTrue);
      expect(preferences.hasAccountBoundAiConsent('uid-a'), isTrue);
    },
  );

  test('provider opens Home authority only for a complete ready receipt', () async {
    final transport = _FakeTransport(
      ensureResponses: [
        EllaProvisioningResponse(
          statusCode: 200,
          receipt: EllaProvisioningReceipt.fromJson({
            'state': 'ready',
            'binding_state': 'active',
            'binding_revision': 1,
            'effective_policy_revision': 'policy-1',
          }),
        ),
      ],
    );
    final provider = EllaProvisioningProvider(transport: transport);

    await provider.start(uid: 'uid-a', requestContext: _requestContext);

    expect(provider.state, EllaProvisioningState.ready);
    expect(provider.isOperational, isTrue);
    expect(transport.ensureCalls, 1);
    expect(transport.statusCalls, 0);
  });

  test('HTTP 426 is a hard update gate and never enters Home authority', () async {
    final transport = _FakeTransport(
      ensureResponses: const [EllaProvisioningResponse(statusCode: 426)],
    );
    final provider = EllaProvisioningProvider(transport: transport);

    await provider.start(uid: 'uid-a', requestContext: _requestContext);

    expect(provider.state, EllaProvisioningState.blocked);
    expect(provider.errorCode, 'upgrade_required');
    expect(provider.isOperational, isFalse);
    expect(transport.ensureCalls, 1);
    expect(transport.statusCalls, 0);
  });

  test('duplicate starts for one account share a single ensure request', () async {
    final transport = _DeferredEnsureTransport();
    final provider = EllaProvisioningProvider(transport: transport);

    final firstStart = provider.start(uid: 'uid-a', requestContext: _requestContext);
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);
    final duplicateStart = provider.start(
      uid: 'uid-a',
      requestContext: EllaProvisioningRequestContext(
        appVersion: '1.0.524+800',
        locale: 'en-US',
        timezone: 'America/Los_Angeles',
        clientRequestId: 'duplicate-request-must-not-send',
      ),
    );

    expect(transport.ensureCalls, 1);
    transport.complete(
      EllaProvisioningResponse(
        statusCode: 200,
        receipt: EllaProvisioningReceipt.fromJson({
          'state': 'ready',
          'binding_state': 'active',
          'binding_revision': 1,
          'effective_policy_revision': 'policy-1',
        }),
      ),
    );
    await Future.wait([firstStart, duplicateStart]);

    expect(provider.isOperational, isTrue);
    expect(transport.ensureCalls, 1);
  });

  test('provisioning writes only a safe receipt and leaves retained compatibility values unchanged', () async {
    SharedPreferences.setMockInitialValues({
      'ellaProvisioningAccountUid': 'uid-a',
      'ellaUserId': 'retained-user',
      'ellaGatewayUrl': 'https://retained.invalid',
      'ellaGatewayToken': 'retained-token',
    });
    await SharedPreferencesUtil.init();
    final transport = _FakeTransport(
      ensureResponses: [
        EllaProvisioningResponse(
          statusCode: 200,
          receipt: EllaProvisioningReceipt.fromJson({
            'state': 'ready',
            'binding_state': 'active',
            'binding_revision': 1,
            'effective_policy_revision': 'policy-1',
            'support_code': 'ELLA-SAFE',
          }),
        ),
      ],
    );
    final provider = EllaProvisioningProvider(transport: transport);

    await provider.start(uid: 'uid-a', requestContext: _requestContext);

    final preferences = SharedPreferencesUtil();
    final cachedReceipt = preferences.getEllaProvisioningReceipt('uid-a');
    expect(preferences.ellaUserId, 'retained-user');
    expect(preferences.ellaGatewayUrl, 'https://retained.invalid');
    expect(preferences.ellaGatewayToken, 'retained-token');
    expect(cachedReceipt?['support_code'], 'ELLA-SAFE');
    expect(cachedReceipt.toString(), isNot(contains('gateway')));
    expect(cachedReceipt.toString(), isNot(contains('token')));
  });

  test('provider bounds polling and surfaces timeout without entering Home', () async {
    final scheduled = <_FakePollHandle>[];
    final transport = _FakeTransport(
      ensureResponses: [
        const EllaProvisioningResponse(
          statusCode: 202,
          receipt: EllaProvisioningReceipt(
            state: EllaProvisioningState.queued,
            retryable: true,
            retryAfter: Duration(milliseconds: 500),
          ),
        ),
      ],
      statusResponses: [
        const EllaProvisioningResponse(
          statusCode: 202,
          receipt: EllaProvisioningReceipt(
            state: EllaProvisioningState.provisioning,
            retryable: true,
            retryAfter: Duration(milliseconds: 500),
          ),
        ),
      ],
    );
    final provider = EllaProvisioningProvider(
      transport: transport,
      maxPollAttempts: 1,
      scheduler: (delay, callback) {
        final handle = _FakePollHandle(delay, callback);
        scheduled.add(handle);
        return handle;
      },
    );

    await provider.start(uid: 'uid-a', requestContext: _requestContext);
    expect(provider.state, EllaProvisioningState.queued);
    expect(scheduled, hasLength(1));

    scheduled.single.fire();
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);

    expect(transport.statusCalls, 1);
    expect(provider.state, EllaProvisioningState.degraded);
    expect(provider.errorCode, 'provisioning_timeout');
    expect(provider.isOperational, isFalse);
  });

  test('provider retries pending ensure with a newly acknowledged consent receipt', () async {
    final scheduled = <_FakePollHandle>[];
    final transport = _FakeTransport(
      ensureResponses: const [
        EllaProvisioningResponse(
          statusCode: 202,
          receipt: EllaProvisioningReceipt(state: EllaProvisioningState.queued, retryable: true),
        ),
        EllaProvisioningResponse(
          statusCode: 202,
          receipt: EllaProvisioningReceipt(state: EllaProvisioningState.queued, retryable: true),
        ),
      ],
    );
    final provider = EllaProvisioningProvider(
      transport: transport,
      scheduler: (delay, callback) {
        final handle = _FakePollHandle(delay, callback);
        scheduled.add(handle);
        return handle;
      },
    );

    await provider.start(uid: 'uid-a', requestContext: _requestContext);
    provider.setConsentReceiptId('consent-2');
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);

    expect(transport.ensureCalls, 2);
    expect(transport.ensureContexts.last.consentReceiptId, 'consent-2');
    provider.dispose();
  });

  test('AI consent becomes authority only after an exact server v8 managed-cloud grant', () async {
    SharedPreferencesUtil().uid = 'uid-a';
    final transport = _FakeConsentTransport(policy: AiConsentPolicy.bundled, submitResponse: _consentStatus());
    final service = EllaAiConsentService(
      transport: transport,
      requestIdFactory: () => 'request-0001',
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final receiptId = await service.grantCurrentConsent(uid: 'uid-a');

    expect(receiptId, 'aicr_server-receipt');
    expect(transport.policyCalls, 1);
    expect(transport.statusCalls, 0);
    expect(transport.submissions, hasLength(1));
    expect(transport.submissions.single.toJson(), {
      'decision': 'granted',
      'policy_version': SharedPreferencesUtil.currentAiConsentContractVersion,
      'processor_set_hash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      'request_id': 'request-0001',
      'app_version': '1.0.528',
      'build_number': '804',
      'locale': 'en-US',
      'scope_version': SharedPreferencesUtil.currentAiConsentScopeVersion,
      'scope_hash': SharedPreferencesUtil.currentAiConsentScopeHash,
    });
    expect(SharedPreferencesUtil().hasAccountBoundAiConsent('uid-a'), isTrue);
    expect(SharedPreferencesUtil().aiConsentReceiptId, receiptId);
    expect(SharedPreferencesUtil().aiConsentProcessorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(SharedPreferencesUtil().aiConsentClientVersion, '1.0.528+804');
    expect(SharedPreferencesUtil().aiConsentLocale, 'en-US');
    expect(SharedPreferencesUtil().aiConsentProfileBindingId, 'profile-binding-a');
    expect(SharedPreferencesUtil().aiConsentScopeVersion, SharedPreferencesUtil.currentAiConsentScopeVersion);
    expect(SharedPreferencesUtil().aiConsentScopeHash, SharedPreferencesUtil.currentAiConsentScopeHash);
    expect(DateTime.tryParse(SharedPreferencesUtil().aiConsentServerDecidedAt), isNotNull);
    expect(SharedPreferencesUtil().aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
  });

  test('AI consent stays disabled when the public policy is unavailable', () async {
    SharedPreferencesUtil().uid = 'uid-a';
    final transport = _FakeConsentTransport(policy: null);
    final service = EllaAiConsentService(
      transport: transport,
      requestIdFactory: () => 'request-0001',
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final receiptId = await service.grantCurrentConsent(uid: 'uid-a');

    expect(receiptId, isNull);
    expect(transport.submissions, isEmpty);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
    expect(SharedPreferencesUtil().aiConsentReceiptId, isEmpty);
  });

  test('decline submits only v8 consent metadata and grants no data authority', () async {
    SharedPreferencesUtil().uid = 'uid-a';
    final transport = _FakeConsentTransport(
      policy: AiConsentPolicy.bundled,
      submitResponse: _consentStatus(authorized: false, decision: 'declined'),
    );
    final service = EllaAiConsentService(
      transport: transport,
      requestIdFactory: () => 'request-decline',
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final declined = await service.declineCurrentConsent(uid: 'uid-a');

    expect(declined, isTrue);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
    expect(transport.submissions, hasLength(1));
    expect(transport.submissions.single.toJson(), {
      'decision': 'declined',
      'policy_version': SharedPreferencesUtil.currentAiConsentContractVersion,
      'processor_set_hash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      'request_id': 'request-decline',
      'app_version': '1.0.528',
      'build_number': '804',
      'locale': 'en-US',
      'scope_version': SharedPreferencesUtil.currentAiConsentScopeVersion,
      'scope_hash': SharedPreferencesUtil.currentAiConsentScopeHash,
    });
    expect(transport.submissions.single.toJson().toString(), isNot(contains('message')));
    expect(transport.submissions.single.toJson().toString(), isNot(contains('audio')));
    expect(transport.submissions.single.toJson().toString(), isNot(contains('memory')));
  });

  test('authenticated status refresh restores only an exact current server grant', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    final transport = _FakeConsentTransport(policy: AiConsentPolicy.bundled, statusResponse: _consentStatus());
    final service = EllaAiConsentService(transport: transport);

    final authorized = await service.refreshServerAuthority(uid: 'uid-a');

    expect(authorized, isTrue);
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentReceiptId, 'aicr_server-receipt');
    expect(transport.policyCalls, 1);
    expect(transport.statusCalls, 1);
  });

  test('active authority remains valid while an early refresh is in flight', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: 'aicr_server-receipt',
      uid: 'uid-a',
      profileBindingId: 'profile-binding-a',
      serverDecidedAt: '2026-07-27T00:00:00Z',
    );
    preferences.markAiConsentServerVerified(
      uid: 'uid-a',
      receiptId: 'aicr_server-receipt',
      policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
      processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      profileBindingId: 'profile-binding-a',
      scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
      scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
    );
    final transport = _DeferredConsentStatusTransport();
    final service = EllaAiConsentService(transport: transport);

    final refresh = service.refreshServerAuthority(uid: 'uid-a');
    await Future<void>.delayed(Duration.zero);

    expect(preferences.aiConsentAccepted, isTrue);
    transport.complete(_consentStatus());
    expect(await refresh, isTrue);
    expect(preferences.aiConsentAccepted, isTrue);
  });

  test('profile switch during delayed policy refresh cannot persist the stale grant', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    final transport = _DeferredConsentPolicyTransport(statusResponse: _consentStatus());
    final service = EllaAiConsentService(transport: transport);

    final refresh = service.refreshServerAuthority(uid: 'uid-a');
    await transport.policyStarted.future;
    preferences.verifiedPersonaId = 'persona-b';
    transport.completePolicy(AiConsentPolicy.bundled);

    expect(await refresh, isFalse);
    expect(transport.statusCalls, 0);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
  });

  test('account switch during delayed policy grant prevents submission and persistence', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    final transport = _DeferredConsentPolicyTransport(submitResponse: _consentStatus());
    final service = EllaAiConsentService(
      transport: transport,
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final grant = service.grantCurrentConsent(uid: 'uid-a');
    await transport.policyStarted.future;
    preferences.uid = 'uid-b';
    transport.completePolicy(AiConsentPolicy.bundled);

    expect(await grant, isNull);
    expect(transport.submissions, isEmpty);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
  });

  test('account switch during delayed status refresh cannot persist the stale grant', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    final transport = _DeferredConsentStatusTransport();
    final service = EllaAiConsentService(transport: transport);

    final refresh = service.refreshServerAuthority(uid: 'uid-a');
    await transport.statusStarted.future;
    preferences.uid = 'uid-b';
    transport.complete(_consentStatus());

    expect(await refresh, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
  });

  test('profile switch during delayed grant submission cannot persist the stale receipt', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.verifiedPersonaId = 'persona-a';
    final transport = _DeferredConsentSubmitTransport();
    final service = EllaAiConsentService(
      transport: transport,
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final grant = service.grantCurrentConsent(uid: 'uid-a');
    await transport.submitStarted.future;
    preferences.verifiedPersonaId = 'persona-b';
    transport.complete(_consentStatus());

    expect(await grant, isNull);
    expect(transport.submissions, hasLength(1));
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
  });

  test('stale server status fails closed even if it claims authorization', () async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    final transport = _FakeConsentTransport(
      policy: AiConsentPolicy.bundled,
      statusResponse: _consentStatus(processorSetHash: 'sha256:stale'),
    );
    final service = EllaAiConsentService(transport: transport);

    final authorized = await service.refreshServerAuthority(uid: 'uid-a');

    expect(authorized, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
  });

  test('profile binding change fails closed and requires reconsent', () async {
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
    final transport = _FakeConsentTransport(
      policy: AiConsentPolicy.bundled,
      statusResponse: _consentStatus(profileBindingId: 'profile-binding-b'),
    );

    final authorized = await EllaAiConsentService(transport: transport).refreshServerAuthority(uid: 'uid-a');

    expect(authorized, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
  });

  test('missing server decision timestamp never becomes v8 authority', () async {
    final transport = _FakeConsentTransport(
      policy: AiConsentPolicy.bundled,
      submitResponse: _consentStatus(includeServerDecidedAt: false),
    );
    SharedPreferencesUtil().uid = 'uid-a';

    final receiptId = await EllaAiConsentService(
      transport: transport,
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    ).grantCurrentConsent(uid: 'uid-a');

    expect(receiptId, isNull);
    expect(SharedPreferencesUtil().aiConsentAccepted, isFalse);
  });

  test('malformed v8 server status parses safely and cannot become authority', () {
    final status = AiConsentStatus.fromJson({
      'subject_uid': 42,
      'authorized': 'true',
      'decision': <String, Object?>{},
      'receipt_id': <String>['unexpected'],
      'policy_version': SharedPreferencesUtil.currentAiConsentContractVersion,
      'processor_set_hash': SharedPreferencesUtil.currentAiConsentProcessorSetHash,
      'profile_binding_id': true,
      'scope_version': SharedPreferencesUtil.currentAiConsentScopeVersion,
      'scope_hash': SharedPreferencesUtil.currentAiConsentScopeHash,
      'server_decided_at': 'not-a-timestamp',
      'policy': {'processors': 'unexpected'},
    });

    expect(status.authorized, isFalse);
    expect(status.subjectUid, isEmpty);
    expect(status.profileBindingId, isEmpty);
    expect(status.serverDecidedAt, isNull);
    expect(status.isCurrentGrantFor('uid-a'), isFalse);
  });

  test('revocation stops local authority before an unreachable server update', () async {
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
    final transport = _FakeConsentTransport(policy: null);
    final service = EllaAiConsentService(
      transport: transport,
      requestIdFactory: () => 'request-revoke',
      clientVersionFactory: () => '1.0.528+804',
      localeFactory: () => 'en-US',
    );

    final synced = await service.revokeCurrentConsent(uid: 'uid-a');

    expect(synced, isFalse);
    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentReceiptId, isEmpty);
    expect(transport.submissions, isEmpty);
  });
}

final _requestContext = EllaProvisioningRequestContext(
  appVersion: '1.0.524+800',
  locale: 'en-US',
  timezone: 'America/Los_Angeles',
  clientRequestId: 'request-1',
);

class _FakeTransport implements EllaProvisioningTransport {
  _FakeTransport({required this.ensureResponses, this.statusResponses = const []});

  final List<EllaProvisioningResponse> ensureResponses;
  final List<EllaProvisioningResponse> statusResponses;
  final List<EllaProvisioningRequestContext> ensureContexts = [];
  int ensureCalls = 0;
  int statusCalls = 0;

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) async {
    ensureContexts.add(context);
    return ensureResponses[ensureCalls++];
  }

  @override
  Future<EllaProvisioningResponse> status() async {
    return statusResponses[statusCalls++];
  }
}

class _FakePollHandle implements EllaProvisioningPollHandle {
  _FakePollHandle(this.delay, this.callback);

  final Duration delay;
  final VoidCallback callback;
  bool canceled = false;

  void fire() {
    if (!canceled) callback();
  }

  @override
  void cancel() => canceled = true;
}

class _DeferredEnsureTransport implements EllaProvisioningTransport {
  final Completer<EllaProvisioningResponse> _completer = Completer<EllaProvisioningResponse>();
  int ensureCalls = 0;

  @override
  Future<EllaProvisioningResponse> ensure(EllaProvisioningRequestContext context) {
    ensureCalls++;
    return _completer.future;
  }

  void complete(EllaProvisioningResponse response) => _completer.complete(response);

  @override
  Future<EllaProvisioningResponse> status() => throw StateError('status should not be called');
}

class _FakeConsentTransport extends EllaAiConsentTransport {
  _FakeConsentTransport({required this.policy, this.statusResponse, this.submitResponse});

  final AiConsentPolicy? policy;
  final AiConsentStatus? statusResponse;
  final AiConsentStatus? submitResponse;
  final List<AiConsentSubmission> submissions = [];
  int policyCalls = 0;
  int statusCalls = 0;

  @override
  Future<AiConsentPolicy?> fetchPolicy() async {
    policyCalls++;
    return policy;
  }

  @override
  Future<AiConsentStatus?> fetchStatus() async {
    statusCalls++;
    return statusResponse;
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) async {
    submissions.add(submission);
    return submitResponse;
  }
}

class _DeferredConsentPolicyTransport extends EllaAiConsentTransport {
  _DeferredConsentPolicyTransport({this.statusResponse, this.submitResponse});

  final AiConsentStatus? statusResponse;
  final AiConsentStatus? submitResponse;
  final Completer<void> policyStarted = Completer<void>();
  final Completer<AiConsentPolicy?> _policy = Completer<AiConsentPolicy?>();
  final List<AiConsentSubmission> submissions = [];
  int statusCalls = 0;

  void completePolicy(AiConsentPolicy? policy) => _policy.complete(policy);

  @override
  Future<AiConsentPolicy?> fetchPolicy() {
    policyStarted.complete();
    return _policy.future;
  }

  @override
  Future<AiConsentStatus?> fetchStatus() async {
    statusCalls++;
    return statusResponse;
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) async {
    submissions.add(submission);
    return submitResponse;
  }
}

class _DeferredConsentStatusTransport extends EllaAiConsentTransport {
  final Completer<AiConsentStatus?> _status = Completer<AiConsentStatus?>();
  final Completer<void> statusStarted = Completer<void>();

  void complete(AiConsentStatus? status) => _status.complete(status);

  @override
  Future<AiConsentPolicy?> fetchPolicy() async => AiConsentPolicy.bundled;

  @override
  Future<AiConsentStatus?> fetchStatus() {
    statusStarted.complete();
    return _status.future;
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) {
    throw StateError('submit should not be called');
  }
}

class _DeferredConsentSubmitTransport extends EllaAiConsentTransport {
  final Completer<AiConsentStatus?> _status = Completer<AiConsentStatus?>();
  final Completer<void> submitStarted = Completer<void>();
  final List<AiConsentSubmission> submissions = [];

  void complete(AiConsentStatus? status) => _status.complete(status);

  @override
  Future<AiConsentPolicy?> fetchPolicy() async => AiConsentPolicy.bundled;

  @override
  Future<AiConsentStatus?> fetchStatus() {
    throw StateError('status should not be called');
  }

  @override
  Future<AiConsentStatus?> submit(AiConsentSubmission submission) {
    submissions.add(submission);
    submitStarted.complete();
    return _status.future;
  }
}

AiConsentStatus _consentStatus({
  bool authorized = true,
  String decision = 'granted',
  String processorSetHash = SharedPreferencesUtil.currentAiConsentProcessorSetHash,
  String profileBindingId = 'profile-binding-a',
  String scopeVersion = SharedPreferencesUtil.currentAiConsentScopeVersion,
  String scopeHash = SharedPreferencesUtil.currentAiConsentScopeHash,
  bool includeServerDecidedAt = true,
}) {
  return AiConsentStatus(
    subjectUid: 'uid-a',
    authorized: authorized,
    policy: AiConsentPolicy.bundled,
    decision: decision,
    receiptId: 'aicr_server-receipt',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: processorSetHash,
    appVersion: '1.0.528',
    buildNumber: '804',
    locale: 'en-US',
    profileBindingId: profileBindingId,
    scopeVersion: scopeVersion,
    scopeHash: scopeHash,
    serverDecidedAt: includeServerDecidedAt ? DateTime.parse('2026-07-27T00:00:00Z') : null,
  );
}
