import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:path/path.dart' as p;
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
    required this.authorityFingerprint,
    required this.sessionId,
    required this.turnId,
    required this.userEventId,
    required this.assistantEventId,
    required this.userTranscript,
    required this.assistantTranscript,
    required this.startedAt,
    required this.completedAt,
    this.turnOrdinal,
  });

  final String uid;
  final String ownerNamespace;
  final String authorityFingerprint;
  final String sessionId;
  final String turnId;
  final String userEventId;
  final String assistantEventId;
  final String userTranscript;
  final String assistantTranscript;
  final DateTime startedAt;
  final DateTime completedAt;
  final int? turnOrdinal;

  Map<String, dynamic> toJson() => {
        'uid': uid,
        'owner_namespace': ownerNamespace,
        'authority_fingerprint': authorityFingerprint,
        'session_id': sessionId,
        'turn_id': turnId,
        'user_event_id': userEventId,
        'assistant_event_id': assistantEventId,
        'user_transcript': userTranscript,
        'assistant_transcript': assistantTranscript,
        'started_at': startedAt.toUtc().toIso8601String(),
        'completed_at': completedAt.toUtc().toIso8601String(),
        if (turnOrdinal != null) 'turn_ordinal': turnOrdinal,
      };

  static V2VTranscriptTurn? tryParse(
    Object? value, {
    required String expectedUid,
    required String expectedOwnerNamespace,
    required String expectedAuthorityFingerprint,
    int? fallbackTurnOrdinal,
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
    final persistedTurnOrdinal = json['turn_ordinal'];
    if (persistedTurnOrdinal != null && (persistedTurnOrdinal is! int || persistedTurnOrdinal < 0)) return null;
    final turnOrdinal = persistedTurnOrdinal as int? ?? fallbackTurnOrdinal;
    if (turnOrdinal != null && turnOrdinal < 0) return null;
    if (terminal == null ||
        json['uid'] != expectedUid ||
        json['owner_namespace'] != expectedOwnerNamespace ||
        json['authority_fingerprint'] != expectedAuthorityFingerprint) {
      return null;
    }
    return V2VTranscriptTurn(
      uid: expectedUid,
      ownerNamespace: expectedOwnerNamespace,
      authorityFingerprint: expectedAuthorityFingerprint,
      sessionId: terminal.sessionId,
      turnId: terminal.turnId,
      userEventId: terminal.userEventId,
      assistantEventId: terminal.assistantEventId,
      userTranscript: terminal.userTranscript,
      assistantTranscript: terminal.assistantTranscript,
      startedAt: terminal.startedAt,
      completedAt: terminal.completedAt,
      turnOrdinal: turnOrdinal,
    );
  }

  V2VTranscriptTurn withTurnOrdinal(int value) => V2VTranscriptTurn(
        uid: uid,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
        sessionId: sessionId,
        turnId: turnId,
        userEventId: userEventId,
        assistantEventId: assistantEventId,
        userTranscript: userTranscript,
        assistantTranscript: assistantTranscript,
        startedAt: startedAt,
        completedAt: completedAt,
        turnOrdinal: value,
      );

  bool samePayload(V2VTranscriptTurn other) {
    final first = toJson();
    final second = other.toJson();
    if (turnOrdinal == null || other.turnOrdinal == null) {
      first.remove('turn_ordinal');
      second.remove('turn_ordinal');
    }
    return jsonEncode(first) == jsonEncode(second);
  }

  bool sameTerminalPayload(V2VTerminalTranscriptTurn other) =>
      sessionId == other.sessionId && sameProviderPayload(other);

  bool sameProviderPayload(V2VTerminalTranscriptTurn other) =>
      turnId == other.turnId &&
      userEventId == other.userEventId &&
      assistantEventId == other.assistantEventId &&
      userTranscript == other.userTranscript &&
      assistantTranscript == other.assistantTranscript &&
      startedAt == other.startedAt &&
      completedAt == other.completedAt;
}

typedef V2VTranscriptTurnWriter = Future<bool> Function(V2VTranscriptTurn turn);
typedef V2VAuthorityCurrent = bool Function();

class V2VAuthorityChangedException extends StateError {
  V2VAuthorityChangedException() : super('V2V authority changed during durable store operation');
}

class _V2VAuthorityLease {
  _V2VAuthorityLease(this.key, this.authorityGenerationAtCapture);

  final String key;
  final int authorityGenerationAtCapture;
}

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
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  });

  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent});

  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent});

  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  });
}

class MemoryV2VTurnDurableStore implements V2VTurnDurableStore {
  final Map<String, List<V2VTranscriptTurn>> _turnsByOwner = {};

  String _key(String uid, String ownerNamespace, String authorityFingerprint) =>
      '$uid\n$ownerNamespace\n$authorityFingerprint';

  @override
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    if (!isAuthorityCurrent()) throw V2VAuthorityChangedException();
    return List<V2VTranscriptTurn>.of(_turnsByOwner[_key(uid, ownerNamespace, authorityFingerprint)] ?? const []);
  }

  @override
  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) async {
    if (!isAuthorityCurrent()) throw V2VAuthorityChangedException();
    final turns = _turnsByOwner.putIfAbsent(_key(turn.uid, turn.ownerNamespace, turn.authorityFingerprint), () => []);
    final existing = turns.where((candidate) => candidate.turnId == turn.turnId).firstOrNull;
    if (existing == null) {
      turns.add(turn);
    } else if (!existing.samePayload(turn)) {
      throw StateError('Conflicting stable V2V turn identity');
    }
  }

  @override
  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) async {
    if (!isAuthorityCurrent()) throw V2VAuthorityChangedException();
    final key = _key(turn.uid, turn.ownerNamespace, turn.authorityFingerprint);
    final turns = _turnsByOwner[key];
    turns?.removeWhere((candidate) => candidate.samePayload(turn));
    if (turns?.isEmpty == true) _turnsByOwner.remove(key);
  }

  @override
  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    if (!isAuthorityCurrent()) throw V2VAuthorityChangedException();
    _turnsByOwner.remove(_key(uid, ownerNamespace, authorityFingerprint));
  }
}

class FileV2VTurnDurableStore implements V2VTurnDurableStore {
  FileV2VTurnDurableStore({
    Directory? baseDirectory,
    this.maxPendingTurns = 256,
    this.maxManifestBytes = 16 * 1024 * 1024,
    this.maxCleanupNamespacesPerLoad = 64,
    Future<void> Function(Directory directory)? afterCleanupMarkerWriteForTesting,
    Future<void> Function(Directory directory)? beforeCleanupDeleteForTesting,
  })  : assert(maxPendingTurns > 0),
        assert(maxManifestBytes > 0),
        assert(maxCleanupNamespacesPerLoad > 0),
        _afterCleanupMarkerWrite = afterCleanupMarkerWriteForTesting,
        _beforeCleanupDelete = beforeCleanupDeleteForTesting,
        _configuredBaseDirectory = baseDirectory;

  static const int _manifestVersion = 2;
  static const String _readyStatus = 'ready';
  static const String _cleanupRequiredStatus = 'cleanup_required';
  static final RegExp _ownerNamespacePattern = RegExp(r'^[0-9a-f]{24}$');
  static final RegExp _authorityFingerprintPattern = RegExp(r'^[0-9a-f]{64}$');

  final Directory? _configuredBaseDirectory;
  final int maxPendingTurns;
  final int maxManifestBytes;
  final int maxCleanupNamespacesPerLoad;
  final Future<void> Function(Directory directory)? _afterCleanupMarkerWrite;
  final Future<void> Function(Directory directory)? _beforeCleanupDelete;
  static final Map<String, Future<void>> _operationTails = {};
  String? _boundCanonicalBasePath;

  @override
  Future<List<V2VTranscriptTurn>> load({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) =>
      _serialized(ownerNamespace, () async {
        _requireAuthorityCurrent(isAuthorityCurrent);
        await _discardLegacyCoarseManifest(ownerNamespace, isAuthorityCurrent);
        await _sweepObsoleteAuthorities(
          uid: uid,
          ownerNamespace: ownerNamespace,
          currentAuthorityFingerprint: authorityFingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
        _requireAuthorityCurrent(isAuthorityCurrent);
        return List<V2VTranscriptTurn>.of(
          await _read(
            uid: uid,
            ownerNamespace: ownerNamespace,
            authorityFingerprint: authorityFingerprint,
            isAuthorityCurrent: isAuthorityCurrent,
          ),
        );
      });

  @override
  Future<void> put(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      _serialized(turn.ownerNamespace, () async {
        _requireAuthorityCurrent(isAuthorityCurrent);
        final turns = await _read(
          uid: turn.uid,
          ownerNamespace: turn.ownerNamespace,
          authorityFingerprint: turn.authorityFingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
        final existing = turns.where((candidate) => candidate.turnId == turn.turnId).firstOrNull;
        if (existing == null) {
          turns.add(turn);
        } else if (!existing.samePayload(turn)) {
          throw StateError('Conflicting stable V2V turn identity');
        } else {
          return;
        }
        await _write(
          uid: turn.uid,
          ownerNamespace: turn.ownerNamespace,
          authorityFingerprint: turn.authorityFingerprint,
          status: _readyStatus,
          turns: turns,
          isAuthorityCurrent: isAuthorityCurrent,
        );
      });

  @override
  Future<void> remove(V2VTranscriptTurn turn, {required V2VAuthorityCurrent isAuthorityCurrent}) =>
      _serialized(turn.ownerNamespace, () async {
        _requireAuthorityCurrent(isAuthorityCurrent);
        final turns = await _read(
          uid: turn.uid,
          ownerNamespace: turn.ownerNamespace,
          authorityFingerprint: turn.authorityFingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
        final originalLength = turns.length;
        turns.removeWhere((candidate) => candidate.samePayload(turn));
        if (turns.length == originalLength) return;
        await _write(
          uid: turn.uid,
          ownerNamespace: turn.ownerNamespace,
          authorityFingerprint: turn.authorityFingerprint,
          status: _readyStatus,
          turns: turns,
          isAuthorityCurrent: isAuthorityCurrent,
        );
      });

  @override
  Future<void> clearOwner({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) =>
      _serialized(ownerNamespace, () async {
        _requireAuthorityCurrent(isAuthorityCurrent);
        final directory = await _authorityDirectory(ownerNamespace, authorityFingerprint, create: false);
        final directoryType = await FileSystemEntity.type(directory.path, followLinks: false);
        _requireAuthorityCurrent(isAuthorityCurrent);
        if (directoryType == FileSystemEntityType.notFound) return;
        await _requireExactAuthorityDirectory(
          directory: directory,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: authorityFingerprint,
        );
        await _writeCleanupMarker(
          directory: directory,
          uid: uid,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: authorityFingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
        await _afterCleanupMarkerWrite?.call(directory);
        _requireAuthorityCurrent(isAuthorityCurrent);
        await _deleteAuthorityDirectory(
          directory: directory,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: authorityFingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
      });

  Future<T> _serialized<T>(String ownerNamespace, Future<T> Function() operation) {
    if (!_ownerNamespacePattern.hasMatch(ownerNamespace)) throw ArgumentError.value(ownerNamespace, 'ownerNamespace');
    final result = Completer<T>();
    final previous = _operationTails[ownerNamespace] ?? Future<void>.value();
    late final Future<void> tail;
    tail = previous.then((_) async {
      try {
        result.complete(await operation());
      } catch (error, stackTrace) {
        result.completeError(error, stackTrace);
      }
    });
    _operationTails[ownerNamespace] = tail;
    unawaited(
      tail.whenComplete(() {
        if (identical(_operationTails[ownerNamespace], tail)) _operationTails.remove(ownerNamespace);
      }),
    );
    return result.future;
  }

  void _requireAuthorityCurrent(V2VAuthorityCurrent isAuthorityCurrent) {
    if (!isAuthorityCurrent()) throw V2VAuthorityChangedException();
  }

  Future<String> _canonicalBasePath() async {
    final configured = _configuredBaseDirectory ?? await getApplicationSupportDirectory();
    final lexicalPath = p.normalize(p.absolute(configured.path));
    final lexicalType = await FileSystemEntity.type(lexicalPath, followLinks: false);
    if (lexicalType != FileSystemEntityType.directory) {
      throw const FileSystemException('Invalid V2V trusted base directory');
    }
    final canonicalPath = p.normalize(await Directory(lexicalPath).resolveSymbolicLinks());
    await _requireNonSymlinkDirectoryChain(canonicalPath);
    final bound = _boundCanonicalBasePath;
    if (bound != null && bound != canonicalPath) {
      throw const FileSystemException('V2V trusted base directory changed');
    }
    _boundCanonicalBasePath = canonicalPath;
    return canonicalPath;
  }

  Future<void> _requireNonSymlinkDirectoryChain(String path, {bool allowMissing = false}) async {
    final normalized = p.normalize(p.absolute(path));
    final parts = p.split(normalized);
    if (parts.isEmpty || !p.isAbsolute(normalized)) throw const FileSystemException('Invalid V2V absolute path');
    var current = parts.first;
    for (final part in parts.skip(1)) {
      current = p.join(current, part);
      final type = await FileSystemEntity.type(current, followLinks: false);
      if (type == FileSystemEntityType.notFound && allowMissing) return;
      if (type != FileSystemEntityType.directory) {
        throw const FileSystemException('Symlinked or invalid V2V path component');
      }
      final resolved = p.normalize(await Directory(current).resolveSymbolicLinks());
      if (resolved != p.normalize(current)) {
        throw const FileSystemException('Symlinked V2V path component');
      }
    }
  }

  Future<Directory> _safeDirectory(
    String path, {
    required bool create,
    V2VAuthorityCurrent? isAuthorityCurrent,
  }) async {
    if (create && isAuthorityCurrent == null) throw StateError('Authority lease required for V2V directory creation');
    final basePath = await _canonicalBasePath();
    final normalized = p.normalize(p.absolute(path));
    if (normalized != basePath && !p.isWithin(basePath, normalized)) {
      throw const FileSystemException('V2V path escapes trusted base');
    }
    if (create) {
      var current = basePath;
      final relative = p.relative(normalized, from: basePath);
      for (final component in p.split(relative).where((value) => value != '.')) {
        await _requireNonSymlinkDirectoryChain(current);
        current = p.join(current, component);
        final type = await FileSystemEntity.type(current, followLinks: false);
        if (type == FileSystemEntityType.notFound) {
          _requireAuthorityCurrent(isAuthorityCurrent!);
          await Directory(current).create();
        } else if (type != FileSystemEntityType.directory) {
          throw const FileSystemException('Symlinked or invalid V2V directory');
        }
        await _requireNonSymlinkDirectoryChain(current);
      }
    } else {
      await _requireNonSymlinkDirectoryChain(normalized, allowMissing: true);
    }
    return Directory(normalized);
  }

  Future<Directory> _ownerDirectory(
    String ownerNamespace, {
    required bool create,
    V2VAuthorityCurrent? isAuthorityCurrent,
  }) async {
    if (!_ownerNamespacePattern.hasMatch(ownerNamespace)) throw ArgumentError.value(ownerNamespace, 'ownerNamespace');
    final basePath = await _canonicalBasePath();
    return _safeDirectory(
      p.join(basePath, 'ella_v2v_turn_outbox', ownerNamespace),
      create: create,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  Future<Directory> _authorityDirectory(
    String ownerNamespace,
    String authorityFingerprint, {
    required bool create,
    V2VAuthorityCurrent? isAuthorityCurrent,
  }) async {
    if (!_authorityFingerprintPattern.hasMatch(authorityFingerprint)) {
      throw ArgumentError.value(authorityFingerprint, 'authorityFingerprint');
    }
    final ownerDirectory = await _ownerDirectory(
      ownerNamespace,
      create: create,
      isAuthorityCurrent: isAuthorityCurrent,
    );
    return _safeDirectory(
      p.join(ownerDirectory.path, authorityFingerprint),
      create: create,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  Future<FileSystemEntityType> _safeFileType(File file, Directory parent) async {
    await _requireNonSymlinkDirectoryChain(parent.path);
    final normalized = p.normalize(p.absolute(file.path));
    if (p.dirname(normalized) != p.normalize(parent.absolute.path)) {
      throw const FileSystemException('V2V file escapes validated directory');
    }
    final type = await FileSystemEntity.type(normalized, followLinks: false);
    if (type != FileSystemEntityType.notFound && type != FileSystemEntityType.file) {
      throw const FileSystemException('Symlinked or invalid V2V file');
    }
    return type;
  }

  Future<List<V2VTranscriptTurn>> _read({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    if (uid.trim().isEmpty) throw ArgumentError.value(uid, 'uid');
    _requireAuthorityCurrent(isAuthorityCurrent);
    final directory = await _authorityDirectory(ownerNamespace, authorityFingerprint, create: false);
    final directoryType = await FileSystemEntity.type(directory.path, followLinks: false);
    _requireAuthorityCurrent(isAuthorityCurrent);
    if (directoryType == FileSystemEntityType.notFound) return [];
    await _requireExactAuthorityDirectory(
      directory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    final manifest = File(p.join(directory.path, 'pending_turns.json'));
    final backup = File('${manifest.path}.bak');
    Object? lastError;
    for (final candidate in [manifest, backup]) {
      _requireAuthorityCurrent(isAuthorityCurrent);
      if (await _safeFileType(candidate, directory) == FileSystemEntityType.notFound) continue;
      try {
        _requireAuthorityCurrent(isAuthorityCurrent);
        final decoded = jsonDecode(await candidate.readAsString());
        _requireAuthorityCurrent(isAuthorityCurrent);
        await _requireExactAuthorityDirectory(
          directory: directory,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: authorityFingerprint,
        );
        if (decoded is! Map) throw const FormatException('Invalid V2V outbox manifest');
        final json = Map<String, dynamic>.from(decoded);
        if (json['version'] != _manifestVersion ||
            json['uid'] != uid ||
            json['owner_namespace'] != ownerNamespace ||
            json['authority_fingerprint'] != authorityFingerprint ||
            !const {_readyStatus, _cleanupRequiredStatus}.contains(json['status']) ||
            json['turns'] is! List) {
          throw const FormatException('Mismatched V2V outbox manifest');
        }
        final turns = <V2VTranscriptTurn>[];
        final turnIds = <String>{};
        final userEventIds = <String>{};
        final assistantEventIds = <String>{};
        final nextLegacyOrdinalBySession = <String, int>{};
        for (final value in json['turns'] as List) {
          final sessionId = value is Map ? value['session_id']?.toString() ?? '' : '';
          final fallbackTurnOrdinal = nextLegacyOrdinalBySession[sessionId] ?? 0;
          final turn = V2VTranscriptTurn.tryParse(
            value,
            expectedUid: uid,
            expectedOwnerNamespace: ownerNamespace,
            expectedAuthorityFingerprint: authorityFingerprint,
            fallbackTurnOrdinal: fallbackTurnOrdinal,
          );
          if (turn == null ||
              !turnIds.add(turn.turnId) ||
              !userEventIds.add(turn.userEventId) ||
              !assistantEventIds.add(turn.assistantEventId)) {
            throw const FormatException('Invalid V2V outbox turn');
          }
          turns.add(turn);
          final nextTurnOrdinal = turn.turnOrdinal! + 1;
          if (nextTurnOrdinal > (nextLegacyOrdinalBySession[turn.sessionId] ?? 0)) {
            nextLegacyOrdinalBySession[turn.sessionId] = nextTurnOrdinal;
          }
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
    required String authorityFingerprint,
    required String status,
    required List<V2VTranscriptTurn> turns,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    if (!const {_readyStatus, _cleanupRequiredStatus}.contains(status)) throw ArgumentError.value(status, 'status');
    if (turns.length > maxPendingTurns) throw StateError('V2V outbox capacity reached');
    final encoded = jsonEncode({
      'version': _manifestVersion,
      'uid': uid,
      'owner_namespace': ownerNamespace,
      'authority_fingerprint': authorityFingerprint,
      'status': status,
      'turns': turns.map((turn) => turn.toJson()).toList(),
    });
    if (utf8.encode(encoded).length > maxManifestBytes) throw StateError('V2V outbox byte capacity reached');
    _requireAuthorityCurrent(isAuthorityCurrent);
    final directory = await _authorityDirectory(
      ownerNamespace,
      authorityFingerprint,
      create: true,
      isAuthorityCurrent: isAuthorityCurrent,
    );
    _requireAuthorityCurrent(isAuthorityCurrent);
    final manifest = File(p.join(directory.path, 'pending_turns.json'));
    final backup = File('${manifest.path}.bak');
    final temporary = File('${manifest.path}.tmp');
    if (await _safeFileType(temporary, directory) == FileSystemEntityType.file) {
      _requireAuthorityCurrent(isAuthorityCurrent);
      await temporary.delete();
    }
    _requireAuthorityCurrent(isAuthorityCurrent);
    await temporary.writeAsString(encoded, flush: true);

    var movedOriginal = false;
    try {
      await _requireExactAuthorityDirectory(
        directory: directory,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
      );
      if (await _safeFileType(manifest, directory) == FileSystemEntityType.file) {
        if (await _safeFileType(backup, directory) == FileSystemEntityType.file) {
          _requireAuthorityCurrent(isAuthorityCurrent);
          await backup.delete();
        }
        _requireAuthorityCurrent(isAuthorityCurrent);
        await manifest.rename(backup.path);
        movedOriginal = true;
      }
      _requireAuthorityCurrent(isAuthorityCurrent);
      await _requireExactAuthorityDirectory(
        directory: directory,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
      );
      await temporary.rename(manifest.path);
      if (await _safeFileType(backup, directory) == FileSystemEntityType.file) {
        _requireAuthorityCurrent(isAuthorityCurrent);
        await backup.delete();
      }
    } catch (_) {
      if (isAuthorityCurrent() &&
          await _safeFileType(manifest, directory) == FileSystemEntityType.notFound &&
          movedOriginal &&
          await _safeFileType(backup, directory) == FileSystemEntityType.file) {
        await backup.rename(manifest.path);
      }
      rethrow;
    }
  }

  Future<void> _discardLegacyCoarseManifest(
    String ownerNamespace,
    V2VAuthorityCurrent isAuthorityCurrent,
  ) async {
    _requireAuthorityCurrent(isAuthorityCurrent);
    final ownerDirectory = await _ownerDirectory(ownerNamespace, create: false);
    final ownerType = await FileSystemEntity.type(ownerDirectory.path, followLinks: false);
    _requireAuthorityCurrent(isAuthorityCurrent);
    if (ownerType == FileSystemEntityType.notFound) return;
    await _requireNonSymlinkDirectoryChain(ownerDirectory.path);
    for (final name in const ['pending_turns.json', 'pending_turns.json.bak', 'pending_turns.json.tmp']) {
      final file = File(p.join(ownerDirectory.path, name));
      if (await _safeFileType(file, ownerDirectory) == FileSystemEntityType.file) {
        _requireAuthorityCurrent(isAuthorityCurrent);
        await file.delete();
      }
    }
  }

  Future<void> _sweepObsoleteAuthorities({
    required String uid,
    required String ownerNamespace,
    required String currentAuthorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    if (uid.trim().isEmpty) throw ArgumentError.value(uid, 'uid');
    _requireAuthorityCurrent(isAuthorityCurrent);
    await _authorityDirectory(ownerNamespace, currentAuthorityFingerprint, create: false);
    final ownerDirectory = await _ownerDirectory(ownerNamespace, create: false);
    final ownerDirectoryType = await FileSystemEntity.type(ownerDirectory.path, followLinks: false);
    _requireAuthorityCurrent(isAuthorityCurrent);
    if (ownerDirectoryType == FileSystemEntityType.notFound) return;
    await _requireNonSymlinkDirectoryChain(ownerDirectory.path);
    var cleanupAttempts = 0;
    await for (final entity in ownerDirectory.list(followLinks: false)) {
      _requireAuthorityCurrent(isAuthorityCurrent);
      final fingerprint = p.basename(entity.path);
      if (!_authorityFingerprintPattern.hasMatch(fingerprint) || fingerprint == currentAuthorityFingerprint) continue;
      if (cleanupAttempts >= maxCleanupNamespacesPerLoad) break;
      cleanupAttempts++;
      try {
        final exactDirectory = await _authorityDirectory(ownerNamespace, fingerprint, create: false);
        await _writeCleanupMarker(
          directory: exactDirectory,
          uid: uid,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: fingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
        await _afterCleanupMarkerWrite?.call(exactDirectory);
        _requireAuthorityCurrent(isAuthorityCurrent);
        await _deleteAuthorityDirectory(
          directory: exactDirectory,
          ownerNamespace: ownerNamespace,
          authorityFingerprint: fingerprint,
          isAuthorityCurrent: isAuthorityCurrent,
        );
      } on FileSystemException {
        // A successfully written marker makes the exact obsolete directory
        // discoverable on the next verified cold start without blocking the
        // current authority's payload hydration.
      }
    }
  }

  Future<void> _writeCleanupMarker({
    required Directory directory,
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    await _requireExactAuthorityDirectory(
      directory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    _requireAuthorityCurrent(isAuthorityCurrent);
    final marker = File(p.join(directory.path, 'cleanup_required.json'));
    final temporary = File('${marker.path}.tmp');
    if (await _safeFileType(temporary, directory) == FileSystemEntityType.file) {
      _requireAuthorityCurrent(isAuthorityCurrent);
      await temporary.delete();
    }
    _requireAuthorityCurrent(isAuthorityCurrent);
    await temporary.writeAsString(
        jsonEncode({
          'version': _manifestVersion,
          'uid': uid,
          'owner_namespace': ownerNamespace,
          'authority_fingerprint': authorityFingerprint,
          'status': _cleanupRequiredStatus,
        }),
        flush: true);
    await _requireExactAuthorityDirectory(
      directory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    if (await _safeFileType(marker, directory) == FileSystemEntityType.file) {
      _requireAuthorityCurrent(isAuthorityCurrent);
      await marker.delete();
    }
    _requireAuthorityCurrent(isAuthorityCurrent);
    await temporary.rename(marker.path);
  }

  Future<void> _requireExactAuthorityDirectory({
    required Directory directory,
    required String ownerNamespace,
    required String authorityFingerprint,
  }) async {
    final expected = await _authorityDirectory(ownerNamespace, authorityFingerprint, create: false);
    if (p.normalize(directory.absolute.path) != p.normalize(expected.absolute.path) ||
        await FileSystemEntity.type(directory.path, followLinks: false) != FileSystemEntityType.directory) {
      throw const FileSystemException('Invalid V2V authority directory');
    }
    await _requireNonSymlinkDirectoryChain(directory.path);
  }

  Future<void> _deleteAuthorityDirectory({
    required Directory directory,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    _requireAuthorityCurrent(isAuthorityCurrent);
    await _requireExactAuthorityDirectory(
      directory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    await _beforeCleanupDelete?.call(directory);
    _requireAuthorityCurrent(isAuthorityCurrent);
    await _requireExactAuthorityDirectory(
      directory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    await _deleteValidatedTree(
      path: directory.path,
      authorityDirectory: directory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
      isAuthorityCurrent: isAuthorityCurrent,
    );
  }

  Future<void> _deleteValidatedTree({
    required String path,
    required Directory authorityDirectory,
    required String ownerNamespace,
    required String authorityFingerprint,
    required V2VAuthorityCurrent isAuthorityCurrent,
  }) async {
    _requireAuthorityCurrent(isAuthorityCurrent);
    await _requireExactAuthorityDirectory(
      directory: authorityDirectory,
      ownerNamespace: ownerNamespace,
      authorityFingerprint: authorityFingerprint,
    );
    final normalized = p.normalize(p.absolute(path));
    final authorityPath = p.normalize(authorityDirectory.absolute.path);
    if (normalized != authorityPath && !p.isWithin(authorityPath, normalized)) {
      throw const FileSystemException('V2V deletion escapes authority directory');
    }
    final type = await FileSystemEntity.type(normalized, followLinks: false);
    _requireAuthorityCurrent(isAuthorityCurrent);
    if (type == FileSystemEntityType.notFound) return;
    if (type == FileSystemEntityType.link) {
      await _requireNonSymlinkDirectoryChain(p.dirname(normalized));
      _requireAuthorityCurrent(isAuthorityCurrent);
      await Link(normalized).delete();
      return;
    }
    if (type == FileSystemEntityType.file) {
      await _requireNonSymlinkDirectoryChain(p.dirname(normalized));
      _requireAuthorityCurrent(isAuthorityCurrent);
      await File(normalized).delete();
      return;
    }
    if (type != FileSystemEntityType.directory) {
      throw const FileSystemException('Invalid V2V deletion entry');
    }
    await _requireNonSymlinkDirectoryChain(normalized);
    final children = await Directory(normalized).list(followLinks: false).toList();
    _requireAuthorityCurrent(isAuthorityCurrent);
    children.sort((first, second) => first.path.compareTo(second.path));
    for (final child in children) {
      await _deleteValidatedTree(
        path: child.path,
        authorityDirectory: authorityDirectory,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
        isAuthorityCurrent: isAuthorityCurrent,
      );
    }
    _requireAuthorityCurrent(isAuthorityCurrent);
    if (normalized == authorityPath) {
      await _requireExactAuthorityDirectory(
        directory: authorityDirectory,
        ownerNamespace: ownerNamespace,
        authorityFingerprint: authorityFingerprint,
      );
    } else {
      await _requireNonSymlinkDirectoryChain(normalized);
    }
    _requireAuthorityCurrent(isAuthorityCurrent);
    await Directory(normalized).delete();
  }
}

class V2VTurnReconciler {
  V2VTurnReconciler({
    required V2VTranscriptTurnWriter writer,
    V2VTurnDurableStore? durableStore,
    void Function()? onWriteFailure,
    List<Duration> unauthorizedCleanupRetryDelays = const [
      Duration(milliseconds: 100),
      Duration(milliseconds: 500),
      Duration(seconds: 2),
      Duration(seconds: 10),
      Duration(seconds: 30),
    ],
  })  : _durableStore = durableStore ?? MemoryV2VTurnDurableStore(),
        _writer = writer,
        _onWriteFailure = onWriteFailure,
        _unauthorizedCleanupRetryDelays = List<Duration>.unmodifiable(unauthorizedCleanupRetryDelays);

  final V2VTurnDurableStore _durableStore;
  final V2VTranscriptTurnWriter _writer;
  final void Function()? _onWriteFailure;
  final List<Duration> _unauthorizedCleanupRetryDelays;
  final Map<String, _V2VSessionTurns> _sessions = {};
  static final Map<String, _V2VAuthorityLease> _authorityLeases = {};
  String _activeSessionId = '';

  int get sessionCountForTesting => _sessions.length;

  int pendingTurnCountForTesting(String sessionId) => _sessions[sessionId]?.pendingTurns.length ?? 0;

  String _authorityKey(String uid, String ownerNamespace, String authorityFingerprint) =>
      '$uid\n$ownerNamespace\n$authorityFingerprint';

  bool _ownsAuthorityLease(_V2VSessionTurns state) =>
      state.authorityGenerationAtCapture == state.authorityLease.authorityGenerationAtCapture &&
      identical(_authorityLeases[state.authorityLease.key], state.authorityLease);

  void _retireReplacedAuthorityStates(_V2VAuthorityLease replacement) {
    for (final state in _sessions.values.toList()) {
      if (state.authorityLease.key != replacement.key || identical(state.authorityLease, replacement)) continue;
      state
        ..unauthorized = true
        ..unauthorizedClearPending = false
        ..ended = true
        ..rerunRequested = false
        ..pendingTurns.clear();
      state.cleanupRetryTimer?.cancel();
      state.cleanupRetryTimer = null;
      if (state.cleanupRetryCompleter?.isCompleted == false) state.cleanupRetryCompleter!.complete();
      state.cleanupRetryCompleter = null;
      state.cleanupRetryFuture = null;
      if (identical(_sessions[state.sessionId], state)) _sessions.remove(state.sessionId);
      if (_activeSessionId == state.sessionId) _activeSessionId = '';
    }
  }

  Future<bool> beginSession({
    required String uid,
    required String ownerNamespace,
    required String authorityFingerprint,
    required String sessionId,
    required bool Function() isAuthorityCurrent,
    int authorityGenerationAtCapture = 0,
  }) async {
    final normalizedUid = uid.trim();
    final normalizedOwnerNamespace = ownerNamespace.trim();
    final normalizedAuthorityFingerprint = authorityFingerprint.trim();
    final normalizedSessionId = sessionId.trim();
    if (normalizedUid.isEmpty ||
        !FileV2VTurnDurableStore._ownerNamespacePattern.hasMatch(normalizedOwnerNamespace) ||
        !FileV2VTurnDurableStore._authorityFingerprintPattern.hasMatch(normalizedAuthorityFingerprint) ||
        normalizedSessionId.isEmpty ||
        authorityGenerationAtCapture < 0 ||
        !isAuthorityCurrent()) {
      return false;
    }
    final authorityKey = _authorityKey(normalizedUid, normalizedOwnerNamespace, normalizedAuthorityFingerprint);
    final authorityLease = _V2VAuthorityLease(authorityKey, authorityGenerationAtCapture);
    _authorityLeases[authorityKey] = authorityLease;
    _retireReplacedAuthorityStates(authorityLease);
    bool exactAuthorityIsCurrent() => identical(_authorityLeases[authorityKey], authorityLease) && isAuthorityCurrent();
    late final List<V2VTranscriptTurn> restored;
    try {
      restored = await _durableStore.load(
        uid: normalizedUid,
        ownerNamespace: normalizedOwnerNamespace,
        authorityFingerprint: normalizedAuthorityFingerprint,
        isAuthorityCurrent: exactAuthorityIsCurrent,
      );
    } catch (_) {
      _onWriteFailure?.call();
      return false;
    }
    if (!exactAuthorityIsCurrent()) return false;
    for (final turn in restored) {
      final restoredState = _sessions.putIfAbsent(
        turn.sessionId,
        () => _V2VSessionTurns(
          uid: normalizedUid,
          ownerNamespace: normalizedOwnerNamespace,
          authorityFingerprint: normalizedAuthorityFingerprint,
          sessionId: turn.sessionId,
          authorityGenerationAtCapture: authorityGenerationAtCapture,
          authorityLease: authorityLease,
          capturedAuthorityIsCurrent: isAuthorityCurrent,
          isAuthorityCurrent: exactAuthorityIsCurrent,
        )..ended = true,
      );
      if (restoredState.uid != normalizedUid ||
          restoredState.ownerNamespace != normalizedOwnerNamespace ||
          restoredState.authorityFingerprint != normalizedAuthorityFingerprint) {
        return false;
      }
      restoredState
        ..capturedAuthorityIsCurrent = isAuthorityCurrent
        ..isAuthorityCurrent = exactAuthorityIsCurrent;
      if (!_rememberTurn(restoredState, turn)) return false;
    }
    final existing = _sessions[normalizedSessionId];
    if (existing != null &&
        (existing.uid != normalizedUid ||
            existing.ownerNamespace != normalizedOwnerNamespace ||
            existing.authorityFingerprint != normalizedAuthorityFingerprint ||
            existing.unauthorized)) {
      return false;
    }
    final state = existing ??
        _V2VSessionTurns(
          uid: normalizedUid,
          ownerNamespace: normalizedOwnerNamespace,
          authorityFingerprint: normalizedAuthorityFingerprint,
          sessionId: normalizedSessionId,
          authorityGenerationAtCapture: authorityGenerationAtCapture,
          authorityLease: authorityLease,
          capturedAuthorityIsCurrent: isAuthorityCurrent,
          isAuthorityCurrent: exactAuthorityIsCurrent,
        );
    state
      ..capturedAuthorityIsCurrent = isAuthorityCurrent
      ..isAuthorityCurrent = exactAuthorityIsCurrent
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
    return _addTerminalTurn(sessionId, terminal, acceptExactDuplicate: false);
  }

  Future<bool> persistTerminalTurnForAck(String sessionId, V2VTerminalTranscriptTurn terminal) {
    return _addTerminalTurn(sessionId, terminal, acceptExactDuplicate: true);
  }

  Future<bool> _addTerminalTurn(
    String sessionId,
    V2VTerminalTranscriptTurn terminal, {
    required bool acceptExactDuplicate,
  }) {
    final state = _authorizedActiveState(sessionId);
    if (state == null || terminal.sessionId != state.sessionId) return Future<bool>.value(false);
    final existing = state.turnsById[terminal.turnId];
    if (existing != null) {
      if (!acceptExactDuplicate || !existing.sameTerminalPayload(terminal)) return Future<bool>.value(false);
      return state.enqueueFutureByTurnId[terminal.turnId] ?? Future<bool>.value(true);
    }
    for (final candidate in _sessions.values) {
      if (identical(candidate, state) ||
          candidate.unauthorized ||
          !identical(candidate.authorityLease, state.authorityLease)) {
        continue;
      }
      final ownerExisting = candidate.turnsById[terminal.turnId];
      if (ownerExisting == null) continue;
      if (!acceptExactDuplicate || !ownerExisting.sameProviderPayload(terminal)) return Future<bool>.value(false);
      return candidate.enqueueFutureByTurnId[terminal.turnId] ?? Future<bool>.value(true);
    }
    if (state.userEventIds.contains(terminal.userEventId) ||
        state.assistantEventIds.contains(terminal.assistantEventId)) {
      return Future<bool>.value(false);
    }
    final turn = V2VTranscriptTurn(
      uid: state.uid,
      ownerNamespace: state.ownerNamespace,
      authorityFingerprint: state.authorityFingerprint,
      sessionId: state.sessionId,
      turnId: terminal.turnId,
      userEventId: terminal.userEventId,
      assistantEventId: terminal.assistantEventId,
      userTranscript: terminal.userTranscript,
      assistantTranscript: terminal.assistantTranscript,
      startedAt: terminal.startedAt,
      completedAt: terminal.completedAt,
      turnOrdinal: state.nextTurnOrdinal++,
    );
    if (!_reserveTurn(state, turn)) return Future<bool>.value(false);

    late final Future<bool> enqueueFuture;
    enqueueFuture = _enqueueAcceptedTurn(state, turn);
    state.enqueueFutures.add(enqueueFuture);
    state.enqueueFutureByTurnId[turn.turnId] = enqueueFuture;
    unawaited(
      enqueueFuture.then<void>((_) {
        state.enqueueFutures.remove(enqueueFuture);
        if (identical(state.enqueueFutureByTurnId[turn.turnId], enqueueFuture)) {
          state.enqueueFutureByTurnId.remove(turn.turnId);
        }
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
        ..._sessions.values.map((state) => state.cleanupRetryFuture).whereType<Future<void>>(),
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
        if (state.rerunRequested) {
          state.rerunRequested = false;
          if (state.unauthorized && state.unauthorizedClearPending) {
            _scheduleUnauthorizedCleanupRetry(state);
            return;
          }
          if (state.pendingTurns.isNotEmpty) {
            _scheduleDrain(state);
            return;
          }
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
        await _durableStore.remove(turn, isAuthorityCurrent: state.isAuthorityCurrent);
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
    if (state.cleanupRetryFuture != null) return;
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
    if (!_ownsAuthorityLease(state)) {
      state
        ..unauthorizedClearPending = false
        ..pendingTurns.clear();
      _maybeEvict(state);
      return;
    }
    try {
      await _durableStore.clearOwner(
        uid: state.uid,
        ownerNamespace: state.ownerNamespace,
        authorityFingerprint: state.authorityFingerprint,
        isAuthorityCurrent: () =>
            _ownsAuthorityLease(state) && state.unauthorized && !state.capturedAuthorityIsCurrent(),
      );
      if (!_ownsAuthorityLease(state)) {
        state
          ..unauthorizedClearPending = false
          ..pendingTurns.clear();
        _maybeEvict(state);
        return;
      }
      final sameOwnerStates = _sessions.values
          .where(
            (candidate) =>
                candidate.uid == state.uid &&
                candidate.ownerNamespace == state.ownerNamespace &&
                candidate.authorityFingerprint == state.authorityFingerprint &&
                identical(candidate.authorityLease, state.authorityLease),
          )
          .toList();
      for (final candidate in sameOwnerStates) {
        candidate.unauthorized = true;
        candidate.unauthorizedClearPending = false;
        candidate.cleanupRetryAttempt = 0;
        candidate.cleanupRetryTimer?.cancel();
        candidate.cleanupRetryTimer = null;
        if (candidate.cleanupRetryCompleter?.isCompleted == false) candidate.cleanupRetryCompleter!.complete();
        candidate.cleanupRetryCompleter = null;
        candidate.cleanupRetryFuture = null;
        candidate.pendingTurns.clear();
        candidate.failedAtRevision = -1;
        _maybeEvict(candidate);
      }
    } catch (_) {
      if (!_ownsAuthorityLease(state)) {
        state
          ..unauthorizedClearPending = false
          ..pendingTurns.clear();
        _maybeEvict(state);
        return;
      }
      state.unauthorizedClearPending = true;
      state.failedAtRevision = state.revision;
      _onWriteFailure?.call();
      state.rerunRequested = true;
    }
  }

  void _scheduleUnauthorizedCleanupRetry(_V2VSessionTurns state) {
    if (!state.unauthorizedClearPending || state.cleanupRetryFuture != null) return;
    if (state.cleanupRetryAttempt >= _unauthorizedCleanupRetryDelays.length) return;
    final delay = _unauthorizedCleanupRetryDelays[state.cleanupRetryAttempt++];
    final completer = Completer<void>();
    state.cleanupRetryCompleter = completer;
    state.cleanupRetryFuture = completer.future;
    state.cleanupRetryTimer = Timer(delay, () {
      state.cleanupRetryTimer = null;
      state.cleanupRetryCompleter = null;
      state.cleanupRetryFuture = null;
      if (state.unauthorizedClearPending && state.enqueueFutures.isEmpty && state.drainFuture == null) {
        state.revision++;
        _scheduleDrain(state);
      }
      completer.complete();
    });
  }

  Future<bool> _enqueueAcceptedTurn(_V2VSessionTurns state, V2VTranscriptTurn turn) async {
    try {
      await _durableStore.put(turn, isAuthorityCurrent: state.isAuthorityCurrent);
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
    state.turnsById[turn.turnId] = turn;
    return true;
  }

  void _releaseTurnReservation(_V2VSessionTurns state, V2VTranscriptTurn turn) {
    state.turnIds.remove(turn.turnId);
    state.userEventIds.remove(turn.userEventId);
    state.assistantEventIds.remove(turn.assistantEventId);
    state.turnsById.remove(turn.turnId);
  }

  bool _rememberTurn(_V2VSessionTurns state, V2VTranscriptTurn turn) {
    final normalizedTurn = turn.turnOrdinal == null ? turn.withTurnOrdinal(state.nextTurnOrdinal) : turn;
    final existing = state.pendingTurns.where((candidate) => candidate.turnId == normalizedTurn.turnId).firstOrNull;
    if (existing != null) return existing.samePayload(normalizedTurn);
    if (!_reserveTurn(state, normalizedTurn)) return false;
    state.pendingTurns.add(normalizedTurn);
    final followingOrdinal = normalizedTurn.turnOrdinal! + 1;
    if (followingOrdinal > state.nextTurnOrdinal) state.nextTurnOrdinal = followingOrdinal;
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
    required this.authorityFingerprint,
    required this.sessionId,
    required this.authorityGenerationAtCapture,
    required this.authorityLease,
    required this.capturedAuthorityIsCurrent,
    required this.isAuthorityCurrent,
  });

  final String uid;
  final String ownerNamespace;
  final String authorityFingerprint;
  final String sessionId;
  final int authorityGenerationAtCapture;
  final _V2VAuthorityLease authorityLease;
  bool Function() capturedAuthorityIsCurrent;
  bool Function() isAuthorityCurrent;
  final List<V2VTranscriptTurn> pendingTurns = [];
  final Set<String> turnIds = {};
  final Set<String> userEventIds = {};
  final Set<String> assistantEventIds = {};
  final Map<String, V2VTranscriptTurn> turnsById = {};
  int revision = 0;
  int nextTurnOrdinal = 0;
  int failedAtRevision = -1;
  bool rerunRequested = false;
  bool ended = false;
  bool unauthorized = false;
  bool unauthorizedClearPending = false;
  int cleanupRetryAttempt = 0;
  Timer? cleanupRetryTimer;
  Completer<void>? cleanupRetryCompleter;
  Future<void>? cleanupRetryFuture;
  final Set<Future<bool>> enqueueFutures = {};
  final Map<String, Future<bool>> enqueueFutureByTurnId = {};
  Future<void>? drainFuture;
}
