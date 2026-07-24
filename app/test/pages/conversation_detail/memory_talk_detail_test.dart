import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/pages/conversation_detail/widgets/memory_talk_detail.dart';

void main() {
  ServerConversation conversation() => ServerConversation(
        id: 'garden',
        createdAt: DateTime(2026, 7, 23, 9, 40),
        structured: Structured(
          'Coffee in the garden',
          'Rose joined you for coffee in the garden.',
        ),
        appResults: [AppResponse('Invisible legacy summary phrase', appId: 'legacy-summary')],
      );

  Widget app(ServerConversation memory, {required String searchQuery, required int currentResultIndex}) {
    return MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(
        body: MemoryTalkDetail(
          conversation: memory,
          receipt: null,
          hasDiscussion: false,
          isTalkSheetOpen: false,
          searchQuery: searchQuery,
          currentResultIndex: currentResultIndex,
          onUndo: () async {},
        ),
      ),
    );
  }

  testWidgets('summary search ignores hidden legacy content and highlights the rendered memory detail', (tester) async {
    final memory = conversation();

    expect(countMemoryTalkSearchMatches(memory, 'legacy summary'), 0);
    expect(countMemoryTalkSearchMatches(memory, 'rose'), 1);
    expect(countMemoryTalkSearchMatches(memory, 'garden'), 2);

    await tester.pumpWidget(app(memory, searchQuery: 'rose', currentResultIndex: 0));

    final overview = tester.widget<Text>(find.byKey(const ValueKey('memory-talk-overview')));
    final spans = (overview.textSpan! as TextSpan).children!.cast<TextSpan>();
    final highlighted = spans.singleWhere((span) => span.text?.toLowerCase() == 'rose');

    expect(highlighted.style?.backgroundColor, isNotNull);
    expect(highlighted.style?.fontWeight, FontWeight.bold);
    expect(find.textContaining('Invisible legacy summary phrase', findRichText: true), findsNothing);
  });

  test('copy summary writes rendered content and ignores conflicting legacy content', () async {
    String? copiedText;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') {
          copiedText = (call.arguments as Map<Object?, Object?>)['text'] as String?;
        }
        return null;
      },
    );
    addTearDown(
      () => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        null,
      ),
    );

    await copyRenderedMemorySummary(conversation());

    expect(copiedText, 'Coffee in the garden\n\nRose joined you for coffee in the garden.');
    expect(copiedText, isNot(contains('Invisible legacy summary phrase')));
  });
}
