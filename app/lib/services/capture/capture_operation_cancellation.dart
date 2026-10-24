import 'dart:async';

/// Explicit user-operation deadline, separate from committed capture state.
class CaptureOperationCancellation {
  CaptureOperationCancellation({this.timeout = const Duration(seconds: 20)});
  final Duration timeout;
  final _cancelled = Completer<void>();
  Timer? _deadline;
  void Function()? onCancel;
  bool get isCancelled => _cancelled.isCompleted;
  Future<void> get whenCancelled => _cancelled.future;
  void arm() => _deadline ??= Timer(timeout, cancel);
  void cancel() {
    if (isCancelled) return;
    _cancelled.complete();
    onCancel?.call();
  }

  void dispose() => _deadline?.cancel();
}

/// Shared production fence prevents replacement providers from activating
/// hardware while retired asynchronous work still owns a receipt.
class CaptureRetirementFence {
  static final shared = CaptureRetirementFence();
  int _pending = 0;
  Completer<void>? _drain;
  bool get pending => _pending > 0;
  Future<void> get drain => _drain?.future ?? Future.value();
  void hold() {
    _pending++;
    _drain ??= Completer<void>();
  }

  void release() {
    if (--_pending == 0) {
      _drain?.complete();
      _drain = null;
    }
  }
}
