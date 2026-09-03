import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:cached_network_image/cached_network_image.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/memory_artwork_cache.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _DelayedArtworkApi extends MemoryArtworkApi {
  _DelayedArtworkApi() : super(authorityProvider: () => null);

  final remoteResult = Completer<MemoryArtworkResult>();
  int loadCalls = 0;
  bool? lastEnqueueIfMissing;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'owner-profile-memory-revision-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    loadCalls += 1;
    lastEnqueueIfMissing = enqueueIfMissing;
    return remoteResult.future;
  }
}

class _ManualGenerationArtworkApi extends MemoryArtworkApi {
  _ManualGenerationArtworkApi({this.initialFailureCode = ''}) : super(authorityProvider: () => null);

  final String initialFailureCode;
  final List<bool> enqueueRequests = [];
  final Completer<MemoryArtworkResult> generationResult = Completer<MemoryArtworkResult>();

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    enqueueRequests.add(enqueueIfMissing);
    if (!enqueueIfMissing) {
      return Future.value(
        MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: initialFailureCode,
        ),
      );
    }
    return generationResult.future;
  }
}

class _RecycledArtworkApi extends MemoryArtworkApi {
  _RecycledArtworkApi() : super(authorityProvider: () => null);

  final recycledResult = Completer<MemoryArtworkResult>();
  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'stale-conversation-list-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    loadCalls += 1;
    if (loadCalls == 1) {
      return Future.value(
        MemoryArtworkResult(
          status: MemoryArtworkResultStatus.ready,
          url: Uri.parse('https://private-storage.example/authoritative.png'),
          cacheKey: 'authoritative-artwork-cache-key',
        ),
      );
    }
    return recycledResult.future;
  }
}

class _ReadyThenSuppressedArtworkApi extends MemoryArtworkApi {
  _ReadyThenSuppressedArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'suppressed-provisional-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    loadCalls += 1;
    if (loadCalls == 1) {
      return Future.value(
        MemoryArtworkResult(
          status: MemoryArtworkResultStatus.ready,
          url: Uri.parse('https://private-storage.example/suppressed.png'),
          cacheKey: 'suppressed-authoritative-cache-key',
        ),
      );
    }
    if (loadCalls == 2) return Future.value(const MemoryArtworkResult(status: MemoryArtworkResultStatus.declined));
    return Future.value(
      MemoryArtworkResult(
        status: MemoryArtworkResultStatus.ready,
        url: Uri.parse('https://private-storage.example/reconsented.png'),
        cacheKey: 'suppressed-authoritative-cache-key',
      ),
    );
  }
}

class _RefreshingArtworkApi extends MemoryArtworkApi {
  _RefreshingArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;
  bool? lastEnqueueIfMissing;
  int? lastPollAttempts;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    lastEnqueueIfMissing = enqueueIfMissing;
    lastPollAttempts = pollAttempts;
    if (loadCalls == 1) return const MemoryArtworkResult(status: MemoryArtworkResultStatus.generating);
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/art.png'),
      cacheKey: 'ready-artwork-cache-key',
    );
  }
}

class _TerminalThenReadyArtworkApi extends MemoryArtworkApi {
  _TerminalThenReadyArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;
  bool? lastEnqueueIfMissing;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    lastEnqueueIfMissing = enqueueIfMissing;
    if (loadCalls == 1) {
      return const MemoryArtworkResult(
        status: MemoryArtworkResultStatus.unavailable,
        failureCode: 'memory_artwork_not_found',
      );
    }
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/terminal-refresh.png'),
      cacheKey: 'terminal-refresh-cache-key',
    );
  }
}

class _RecoveringEnrichmentArtworkApi extends MemoryArtworkApi {
  _RecoveringEnrichmentArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;
  final List<bool> enqueueRequests = [];

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    enqueueRequests.add(enqueueIfMissing);
    if (loadCalls == 1) {
      return const MemoryArtworkResult(
        status: MemoryArtworkResultStatus.unavailable,
        failureCode: 'memory_artwork_enrichment_not_terminal',
      );
    }
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/recovered.png'),
      cacheKey: 'recovered-artwork-cache-key',
    );
  }
}

class _PublishedRefreshArtworkApi extends MemoryArtworkApi {
  _PublishedRefreshArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/published-$loadCalls.png'),
      cacheKey: 'published-refresh-$loadCalls',
      styleVersion: loadCalls == 1 ? memoryArtworkDefaultStyle : memoryArtworkAnimeStorybookStyle,
      requestedStyleVersion: memoryArtworkAnimeStorybookStyle,
      refreshPending: loadCalls == 1,
    );
  }
}

class _AuthorityRefreshArtworkApi extends MemoryArtworkApi {
  _AuthorityRefreshArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;
  bool terminal = false;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'authority-artwork-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    if (terminal) return const MemoryArtworkResult(status: MemoryArtworkResultStatus.declined);
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/authority-$loadCalls.png'),
      cacheKey: 'authority-artwork-cache-key',
    );
  }
}

class _AuthorityBecomesReadyArtworkApi extends MemoryArtworkApi {
  _AuthorityBecomesReadyArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'settling-authority-artwork-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    if (loadCalls == 1) {
      return const MemoryArtworkResult(
        status: MemoryArtworkResultStatus.unavailable,
        failureCode: 'memory_artwork_authority_unavailable',
      );
    }
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/settled-authority.png'),
      cacheKey: 'settling-authority-artwork-cache-key',
    );
  }
}

class _RecoveredAuthorityRecycledArtworkApi extends MemoryArtworkApi {
  _RecoveredAuthorityRecycledArtworkApi() : super(authorityProvider: () => null);

  final recycledResult = Completer<MemoryArtworkResult>();
  bool authorityAvailable = false;
  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      authorityAvailable ? 'recovered-authority-provisional-cache-key' : '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) {
    loadCalls += 1;
    if (loadCalls == 1) {
      authorityAvailable = true;
      return Future.value(
        const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_authority_unavailable',
        ),
      );
    }
    if (loadCalls == 2) {
      return Future.value(
        MemoryArtworkResult(
          status: MemoryArtworkResultStatus.ready,
          url: Uri.parse('https://private-storage.example/recovered-authority.png'),
          cacheKey: 'recovered-authority-authoritative-cache-key',
        ),
      );
    }
    return recycledResult.future;
  }
}

class _MutableArtworkAuthority implements ExactAccountAuthorityVerifier {
  _MutableArtworkAuthority();

  bool current = true;

  @override
  String get uid => 'test-user';

  @override
  bool isExactCurrent() => current;
}

class _AuthorityDriftDuringCachePublishApi extends MemoryArtworkApi {
  _AuthorityDriftDuringCachePublishApi(this.authority) : super(authorityProvider: () => null);

  final _MutableArtworkAuthority authority;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      authority.current ? 'drift-provisional-cache-key' : '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/drift.png'),
      cacheKey: 'drift-authoritative-cache-key',
      authority: authority,
    );
  }
}

class _RetryingReadyArtworkApi extends MemoryArtworkApi {
  _RetryingReadyArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'retrying-ready-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/retrying-ready.png'),
      cacheKey: 'retrying-ready-cache-key',
    );
  }
}

class _PersistentlyUnavailableAuthorityArtworkApi extends MemoryArtworkApi {
  _PersistentlyUnavailableAuthorityArtworkApi(this.failureCode) : super(authorityProvider: () => null);

  int loadCalls = 0;
  final String failureCode;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      'persistently-unavailable-authority-cache-key';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    return MemoryArtworkResult(status: MemoryArtworkResultStatus.unavailable, failureCode: failureCode);
  }
}

class _AuthoritySettlesAfterFinalRetryArtworkApi extends MemoryArtworkApi {
  _AuthoritySettlesAfterFinalRetryArtworkApi() : super(authorityProvider: () => null);

  int loadCalls = 0;

  @override
  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) =>
      '';

  @override
  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    loadCalls += 1;
    if (loadCalls <= 2) {
      return const MemoryArtworkResult(
        status: MemoryArtworkResultStatus.unavailable,
        failureCode: 'memory_artwork_authority_unavailable',
      );
    }
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.ready,
      url: Uri.parse('https://private-storage.example/settled-after-final-retry.png'),
      cacheKey: 'settled-after-final-retry-cache-key',
    );
  }
}

void main() {
  setUp(MemoryArtworkCache.resetRuntimeTrustForTesting);
  tearDown(MemoryArtworkCache.resetRuntimeTrustForTesting);

  Future<void> trustDisplayKey(String cacheKey) async {
    final trustedKey = await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: cacheKey,
      authoritativeCacheKey: cacheKey,
      isAuthorityCurrent: () => true,
    );
    expect(trustedKey, cacheKey);
  }

  test('a superseded source key stays untrusted after alias pressure evicts its replacement mapping', () async {
    const staleCacheKey = 'stale-ready-cache-key';
    const replacementCacheKey = 'replacement-ready-cache-key';
    await trustDisplayKey(staleCacheKey);
    expect(
      await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: staleCacheKey,
        authoritativeCacheKey: replacementCacheKey,
        isAuthorityCurrent: () => true,
      ),
      replacementCacheKey,
    );
    expect(MemoryArtworkCache.resolveDisplayCacheKey(staleCacheKey), replacementCacheKey);

    for (var index = 0; index < 501; index++) {
      final provisionalCacheKey = 'pressure-provisional-$index';
      final authoritativeCacheKey = 'pressure-authoritative-$index';
      MemoryArtworkCache.suppressDisplayCacheKeys({provisionalCacheKey, authoritativeCacheKey});
      await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
        {provisionalCacheKey, authoritativeCacheKey},
        (_) async {},
      );
      expect(
        await MemoryArtworkCache.rememberDisplayCacheKey(
          provisionalCacheKey: provisionalCacheKey,
          authoritativeCacheKey: authoritativeCacheKey,
          isAuthorityCurrent: () => true,
        ),
        isNotNull,
      );
    }

    expect(
      MemoryArtworkCache.resolveDisplayCacheKey(staleCacheKey),
      isEmpty,
      reason: 'alias eviction must not revive the superseded persistent disk key',
    );
  });

  testWidgets('overflow recovery stays memory-only and disappears on a terminal response', (tester) async {
    for (var index = 0; index <= 4096; index++) {
      MemoryArtworkCache.suppressDisplayCacheKeys({'terminal-capacity-$index'});
    }
    expect(MemoryArtworkCache.resolveDisplayCacheKey('terminal-capacity-0'), isEmpty);

    final api = _AuthorityRefreshArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-after-suppression-capacity',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );
    var persistentCacheLookups = 0;
    expect(MemoryArtworkCache.isPersistentManagerInitializedForTesting, isFalse);

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            refreshEpoch: refreshEpoch,
            cachedFileLookup: (_) async {
              persistentCacheLookups += 1;
              return null;
            },
            retryDelay: const Duration(milliseconds: 10),
            maxImageDownloadRetries: 1,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();

    expect(find.byKey(const Key('memory-generated-artwork-memory-after-suppression-capacity')), findsOneWidget);
    final networkImageFinder = find.byKey(
      const Key('memory-generated-artwork-network-memory-after-suppression-capacity-0'),
    );
    expect(networkImageFinder, findsOneWidget);
    expect(
      find.descendant(of: networkImageFinder, matching: find.byType(CachedNetworkImage)),
      findsNothing,
      reason: 'overflow recovery must not write private artwork to the persistent cache manager',
    );
    final image = tester.widget<Image>(networkImageFinder);
    expect(image.image, isA<NetworkImage>());
    expect((image.image as NetworkImage).url, 'https://private-storage.example/authority-1.png');
    expect(persistentCacheLookups, 0, reason: 'overflow recovery must not read or write the persistent cache');
    expect(MemoryArtworkCache.isPersistentManagerInitializedForTesting, isFalse);
    expect(
      MemoryArtworkCache.resolveDisplayCacheKey('authority-artwork-cache-key'),
      isEmpty,
      reason: 'overflow mode must keep persistent cache reads fail-closed',
    );

    image.errorBuilder!(tester.element(networkImageFinder), Exception('network unavailable'), StackTrace.empty);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(MemoryArtworkCache.isPersistentManagerInitializedForTesting, isFalse);
    expect(api.loadCalls, 2, reason: 'network-only download recovery must remain bounded and fetch fresh authority');
    expect(find.byType(CachedNetworkImage), findsNothing);

    api.terminal = true;
    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();

    expect(find.byKey(const Key('memory-generated-artwork-memory-after-suppression-capacity')), findsNothing);
    expect(find.byType(CachedNetworkImage), findsNothing);
    expect(find.text('Illustration unavailable'), findsOneWidget);
    expect(persistentCacheLookups, 0, reason: 'terminal cleanup cannot expose a disk artifact that was never written');
    expect(MemoryArtworkCache.isPersistentManagerInitializedForTesting, isFalse);
  });

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

  testWidgets('recycled cards use the authoritative disk key before a repeated metadata request completes', (
    tester,
  ) async {
    final api = _RecycledArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
    final conversation = ServerConversation(
      id: 'memory-recycled',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    Widget buildArtwork() => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (cacheKey) async {
              requestedKeys.add(cacheKey);
              return cacheKey == 'authoritative-artwork-cache-key' ? cachedFile : null;
            },
          ),
        );

    await tester.pumpWidget(buildArtwork());
    await tester.pump();
    expect(api.loadCalls, 1);
    expect(find.byKey(const Key('memory-cached-artwork-memory-recycled')), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(api.recycledResult.isCompleted, isFalse);
    expect(requestedKeys.last, 'authoritative-artwork-cache-key');
    expect(find.byKey(const Key('memory-cached-artwork-memory-recycled')), findsOneWidget);
    expect(find.byKey(const Key('memory-artwork-placeholder-memory-recycled')), findsNothing);
  });

  testWidgets('renders owner-scoped disk artwork before signed URL refresh completes', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
    await trustDisplayKey('owner-profile-memory-revision-cache-key');
    final conversation = ServerConversation(
      id: 'memory-cached',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    Widget buildArtwork() => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: SizedBox(
            width: 320,
            height: 180,
            child: MemoryArtworkImage(
              conversation: conversation,
              api: api,
              cachedFileLookup: (cacheKey) async {
                requestedKeys.add(cacheKey);
                return cachedFile;
              },
            ),
          ),
        );

    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-cached')), findsOneWidget);
    expect(find.byKey(const Key('memory-artwork-placeholder-memory-cached')), findsNothing);
    expect(api.remoteResult.isCompleted, isFalse);
    expect(requestedKeys, ['owner-profile-memory-revision-cache-key']);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-cached')), findsOneWidget);
    expect(api.loadCalls, 2, reason: 'route recreation may refresh the signed URL without blanking cached bytes');
    expect(requestedKeys, hasLength(2));
  });

  testWidgets('keeps cached artwork visible through a transient refresh failure', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    await trustDisplayKey('owner-profile-memory-revision-cache-key');
    final conversation = ServerConversation(
      id: 'memory-transient',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => cachedFile,
        ),
      ),
    );
    await tester.pump();
    api.remoteResult.complete(
      const MemoryArtworkResult(
        status: MemoryArtworkResultStatus.unavailable,
        failureCode: 'memory_artwork_unavailable',
      ),
    );
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-transient')), findsOneWidget);
    expect(find.text('Illustration unavailable'), findsNothing);
  });

  testWidgets('visible artwork never enqueues work just because the card is rendered', (tester) async {
    final api = _DelayedArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-ready-metadata',
      createdAt: DateTime(2026, 8, 26),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(conversation: conversation, api: api, cachedFileLookup: (_) async => null),
      ),
    );
    await tester.pump();

    expect(api.lastEnqueueIfMissing, isFalse);
  });

  testWidgets('an explicit artwork action enqueues only that unavailable memory', (tester) async {
    final api = _ManualGenerationArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-manual-generation',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: SizedBox(
          width: 320,
          height: 220,
          child: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            allowManualGeneration: true,
            maxTransientRetries: 0,
          ),
        ),
      ),
    );
    await tester.pump();

    expect(api.enqueueRequests, [isFalse], reason: 'rendering the unavailable card remains read-only');
    expect(find.text('Try artwork again'), findsOneWidget);
    expect(find.byIcon(Icons.auto_awesome_outlined), findsOneWidget);

    await tester.tap(find.byKey(const Key('memory-artwork-placeholder-memory-manual-generation')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('memory-artwork-placeholder-memory-manual-generation')));
    await tester.pump();

    expect(api.enqueueRequests, [isFalse, isTrue], reason: 'repeated taps cannot create duplicate generation calls');
    expect(find.text('Preparing illustration…'), findsOneWidget);

    api.generationResult.complete(const MemoryArtworkResult(status: MemoryArtworkResultStatus.generating));
    await tester.pump();
    expect(find.text('Preparing illustration…'), findsOneWidget);
  });

  testWidgets('a source photo keeps artwork retry and preparing progress visible', (tester) async {
    final api = _ManualGenerationArtworkApi();
    final photoData = await rootBundle.load('assets/images/onboarding-bg-1.webp');
    final conversation = ServerConversation(
      id: 'memory-source-photo-generation',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      photos: [
        ConversationPhoto(
          id: 'photo-1',
          base64: base64Encode(photoData.buffer.asUint8List()),
          createdAt: DateTime(2026, 9, 2),
        ),
      ],
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: SizedBox(
          width: 320,
          height: 220,
          child: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            allowManualGeneration: true,
            maxTransientRetries: 0,
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.byKey(const Key('memory-source-photo')), findsOneWidget);
    expect(find.byKey(const Key('memory-artwork-photo-retry-memory-source-photo-generation')), findsOneWidget);

    await tester.tap(find.byKey(const Key('memory-artwork-photo-retry-memory-source-photo-generation')));
    await tester.pump();

    expect(api.enqueueRequests, [isFalse, isTrue]);
    expect(find.byKey(const Key('memory-source-photo')), findsOneWidget);
    expect(
      find.byKey(const Key('memory-artwork-generation-progress-memory-source-photo-generation')),
      findsOneWidget,
    );
  });

  testWidgets('enabling bounded automatic recovery rechecks the same visible memory', (tester) async {
    final api = _ManualGenerationArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-hero-recovery',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
    );

    Widget buildArtwork({required bool recover}) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: SizedBox(
            width: 320,
            height: 220,
            child: MemoryArtworkImage(
              conversation: conversation,
              api: api,
              cachedFileLookup: (_) async => null,
              enqueueIfMissing: recover,
              maxTransientRetries: 0,
            ),
          ),
        );

    await tester.pumpWidget(buildArtwork(recover: false));
    await tester.pump();
    expect(api.enqueueRequests, [isFalse]);

    await tester.pumpWidget(buildArtwork(recover: true));
    await tester.pump();

    expect(api.enqueueRequests, [isFalse, isTrue]);
    expect(
      find.byKey(const Key('memory-artwork-generation-progress-memory-hero-recovery')),
      findsOneWidget,
    );
  });

  testWidgets('manual generation stays unavailable for consent and authority failures', (tester) async {
    final api = _ManualGenerationArtworkApi(initialFailureCode: 'memory_artwork_consent_required');
    final conversation = ServerConversation(
      id: 'memory-policy-blocked',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          allowManualGeneration: true,
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Illustration unavailable'), findsOneWidget);
    expect(find.text('Try artwork again'), findsNothing);
    expect(find.byIcon(Icons.auto_awesome_outlined), findsNothing);
    expect(api.enqueueRequests, [isFalse]);
  });

  testWidgets('manual generation fails closed for raw terminal artwork states', (tester) async {
    const terminalCodes = {
      'deletion_pending',
      'authority_changed',
      'preference_changed',
      'source_changed',
      'prompt_changed',
      'job_claim_invalid',
    };

    for (final terminalCode in terminalCodes) {
      final api = _ManualGenerationArtworkApi(initialFailureCode: terminalCode);
      final conversation = ServerConversation(
        id: 'memory-policy-$terminalCode',
        createdAt: DateTime(2026, 9, 2),
        structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      );

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            allowManualGeneration: true,
          ),
        ),
      );
      await tester.pump();

      expect(find.text('Try artwork again'), findsNothing, reason: terminalCode);
      expect(api.enqueueRequests, [isFalse], reason: terminalCode);
    }
  });

  testWidgets('suppresses cached artwork after a terminal policy response', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    await trustDisplayKey('owner-profile-memory-revision-cache-key');
    final conversation = ServerConversation(
      id: 'memory-declined',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => cachedFile,
          cacheEvictor: (_) async {},
        ),
      ),
    );
    await tester.pump();
    expect(find.byKey(const Key('memory-cached-artwork-memory-declined')), findsOneWidget);

    api.remoteResult.complete(const MemoryArtworkResult(status: MemoryArtworkResultStatus.declined));
    await tester.pump();
    await tester.pump();

    expect(find.byKey(const Key('memory-cached-artwork-memory-declined')), findsNothing);
    expect(find.text('Illustration unavailable'), findsOneWidget);
  });

  testWidgets('terminal suppression removes the authoritative alias before the card is recycled', (tester) async {
    final api = _ReadyThenSuppressedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
    final evictedKeys = <String>[];
    final evictionRelease = Completer<void>();
    final conversation = ServerConversation(
      id: 'memory-recycled-suppression',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A cached memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            refreshEpoch: refreshEpoch,
            cachedFileLookup: (cacheKey) async {
              requestedKeys.add(cacheKey);
              return cacheKey == 'suppressed-authoritative-cache-key' ? cachedFile : null;
            },
            cacheEvictor: (cacheKey) async {
              evictedKeys.add(cacheKey);
              await evictionRelease.future;
            },
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    expect(api.loadCalls, 1);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();
    await tester.pump();
    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-cached-artwork-memory-recycled-suppression')), findsNothing);
    expect(evictedKeys, isNotEmpty);

    requestedKeys.clear();
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();

    expect(api.loadCalls, 3);
    expect(requestedKeys, isEmpty, reason: 'terminal tombstones must block disk reads before async eviction finishes');
    expect(find.byKey(const Key('memory-cached-artwork-memory-recycled-suppression')), findsNothing);
    expect(find.byKey(const Key('memory-generated-artwork-memory-recycled-suppression')), findsNothing);

    evictionRelease.complete();
    await tester.pump();
    await tester.pump();
    expect(evictedKeys, containsAll({'suppressed-provisional-cache-key', 'suppressed-authoritative-cache-key'}));
    expect(find.byKey(const Key('memory-generated-artwork-memory-recycled-suppression')), findsOneWidget);
  });

  testWidgets('shows a friendly preparing state while artwork is being generated', (tester) async {
    final api = _DelayedArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-generating',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A new memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(conversation: conversation, api: api, cachedFileLookup: (_) async => null),
      ),
    );
    await tester.pump();

    expect(find.text('Preparing illustration…'), findsOneWidget);
    expect(
      find.byKey(const Key('memory-artwork-generation-progress-memory-generating')),
      findsOneWidget,
    );

    api.remoteResult.complete(const MemoryArtworkResult(status: MemoryArtworkResultStatus.generating));
    await tester.pump();
    expect(find.text('Preparing illustration…'), findsOneWidget);
  });

  testWidgets('continues refreshing a visible generating illustration until it becomes ready', (tester) async {
    final api = _RefreshingArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-eventually-ready',
      createdAt: DateTime(2026, 8, 26),
      structured: Structured('[Ella] A new memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          retryDelay: const Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Preparing illustration…'), findsOneWidget);
    expect(api.loadCalls, 1);
    expect(api.lastPollAttempts, 0, reason: 'each visible card performs one display read per queue revision');

    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-eventually-ready')), findsOneWidget);
  });

  testWidgets('a completed queue refresh immediately rechecks visible artwork without enqueuing', (tester) async {
    final api = _RefreshingArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-queue-complete',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            refreshEpoch: refreshEpoch,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    expect(api.loadCalls, 1);
    expect(api.lastEnqueueIfMissing, isFalse);
    expect(find.text('Preparing illustration…'), findsOneWidget);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(api.lastEnqueueIfMissing, isFalse);
    expect(find.byKey(const Key('memory-generated-artwork-memory-queue-complete')), findsOneWidget);
  });

  testWidgets('waits for a parent refresh after a terminal unavailable result without creating artwork', (
    tester,
  ) async {
    final api = _TerminalThenReadyArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-terminal-unavailable',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            retryDelay: const Duration(milliseconds: 10),
            refreshEpoch: refreshEpoch,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(api.loadCalls, 1, reason: 'terminal missing artwork must not poll forever');
    expect(api.lastEnqueueIfMissing, isFalse, reason: 'displaying a card must not spend image allowance');
    expect(find.text('Illustration unavailable'), findsOneWidget);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();

    expect(api.loadCalls, 2, reason: 'a later queue revision gets one fresh display read');
    expect(find.byKey(const Key('memory-generated-artwork-memory-terminal-unavailable')), findsOneWidget);
  });

  testWidgets('an authority change evicts cached artwork before fetching the replacement account result', (
    tester,
  ) async {
    final api = _AuthorityRefreshArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final evicted = <String>[];
    final conversation = ServerConversation(
      id: 'memory-authority-refresh',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'revision-a',
      ),
    );

    Widget buildArtwork(int authorityEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            authorityEpoch: authorityEpoch,
            cachedFileLookup: (_) async => cachedFile,
            cacheEvictor: (cacheKey) async => evicted.add(cacheKey),
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    expect(find.byKey(const Key('memory-cached-artwork-memory-authority-refresh')), findsOneWidget);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();

    expect(evicted, ['authority-artwork-cache-key']);
    expect(api.loadCalls, 2);
  });

  testWidgets('retries when replacement account artwork authority settles after the widget refresh', (tester) async {
    final api = _AuthorityBecomesReadyArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-settling-authority',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'revision-a',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          retryDelay: const Duration(milliseconds: 10),
          authorityEpoch: 1,
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-settling-authority')), findsOneWidget);
  });

  testWidgets('recovered authority retains its disk alias after the card is recycled', (tester) async {
    final api = _RecoveredAuthorityRecycledArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
    final conversation = ServerConversation(
      id: 'memory-recovered-authority-recycled',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'revision-a',
      ),
    );

    Widget buildArtwork() => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            retryDelay: const Duration(milliseconds: 10),
            authorityEpoch: 1,
            cachedFileLookup: (cacheKey) async {
              requestedKeys.add(cacheKey);
              return cacheKey == 'recovered-authority-authoritative-cache-key' ? cachedFile : null;
            },
          ),
        );

    await tester.pumpWidget(buildArtwork());
    await tester.pump();
    expect(api.loadCalls, 1);

    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-recovered-authority-recycled')), findsOneWidget);

    requestedKeys.clear();
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(buildArtwork());
    await tester.pump();

    expect(api.loadCalls, 3);
    expect(api.recycledResult.isCompleted, isFalse);
    expect(requestedKeys, ['recovered-authority-authoritative-cache-key']);
    expect(find.byKey(const Key('memory-cached-artwork-memory-recovered-authority-recycled')), findsOneWidget);
    expect(find.byKey(const Key('memory-artwork-placeholder-memory-recovered-authority-recycled')), findsNothing);
  });

  testWidgets('authority drift during cache cleanup cannot publish or render the stale ready response', (tester) async {
    final authority = _MutableArtworkAuthority();
    final api = _AuthorityDriftDuringCachePublishApi(authority);
    final evictionRelease = Completer<void>();
    var evictionCalls = 0;
    const provisionalKey = 'drift-provisional-cache-key';
    const authoritativeKey = 'drift-authoritative-cache-key';
    MemoryArtworkCache.suppressDisplayCacheKeys({provisionalKey, authoritativeKey});
    final conversation = ServerConversation(
      id: 'memory-authority-drift-during-cache-publish',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'revision-a',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          cacheEvictor: (_) {
            evictionCalls += 1;
            return evictionRelease.future;
          },
        ),
      ),
    );
    await tester.pump();
    expect(evictionCalls, 2);

    authority.current = false;
    evictionRelease.complete();
    await tester.pump();

    expect(find.byKey(const Key('memory-generated-artwork-memory-authority-drift-during-cache-publish')), findsNothing);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(provisionalKey), isEmpty);
    expect(MemoryArtworkCache.resolveDisplayCacheKey(authoritativeKey), isEmpty);
  });

  testWidgets('a failed terminal-cache cleanup retries before publishing ready artwork', (tester) async {
    final api = _RetryingReadyArtworkApi();
    var evictionCalls = 0;
    const cacheKey = 'retrying-ready-cache-key';
    MemoryArtworkCache.suppressDisplayCacheKeys({cacheKey});
    final conversation = ServerConversation(
      id: 'memory-retrying-ready-cache-cleanup',
      createdAt: DateTime(2026, 9, 2),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.ready,
        styleVersion: memoryArtworkDefaultStyle,
        enrichmentRevision: 'revision-a',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          retryDelay: const Duration(milliseconds: 10),
          cachedFileLookup: (_) async => null,
          cacheEvictor: (_) async {
            evictionCalls += 1;
            if (evictionCalls == 1) throw Exception('disk busy');
          },
        ),
      ),
    );
    await tester.pump();
    expect(find.byKey(const Key('memory-generated-artwork-memory-retrying-ready-cache-cleanup')), findsNothing);

    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(evictionCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-retrying-ready-cache-cleanup')), findsOneWidget);
  });

  testWidgets('bounds replacement-authority retries instead of polling a failing endpoint indefinitely', (
    tester,
  ) async {
    final api = _PersistentlyUnavailableAuthorityArtworkApi('memory_artwork_authority_unavailable');
    final conversation = ServerConversation(
      id: 'memory-persistently-unavailable-authority',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          retryDelay: const Duration(milliseconds: 10),
          maxAuthorityUnavailableRetries: 2,
          authorityEpoch: 1,
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(api.loadCalls, 3, reason: 'initial request plus the configured bounded retries');
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('does not renew an exhausted authority retry budget for a queue refresh', (tester) async {
    final api = _PersistentlyUnavailableAuthorityArtworkApi('memory_artwork_authority_unavailable');
    final conversation = ServerConversation(
      id: 'memory-authority-budget-refresh',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            retryDelay: const Duration(milliseconds: 10),
            maxAuthorityUnavailableRetries: 2,
            authorityEpoch: 1,
            refreshEpoch: refreshEpoch,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    expect(api.loadCalls, 3);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    expect(api.loadCalls, 3, reason: 'queue completion is not a new authority');
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('a successful final authority retry permits later queue refreshes', (tester) async {
    final api = _AuthoritySettlesAfterFinalRetryArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-authority-final-retry',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            retryDelay: const Duration(milliseconds: 10),
            maxAuthorityUnavailableRetries: 2,
            authorityEpoch: 1,
            refreshEpoch: refreshEpoch,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 3, reason: 'the second retry is allowed to observe recovered authority');
    expect(find.byKey(const Key('memory-generated-artwork-memory-authority-final-retry')), findsOneWidget);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();
    expect(api.loadCalls, 4, reason: 'a successful final retry must not permanently freeze later refreshes');
  });

  testWidgets('bounds runtime-authority retries instead of polling a failing endpoint indefinitely', (tester) async {
    final api = _PersistentlyUnavailableAuthorityArtworkApi('memory_artwork_runtime_authority_unavailable');
    final conversation = ServerConversation(
      id: 'memory-persistently-unavailable-runtime-authority',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.ready),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          retryDelay: const Duration(milliseconds: 10),
          maxAuthorityUnavailableRetries: 2,
          authorityEpoch: 1,
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(api.loadCalls, 3, reason: 'initial request plus the configured bounded retries');
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('keeps published artwork visible while polling for a replacement style', (tester) async {
    final api = _PublishedRefreshArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-style-refresh',
      createdAt: DateTime(2026, 8, 26),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(
        status: MemoryArtworkStatus.generating,
        styleVersion: memoryArtworkAnimeStorybookStyle,
        enrichmentRevision: 'summary-revision-1',
      ),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          retryDelay: const Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();

    expect(api.loadCalls, 1);
    expect(find.byKey(const Key('memory-generated-artwork-memory-style-refresh')), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-style-refresh')), findsOneWidget);
  });

  testWidgets('failed signed image load evicts cache and refreshes the signed URL', (tester) async {
    final api = _RefreshingArtworkApi();
    final evictedKeys = <String>[];
    final conversation = ServerConversation(
      id: 'memory-expired-signed-url',
      createdAt: DateTime(2026, 8, 26),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          cacheEvictor: (cacheKey) async => evictedKeys.add(cacheKey),
          retryDelay: const Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    final image = tester.widget<CachedNetworkImage>(
      find.byKey(const Key('memory-generated-artwork-network-memory-expired-signed-url-0')),
    );
    image.errorListener!(Exception('expired signed URL'));
    await tester.pump();

    expect(evictedKeys, ['ready-artwork-cache-key']);
    expect(find.text('Preparing illustration…'), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    expect(api.loadCalls, 3, reason: 'a failed image download must obtain a fresh signed URL');
  });

  testWidgets('bounds repeated signed image recovery without enqueuing artwork work', (tester) async {
    final api = _RefreshingArtworkApi();
    final evictedKeys = <String>[];
    final conversation = ServerConversation(
      id: 'memory-repeated-signed-url-failure',
      createdAt: DateTime(2026, 8, 31),
      structured: Structured('[Ella] A memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MemoryArtworkImage(
          conversation: conversation,
          api: api,
          cachedFileLookup: (_) async => null,
          cacheEvictor: (cacheKey) async => evictedKeys.add(cacheKey),
          maxImageDownloadRetries: 2,
          retryDelay: const Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 10));
    await tester.pump();

    Future<void> failSignedImage() async {
      final image = tester.widget<CachedNetworkImage>(
        find.byKey(const Key('memory-generated-artwork-network-memory-repeated-signed-url-failure-0')),
      );
      image.errorListener!(Exception('expired signed URL'));
      await tester.pump();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 10));
      await tester.pump();
    }

    await failSignedImage();
    await failSignedImage();

    final exhaustedImage = tester.widget<CachedNetworkImage>(
      find.byKey(const Key('memory-generated-artwork-network-memory-repeated-signed-url-failure-0')),
    );
    exhaustedImage.errorListener!(Exception('expired signed URL again'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));

    expect(api.loadCalls, 4, reason: 'initial readiness plus two bounded signed-URL recovery reads');
    expect(api.lastEnqueueIfMissing, isFalse);
    expect(evictedKeys, hasLength(2));
    expect(find.text('Illustration unavailable'), findsOneWidget);
  });

  testWidgets('does not poll a terminal enrichment result until the parent publishes a new revision', (tester) async {
    final api = _RecoveringEnrichmentArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-awaiting-enrichment',
      createdAt: DateTime(2026, 8, 26),
      structured: Structured('[Ella] A new memory', '[Ella] A useful generic summary.'),
    );

    Widget buildArtwork(int refreshEpoch) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: conversation,
            api: api,
            cachedFileLookup: (_) async => null,
            retryDelay: const Duration(milliseconds: 10),
            refreshEpoch: refreshEpoch,
          ),
        );

    await tester.pumpWidget(buildArtwork(0));
    await tester.pump();

    expect(find.text('Illustration unavailable'), findsOneWidget);
    expect(api.loadCalls, 1);

    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump();
    expect(api.loadCalls, 1);

    await tester.pumpWidget(buildArtwork(1));
    await tester.pump();
    expect(api.loadCalls, 2);
    expect(find.byKey(const Key('memory-generated-artwork-memory-awaiting-enrichment')), findsOneWidget);
  });

  testWidgets('refreshes unavailable hero artwork when conversation enrichment becomes terminal', (tester) async {
    final api = _RecoveringEnrichmentArtworkApi();

    ServerConversation conversation({required bool terminal}) => ServerConversation(
          id: 'memory-enrichment-transition',
          createdAt: DateTime(2026, 8, 26),
          structured: Structured('[Ella] A new memory', '[Ella] A useful generic summary.'),
          enrichmentState: terminal
              ? const {'status': 'completed', 'canonical_status': 'completed', 'pending': false}
              : const {'status': 'processing', 'canonical_status': 'pending', 'pending': true},
          activeSummaryVersionId: terminal ? 'summary-version-2' : null,
        );

    Widget buildArtwork(ServerConversation value) => MaterialApp(
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: MemoryArtworkImage(
            conversation: value,
            api: api,
            cachedFileLookup: (_) async => null,
            retryDelay: const Duration(milliseconds: 10),
            enqueueIfMissing: true,
          ),
        );

    await tester.pumpWidget(buildArtwork(conversation(terminal: false)));
    await tester.pump();

    expect(find.text('Illustration unavailable'), findsOneWidget);
    expect(api.loadCalls, 1);
    expect(api.enqueueRequests, [isTrue]);

    await tester.pumpWidget(buildArtwork(conversation(terminal: true)));
    await tester.pump();

    expect(api.loadCalls, 2);
    expect(api.enqueueRequests, [isTrue, isTrue]);
    expect(find.byKey(const Key('memory-generated-artwork-memory-enrichment-transition')), findsOneWidget);
  });

  testWidgets('compact preparing state fits at 200 percent text scale', (tester) async {
    final api = _DelayedArtworkApi();
    final conversation = ServerConversation(
      id: 'memory-generating-compact',
      createdAt: DateTime(2026, 8, 25),
      structured: Structured('[Ella] A new memory', '[Ella] A useful enriched summary.'),
      artwork: const MemoryArtworkState(status: MemoryArtworkStatus.generating),
    );

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MediaQuery(
          data: const MediaQueryData(textScaler: TextScaler.linear(2)),
          child: Center(
            child: SizedBox(
              width: 112,
              height: 112,
              child: MemoryArtworkImage(conversation: conversation, api: api, cachedFileLookup: (_) async => null),
            ),
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.text('Preparing illustration…'), findsOneWidget);
    expect(find.byIcon(Icons.brush_outlined), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
