import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
import 'package:omi/ella/services/ella_ai_consent_service.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_invite_link_controller.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';

void main() {
  Future<void> pumpGate(
    WidgetTester tester, {
    required EllaEntitlement entitlement,
    EllaInviteRedemptionError? inviteError,
    String inviteCode = '',
    int? retryAfterSeconds,
  }) async {
    final provider = EllaEntitlementProvider.demo(
      initialEntitlement: entitlement,
      initialInviteError: inviteError,
      initialInviteCode: inviteCode,
      initialRetryAfterSeconds: retryAfterSeconds,
    );
    addTearDown(provider.dispose);
    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            startOnMount: false,
            onSignOutOverride: () {},
            readyChild: const Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pump();
  }

  setUp(EllaInviteLinkController.instance.clear);

  testWidgets('no entitlement is a warm waitlist state without error styling', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none);

    expect(find.text('You’re on the list'), findsOneWidget);
    expect(find.byIcon(Icons.favorite_outline_rounded), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsNothing);
    final icon = tester.widget<Icon>(find.byIcon(Icons.favorite_outline_rounded));
    expect(icon.color, isNot(Colors.red));
  });

  testWidgets('bound invited entitlement proceeds to provisioning', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.invited);

    expect(find.text('ONBOARDING READY'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('universal-link fixture is prefilled and needs one confirm tap', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none, inviteCode: 'ELLA7K9Q');

    expect(find.text('Your invite is ready'), findsOneWidget);
    expect(find.text('ELLA7K9Q'), findsNWidgets(2));
    expect(find.widgetWithText(FilledButton, 'Confirm invite'), findsOneWidget);
  });

  testWidgets('prefilled invite can be explicitly dismissed without redeeming', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none, inviteCode: 'ELLA7K9Q');

    await tester.tap(find.byTooltip('Clear'));
    await tester.pump();

    expect(find.text('You’re on the list'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('successful redemption clears the pending fragment code', (tester) async {
    final links = EllaInviteLinkController.instance;
    links.accept(Uri.parse('https://ella-ai-care.com/invite#c=ELLA-7K9Q'));
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none);

    expect(links.pendingCode, 'ELLA7K9Q');
    await tester.tap(find.widgetWithText(FilledButton, 'Confirm invite'));
    await tester.pumpAndSettle();

    expect(find.text('ONBOARDING READY'), findsOneWidget);
    expect(links.pendingCode, isEmpty);
  });

  testWidgets('active entitlement continues directly', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.active);
    expect(find.text('ONBOARDING READY'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('capacity waitlist can still open invite entry', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none, inviteError: EllaInviteRedemptionError.capacity);

    await tester.tap(find.text('Enter an invite code'));
    await tester.pump();

    expect(find.byType(TextField), findsOneWidget);
    expect(find.textContaining('welcoming a few people at a time'), findsOneWidget);
  });

  testWidgets('every invite failure uses person-respecting informational copy', (tester) async {
    final cases = {
      EllaInviteRedemptionError.invalid: 'The code we received does not match an invite.',
      EllaInviteRedemptionError.expired: 'This invitation has reached its end date.',
      EllaInviteRedemptionError.capacity: 'Ella is welcoming a few people at a time.',
      EllaInviteRedemptionError.rateLimited: 'Let’s pause for a moment',
    };

    for (final entry in cases.entries) {
      await pumpGate(
        tester,
        entitlement: EllaAccessDemoFixtures.none,
        inviteError: entry.key,
        retryAfterSeconds: entry.key == EllaInviteRedemptionError.rateLimited ? 75 : null,
      );
      expect(find.textContaining(entry.value), findsOneWidget, reason: entry.key.name);
      if (entry.key == EllaInviteRedemptionError.rateLimited) {
        expect(find.textContaining('75 seconds'), findsOneWidget);
      }
      expect(find.byIcon(Icons.error_outline), findsNothing, reason: entry.key.name);
    }
  });

  testWidgets('revoked or expired entitlement shows safe support recovery instead of invite entry', (tester) async {
    final revoked = EllaEntitlement(
      status: EllaEntitlementStatus.revoked,
      quota: EllaAccessDemoFixtures.quota(),
      supportCode: 'SUP-4F2A',
    );
    await pumpGate(tester, entitlement: revoked);

    expect(find.text('Your invitation needs a quick check'), findsOneWidget);
    expect(find.text('SUP-4F2A'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('temporary service failure is not described as the person\'s connection failing', (tester) async {
    final provider = EllaEntitlementProvider(
      transport: const _UnavailableEntitlementTransport(),
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    addTearDown(provider.dispose);
    await provider.load();

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            startOnMount: false,
            onSignOutOverride: _noop,
            readyChild: Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Ella’s service needs a moment'), findsOneWidget);
    expect(find.textContaining('Your account and memories are safe'), findsOneWidget);
    expect(find.text('ELLA-ACCESS-RETRY'), findsOneWidget);
  });

  testWidgets('revoked recovery requires a fresh explicit consent grant before retrying entitlement', (tester) async {
    final transport = _RecoveryEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    addTearDown(provider.dispose);
    await provider.load();
    final grantedUids = <String>[];

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            startOnMount: false,
            onSignOutOverride: _noop,
            consentGrantRequester: (uid) async {
              grantedUids.add(uid);
              return const AiConsentGrantOutcome.accepted('aicr-fresh');
            },
            readyChild: const Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pump();

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await tester.pumpAndSettle();
    expect(find.text('Allow and continue'), findsOneWidget);
    expect(transport.fetchCalls, 1);

    await tester.tap(find.widgetWithText(FilledButton, 'Allow and continue'));
    await tester.pumpAndSettle();

    expect(grantedUids, ['uid-a']);
    expect(transport.fetchCalls, 2);
    expect(find.text('ONBOARDING READY'), findsOneWidget);
  });

  testWidgets('revoked recovery stays closed when consent is declined', (tester) async {
    final transport = _RecoveryEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    addTearDown(provider.dispose);
    await provider.load();
    var declineCalls = 0;

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            startOnMount: false,
            onSignOutOverride: _noop,
            consentGrantRequester: (_) async => const AiConsentGrantOutcome.accepted('unexpected'),
            consentDeclineRequester: (_) async {
              declineCalls++;
              return true;
            },
            readyChild: const Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pump();

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();

    expect(declineCalls, 1);
    expect(transport.fetchCalls, 1);
    expect(find.text('ONBOARDING READY'), findsNothing);
  });

  testWidgets('account drift during revoked recovery cannot retry under the replacement account', (tester) async {
    final transport = _RecoveryEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    addTearDown(provider.dispose);
    await provider.load();
    final grant = Completer<AiConsentGrantOutcome>();

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            startOnMount: false,
            onSignOutOverride: _noop,
            consentGrantRequester: (_) => grant.future,
            readyChild: const Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pump();

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Allow and continue'));
    await tester.pump();
    provider.bindAuthenticatedUid('uid-b');
    grant.complete(const AiConsentGrantOutcome.accepted('stale-receipt'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));

    expect(transport.fetchCalls, 1);
    expect(find.text('ONBOARDING READY'), findsNothing);
  });

  testWidgets('non-English internal pilot performs no claim request and exposes no English-only claim UI', (
    tester,
  ) async {
    final transport = _CountingEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'uid-a',
    );
    addTearDown(provider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          locale: Locale('es'),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaEntitlementGatePage(
            pilotLocaleRestricted: true,
            onSignOutOverride: _noop,
            readyChild: Text('ONBOARDING READY'),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(transport.fetchCalls, 0);
    expect(transport.redeemCalls, 0);
    expect(find.text('Seleccionar idioma'), findsOneWidget);
    expect(find.text('¡Esta función estará disponible pronto!'), findsOneWidget);
    expect(find.text('ONBOARDING READY'), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(find.text('Allow and continue'), findsNothing);
  });
}

void _noop() {}

class _CountingEntitlementTransport implements EllaEntitlementTransport {
  int fetchCalls = 0;
  int redeemCalls = 0;

  @override
  Future<EllaEntitlement> fetch() async {
    fetchCalls++;
    return EllaAccessDemoFixtures.invited;
  }

  @override
  Future<EllaEntitlement> redeem(String code) async {
    redeemCalls++;
    return EllaAccessDemoFixtures.active;
  }
}

class _RecoveryEntitlementTransport implements EllaEntitlementTransport {
  int fetchCalls = 0;

  @override
  Future<EllaEntitlement> fetch() async {
    fetchCalls++;
    return fetchCalls == 1
        ? EllaEntitlement(
            status: EllaEntitlementStatus.revoked,
            quota: EllaAccessDemoFixtures.quota(),
            supportCode: 'SUP-RECOVERY',
          )
        : EllaAccessDemoFixtures.active;
  }

  @override
  Future<EllaEntitlement> redeem(String code) => throw UnimplementedError();
}

class _UnavailableEntitlementTransport implements EllaEntitlementTransport {
  const _UnavailableEntitlementTransport();

  @override
  Future<EllaEntitlement> fetch() => Future.error(
        const EllaEntitlementRequestException(EllaEntitlementFailureKind.unavailable, supportCode: 'ELLA-ACCESS-RETRY'),
      );

  @override
  Future<EllaEntitlement> redeem(String code) => throw UnimplementedError();
}
