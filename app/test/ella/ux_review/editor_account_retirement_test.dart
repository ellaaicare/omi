import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/widgets/ella_v7_companion_card.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'thick_review_regressions_test.dart' as review;

void main() {
  review.main();
  for (final returnToOriginal in [false, true]) {
    testWidgets('account editor retires without exposing old draft ABA=$returnToOriginal', (tester) async {
      final prefs = SharedPreferencesUtil();
      prefs.uid = 'fictional-a';
      await tester.pumpWidget(
          review.app(const Scaffold(body: SingleChildScrollView(child: EllaV7CompanionCard())), dark: true));
      await tester.tap(find.byKey(const Key('v7-companion-customize')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('v7-companion-name')), 'Fictional retired draft');
      prefs.uid = 'fictional-b';
      if (returnToOriginal) prefs.uid = 'fictional-a';
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('v7-companion-name')), findsNothing);
      final save = tester.widget<FilledButton>(find.byKey(const Key('v7-companion-save')));
      expect(save.onPressed, isNull);
      expect(EllaV7CompanionPreferences.shared.name, isEmpty);
      await tester.tap(find.text('Close'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('v7-companion-save')), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }
}
