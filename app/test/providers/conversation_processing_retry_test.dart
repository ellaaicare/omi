import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/conversation_provider.dart';

ServerConversation _conversation(ConversationStatus status) {
  return ServerConversation(
    id: 'conversation-1',
    createdAt: DateTime.utc(2026, 7, 20, 8),
    structured: Structured(status == ConversationStatus.completed ? 'Recovered summary' : '', ''),
    status: status,
    processingError: status == ConversationStatus.failed ? 'conversation_summary_failed' : null,
  );
}

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  testWidgets('retry moves failed conversation through processing to completed', (tester) async {
    final failed = _conversation(ConversationStatus.failed);
    final processing = _conversation(ConversationStatus.processing);
    final completed = _conversation(ConversationStatus.completed);
    var retryCalls = 0;
    var pollCalls = 0;

    final provider = ConversationProvider(
      retryConversationProcessingCall: (conversationId, requestId, {correctionText}) async {
        retryCalls += 1;
        expect(conversationId, failed.id);
        expect(requestId, isNotEmpty);
        expect(correctionText, 'The speakers were discussing their summer plans.');
        return ConversationProcessingRetryResult(
          outcome: ConversationProcessingRetryOutcome.processing,
          conversation: processing,
        );
      },
      conversationByIdCall: (conversationId) async {
        pollCalls += 1;
        return completed;
      },
    );
    provider.failedConversations = [failed];
    provider.conversations = [failed];

    expect(
      await provider.retryFailedConversation(
        failed.id,
        correctionText: 'The speakers were discussing their summer plans.',
      ),
      isTrue,
    );
    expect(retryCalls, 1);
    expect(provider.failedConversations, isEmpty);
    expect(provider.processingConversations.single.status, ConversationStatus.processing);
    expect(provider.isConversationRetrying(failed.id), isTrue);
    expect(await provider.retryFailedConversation(failed.id), isTrue);
    expect(retryCalls, 1);

    await tester.pump(const Duration(seconds: 3));
    await tester.pump();

    expect(pollCalls, 1);
    expect(provider.processingConversations, isEmpty);
    expect(provider.conversations.single.status, ConversationStatus.completed);
    expect(provider.isConversationRetrying(failed.id), isFalse);

    provider.dispose();
  });

  testWidgets('failed retry response remains visible and retryable', (tester) async {
    final failed = _conversation(ConversationStatus.failed);
    final provider = ConversationProvider(
      retryConversationProcessingCall: (_, __, {correctionText}) async {
        expect(correctionText, isNull);
        return ConversationProcessingRetryResult(
          outcome: ConversationProcessingRetryOutcome.failed,
          conversation: failed,
        );
      },
    );
    provider.failedConversations = [failed];

    expect(await provider.retryFailedConversation(failed.id), isTrue);
    expect(provider.failedConversations.single.id, failed.id);
    expect(provider.isConversationRetrying(failed.id), isFalse);

    provider.dispose();
  });
}
