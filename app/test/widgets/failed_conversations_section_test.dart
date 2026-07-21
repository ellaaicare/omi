import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversations/widgets/failed_conversations_section.dart';

ServerConversation _failedConversation() {
  return ServerConversation(
    id: 'failed-conversation',
    createdAt: DateTime.utc(2026, 7, 20, 8),
    structured: Structured('', ''),
    transcriptSegments: [
      TranscriptSegment(
        id: 'segment-1',
        text: 'The preserved transcript remains available while the summary is repaired.',
        speaker: 'SPEAKER_00',
        isUser: true,
        personId: null,
        start: 0,
        end: 5,
        translations: [],
      ),
    ],
    status: ConversationStatus.failed,
    processingError: 'provider.invalid_api_key secret detail',
    processingErrorAt: DateTime.utc(2026, 7, 20, 8, 5),
  );
}

Widget _app({required bool retrying, required RetryFailedConversation onRetry}) {
  return MaterialApp(
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(
      body: FailedConversationsSection(
        conversations: [_failedConversation()],
        isRetrying: (_) => retrying,
        onRetry: onRetry,
      ),
    ),
  );
}

void main() {
  testWidgets('shows safe copy and preserved transcript without provider internals', (tester) async {
    await tester.pumpWidget(_app(retrying: false, onRetry: (_, {correctionText}) async => true));
    await tester.pumpAndSettle();

    expect(find.text('Needs processing'), findsNWidgets(2));
    expect(find.text('Your transcript is safe. Ask Ella to create its summary.'), findsOneWidget);
    expect(find.textContaining('The preserved transcript remains available'), findsOneWidget);
    expect(find.textContaining('invalid_api_key'), findsNothing);
  });

  testWidgets('retries the selected conversation once with optional Ella context', (tester) async {
    var calls = 0;
    String? retriedId;
    String? submittedContext;
    await tester.pumpWidget(
      _app(
        retrying: false,
        onRetry: (conversationId, {correctionText}) async {
          calls += 1;
          retriedId = conversationId;
          submittedContext = correctionText;
          return true;
        },
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('retry-conversation-failed-conversation')));
    await tester.pumpAndSettle();

    expect(find.text('Retry with Ella'), findsNWidgets(2));
    await tester.enterText(
      find.byKey(const ValueKey('retry-context-failed-conversation')),
      'This was a family conversation about summer plans.',
    );
    await tester.tap(find.byKey(const ValueKey('submit-retry-failed-conversation')));
    await tester.pumpAndSettle();

    expect(calls, 1);
    expect(retriedId, 'failed-conversation');
    expect(submittedContext, 'This was a family conversation about summer plans.');
  });

  testWidgets('disables retry while processing is already restarting', (tester) async {
    await tester.pumpWidget(_app(retrying: true, onRetry: (_, {correctionText}) async => true));
    await tester.pumpAndSettle();

    expect(find.text('Processing...'), findsOneWidget);
    final button = tester.widget<FilledButton>(find.byKey(const ValueKey('retry-conversation-failed-conversation')));
    expect(button.onPressed, isNull);
  });
}
