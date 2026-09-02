import 'dart:collection';

import 'package:flutter_cache_manager/flutter_cache_manager.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static const int _maxDisplayAliases = 1000;
  static CacheManager? _manager;
  static final LinkedHashMap<String, String> _displayAliases = LinkedHashMap();

  static CacheManager get manager => _manager ??= CacheManager(
        Config('ellaMemoryArtworkCacheV1', stalePeriod: const Duration(days: 30), maxNrOfCacheObjects: 1000),
      );

  /// Resolves stale conversation-list metadata to the authoritative cache key
  /// returned by the artwork endpoint. Sliver recycling must not make an
  /// already-downloaded image wait for that endpoint again.
  static String resolveDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isEmpty) return '';
    final authoritativeCacheKey = _displayAliases.remove(provisionalCacheKey);
    if (authoritativeCacheKey == null) return provisionalCacheKey;
    _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
    return authoritativeCacheKey;
  }

  static void rememberDisplayCacheKey({required String provisionalCacheKey, required String authoritativeCacheKey}) {
    if (provisionalCacheKey.isEmpty || authoritativeCacheKey.isEmpty) return;
    _displayAliases.remove(provisionalCacheKey);
    _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
    while (_displayAliases.length > _maxDisplayAliases) {
      _displayAliases.remove(_displayAliases.keys.first);
    }
  }

  static void forgetDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isNotEmpty) _displayAliases.remove(provisionalCacheKey);
  }

  static Future<void> clear() async {
    _displayAliases.clear();
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }
}
