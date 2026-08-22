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
