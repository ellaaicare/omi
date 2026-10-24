import 'dart:convert';
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef IrisReminderPageReader = Future<ActionItemsResponse> Function(
    int offset, ExactAccountAuthorityVerifier authority);

/// Reuses the existing action-items endpoint under an exact owner lease. This
/// view never adopts the legacy provider's unscoped in-memory cache.
class IrisJournalReminders extends ChangeNotifier {
  IrisJournalReminders({this.readPage = _readPage, this.authorityProvider = WalOwnerAuthority.active}) {
    _retirementTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (_authority != null && !_authority!.isExactCurrent()) retire();
    });
  }
  late final Timer _retirementTimer;
  bool Function() captureVisibility() {
    final authority = _authority, generation = _generation;
    return () =>
        authority != null &&
        identical(authority, _authority) &&
        generation == _generation &&
        authority.isExactCurrent();
  }

  final IrisReminderPageReader readPage;
  final ExactAccountAuthorityVerifier? Function() authorityProvider;
  ExactAccountAuthorityVerifier? _authority;
  List<ActionItemWithMetadata> _items = [];
  int _generation = 0;
  bool loading = false, unavailable = false, hasMore = false;
  List<ActionItemWithMetadata> get items => _authority?.isExactCurrent() == true ? List.unmodifiable(_items) : const [];
  void retire() {
    _generation++;
    _authority = null;
    _items = [];
    loading = false;
    unavailable = false;
    hasMore = false;
    notifyListeners();
  }

  Future<void> load() => _load(append: false);
  Future<void> loadMore() => _load(append: true);

  Future<void> _load({required bool append}) async {
    if (append && (loading || !hasMore || _authority?.isExactCurrent() != true)) return;
    final retained = append ? items : const <ActionItemWithMetadata>[];

    final generation = ++_generation;
    final authority = append ? _authority : authorityProvider();
    if (authority == null || !authority.isExactCurrent()) {
      retire();
      return;
    }
    _authority = authority;
    _items = retained;
    loading = true;
    unavailable = false;
    hasMore = false;
    notifyListeners();
    bool current() => generation == _generation && authority.isExactCurrent();
    try {
      final loaded = List<ActionItemWithMetadata>.of(_items);
      bool more = true;
      for (int page = 0; page < 5 && more; page++) {
        final response = await readPage(loaded.length, authority).timeout(const Duration(seconds: 10));
        if (!current()) return;
        if (response.hasMore && response.actionItems.isEmpty) throw StateError('incomplete reminder page');
        loaded.addAll(response.actionItems);
        more = response.hasMore;
      }
      if (!current()) return;
      final ids = <String>{};
      _items = loaded.where((item) => ids.add(item.id)).toList();
      hasMore = more;
    } catch (_) {
      if (current()) {
        unavailable = true;
        hasMore = append;
      }
    } finally {
      if (current()) {
        loading = false;
        notifyListeners();
      }
    }
  }

  static Future<ActionItemsResponse> _readPage(int offset, ExactAccountAuthorityVerifier authority) async {
    if (!authority.isExactCurrent()) throw StateError('reminder authority retired');
    final response = await makeApiCall(
        url: '${Env.apiBaseUrl}v1/action-items?limit=100&offset=$offset',
        headers: const {},
        method: 'GET',
        body: '',
        retries: 0,
        timeout: const Duration(seconds: 10),
        requireAuthCheck: true,
        expectedAuthenticatedUid: authority.uid,
        exactAuthority: authority);
    if (!authority.isExactCurrent() || response?.statusCode != 200) throw StateError('reminder read unavailable');
    return ActionItemsResponse.fromJson(jsonDecode(utf8.decode(response!.bodyBytes)));
  }

  @override
  void dispose() {
    _generation++;
    _retirementTimer.cancel();
    super.dispose();
  }
}
