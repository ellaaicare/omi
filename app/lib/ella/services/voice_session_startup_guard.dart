class VoiceSessionStartupGuard {
  int _generation = 0;
  bool _disposed = false;
  bool _starting = false;

  bool get isStarting => _starting;

  int begin() {
    if (_disposed) throw StateError('Voice startup guard is disposed');
    _starting = true;
    return ++_generation;
  }

  bool isCurrent(int generation) => !_disposed && generation == _generation;

  void complete(int generation) {
    if (isCurrent(generation)) _starting = false;
  }

  void cancel() {
    _generation += 1;
    _starting = false;
  }

  void dispose() {
    _disposed = true;
    _generation += 1;
    _starting = false;
  }
}
