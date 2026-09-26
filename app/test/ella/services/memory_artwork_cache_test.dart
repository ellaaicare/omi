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
}
