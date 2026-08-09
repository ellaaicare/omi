import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_repository.dart';

class TodayCardController extends ChangeNotifier {
  TodayCardController({required TodayCardRepository repository, required TodayCardCache cache})
      : _repository = repository,
        _cache = cache;

  final TodayCardRepository _repository;
  final TodayCardCache _cache;

  TodayCardViewState state = const TodayCardViewState.preparing();

  String _uid = '';
  String _authorityKey = '';
  bool _isProvisioningReady = false;
  int _generation = 0;
  bool _disposed = false;

  String get uid => _uid;

  Future<void> updateAuthority({
    required String uid,
    required String authorityKey,
    required bool isProvisioningReady,
  }) async {
    final normalizedUid = uid.trim();
    final normalizedAuthorityKey = authorityKey.trim();
    final accountChanged = normalizedUid != _uid;
    final authorityChanged = normalizedAuthorityKey != _authorityKey;
    final becameReady = !_isProvisioningReady && isProvisioningReady;
    final lostAuthority = _isProvisioningReady && !isProvisioningReady;
    if (!accountChanged && !authorityChanged && !becameReady && _isProvisioningReady == isProvisioningReady) return;

    if (accountChanged || authorityChanged || lostAuthority) {
      final previousUid = _uid;
      final previousAuthorityKey = _authorityKey;
      final transitionGeneration = ++_generation;
      _uid = normalizedUid;
      _authorityKey = normalizedAuthorityKey;
      _isProvisioningReady = isProvisioningReady;
      state = const TodayCardViewState.preparing();
      _notify();
      if (previousUid.isNotEmpty) {
        await _cache.clear(uid: previousUid, authorityKey: previousAuthorityKey);
      }
      if (_disposed ||
          _generation != transitionGeneration ||
          _uid != normalizedUid ||
          _authorityKey != normalizedAuthorityKey) {
        return;
      }
    } else {
      _isProvisioningReady = isProvisioningReady;
    }
    if (_uid.isEmpty || _authorityKey.isEmpty || !_isProvisioningReady) return;
    await _load();
  }

  Future<void> retry() async {
    if (_uid.isEmpty || _authorityKey.isEmpty || !_isProvisioningReady) return;
    await _load();
  }

  Future<void> onResumed() => retry();

  Future<void> _load() async {
    final uid = _uid;
    final authorityKey = _authorityKey;
    final generation = ++_generation;
    final cached = await _cache.read(uid: uid, authorityKey: authorityKey);
    if (!_isCurrent(uid, authorityKey, generation)) return;

    state = TodayCardViewState.preparing(card: cached, isCached: cached != null);
    _notify();

    try {
      final response = await _repository.fetch(uid: uid);
      if (!_isCurrent(uid, authorityKey, generation)) return;
      if (!response.isValid) {
        if (response.isAuthoritative) {
          await _cache.clear(uid: uid, authorityKey: authorityKey);
          if (!_isCurrent(uid, authorityKey, generation)) return;
        }
        _applyDegraded(
          response.isAuthoritative ? null : cached,
          response.hasCurrentContract ? 'invalid_today_card_response' : 'stale_contract',
          isCached: !response.isAuthoritative && cached != null,
        );
        return;
      }

      switch (response.status) {
        case TodayCardStatus.ready:
          final card = response.card!;
          final stored = await _cache.write(
            uid: uid,
            authorityKey: authorityKey,
            card: card,
            maxAge: response.cacheMaxAge,
            isCurrent: () => _isCurrent(uid, authorityKey, generation),
          );
          if (!stored || !_isCurrent(uid, authorityKey, generation)) return;
          state = TodayCardViewState(status: TodayCardStatus.ready, card: card);
        case TodayCardStatus.preparing:
          final retained = response.card ?? (response.isAuthoritative ? null : cached);
          if (response.isAuthoritative && response.card == null) {
            await _cache.clear(uid: uid, authorityKey: authorityKey);
            if (!_isCurrent(uid, authorityKey, generation)) return;
          }
          state = TodayCardViewState(status: TodayCardStatus.preparing, card: retained, isCached: retained == cached);
        case TodayCardStatus.newUser:
          await _cache.clear(uid: uid, authorityKey: authorityKey);
          if (!_isCurrent(uid, authorityKey, generation)) return;
          state = TodayCardViewState(status: TodayCardStatus.newUser, card: response.card);
        case TodayCardStatus.degraded:
          final invalidatesCache = response.isAuthoritative || response.invalidatesCachedCard;
          if (invalidatesCache && response.card == null) {
            await _cache.clear(uid: uid, authorityKey: authorityKey);
            if (!_isCurrent(uid, authorityKey, generation)) return;
          }
          _applyDegraded(
            response.card ?? (invalidatesCache ? null : cached),
            response.errorCode,
            isCached: !invalidatesCache && response.card == null && cached != null,
          );
          return;
      }
      _notify();
    } catch (_) {
      if (!_isCurrent(uid, authorityKey, generation)) return;
      _applyDegraded(cached, 'today_card_unavailable');
    }
  }

  void _applyDegraded(TodayCard? card, String errorCode, {bool? isCached}) {
    state = TodayCardViewState(
      status: TodayCardStatus.degraded,
      card: card,
      isCached: isCached ?? card != null,
      errorCode: errorCode,
    );
    _notify();
  }

  bool _isCurrent(String uid, String authorityKey, int generation) =>
      !_disposed && _uid == uid && _authorityKey == authorityKey && _isProvisioningReady && _generation == generation;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    super.dispose();
  }
}
