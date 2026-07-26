import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/pages/ella_access_demo_gallery_page.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  testWidgets('Demo Mode gallery exposes every new access and voice state', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: EllaAccessDemoGalleryPage(),
      ),
    );

    expect(EllaAccessDemoScenario.values, hasLength(19));
    expect(find.byType(ListTile), findsWidgets);
    const labels = [
      'Waitlist — no entitlement',
      'Invite — code entry',
      'Invite — link or QR prefilled',
      'Entitled — continue',
      'Invite — code not recognized',
      'Invite — end date reached',
      'Invite — capacity reached',
      'Invite — retry pause',
      'Entitlement — suspended',
      'Entitlement — revoked',
      'Entitlement — expired',
      'Setup — taking longer',
      'Voice — gentle 80% warning',
      'Voice — daily rest',
      'Voice — monthly reset',
      'Voice — another conversation active',
      'Voice — access paused',
      'Voice — conversation-time limit',
      'Voice — technical connection issue',
    ];
    for (final label in labels) {
      for (var attempt = 0; find.text(label).evaluate().isEmpty && attempt < 8; attempt++) {
        await tester.drag(find.byType(ListView), const Offset(0, -180));
        await tester.pump();
      }
      expect(find.text(label), findsOneWidget);
    }
  });
}
