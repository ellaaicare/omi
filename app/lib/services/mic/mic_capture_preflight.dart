/// Exact permission/setup reservation; labels never authorize cancellation.
abstract interface class MicCapturePreflight {
  bool get isCurrent;
  bool get hasAttemptedStart;

  /// Releases only an unused exact reservation. Never stops an active engine.
  Future<bool> cancelBeforeStart();
}

abstract interface class PreflightMicCapture {
  MicCapturePreflight? reserveCapturePreflight({required bool Function() hasAuthority});
}
