import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets/correct_summary_sheet.dart';

// The correction sheet's placeholder text used to hard-code a real account
// holder's name. It must now come from the localization layer and contain
// only generic, fictional wording. These tests exercise the actual rendered
// widget rather than regexing the Dart source, so a future regression that
// reintroduces a literal string in the widget (bypassing l10n entirely)
// would still be caught: the rendered hint would then no longer match what
// the localization layer reports for the same BuildContext.
const _expectedFictionalHint = 'Example: This was Avery and Jordan discussing weekend plans, not a work meeting.';

void main() {
  ServerConversation buildConversation() {
    return ServerConversation(
      id: 'conv-correction-1',
      createdAt: DateTime.parse('2026-04-23T12:00:00Z').toLocal(),
      structured: Structured('Evening chat', 'A chat happened.'),
    );
  }

  group('CorrectSummarySheet', () {
    testWidgets('correction text field hint is the localized generic example', (tester) async {
      final conversation = buildConversation();
      String? localizedHint;

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Builder(
            builder: (context) {
              localizedHint = AppLocalizations.of(context).correctionSheetHintExample;
              return Scaffold(
                body: CorrectSummarySheet(conversation: conversation, appSummary: 'A chat happened.'),
              );
            },
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(localizedHint, _expectedFictionalHint);

      final textField = tester.widget<TextField>(find.byType(TextField));
      expect(textField.decoration?.hintText, localizedHint);
    });

    testWidgets('tapping Correct Summary opens the sheet showing the localized hint', (tester) async {
      final conversation = buildConversation();
      String? localizedHint;

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Builder(
            builder: (context) {
              localizedHint = AppLocalizations.of(context).correctionSheetHintExample;
              return Scaffold(
                body: CorrectSummaryButton(conversation: conversation, appSummary: 'A chat happened.'),
              );
            },
          ),
        ),
      );

      await tester.tap(find.text('Correct Summary'));
      await tester.pumpAndSettle();

      final textField = tester.widget<TextField>(find.byType(TextField));
      expect(textField.decoration?.hintText, isNotNull);
      expect(textField.decoration?.hintText, localizedHint);
    });
  });
}
