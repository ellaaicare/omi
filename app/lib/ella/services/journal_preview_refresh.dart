import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef JournalPreviewLoader = Future<ServerConversation?> Function(
  String id, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});

/// Stable content witness also catches in-place mutations to a retained row.
String journalPreviewRowWitness(ServerConversation row) =>
    jsonEncode([row.summaryVersionBodyUidPresent, row.summaryVersionBodyUid, row.toJson()]);

typedef JournalPreviewCommit = bool Function(
    ServerConversation incoming, ServerConversation expectedRow, String expectedWitness);

String? _version(ServerConversation row) {
  final raw = row.summaryVersionActiveAuthority ?? row.activeSummaryVersionId;
  return raw is String && raw.trim().isNotEmpty ? raw : null;
}

bool _pairMatches(ServerConversation row, Map<String, dynamic> version) =>
    version['title'] == row.structured.title && version['overview'] == row.structured.overview;

/// UUIDs are opaque. Only exact equality or explicit retained parent lineage
/// establishes a version relationship; timestamps and list order do not.
bool journalPreviewVersionMayAdvance(ServerConversation expected, ServerConversation incoming) {
  final before = _version(expected), after = _version(incoming);
  for (final row in [expected, incoming]) {
    final raw = row.summaryVersionActiveAuthority;
    if (raw != null && (raw is! String || raw.trim().isEmpty)) return false;
  }
  if (after == null) {
    return before == null &&
        (incoming.summaryVersions == null ||
            incoming.summaryVersions is List && (incoming.summaryVersions as List).isEmpty) &&
        (expected.summaryVersions == null ||
            expected.summaryVersions is List && (expected.summaryVersions as List).isEmpty);
  }
  final raw = incoming.summaryVersions;
  if (raw is! List || raw.isEmpty) return false;
  final versions = <String, Map<String, dynamic>>{};
  var active = 0;
  for (final item in raw) {
    if (item is! Map || item['id'] is! String || (item['id'] as String).isEmpty) return false;
    final id = item['id'] as String;
    if (versions.containsKey(id)) return false;
    if (item['based_on_version_id'] != null &&
        (item['based_on_version_id'] is! String || (item['based_on_version_id'] as String).isEmpty)) return false;
    if (item['is_active'] is! bool) return false;
    if (item['conversation_id'] != null && item['conversation_id'] != incoming.id) return false;
    if (item['is_active'] == true) {
      active++;
      if (id != after) return false;
    }
    versions[id] = Map<String, dynamic>.from(item);
  }
  final children = <String, int>{};
  for (final version in versions.values) {
    final parent = version['based_on_version_id'];
    if (parent != null) {
      if (!versions.containsKey(parent)) return false;
      children[parent] = (children[parent] ?? 0) + 1;
      if (children[parent]! > 1) return false;
    }
    final chain = <String>{};
    String? node = version['id'];
    while (node != null) {
      if (!chain.add(node)) return false;
      node = versions[node]?['based_on_version_id'];
    }
  }
  final selected = versions[after];
  if (active != 1 || selected == null || !_pairMatches(incoming, selected)) return false;
  if (before == null) {
    return expected.summaryVersions == null ||
        expected.summaryVersions is List && (expected.summaryVersions as List).isEmpty;
  }
  final previous = versions[before];
  if (previous == null || !_pairMatches(expected, previous)) return false;
  final visited = <String>{};
  String? cursor = after;
  while (cursor != null && visited.add(cursor)) {
    if (cursor == before) return true;
    final parent = versions[cursor]?['based_on_version_id'];
    cursor = parent is String && parent.isNotEmpty ? parent : null;
  }
  return false;
}

class _PreviewAuthority implements AccountCommitAuthority {
  _PreviewAuthority(this.authority, this.presentationCurrent);
  final ExactAccountAuthorityVerifier authority;
  final bool Function() presentationCurrent;
  @override
  String get uid => authority.uid;
  @override
  bool isCurrent() => presentationCurrent() && authority.isExactCurrent();
  @override
  bool isExactCurrent() => isCurrent();
}

/// A preview never promotes a cached row to freshly loaded summary authority.
/// One detached exact-owner response supplies title, overview and source words.
enum JournalPreviewFailure { unavailable, changedDuringRead, invalidResponse }

class JournalPreviewRefresh extends ChangeNotifier {
  JournalPreviewRefresh({
    required this.id,
    required ExactAccountAuthorityVerifier authority,
    required bool Function() presentationCurrent,
    JournalPreviewLoader? loader,
    required this.currentRow,
    required this.commitRow,
  })  : _authority = _PreviewAuthority(authority, presentationCurrent),
        _loader = loader ?? getConversationById;
  final String id;
  final _PreviewAuthority _authority;
  final JournalPreviewLoader _loader;
  final ServerConversation? Function() currentRow;
  final JournalPreviewCommit commitRow;
  EllaAccountCommitLease? _lease;
  Timer? _watch;
  int _generation = 0;
  bool _disposed = false;
  bool loading = false;
  JournalPreviewFailure? failure;
  ServerConversation? _memory;
  String? _acceptedWitness;
  String? _observedRowWitness;
  bool get isCurrent => !_disposed && (_lease?.isCurrent ?? false);
  ServerConversation? get memory => isCurrent &&
          _memory != null &&
          identical(currentRow(), _memory) &&
          journalPreviewRowWitness(_memory!) == _acceptedWitness
      ? _memory
      : null;

  // Retained content is useful while a GET is pending or unavailable. It is
  // never fresh-read authority, and disappears with this exact account lease.
  ServerConversation? get retainedMemory {
    if (!isCurrent) return null;
    final row = currentRow();
    return row != null &&
            row.id == id &&
            (!row.summaryVersionBodyUidPresent || row.summaryVersionBodyUid == _lease!.uid)
        ? row
        : null;
  }

  void _retire() {
    _generation++;
    _watch?.cancel();
    _watch = null;
    _lease?.close();
    _lease = null;
    _memory = null;
    _acceptedWitness = null;
    _observedRowWitness = null;
    loading = false;
    failure = null;
    if (!_disposed) notifyListeners();
  }

  Future<void> load() async {
    if (_disposed) return;
    _retire();
    if (id.isEmpty) return;
    final lease = EllaAccountCommitBarrier.begin(authorityProvider: () => _authority, onInvalidated: _retire);
    if (lease == null) return;
    _lease = lease;
    final expectedRow = currentRow();
    if (expectedRow == null || expectedRow.id != id) {
      _retire();
      return;
    }
    final expectedWitness = journalPreviewRowWitness(expectedRow);
    _observedRowWitness = expectedWitness;
    final generation = _generation;
    bool current() => isCurrent && generation == _generation;
    _watch = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!lease.isCurrent) {
        _retire();
      } else if (_memory != null && memory == null) {
        _memory = null;
        _acceptedWitness = null;
        notifyListeners();
      } else {
        final retained = retainedMemory;
        final witness = retained == null ? null : journalPreviewRowWitness(retained);
        if (witness != _observedRowWitness) {
          _observedRowWitness = witness;
          notifyListeners();
        }
      }
    });
    loading = true;
    notifyListeners();
    try {
      final response = await _loader(id, expectedAuthenticatedUid: lease.uid, exactAuthority: lease);
      if (!current()) return;
      if (response != null &&
          response.id == id &&
          (!response.summaryVersionBodyUidPresent || response.summaryVersionBodyUid == lease.uid)) {
        final snapshot = ServerConversation.fromJson(jsonDecode(jsonEncode(response.toJson())));
        final liveRow = currentRow();
        if (!current()) return;
        if (liveRow == null ||
            (liveRow.summaryVersionBodyUidPresent && liveRow.summaryVersionBodyUid != lease.uid) ||
            journalPreviewRowWitness(liveRow) != expectedWitness ||
            !journalPreviewVersionMayAdvance(liveRow, snapshot)) {
          failure = JournalPreviewFailure.changedDuringRead;
          return;
        }
        // No await between validation and conditional provider commit. The
        // callback repeats the row witness check at the actual write boundary.
        final acceptedWitness = journalPreviewRowWitness(snapshot);
        if (!commitRow(snapshot, liveRow, expectedWitness)) {
          failure = JournalPreviewFailure.changedDuringRead;
          return;
        }
        if (!current() || !identical(currentRow(), snapshot) || journalPreviewRowWitness(snapshot) != acceptedWitness)
          return;
        _memory = snapshot;
        _acceptedWitness = acceptedWitness;
      } else {
        failure = response == null ? JournalPreviewFailure.unavailable : JournalPreviewFailure.invalidResponse;
      }
    } catch (_) {
      // Closed local classification only: no owner, response or exception text.
      if (current()) failure = JournalPreviewFailure.unavailable;
    } finally {
      if (current()) {
        loading = false;
        notifyListeners();
      }
    }
  }

  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _retire();
    super.dispose();
  }
}
