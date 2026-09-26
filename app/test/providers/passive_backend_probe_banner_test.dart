import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/services/connectivity_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  final service = ConnectivityService();

  tearDown(() {
    service.applyHealthProbeForTest(statusCode: 200);
  });

  testWidgets('unreachable probes show localized copy and hide internal tokens', (tester) async {
    final cases = <void Function()>[
      () => service.applyHealthProbeForTest(error: TimeoutException('probe')),
      () => service.applyHealthProbeForTest(statusCode: 503),
      () => service.applyHealthProbeForTest(error: StateError('socket failed')),
      service.applyMissingInterfaceProbeForTest,
    ];

    for (final arrange in cases) {
      arrange();
      final provider = ConnectivityProvider();
      addTearDown(provider.dispose);
      await tester.pumpWidget(
        ChangeNotifierProvider<ConnectivityProvider>.value(
          value: provider,
          child: const MaterialApp(
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: Scaffold(body: PassiveBackendProbeBanner()),
          ),
        ),
      );

      final l10n = AppLocalizations.of(tester.element(find.byType(PassiveBackendProbeBanner)));
      expect(find.text(l10n.ellaServerUnreachableBanner), findsOneWidget);
      expect(find.byKey(const Key('passive-backend-probe-banner')), findsOneWidget);
      final rendered = tester.widget<Text>(find.byKey(const Key('passive-backend-probe-banner'))).data!;
      for (final forbidden in [
        'timeout',
        'http_503',
        'http_',
        'no_network_interface',
        'TimeoutException',
        'StateError',
        'Backend health probe',
        'api.ella-ai-care.com',
        'Exception',
      ]) {
        expect(rendered.contains(forbidden), isFalse, reason: 'banner exposed $forbidden');
      }
    }

    service.applyHealthProbeForTest(statusCode: 200);
    final provider = ConnectivityProvider();
    addTearDown(provider.dispose);
    await tester.pumpWidget(
      ChangeNotifierProvider<ConnectivityProvider>.value(
        value: provider,
        child: const MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: PassiveBackendProbeBanner()),
        ),
      ),
    );
    expect(find.byKey(const Key('passive-backend-probe-banner')), findsNothing);
  });
}
