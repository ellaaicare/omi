import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:cached_network_image/cached_network_image.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/l10n/app_localizations.dart';

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
    return MemoryArtworkResult(
      status: MemoryArtworkResultStatus.unavailable,
      failureCode: failureCode,
    );
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
  testWidgets('renders owner-scoped disk artwork before signed URL refresh completes', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
    final requestedKeys = <String>[];
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
        home: MemoryArtworkImage(conversation: conversation, api: api, cachedFileLookup: (_) async => cachedFile),
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

  testWidgets('suppresses cached artwork after a terminal policy response', (tester) async {
    final api = _DelayedArtworkApi();
    final cachedFile = File('assets/images/onboarding-bg-1.webp');
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
        home: MemoryArtworkImage(conversation: conversation, api: api, cachedFileLookup: (_) async => cachedFile),
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

  testWidgets('waits for a parent refresh after a terminal unavailable result without creating artwork',
      (tester) async {
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

  testWidgets('bounds replacement-authority retries instead of polling a failing endpoint indefinitely',
      (tester) async {
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
