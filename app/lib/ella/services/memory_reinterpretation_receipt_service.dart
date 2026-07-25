import 'package:omi/backend/http/api/conversations.dart';

enum MemoryReceiptDiscoveryState { applied, noChange, pendingReview, failed, timeout, sessionMismatch, cancelled }

class MemoryReceiptDiscoveryResult {
  const MemoryReceiptDiscoveryResult(this.state, {this.receipt});

  final MemoryReceiptDiscoveryState state;
  final ConversationCorrectionReceipt? receipt;
}

typedef LatestReinterpretationFetcher = Future<ConversationReinterpretationJob?> Function(String conversationId);
typedef CorrectionReceiptFetcher = Future<ConversationCorrectionReceipt?> Function(
    String conversationId, String correctionId);
typedef MemoryReceiptPollDelay = Future<void> Function(Duration duration);

class MemoryReinterpretationReceiptDiscovery {
  MemoryReinterpretationReceiptDiscovery({
    LatestReinterpretationFetcher? fetchLatest,
    CorrectionReceiptFetcher? fetchReceipt,
    MemoryReceiptPollDelay? wait,
    this.maxAttempts = 40,
    this.pollInterval = const Duration(milliseconds: 750),
  })  : _fetchLatest = fetchLatest ?? _getLatest,
        _fetchReceipt = fetchReceipt ?? _getReceipt,
        _wait = wait ?? ((duration) => Future<void>.delayed(duration));

  final LatestReinterpretationFetcher _fetchLatest;
  final CorrectionReceiptFetcher _fetchReceipt;
  final MemoryReceiptPollDelay _wait;
  final int maxAttempts;
  final Duration pollInterval;

  static Future<ConversationReinterpretationJob?> _getLatest(String conversationId) =>
      getLatestConversationReinterpretation(conversationId: conversationId);

  static Future<ConversationCorrectionReceipt?> _getReceipt(String conversationId, String correctionId) =>
      getConversationCorrectionReceipt(conversationId: conversationId, correctionId: correctionId);

  Future<MemoryReceiptDiscoveryResult> discover({
    required String conversationId,
    required String sessionId,
    bool Function()? shouldContinue,
  }) async {
    var sawSessionMismatch = false;
    for (var attempt = 0; attempt < maxAttempts; attempt++) {
      if (shouldContinue?.call() == false) {
        return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.cancelled);
      }

      final job = await _fetchLatest(conversationId);
      if (shouldContinue?.call() == false) {
        return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.cancelled);
      }

      if (job != null) {
        if (job.conversationId != conversationId || job.sessionId != sessionId) {
          sawSessionMismatch = true;
        } else {
          if (job.isPendingReview) {
            return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.pendingReview);
          }

          final correctionId = job.appliedCorrectionId;
          if (job.isApplied && correctionId != null) {
            final receipt = await _fetchReceipt(conversationId, correctionId);
            if (shouldContinue?.call() == false) {
              return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.cancelled);
            }
            if (receipt != null &&
                receipt.conversationId == conversationId &&
                receipt.correctionId == correctionId &&
                receipt.isApplied) {
              return MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.applied, receipt: receipt);
            }
          } else if (job.isNoChange) {
            return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.noChange);
          } else if (!job.isPending) {
            return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.failed);
          }
        }
      }

      if (attempt + 1 < maxAttempts) {
        await _wait(pollInterval);
      }
    }

    return MemoryReceiptDiscoveryResult(
      sawSessionMismatch ? MemoryReceiptDiscoveryState.sessionMismatch : MemoryReceiptDiscoveryState.timeout,
    );
  }
}
