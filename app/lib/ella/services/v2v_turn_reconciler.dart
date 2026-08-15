import 'dart:async';

class V2VTerminalTranscriptTurn {
  const V2VTerminalTranscriptTurn({
    required this.sessionId,
    required this.turnId,
    required this.userEventId,
    required this.assistantEventId,
    required this.userTranscript,
    required this.assistantTranscript,
    required this.startedAt,
    required this.completedAt,
  });

  final String sessionId;
  final String turnId;
  final String userEventId;
  final String assistantEventId;
  final String userTranscript;
  final String assistantTranscript;
  final DateTime startedAt;
  final DateTime completedAt;

  static V2VTerminalTranscriptTurn? tryParse(Map<String, dynamic> value) {
    if (value['contract_version'] != 1 || value['terminal'] != true) return null;
    final sessionId = value['session_id']?.toString().trim() ?? '';
    final turnId = value['turn_id']?.toString().trim() ?? '';
    final userEventId = value['user_event_id']?.toString().trim() ?? '';
    final assistantEventId = value['assistant_event_id']?.toString().trim() ?? '';
    final userTranscript = value['user_text']?.toString().trim() ?? '';
    final assistantTranscript = value['assistant_text']?.toString().trim() ?? '';
    final startedAt = DateTime.tryParse(value['started_at']?.toString() ?? '')?.toUtc();
    final completedAt = DateTime.tryParse(value['completed_at']?.toString() ?? '')?.toUtc();
    final sessionPattern = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$');
    final turnPattern = RegExp(r'^v2v-turn-[0-9a-f]{32}$');
    if (!sessionPattern.hasMatch(sessionId) ||
        !turnPattern.hasMatch(turnId) ||
        userEventId != '$turnId:user' ||
        assistantEventId != '$turnId:assistant' ||
        userTranscript.isEmpty ||
        assistantTranscript.isEmpty ||
        startedAt == null ||
        completedAt == null ||
        completedAt.isBefore(startedAt)) {
      return null;
    }
    return V2VTerminalTranscriptTurn(
      sessionId: sessionId,
      turnId: turnId,
      userEventId: userEventId,
      assistantEventId: assistantEventId,
      userTranscript: userTranscript,
      assistantTranscript: assistantTranscript,
      startedAt: startedAt,
      completedAt: completedAt,
    );
  }
}

class V2VTranscriptTurn {
  const V2VTranscriptTurn({
    required this.uid,
    required this.sessionId,
    required this.turnId,
    required this.userEventId,
    required this.assistantEventId,
    required this.userTranscript,
    required this.assistantTranscript,
    required this.startedAt,
    required this.completedAt,
  });

  final String uid;
  final String sessionId;
  final String turnId;
  final String userEventId;
  final String assistantEventId;
  final String userTranscript;
  final String assistantTranscript;
  final DateTime startedAt;
  final DateTime completedAt;
}

typedef V2VTranscriptTurnWriter = Future<bool> Function(V2VTranscriptTurn turn);

class V2VTurnReconciler {
  V2VTurnReconciler({required V2VTranscriptTurnWriter writer, void Function()? onWriteFailure})
      : _writer = writer,
        _onWriteFailure = onWriteFailure;

  final V2VTranscriptTurnWriter _writer;
  final void Function()? _onWriteFailure;
  final Map<String, _V2VSessionTurns> _sessions = {};
  String _activeSessionId = '';

  int get sessionCountForTesting => _sessions.length;

  int pendingTurnCountForTesting(String sessionId) => _sessions[sessionId]?.pendingTurns.length ?? 0;

  bool beginSession({required String uid, required String sessionId, required bool Function() isAuthorityCurrent}) {
    final normalizedUid = uid.trim();
    final normalizedSessionId = sessionId.trim();
    if (normalizedUid.isEmpty || normalizedSessionId.isEmpty || !isAuthorityCurrent()) return false;
    final existing = _sessions[normalizedSessionId];
    if (existing != null && (existing.uid != normalizedUid || existing.unauthorized)) return false;
    final state = existing ??
        _V2VSessionTurns(uid: normalizedUid, sessionId: normalizedSessionId, isAuthorityCurrent: isAuthorityCurrent);
    state
      ..isAuthorityCurrent = isAuthorityCurrent
      ..ended = false;
    _sessions[normalizedSessionId] = state;
    _activeSessionId = normalizedSessionId;
    if (state.pendingTurns.isNotEmpty) _signal(state);
    return true;
  }

  void endSession(String sessionId) {
    final normalizedSessionId = sessionId.trim();
    if (_activeSessionId == normalizedSessionId) _activeSessionId = '';
    final state = _sessions[normalizedSessionId];
    if (state == null) return;
    state.ended = true;
    if (!state.isAuthorityCurrent()) state.unauthorized = true;
    _maybeEvict(state);
  }

  void addTerminalTurn(String sessionId, V2VTerminalTranscriptTurn terminal) {
    final state = _authorizedActiveState(sessionId);
    if (state == null || terminal.sessionId != state.sessionId) return;
    if (state.turnIds.contains(terminal.turnId) ||
        state.userEventIds.contains(terminal.userEventId) ||
        state.assistantEventIds.contains(terminal.assistantEventId)) {
      return;
    }
    state.turnIds.add(terminal.turnId);
    state.userEventIds.add(terminal.userEventId);
    state.assistantEventIds.add(terminal.assistantEventId);
    state.pendingTurns.add(
      V2VTranscriptTurn(
        uid: state.uid,
        sessionId: state.sessionId,
        turnId: terminal.turnId,
        userEventId: terminal.userEventId,
        assistantEventId: terminal.assistantEventId,
        userTranscript: terminal.userTranscript,
        assistantTranscript: terminal.assistantTranscript,
        startedAt: terminal.startedAt,
        completedAt: terminal.completedAt,
      ),
    );
    _signal(state);
  }

  void retryAuthorizedPending() {
    for (final state in _sessions.values.toList()) {
      if (!state.isAuthorityCurrent()) {
        state.unauthorized = true;
        _maybeEvict(state);
        continue;
      }
      if (state.pendingTurns.isNotEmpty) _signal(state);
    }
  }

  Future<void> settle() async {
    while (true) {
      final pending = _sessions.values.map((state) => state.drainFuture).whereType<Future<void>>().toList();
      if (pending.isEmpty) return;
      await Future.wait(pending);
    }
  }

  _V2VSessionTurns? _authorizedActiveState(String sessionId) {
    if (sessionId.isEmpty || sessionId != _activeSessionId) return null;
    final state = _sessions[sessionId];
    if (state == null || state.ended || state.unauthorized || !state.isAuthorityCurrent()) return null;
    return state;
  }

  void _signal(_V2VSessionTurns state) {
    if (state.unauthorized || state.pendingTurns.isEmpty) return;
    if (!state.isAuthorityCurrent()) {
      state.unauthorized = true;
      _maybeEvict(state);
      return;
    }
    state.revision++;
    if (state.drainFuture != null) {
      state.rerunRequested = true;
      return;
    }
    _scheduleDrain(state);
  }

  void _scheduleDrain(_V2VSessionTurns state) {
    final future = Future<void>.microtask(() => _drain(state));
    state.drainFuture = future;
    unawaited(
      future.whenComplete(() {
        if (!identical(state.drainFuture, future)) return;
        state.drainFuture = null;
        if (state.rerunRequested && !state.unauthorized && state.pendingTurns.isNotEmpty) {
          state.rerunRequested = false;
          _scheduleDrain(state);
          return;
        }
        state.rerunRequested = false;
        _maybeEvict(state);
      }),
    );
  }

  Future<void> _drain(_V2VSessionTurns state) async {
    while (state.pendingTurns.isNotEmpty) {
      if (!state.isAuthorityCurrent()) {
        state.unauthorized = true;
        return;
      }
      if (state.failedAtRevision == state.revision) return;
      final turn = state.pendingTurns.first;
      final attemptRevision = state.revision;
      var committed = false;
      try {
        committed = await _writer(turn);
      } catch (_) {
        committed = false;
      }
      if (!state.isAuthorityCurrent()) {
        state.unauthorized = true;
        return;
      }
      if (!committed) {
        state.failedAtRevision = attemptRevision;
        _onWriteFailure?.call();
        return;
      }
      state.failedAtRevision = -1;
      state.pendingTurns.removeAt(0);
    }
  }

  void _maybeEvict(_V2VSessionTurns state) {
    if (state.drainFuture != null) return;
    if (state.unauthorized || (state.ended && state.pendingTurns.isEmpty)) {
      _sessions.remove(state.sessionId);
    }
  }
}

class _V2VSessionTurns {
  _V2VSessionTurns({required this.uid, required this.sessionId, required this.isAuthorityCurrent});

  final String uid;
  final String sessionId;
  bool Function() isAuthorityCurrent;
  final List<V2VTranscriptTurn> pendingTurns = [];
  final Set<String> turnIds = {};
  final Set<String> userEventIds = {};
  final Set<String> assistantEventIds = {};
  int revision = 0;
  int failedAtRevision = -1;
  bool rerunRequested = false;
  bool ended = false;
  bool unauthorized = false;
  Future<void>? drainFuture;
}
