import 'package:flutter_cache_manager/flutter_cache_manager.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static CacheManager? _manager;

  static CacheManager get manager => _manager ??= CacheManager(
        Config(
          'ellaMemoryArtworkCacheV1',
          stalePeriod: const Duration(days: 30),
          maxNrOfCacheObjects: 1000,
        ),
      );

  static Future<void> clear() async {
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }
}
