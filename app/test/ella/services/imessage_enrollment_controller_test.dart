import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';
import 'package:omi/ella/services/imessage_enrollment_controller.dart';

void main() {
  test('registration stays pending until a later server status says ready', () async {
    final gateway = _FakeGateway();
    final launched = <Uri>[];
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (uri) async {
        launched.add(uri);
        return true;
      },
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await controller.loadConsentPolicy();
    await controller.start('+12025550123');

    expect(controller.status?.state, ImessageEnrollmentState.verificationPending);
    expect(controller.isReady, isFalse);
    expect(gateway.startedHandset, '+12025550123');
    expect(gateway.startedReceipt, '00000000-0000-4000-8000-000000000005');
    expect(await controller.openMessages(), isTrue);
    expect(launched.single.scheme, 'sms');
    expect(launched.single.path, '+12025550123');
    expect(launched.single.queryParameters['body'], '123456');

    gateway.status = _status(ImessageEnrollmentState.ready, textDm: true);
    await controller.load();

    expect(controller.isReady, isTrue);
  });

  test('account drift discards an in-flight owner response', () async {
    var authority = 'owner-a';
    final completion = Completer<ImessageEnrollmentStatus>();
    final gateway = _FakeGateway(fetch: () => completion.future);
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    final load = controller.load();
    authority = 'owner-b';
    completion.complete(_status(ImessageEnrollmentState.ready, textDm: true));
    await load;

    expect(controller.status, isNull);
    expect(controller.isReady, isFalse);
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.authorityChanged);
  });

  test('account drift while opening Messages fails closed', () async {
    var authority = 'owner-a';
    final launch = Completer<bool>();
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) => launch.future,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await controller.loadConsentPolicy();
    await controller.start('+12025550123');

    final opening = controller.openMessages();
    authority = 'owner-b';
    launch.complete(true);

    expect(await opening, isFalse);
    expect(controller.status, isNull);
    expect(controller.proof, isNull);
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.authorityChanged);
  });

  test('missing dedicated consent policy fails closed before registration', () async {
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    await controller.start('+12025550123');

    expect(gateway.startCalls, 0);
    expect(controller.status, isNull);
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.consentUnavailable);
  });

  test('revocation uses current generation and clears one-time proof', () async {
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    await controller.loadConsentPolicy();
    await controller.start('+12025550123');
    await controller.revoke();

    expect(gateway.revokedGeneration, 4);
    expect(controller.status?.state, ImessageEnrollmentState.revoked);
    expect(controller.proof, isNull);
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted, ImessageConsentDecision.revoked]);
  });

  test('decline records the current policy and never starts enrollment', () async {
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    await controller.loadConsentPolicy();
    await controller.declineConsent();

    expect(gateway.consentDecisions, [ImessageConsentDecision.declined]);
    expect(gateway.startCalls, 0);
    expect(controller.consentPolicy, isNull);
  });
}

class _FakeGateway implements ImessageEnrollmentGateway, ImessageConsentGateway {
  _FakeGateway({Future<ImessageEnrollmentStatus> Function()? fetch}) : _fetch = fetch;

  final Future<ImessageEnrollmentStatus> Function()? _fetch;
  ImessageEnrollmentStatus status = _status(ImessageEnrollmentState.notConnected);
  String? startedHandset;
  String? startedReceipt;
  int startCalls = 0;
  int? revokedGeneration;
  final consentDecisions = <ImessageConsentDecision>[];

  @override
  Future<ImessageConsentPolicy> fetchConsentPolicy() async => _policy();

  @override
  Future<ImessageConsentReceipt> recordConsent({
    required ImessageConsentDecision decision,
    required ImessageConsentPolicy policy,
    required String requestId,
    required String appVersion,
    required String buildNumber,
  }) async {
    consentDecisions.add(decision);
    return _receipt(policy, decision);
  }

  @override
  Future<ImessageEnrollmentStatus> fetchStatus() async {
    final fetch = _fetch;
    if (fetch == null) return status;
    return await fetch();
  }

  @override
  Future<ImessageEnrollmentStatus> revoke({required int expectedGeneration, required String idempotencyKey}) async {
    revokedGeneration = expectedGeneration;
    status = _status(ImessageEnrollmentState.revoked);
    return status;
  }

  @override
  Future<ImessageEnrollmentStartResponse> start({
    required String handsetE164,
    required String consentReceiptId,
    required String idempotencyKey,
  }) async {
    startCalls += 1;
    startedHandset = handsetE164;
    startedReceipt = consentReceiptId;
    status = _status(ImessageEnrollmentState.verificationPending, destination: '+12025550123');
    return ImessageEnrollmentStartResponse(
      status: status,
      proof: ImessageEnrollmentProof(code: '123456', expiresAt: DateTime.utc(2026, 9, 18, 8, 5)),
    );
  }
}

Future<({String version, String buildNumber})> _appInfo() async => (version: '1.0.0', buildNumber: '900');

String _hash(String character) => 'sha256:${List.filled(64, character).join()}';

ImessageConsentPolicy _policy() => ImessageConsentPolicy(
      policyVersion: ImessageConsentPolicy.supportedPolicyVersion,
      processorSetHash: _hash('a'),
      scopeVersion: ImessageConsentPolicy.supportedScopeVersion,
      scopeHash: _hash('b'),
      recipients: const ['Photon', 'Ella self-hosted Hermes', 'OpenAI'],
      dataClasses: const ['phone number', 'message text'],
      textDmOnly: true,
    );

ImessageConsentReceipt _receipt(ImessageConsentPolicy policy, ImessageConsentDecision decision) {
  return ImessageConsentReceipt(
    schemaVersion: ImessageConsentReceipt.schema,
    receiptId: '00000000-0000-4000-8000-000000000005',
    decision: decision,
    policyVersion: policy.policyVersion,
    processorSetHash: policy.processorSetHash,
    scopeVersion: policy.scopeVersion,
    scopeHash: policy.scopeHash,
    authorityRevision: 1,
    decidedAt: DateTime.utc(2026, 9, 18, 8),
  );
}

ImessageEnrollmentStatus _status(
  ImessageEnrollmentState state, {
  bool textDm = false,
  String? destination,
}) {
  final reason = switch (state) {
    ImessageEnrollmentState.notConnected => ImessageEnrollmentReason.notEnrolled,
    ImessageEnrollmentState.verificationPending => ImessageEnrollmentReason.verificationPending,
    ImessageEnrollmentState.ready => ImessageEnrollmentReason.ready,
    ImessageEnrollmentState.temporarilyUnavailable => ImessageEnrollmentReason.transportUnhealthy,
    ImessageEnrollmentState.revoked => ImessageEnrollmentReason.bindingRevoked,
  };
  return ImessageEnrollmentStatus(
    schemaVersion: ImessageEnrollmentStatus.schema,
    state: state,
    reason: reason,
    authorityGeneration: 4,
    assignedDestination: destination,
    features: ImessageEnrollmentFeatures(
      textDm: textDm,
      groups: false,
      attachments: false,
      caregiverDelivery: false,
    ),
  );
}
