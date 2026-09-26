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

    await MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: firstScope,
      displayCacheKey: provisional,
      cacheKeys: {compactVariant},
    );
    await MemoryArtworkCache.rememberPublishedVariantCacheKeys(
      scopeKey: firstScope,
      displayCacheKey: provisional,
      cacheKeys: {largeVariant},
    );

    MemoryArtworkCache.resetRuntimeTrustForTesting();

    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: firstScope, displayCacheKey: provisional),
      {compactVariant, largeVariant},
    );
    expect(
      MemoryArtworkCache.publishedVariantCacheKeys(scopeKey: firstScope, displayCacheKey: '8' * 64),
      isEmpty,
    );

    await MemoryArtworkCache.rememberPublishedVariantCacheKeys(
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
      {replacementVariant},
    );
  });
}
