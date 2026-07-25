import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';

const conversationId = 'conversation-1';
const sessionId = 'session-1';
const correctionId = 'correction-1';

ConversationReinterpretationJob job({
  String status = 'applied',
  String currentSessionId = sessionId,
  List<String> correctionIds = const [correctionId],
}) =>
    ConversationReinterpretationJob(
      jobId: 'job-1',
      sessionId: currentSessionId,
      conversationId: conversationId,
      status: status,
      correctionIds: correctionIds,
      receipts: correctionIds
          .map(
            (id) => ConversationReinterpretationReceiptReference(
              conversationId: conversationId,
              correctionId: id,
              status: 'applied',
            ),
          )
          .toList(),
    );

ConversationCorrectionReceipt appliedReceipt() => const ConversationCorrectionReceipt(
      correctionId: correctionId,
      conversationId: conversationId,
      status: 'applied',
      before: ConversationCorrectionSummary(title: 'Before'),
      after: ConversationCorrectionSummary(title: 'After'),
    );

void main() {
  test('parses identifier and status fields from latest reinterpretation response', () {
    final parsed = ConversationReinterpretationJob.tryParse({
      'job_id': 'job-1',
      'session_id': sessionId,
      'conversation_id': conversationId,
      'status': 'applied',
      'correction_ids': [correctionId],
      'receipts': [
        {'conversation_id': conversationId, 'correction_id': correctionId, 'status': 'applied'},
      ],
      'proposal_plan': {'private': 'ignored'},
    });

    expect(parsed?.sessionId, sessionId);
    expect(parsed?.appliedCorrectionId, correctionId);
  });

  test('ignores malformed optional correction collections', () {
    final parsed = ConversationReinterpretationJob.tryParse({
      'job_id': 'job-1',
      'session_id': sessionId,
      'conversation_id': conversationId,
      'status': 'no_change',
      'correction_ids': 'not-a-list',
      'receipts': {'not': 'a-list'},
    });

    expect(parsed?.correctionIds, isEmpty);
    expect(parsed?.receipts, isEmpty);
  });

  test('returns a matching authoritative applied receipt', () async {
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(),
      fetchReceipt: (_, __) async => appliedReceipt(),
      wait: (_) async {},
      maxAttempts: 1,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.applied);
    expect(result.receipt?.after.title, 'After');
  });

  test('stops without fetching a receipt for no-change', () async {
    var receiptFetches = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(status: 'no_change', correctionIds: const []),
      fetchReceipt: (_, __) async {
        receiptFetches++;
        return null;
      },
      wait: (_) async {},
      maxAttempts: 1,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.noChange);
    expect(receiptFetches, 0);
  });

  test('returns pending-review when no applied correction exists', () async {
    var receiptFetches = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(status: 'pending_review'),
      fetchReceipt: (_, __) async {
        receiptFetches++;
        return null;
      },
      wait: (_) async {},
      maxAttempts: 1,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.pendingReview);
    expect(result.receipt, isNull);
    expect(receiptFetches, 0);
  });

  test('times out after a bounded number of pending polls', () async {
    var polls = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async {
        polls++;
        return job(status: 'running', correctionIds: const []);
      },
      fetchReceipt: (_, __) async => null,
      wait: (_) async {},
      maxAttempts: 3,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.timeout);
    expect(polls, 3);
  });

  test('rejects a stale cross-session result without fetching its receipt', () async {
    var receiptFetches = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(currentSessionId: 'previous-session'),
      fetchReceipt: (_, __) async {
        receiptFetches++;
        return appliedReceipt();
      },
      wait: (_) async {},
      maxAttempts: 2,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.sessionMismatch);
    expect(result.receipt, isNull);
    expect(receiptFetches, 0);
  });
}
