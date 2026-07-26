import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_entitlement_gate_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';

void main() {
  Future<void> pumpGate(
    WidgetTester tester, {
    required EllaEntitlement entitlement,
    EllaInviteRedemptionError? inviteError,
    String inviteCode = '',
  }) async {
    final provider = EllaEntitlementProvider.demo(
      initialEntitlement: entitlement,
      initialInviteError: inviteError,
      initialInviteCode: inviteCode,
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

  testWidgets('no entitlement is a warm waitlist state without error styling', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.none);

    expect(find.text('You’re on the list'), findsOneWidget);
    expect(find.byIcon(Icons.favorite_outline_rounded), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsNothing);
    final icon = tester.widget<Icon>(find.byIcon(Icons.favorite_outline_rounded));
    expect(icon.color, isNot(Colors.red));
  });

  testWidgets('invited state has a large paste-friendly entry field', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.invited);

    expect(find.text('Use your Ella invite'), findsOneWidget);
    expect(find.text('Paste code'), findsOneWidget);
    final textField = tester.widget<TextField>(find.byType(TextField));
    expect(textField.keyboardType, TextInputType.visiblePassword);
    expect(textField.style?.fontSize, 22);
  });

  testWidgets('universal-link fixture is prefilled and needs one confirm tap', (tester) async {
    await pumpGate(
      tester,
      entitlement: EllaAccessDemoFixtures.invited,
      inviteCode: 'ELLA7K9Q',
    );

    expect(find.text('Your invite is ready'), findsOneWidget);
    expect(find.text('ELLA7K9Q'), findsNWidgets(2));
    expect(find.widgetWithText(FilledButton, 'Confirm invite'), findsOneWidget);
  });

  testWidgets('active entitlement continues directly', (tester) async {
    await pumpGate(tester, entitlement: EllaAccessDemoFixtures.active);
    expect(find.text('ONBOARDING READY'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('capacity waitlist can still open invite entry', (tester) async {
    await pumpGate(
      tester,
      entitlement: EllaAccessDemoFixtures.none,
      inviteError: EllaInviteRedemptionError.capacity,
    );

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
        entitlement: entry.key == EllaInviteRedemptionError.capacity
            ? EllaAccessDemoFixtures.none
            : EllaAccessDemoFixtures.invited,
        inviteError: entry.key,
      );
      expect(find.textContaining(entry.value), findsOneWidget, reason: entry.key.name);
      expect(find.byIcon(Icons.error_outline), findsNothing, reason: entry.key.name);
    }
  });
}
