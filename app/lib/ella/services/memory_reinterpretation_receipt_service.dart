import 'package:omi/backend/http/api/conversations.dart';

enum MemoryReceiptDiscoveryState { applied, noChange, pendingReview, failed, timeout, sessionMismatch, cancelled }

class MemoryReceiptDiscoveryRequest {
  const MemoryReceiptDiscoveryRequest({required this.conversationId, required this.sessionId});

  final String conversationId;
  final String sessionId;

  String get key => '$conversationId:$sessionId';
}

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
  // Production waits 45 seconds before the worker is eligible. A 90-second
  // window leaves another 45 seconds for worker/API latency and 24 seconds
  // beyond the observed 66-second successful canary.
  static const productionDebounce = Duration(seconds: 45);
  static const defaultMaxWait = Duration(seconds: 90);
  static const defaultPollInterval = Duration(seconds: 2);
  static const defaultMaxAttempts = 46;

  MemoryReinterpretationReceiptDiscovery({
    LatestReinterpretationFetcher? fetchLatest,
    CorrectionReceiptFetcher? fetchReceipt,
    MemoryReceiptPollDelay? wait,
    this.maxAttempts = defaultMaxAttempts,
    this.pollInterval = defaultPollInterval,
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
          final correctionId = job.appliedCorrectionId;
          if (job.hasTerminalAppliedCorrection && correctionId != null) {
            final receipt = await _fetchReceipt(conversationId, correctionId);
            if (shouldContinue?.call() == false) {
              return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.cancelled);
            }
            if (receipt != null) {
              if (receipt.conversationId != conversationId || receipt.correctionId != correctionId) {
                return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.failed);
              }
              if (receipt.isApplied) {
                return MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.applied, receipt: receipt);
              }
              if (!receipt.isPending) {
                return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.failed);
              }
            }
          } else if (job.hasTerminalAppliedCorrection) {
            return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.failed);
          } else if (job.isPendingReview) {
            return const MemoryReceiptDiscoveryResult(MemoryReceiptDiscoveryState.pendingReview);
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
