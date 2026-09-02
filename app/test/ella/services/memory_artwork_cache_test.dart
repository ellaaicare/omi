import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/memory_artwork_cache.dart';

void main() {
  setUp(MemoryArtworkCache.clear);
  tearDown(MemoryArtworkCache.clear);

  test('a new terminal tombstone remains fail-closed while older evictions are pending', () async {
    final releases = List.generate(1000, (_) => Completer<void>());
    final pendingEvictions = <Future<void>>[];

    for (var index = 0; index < releases.length; index++) {
      final cacheKey = 'pending-terminal-$index';
      MemoryArtworkCache.suppressDisplayCacheKeys({cacheKey});
      pendingEvictions.add(
        MemoryArtworkCache.evictSuppressedDisplayCacheKeys({cacheKey}, (_) => releases[index].future),
      );
    }

    const newestKey = 'new-terminal-key';
    MemoryArtworkCache.suppressDisplayCacheKeys({newestKey});

    expect(MemoryArtworkCache.resolveDisplayCacheKey(newestKey), isEmpty);

    for (final release in releases) {
      release.complete();
    }
    await Future.wait(pendingEvictions);
  });

  test('failed cleanup stays fail-closed through later suppression pressure and can be retried', () async {
    const failedKey = 'failed-terminal-key';
    MemoryArtworkCache.suppressDisplayCacheKeys({failedKey});
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys({failedKey}, (_) async => throw Exception('disk busy'));

    for (var index = 0; index <= 1000; index++) {
      final cacheKey = 'later-terminal-$index';
      MemoryArtworkCache.suppressDisplayCacheKeys({cacheKey});
      await MemoryArtworkCache.evictSuppressedDisplayCacheKeys({cacheKey}, (_) async {});
    }

    expect(MemoryArtworkCache.resolveDisplayCacheKey(failedKey), isEmpty);
    expect(
      await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: failedKey,
        authoritativeCacheKey: failedKey,
        isAuthorityCurrent: () => true,
      ),
      isNull,
    );

    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys({failedKey}, (_) async {});
    final recoveredCacheKey = await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: failedKey,
      authoritativeCacheKey: failedKey,
      isAuthorityCurrent: () => true,
    );
    expect(recoveredCacheKey, isNotNull);
    expect(recoveredCacheKey, isNot(failedKey));
    expect(MemoryArtworkCache.resolveDisplayCacheKey(failedKey), recoveredCacheKey);
  });

  test('a terminal key never becomes readable again after a stale download rewrites it or the app restarts', () async {
    const provisionalKey = 'terminal-provisional-key';
    const authoritativeKey = 'terminal-authoritative-key';
    MemoryArtworkCache.suppressDisplayCacheKeys({provisionalKey, authoritativeKey});
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
      {provisionalKey, authoritativeKey},
      (_) async {},
    );

    final recoveredCacheKey = await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: provisionalKey,
      authoritativeCacheKey: authoritativeKey,
      isAuthorityCurrent: () => true,
    );

    expect(recoveredCacheKey, isNotNull);
    expect(recoveredCacheKey, isNot(authoritativeKey));
    // An old image request may still write the terminal keys after deletion.
    // Resolution must continue to bypass those keys for the new ready image.
    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisionalKey), recoveredCacheKey);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(authoritativeKey), recoveredCacheKey);

    MemoryArtworkCache.resetRuntimeTrustForTesting();

    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisionalKey), isEmpty);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(authoritativeKey), isEmpty);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(recoveredCacheKey!), isEmpty);
  });

  test('a timed-out eviction stays serialized until the underlying deletion finishes', () async {
    const cacheKey = 'hung-terminal-key';
    final release = Completer<void>();
    var evictionCalls = 0;
    MemoryArtworkCache.suppressDisplayCacheKeys({cacheKey});

    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
      {cacheKey},
      (_) {
        evictionCalls += 1;
        return release.future;
      },
      waitTimeout: Duration.zero,
    );
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
      {cacheKey},
      (_) {
        evictionCalls += 1;
        return Future.value();
      },
      waitTimeout: Duration.zero,
    );

    expect(evictionCalls, 1);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(cacheKey), isEmpty);

    release.complete();
    await Future<void>.delayed(Duration.zero);
    final recoveredCacheKey = await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: cacheKey,
      authoritativeCacheKey: cacheKey,
      isAuthorityCurrent: () => true,
    );
    expect(recoveredCacheKey, isNotNull);
    expect(evictionCalls, 1);
  });
}
