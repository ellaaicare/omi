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

  test('pending status refresh keeps the usable one-time proof', () async {
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await controller.loadConsentPolicy();
    await controller.start('+12025550123');
    expect(controller.proof?.code, '123456');

    await controller.load();

    expect(controller.status?.state, ImessageEnrollmentState.verificationPending);
    expect(controller.proof?.code, '123456');
    expect(controller.canOpenMessages, isTrue);
  });

  test('ambiguous start retry reuses the exact receipt and idempotency key', () async {
    final generatedIds = <String>['consent-request', 'start-attempt', 'unexpected'];
    var generatedIndex = 0;
    final gateway = _FakeGateway()..startFailuresRemaining = 1;
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await controller.loadConsentPolicy();
    await controller.start('+12025550123');
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.transport);

    await controller.start('+12025550123');

    expect(gateway.consentDecisions, [ImessageConsentDecision.granted]);
    expect(gateway.startedReceipts, [
      '00000000-0000-4000-8000-000000000005',
      '00000000-0000-4000-8000-000000000005',
    ]);
    expect(gateway.startedIdempotencyKeys, ['start-attempt', 'start-attempt']);
    expect(generatedIndex, 2);
    expect(controller.status?.state, ImessageEnrollmentState.verificationPending);
    expect(controller.proof?.code, '123456');
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

  test('stale owner A completion cannot clear committed owner B state', () async {
    var authority = 'owner-a';
    var fetchCount = 0;
    final ownerAResponse = Completer<ImessageEnrollmentStatus>();
    final ownerBResponse = Completer<ImessageEnrollmentStatus>();
    final gateway = _FakeGateway(
      fetch: () {
        fetchCount += 1;
        return fetchCount == 1 ? ownerAResponse.future : ownerBResponse.future;
      },
    );
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    final ownerALoad = controller.load();
    authority = 'owner-b';
    expect(controller.handleAuthorityChanged(), isTrue);
    final ownerBLoad = controller.load();
    ownerBResponse.complete(_status(ImessageEnrollmentState.notConnected));
    await ownerBLoad;
    expect(controller.status?.state, ImessageEnrollmentState.notConnected);

    ownerAResponse.complete(_status(ImessageEnrollmentState.ready, textDm: true));
    await ownerALoad;

    expect(controller.status?.state, ImessageEnrollmentState.notConnected);
    expect(controller.failure, isNull);
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

  test('account switch after owner A starts never launches owner A proof for owner B', () async {
    var authority = 'owner-a';
    final launched = <Uri>[];
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
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

    authority = 'owner-b';

    expect(controller.status, isNull);
    expect(controller.proof, isNull);
    expect(await controller.openMessages(), isFalse);
    expect(launched, isEmpty);
    expect(controller.status, isNull);
    expect(controller.proof, isNull);
    expect(controller.consentPolicy, isNull);
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.authorityChanged);
  });

  test('start response cannot claim ready before inbound handset proof', () async {
    final launched = <Uri>[];
    final gateway = _FakeGateway()
      ..startResponse = ImessageEnrollmentStartResponse(
        status: _status(ImessageEnrollmentState.ready, textDm: true, destination: '+12025550123'),
        proof: ImessageEnrollmentProof(code: '123456', expiresAt: DateTime.utc(2026, 9, 18, 8, 5)),
      );
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

    expect(controller.isReady, isFalse);
    expect(controller.status, isNull);
    expect(controller.proof, isNull);
    expect(controller.failure?.kind, ImessageEnrollmentFailureKind.malformedResponse);
    expect(await controller.openMessages(), isFalse);
    expect(launched, isEmpty);
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

  test('revocation closes the binding before advancing consent generation', () async {
    final gateway = _FakeGateway()..rejectRevokeAfterRevokedConsent = true;
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await controller.loadConsentPolicy();
    await controller.start('+12025550123');
    await controller.revoke();

    expect(gateway.revokedGeneration, 4);
    expect(controller.status?.state, ImessageEnrollmentState.revoked);
    expect(controller.proof, isNull);
    expect(controller.failure, isNull);
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted, ImessageConsentDecision.revoked]);
    expect(gateway.events, ['consent:granted', 'revoke', 'consent:revoked']);
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
  int startFailuresRemaining = 0;
  int? revokedGeneration;
  bool rejectRevokeAfterRevokedConsent = false;
  ImessageEnrollmentStartResponse? startResponse;
  final consentDecisions = <ImessageConsentDecision>[];
  final startedReceipts = <String>[];
  final startedIdempotencyKeys = <String>[];
  final events = <String>[];

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
    events.add('consent:${decision.wireValue}');
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
    events.add('revoke');
    if (rejectRevokeAfterRevokedConsent &&
        consentDecisions.isNotEmpty &&
        consentDecisions.last == ImessageConsentDecision.revoked) {
      throw const ImessageEnrollmentFailure(
        ImessageEnrollmentFailureKind.conflict,
        code: 'imessage_binding_generation_conflict',
      );
    }
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
    startedReceipts.add(consentReceiptId);
    startedIdempotencyKeys.add(idempotencyKey);
    if (startFailuresRemaining > 0) {
      startFailuresRemaining -= 1;
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
    }
    final override = startResponse;
    if (override != null) return override;
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
      recipients: const ['Ella self-hosted Hermes and Honcho', 'Photon iMessage transport'],
      dataClasses: const [
        'the text messages you send to Ella',
        "Ella's text replies",
        'messaging delivery identifiers',
      ],
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
