import 'dart:async';

class Mutex {
  Completer<void>? _completer;

  Future<void> acquire() async {
    while (_completer != null) {
      await _completer!.future;
    }
    _completer = Completer<void>();
  }

  /// Like [acquire], but gives up after [timeout] instead of waiting
  /// indefinitely for the current holder to [release]. Returns `true` once
  /// the lock is held, `false` if it timed out first — in which case the
  /// lock was never touched, so it stays exactly as available/held as it
  /// was before this call (no dangling acquisition to clean up).
  ///
  /// Used on app-termination paths (ellaaicare/ella-ai#1280 RUN-010 / #1287)
  /// where a stuck holder must never turn a bounded cleanup into an
  /// unbounded wait.
  Future<bool> tryAcquire(Duration timeout) async {
    final deadline = DateTime.now().add(timeout);
    while (_completer != null) {
      final remaining = deadline.difference(DateTime.now());
      if (remaining <= Duration.zero) return false;
      var timedOut = false;
      await _completer!.future.timeout(remaining, onTimeout: () {
        timedOut = true;
      });
      if (timedOut) return false;
    }
    _completer = Completer<void>();
    return true;
  }

  void release() {
    final completer = _completer;
    _completer = null;
    if (completer != null && !completer.isCompleted) {
      completer.complete();
    }
  }
}
