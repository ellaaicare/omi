import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';

void main() {
  testWidgets(
    'Ella empty memory summary is readable and exposes no legacy template picker',
    (tester) async {
      final provider = ConversationDetailProvider()
        ..setCachedConversation(
          ServerConversation(
            id: 'memory-without-summary',
            createdAt: DateTime(2026, 8, 10),
            structured: Structured('', ''),
          ),
        );
      addTearDown(provider.dispose);

      await tester.pumpWidget(
        ChangeNotifierProvider<ConversationDetailProvider>.value(
          value: provider,
          child: MaterialApp(
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: const Scaffold(body: GetAppsWidgets()),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('conversation-summary-empty')),
        findsOneWidget,
      );
      expect(
        find.textContaining("Ella hasn't created a summary"),
        findsOneWidget,
      );
      expect(find.text('Generate Summary'), findsNothing);
      expect(find.text('Summary Template'), findsNothing);

      final description = tester.widget<Text>(
        find.textContaining("Ella hasn't created a summary"),
      );
      expect(description.style?.color, EllaColors.inkSoft);
    },
  );
}
