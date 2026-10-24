import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_thinking_indicator.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  Future<AppLocalizations> mount(WidgetTester tester, {bool reducedMotion = false, double textScale = 1}) async {
    await tester.pumpWidget(MaterialApp(
      theme: ellaThemeData(),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: MediaQuery(
        data: MediaQueryData(disableAnimations: reducedMotion, textScaler: TextScaler.linear(textScale)),
        child: const Scaffold(body: SizedBox(width: 280, child: EllaThinkingIndicator())),
      ),
    ));
    return AppLocalizations.of(tester.element(find.byType(EllaThinkingIndicator)));
  }

  testWidgets('elapsed wait never promises completion progress', (tester) async {
    final l10n = await mount(tester);
    await tester.pump(const Duration(seconds: 29));
    expect(find.textContaining(l10n.ellaChatWaitingResponse), findsOneWidget);
    expect(find.text(l10n.ellaChatElapsedSeconds(29)), findsOneWidget);
    expect(find.textContaining('Almost there'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('reduced motion and large type keep pending status readable without pulse frames', (tester) async {
    final l10n = await mount(tester, reducedMotion: true, textScale: 3);
    await tester.pump(const Duration(milliseconds: 100));
    expect(tester.binding.hasScheduledFrame, isFalse);
    expect(find.textContaining(l10n.ellaChatWaitingResponse), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
