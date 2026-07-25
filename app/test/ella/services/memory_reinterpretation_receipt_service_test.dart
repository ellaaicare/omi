import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';

const conversationId = 'conversation-1';
const sessionId = 'session-1';
const correctionId = 'correction-1';

ConversationReinterpretationJob job({
  String status = 'applied',
  String currentSessionId = sessionId,
  String outcome = '',
  List<String> correctionIds = const [correctionId],
  List<ConversationReinterpretationReceiptReference>? receipts,
}) =>
    ConversationReinterpretationJob(
      jobId: 'job-1',
      sessionId: currentSessionId,
      conversationId: conversationId,
      status: status,
      outcome: outcome,
      correctionIds: correctionIds,
      receipts: receipts ??
          correctionIds
              .map(
                (id) => ConversationReinterpretationReceiptReference(
                  conversationId: conversationId,
                  correctionId: id,
                  status: 'applied',
                ),
              )
              .toList(),
    );

ConversationCorrectionReceipt appliedReceipt({String id = correctionId}) => ConversationCorrectionReceipt(
      correctionId: id,
      conversationId: conversationId,
      status: 'applied',
      before: const ConversationCorrectionSummary(title: 'Before'),
      after: const ConversationCorrectionSummary(title: 'After'),
    );

void main() {
  test('parses identifier and status fields from latest reinterpretation response', () {
    final parsed = ConversationReinterpretationJob.tryParse({
      'job_id': 'job-1',
      'session_id': sessionId,
      'conversation_id': conversationId,
      'status': 'applied',
      'outcome': 'applied',
      'correction_ids': [correctionId],
      'receipts': [
        {'conversation_id': conversationId, 'correction_id': correctionId, 'status': 'applied'},
      ],
      'proposal_plan': {'private': 'ignored'},
    });

    expect(parsed?.sessionId, sessionId);
    expect(parsed?.outcome, 'applied');
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

  test('discovers a receipt that becomes available after the observed 66-second completion', () async {
    var elapsed = Duration.zero;
    var polls = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async {
        polls++;
        return elapsed < const Duration(seconds: 66) ? null : job();
      },
      fetchReceipt: (_, __) async => appliedReceipt(),
      wait: (duration) async => elapsed += duration,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.applied);
    expect(elapsed, const Duration(seconds: 66));
    expect(elapsed, lessThan(MemoryReinterpretationReceiptDiscovery.defaultMaxWait));
    expect(polls, 34);
  });

  test('surfaces the applied receipt from an applied-with-pending terminal job', () async {
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(status: 'pending_review', outcome: 'applied_with_pending'),
      fetchReceipt: (_, __) async => appliedReceipt(),
      wait: (_) async {},
      maxAttempts: 1,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.applied);
    expect(result.receipt?.correctionId, correctionId);
  });

  test('uses the latest applied correction when a job applied more than one correction', () async {
    const latestCorrectionId = 'correction-2';
    String? fetchedCorrectionId;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => job(correctionIds: const [correctionId, latestCorrectionId]),
      fetchReceipt: (_, requestedCorrectionId) async {
        fetchedCorrectionId = requestedCorrectionId;
        return appliedReceipt(id: requestedCorrectionId);
      },
      wait: (_) async {},
      maxAttempts: 1,
    );

    final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

    expect(result.state, MemoryReceiptDiscoveryState.applied);
    expect(fetchedCorrectionId, latestCorrectionId);
    expect(result.receipt?.correctionId, latestCorrectionId);
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
      fetchLatest: (_) async => job(status: 'pending_review', correctionIds: const [], receipts: const []),
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

  test('cancels without waiting when its owner stops the discovery', () async {
    var active = true;
    var waits = 0;
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async {
        active = false;
        return null;
      },
      fetchReceipt: (_, __) async => null,
      wait: (_) async => waits++,
      maxAttempts: 3,
    );

    final result = await discovery.discover(
      conversationId: conversationId,
      sessionId: sessionId,
      shouldContinue: () => active,
    );

    expect(result.state, MemoryReceiptDiscoveryState.cancelled);
    expect(waits, 0);
  });

  test('maps conflict and dead-letter terminal jobs to failed', () async {
    for (final status in const ['conflict', 'dead_letter']) {
      final discovery = MemoryReinterpretationReceiptDiscovery(
        fetchLatest: (_) async => job(status: status, correctionIds: const [], receipts: const []),
        fetchReceipt: (_, __) async => null,
        wait: (_) async {},
        maxAttempts: 1,
      );

      final result = await discovery.discover(conversationId: conversationId, sessionId: sessionId);

      expect(result.state, MemoryReceiptDiscoveryState.failed, reason: status);
    }
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
