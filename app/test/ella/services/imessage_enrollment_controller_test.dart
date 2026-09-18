import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';
import 'package:omi/ella/services/imessage_enrollment_attempt_store.dart';
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
    expect(launched.single.toString(), 'sms:+12025550123&body=123456');

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

  test('page reconstruction restores owner proof and successful start identity from memory only', () async {
    final gateway = _FakeGateway();
    final sessionStore = ImessageEnrollmentSessionStore();
    final launched = <Uri>[];
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');
    first.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (uri) async {
        launched.add(uri);
        return true;
      },
      idGenerator: () => 'unexpected-new-id',
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await reconstructed.load();

    expect(reconstructed.status?.state, ImessageEnrollmentState.verificationPending);
    expect(reconstructed.proof?.code, '123456');
    expect(reconstructed.canRetryPendingStart, isTrue);
    expect(await reconstructed.openMessages(), isTrue);
    expect(launched.single.toString(), 'sms:+12025550123&body=123456');
    expect(gateway.startCalls, 1);
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted]);
  });

  test('ambiguous start identity survives controller reconstruction without a fresh grant', () async {
    final generatedIds = <String>['consent-request', 'start-attempt', 'unexpected'];
    var generatedIndex = 0;
    final gateway = _FakeGateway()..startFailuresRemaining = 1;
    final sessionStore = ImessageEnrollmentSessionStore();
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');
    first.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await reconstructed.start('+12025550123');

    expect(gateway.consentDecisions, [ImessageConsentDecision.granted]);
    expect(gateway.startedIdempotencyKeys, ['start-attempt', 'start-attempt']);
    expect(generatedIndex, 2);
    expect(reconstructed.proof?.code, '123456');
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

  test('accepted start response lost survives fresh process and replays the exact attempt', () async {
    final generatedIds = <String>['consent-request', 'start-attempt'];
    var generatedIndex = 0;
    final gateway = _FakeGateway()..startResponsesLostRemaining = 1;
    final attemptStore = ImessageEnrollmentMemoryAttemptStore();
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      attemptStore: attemptStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');

    expect(first.failure?.kind, ImessageEnrollmentFailureKind.transport);
    expect(gateway.status.state, ImessageEnrollmentState.verificationPending);
    first.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => throw StateError('must reuse the durable attempt'),
      appInfoReader: _appInfo,
      sessionStore: ImessageEnrollmentSessionStore(),
      attemptStore: attemptStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await reconstructed.load();

    expect(reconstructed.status?.state, ImessageEnrollmentState.verificationPending);
    expect(reconstructed.proof, isNull);
    expect(reconstructed.canRetryPendingStart, isTrue);

    await reconstructed.retryPendingStart();

    expect(reconstructed.proof?.code, '123456');
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted]);
    expect(gateway.startedReceipts.toSet(), hasLength(1));
    expect(gateway.startedIdempotencyKeys, ['start-attempt', 'start-attempt']);
    expect(gateway.consentAuthorityRevision, 1);
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
    await Future<void>.delayed(Duration.zero);
    expect(fetchCount, 1);
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

  test('refresh started before enrollment cannot erase the committed proof', () async {
    final staleStatus = Completer<ImessageEnrollmentStatus>();
    final gateway = _FakeGateway(fetch: () => staleStatus.future);
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

    final refresh = controller.load();
    await controller.start('+12025550123');
    expect(controller.proof?.code, '123456');

    staleStatus.complete(_status(ImessageEnrollmentState.notConnected));
    await refresh;

    expect(controller.status?.state, ImessageEnrollmentState.verificationPending);
    expect(controller.proof?.code, '123456');
    expect(controller.canOpenMessages, isTrue);
  });

  test('pull refresh is ignored while enrollment mutation is in flight', () async {
    final startResponse = Completer<ImessageEnrollmentStartResponse>();
    final gateway = _FakeGateway()..startCompletion = startResponse;
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

    final starting = controller.start('+12025550123');
    await Future<void>.delayed(Duration.zero);
    await controller.load();
    expect(gateway.fetchStatusCalls, 0);

    startResponse.complete(_startResponse());
    await starting;

    expect(controller.proof?.code, '123456');
    expect(controller.operation, ImessageEnrollmentOperation.idle);
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
    final generatedIds = <String>['grant-request', 'start-attempt', 'binding-revoke', 'consent-revoke'];
    var generatedIndex = 0;
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
    await controller.revoke();

    expect(gateway.revokedGeneration, 4);
    expect(controller.status?.state, ImessageEnrollmentState.revoked);
    expect(controller.proof, isNull);
    expect(controller.failure, isNull);
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted, ImessageConsentDecision.revoked]);
    expect(gateway.events, ['consent:granted', 'revoke', 'consent:revoked']);
  });

  test('failed consent revocation remains retryable without closing the binding twice', () async {
    final gateway = _FakeGateway()..revokedConsentFailuresRemaining = 1;
    final sessionStore = ImessageEnrollmentSessionStore();
    final generatedIds = <String>['grant-request', 'start-attempt', 'binding-revoke', 'consent-revoke'];
    var generatedIndex = 0;
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await controller.loadConsentPolicy();
    await controller.start('+12025550123');

    await controller.revoke();

    expect(controller.status?.state, ImessageEnrollmentState.revoked);
    expect(controller.consentRevocationPending, isTrue);
    expect(gateway.revokeCalls, 1);
    expect(gateway.revokedConsentCalls, 1);
    controller.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => throw StateError('retry must reuse the consent revoke identity'),
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    expect(reconstructed.handleAuthorityChanged(), isTrue);
    expect(reconstructed.status?.state, ImessageEnrollmentState.revoked);
    expect(reconstructed.consentRevocationPending, isTrue);

    await reconstructed.revoke();

    expect(reconstructed.consentRevocationPending, isFalse);
    expect(reconstructed.failure, isNull);
    expect(gateway.revokeCalls, 1);
    expect(gateway.revokedConsentCalls, 2);
    expect(gateway.events.where((event) => event == 'revoke'), hasLength(1));
    expect(gateway.revokedConsentRequestIds.toSet(), hasLength(1));
    expect(gateway.consentAuthorityRevision, 2);
  });

  test('accepted binding revoke response lost reconciles from fresh server status without a second revoke', () async {
    final generatedIds = <String>['grant-request', 'start-attempt', 'binding-revoke', 'consent-revoke'];
    var generatedIndex = 0;
    final gateway = _FakeGateway()..revokeResponsesLostRemaining = 1;
    final attemptStore = ImessageEnrollmentMemoryAttemptStore();
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      attemptStore: attemptStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');
    await first.revoke();

    expect(first.failure?.kind, ImessageEnrollmentFailureKind.transport);
    expect(gateway.revokeCalls, 1);
    expect(gateway.status.reason, ImessageEnrollmentReason.bindingRevoked);
    first.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      sessionStore: ImessageEnrollmentSessionStore(),
      attemptStore: attemptStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await reconstructed.load();

    expect(reconstructed.consentRevocationPending, isTrue);
    await reconstructed.revoke();

    expect(gateway.revokeCalls, 1);
    expect(gateway.revokedIdempotencyKeys, ['binding-revoke']);
    expect(reconstructed.status?.reason, ImessageEnrollmentReason.consentRevoked);
    expect(reconstructed.consentRevocationPending, isFalse);
  });

  test('fresh binding-revoked status resumes consent cleanup without another binding mutation', () async {
    final gateway = _FakeGateway()
      ..status = _status(
        ImessageEnrollmentState.revoked,
        reason: ImessageEnrollmentReason.bindingRevoked,
        generation: 5,
      );
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'consent-revoke',
      appInfoReader: _appInfo,
      sessionStore: ImessageEnrollmentSessionStore(),
      attemptStore: ImessageEnrollmentMemoryAttemptStore(),
    );

    await controller.load();

    expect(controller.consentRevocationPending, isTrue);
    await controller.revoke();

    expect(gateway.revokeCalls, 0);
    expect(gateway.revokedConsentCalls, 1);
    expect(controller.status?.reason, ImessageEnrollmentReason.consentRevoked);
  });

  test('accepted consent revoke response lost becomes terminal after fresh status reconciliation', () async {
    final generatedIds = <String>['grant-request', 'start-attempt', 'binding-revoke', 'consent-revoke'];
    var generatedIndex = 0;
    final gateway = _FakeGateway()..revokedConsentResponsesLostRemaining = 1;
    final attemptStore = ImessageEnrollmentMemoryAttemptStore();
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => generatedIds[generatedIndex++],
      appInfoReader: _appInfo,
      attemptStore: attemptStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');
    await first.revoke();

    expect(first.failure?.kind, ImessageEnrollmentFailureKind.transport);
    expect(first.consentRevocationPending, isTrue);
    expect(gateway.status.reason, ImessageEnrollmentReason.consentRevoked);
    expect(gateway.consentAuthorityRevision, 2);
    first.dispose();

    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => throw StateError('terminal status must not mint another identity'),
      appInfoReader: _appInfo,
      sessionStore: ImessageEnrollmentSessionStore(),
      attemptStore: attemptStore,
    );
    await reconstructed.load();

    expect(reconstructed.status?.reason, ImessageEnrollmentReason.consentRevoked);
    expect(reconstructed.consentRevocationPending, isFalse);
    expect(gateway.revokedConsentCalls, 1);
    expect(gateway.revokedConsentRequestIds, ['consent-revoke']);
    expect(gateway.consentDecisions.where((decision) => decision == ImessageConsentDecision.revoked), hasLength(1));
    expect(gateway.consentAuthorityRevision, 2);
  });

  test('fresh consent-revoked status is terminal and never offers cleanup', () async {
    final gateway = _FakeGateway()
      ..status = _status(
        ImessageEnrollmentState.revoked,
        reason: ImessageEnrollmentReason.consentRevoked,
        generation: 0,
      );
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => throw StateError('terminal status cannot mutate'),
      appInfoReader: _appInfo,
      sessionStore: ImessageEnrollmentSessionStore(),
      attemptStore: ImessageEnrollmentMemoryAttemptStore(),
    );

    await controller.load();

    expect(controller.status?.reason, ImessageEnrollmentReason.consentRevoked);
    expect(controller.consentRevocationPending, isFalse);
    expect(controller.canDisconnect, isFalse);
    expect(gateway.revokeCalls, 0);
    expect(gateway.revokedConsentCalls, 0);
  });

  test('owner switch clears the memory-only enrollment session before reconstruction', () async {
    var authority = 'owner-a';
    final gateway = _FakeGateway();
    final sessionStore = ImessageEnrollmentSessionStore();
    final first = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await first.loadConsentPolicy();
    await first.start('+12025550123');

    authority = 'owner-b';
    expect(first.handleAuthorityChanged(), isTrue);
    first.dispose();
    gateway.status = _status(ImessageEnrollmentState.notConnected);
    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-2',
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await reconstructed.load();

    expect(reconstructed.status?.state, ImessageEnrollmentState.notConnected);
    expect(reconstructed.proof, isNull);
    expect(reconstructed.canRetryPendingStart, isFalse);
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
  int startResponsesLostRemaining = 0;
  int fetchStatusCalls = 0;
  int? revokedGeneration;
  int revokeCalls = 0;
  int revokeResponsesLostRemaining = 0;
  int revokedConsentCalls = 0;
  int revokedConsentFailuresRemaining = 0;
  int revokedConsentResponsesLostRemaining = 0;
  int consentAuthorityRevision = 0;
  bool rejectRevokeAfterRevokedConsent = false;
  ImessageEnrollmentStartResponse? startResponse;
  Completer<ImessageEnrollmentStartResponse>? startCompletion;
  final consentDecisions = <ImessageConsentDecision>[];
  final startedReceipts = <String>[];
  final startedIdempotencyKeys = <String>[];
  final revokedIdempotencyKeys = <String>[];
  final revokedConsentRequestIds = <String>[];
  final Map<String, ImessageConsentReceipt> _consentReceipts = {};
  final Set<String> _acceptedStartKeys = {};
  final Set<String> _acceptedRevokeKeys = {};
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
    if (decision == ImessageConsentDecision.revoked) {
      revokedConsentCalls += 1;
      revokedConsentRequestIds.add(requestId);
      if (revokedConsentFailuresRemaining > 0) {
        revokedConsentFailuresRemaining -= 1;
        throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
      }
    }
    final existing = _consentReceipts[requestId];
    if (existing != null) return existing;
    consentAuthorityRevision += 1;
    consentDecisions.add(decision);
    events.add('consent:${decision.wireValue}');
    final receipt = _receipt(policy, decision, authorityRevision: consentAuthorityRevision);
    _consentReceipts[requestId] = receipt;
    if (decision == ImessageConsentDecision.revoked) {
      status = _status(
        ImessageEnrollmentState.revoked,
        reason: ImessageEnrollmentReason.consentRevoked,
        generation: 0,
      );
      if (revokedConsentResponsesLostRemaining > 0) {
        revokedConsentResponsesLostRemaining -= 1;
        throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
      }
    }
    return receipt;
  }

  @override
  Future<ImessageEnrollmentStatus> fetchStatus() async {
    fetchStatusCalls += 1;
    final fetch = _fetch;
    if (fetch == null) return status;
    return await fetch();
  }

  @override
  Future<ImessageEnrollmentStatus> revoke({required int expectedGeneration, required String idempotencyKey}) async {
    revokeCalls += 1;
    revokedIdempotencyKeys.add(idempotencyKey);
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
    if (!_acceptedRevokeKeys.contains(idempotencyKey)) {
      _acceptedRevokeKeys.add(idempotencyKey);
      status = _status(
        ImessageEnrollmentState.revoked,
        reason: ImessageEnrollmentReason.bindingRevoked,
        generation: expectedGeneration + 1,
      );
    }
    if (revokeResponsesLostRemaining > 0) {
      revokeResponsesLostRemaining -= 1;
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
    }
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
    final completion = startCompletion;
    if (completion != null) return completion.future;
    final override = startResponse;
    if (override != null) return override;
    _acceptedStartKeys.add(idempotencyKey);
    status = _status(ImessageEnrollmentState.verificationPending, destination: '+12025550123');
    if (startResponsesLostRemaining > 0) {
      startResponsesLostRemaining -= 1;
      throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
    }
    return ImessageEnrollmentStartResponse(
      status: status,
      proof: ImessageEnrollmentProof(code: '123456', expiresAt: DateTime.utc(2026, 9, 18, 8, 5)),
    );
  }
}

ImessageEnrollmentStartResponse _startResponse() => ImessageEnrollmentStartResponse(
      status: _status(ImessageEnrollmentState.verificationPending, destination: '+12025550123'),
      proof: ImessageEnrollmentProof(code: '123456', expiresAt: DateTime.utc(2026, 9, 18, 8, 5)),
    );

Future<({String version, String buildNumber})> _appInfo() async => (version: '1.0.0', buildNumber: '900');

String _hash(String character) => 'sha256:${List.filled(64, character).join()}';

ImessageConsentPolicy _policy() => ImessageConsentPolicy(
      policyVersion: ImessageConsentPolicy.supportedPolicyVersion,
      processorSetHash: _hash('a'),
      scopeVersion: ImessageConsentPolicy.supportedScopeVersion,
      scopeHash: _hash('b'),
      recipients: const ['Ella self-hosted Hermes and Honcho', 'Photon iMessage transport'],
      dataClasses: const [
        'your handset phone number used for iMessage transport registration',
        'the text messages you send to Ella',
        "Ella's text replies",
        'messaging delivery identifiers',
      ],
      textDmOnly: true,
    );

ImessageConsentReceipt _receipt(
  ImessageConsentPolicy policy,
  ImessageConsentDecision decision, {
  int authorityRevision = 1,
}) {
  return ImessageConsentReceipt(
    schemaVersion: ImessageConsentReceipt.schema,
    receiptId: '00000000-0000-4000-8000-000000000005',
    decision: decision,
    policyVersion: policy.policyVersion,
    processorSetHash: policy.processorSetHash,
    scopeVersion: policy.scopeVersion,
    scopeHash: policy.scopeHash,
    authorityRevision: authorityRevision,
    decidedAt: DateTime.utc(2026, 9, 18, 8),
  );
}

ImessageEnrollmentStatus _status(
  ImessageEnrollmentState state, {
  bool textDm = false,
  String? destination,
  ImessageEnrollmentReason? reason,
  int generation = 4,
}) {
  final resolvedReason = reason ??
      switch (state) {
        ImessageEnrollmentState.notConnected => ImessageEnrollmentReason.notEnrolled,
        ImessageEnrollmentState.verificationPending => ImessageEnrollmentReason.verificationPending,
        ImessageEnrollmentState.ready => ImessageEnrollmentReason.ready,
        ImessageEnrollmentState.temporarilyUnavailable => ImessageEnrollmentReason.transportUnhealthy,
        ImessageEnrollmentState.revoked => ImessageEnrollmentReason.bindingRevoked,
      };
  return ImessageEnrollmentStatus(
    schemaVersion: ImessageEnrollmentStatus.schema,
    state: state,
    reason: resolvedReason,
    authorityGeneration: generation,
    assignedDestination: destination,
    features: ImessageEnrollmentFeatures(
      textDm: textDm,
      groups: false,
      attachments: false,
      caregiverDelivery: false,
    ),
  );
}
