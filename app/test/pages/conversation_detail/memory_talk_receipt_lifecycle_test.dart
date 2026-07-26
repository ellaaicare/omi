import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';
import 'package:omi/ella/widgets/memory_correction_receipt.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';

const conversationId = 'conversation-1';
const sessionId = 'session-1';
const correctionId = 'correction-1';

ServerConversation memoryConversation() => ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 7, 24),
      structured: Structured('A seeded memory', 'The selected memory overview'),
    );

ConversationReinterpretationJob appliedJob() => const ConversationReinterpretationJob(
      jobId: 'job-1',
      sessionId: sessionId,
      conversationId: conversationId,
      status: 'applied',
      outcome: 'applied',
      correctionIds: [correctionId],
      receipts: [
        ConversationReinterpretationReceiptReference(
          conversationId: conversationId,
          correctionId: correctionId,
          status: 'applied',
        ),
      ],
    );

ConversationCorrectionReceipt appliedReceipt() => const ConversationCorrectionReceipt(
      correctionId: correctionId,
      conversationId: conversationId,
      status: 'applied',
      before: ConversationCorrectionSummary(title: 'Before'),
      after: ConversationCorrectionSummary(title: 'After'),
    );

class _FakeMemoryVoiceSheet extends StatefulWidget {
  const _FakeMemoryVoiceSheet({required this.onSessionEnded});

  final ValueChanged<MemoryReceiptDiscoveryRequest> onSessionEnded;

  @override
  State<_FakeMemoryVoiceSheet> createState() => _FakeMemoryVoiceSheetState();
}

class _FakeMemoryVoiceSheetState extends State<_FakeMemoryVoiceSheet> {
  @override
  void dispose() {
    widget.onSessionEnded(
      const MemoryReceiptDiscoveryRequest(conversationId: conversationId, sessionId: sessionId),
    );
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ElevatedButton(
          key: const ValueKey('end-memory-session'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('End session'),
        ),
      ),
    );
  }
}

Widget app({
  required MemoryReinterpretationReceiptDiscovery discovery,
}) {
  return MaterialApp(
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(
      body: MemoryTalkButton(
        conversation: memoryConversation(),
        receiptDiscovery: discovery,
        routeOpener: (context, _, onSessionEnded) => showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          isDismissible: false,
          enableDrag: false,
          builder: (_) => FractionallySizedBox(
            heightFactor: 0.94,
            child: _FakeMemoryVoiceSheet(onSessionEnded: onSessionEnded),
          ),
        ),
      ),
    ),
  );
}

void main() {
  test('memory modal keeps the exact conversation and summary version scope', () {
    final conversation = ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 7, 24),
      structured: Structured('A seeded memory', 'The selected memory overview'),
      activeSummaryVersionId: 'summary-v4',
    );

    final scope = MemoryTalkButton.sessionScopeFor(conversation);

    expect(scope.conversationId, conversationId);
    expect(scope.expectedActiveSummaryVersionId, 'summary-v4');
  });

  testWidgets('discovers a delayed receipt after the memory voice sheet closes', (tester) async {
    final delayedJob = Completer<ConversationReinterpretationJob?>();
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) => delayedJob.future,
      fetchReceipt: (_, __) async => appliedReceipt(),
      wait: (_) async {},
      maxAttempts: 1,
    );

    await tester.pumpWidget(app(discovery: discovery));
    await tester.tap(find.byKey(const ValueKey('memory-talk-$conversationId')));
    await tester.pumpAndSettle();

    expect(find.byType(_FakeMemoryVoiceSheet), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('end-memory-session')));
    await tester.pumpAndSettle();

    expect(find.byType(_FakeMemoryVoiceSheet), findsNothing);
    expect(find.byType(MemoryCorrectionReceiptChip), findsNothing);

    delayedJob.complete(appliedJob());
    await tester.pump();
    await tester.pumpAndSettle();

    expect(find.byType(MemoryCorrectionReceiptChip), findsOneWidget);
    expect(find.text('Memory updated'), findsOneWidget);
    expect(find.text('Review'), findsOneWidget);
  });
}
