import 'dart:async';

/// Foreground owner-bound GET scheduling only. It cannot enqueue enrichment,
/// reprocess a recording, change consent, or select a summary version.
class SummaryReadRetry {
  SummaryReadRetry({
    required this.current,
    required this.pending,
    required this.externallyBusy,
    required this.refresh,
    DateTime Function()? now,
    this.delays = const [Duration(seconds: 10), Duration(seconds: 30), Duration(seconds: 120), Duration(seconds: 300)],
  }) : now = now ?? DateTime.now;

  final bool Function() current, pending, externallyBusy;
  final Future<void> Function() refresh;
  final DateTime Function() now;
  final List<Duration> delays;
  Timer? _timer;
  DateTime? _nextAt;
  var _step = 0, _generation = 0;
  bool _active = true, _disposed = false, _busy = false;

  void setActive(bool active) {
    if (_disposed) return;
    _active = active;
    if (!active) {
      _timer?.cancel();
      _timer = null;
    } else {
      reconsider();
    }
  }

  void cancel() {
    _generation++;
    _timer?.cancel();
    _timer = null;
    _nextAt = null;
    _step = 0;
    _busy = false;
  }

  void reconsider() {
    if (_disposed) return;
    if (!current() || !pending()) {
      cancel();
      return;
    }
    if (_busy) return;
    _nextAt ??= now().add(delays[_step.clamp(0, delays.length - 1)]);
    if (!_active || _timer?.isActive == true) return;
    final remaining = _nextAt!.difference(now());
    _timer = Timer(remaining.isNegative ? Duration.zero : remaining, _fire);
  }

  Future<void> _fire() async {
    _timer = null;
    if (_disposed || !_active || !current() || !pending()) {
      reconsider();
      return;
    }
    if (externallyBusy()) {
      // Join the provider's existing single-flight boundary. Keep the original
      // deadline; a completed manual read can settle or rearm this same intent.
      _timer = Timer(const Duration(seconds: 1), _fire);
      return;
    }
    final generation = _generation;
    _busy = true;
    _nextAt = null;
    try {
      await refresh();
    } catch (_) {
      // The provider publishes stale/error evidence. No write or retry here.
    } finally {
      if (!_disposed && generation == _generation) {
        _busy = false;
        if (_step < delays.length - 1) _step++;
        reconsider();
      }
    }
  }

  void dispose() {
    _disposed = true;
    cancel();
  }
}
