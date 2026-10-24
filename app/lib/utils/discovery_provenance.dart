/// Bounded, memory-only diagnostic ledger. Never participates in admission.
/// Identifiers are ephemeral correlation keys and are never returned by snapshot.
class DiscoveryProvenanceLedger {
  static const sources = <String>[
    'scan_callback_advertised_name', 'scan_callback_cached_name', 'scan_other',
    'retrieved_connected', 'retrieved_known', 'restored',
    'retained', 'unknown',
  ];
  static const _bound = 1024;
  int _serial = 0;
  int? _active;
  int? _knownCount;
  String _phase = 'not_observed';
  int _duplicates = 0, _overflow = 0;
  Map<String, String> _admitted = {};
  final Set<String> _retained = {};
  final Set<String> _seen = {};
  Map<String, int> _events = {}, _rows = {}, _friendRows = {};
  int? get current => _active;
  Object? _owner;
  int begin({Object? owner}) {
    _owner = owner;
    _active = ++_serial; _phase = 'scanning'; _knownCount = null;
    _duplicates = 0; _overflow = 0;
    _admitted.clear(); _retained.clear(); _seen.clear();
    _events = {}; _rows = {}; _friendRows = {};
    return _active!;
  }
  bool _owns(int? token) => token != null && token == _active;
  void knownInput(int? token, int count) {
    if (_owns(token)) _knownCount = count < 0 ? null : count.clamp(0, 1000000).toInt();
  }
  String candidateSource({required String source, required bool advertisedName,
      required bool cachedName}) => switch (source) {
    'scan' => advertisedName ? 'scan_callback_advertised_name' : cachedName ? 'scan_callback_cached_name' : 'scan_other',
    'retrievedConnected' => 'retrieved_connected',
    'retrievedKnown' => 'retrieved_known',
    'restored' => 'restored',
    _ => 'unknown',
  };
  void candidate(int? token, String id, String source) {
    if (!_owns(token)) return;
    final key = sources.contains(source) ? source : 'unknown';
    _events[key] = ((_events[key] ?? 0) + 1).clamp(0, 1000000).toInt();
    if (_seen.contains(id)) { _duplicates = (_duplicates + 1).clamp(0, 1000000).toInt(); }
    else if (_seen.length < _bound) { _seen.add(id); }
    else { _overflow = (_overflow + 1).clamp(0, 1000000).toInt(); }
  }
  void admitted(int? token, String id, String source) {
    if (!_owns(token)) return;
    if (_admitted.containsKey(id) || _admitted.length < _bound) {
      _admitted[id] = sources.contains(source) ? source : 'unknown';
    } else { _overflow = (_overflow + 1).clamp(0, 1000000).toInt(); }
  }
  /// Called only by the retained-list subscription path, not fresh discovery.
  void retainedPublication(int? token, Iterable<String> ids) {
    if (!_owns(token)) return;
    for (final id in ids.take(_bound + 1)) {
      if (_retained.length < _bound) { _retained.add(id); }
      else { _overflow = (_overflow + 1).clamp(0, 1000000).toInt(); }
    }
  }
  /// Counts the actual rows being rendered, not every candidate ever seen.
  void publish(int? token, Iterable<({String id, bool friend})> rows) {
    if (!_owns(token)) return;
    final result = <String, int>{}, approved = <String, int>{};
    var n = 0;
    for (final row in rows.take(_bound + 1)) {
      if (++n > _bound) { _overflow = (_overflow + 1).clamp(0, 1000000).toInt(); break; }
      final source = _admitted[row.id] ?? (_retained.contains(row.id) ? 'retained' : 'unknown');
      result[source] = (result[source] ?? 0) + 1;
      if (row.friend) approved[source] = (approved[source] ?? 0) + 1;
    }
    _rows = result; _friendRows = approved;
  }
  void finish(int? token, {bool failed = false}) {
    if (!_owns(token)) return;
    _phase = failed ? 'failed' : 'completed'; _active = null;
    _forgetKeys();
  }
  /// Retired diagnostics cannot retain a visible row or attribution key.
  void retirePass(int? token) {
    if (token == null || (token != _active && !(token == _serial && _active == null))) return;
    retire();
  }
  /// Navigation cancel can retain completed count-only evidence for its owner.
  void cancel() {
    if (_phase == 'completed' || _phase == 'retired') { _active = null; _forgetKeys(); return; }
    retire();
  }
  Map<String, Object?> snapshotFor(Object? owner) => owner == _owner
      ? snapshot : DiscoveryProvenanceLedger().snapshot;
  void retire() {
    _active = null; _phase = 'retired'; _knownCount = null; _owner = null;
    _events = {}; _rows = {}; _friendRows = {}; _duplicates = 0; _overflow = 0;
    _forgetKeys();
  }
  void _forgetKeys() { _admitted.clear(); _retained.clear(); _seen.clear(); }
  static Map<String, Object?>? safeSnapshot(Map<String, Object?>? input) {
    if (input == null) return null;
    final keys = <String>{'schema', 'pass', 'phase', 'known_id_input_count',
      'candidate_repeat_events', 'correlation_overflow', 'native_dedup_decision',
      for (final source in sources) 'candidate_$source',
      for (final source in sources) 'row_$source',
      for (final source in sources) 'friend_row_$source'};
    if (input.length != keys.length || !input.keys.every(keys.contains) ||
        input['schema'] != 'ella.discovery.provenance.v1' ||
        !const {'not_observed','scanning','completed','failed','retired'}.contains(input['phase']) ||
        input['native_dedup_decision'] != 'not_observed_by_this_ledger') return null;
    for (final key in keys.difference({'schema','phase','native_dedup_decision'})) {
      final n = input[key];
      if (key == 'known_id_input_count' && n == null) continue;
      if (n is! int || n < 0 || n > 1000000) return null;
    }
    return Map.unmodifiable(input);
  }
  Map<String, Object?> get snapshot => Map.unmodifiable({
    'schema': 'ella.discovery.provenance.v1', 'pass': _serial.clamp(0, 1000000),
    'phase': _phase, 'known_id_input_count': _knownCount,
    'candidate_repeat_events': _duplicates, 'correlation_overflow': _overflow,
    'native_dedup_decision': 'not_observed_by_this_ledger',
    for (final source in sources) 'candidate_$source': _events[source] ?? 0,
    for (final source in sources) 'row_$source': _rows[source] ?? 0,
    for (final source in sources) 'friend_row_$source': _friendRows[source] ?? 0,
  });
}
