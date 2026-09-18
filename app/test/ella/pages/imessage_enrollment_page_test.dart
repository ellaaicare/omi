import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/pages/imessage_enrollment_page.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';
import 'package:omi/ella/services/imessage_enrollment_controller.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  test('enrollment surface is only supported on native iOS', () {
    expect(
      isImessageEnrollmentSupportedPlatform(platform: TargetPlatform.iOS, isWeb: false),
      isTrue,
    );
    expect(
      isImessageEnrollmentSupportedPlatform(platform: TargetPlatform.android, isWeb: false),
      isFalse,
    );
    expect(
      isImessageEnrollmentSupportedPlatform(platform: TargetPlatform.iOS, isWeb: true),
      isFalse,
    );
  });

  testWidgets('unsupported platforms render unavailable without contacting enrollment', (tester) async {
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    await tester.pumpWidget(
      _TestApp(
        controller: controller,
        pagePlatformSupported: false,
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('iMessage is unavailable'), findsOneWidget);
    expect(find.text('Set up iMessage'), findsNothing);
    expect(gateway.fetchStatusCalls, 0);
  });

  testWidgets('requires explicit consent and keeps registration pending until server verification', (tester) async {
    await tester.binding.setSurfaceSize(const Size(430, 932));
    addTearDown(() => tester.binding.setSurfaceSize(null));

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

    await tester.pumpWidget(_TestApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Not connected'), findsOneWidget);
    await tester.tap(find.text('Set up iMessage'));
    await tester.pumpAndSettle();

    expect(
      find.textContaining('Ella self-hosted Hermes and Honcho, Photon iMessage transport process'),
      findsOneWidget,
    );
    final continueButton = tester.widget<ElevatedButton>(
      find.widgetWithText(ElevatedButton, 'Agree and continue'),
    );
    expect(continueButton.onPressed, isNull);

    await _scrollTo(tester, find.text('Not now'));
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();
    expect(gateway.startCalls, 0);
    expect(gateway.consentDecisions, [ImessageConsentDecision.declined]);

    await _scrollTo(tester, find.text('Set up iMessage'));
    await tester.tap(find.text('Set up iMessage'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '+12025550123');
    await _scrollTo(tester, find.byType(CheckboxListTile));
    await tester.tap(find.byType(CheckboxListTile));
    await tester.pump();
    await _scrollTo(tester, find.text('Agree and continue'));
    await tester.tap(find.text('Agree and continue'));
    await tester.pumpAndSettle();

    expect(gateway.consentDecisions, [ImessageConsentDecision.declined, ImessageConsentDecision.granted]);
    expect(gateway.startCalls, 1);
    expect(find.text('Verification pending'), findsOneWidget);
    expect(find.text('Ready for private messages'), findsNothing);

    await _scrollTo(tester, find.text('Open Messages'));
    await tester.tap(find.text('Open Messages'));
    await tester.pump();
    expect(launched.single.toString(), 'sms:+12025550123&body=123456');
  });

  testWidgets('reconstructed page keeps the original pending proof without another grant', (tester) async {
    final gateway = _FakeGateway();
    final sessionStore = ImessageEnrollmentSessionStore();
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
    await tester.pumpWidget(_TestApp(controller: first));
    await tester.pumpAndSettle();
    expect(find.text('Open Messages'), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    first.dispose();
    final reconstructed = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'unexpected-new-id',
      appInfoReader: _appInfo,
      sessionStore: sessionStore,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );
    await tester.pumpWidget(_TestApp(controller: reconstructed));
    await tester.pumpAndSettle();

    expect(find.text('Verification pending'), findsOneWidget);
    expect(find.text('Open Messages'), findsOneWidget);
    expect(find.text('Start again'), findsNothing);
    expect(gateway.startCalls, 1);
    expect(gateway.consentDecisions, [ImessageConsentDecision.granted]);
  });

  testWidgets('every server state with a live binding exposes owner disconnect', (tester) async {
    for (final status in [
      _status(ImessageEnrollmentState.verificationPending, destination: '+12025550123'),
      _status(ImessageEnrollmentState.ready, textDm: false),
      _status(ImessageEnrollmentState.temporarilyUnavailable),
    ]) {
      final gateway = _FakeGateway()..status = status;
      final controller = ImessageEnrollmentController(
        gateway: gateway,
        consentGateway: gateway,
        authorityReader: () => 'owner-a',
        messagesLauncher: (_) async => true,
        idGenerator: () => 'request-1',
        appInfoReader: _appInfo,
      );

      await tester.pumpWidget(_TestApp(controller: controller));
      await tester.pumpAndSettle();

      expect(find.text('Disconnect this iPhone'), findsOneWidget, reason: '${status.state} must be revocable');
      await tester.pumpWidget(const SizedBox.shrink());
      controller.dispose();
    }
  });

  testWidgets('failed consent cleanup stays visibly retryable after binding close', (tester) async {
    final gateway = _FakeGateway()..revokedConsentFailuresRemaining = 1;
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

    await tester.pumpWidget(_TestApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Finish disconnecting'), findsOneWidget);
    expect(find.textContaining('still needs to finish withdrawing'), findsOneWidget);
    await tester.tap(find.text('Finish disconnecting'));
    await tester.pumpAndSettle();

    expect(gateway.revokeCalls, 1);
    expect(gateway.revokedConsentCalls, 2);
    expect(find.text('Connect again'), findsOneWidget);
  });

  testWidgets('fresh binding-revoked status offers Finish disconnecting and skips binding revoke', (tester) async {
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
    );

    await tester.pumpWidget(_TestApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Finish disconnecting'), findsOneWidget);
    await tester.tap(find.text('Finish disconnecting'));
    await tester.pumpAndSettle();

    expect(gateway.revokeCalls, 0);
    expect(gateway.revokedConsentCalls, 1);
    expect(find.text('Connect again'), findsOneWidget);
  });

  testWidgets('fresh consent-revoked status is terminal and offers reconnect', (tester) async {
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
      idGenerator: () => throw StateError('terminal state cannot mutate'),
      appInfoReader: _appInfo,
    );

    await tester.pumpWidget(_TestApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Finish disconnecting'), findsNothing);
    expect(find.text('Connect again'), findsOneWidget);
    expect(gateway.revokeCalls, 0);
    expect(gateway.revokedConsentCalls, 0);
  });

  testWidgets('server ready without text DM capability is presented unavailable', (tester) async {
    final gateway = _FakeGateway()..status = _status(ImessageEnrollmentState.ready, textDm: false);
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => 'owner-a',
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
    );

    await tester.pumpWidget(_TestApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('iMessage is unavailable'), findsOneWidget);
    expect(find.text('Ready for private messages'), findsNothing);
  });

  testWidgets('auth change clears owner A UI and loads owner B state', (tester) async {
    var authority = 'owner-a';
    final authorityChanges = StreamController<String?>();
    addTearDown(authorityChanges.close);
    final gateway = _FakeGateway();
    final controller = ImessageEnrollmentController(
      gateway: gateway,
      consentGateway: gateway,
      authorityReader: () => authority,
      messagesLauncher: (_) async => true,
      idGenerator: () => 'request-1',
      appInfoReader: _appInfo,
      now: () => DateTime.utc(2026, 9, 18, 8),
    );

    await tester.pumpWidget(_TestApp(controller: controller, authorityChanges: authorityChanges.stream));
    await tester.pumpAndSettle();
    await controller.loadConsentPolicy();
    await controller.start('+12025550123');
    await tester.pump();
    expect(find.text('Verification pending'), findsOneWidget);

    gateway.status = _status(ImessageEnrollmentState.notConnected);
    authority = 'owner-b';
    authorityChanges.add(authority);
    await tester.pumpAndSettle();

    expect(find.text('Verification pending'), findsNothing);
    expect(find.text('Not connected'), findsOneWidget);
    expect(controller.proof, isNull);
    expect(controller.consentPolicy, isNull);
  });
}

Future<void> _scrollTo(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(finder, 240, scrollable: find.byType(Scrollable).first);
  await tester.pumpAndSettle();
}

class _TestApp extends StatelessWidget {
  const _TestApp({required this.controller, this.authorityChanges, this.pagePlatformSupported = true});

  final ImessageEnrollmentController controller;
  final Stream<String?>? authorityChanges;
  final bool pagePlatformSupported;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppLocalizations.supportedLocales,
      home: ImessageEnrollmentPage(
        controller: controller,
        authorityChanges: authorityChanges,
        platformSupported: pagePlatformSupported,
      ),
    );
  }
}

class _FakeGateway implements ImessageEnrollmentGateway, ImessageConsentGateway {
  ImessageEnrollmentStatus status = _status(ImessageEnrollmentState.notConnected);
  int fetchStatusCalls = 0;
  int startCalls = 0;
  int revokeCalls = 0;
  int revokedConsentCalls = 0;
  int revokedConsentFailuresRemaining = 0;
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
    if (decision == ImessageConsentDecision.revoked) {
      revokedConsentCalls += 1;
      if (revokedConsentFailuresRemaining > 0) {
        revokedConsentFailuresRemaining -= 1;
        throw const ImessageEnrollmentFailure(ImessageEnrollmentFailureKind.transport);
      }
    }
    consentDecisions.add(decision);
    if (decision == ImessageConsentDecision.revoked) {
      status = _status(
        ImessageEnrollmentState.revoked,
        reason: ImessageEnrollmentReason.consentRevoked,
        generation: 0,
      );
    }
    return _receipt(policy, decision);
  }

  @override
  Future<ImessageEnrollmentStatus> fetchStatus() async {
    fetchStatusCalls += 1;
    return status;
  }

  @override
  Future<ImessageEnrollmentStatus> revoke({required int expectedGeneration, required String idempotencyKey}) async {
    revokeCalls += 1;
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
        'your handset phone number used for iMessage transport registration',
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
