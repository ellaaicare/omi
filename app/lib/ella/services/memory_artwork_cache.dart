import 'dart:collection';

import 'package:flutter_cache_manager/flutter_cache_manager.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static const int _maxDisplayAliases = 1000;
  static CacheManager? _manager;
  static final LinkedHashMap<String, String> _displayAliases = LinkedHashMap();
  static final LinkedHashSet<String> _suppressedDisplayKeys = LinkedHashSet();

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

  static void rememberDisplayCacheKey({required String provisionalCacheKey, required String authoritativeCacheKey}) {
    if (provisionalCacheKey.isEmpty || authoritativeCacheKey.isEmpty) return;
    _suppressedDisplayKeys.remove(provisionalCacheKey);
    _suppressedDisplayKeys.remove(authoritativeCacheKey);
    _displayAliases.remove(provisionalCacheKey);
    _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
    while (_displayAliases.length > _maxDisplayAliases) {
      _displayAliases.remove(_displayAliases.keys.first);
    }
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
    }
    while (_suppressedDisplayKeys.length > _maxDisplayAliases) {
      _suppressedDisplayKeys.remove(_suppressedDisplayKeys.first);
    }
  }

  static Future<void> clear() async {
    _displayAliases.clear();
    _suppressedDisplayKeys.clear();
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }
}
