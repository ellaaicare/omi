import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
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

  testWidgets('non-English internal pilot performs no claim request and exposes no English-only claim UI',
      (tester) async {
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
