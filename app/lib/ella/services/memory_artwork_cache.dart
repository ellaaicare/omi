import 'dart:collection';

import 'package:flutter_cache_manager/flutter_cache_manager.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static const int _maxDisplayAliases = 1000;
  static CacheManager? _manager;
  static final LinkedHashMap<String, String> _displayAliases = LinkedHashMap();
  static final LinkedHashSet<String> _suppressedDisplayKeys = LinkedHashSet();
  static final Map<String, int> _suppressionGenerations = {};
  static final Map<String, Future<void>> _pendingEvictions = {};
  static int _nextSuppressionGeneration = 0;

  static CacheManager get manager => _manager ??= CacheManager(
        Config('ellaMemoryArtworkCacheV1', stalePeriod: const Duration(days: 30), maxNrOfCacheObjects: 1000),
      );

  /// Resolves stale conversation-list metadata to the authoritative cache key
  /// returned by the artwork endpoint. Sliver recycling must not make an
  /// already-downloaded image wait for that endpoint again.
  static String resolveDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isEmpty) return '';
    if (_suppressedDisplayKeys.contains(provisionalCacheKey)) return '';
    final authoritativeCacheKey = _displayAliases.remove(provisionalCacheKey);
    if (authoritativeCacheKey == null) return provisionalCacheKey;
    if (_suppressedDisplayKeys.contains(authoritativeCacheKey)) return '';
    _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
    return authoritativeCacheKey;
  }

  static Future<bool> rememberDisplayCacheKey({
    required String provisionalCacheKey,
    required String authoritativeCacheKey,
  }) async {
    if (authoritativeCacheKey.isEmpty) return false;
    final cacheKeys = {authoritativeCacheKey, if (provisionalCacheKey.isNotEmpty) provisionalCacheKey};
    final suppressionSnapshot = {
      for (final cacheKey in cacheKeys) cacheKey: _suppressionGenerations[cacheKey] ?? 0,
    };
    final evictions = cacheKeys.map((cacheKey) => _pendingEvictions[cacheKey]).whereType<Future<void>>().toList();
    if (evictions.isNotEmpty) await Future.wait(evictions);
    if (cacheKeys.any((cacheKey) => (_suppressionGenerations[cacheKey] ?? 0) != suppressionSnapshot[cacheKey])) {
      return false;
    }
    if (provisionalCacheKey.isNotEmpty) _suppressedDisplayKeys.remove(provisionalCacheKey);
    _suppressedDisplayKeys.remove(authoritativeCacheKey);
    if (provisionalCacheKey.isNotEmpty) {
      _displayAliases.remove(provisionalCacheKey);
      _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
      while (_displayAliases.length > _maxDisplayAliases) {
        _displayAliases.remove(_displayAliases.keys.first);
      }
    }
    return true;
  }

  static void forgetDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isNotEmpty) _displayAliases.remove(provisionalCacheKey);
  }

  /// Blocks disk reads synchronously while terminal-policy cleanup removes the
  /// underlying files asynchronously. A later authoritative ready response
  /// explicitly clears the tombstones in [rememberDisplayCacheKey].
  static void suppressDisplayCacheKeys(Iterable<String> cacheKeys) {
    final keys = cacheKeys.where((cacheKey) => cacheKey.isNotEmpty).toSet();
    if (keys.isEmpty) return;
    _displayAliases
        .removeWhere((provisional, authoritative) => keys.contains(provisional) || keys.contains(authoritative));
    for (final cacheKey in keys) {
      _suppressedDisplayKeys.remove(cacheKey);
      _suppressedDisplayKeys.add(cacheKey);
      _suppressionGenerations[cacheKey] = ++_nextSuppressionGeneration;
    }
    while (_suppressedDisplayKeys.length > _maxDisplayAliases) {
      final removableKey = _suppressedDisplayKeys.cast<String?>().firstWhere(
            (cacheKey) => cacheKey != null && !_pendingEvictions.containsKey(cacheKey),
            orElse: () => null,
          );
      if (removableKey == null) break;
      _suppressedDisplayKeys.remove(removableKey);
      _suppressionGenerations.remove(removableKey);
    }
  }

  static Future<void> evictSuppressedDisplayCacheKeys(
    Iterable<String> cacheKeys,
    Future<void> Function(String cacheKey) evict,
  ) async {
    final evictions = <Future<void>>[];
    for (final cacheKey in cacheKeys.where((cacheKey) => cacheKey.isNotEmpty).toSet()) {
      final previousEviction = _pendingEvictions[cacheKey];
      late final Future<void> eviction;
      eviction = () async {
        if (previousEviction != null) await previousEviction;
        try {
          await evict(cacheKey);
        } catch (_) {
          // The tombstone remains authoritative even if disk cleanup fails.
        }
      }()
          .whenComplete(() {
        if (identical(_pendingEvictions[cacheKey], eviction)) _pendingEvictions.remove(cacheKey);
      });
      _pendingEvictions[cacheKey] = eviction;
      evictions.add(eviction);
    }
    await Future.wait(evictions);
  }

  static Future<void> clear() async {
    _displayAliases.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _pendingEvictions.clear();
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }
}
