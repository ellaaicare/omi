import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter_cache_manager/flutter_cache_manager.dart';

import 'package:omi/backend/preferences.dart';

class MemoryArtworkCache {
  MemoryArtworkCache._();

  static const int _maxDisplayAliases = 1000;
  static const int _maxPublishedVariantScopes = 1000;
  static const int _maxPublishedVariantKeysPerScope = 16;
  static const int _maxTrustedDisplayKeys = 1000;
  static const int _maxSuppressedDisplayKeys = 4096;
  static const Duration _evictionTimeout = Duration(seconds: 5);
  static const String _displayAliasesPreferenceKey = 'ellaMemoryArtworkDisplayAliasesV2';
  static const String _publishedVariantKeysPreferenceKey = 'ellaMemoryArtworkPublishedVariantKeysV1';
  static CacheManager? _manager;
  static final LinkedHashMap<String, String> _displayAliases = LinkedHashMap();
  static final LinkedHashMap<String, Set<String>> _publishedVariantKeys = LinkedHashMap();
  static final Map<String, String> _publishedVariantDisplayKeys = {};
  static final LinkedHashSet<String> _trustedDisplayKeys = LinkedHashSet();
  static final LinkedHashSet<String> _suppressedDisplayKeys = LinkedHashSet();
  static final Map<String, int> _suppressionGenerations = {};
  static final Map<String, int> _completedEvictionGenerations = {};
  static final Map<String, Future<bool>> _pendingEvictions = {};
  static int _nextSuppressionGeneration = 0;
  static int _nextRecoveryCacheGeneration = 0;
  static final String _networkOnlyCacheNamespace = _createNetworkOnlyCacheNamespace();
  static bool _diskReadsDisabled = false;
  static bool _persistentAliasesLoaded = false;

  static CacheManager get manager => _manager ??= CacheManager(
        Config('ellaMemoryArtworkCacheV1', stalePeriod: const Duration(days: 30), maxNrOfCacheObjects: 1000),
      );

  static bool isNetworkOnlyDisplayCacheKey(String cacheKey) {
    return cacheKey.contains('-network-only-v1-$_networkOnlyCacheNamespace-');
  }

  @visibleForTesting
  static bool get isPersistentManagerInitializedForTesting => _manager != null;

  /// Resolves stale conversation-list metadata to the authoritative cache key
  /// returned by the artwork endpoint. Sliver recycling must not make an
  /// already-downloaded image wait for that endpoint again.
  static String resolveDisplayCacheKey(String provisionalCacheKey) {
    _loadPersistentAliases();
    if (provisionalCacheKey.isEmpty || _diskReadsDisabled) return '';
    final authoritativeCacheKey = _displayAliases[provisionalCacheKey];
    if (authoritativeCacheKey != null) {
      if (_suppressedDisplayKeys.contains(authoritativeCacheKey)) {
        return '';
      }
      _displayAliases.remove(provisionalCacheKey);
      _displayAliases[provisionalCacheKey] = authoritativeCacheKey;
      _trustDisplayKey(authoritativeCacheKey, addIfMissing: true);
      return authoritativeCacheKey;
    }
    if (_suppressedDisplayKeys.contains(provisionalCacheKey) || !_trustDisplayKey(provisionalCacheKey)) return '';
    return provisionalCacheKey;
  }

  static Future<String?> rememberDisplayCacheKey({
    required String provisionalCacheKey,
    required String authoritativeCacheKey,
    required bool Function() isAuthorityCurrent,
    Duration evictionWaitTimeout = _evictionTimeout,
  }) async {
    _loadPersistentAliases();
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
    final suppressionSnapshot = {for (final cacheKey in cacheKeys) cacheKey: _suppressionGenerations[cacheKey] ?? 0};
    final evictions = cacheKeys.map((cacheKey) => _pendingEvictions[cacheKey]).whereType<Future<bool>>().toList();
    if (evictions.isNotEmpty) {
      final evictionResults = await Future.wait(
        evictions.map((eviction) => _waitForEviction(eviction, evictionWaitTimeout)),
      );
      if (evictionResults.any((completed) => !completed)) return null;
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
      if (candidate != null &&
          !cacheKeys.contains(candidate) &&
          candidate.startsWith('$authoritativeCacheKey-recovery-') &&
          !_suppressedDisplayKeys.contains(candidate)) {
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
    if (provisionalCacheKey.isNotEmpty && provisionalCacheKey != publishedCacheKey) {
      _displayAliases.remove(provisionalCacheKey);
      _displayAliases[provisionalCacheKey] = publishedCacheKey;
    }
    while (_displayAliases.length > _maxDisplayAliases) {
      _displayAliases.remove(_displayAliases.keys.first);
    }
    _trustDisplayKey(publishedCacheKey, addIfMissing: true);
    await _persistDisplayAliases();
    return publishedCacheKey;
  }

  static Set<String> publishedVariantCacheKeys({required String scopeKey, required String displayCacheKey}) {
    _loadPersistentAliases();
    if (scopeKey.isEmpty || _publishedVariantDisplayKeys[scopeKey] != displayCacheKey) return const <String>{};
    final keys = _publishedVariantKeys.remove(scopeKey);
    if (keys == null) return const <String>{};
    _publishedVariantKeys[scopeKey] = keys;
    return Set<String>.unmodifiable(keys);
  }

  /// Atomically drains every ledger for one owner-scoped memory identity.
  /// The display key stays stable across authority epochs and artwork revisions.
  static Set<String> takePublishedVariantCacheKeys({required String displayCacheKey}) {
    _loadPersistentAliases();
    if (!_isPersistentPublishedCacheKey(displayCacheKey)) return const <String>{};
    final matchingScopes = _publishedVariantDisplayKeys.entries
        .where((entry) => entry.value == displayCacheKey)
        .map((entry) => entry.key)
        .toList(growable: false);
    if (matchingScopes.isEmpty) return const <String>{};

    final published = <String>{};
    for (final scopeKey in matchingScopes) {
      published.addAll(_publishedVariantKeys.remove(scopeKey) ?? const <String>{});
      _publishedVariantDisplayKeys.remove(scopeKey);
    }
    unawaited(_persistPublishedVariantKeys());
    return Set<String>.unmodifiable(published);
  }

  static void rememberPublishedVariantCacheKeys({
    required String scopeKey,
    required String displayCacheKey,
    required Iterable<String> cacheKeys,
  }) {
    _loadPersistentAliases();
    if (!_isPersistentVariantScope(scopeKey) || !_isPersistentPublishedCacheKey(displayCacheKey)) return;
    final keys = cacheKeys.where(_isPersistentPublishedCacheKey).toSet();
    if (keys.isEmpty) return;

    final staleScopes = _publishedVariantDisplayKeys.entries
        .where((entry) => entry.value == displayCacheKey && entry.key != scopeKey)
        .map((entry) => entry.key)
        .toList(growable: false);
    final published = _publishedVariantKeys.remove(scopeKey) ?? <String>{};
    for (final staleScope in staleScopes) {
      published.addAll(_publishedVariantKeys.remove(staleScope) ?? const <String>{});
      _publishedVariantDisplayKeys.remove(staleScope);
    }

    for (final cacheKey in keys) {
      published.remove(cacheKey);
      published.add(cacheKey);
    }
    while (published.length > _maxPublishedVariantKeysPerScope) {
      published.remove(published.first);
    }
    _publishedVariantKeys[scopeKey] = published;
    _publishedVariantDisplayKeys[scopeKey] = displayCacheKey;
    while (_publishedVariantKeys.length > _maxPublishedVariantScopes) {
      final oldestScope = _publishedVariantKeys.keys.first;
      _publishedVariantKeys.remove(oldestScope);
      _publishedVariantDisplayKeys.remove(oldestScope);
    }
    unawaited(_persistPublishedVariantKeys().catchError((_) {}));
  }

  static void forgetDisplayCacheKey(String provisionalCacheKey) {
    _loadPersistentAliases();
    if (provisionalCacheKey.isNotEmpty && _displayAliases.remove(provisionalCacheKey) != null) {
      unawaited(_persistDisplayAliases());
    }
  }

  /// Blocks disk reads synchronously while terminal-policy cleanup removes the
  /// underlying files asynchronously. Process-local trust also starts empty,
  /// so a relaunch cannot read persistent files until the endpoint validates
  /// them again.
  static void suppressDisplayCacheKeys(Iterable<String> cacheKeys) {
    _loadPersistentAliases();
    final keys = cacheKeys.where((cacheKey) => cacheKey.isNotEmpty).toSet();
    if (keys.isEmpty) return;
    _displayAliases.removeWhere(
      (provisional, authoritative) => keys.contains(provisional) || keys.contains(authoritative),
    );
    final suppressedScopes = _publishedVariantKeys.entries
        .where((entry) => entry.value.any(keys.contains))
        .map((entry) => entry.key)
        .toList(growable: false);
    for (final scopeKey in suppressedScopes) {
      _publishedVariantKeys.remove(scopeKey);
      _publishedVariantDisplayKeys.remove(scopeKey);
    }
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
    unawaited(_persistDisplayAliases());
    unawaited(_persistPublishedVariantKeys());
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
    return List.generate(8, (_) => random.nextInt(1 << 16).toRadixString(16).padLeft(4, '0')).join();
  }

  /// Simulates a process restart without deleting persistent cache files.
  /// Persistent files are deliberately untrusted until the endpoint validates
  /// the exact account/profile key in the new process.
  @visibleForTesting
  static void resetRuntimeTrustForTesting() {
    _displayAliases.clear();
    _publishedVariantKeys.clear();
    _publishedVariantDisplayKeys.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
    _diskReadsDisabled = false;
    _persistentAliasesLoaded = false;
  }

  /// Revokes every in-memory artwork capability without deleting owner-scoped
  /// files. A freshly authenticated authority must validate each key before a
  /// persistent file can be read again.
  static void revokeRuntimeTrust({bool preserveDisplayAliases = false}) {
    _loadPersistentAliases();
    if (!preserveDisplayAliases) {
      _displayAliases.clear();
      _publishedVariantKeys.clear();
      _publishedVariantDisplayKeys.clear();
      _suppressedDisplayKeys.clear();
      _suppressionGenerations.clear();
      _completedEvictionGenerations.clear();
      _diskReadsDisabled = false;
      unawaited(SharedPreferencesUtil().remove(_displayAliasesPreferenceKey));
      unawaited(SharedPreferencesUtil().remove(_publishedVariantKeysPreferenceKey));
    }
    _trustedDisplayKeys.clear();
    // A detached terminal eviction can still delete its key after authority
    // returns. Keep that future as a serialization fence until it completes.
  }

  static void _enterFailClosedDiskMode() {
    _diskReadsDisabled = true;
    _displayAliases.clear();
    _publishedVariantKeys.clear();
    _publishedVariantDisplayKeys.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
    unawaited(_persistDisplayAliases());
    unawaited(_persistPublishedVariantKeys());
  }

  static Future<void> clear() async {
    _displayAliases.clear();
    _publishedVariantKeys.clear();
    _publishedVariantDisplayKeys.clear();
    _trustedDisplayKeys.clear();
    _suppressedDisplayKeys.clear();
    _suppressionGenerations.clear();
    _completedEvictionGenerations.clear();
    _pendingEvictions.clear();
    _diskReadsDisabled = false;
    _persistentAliasesLoaded = true;
    _nextRecoveryCacheGeneration = 0;
    await SharedPreferencesUtil().remove(_displayAliasesPreferenceKey);
    await SharedPreferencesUtil().remove(_publishedVariantKeysPreferenceKey);
    final activeManager = _manager;
    if (activeManager == null) return;
    await activeManager.emptyCache();
  }

  static void _loadPersistentAliases() {
    if (_persistentAliasesLoaded) return;
    _persistentAliasesLoaded = true;
    final encoded = SharedPreferencesUtil().getString(_displayAliasesPreferenceKey);
    try {
      if (encoded.isNotEmpty) {
        final decoded = jsonDecode(encoded);
        if (decoded is Map) {
          for (final entry in decoded.entries) {
            final provisional = entry.key.toString();
            final authoritative = entry.value?.toString() ?? '';
            if (!_persistentCacheKey.hasMatch(provisional) || !_persistentCacheKey.hasMatch(authoritative)) continue;
            _displayAliases[provisional] = authoritative;
          }
        }
      }
      while (_displayAliases.length > _maxDisplayAliases) {
        _displayAliases.remove(_displayAliases.keys.first);
      }
    } catch (_) {
      _displayAliases.clear();
    }

    final encodedVariantKeys = SharedPreferencesUtil().getString(_publishedVariantKeysPreferenceKey);
    try {
      if (encodedVariantKeys.isEmpty) return;
      final decoded = jsonDecode(encodedVariantKeys);
      if (decoded is! Map) return;
      for (final entry in decoded.entries) {
        final scopeKey = entry.key.toString();
        final value = entry.value;
        if (!_isPersistentVariantScope(scopeKey) || value is! Map) continue;
        final displayCacheKey = value['display_cache_key']?.toString() ?? '';
        final values = value['cache_keys'];
        if (!_isPersistentPublishedCacheKey(displayCacheKey) || values is! List) continue;
        final keys = LinkedHashSet<String>.from(
          values.map((value) => value.toString()).where(_isPersistentPublishedCacheKey),
        );
        while (keys.length > _maxPublishedVariantKeysPerScope) {
          keys.remove(keys.first);
        }
        if (keys.isNotEmpty) {
          _publishedVariantKeys[scopeKey] = keys;
          _publishedVariantDisplayKeys[scopeKey] = displayCacheKey;
        }
      }
      while (_publishedVariantKeys.length > _maxPublishedVariantScopes) {
        final oldestScope = _publishedVariantKeys.keys.first;
        _publishedVariantKeys.remove(oldestScope);
        _publishedVariantDisplayKeys.remove(oldestScope);
      }
    } catch (_) {
      _publishedVariantKeys.clear();
      _publishedVariantDisplayKeys.clear();
    }
  }

  static Future<void> _persistDisplayAliases() async {
    if (!_persistentAliasesLoaded) return;
    final aliases = <String, String>{
      for (final entry in _displayAliases.entries)
        if (_persistentCacheKey.hasMatch(entry.key) && _persistentCacheKey.hasMatch(entry.value))
          entry.key: entry.value,
    };
    await SharedPreferencesUtil().saveString(_displayAliasesPreferenceKey, jsonEncode(aliases));
  }

  static Future<void> _persistPublishedVariantKeys() async {
    if (!_persistentAliasesLoaded) return;
    final publishedVariantKeys = <String, Map<String, Object>>{
      for (final entry in _publishedVariantKeys.entries)
        if (_isPersistentVariantScope(entry.key) &&
            _isPersistentPublishedCacheKey(_publishedVariantDisplayKeys[entry.key] ?? ''))
          entry.key: {
            'display_cache_key': _publishedVariantDisplayKeys[entry.key]!,
            'cache_keys': entry.value.where(_isPersistentPublishedCacheKey).toList(growable: false),
          },
    };
    await SharedPreferencesUtil().saveString(
      _publishedVariantKeysPreferenceKey,
      jsonEncode(publishedVariantKeys),
    );
  }

  static final RegExp _persistentCacheKey = RegExp(r'^[a-f0-9]{64}$');
  static final RegExp _persistentPublishedCacheKey = RegExp(r'^[A-Za-z0-9._:-]{1,512}$');

  static bool _isPersistentPublishedCacheKey(String cacheKey) => _persistentPublishedCacheKey.hasMatch(cacheKey);

  static bool _isPersistentVariantScope(String scopeKey) =>
      scopeKey.isNotEmpty && scopeKey.length <= 2048 && !scopeKey.contains(RegExp(r'[\x00-\x1F]'));
}
