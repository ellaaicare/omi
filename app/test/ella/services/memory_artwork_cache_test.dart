import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/services/memory_artwork_cache.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    await MemoryArtworkCache.clear();
  });

  tearDown(() async {
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();
    MemoryArtworkCache.configurePublishedVariantPersistenceForTesting();
    await MemoryArtworkCache.clear();
    MemoryArtworkCache.resetRuntimeTrustForTesting();
  });

  test('saved owner-scoped alias is available first after a cold start', () async {
    final ownerAProvisional = 'a' * 64;
    final ownerAAuthoritative = 'b' * 64;
    final ownerBProvisional = 'c' * 64;

    expect(
      await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: ownerAProvisional,
        authoritativeCacheKey: ownerAAuthoritative,
        isAuthorityCurrent: () => true,
      ),
      ownerAAuthoritative,
    );
    await Future<void>.delayed(Duration.zero);

    MemoryArtworkCache.resetRuntimeTrustForTesting();

    expect(MemoryArtworkCache.resolveDisplayCacheKey(ownerAProvisional), ownerAAuthoritative);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(ownerBProvisional), isEmpty);
  });

  test('runtime trust revocation preserves saved aliases only when explicitly requested', () async {
    final provisional = 'd' * 64;
    final authoritative = 'e' * 64;
    await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: provisional,
      authoritativeCacheKey: authoritative,
      isAuthorityCurrent: () => true,
    );
    await Future<void>.delayed(Duration.zero);

    MemoryArtworkCache.revokeRuntimeTrust(preserveDisplayAliases: true);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisional), authoritative);

    MemoryArtworkCache.revokeRuntimeTrust();
    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisional), isEmpty);
  });

  test('a newly selected responsive variant replaces the prior display alias', () async {
    final provisional = '1' * 64;
    final compactVariant = '2' * 64;
    final largeVariant = '3' * 64;

    expect(
      await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: provisional,
        authoritativeCacheKey: compactVariant,
        isAuthorityCurrent: () => true,
      ),
      compactVariant,
    );
    expect(
      await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: provisional,
        authoritativeCacheKey: largeVariant,
        isAuthorityCurrent: () => true,
      ),
      largeVariant,
    );
    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisional), largeVariant);
  });

  test('published responsive variants survive restart and reset when their authority scope changes', () async {
    final provisional = '4' * 64;
    final compactVariant = '5' * 64;
    final largeVariant = '6' * 64;
    final replacementVariant = '7' * 64;
    const firstScope = 'memory-a:authority-1:artwork-1';
    const secondScope = 'memory-a:authority-2:artwork-1';

    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: firstScope,
      displayCacheKey: provisional,
      cacheKeys: {compactVariant},
    );
    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: firstScope,
      displayCacheKey: provisional,
      cacheKeys: {largeVariant},
    );
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();

    MemoryArtworkCache.resetRuntimeTrustForTesting();

    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: firstScope, displayCacheKey: provisional),
      {compactVariant, largeVariant},
    );
    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: firstScope, displayCacheKey: '8' * 64),
      isEmpty,
    );

    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: secondScope,
      displayCacheKey: provisional,
      cacheKeys: {replacementVariant},
    );

    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: firstScope, displayCacheKey: provisional),
      isEmpty,
    );
    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: secondScope, displayCacheKey: provisional),
      {compactVariant, largeVariant, replacementVariant},
    );

    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();
    MemoryArtworkCache.resetRuntimeTrustForTesting();
    final terminalKeys = MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(
      displayCacheKey: provisional,
    );
    expect(
      terminalKeys,
      {compactVariant, largeVariant, replacementVariant},
    );
    MemoryArtworkCache.suppressDisplayCacheKeys(terminalKeys);
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();

    MemoryArtworkCache.resetRuntimeTrustForTesting();
    expect(
      MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(displayCacheKey: provisional),
      {compactVariant, largeVariant, replacementVariant},
    );
    MemoryArtworkCache.suppressDisplayCacheKeys(terminalKeys);
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(terminalKeys, (_) async {});
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();

    MemoryArtworkCache.resetRuntimeTrustForTesting();
    expect(MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(displayCacheKey: provisional), isEmpty);
  });

  test('transient variant ledger write failure retries and survives restart', () async {
    final provisional = '8' * 64;
    final variant = '9' * 64;
    var attempts = 0;
    MemoryArtworkCache.configurePublishedVariantPersistenceForTesting(
      retryDelay: Duration.zero,
      writer: (key, value) async {
        attempts++;
        if (attempts == 1) throw StateError('transient write failure');
        return SharedPreferencesUtil().saveString(key, value);
      },
    );

    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: 'memory-a:authority-1:artwork-1',
      displayCacheKey: provisional,
      cacheKeys: {variant},
    );
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();

    expect(attempts, 2);
    MemoryArtworkCache.configurePublishedVariantPersistenceForTesting();
    MemoryArtworkCache.resetRuntimeTrustForTesting();
    expect(MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(displayCacheKey: provisional), {variant});
  });

  test('a stale eviction generation retains its ledger until the newest deletion succeeds', () async {
    final displayCacheKey = 'c' * 64;
    final variantCacheKey = 'd' * 64;
    final releaseFirstEviction = Completer<void>();
    final secondEvictionStarted = Completer<void>();
    final releaseSecondEviction = Completer<void>();
    var evictionCalls = 0;
    var diskHasVariant = true;

    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: 'memory-a:authority-1:artwork-1',
      displayCacheKey: displayCacheKey,
      cacheKeys: {variantCacheKey},
    );
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();
    MemoryArtworkCache.suppressDisplayCacheKeys({variantCacheKey});

    Future<void> evict(String _) async {
      evictionCalls++;
      if (evictionCalls == 1) {
        await releaseFirstEviction.future;
        diskHasVariant = false;
        diskHasVariant = true; // A stale image request rewrites the just-deleted file.
        return;
      }
      if (evictionCalls == 2) {
        secondEvictionStarted.complete();
        await releaseSecondEviction.future;
      }
      diskHasVariant = false;
    }

    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
      {variantCacheKey},
      evict,
      waitTimeout: Duration.zero,
    );
    MemoryArtworkCache.suppressDisplayCacheKeys({variantCacheKey});
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
      {variantCacheKey},
      evict,
      waitTimeout: Duration.zero,
    );
    expect(evictionCalls, 1);

    releaseFirstEviction.complete();
    await secondEvictionStarted.future;
    expect(diskHasVariant, isTrue);

    MemoryArtworkCache.resetRuntimeTrustForTesting();
    expect(
      MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(displayCacheKey: displayCacheKey),
      {variantCacheKey},
    );

    releaseSecondEviction.complete();
    await Future<void>.delayed(Duration.zero);
    MemoryArtworkCache.suppressDisplayCacheKeys({variantCacheKey});
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys({variantCacheKey}, evict);
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();

    expect(evictionCalls, 3);
    expect(diskHasVariant, isFalse);
    MemoryArtworkCache.resetRuntimeTrustForTesting();
    expect(
      MemoryArtworkCache.publishedVariantCacheKeysForTerminalCleanup(displayCacheKey: displayCacheKey),
      isEmpty,
    );
  });

  test('explicit clear is bounded and removes a late stale ledger write', () async {
    final writerStarted = Completer<void>();
    final releaseWriter = Completer<void>();
    MemoryArtworkCache.configurePublishedVariantPersistenceForTesting(
      cancellationTimeout: Duration.zero,
      writer: (key, value) async {
        if (!writerStarted.isCompleted) writerStarted.complete();
        await releaseWriter.future;
        return SharedPreferencesUtil().saveString(key, value);
      },
    );

    MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: 'memory-a:authority-1:artwork-1',
      displayCacheKey: 'a' * 64,
      cacheKeys: {'b' * 64},
    );
    await writerStarted.future;

    await MemoryArtworkCache.clear().timeout(const Duration(seconds: 1));
    expect(SharedPreferencesUtil().getString('ellaMemoryArtworkPublishedVariantKeysV1'), isEmpty);

    releaseWriter.complete();
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);
    await MemoryArtworkCache.waitForPublishedVariantPersistenceForTesting();
    expect(SharedPreferencesUtil().getString('ellaMemoryArtworkPublishedVariantKeysV1'), '{}');
    MemoryArtworkCache.configurePublishedVariantPersistenceForTesting();
  });
}
