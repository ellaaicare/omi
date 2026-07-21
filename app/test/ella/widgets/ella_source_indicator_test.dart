import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/widgets/ella_source_indicator.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  Widget buildTestApp(String value) {
    return MaterialApp(
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: EllaSourceText(value)),
    );
  }

  testWidgets('shows an Ella source indicator only for tagged content', (tester) async {
    await tester.pumpWidget(buildTestApp('🪽 [Ella] Family lunch'));
    expect(find.byIcon(Icons.auto_awesome_rounded), findsOneWidget);
    expect(find.textContaining('[Ella]'), findsNothing);
    expect(find.textContaining('Family lunch'), findsOneWidget);

    await tester.pumpWidget(buildTestApp('Generic summary'));
    expect(find.byIcon(Icons.auto_awesome_rounded), findsNothing);
  });
}
