import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef RetainedSummaryLoader = Future<ServerConversation?> Function(
  String id, {
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});

/// Exact stored historical text. Never replaces the canonical structured summary.
class RetainedSummary {
  const RetainedSummary(this.id, this.title, this.overview, this.emoji, this.category);
  final String id, title, overview, emoji, category;
}

class RetainedSummaryCatalog {
  RetainedSummaryCatalog._(this.versions, this.error);
  final List<RetainedSummary> versions;
  final String? error;
  static bool _text(Object? value) => value is String && value.trim().isNotEmpty;
  factory RetainedSummaryCatalog.parse(Object? raw, Object? activeId) {
    RetainedSummaryCatalog fail(String error) => RetainedSummaryCatalog._(const [], error);
    if (raw is! List || raw.isEmpty) return fail('stock_missing');
    final ids = <String>{};
    final active = <String>[];
    final stock = <RetainedSummary>[];
    for (final item in raw) {
      if (item is! Map || !_text(item['id'])) return fail('versions_invalid');
      final id = item['id'] as String;
      if (!ids.add(id)) return fail('duplicate_version_id');
      if (!_text(item['source']) || !_text(item['kind']) || item['is_active'] is! bool) {
        return fail('versions_invalid');
      }
      if (item['is_active'] == true) active.add(id);
      if (item['source'] == 'omi' && item['kind'] == 'generated') {
        if (!['title', 'overview', 'emoji', 'category'].every((key) => _text(item[key]))) {
          return fail('stock_content_invalid');
        }
        stock.add(RetainedSummary(id, item['title'], item['overview'], item['emoji'], item['category']));
      }
    }
    if (!_text(activeId) || active.length != 1 || active.single != activeId) {
      return fail('active_authority_invalid');
    }
    return RetainedSummaryCatalog._(List.unmodifiable(stock), stock.isEmpty ? 'stock_missing' : null);
  }
  RetainedSummary? select([String? id]) {
    if (error != null) return null;
    if (id == null) return versions.length == 1 ? versions.single : null;
    for (final version in versions) {
      if (version.id == id) return version;
    }
    return null;
  }
}

/// Keeps its authority lease alive for the entire presentation lifetime.
/// Selection is local; only mounting/changing memory performs an owner-scoped GET.
class RetainedSummaryController extends ChangeNotifier {
  RetainedSummaryController({ActiveAccountAuthorityProvider? authority, RetainedSummaryLoader? loader})
      : _authority = authority ?? WalOwnerAuthority.activeAccount,
        _loader = loader ?? getConversationById;
  final ActiveAccountAuthorityProvider _authority;
  final RetainedSummaryLoader _loader;
  EllaAccountCommitLease? _lease;
  Timer? _authorityWatch;
  RetainedSummaryCatalog? _catalog;
  String? _selectedId;
  int _generation = 0;
  bool _disposed = false;
  bool get _current => !_disposed && (_lease?.isCurrent ?? false);
  List<RetainedSummary> get versions => _current ? (_catalog?.versions ?? const []) : const [];
  RetainedSummary? get selected => _current ? _catalog?.select(_selectedId) : null;
  void _revoke() {
    _authorityWatch?.cancel();
    _authorityWatch = null;
    _generation++;
    _catalog = null;
    _selectedId = null;
    _lease?.close();
    _lease = null;
    if (!_disposed) notifyListeners();
  }

  Future<void> load(String id) async {
    _revoke();
    final lease = EllaAccountCommitBarrier.begin(authorityProvider: _authority, onInvalidated: _revoke);
    if (lease == null || _disposed || id.isEmpty) {
      lease?.close();
      return;
    }
    _lease = lease;
    // Some consent/provisioning retirements do not emit an account transition.
    // Bound their visible stale-text window without network or write activity.
    _authorityWatch = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (!lease.isCurrent) _revoke();
    });
    final generation = _generation;
    try {
      if (!lease.isCurrent) return;
      final memory = await _loader(id, expectedAuthenticatedUid: lease.uid, exactAuthority: lease);
      if (!_current ||
          generation != _generation ||
          memory == null ||
          memory.id != id ||
          (memory.summaryVersionBodyUidPresent && memory.summaryVersionBodyUid != lease.uid)) {
        return;
      }
      // Detach from mutable cached models. The API's trusted owner-scoped GET is authority.
      final snapshot = jsonDecode(jsonEncode(memory.summaryVersions));
      _catalog =
          RetainedSummaryCatalog.parse(snapshot, memory.summaryVersionActiveAuthority ?? memory.activeSummaryVersionId);
      notifyListeners();
    } catch (_) {
      if (_current && generation == _generation) {
        _catalog = null;
        notifyListeners();
      }
    }
  }

  void select(String id) {
    if (!_current || _catalog?.select(id) == null) return;
    _selectedId = id;
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _revoke();
    super.dispose();
  }
}
