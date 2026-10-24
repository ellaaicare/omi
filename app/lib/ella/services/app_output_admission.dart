/// One isolate's synchronous admission boundary, not an observation of silence.
/// Native autonomous output must also be excluded before enabling dictation.
final class AppOutputAdmission {
  AppOutputAdmission();
  static final shared = AppOutputAdmission();
  final Set<AppOutputPermit> _outputs = {};
  DartDictationOutputPermit? _dictation;

  bool get hasOutput => _outputs.isNotEmpty;
  bool get dictationReserved => _dictation != null;

  AppOutputPermit reserveOutput(String route) {
    if (_dictation != null) throw StateError('Output blocked by dictation reservation');
    final permit = AppOutputPermit._(this, route);
    _outputs.add(permit);
    return permit;
  }

  /// This reserves Dart routes only. The host must obtain native exclusion
  /// before permission/input effects; no cached native quiet flag is sufficient.
  DartDictationOutputPermit? tryReserveDictation() {
    if (_dictation != null || _outputs.isNotEmpty) return null;
    final permit = DartDictationOutputPermit(this);
    _dictation = permit;
    return permit;
  }
}

final class AppOutputPermit {
  AppOutputPermit._(this._registry, this.route);
  final AppOutputAdmission _registry;
  final String route;
  bool get isCurrent => _registry._outputs.contains(this);

  /// Only exact successful physical teardown may call this. Stop(), player
  /// state, completion callbacks, and failed-dispose retries are insufficient.
  void releaseAfterConfirmedTeardown() => _registry._outputs.remove(this);
}

/// Intentionally not a host DictationOutputLease: Dart admission alone cannot
/// be registered as production native spoken-media exclusion.
final class DartDictationOutputPermit {
  DartDictationOutputPermit(this._registry);
  final AppOutputAdmission _registry;
  bool get isCurrent => identical(_registry._dictation, this);
  Future<void> releaseAfterInputStopped() async {
    if (isCurrent) _registry._dictation = null;
  }
}
