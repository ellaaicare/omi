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
  bool _isProvisioningReady = false;
  int _generation = 0;
  bool _disposed = false;

  Future<void> updateAuthority({required String uid, required bool isProvisioningReady}) async {
    final normalizedUid = uid.trim();
    final accountChanged = normalizedUid != _uid;
    final becameReady = !_isProvisioningReady && isProvisioningReady;
    final lostAuthority = _isProvisioningReady && !isProvisioningReady;
    if (!accountChanged && !becameReady && _isProvisioningReady == isProvisioningReady) return;

    if (accountChanged || lostAuthority) {
      _generation++;
      _uid = normalizedUid;
      state = const TodayCardViewState.preparing();
      _notify();
    }
    _isProvisioningReady = isProvisioningReady;
    if (_uid.isEmpty || !_isProvisioningReady) return;
    await _load();
  }

  Future<void> retry() async {
    if (_uid.isEmpty || !_isProvisioningReady) return;
    await _load();
  }

  Future<void> onResumed() => retry();

  Future<void> _load() async {
    final uid = _uid;
    final generation = ++_generation;
    final cached = await _cache.read(uid: uid);
    if (!_isCurrent(uid, generation)) return;

    state = TodayCardViewState.preparing(card: cached, isCached: cached != null);
    _notify();

    try {
      final response = await _repository.fetch(uid: uid);
      if (!_isCurrent(uid, generation)) return;
      if (!response.isValid) {
        _applyDegraded(cached, response.hasCurrentContract ? 'invalid_today_card_response' : 'stale_contract');
        return;
      }

      switch (response.status) {
        case TodayCardStatus.ready:
          final card = response.card!;
          await _cache.write(uid: uid, card: card);
          if (!_isCurrent(uid, generation)) return;
          state = TodayCardViewState(status: TodayCardStatus.ready, card: card);
        case TodayCardStatus.preparing:
          state = TodayCardViewState(status: TodayCardStatus.preparing, card: cached, isCached: cached != null);
        case TodayCardStatus.newUser:
          await _cache.clear(uid: uid);
          if (!_isCurrent(uid, generation)) return;
          state = TodayCardViewState(status: TodayCardStatus.newUser, card: response.card);
        case TodayCardStatus.degraded:
          _applyDegraded(
            response.card ?? cached,
            response.errorCode,
            isCached: response.card == null && cached != null,
          );
          return;
      }
      _notify();
    } catch (_) {
      if (!_isCurrent(uid, generation)) return;
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

  bool _isCurrent(String uid, int generation) =>
      !_disposed && _uid == uid && _isProvisioningReady && _generation == generation;

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
