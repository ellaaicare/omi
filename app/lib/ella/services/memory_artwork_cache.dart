import 'dart:collection';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static const int _maxDisplayAliases = 1000;
  static const int _maxTrustedDisplayKeys = 1000;
  static const int _maxSuppressedDisplayKeys = 4096;
  static const Duration _evictionTimeout = Duration(seconds: 5);
  static CacheManager? _manager;
  static final LinkedHashMap<String, String> _displayAliases = LinkedHashMap();
  static final LinkedHashSet<String> _trustedDisplayKeys = LinkedHashSet();
  static final LinkedHashSet<String> _suppressedDisplayKeys = LinkedHashSet();
  static final Map<String, int> _suppressionGenerations = {};
  static final Map<String, int> _completedEvictionGenerations = {};
  static final Map<String, Future<bool>> _pendingEvictions = {};
  static int _nextSuppressionGeneration = 0;
  static int _nextRecoveryCacheGeneration = 0;
  static final String _networkOnlyCacheNamespace = _createNetworkOnlyCacheNamespace();
  static bool _diskReadsDisabled = false;

  static CacheManager get manager => _manager ??= CacheManager(
        Config('ellaMemoryArtworkCacheV1', stalePeriod: const Duration(days: 30), maxNrOfCacheObjects: 1000),
      );

  static bool isNetworkOnlyDisplayCacheKey(String cacheKey) {
    return cacheKey.contains('-network-only-v1-$_networkOnlyCacheNamespace-');
  }

  /// Resolves stale conversation-list metadata to the authoritative cache key
  /// returned by the artwork endpoint. Sliver recycling must not make an
  /// already-downloaded image wait for that endpoint again.
  static String resolveDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isEmpty || _diskReadsDisabled) return '';
    final authoritativeCacheKey = _displayAliases.remove(provisionalCacheKey);
    if (authoritativeCacheKey != null) {
      if (_suppressedDisplayKeys.contains(authoritativeCacheKey) || !_trustDisplayKey(authoritativeCacheKey)) {
        return '';
      }
      _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
      return authoritativeCacheKey;
    }
    if (_suppressedDisplayKeys.contains(provisionalCacheKey) || !_trustDisplayKey(provisionalCacheKey)) return '';
    return provisionalCacheKey;
  }

  static Future<String?> rememberDisplayCacheKey({
    required String provisionalCacheKey,
    required String authoritativeCacheKey,
    required bool Function() isAuthorityCurrent,
  }) async {
    if (authoritativeCacheKey.isEmpty || !isAuthorityCurrent()) return null;
    if (_diskReadsDisabled) {
      // Suppression overflow discards per-key disk authority, but a newly
      // authenticated URL can still render under a collision-resistant key.
      // The key is intentionally not trusted for later persistent reads.
      final networkOnlyCacheKey =
          '$authoritativeCacheKey-network-only-v1-$_networkOnlyCacheNamespace-${++_nextRecoveryCacheGeneration}';
      return isAuthorityCurrent() ? networkOnlyCacheKey : null;
    }
    final cacheKeys = {authoritativeCacheKey, if (provisionalCacheKey.isNotEmpty) provisionalCacheKey};
    final suppressionSnapshot = {
      for (final cacheKey in cacheKeys) cacheKey: _suppressionGenerations[cacheKey] ?? 0,
    };
    final evictions = cacheKeys.map((cacheKey) => _pendingEvictions[cacheKey]).whereType<Future<bool>>().toList();
    if (evictions.isNotEmpty) {
      await Future.wait(evictions.map((eviction) => _waitForEviction(eviction, _evictionTimeout)));
    }
    if (_diskReadsDisabled || !isAuthorityCurrent()) return null;
    if (cacheKeys.any((cacheKey) => (_suppressionGenerations[cacheKey] ?? 0) != suppressionSnapshot[cacheKey])) {
      return null;
    }
    for (final cacheKey in cacheKeys.where(_suppressedDisplayKeys.contains)) {
      final generation = _suppressionGenerations[cacheKey];
      if (generation == null || _completedEvictionGenerations[cacheKey] != generation) return null;
    }

    String? existingRecoveryCacheKey;
    for (final cacheKey in cacheKeys) {
      final candidate = _displayAliases[cacheKey];
      if (candidate != null && !_suppressedDisplayKeys.contains(candidate) && _trustedDisplayKeys.contains(candidate)) {
        existingRecoveryCacheKey = candidate;
        break;
      }
    }
    final hasSuppressedKey = cacheKeys.any(_suppressedDisplayKeys.contains);
    final publishedCacheKey = existingRecoveryCacheKey ??
        (hasSuppressedKey
            ? '$authoritativeCacheKey-recovery-${++_nextRecoveryCacheGeneration}'
            : authoritativeCacheKey);
    if (!isAuthorityCurrent()) return null;

    for (final cacheKey in cacheKeys) {
      if (cacheKey == publishedCacheKey) continue;
      _trustedDisplayKeys.remove(cacheKey);
      _displayAliases.remove(cacheKey);
      _displayAliases[cacheKey] = publishedCacheKey;
    }
    while (_displayAliases.length > _maxDisplayAliases) {
      _displayAliases.remove(_displayAliases.keys.first);
    }
    _trustDisplayKey(publishedCacheKey, addIfMissing: true);
    return publishedCacheKey;
  }

  static void forgetDisplayCacheKey(String provisionalCacheKey) {
    if (provisionalCacheKey.isNotEmpty) _displayAliases.remove(provisionalCacheKey);
  }

  /// Blocks disk reads synchronously while terminal-policy cleanup removes the
  /// underlying files asynchronously. Process-local trust also starts empty,
  /// so a relaunch cannot read persistent files until the endpoint validates
  /// them again.
  static void suppressDisplayCacheKeys(Iterable<String> cacheKeys) {
    final keys = cacheKeys.where((cacheKey) => cacheKey.isNotEmpty).toSet();
    if (keys.isEmpty) return;
    _displayAliases
        .removeWhere((provisional, authoritative) => keys.contains(provisional) || keys.contains(authoritative));
    for (final cacheKey in keys) {
      _trustedDisplayKeys.remove(cacheKey);
      _suppressedDisplayKeys.remove(cacheKey);
      _suppressedDisplayKeys.add(cacheKey);
      _suppressionGenerations[cacheKey] = ++_nextSuppressionGeneration;
      _completedEvictionGenerations.remove(cacheKey);
    }
    if (_suppressedDisplayKeys.length > _maxSuppressedDisplayKeys) {
      _enterFailClosedDiskMode();
    }
  }

  static Future<void> evictSuppressedDisplayCacheKeys(
    Iterable<String> cacheKeys,
    Future<void> Function(String cacheKey) evict, {
    Duration waitTimeout = _evictionTimeout,
  }) async {
    final evictions = <Future<bool>>[];
    for (final cacheKey in cacheKeys.where((cacheKey) => cacheKey.isNotEmpty).toSet()) {
      evictions.add(_evictSuppressedDisplayCacheKey(cacheKey, evict, waitTimeout));
    }
    await Future.wait(evictions);
  }

  static Future<bool> _evictSuppressedDisplayCacheKey(
    String cacheKey,
    Future<void> Function(String cacheKey) evict,
    Duration waitTimeout,
  ) async {
    if (_diskReadsDisabled) return false;
    final pendingEviction = _pendingEvictions[cacheKey];
    if (pendingEviction != null) return _waitForEviction(pendingEviction, waitTimeout);
    if (_diskReadsDisabled) return false;
    if (!_suppressedDisplayKeys.contains(cacheKey)) return true;

    final suppressionGeneration = _suppressionGenerations[cacheKey];
    if (suppressionGeneration == null) return false;
    if (_completedEvictionGenerations[cacheKey] == suppressionGeneration) return true;
    late final Future<bool> eviction;
    eviction = () async {
      try {
        await evict(cacheKey);
      } catch (_) {
        // Failed cleanup remains tombstoned and can be retried by a later
        // terminal cleanup or authoritative ready response.
        return false;
      }
      if (!_diskReadsDisabled && _suppressionGenerations[cacheKey] == suppressionGeneration) {
        // Keep the tombstone authoritative. A stale in-flight image download
        // can rewrite the old key after deletion, so a later ready response
        // publishes under a new cache generation instead of trusting it.
        _completedEvictionGenerations[cacheKey] = suppressionGeneration;
      }
      return true;
    }()
        .whenComplete(() {
      if (identical(_pendingEvictions[cacheKey], eviction)) _pendingEvictions.remove(cacheKey);
    });
    _pendingEvictions[cacheKey] = eviction;
    return _waitForEviction(eviction, waitTimeout);
  }

  static Future<bool> _waitForEviction(Future<bool> eviction, Duration timeout) async {
    try {
      return await eviction.timeout(timeout);
    } catch (_) {
      return false;
    }
  }

  static bool _trustDisplayKey(String cacheKey, {bool addIfMissing = false}) {
    final wasTrusted = _trustedDisplayKeys.remove(cacheKey);
    if (!wasTrusted && !addIfMissing) return false;
    _trustedDisplayKeys.add(cacheKey);
    while (_trustedDisplayKeys.length > _maxTrustedDisplayKeys) {
      _trustedDisplayKeys.remove(_trustedDisplayKeys.first);
    }
    return true;
  }

  static String _createNetworkOnlyCacheNamespace() {
    final random = Random.secure();
    return List.generate(
      8,
      (_) => random.nextInt(1 << 16).toRadixString(16).padLeft(4, '0'),
    ).join();
  }

  /// Simulates a process restart without deleting persistent cache files.
  /// Persistent files are deliberately untrusted until the endpoint validates
  /// the exact account/profile key in the new process.
  @visibleForTesting
  static void resetRuntimeTrustForTesting() {
    _displayAliases.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
    _diskReadsDisabled = false;
  }

  static void _enterFailClosedDiskMode() {
    _diskReadsDisabled = true;
    _displayAliases.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
  }

  static Future<void> clear() async {
    _displayAliases.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
    _diskReadsDisabled = false;
    _nextRecoveryCacheGeneration = 0;
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }
}
