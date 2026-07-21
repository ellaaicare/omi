import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

void main() {
  testWidgets('every HomePageWrapper entry remains gated and preserves pending navigation', (tester) async {
    final provider = EllaProvisioningProvider();
    addTearDown(provider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: HomePageWrapper(
            navigateToRoute: '/chat',
            autoMessage: 'pending notification message',
            provisioningGateStartOnMount: false,
          ),
        ),
      ),
    );

    final gate = tester.widget<EllaProvisioningGatePage>(find.byType(EllaProvisioningGatePage));
    final readyHome = gate.readyChild as HomePageWrapper;

    expect(find.byType(HomePage), findsNothing);
    expect(readyHome.navigateToRoute, '/chat');
    expect(readyHome.autoMessage, 'pending notification message');
    expect(tester.takeException(), isNull);
  });
}
