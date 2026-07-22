import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversations/widgets/ella_enriched_badge.dart';

ServerConversation _conversation(String id, {required String state, bool generic = false}) {
  return ServerConversation(
    id: id,
    createdAt: DateTime.utc(2026, 7, 21),
    structured: Structured('A long memory title that must remain on one line', 'Overview'),
    summaryVersions: [
      ConversationSummaryVersion(
        id: 'active-v2',
        source: generic ? 'omi' : 'hermes_parallel',
        kind: generic ? 'generic' : 'hermes_enriched',
        isActive: true,
      ),
    ],
    activeSummaryVersionId: 'active-v2',
    enrichmentState: {'status': state, 'pending': state == 'writeback_pending_canonical'},
  );
}

Widget _app(List<ServerConversation> conversations) {
  return MaterialApp(
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(
      body: SizedBox(
        width: 240,
        child: Column(
          children: [
            for (final conversation in conversations)
              ConversationTitleWithEllaBadge(
                conversation: conversation,
                title: conversation.structured.title,
                style: const TextStyle(fontSize: 16, height: 1.5),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
          ],
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('shows an accessible badge only for applied structured Ella enrichment', (tester) async {
    final conversations = [
      _conversation('generic', state: 'writeback_applied', generic: true),
      _conversation('pending', state: 'writeback_pending_canonical'),
      _conversation('failed', state: 'failed'),
      _conversation('applied', state: 'writeback_applied'),
    ];

    await tester.pumpWidget(_app(conversations));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('ella-enriched-badge-generic')), findsNothing);
    expect(find.byKey(const ValueKey('ella-enriched-badge-pending')), findsNothing);
    expect(find.byKey(const ValueKey('ella-enriched-badge-failed')), findsNothing);
    expect(find.byKey(const ValueKey('ella-enriched-badge-applied')), findsOneWidget);
    expect(find.bySemanticsLabel('Summary enriched by Ella'), findsOneWidget);
    expect(tester.takeException(), isNull);

    final heights = conversations
        .map((conversation) => tester.getSize(find.byKey(ValueKey('conversation-title-${conversation.id}'))).height)
        .toSet();
    expect(heights, hasLength(1));
  });
}
