import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/env/env.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _MutableAuthority implements AccountCommitAuthority {
  _MutableAuthority(this.uid);

  @override
  final String uid;
  bool current = true;

  @override
  bool isCurrent() => current;

  @override
  bool isExactCurrent() => current;
}

class _InitialLoadProvider extends ConversationProvider {
  _InitialLoadProvider(_MutableAuthority owner, Completer<ConversationsFetchResult> primary)
      : super(
          authenticatedUid: () => owner.uid,
          activeAuthority: () => owner,
          conversationsFetchCall: () => primary.future,
          failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
        );

  int dailySummaryReads = 0;

  @override
  Future<void> checkHasDailySummaries() async {
    dailySummaryReads++;
  }
}

class _TestEnv implements EnvFields {
  @override
  String? get apiBaseUrl => 'https://api.ella.test/';
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get growthbookApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get mixpanelProjectToken => null;
  @override
  String? get openAIAPIKey => null;
  @override
  bool? get useAuthCustomToken => false;
  @override
  bool? get useWebAuth => false;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init(_TestEnv());

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  ServerConversation conversation(String id, {bool discarded = false}) {
    final startedAt = DateTime.parse('2026-07-08T19:00:00Z');
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 10)),
      structured: Structured('Memory $id', 'Overview'),
      discarded: discarded,
    );
  }

  test('Ella-visible conversations never leak discarded cache records', () {
    final provider = ConversationProvider();
    addTearDown(provider.dispose);
    provider.conversations = [conversation('kept'), conversation('discarded', discarded: true)];

    expect(provider.visibleConversations.map((item) => item.id), ['kept']);

    provider.showDiscardedConversations = true;
    expect(provider.visibleConversations.map((item) => item.id), ['kept', 'discarded']);
  });

  test('legacy cache without an owning uid is rejected', () async {
    final legacy = conversation('legacy');
    SharedPreferences.setMockInitialValues({
      'uid': 'current-user',
      'cachedConversations': [jsonEncode(legacy.toJson())],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().cachedConversations, isEmpty);
  });

  test('cache for the current uid remains available', () async {
    final cached = conversation('current');
    SharedPreferences.setMockInitialValues({
      'uid': 'current-user',
      'cachedConversationsUid': 'current-user',
      'cachedConversations': [jsonEncode(cached.toJson())],
    });
    await SharedPreferencesUtil.init();

    expect(SharedPreferencesUtil().cachedConversations.single.id, 'current');
  });

  test('owned completed cache is visible while the initial canonical GET is pending', () async {
    SharedPreferencesUtil().uid = 'current-user';
    SharedPreferencesUtil().cachedConversations = [conversation('cached')];
    final result = Completer<ConversationsFetchResult>();
    final authority = _MutableAuthority('current-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      authenticatedUid: () => 'current-user',
      conversationsFetchCall: () => result.future,
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
    );
    final loading = provider.fetchConversations();
    addTearDown(() async {
      if (!result.isCompleted) result.complete(const ConversationsFetchResult.success([]));
      await loading;
      provider.dispose();
    });

    expect(provider.visibleConversations.map((item) => item.id), ['cached']);
    expect(provider.groupedConversations.values.expand((items) => items).map((item) => item.id), ['cached']);
    expect(provider.hasLoadedConversations, isFalse);
    expect(provider.hasFreshConversations, isFalse);
    expect(provider.isShowingCachedConversations, isTrue);
    expect(provider.isLoadingConversations, isTrue);
    expect(provider.hasMoreConversations, isTrue);
  });

  group('owned initial cache projection', () {
    ConversationProvider pendingProvider(
      Completer<ConversationsFetchResult> primary, {
      _MutableAuthority? authority,
      String Function()? signedUid,
      ConversationsFetchCall? failures,
      ConversationDeleteCall? delete,
      Duration timeout = const Duration(seconds: 15),
    }) =>
        ConversationProvider(
          authenticatedUid: signedUid ?? (() => 'current-user'),
          activeAuthority: () => authority,
          conversationsFetchCall: () => primary.future,
          failedConversationsFetchCall: failures ?? (() async => const ConversationsFetchResult.success([])),
          conversationDeleteCall: delete,
          conversationsFetchTimeout: timeout,
        );

    for (final condition in [
      'signed out',
      'wrong signed uid',
      'wrong preferences uid',
      'unowned cache',
      'foreign cache',
      'no authority',
      'wrong authority uid',
      'retired authority',
      'unavailable identity',
    ]) {
      test('$condition does not hydrate or rewrite the cache while GET is pending', () async {
        SharedPreferencesUtil().uid = 'current-user';
        SharedPreferencesUtil().cachedConversations = [conversation('cached')];
        final authority = _MutableAuthority(condition == 'wrong authority uid' ? 'other-user' : 'current-user');
        if (condition == 'retired authority') authority.current = false;
        if (condition == 'wrong preferences uid') SharedPreferencesUtil().uid = 'other-user';
        if (condition == 'unowned cache') await SharedPreferencesUtil().saveString('cachedConversationsUid', '');
        if (condition == 'foreign cache') {
          await SharedPreferencesUtil().saveString('cachedConversationsUid', 'other-user');
        }
        final originalBytes = SharedPreferencesUtil().getStringList('cachedConversations');
        final primary = Completer<ConversationsFetchResult>();
        final provider = pendingProvider(
          primary,
          authority: condition == 'no authority' ? null : authority,
          signedUid: () {
            if (condition == 'unavailable identity') throw StateError('Synthetic identity unavailable');
            if (condition == 'signed out') return '';
            return condition == 'wrong signed uid' ? 'other-user' : 'current-user';
          },
        );
        final loading = provider.fetchConversations();
        addTearDown(() async {
          if (!primary.isCompleted) primary.complete(const ConversationsFetchResult.success([]));
          await loading;
          provider.dispose();
        });

        expect(provider.visibleConversations, isEmpty);
        expect(provider.isShowingCachedConversations, isFalse);
        expect(SharedPreferencesUtil().getStringList('cachedConversations'), originalBytes);
      });
    }

    for (final filter in ['folder', 'date', 'starred', 'daily summaries', 'existing projection', 'fresh empty']) {
      test('$filter does not hydrate the global startup cache', () async {
        SharedPreferencesUtil().uid = 'current-user';
        SharedPreferencesUtil().cachedConversations = [conversation('cached')];
        final primary = Completer<ConversationsFetchResult>();
        final provider = pendingProvider(primary, authority: _MutableAuthority('current-user'));
        switch (filter) {
          case 'folder':
            provider.selectedFolderId = 'folder';
          case 'date':
            provider.selectedDate = DateTime(2026, 7, 8);
          case 'starred':
            provider.showStarredOnly = true;
          case 'daily summaries':
            provider.showDailySummaries = true;
          case 'existing projection':
            provider.conversations = [conversation('existing')];
          case 'fresh empty':
            provider.hasLoadedConversations = true;
            provider.hasFreshConversations = true;
        }
        final loading = provider.fetchConversations();
        addTearDown(() async {
          if (!primary.isCompleted) primary.complete(const ConversationsFetchResult.success([]));
          await loading;
          provider.dispose();
        });

        expect(provider.conversations.map((item) => item.id), filter == 'existing projection' ? ['existing'] : []);
        expect(provider.isShowingCachedConversations, isFalse);
      });
    }

    test('completed cache preserves metadata and excludes deleted, non-completed and hidden rows', () async {
      SharedPreferencesUtil().uid = 'current-user';
      final enriched = ServerConversation.fromJson({
        ...conversation('kept').toJson(),
        'active_summary_version_id': 'canonical-v2',
        'enrichment_state': {'status': 'writeback_applied', 'result_summary_version_id': 'canonical-v2'},
      });
      SharedPreferencesUtil().cachedConversations = [
        enriched,
        conversation('processing')..status = ConversationStatus.processing,
        conversation('failed')..status = ConversationStatus.failed,
        ServerConversation.fromJson({...conversation('deleted').toJson(), 'deleted': true}),
        conversation('discarded', discarded: true),
        ServerConversation.fromJson({
          ...conversation('short').toJson(),
          'finished_at': conversation('short').startedAt!.add(const Duration(seconds: 1)).toIso8601String(),
        }),
      ];
      final bytes = SharedPreferencesUtil().getStringList('cachedConversations');
      final primary = Completer<ConversationsFetchResult>();
      final provider = pendingProvider(primary, authority: _MutableAuthority('current-user'))
        ..shortConversationThreshold = 60;
      final loading = provider.fetchConversations();
      addTearDown(() async {
        if (!primary.isCompleted) primary.complete(const ConversationsFetchResult.success([]));
        await loading;
        provider.dispose();
      });

      expect(provider.visibleConversations.map((item) => item.id), ['kept']);
      expect(provider.conversations.map((item) => item.id), ['kept', 'discarded', 'short']);
      expect(provider.visibleConversations.single.activeSummaryVersionId, 'canonical-v2');
      expect(provider.visibleConversations.single.enrichmentState, enriched.enrichmentState);
      expect(provider.processingConversations, isEmpty);
      expect(SharedPreferencesUtil().getStringList('cachedConversations'), bytes);
    });

    test('malformed owned cache does not prevent the canonical GET', () async {
      SharedPreferencesUtil().uid = 'current-user';
      await SharedPreferencesUtil().saveString('cachedConversationsUid', 'current-user');
      await SharedPreferencesUtil().saveStringList('cachedConversations', ['invalid JSON']);
      final primary = Completer<ConversationsFetchResult>();
      final provider = pendingProvider(primary, authority: _MutableAuthority('current-user'));
      addTearDown(provider.dispose);
      final loading = provider.fetchConversations();
      expect(provider.conversations, isEmpty);
      primary.complete(ConversationsFetchResult.success([conversation('canonical')]));
      await loading;
      expect(provider.conversations.single.id, 'canonical');
      expect(provider.hasFreshConversations, isTrue);
    });

    for (final outcome in ['empty', 'completed', 'failure', 'timeout']) {
      test('$outcome canonical result preserves freshness and cache semantics', () async {
        SharedPreferencesUtil().uid = 'current-user';
        SharedPreferencesUtil().cachedConversations = [conversation('cached')];
        final primary = Completer<ConversationsFetchResult>();
        final provider = pendingProvider(primary,
            authority: _MutableAuthority('current-user'),
            timeout: outcome == 'timeout' ? const Duration(milliseconds: 10) : const Duration(seconds: 15));
        addTearDown(provider.dispose);
        final loading = provider.fetchConversations();
        expect(provider.conversations.single.id, 'cached');
        if (outcome != 'timeout') {
          primary.complete(outcome == 'failure'
              ? const ConversationsFetchResult.failure()
              : ConversationsFetchResult.success(outcome == 'completed' ? [conversation('canonical')] : []));
        }
        await loading;
        final fresh = outcome == 'empty' || outcome == 'completed';
        final expected = outcome == 'empty' ? <String>[] : [fresh ? 'canonical' : 'cached'];
        expect(provider.hasLoadedConversations, isTrue);
        expect(provider.hasFreshConversations, fresh);
        expect(provider.isShowingCachedConversations, !fresh);
        expect(provider.isLoadingConversations, isFalse);
        expect(provider.conversations.map((item) => item.id), expected);
        expect(provider.searchedConversations.map((item) => item.id), expected);
        expect(provider.groupedConversations.values.expand((items) => items).map((item) => item.id), expected);
        expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), expected);
        if (!primary.isCompleted) primary.complete(const ConversationsFetchResult.failure());
      });
    }

    for (final transition in ['signed uid', 'preferences uid', 'authority', 'barrier', 'reset', 'dispose']) {
      test('$transition rejects delayed primary and failed-record responses from hydrated cache', () async {
        SharedPreferencesUtil().uid = 'current-user';
        SharedPreferencesUtil().cachedConversations = [conversation('cached')];
        final primary = Completer<ConversationsFetchResult>();
        final failures = Completer<ConversationsFetchResult>();
        final authority = _MutableAuthority('current-user');
        var signedUid = 'current-user';
        final provider =
            pendingProvider(primary, authority: authority, signedUid: () => signedUid, failures: () => failures.future);
        var notifications = 0;
        provider.addListener(() => notifications++);
        final loading = provider.fetchConversations();
        expect(provider.conversations.single.id, 'cached');
        switch (transition) {
          case 'signed uid':
            signedUid = 'other-user';
          case 'preferences uid':
            SharedPreferencesUtil().uid = 'other-user';
          case 'authority':
            authority.current = false;
          case 'barrier':
            EllaAccountCommitBarrier.quiesceForAccountTransition();
          case 'reset':
            provider.reset();
          case 'dispose':
            provider.dispose();
        }
        final cacheBytes = SharedPreferencesUtil().getStringList('cachedConversations');
        final notificationsAfterTransition = notifications;
        primary.complete(ConversationsFetchResult.success([conversation('stale-primary')]));
        failures.complete(ConversationsFetchResult.success([
          ServerConversation.fromJson({
            ...conversation('stale-failed').toJson(),
            'status': 'failed',
            'processing_error': 'conversation_summary_failed',
          }),
        ]));
        await loading;
        await pumpEventQueue();
        expect(provider.conversations.where((item) => item.id == 'stale-primary'), isEmpty);
        expect(provider.failedConversations, isEmpty);
        expect(SharedPreferencesUtil().getStringList('cachedConversations'), cacheBytes);
        if (transition == 'dispose') {
          expect(notifications, notificationsAfterTransition);
          EllaAccountCommitBarrier.quiesceForAccountTransition();
        } else {
          expect(provider.conversations, isEmpty);
          provider.dispose();
        }
      });
    }

    test('cached projection remains account-fenced after primary failure completes', () async {
      SharedPreferencesUtil().uid = 'current-user';
      SharedPreferencesUtil().cachedConversations = [conversation('cached')];
      final primary = Completer<ConversationsFetchResult>();
      final provider = pendingProvider(primary, authority: _MutableAuthority('current-user'));
      addTearDown(provider.dispose);
      final loading = provider.fetchConversations();
      primary.complete(const ConversationsFetchResult.failure());
      await loading;
      expect(provider.conversations.single.id, 'cached');
      EllaAccountCommitBarrier.quiesceForAccountTransition();
      expect(provider.conversations, isEmpty);
      expect(provider.isShowingCachedConversations, isFalse);
    });

    for (final transition in ['reset', 'dispose']) {
      test('$transition during initial cached load cannot start a later daily-summary request', () async {
        SharedPreferencesUtil().uid = 'current-user';
        SharedPreferencesUtil().cachedConversations = [conversation('cached')];
        final primary = Completer<ConversationsFetchResult>();
        final provider = _InitialLoadProvider(_MutableAuthority('current-user'), primary);
        final loading = provider.ensureFreshConversations();
        expect(provider.conversations.single.id, 'cached');
        if (transition == 'dispose') {
          provider.dispose();
        } else {
          provider.reset();
        }
        primary.complete(ConversationsFetchResult.success([conversation('stale')]));
        await loading;
        expect(provider.dailySummaryReads, 0);
        if (transition != 'dispose') provider.dispose();
      });
    }

    test('failed-record refresh still completes independently after cached rows become canonical', () async {
      SharedPreferencesUtil().uid = 'current-user';
      SharedPreferencesUtil().cachedConversations = [conversation('cached')];
      final primary = Completer<ConversationsFetchResult>();
      final failures = Completer<ConversationsFetchResult>();
      final provider =
          pendingProvider(primary, authority: _MutableAuthority('current-user'), failures: () => failures.future);
      addTearDown(provider.dispose);
      final loading = provider.fetchConversations();
      primary.complete(ConversationsFetchResult.success([conversation('canonical')]));
      await loading;
      failures.complete(ConversationsFetchResult.success([
        ServerConversation.fromJson({
          ...conversation('failed-summary').toJson(),
          'status': 'failed',
          'processing_error': 'conversation_summary_failed',
        }),
      ]));
      await pumpEventQueue();
      expect(provider.failedConversations.single.id, 'failed-summary');
      expect(provider.conversations.single.id, 'canonical');
      expect(provider.isShowingCachedConversations, isFalse);
    });

    test('permanent deletion during hydration cannot be restored by canonical response or retained cache', () async {
      SharedPreferencesUtil().uid = 'current-user';
      final removed = conversation('deleted');
      SharedPreferencesUtil().cachedConversations = [removed, conversation('kept')];
      final primary = Completer<ConversationsFetchResult>();
      final provider =
          pendingProvider(primary, authority: _MutableAuthority('current-user'), delete: (_, __) async => true);
      addTearDown(provider.dispose);
      final loading = provider.fetchConversations();
      expect(await provider.deleteConversationPermanently(removed), isTrue);
      primary.complete(ConversationsFetchResult.success([removed, conversation('kept')]));
      await loading;
      expect(provider.conversations.map((item) => item.id), ['kept']);
      expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), ['kept']);
    });

    test('an older overlapping GET cannot replace the newer canonical projection or pagination', () async {
      SharedPreferencesUtil().uid = 'current-user';
      SharedPreferencesUtil().cachedConversations = [conversation('cached')];
      final first = Completer<ConversationsFetchResult>();
      final second = Completer<ConversationsFetchResult>();
      final pageOffsets = <int>[];
      var reads = 0;
      final authority = _MutableAuthority('current-user');
      final provider = ConversationProvider(
        authenticatedUid: () => 'current-user',
        activeAuthority: () => authority,
        conversationsFetchCall: () => reads++ == 0 ? first.future : second.future,
        failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
        conversationsPageFetchCall: ({required limit, required offset}) async {
          pageOffsets.add(offset);
          return const ConversationsFetchResult.success([]);
        },
      );
      addTearDown(provider.dispose);
      final oldLoading = provider.fetchConversations();
      final newLoading = provider.fetchConversations();
      second.complete(ConversationsFetchResult.success(List.generate(50, (index) => conversation('new-$index'))));
      await newLoading;
      first.complete(ConversationsFetchResult.success([conversation('old')]));
      await oldLoading;
      expect(provider.conversations.length, 50);
      expect(provider.hasFreshConversations, isTrue);
      expect(provider.isShowingCachedConversations, isFalse);
      await provider.getMoreConversationsFromServer();
      expect(pageOffsets, [50]);
      expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id),
          unorderedEquals(List.generate(50, (index) => 'new-$index')));
    });
  });

  test('confirmed permanent deletion removes every local projection and cache entry', () async {
    SharedPreferences.setMockInitialValues({'uid': 'current-user'});
    await SharedPreferencesUtil.init();
    final deleted = conversation('delete-me');
    final kept = conversation('keep-me');
    final requestedIds = <String>[];
    final authority = _MutableAuthority('current-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (id, exactAuthority) async {
        expect(exactAuthority.uid, authority.uid);
        expect(exactAuthority.isExactCurrent(), isTrue);
        requestedIds.add(id);
        return true;
      },
    );
    addTearDown(provider.dispose);
    provider.conversations = [deleted, kept];
    provider.searchedConversations = [deleted, kept];
    provider.failedConversations = [deleted];
    SharedPreferencesUtil().cachedConversations = [deleted, kept];

    final result = await provider.deleteConversationPermanently(deleted);

    expect(result, isTrue);
    expect(requestedIds, ['delete-me']);
    expect(provider.conversations.map((item) => item.id), ['keep-me']);
    expect(provider.searchedConversations.map((item) => item.id), ['keep-me']);
    expect(provider.failedConversations, isEmpty);
    expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), ['keep-me']);
  });

  test('confirmed permanent deletion purges global cache while a folder is selected', () async {
    SharedPreferences.setMockInitialValues({'uid': 'current-user'});
    await SharedPreferencesUtil.init();
    final deleted = conversation('delete-from-folder');
    final globallyCached = conversation('global-cache-entry');
    final authority = _MutableAuthority('current-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (_, __) async => true,
    )
      ..selectedFolderId = 'folder-1'
      ..conversations = [deleted];
    addTearDown(provider.dispose);
    SharedPreferencesUtil().cachedConversations = [deleted, globallyCached];

    expect(await provider.deleteConversationPermanently(deleted), isTrue);

    expect(provider.conversations, isEmpty);
    expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), ['global-cache-entry']);
  });

  test('failed permanent deletion preserves every local projection', () async {
    final memory = conversation('still-here');
    final authority = _MutableAuthority('test-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (_, __) async => false,
    );
    addTearDown(provider.dispose);
    provider.conversations = [memory];
    provider.searchedConversations = [memory];

    final result = await provider.deleteConversationPermanently(memory);

    expect(result, isFalse);
    expect(provider.conversations, [memory]);
    expect(provider.searchedConversations, [memory]);
  });

  test('thrown permanent deletion request preserves every local projection', () async {
    final memory = conversation('still-here-after-error');
    final authority = _MutableAuthority('test-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (_, __) async => throw StateError('network unavailable'),
    );
    addTearDown(provider.dispose);
    provider.conversations = [memory];
    provider.searchedConversations = [memory];

    final result = await provider.deleteConversationPermanently(memory);

    expect(result, isFalse);
    expect(provider.conversations, [memory]);
    expect(provider.searchedConversations, [memory]);
  });

  test('account transition rejects delayed delete success without mutating replacement state', () async {
    SharedPreferences.setMockInitialValues({'uid': 'uid-a'});
    await SharedPreferencesUtil.init();
    final authority = _MutableAuthority('uid-a');
    final response = Completer<http.Response?>();
    late ExactAccountAuthorityVerifier requestAuthority;
    final original = conversation('account-a-memory');
    final replacement = conversation('account-b-memory');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (id, exactAuthority) {
        expect(id, 'account-a-memory');
        return deleteConversationServer(
          id,
          expectedAuthenticatedUid: exactAuthority.uid,
          exactAuthority: exactAuthority,
          transport: ({required url, required expectedAuthenticatedUid, required exactAuthority}) {
            expect(url, endsWith('/v1/conversations/account-a-memory'));
            expect(expectedAuthenticatedUid, 'uid-a');
            requestAuthority = exactAuthority!;
            return response.future;
          },
        );
      },
    )..conversations = [original];
    addTearDown(provider.dispose);
    var notifications = 0;
    provider.addListener(() => notifications++);

    final deletion = provider.deleteConversationPermanently(original);
    await pumpEventQueue();
    expect(requestAuthority.uid, 'uid-a');
    expect(requestAuthority.isExactCurrent(), isTrue);

    authority.current = false;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    SharedPreferencesUtil().uid = 'uid-b';
    provider.conversations = [replacement];
    provider.searchedConversations = [replacement];
    SharedPreferencesUtil().cachedConversations = [replacement];
    final notificationsAfterTransition = notifications;

    response.complete(http.Response('', 204));
    expect(await deletion, isFalse);

    expect(requestAuthority.isExactCurrent(), isFalse);
    expect(provider.conversations, [replacement]);
    expect(provider.searchedConversations, [replacement]);
    expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), ['account-b-memory']);
    expect(notifications, notificationsAfterTransition);
  });

  test('primary memories finish loading while failed-summary request is still pending', () async {
    final failedRequest = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      conversationsFetchCall: () async => ConversationsFetchResult.success([conversation('recent')]),
      failedConversationsFetchCall: () => failedRequest.future,
    );
    addTearDown(() {
      if (!failedRequest.isCompleted) {
        failedRequest.complete(const ConversationsFetchResult.failure());
      }
      provider.dispose();
    });

    await provider.fetchConversations().timeout(const Duration(seconds: 1));

    expect(provider.hasLoadedConversations, isTrue);
    expect(provider.isLoadingConversations, isFalse);
    expect(provider.hasFreshConversations, isTrue);
    expect(provider.visibleConversations.map((item) => item.id), ['recent']);
  });

  test('failed-summary refresh updates separately after primary memories load', () async {
    final failedRequest = Completer<ConversationsFetchResult>();
    final startedAt = DateTime.parse('2026-07-08T19:00:00Z');
    final failedWithReason = ServerConversation(
      id: 'failed-summary',
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 10)),
      structured: Structured('Failed memory', 'Overview'),
      status: ConversationStatus.failed,
      processingError: 'conversation_summary_failed',
    );
    final provider = ConversationProvider(
      conversationsFetchCall: () async => ConversationsFetchResult.success([conversation('recent')]),
      failedConversationsFetchCall: () => failedRequest.future,
    );
    addTearDown(provider.dispose);

    await provider.fetchConversations();
    expect(provider.failedConversations, isEmpty);

    failedRequest.complete(ConversationsFetchResult.success([failedWithReason]));
    await pumpEventQueue();

    expect(provider.failedConversations.map((item) => item.id), ['failed-summary']);
    expect(provider.visibleConversations.map((item) => item.id), ['recent']);
  });

  test('primary memory timeout releases the loading state', () async {
    final primaryRequest = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      conversationsFetchCall: () => primaryRequest.future,
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
      conversationsFetchTimeout: const Duration(milliseconds: 10),
    );
    addTearDown(() {
      if (!primaryRequest.isCompleted) {
        primaryRequest.complete(const ConversationsFetchResult.failure());
      }
      provider.dispose();
    });

    await provider.fetchConversations();

    expect(provider.hasLoadedConversations, isTrue);
    expect(provider.isLoadingConversations, isFalse);
    expect(provider.hasFreshConversations, isFalse);
  });

  test('stale background refresh cannot clear a newer primary loading state', () async {
    final staleRequest = Completer<ConversationsFetchResult>();
    final currentRequest = Completer<ConversationsFetchResult>();
    var requestCount = 0;
    final provider = ConversationProvider(
      conversationsFetchCall: () {
        requestCount += 1;
        return requestCount == 1 ? staleRequest.future : currentRequest.future;
      },
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
    );
    addTearDown(() {
      if (!staleRequest.isCompleted) {
        staleRequest.complete(const ConversationsFetchResult.failure());
      }
      if (!currentRequest.isCompleted) {
        currentRequest.complete(const ConversationsFetchResult.failure());
      }
      provider.dispose();
    });

    final staleRefresh = provider.forceRefreshConversations();
    final currentRefresh = provider.fetchConversations();
    expect(provider.isLoadingConversations, isTrue);

    staleRequest.complete(ConversationsFetchResult.success([conversation('stale')]));
    await staleRefresh;

    expect(provider.isLoadingConversations, isTrue);
    expect(provider.visibleConversations, isEmpty);

    currentRequest.complete(ConversationsFetchResult.success([conversation('current')]));
    await currentRefresh;

    expect(provider.isLoadingConversations, isFalse);
    expect(provider.visibleConversations.map((item) => item.id), ['current']);
  });

  test('incremental refresh replaces a completed summary and preserves paginated history and cache', () async {
    SharedPreferencesUtil().uid = 'uid-a';
    final updated = ServerConversation(
      id: 'current',
      createdAt: DateTime.parse('2026-07-08T19:00:00Z'),
      structured: Structured('Canonical title', 'Canonical overview'),
      activeSummaryVersionId: 'version-2',
      enrichmentState: {'status': 'writeback_applied'},
    );
    final provider = ConversationProvider(
      conversationsFetchCall: () async => ConversationsFetchResult.success([updated]),
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
    )..conversations = [conversation('current'), conversation('older-page')];
    addTearDown(provider.dispose);
    await provider.forceRefreshConversations();
    expect(provider.conversations.map((item) => item.id), ['current', 'older-page']);
    expect(provider.conversations.first.activeSummaryVersionId, 'version-2');
    expect(provider.conversations.first.structured.title, 'Canonical title');
    expect(SharedPreferencesUtil().cachedConversations.first.activeSummaryVersionId, 'version-2');
  });

  for (final incremental in [true, false]) {
    final refreshName = incremental ? 'incremental' : 'primary';
    for (final deleteSucceeds in [true, false]) {
      test('deferred $refreshName list respects ${deleteSucceeds ? 'successful' : 'failed'} permanent deletion',
          () async {
        SharedPreferencesUtil().uid = 'uid-a';
        final authority = _MutableAuthority('uid-a');
        final deleted = conversation('deleted');
        final response = Completer<ConversationsFetchResult>();
        final provider = ConversationProvider(
          activeAuthority: () => authority,
          conversationsFetchCall: () => response.future,
          failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
          conversationDeleteCall: (_, __) async => deleteSucceeds,
        )..conversations = [deleted, conversation('retained')];
        addTearDown(provider.dispose);
        SharedPreferencesUtil().cachedConversations = provider.conversations;

        final refresh = incremental ? provider.forceRefreshConversations() : provider.fetchConversations();
        await pumpEventQueue();
        expect(await provider.deleteConversationPermanently(deleted), deleteSucceeds);
        response.complete(ConversationsFetchResult.success([deleted, conversation('unrelated')]));
        await refresh;

        final expected = deleteSucceeds ? ['unrelated'] : ['deleted', 'unrelated'];
        if (incremental) expected.insert(deleteSucceeds ? 0 : 1, 'retained');
        expect(provider.conversations.map((item) => item.id), unorderedEquals(expected));
        expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), unorderedEquals(expected));
        expect(provider.canProjectCaptureConversation('deleted'), !deleteSucceeds);
      });
    }

    test('$refreshName list from before account reset cannot restore deleted state or cache', () async {
      SharedPreferencesUtil().uid = 'uid-a';
      var authority = _MutableAuthority('uid-a');
      final deleted = conversation('same-id');
      final oldResponse = Completer<ConversationsFetchResult>();
      var requests = 0;
      final provider = ConversationProvider(
        activeAuthority: () => authority,
        conversationsFetchCall: () => ++requests == 1
            ? oldResponse.future
            : Future.value(ConversationsFetchResult.success([conversation('same-id')])),
        failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
        conversationDeleteCall: (_, __) async => true,
      )..conversations = [deleted];
      addTearDown(provider.dispose);

      final refresh = incremental ? provider.forceRefreshConversations() : provider.fetchConversations();
      await pumpEventQueue();
      expect(await provider.deleteConversationPermanently(deleted), isTrue);
      authority.current = false;
      provider.reset();
      SharedPreferencesUtil().uid = 'uid-b';
      authority = _MutableAuthority('uid-b');
      provider.conversations = [conversation('replacement')];
      SharedPreferencesUtil().cachedConversations = provider.conversations;
      oldResponse.complete(ConversationsFetchResult.success([deleted, conversation('old-account')]));
      await refresh;
      expect(provider.conversations.map((item) => item.id), ['replacement']);
      expect(SharedPreferencesUtil().cachedConversations.map((item) => item.id), ['replacement']);

      await provider.fetchConversations();
      expect(provider.conversations.map((item) => item.id), ['same-id']);
      expect(provider.canProjectCaptureConversation('same-id'), isTrue);
    });
  }

  test('later page excludes confirmed deleted records without changing raw server offsets', () async {
    final authority = _MutableAuthority('uid-a');
    final deleted = conversation('deleted');
    final offsets = <int>[];
    final stalePage = [deleted, ...List.generate(49, (index) => conversation('page-$index'))];
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (_, __) async => true,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        offsets.add(offset);
        return ConversationsFetchResult.success(offsets.length == 1 ? stalePage : [conversation('last')]);
      },
    )..conversations = [deleted, conversation('retained')];
    addTearDown(provider.dispose);

    expect(await provider.deleteConversationPermanently(deleted), isTrue);
    await provider.getMoreConversationsFromServer();
    expect(provider.conversations.map((item) => item.id), isNot(contains('deleted')));
    expect(provider.hasMoreConversations, isTrue);
    await provider.getMoreConversationsFromServer();
    expect(offsets, [1, 51]);
  });

  test('deferred failed-summary list cannot restore a confirmed deleted failure', () async {
    final authority = _MutableAuthority('uid-a');
    final deleted = ServerConversation(
      id: 'deleted-failure',
      createdAt: DateTime.parse('2026-07-08T19:00:00Z'),
      structured: Structured('Failed memory', ''),
      status: ConversationStatus.failed,
      processingError: 'conversation_summary_failed',
    );
    final response = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsFetchCall: () async => const ConversationsFetchResult.success([]),
      failedConversationsFetchCall: () => response.future,
      conversationDeleteCall: (_, __) async => true,
    )..failedConversations = [deleted];
    addTearDown(provider.dispose);

    await provider.forceRefreshConversations();
    expect(await provider.deleteConversationPermanently(deleted), isTrue);
    response.complete(ConversationsFetchResult.success([deleted]));
    await pumpEventQueue();
    expect(provider.failedConversations, isEmpty);
  });

  test('primary failure excludes confirmed deletion from a retained cache snapshot', () async {
    SharedPreferencesUtil().uid = 'uid-a';
    final authority = _MutableAuthority('uid-a');
    final deleted = conversation('deleted');
    final response = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsFetchCall: () => response.future,
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
      conversationDeleteCall: (_, __) async => true,
    )..conversations = [deleted];
    addTearDown(provider.dispose);

    final refresh = provider.fetchConversations();
    expect(await provider.deleteConversationPermanently(deleted), isTrue);
    SharedPreferencesUtil().cachedConversations = [deleted, conversation('retained')];
    response.complete(const ConversationsFetchResult.failure());
    await refresh;
    expect(provider.conversations.map((item) => item.id), ['retained']);
    expect(provider.searchedConversations.map((item) => item.id), ['retained']);
  });

  test('memory pagination deduplicates shifted pages and records the terminal page', () async {
    final authority = _MutableAuthority('uid-a');
    final initial = List.generate(50, (index) => conversation('memory-$index'));
    final refreshedDuplicate = ServerConversation(
      id: 'memory-0',
      createdAt: DateTime.parse('2026-07-08T19:00:00Z'),
      structured: Structured('Updated memory', 'Updated overview'),
    );
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        expect(limit, 50);
        expect(offset, 50);
        return ConversationsFetchResult.success([refreshedDuplicate, conversation('memory-older')]);
      },
    )
      ..conversations = initial
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await provider.getMoreConversationsFromServer();

    expect(provider.conversations, hasLength(51));
    expect(provider.conversations.where((item) => item.id == 'memory-0'), hasLength(1));
    expect(provider.conversations.firstWhere((item) => item.id == 'memory-0').structured.title, 'Updated memory');
    expect(provider.conversations.map((item) => item.id), contains('memory-older'));
    expect(provider.hasMoreConversations, isFalse);
    expect(provider.isLoadingMoreConversations, isFalse);
    expect(provider.loadMoreConversationsFailed, isFalse);
  });

  test('failed memory page is non-destructive and can be retried', () async {
    final authority = _MutableAuthority('uid-a');
    var requests = 0;
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        requests += 1;
        return requests == 1
            ? const ConversationsFetchResult.failure(statusCode: 503)
            : ConversationsFetchResult.success([conversation('memory-older')]);
      },
    )
      ..conversations = [conversation('memory-current')]
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await provider.getMoreConversationsFromServer();
    expect(provider.conversations.map((item) => item.id), ['memory-current']);
    expect(provider.loadMoreConversationsFailed, isTrue);
    expect(provider.hasMoreConversations, isTrue);

    await provider.getMoreConversationsFromServer();
    expect(provider.conversations.map((item) => item.id), containsAll(['memory-current', 'memory-older']));
    expect(provider.loadMoreConversationsFailed, isFalse);
    expect(provider.hasMoreConversations, isFalse);
  });

  test('memory pagination advances the server offset when a full page contains only duplicates', () async {
    final authority = _MutableAuthority('uid-a');
    final initial = List.generate(50, (index) => conversation('memory-$index'));
    final requestedOffsets = <int>[];
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        requestedOffsets.add(offset);
        if (offset == 50) return ConversationsFetchResult.success(initial);
        return ConversationsFetchResult.success([conversation('memory-older')]);
      },
    )
      ..conversations = initial
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await provider.getMoreConversationsFromServer();
    await provider.getMoreConversationsFromServer();

    expect(requestedOffsets, [50, 100]);
    expect(provider.conversations, hasLength(51));
    expect(provider.conversations.map((item) => item.id), contains('memory-older'));
    expect(provider.hasMoreConversations, isFalse);
  });

  test('confirmed deletion contracts the consumed offset before requesting the next page', () async {
    final authority = _MutableAuthority('uid-a');
    final initial = List.generate(50, (index) => conversation('memory-$index'));
    final requestedOffsets = <int>[];
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsFetchCall: () async => ConversationsFetchResult.success(initial),
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
      conversationDeleteCall: (_, __) async => true,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        requestedOffsets.add(offset);
        return ConversationsFetchResult.success([conversation('memory-older')]);
      },
    );
    addTearDown(provider.dispose);

    await provider.fetchConversations();
    expect(provider.conversations, hasLength(50));
    expect(await provider.deleteConversationPermanently(provider.conversations.first), isTrue);
    await provider.getMoreConversationsFromServer();

    expect(requestedOffsets, [49]);
    expect(provider.conversations.map((item) => item.id), contains('memory-older'));
  });

  test('confirmed deletion invalidates an in-flight page that used the pre-delete offset', () async {
    final authority = _MutableAuthority('uid-a');
    final initial = List.generate(50, (index) => conversation('memory-$index'));
    final stalePage = Completer<ConversationsFetchResult>();
    final requestedOffsets = <int>[];
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsFetchCall: () async => ConversationsFetchResult.success(initial),
      failedConversationsFetchCall: () async => const ConversationsFetchResult.success([]),
      conversationDeleteCall: (_, __) async => true,
      conversationsPageFetchCall: ({required limit, required offset}) {
        requestedOffsets.add(offset);
        if (requestedOffsets.length == 1) return stalePage.future;
        return Future.value(ConversationsFetchResult.success([conversation('memory-after-delete')]));
      },
    );
    addTearDown(provider.dispose);

    await provider.fetchConversations();
    final oldPageRequest = provider.getMoreConversationsFromServer();
    await pumpEventQueue();
    expect(requestedOffsets, [50]);

    expect(await provider.deleteConversationPermanently(provider.conversations.first), isTrue);
    stalePage.complete(ConversationsFetchResult.success([conversation('memory-stale-offset')]));
    await oldPageRequest;
    expect(provider.conversations.map((item) => item.id), isNot(contains('memory-stale-offset')));

    await provider.getMoreConversationsFromServer();
    expect(requestedOffsets, [50, 49]);
    expect(provider.conversations.map((item) => item.id), contains('memory-after-delete'));
  });

  test('account transition discards a delayed memory page and clears loading state', () async {
    final authority = _MutableAuthority('uid-a');
    final response = Completer<ConversationsFetchResult>();
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) => response.future,
    )
      ..conversations = [conversation('account-a-memory')]
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    final request = provider.getMoreConversationsFromServer();
    await pumpEventQueue();
    expect(provider.isLoadingMoreConversations, isTrue);

    authority.current = false;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    response.complete(ConversationsFetchResult.success([conversation('stale-account-a-page')]));
    await request;

    expect(provider.conversations.map((item) => item.id), ['account-a-memory']);
    expect(provider.isLoadingMoreConversations, isFalse);
    expect(provider.loadMoreConversationsFailed, isFalse);
  });
}
