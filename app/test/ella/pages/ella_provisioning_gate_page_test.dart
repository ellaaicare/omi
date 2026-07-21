import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/pages/ella_provisioning_gate_page.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

void main() {
  testWidgets('setup failure remains fail closed and exposes its support code', (tester) async {
    final provider = EllaProvisioningProvider()
      ..state = EllaProvisioningState.blocked
      ..receipt = const EllaProvisioningReceipt(
        state: EllaProvisioningState.blocked,
        supportCode: 'ELLA-SUPPORT-301',
        errorCode: 'provisioning_disabled',
      );
    addTearDown(provider.dispose);

    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: EllaProvisioningGatePage(startOnMount: false),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('ELLA-SUPPORT-301'), findsOneWidget);
    expect(find.byType(HomePageWrapper), findsNothing);
    expect(find.byIcon(Icons.lock_outline_rounded), findsOneWidget);
  });
}
