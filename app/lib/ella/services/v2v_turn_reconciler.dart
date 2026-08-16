import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:path_provider/path_provider.dart';

const int maxV2VTranscriptChars = 20000;

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
        userTranscript.length > maxV2VTranscriptChars ||
        assistantTranscript.length > maxV2VTranscriptChars ||
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
    required this.ownerNamespace,
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
  final String ownerNamespace;
  final String sessionId;
  final String turnId;
  final String userEventId;
  final String assistantEventId;
  final String userTranscript;
  final String assistantTranscript;
  final DateTime startedAt;
  final DateTime completedAt;

  Map<String, dynamic> toJson() => {
        'uid': uid,
        'owner_namespace': ownerNamespace,
        'session_id': sessionId,
        'turn_id': turnId,
        'user_event_id': userEventId,
        'assistant_event_id': assistantEventId,
        'user_transcript': userTranscript,
        'assistant_transcript': assistantTranscript,
        'started_at': startedAt.toUtc().toIso8601String(),
        'completed_at': completedAt.toUtc().toIso8601String(),
      };

  static V2VTranscriptTurn? tryParse(
    Object? value, {
    required String expectedUid,
    required String expectedOwnerNamespace,
  }) {
    if (value is! Map) return null;
    final json = Map<String, dynamic>.from(value);
    final terminal = V2VTerminalTranscriptTurn.tryParse({
      'contract_version': 1,
      'terminal': true,
      'session_id': json['session_id'],
      'turn_id': json['turn_id'],
      'user_event_id': json['user_event_id'],
      'assistant_event_id': json['assistant_event_id'],
      'user_text': json['user_transcript'],
      'assistant_text': json['assistant_transcript'],
      'started_at': json['started_at'],
      'completed_at': json['completed_at'],
    });
    if (terminal == null || json['uid'] != expectedUid || json['owner_namespace'] != expectedOwnerNamespace) {
      return null;
    }
    return V2VTranscriptTurn(
      uid: expectedUid,
      ownerNamespace: expectedOwnerNamespace,
      sessionId: terminal.sessionId,
      turnId: terminal.turnId,
      userEventId: terminal.userEventId,
      assistantEventId: terminal.assistantEventId,
      userTranscript: terminal.userTranscript,
      assistantTranscript: terminal.assistantTranscript,
      startedAt: terminal.startedAt,
      completedAt: terminal.completedAt,
    );
  }

  bool samePayload(V2VTranscriptTurn other) => jsonEncode(toJson()) == jsonEncode(other.toJson());
}

typedef V2VTranscriptTurnWriter = Future<bool> Function(V2VTranscriptTurn turn);

Future<bool> activateV2VTransportInOrder({
  required Future<bool> Function() armSession,
  required void Function() attachListener,
  required Future<bool> Function() startMicrophone,
}) async {
  if (!await armSession()) return false;
  attachListener();
  return startMicrophone();
}

abstract interface class V2VTurnDurableStore {
  Future<List<V2VTranscriptTurn>> load({required String uid, required String ownerNamespace});

  Future<void> put(V2VTranscriptTurn turn);

  Future<void> remove(V2VTranscriptTurn turn);

  Future<void> clearOwner({required String uid, required String ownerNamespace});
}

class MemoryV2VTurnDurableStore implements V2VTurnDurableStore {
  final Map<String, List<V2VTranscriptTurn>> _turnsByOwner = {};

  String _key(String uid, String ownerNamespace) => '$uid\n$ownerNamespace';

  @override
  Future<List<V2VTranscriptTurn>> load({required String uid, required String ownerNamespace}) async =>
      List<V2VTranscriptTurn>.of(_turnsByOwner[_key(uid, ownerNamespace)] ?? const []);

  @override
  Future<void> put(V2VTranscriptTurn turn) async {
    final turns = _turnsByOwner.putIfAbsent(_key(turn.uid, turn.ownerNamespace), () => []);
    final existing = turns.where((candidate) => candidate.turnId == turn.turnId).firstOrNull;
    if (existing == null) {
      turns.add(turn);
    } else if (!existing.samePayload(turn)) {
      throw StateError('Conflicting stable V2V turn identity');
    }
  }

  @override
  Future<void> remove(V2VTranscriptTurn turn) async {
    final key = _key(turn.uid, turn.ownerNamespace);
    final turns = _turnsByOwner[key];
    turns?.removeWhere((candidate) => candidate.samePayload(turn));
    if (turns?.isEmpty == true) _turnsByOwner.remove(key);
  }

  @override
  Future<void> clearOwner({required String uid, required String ownerNamespace}) async {
    _turnsByOwner.remove(_key(uid, ownerNamespace));
  }
}

class FileV2VTurnDurableStore implements V2VTurnDurableStore {
  FileV2VTurnDurableStore({
    Directory? baseDirectory,
    this.maxPendingTurns = 256,
    this.maxManifestBytes = 16 * 1024 * 1024,
  })  : assert(maxPendingTurns > 0),
        assert(maxManifestBytes > 0),
        _configuredBaseDirectory = baseDirectory;

  static const int _manifestVersion = 1;
  static final RegExp _ownerNamespacePattern = RegExp(r'^[0-9a-f]{24}$');

  final Directory? _configuredBaseDirectory;
  final int maxPendingTurns;
  final int maxManifestBytes;
  static Future<void> _operationTail = Future<void>.value();

  @override
  Future<List<V2VTranscriptTurn>> load({required String uid, required String ownerNamespace}) =>
      _serialized(() async => List<V2VTranscriptTurn>.of(await _read(uid: uid, ownerNamespace: ownerNamespace)));

  @override
  Future<void> put(V2VTranscriptTurn turn) => _serialized(() async {
        final turns = await _read(uid: turn.uid, ownerNamespace: turn.ownerNamespace);
        final existing = turns.where((candidate) => candidate.turnId == turn.turnId).firstOrNull;
        if (existing == null) {
          turns.add(turn);
        } else if (!existing.samePayload(turn)) {
          throw StateError('Conflicting stable V2V turn identity');
        } else {
          return;
        }
        await _write(uid: turn.uid, ownerNamespace: turn.ownerNamespace, turns: turns);
      });

  @override
  Future<void> remove(V2VTranscriptTurn turn) => _serialized(() async {
        final turns = await _read(uid: turn.uid, ownerNamespace: turn.ownerNamespace);
        final originalLength = turns.length;
        turns.removeWhere((candidate) => candidate.samePayload(turn));
        if (turns.length == originalLength) return;
        await _write(uid: turn.uid, ownerNamespace: turn.ownerNamespace, turns: turns);
      });

  @override
  Future<void> clearOwner({required String uid, required String ownerNamespace}) => _serialized(() async {
        await _read(uid: uid, ownerNamespace: ownerNamespace);
        final directory = await _ownerDirectory(ownerNamespace);
        final manifest = File('${directory.path}/pending_turns.json');
        final backup = File('${manifest.path}.bak');
        final temporary = File('${manifest.path}.tmp');
        for (final file in [manifest, backup, temporary]) {
          if (await file.exists()) await file.delete();
        }
      });

  Future<T> _serialized<T>(Future<T> Function() operation) {
    final result = Completer<T>();
    _operationTail = _operationTail.then((_) async {
      try {
        result.complete(await operation());
      } catch (error, stackTrace) {
        result.completeError(error, stackTrace);
      }
    });
    return result.future;
  }

  Future<Directory> _ownerDirectory(String ownerNamespace) async {
    if (!_ownerNamespacePattern.hasMatch(ownerNamespace)) throw ArgumentError.value(ownerNamespace, 'ownerNamespace');
    final base = _configuredBaseDirectory ?? await getApplicationSupportDirectory();
    return Directory('${base.path}/ella_v2v_turn_outbox/$ownerNamespace');
  }

  Future<List<V2VTranscriptTurn>> _read({required String uid, required String ownerNamespace}) async {
    if (uid.trim().isEmpty) throw ArgumentError.value(uid, 'uid');
    final directory = await _ownerDirectory(ownerNamespace);
    final manifest = File('${directory.path}/pending_turns.json');
    final backup = File('${manifest.path}.bak');
    Object? lastError;
    for (final candidate in [manifest, backup]) {
      if (!await candidate.exists()) continue;
      try {
        final decoded = jsonDecode(await candidate.readAsString());
        if (decoded is! Map) throw const FormatException('Invalid V2V outbox manifest');
        final json = Map<String, dynamic>.from(decoded);
        if (json['version'] != _manifestVersion ||
            json['uid'] != uid ||
            json['owner_namespace'] != ownerNamespace ||
            json['turns'] is! List) {
          throw const FormatException('Mismatched V2V outbox manifest');
        }
        final turns = <V2VTranscriptTurn>[];
        final turnIds = <String>{};
        final userEventIds = <String>{};
        final assistantEventIds = <String>{};
        for (final value in json['turns'] as List) {
          final turn = V2VTranscriptTurn.tryParse(
            value,
            expectedUid: uid,
            expectedOwnerNamespace: ownerNamespace,
          );
          if (turn == null ||
              !turnIds.add(turn.turnId) ||
              !userEventIds.add(turn.userEventId) ||
              !assistantEventIds.add(turn.assistantEventId)) {
            throw const FormatException('Invalid V2V outbox turn');
          }
          turns.add(turn);
        }
        return turns;
      } catch (error) {
        lastError = error;
      }
    }
    if (lastError != null) throw lastError;
    return [];
  }

  Future<void> _write({
    required String uid,
    required String ownerNamespace,
    required List<V2VTranscriptTurn> turns,
  }) async {
    if (turns.length > maxPendingTurns) throw StateError('V2V outbox capacity reached');
    final encoded = jsonEncode({
      'version': _manifestVersion,
      'uid': uid,
      'owner_namespace': ownerNamespace,
      'turns': turns.map((turn) => turn.toJson()).toList(),
    });
    if (utf8.encode(encoded).length > maxManifestBytes) throw StateError('V2V outbox byte capacity reached');
    final directory = await _ownerDirectory(ownerNamespace);
    await directory.create(recursive: true);
    final manifest = File('${directory.path}/pending_turns.json');
    final backup = File('${manifest.path}.bak');
    final temporary = File('${manifest.path}.tmp');
    if (await temporary.exists()) await temporary.delete();
    await temporary.writeAsString(encoded, flush: true);

    var movedOriginal = false;
    try {
      if (await manifest.exists()) {
        if (await backup.exists()) await backup.delete();
        await manifest.rename(backup.path);
        movedOriginal = true;
      }
      await temporary.rename(manifest.path);
      if (await backup.exists()) await backup.delete();
    } catch (_) {
      if (!await manifest.exists() && movedOriginal && await backup.exists()) {
        await backup.rename(manifest.path);
      }
      rethrow;
    }
  }
}

class V2VTurnReconciler {
  V2VTurnReconciler({
    required V2VTranscriptTurnWriter writer,
    V2VTurnDurableStore? durableStore,
    void Function()? onWriteFailure,
  })  : _durableStore = durableStore ?? MemoryV2VTurnDurableStore(),
        _writer = writer,
        _onWriteFailure = onWriteFailure;

  final V2VTurnDurableStore _durableStore;
  final V2VTranscriptTurnWriter _writer;
  final void Function()? _onWriteFailure;
  final Map<String, _V2VSessionTurns> _sessions = {};
  String _activeSessionId = '';

  int get sessionCountForTesting => _sessions.length;

  int pendingTurnCountForTesting(String sessionId) => _sessions[sessionId]?.pendingTurns.length ?? 0;

  Future<bool> beginSession({
    required String uid,
    required String ownerNamespace,
    required String sessionId,
    required bool Function() isAuthorityCurrent,
  }) async {
    final normalizedUid = uid.trim();
    final normalizedOwnerNamespace = ownerNamespace.trim();
    final normalizedSessionId = sessionId.trim();
    if (normalizedUid.isEmpty ||
        !FileV2VTurnDurableStore._ownerNamespacePattern.hasMatch(normalizedOwnerNamespace) ||
        normalizedSessionId.isEmpty ||
        !isAuthorityCurrent()) {
      return false;
    }
    late final List<V2VTranscriptTurn> restored;
    try {
      restored = await _durableStore.load(uid: normalizedUid, ownerNamespace: normalizedOwnerNamespace);
    } catch (_) {
      _onWriteFailure?.call();
      return false;
    }
    if (!isAuthorityCurrent()) return false;
    for (final turn in restored) {
      final restoredState = _sessions.putIfAbsent(
        turn.sessionId,
        () => _V2VSessionTurns(
          uid: normalizedUid,
          ownerNamespace: normalizedOwnerNamespace,
          sessionId: turn.sessionId,
          isAuthorityCurrent: isAuthorityCurrent,
        )..ended = true,
      );
      if (restoredState.uid != normalizedUid || restoredState.ownerNamespace != normalizedOwnerNamespace) return false;
      restoredState.isAuthorityCurrent = isAuthorityCurrent;
      if (!_rememberTurn(restoredState, turn)) return false;
    }
    final existing = _sessions[normalizedSessionId];
    if (existing != null &&
        (existing.uid != normalizedUid ||
            existing.ownerNamespace != normalizedOwnerNamespace ||
            existing.unauthorized)) {
      return false;
    }
    final state = existing ??
        _V2VSessionTurns(
          uid: normalizedUid,
          ownerNamespace: normalizedOwnerNamespace,
          sessionId: normalizedSessionId,
          isAuthorityCurrent: isAuthorityCurrent,
        );
    state
      ..isAuthorityCurrent = isAuthorityCurrent
      ..ended = false;
    _sessions[normalizedSessionId] = state;
    _activeSessionId = normalizedSessionId;
    for (final candidate in _sessions.values.toList()) {
      if (candidate.pendingTurns.isNotEmpty) _signal(candidate);
    }
    return true;
  }

  void endSession(String sessionId) {
    final normalizedSessionId = sessionId.trim();
    if (_activeSessionId == normalizedSessionId) _activeSessionId = '';
    final state = _sessions[normalizedSessionId];
    if (state == null) return;
    state.ended = true;
    if (!state.isAuthorityCurrent()) {
      _markUnauthorized(state);
      return;
    }
    _maybeEvict(state);
  }

  Future<bool> addTerminalTurn(String sessionId, V2VTerminalTranscriptTurn terminal) {
    final state = _authorizedActiveState(sessionId);
    if (state == null || terminal.sessionId != state.sessionId) return Future<bool>.value(false);
    if (state.turnIds.contains(terminal.turnId) ||
        state.userEventIds.contains(terminal.userEventId) ||
        state.assistantEventIds.contains(terminal.assistantEventId)) {
      return Future<bool>.value(false);
    }
    final turn = V2VTranscriptTurn(
      uid: state.uid,
      ownerNamespace: state.ownerNamespace,
      sessionId: state.sessionId,
      turnId: terminal.turnId,
      userEventId: terminal.userEventId,
      assistantEventId: terminal.assistantEventId,
      userTranscript: terminal.userTranscript,
      assistantTranscript: terminal.assistantTranscript,
      startedAt: terminal.startedAt,
      completedAt: terminal.completedAt,
    );
    if (!_reserveTurn(state, turn)) return Future<bool>.value(false);

    late final Future<bool> enqueueFuture;
    enqueueFuture = _enqueueAcceptedTurn(state, turn);
    state.enqueueFutures.add(enqueueFuture);
    unawaited(
      enqueueFuture.then<void>((_) {
        state.enqueueFutures.remove(enqueueFuture);
        if (state.unauthorized && state.unauthorizedClearPending && state.enqueueFutures.isEmpty) {
          _markUnauthorized(state);
          return;
        }
        _maybeEvict(state);
      }),
    );
    return enqueueFuture;
  }

  void retryAuthorizedPending() {
    for (final state in _sessions.values.toList()) {
      if (state.unauthorized) {
        if (state.unauthorizedClearPending) _markUnauthorized(state);
        continue;
      }
      if (!state.isAuthorityCurrent()) {
        _markUnauthorized(state);
        continue;
      }
      if (state.pendingTurns.isNotEmpty) _signal(state);
    }
  }

  Future<void> settle() async {
    while (true) {
      final pending = <Future<void>>[
        for (final state in _sessions.values)
          for (final enqueueFuture in state.enqueueFutures) enqueueFuture.then<void>((_) {}),
        ..._sessions.values.map((state) => state.drainFuture).whereType<Future<void>>(),
      ];
      if (pending.isEmpty) return;
      await Future.wait(pending);
    }
  }

  _V2VSessionTurns? _authorizedActiveState(String sessionId) {
    if (sessionId.isEmpty || sessionId != _activeSessionId) return null;
    final state = _sessions[sessionId];
    if (state == null || state.ended || state.unauthorized) return null;
    if (!state.isAuthorityCurrent()) {
      _markUnauthorized(state);
      return null;
    }
    return state;
  }

  void _signal(_V2VSessionTurns state) {
    if (state.pendingTurns.isEmpty) return;
    if (state.unauthorized || !state.isAuthorityCurrent()) {
      _markUnauthorized(state);
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
    if (state.unauthorized || !state.isAuthorityCurrent()) {
      state
        ..unauthorized = true
        ..unauthorizedClearPending = true;
      await _clearUnauthorized(state);
      return;
    }
    while (state.pendingTurns.isNotEmpty) {
      if (state.unauthorized || !state.isAuthorityCurrent()) {
        state
          ..unauthorized = true
          ..unauthorizedClearPending = true;
        await _clearUnauthorized(state);
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
        state
          ..unauthorized = true
          ..unauthorizedClearPending = true;
        await _clearUnauthorized(state);
        return;
      }
      if (!committed) {
        state.failedAtRevision = attemptRevision;
        _onWriteFailure?.call();
        return;
      }
      try {
        await _durableStore.remove(turn);
      } catch (_) {
        state.failedAtRevision = attemptRevision;
        _onWriteFailure?.call();
        return;
      }
      state.failedAtRevision = -1;
      state.pendingTurns.removeAt(0);
    }
  }

  void _markUnauthorized(_V2VSessionTurns state) {
    state
      ..unauthorized = true
      ..unauthorizedClearPending = true;
    if (state.enqueueFutures.isNotEmpty) {
      state.rerunRequested = true;
      return;
    }
    state.revision++;
    if (state.drainFuture != null) {
      state.rerunRequested = true;
      return;
    }
    _scheduleDrain(state);
  }

  Future<void> _clearUnauthorized(_V2VSessionTurns state) async {
    try {
      await _durableStore.clearOwner(uid: state.uid, ownerNamespace: state.ownerNamespace);
      final sameOwnerStates = _sessions.values
          .where((candidate) => candidate.uid == state.uid && candidate.ownerNamespace == state.ownerNamespace)
          .toList();
      for (final candidate in sameOwnerStates) {
        candidate.unauthorized = true;
        candidate.unauthorizedClearPending = false;
        candidate.pendingTurns.clear();
        candidate.failedAtRevision = -1;
        _maybeEvict(candidate);
      }
    } catch (_) {
      state.unauthorizedClearPending = true;
      state.failedAtRevision = state.revision;
      _onWriteFailure?.call();
    }
  }

  Future<bool> _enqueueAcceptedTurn(_V2VSessionTurns state, V2VTranscriptTurn turn) async {
    try {
      await _durableStore.put(turn);
    } catch (_) {
      _releaseTurnReservation(state, turn);
      _onWriteFailure?.call();
      if (!state.isAuthorityCurrent()) _markUnauthorized(state);
      return false;
    }
    if (state.unauthorized || !state.isAuthorityCurrent()) {
      state
        ..unauthorized = true
        ..unauthorizedClearPending = true;
      await _clearUnauthorized(state);
      return false;
    }
    state.pendingTurns.add(turn);
    _signal(state);
    return true;
  }

  bool _reserveTurn(_V2VSessionTurns state, V2VTranscriptTurn turn) {
    if (state.turnIds.contains(turn.turnId) ||
        state.userEventIds.contains(turn.userEventId) ||
        state.assistantEventIds.contains(turn.assistantEventId)) {
      return false;
    }
    state.turnIds.add(turn.turnId);
    state.userEventIds.add(turn.userEventId);
    state.assistantEventIds.add(turn.assistantEventId);
    return true;
  }

  void _releaseTurnReservation(_V2VSessionTurns state, V2VTranscriptTurn turn) {
    state.turnIds.remove(turn.turnId);
    state.userEventIds.remove(turn.userEventId);
    state.assistantEventIds.remove(turn.assistantEventId);
  }

  bool _rememberTurn(_V2VSessionTurns state, V2VTranscriptTurn turn) {
    final existing = state.pendingTurns.where((candidate) => candidate.turnId == turn.turnId).firstOrNull;
    if (existing != null) return existing.samePayload(turn);
    if (!_reserveTurn(state, turn)) return false;
    state.pendingTurns.add(turn);
    return true;
  }

  void _maybeEvict(_V2VSessionTurns state) {
    if (state.drainFuture != null || state.enqueueFutures.isNotEmpty) return;
    final clearedUnauthorized = state.unauthorized && !state.unauthorizedClearPending;
    if ((clearedUnauthorized || state.ended) && state.pendingTurns.isEmpty) {
      _sessions.remove(state.sessionId);
    }
  }
}

class _V2VSessionTurns {
  _V2VSessionTurns({
    required this.uid,
    required this.ownerNamespace,
    required this.sessionId,
    required this.isAuthorityCurrent,
  });

  final String uid;
  final String ownerNamespace;
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
  bool unauthorizedClearPending = false;
  final Set<Future<bool>> enqueueFutures = {};
  Future<void>? drainFuture;
}
