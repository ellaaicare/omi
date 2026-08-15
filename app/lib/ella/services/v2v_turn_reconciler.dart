import 'dart:async';

class V2VTranscriptTurn {
  const V2VTranscriptTurn({
    required this.uid,
    required this.sessionId,
    required this.turnId,
    required this.userTranscript,
    required this.assistantTranscript,
    required this.startedAt,
    required this.completedAt,
  });

  final String uid;
  final String sessionId;
  final String turnId;
  final String userTranscript;
  final String assistantTranscript;
  final DateTime startedAt;
  final DateTime completedAt;
}

typedef V2VTranscriptTurnWriter = Future<bool> Function(V2VTranscriptTurn turn);

class V2VTurnReconciler {
  V2VTurnReconciler({
    required V2VTranscriptTurnWriter writer,
    DateTime Function()? clock,
    void Function()? onWriteFailure,
  })  : _writer = writer,
        _clock = clock ?? DateTime.now,
        _onWriteFailure = onWriteFailure;

  final V2VTranscriptTurnWriter _writer;
  final DateTime Function() _clock;
  final void Function()? _onWriteFailure;
  final Map<String, _V2VSessionTurns> _sessions = {};
  String _activeSessionId = '';

  bool beginSession({required String uid, required String sessionId, required bool Function() isAuthorityCurrent}) {
    final normalizedUid = uid.trim();
    final normalizedSessionId = sessionId.trim();
    if (normalizedUid.isEmpty || normalizedSessionId.isEmpty || !isAuthorityCurrent()) return false;
    final existing = _sessions[normalizedSessionId];
    if (existing != null && existing.uid != normalizedUid) return false;
    _sessions[normalizedSessionId] = existing ??
        _V2VSessionTurns(uid: normalizedUid, sessionId: normalizedSessionId, isAuthorityCurrent: isAuthorityCurrent);
    _activeSessionId = normalizedSessionId;
    return true;
  }

  void endSession(String sessionId) {
    if (_activeSessionId == sessionId) _activeSessionId = '';
  }

  void beginUserTurn(String sessionId) {
    final state = _authorizedActiveState(sessionId);
    if (state == null) return;
    state.userGeneration++;
  }

  void addUserTerminal(String sessionId, String text, {String eventId = ''}) {
    final state = _authorizedActiveState(sessionId);
    final normalized = text.trim();
    if (state == null || normalized.isEmpty) return;
    final normalizedEventId = eventId.trim();
    if (normalizedEventId.isNotEmpty && !state.userEventIds.add(normalizedEventId)) return;
    final repeatsLastTerminal = state.userTerminals.isNotEmpty && state.userTerminals.last.text == normalized;
    final repeatsCurrentGeneration =
        state.userGeneration > 0 && state.userGeneration == state.lastAcceptedUserGeneration;
    final repeatsUnidentifiedPendingTurn =
        state.userGeneration == 0 && state.userTerminals.length >= state.assistantTerminals.length;
    if (repeatsLastTerminal && (repeatsCurrentGeneration || repeatsUnidentifiedPendingTurn)) {
      return;
    }
    state.lastAcceptedUserGeneration = state.userGeneration;
    state.userTerminals.add(_TerminalTranscript(normalized, _clock()));
    _signal(state);
  }

  void addAssistantFragment(String sessionId, String text) {
    final state = _authorizedActiveState(sessionId);
    if (state == null || text.isEmpty) return;
    state.assistantStartedAt ??= _clock();
    state.assistantBuffer.write(text);
  }

  void discardNonterminalAssistant(String sessionId) {
    final state = _authorizedActiveState(sessionId);
    if (state == null) return;
    state.assistantBuffer.clear();
    state.assistantStartedAt = null;
  }

  void markAssistantTerminal(String sessionId, {String finalText = '', String eventId = ''}) {
    final state = _authorizedActiveState(sessionId);
    if (state == null) return;
    final normalizedEventId = eventId.trim();
    if (normalizedEventId.isNotEmpty && !state.assistantEventIds.add(normalizedEventId)) return;

    final normalizedFinal = finalText.trim();
    final accumulated = state.assistantBuffer.toString().trim();
    final terminalText = normalizedFinal.isEmpty
        ? accumulated
        : accumulated.isEmpty || normalizedFinal.startsWith(accumulated)
            ? normalizedFinal
            : accumulated;
    state.assistantBuffer.clear();
    if (terminalText.isEmpty) {
      state.assistantStartedAt = null;
      return;
    }
    state.assistantTerminals.add(_TerminalTranscript(terminalText, _clock(), startedAt: state.assistantStartedAt));
    state.assistantStartedAt = null;
    _signal(state);
  }

  void retryAuthorizedPending() {
    for (final state in _sessions.values) {
      if (!state.isAuthorityCurrent()) continue;
      _signal(state);
    }
  }

  Future<void> settle() async {
    while (true) {
      final pending = _sessions.values.map((state) => state.drainFuture).whereType<Future<void>>().toList();
      if (pending.isEmpty) return;
      await Future.wait(pending);
      if (_sessions.values.every((state) => state.drainFuture == null)) return;
    }
  }

  _V2VSessionTurns? _authorizedActiveState(String sessionId) {
    if (sessionId.isEmpty || sessionId != _activeSessionId) return null;
    final state = _sessions[sessionId];
    if (state == null || !state.isAuthorityCurrent()) return null;
    return state;
  }

  void _signal(_V2VSessionTurns state) {
    state.revision++;
    if (state.drainFuture != null) return;
    final future = Future<void>.microtask(() => _drain(state));
    state.drainFuture = future;
    unawaited(
      future.whenComplete(() {
        if (identical(state.drainFuture, future)) state.drainFuture = null;
      }),
    );
  }

  Future<void> _drain(_V2VSessionTurns state) async {
    while (state.nextTurnIndex < state.userTerminals.length && state.nextTurnIndex < state.assistantTerminals.length) {
      if (!state.isAuthorityCurrent()) return;
      if (state.failedAtRevision == state.revision) return;
      final index = state.nextTurnIndex;
      final user = state.userTerminals[index];
      final assistant = state.assistantTerminals[index];
      final startedAt = [user.startedAt, assistant.startedAt].reduce((a, b) => a.isBefore(b) ? a : b);
      final completedAt = [user.completedAt, assistant.completedAt].reduce((a, b) => a.isAfter(b) ? a : b);
      final turn = V2VTranscriptTurn(
        uid: state.uid,
        sessionId: state.sessionId,
        turnId: 'turn-${(index + 1).toString().padLeft(6, '0')}',
        userTranscript: user.text,
        assistantTranscript: assistant.text,
        startedAt: startedAt,
        completedAt: completedAt,
      );

      var committed = false;
      try {
        committed = await _writer(turn);
      } catch (_) {
        committed = false;
      }
      if (!state.isAuthorityCurrent()) return;
      if (!committed) {
        state.failedAtRevision = state.revision;
        _onWriteFailure?.call();
        return;
      }
      state.failedAtRevision = -1;
      state.nextTurnIndex++;
    }
  }
}

class _V2VSessionTurns {
  _V2VSessionTurns({required this.uid, required this.sessionId, required this.isAuthorityCurrent});

  final String uid;
  final String sessionId;
  final bool Function() isAuthorityCurrent;
  final List<_TerminalTranscript> userTerminals = [];
  final List<_TerminalTranscript> assistantTerminals = [];
  final Set<String> userEventIds = {};
  final Set<String> assistantEventIds = {};
  final StringBuffer assistantBuffer = StringBuffer();
  DateTime? assistantStartedAt;
  int userGeneration = 0;
  int lastAcceptedUserGeneration = -1;
  int nextTurnIndex = 0;
  int revision = 0;
  int failedAtRevision = -1;
  Future<void>? drainFuture;
}

class _TerminalTranscript {
  _TerminalTranscript(this.text, this.completedAt, {DateTime? startedAt}) : startedAt = startedAt ?? completedAt;

  final String text;
  final DateTime startedAt;
  final DateTime completedAt;
}
