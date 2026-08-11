import 'dart:async';

import 'package:flutter/foundation.dart';

import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_repository.dart';

typedef TodayCardExpiryScheduler = Timer Function(Duration duration, void Function() callback);

class TodayCardController extends ChangeNotifier {
  TodayCardController({
    required TodayCardRepository repository,
    required TodayCardCache cache,
    Future<void> Function()? onRevalidationRequired,
    TodayCardExpiryScheduler expiryScheduler = _defaultExpiryScheduler,
    TodayCardExpiryScheduler pollScheduler = _defaultExpiryScheduler,
    Duration authorityWaitTimeout = const Duration(seconds: 12),
    int maxPreparingPolls = 3,
    int maxTransientRetries = 2,
    Duration transientRetryDelay = const Duration(seconds: 2),
  })  : _repository = repository,
        _cache = cache,
        _onRevalidationRequired = onRevalidationRequired,
        _expiryScheduler = expiryScheduler,
        _pollScheduler = pollScheduler,
        _authorityWaitTimeout = authorityWaitTimeout,
        _maxPreparingPolls = maxPreparingPolls,
        _maxTransientRetries = maxTransientRetries,
        _transientRetryDelay = transientRetryDelay;

  static Timer _defaultExpiryScheduler(Duration duration, void Function() callback) => Timer(duration, callback);

  final TodayCardRepository _repository;
  final TodayCardCache _cache;
  final Future<void> Function()? _onRevalidationRequired;
  final TodayCardExpiryScheduler _expiryScheduler;
  final TodayCardExpiryScheduler _pollScheduler;
  final Duration _authorityWaitTimeout;
  final int _maxPreparingPolls;
  final int _maxTransientRetries;
  final Duration _transientRetryDelay;

  TodayCardViewState state = const TodayCardViewState.preparing();

  String _uid = '';
  String _authorityKey = '';
  bool _isProvisioningReady = false;
  int _generation = 0;
  bool _disposed = false;
  Timer? _expiryTimer;
  Timer? _pollTimer;
  Timer? _authorityTimer;
  int _preparingPolls = 0;
  int _transientRetries = 0;

  String get uid => _uid;

  Future<void> updateAuthority({
    required String uid,
    required String authorityKey,
    required bool isProvisioningReady,
    bool forceReload = false,
  }) async {
    final normalizedUid = uid.trim();
    final normalizedAuthorityKey = authorityKey.trim();
    final accountChanged = normalizedUid != _uid;
    final authorityChanged = normalizedAuthorityKey != _authorityKey;
    final becameReady = !_isProvisioningReady && isProvisioningReady;
    final lostAuthority = _isProvisioningReady && !isProvisioningReady;
    if (!forceReload &&
        !accountChanged &&
        !authorityChanged &&
        !becameReady &&
        _isProvisioningReady == isProvisioningReady) {
      return;
    }

    if (accountChanged || authorityChanged || lostAuthority) {
      _cancelTimers();
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
    if (_uid.isEmpty || _authorityKey.isEmpty) {
      _applyDegraded(null, 'today_card_authority_unavailable', isCached: false);
      return;
    }
    if (!_isProvisioningReady) {
      _scheduleAuthorityTimeout(uid: _uid, authorityKey: _authorityKey);
      return;
    }
    _authorityTimer?.cancel();
    _authorityTimer = null;
    if (accountChanged || authorityChanged || becameReady || forceReload) {
      _preparingPolls = 0;
      _transientRetries = 0;
    }
    await _load();
  }

  void invalidateAuthority() {
    if (_disposed) return;
    final previousUid = _uid;
    final previousAuthorityKey = _authorityKey;
    _cancelTimers();
    _generation++;
    _isProvisioningReady = false;
    state = const TodayCardViewState.preparing();
    _notify();
    if (_uid.isNotEmpty && _authorityKey.isNotEmpty) {
      _scheduleAuthorityTimeout(uid: _uid, authorityKey: _authorityKey);
    }
    if (previousUid.isNotEmpty) {
      unawaited(_cache.clear(uid: previousUid, authorityKey: previousAuthorityKey));
    }
  }

  Future<void> retry() async {
    if (_uid.isEmpty || _authorityKey.isEmpty || !_isProvisioningReady) return;
    _pollTimer?.cancel();
    _pollTimer = null;
    _preparingPolls = 0;
    await _load();
  }

  Future<void> onResumed() => retry();

  Future<void> _load() async {
    final uid = _uid;
    final authorityKey = _authorityKey;
    final generation = ++_generation;
    final cachedEntry = await _cache.read(uid: uid, authorityKey: authorityKey);
    if (!_isCurrent(uid, authorityKey, generation)) return;
    final cached = cachedEntry?.card;
    if (cachedEntry != null) {
      _scheduleExpiry(cachedEntry.freshnessRemaining, uid: uid, authorityKey: authorityKey);
    }

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
      if (response.card != null && response.cacheMaxAge <= Duration.zero) {
        _cancelExpiry();
        await _cache.clear(uid: uid, authorityKey: authorityKey);
        if (!_isCurrent(uid, authorityKey, generation)) return;
        _applyDegraded(null, 'invalid_today_card_cache_policy', isCached: false);
        return;
      }

      switch (response.status) {
        case TodayCardStatus.ready:
          _cancelPoll();
          _preparingPolls = 0;
          _transientRetries = 0;
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
          _scheduleExpiry(response.cacheMaxAge, uid: uid, authorityKey: authorityKey);
        case TodayCardStatus.preparing:
          final retained = response.card ?? (response.isAuthoritative ? null : cached);
          if (response.isAuthoritative && response.card == null) {
            _cancelExpiry();
            await _cache.clear(uid: uid, authorityKey: authorityKey);
            if (!_isCurrent(uid, authorityKey, generation)) return;
          }
          state = TodayCardViewState(status: TodayCardStatus.preparing, card: retained, isCached: retained == cached);
          if (response.card != null) {
            _scheduleExpiry(response.cacheMaxAge, uid: uid, authorityKey: authorityKey);
          }
          _schedulePreparingPoll(
            response.retryAfter,
            uid: uid,
            authorityKey: authorityKey,
            retainedCard: retained,
            isCached: retained == cached,
          );
        case TodayCardStatus.newUser:
          _cancelPoll();
          _preparingPolls = 0;
          _transientRetries = 0;
          _cancelExpiry();
          await _cache.clear(uid: uid, authorityKey: authorityKey);
          if (!_isCurrent(uid, authorityKey, generation)) return;
          state = TodayCardViewState(status: TodayCardStatus.newUser, card: response.card);
          if (response.card != null) {
            _scheduleExpiry(response.cacheMaxAge, uid: uid, authorityKey: authorityKey);
          }
        case TodayCardStatus.degraded:
          if (!response.isAuthoritative &&
              response.errorCode == 'today_card_unavailable' &&
              _scheduleTransientRetry(uid: uid, authorityKey: authorityKey, cached: cached)) {
            return;
          }
          _cancelPoll();
          _preparingPolls = 0;
          _transientRetries = 0;
          final invalidatesCache = response.isAuthoritative || response.invalidatesCachedCard;
          if (invalidatesCache && response.card == null) {
            _cancelExpiry();
            await _cache.clear(uid: uid, authorityKey: authorityKey);
            if (!_isCurrent(uid, authorityKey, generation)) return;
          }
          final degradedCard = response.card ?? (invalidatesCache ? null : cached);
          _applyDegraded(
            degradedCard,
            response.errorCode,
            isCached: !invalidatesCache && response.card == null && cached != null,
          );
          if (response.card != null) {
            _scheduleExpiry(response.cacheMaxAge, uid: uid, authorityKey: authorityKey);
          }
          return;
      }
      _notify();
    } catch (_) {
      if (!_isCurrent(uid, authorityKey, generation)) return;
      if (_scheduleTransientRetry(uid: uid, authorityKey: authorityKey, cached: cached)) return;
      _applyDegraded(cached, 'today_card_unavailable');
    }
  }

  bool _scheduleTransientRetry({
    required String uid,
    required String authorityKey,
    required TodayCard? cached,
  }) {
    _cancelPoll();
    if (_transientRetries >= _maxTransientRetries) return false;
    state = TodayCardViewState.preparing(card: cached, isCached: cached != null);
    _notify();
    _pollTimer = _pollScheduler(_transientRetryDelay, () {
      _pollTimer = null;
      if (_disposed || _uid != uid || _authorityKey != authorityKey || !_isProvisioningReady) return;
      _transientRetries++;
      unawaited(_load());
    });
    return true;
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

  void _scheduleAuthorityTimeout({required String uid, required String authorityKey}) {
    _authorityTimer?.cancel();
    if (_disposed || _authorityWaitTimeout <= Duration.zero) {
      _showAuthorityUnavailable(uid: uid, authorityKey: authorityKey);
      return;
    }
    _authorityTimer = _pollScheduler(
      _authorityWaitTimeout,
      () => _showAuthorityUnavailable(uid: uid, authorityKey: authorityKey),
    );
  }

  void _showAuthorityUnavailable({required String uid, required String authorityKey}) {
    _authorityTimer = null;
    if (_disposed || _uid != uid || _authorityKey != authorityKey || _isProvisioningReady) return;
    _applyDegraded(null, 'today_card_authority_unavailable', isCached: false);
  }

  void _schedulePreparingPoll(
    Duration retryAfter, {
    required String uid,
    required String authorityKey,
    required TodayCard? retainedCard,
    required bool isCached,
  }) {
    _cancelPoll();
    if (_preparingPolls >= _maxPreparingPolls) {
      _applyDegraded(retainedCard, 'today_card_preparing_timeout', isCached: isCached);
      return;
    }
    final delay = retryAfter <= Duration.zero
        ? const Duration(seconds: 30)
        : Duration(seconds: retryAfter.inSeconds.clamp(5, 60));
    _pollTimer = _pollScheduler(delay, () {
      _pollTimer = null;
      if (_disposed || _uid != uid || _authorityKey != authorityKey || !_isProvisioningReady) return;
      _preparingPolls++;
      unawaited(_load());
    });
  }

  void _scheduleExpiry(Duration duration, {required String uid, required String authorityKey}) {
    _cancelExpiry();
    if (_disposed || duration <= Duration.zero) {
      _expireCurrent(uid: uid, authorityKey: authorityKey);
      return;
    }
    _expiryTimer = _expiryScheduler(duration, () => _expireCurrent(uid: uid, authorityKey: authorityKey));
  }

  void _expireCurrent({required String uid, required String authorityKey}) {
    _expiryTimer = null;
    if (_disposed || _uid != uid || _authorityKey != authorityKey || !_isProvisioningReady) return;
    final expiryGeneration = ++_generation;
    state = const TodayCardViewState.preparing();
    _notify();
    unawaited(_clearAndRevalidate(uid: uid, authorityKey: authorityKey, generation: expiryGeneration));
  }

  Future<void> _clearAndRevalidate({
    required String uid,
    required String authorityKey,
    required int generation,
  }) async {
    await _cache.clear(uid: uid, authorityKey: authorityKey);
    if (!_isCurrent(uid, authorityKey, generation)) return;
    final revalidate = _onRevalidationRequired;
    if (revalidate != null) {
      await revalidate();
    } else {
      await _load();
    }
  }

  void _cancelExpiry() {
    _expiryTimer?.cancel();
    _expiryTimer = null;
  }

  void _cancelPoll() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  void _cancelTimers() {
    _cancelExpiry();
    _cancelPoll();
    _authorityTimer?.cancel();
    _authorityTimer = null;
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
    _cancelTimers();
    super.dispose();
  }
}
