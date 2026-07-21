import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/conversation_provider.dart';

ServerConversation _conversation(
  ConversationStatus status, {
  String overview = '',
  Map<String, dynamic>? enrichmentState,
}) {
  return ServerConversation(
    id: 'conversation-1',
    createdAt: DateTime.utc(2026, 7, 20, 8),
    structured: Structured('Conversation', overview),
    status: status,
    enrichmentState: enrichmentState,
    processingError: status == ConversationStatus.failed ? 'conversation_summary_failed' : null,
  );
}

ConversationProcessingRetryResult _result(
  ConversationProcessingRetryOutcome outcome,
  ServerConversation conversation, {
  String? phase,
}) {
  return ConversationProcessingRetryResult(
    outcome: outcome,
    recoveryMode: ConversationProcessingRecoveryMode.enrichmentOnly,
    phase: phase,
    genericStatus: 'completed',
    genericVectorStatus: 'completed',
    enrichmentStatus: outcome == ConversationProcessingRetryOutcome.completed ? 'completed' : 'pending',
    vectorStatus: outcome == ConversationProcessingRetryOutcome.completed ? 'completed' : 'pending',
    attemptCount: 1,
    conversation: conversation,
  );
}

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('parses the complete processing retry receipt', () {
    final result = ConversationProcessingRetryResult.fromJson({
      'outcome': 'processing',
      'recovery_mode': 'enrichment_only',
      'phase': 'generic_completed',
      'generic_status': 'completed',
      'generic_vector_status': 'completed',
      'enrichment_status': 'pending',
      'vector_status': 'pending',
      'lease_expires_at': '2026-07-20T08:15:00Z',
      'attempt_count': 2,
      'conversation': {
        'id': 'conversation-1',
        'created_at': '2026-07-20T08:00:00Z',
        'structured': {
          'title': 'Generic',
          'overview': 'Generic summary',
          'emoji': '',
          'category': 'other',
        },
        'status': 'completed',
      },
    });

    expect(result.outcome, ConversationProcessingRetryOutcome.processing);
    expect(result.recoveryMode, ConversationProcessingRecoveryMode.enrichmentOnly);
    expect(result.phase, 'generic_completed');
    expect(result.genericStatus, 'completed');
    expect(result.genericVectorStatus, 'completed');
    expect(result.enrichmentStatus, 'pending');
    expect(result.vectorStatus, 'pending');
    expect(result.leaseExpiresAt, isNotNull);
    expect(result.attemptCount, 2);
    expect(result.conversation.status, ConversationStatus.completed);
    expect(result.isTerminal, isFalse);
  });

  testWidgets('enrichment-only retry keeps generic summary visible until Hermes completes', (tester) async {
    final failedEnrichment = _conversation(
      ConversationStatus.completed,
      overview: 'Generic summary',
      enrichmentState: {'status': 'failed', 'pending': true},
    );
    final generic = _conversation(ConversationStatus.completed, overview: 'Generic summary');
    final enriched = _conversation(ConversationStatus.completed, overview: 'Ella enriched summary');
    final conversationIds = <String>[];
    final requestIds = <String>[];
    final contexts = <String?>[];

    final provider = ConversationProvider(
      retryConversationProcessingCall: (conversationId, requestId, {correctionText}) async {
        conversationIds.add(conversationId);
        requestIds.add(requestId);
        contexts.add(correctionText);
        return requestIds.length == 1
            ? _result(ConversationProcessingRetryOutcome.processing, generic, phase: 'generic_completed')
            : _result(ConversationProcessingRetryOutcome.completed, enriched, phase: 'completed');
      },
    );
    provider.failedConversations = [failedEnrichment];
    provider.conversations = [failedEnrichment];

    expect(
      await provider.retryFailedConversation(
        failedEnrichment.id,
        correctionText: 'The speakers were discussing their summer plans.',
      ),
      isTrue,
    );
    expect(provider.failedConversations, isEmpty);
    expect(provider.conversations.single.structured.overview, 'Generic summary');
    expect(provider.isConversationRetrying(failedEnrichment.id), isTrue);
    expect(await provider.retryFailedConversation(failedEnrichment.id), isTrue);
    expect(requestIds, hasLength(1));

    await tester.pump(const Duration(seconds: 3));
    await tester.pump();

    expect(requestIds, hasLength(2));
    expect(conversationIds, everyElement(failedEnrichment.id));
    expect(requestIds.toSet(), hasLength(1));
    expect(contexts, everyElement('The speakers were discussing their summer plans.'));
    expect(provider.failedConversations, isEmpty);
    expect(provider.conversations.single.structured.overview, 'Ella enriched summary');
    expect(provider.isConversationRetrying(failedEnrichment.id), isFalse);

    provider.dispose();
  });

  testWidgets('terminal enrichment failure stays visible and a user retry gets a fresh id', (tester) async {
    final generic = _conversation(ConversationStatus.completed, overview: 'Generic summary');
    final failedEnrichment = _conversation(
      ConversationStatus.completed,
      overview: 'Generic summary',
      enrichmentState: {'status': 'failed', 'pending': true},
    );
    final requestIds = <String>[];

    final provider = ConversationProvider(
      retryConversationProcessingCall: (_, requestId, {correctionText}) async {
        requestIds.add(requestId);
        if (requestIds.length == 1) {
          return _result(ConversationProcessingRetryOutcome.processing, generic);
        }
        return _result(ConversationProcessingRetryOutcome.failed, failedEnrichment, phase: 'failed');
      },
    );
    provider.failedConversations = [failedEnrichment];
    provider.conversations = [generic];

    expect(await provider.retryFailedConversation(generic.id), isTrue);
    await tester.pump(const Duration(seconds: 3));
    await tester.pump();

    expect(requestIds, hasLength(2));
    expect(requestIds[1], requestIds[0]);
    expect(provider.failedConversations.single.id, generic.id);
    expect(provider.conversations.single.structured.overview, 'Generic summary');
    expect(provider.isConversationRetrying(generic.id), isFalse);

    expect(await provider.retryFailedConversation(generic.id), isTrue);
    expect(requestIds, hasLength(3));
    expect(requestIds[2], isNot(requestIds[0]));
    expect(provider.failedConversations.single.id, generic.id);

    provider.dispose();
  });

  testWidgets('slow processing poll never overlaps another request', (tester) async {
    final processing = _conversation(ConversationStatus.processing);
    final completed = _conversation(ConversationStatus.completed, overview: 'Recovered summary');
    final firstPoll = Completer<ConversationProcessingRetryResult?>();
    var calls = 0;
    var inFlight = 0;
    var maxInFlight = 0;

    final provider = ConversationProvider(
      retryConversationProcessingCall: (_, __, {correctionText}) async {
        calls += 1;
        if (calls == 1) return _result(ConversationProcessingRetryOutcome.processing, processing);
        inFlight += 1;
        if (inFlight > maxInFlight) maxInFlight = inFlight;
        if (calls == 2) {
          final result = await firstPoll.future;
          inFlight -= 1;
          return result;
        }
        inFlight -= 1;
        return _result(ConversationProcessingRetryOutcome.completed, completed);
      },
    );

    expect(await provider.retryFailedConversation(processing.id), isTrue);
    await tester.pump(const Duration(seconds: 3));
    expect(calls, 2);
    expect(inFlight, 1);

    await tester.pump(const Duration(seconds: 12));
    expect(calls, 2);
    expect(maxInFlight, 1);

    firstPoll.complete(_result(ConversationProcessingRetryOutcome.processing, processing));
    await tester.pump();
    await tester.pump(const Duration(seconds: 3));
    await tester.pump();

    expect(calls, 3);
    expect(maxInFlight, 1);
    expect(provider.conversations.single.status, ConversationStatus.completed);
    expect(provider.isConversationRetrying(processing.id), isFalse);

    provider.dispose();
  });

  testWidgets('processing poll stops after forty completed attempts', (tester) async {
    final processing = _conversation(ConversationStatus.processing);
    var calls = 0;
    final provider = ConversationProvider(
      retryConversationProcessingCall: (_, __, {correctionText}) async {
        calls += 1;
        return calls == 1 ? _result(ConversationProcessingRetryOutcome.processing, processing) : null;
      },
    );

    expect(await provider.retryFailedConversation(processing.id), isTrue);
    for (var attempt = 0; attempt < 40; attempt += 1) {
      await tester.pump(const Duration(seconds: 3));
      await tester.pump();
    }

    expect(calls, 41);
    expect(provider.isConversationRetrying(processing.id), isFalse);
    await tester.pump(const Duration(seconds: 30));
    expect(calls, 41);

    provider.dispose();
  });

  testWidgets('completion after provider disposal cannot restart or update polling', (tester) async {
    final processing = _conversation(ConversationStatus.processing);
    final completed = _conversation(ConversationStatus.completed);
    final latePoll = Completer<ConversationProcessingRetryResult?>();
    var calls = 0;
    final provider = ConversationProvider(
      retryConversationProcessingCall: (_, __, {correctionText}) async {
        calls += 1;
        if (calls == 1) return _result(ConversationProcessingRetryOutcome.processing, processing);
        return await latePoll.future;
      },
    );

    expect(await provider.retryFailedConversation(processing.id), isTrue);
    await tester.pump(const Duration(seconds: 3));
    expect(calls, 2);

    provider.dispose();
    latePoll.complete(_result(ConversationProcessingRetryOutcome.completed, completed));
    await tester.pump();
    await tester.pump(const Duration(seconds: 30));

    expect(calls, 2);
  });
}
