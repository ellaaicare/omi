import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/conversation_provider.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

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
    final provider = ConversationProvider(
      conversationDeleteCall: (id) async {
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
    final provider = ConversationProvider(conversationDeleteCall: (_) async => true)
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
    final provider = ConversationProvider(conversationDeleteCall: (_) async => false);
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
    final provider = ConversationProvider(
      conversationDeleteCall: (_) async => throw StateError('network unavailable'),
    );
    addTearDown(provider.dispose);
    provider.conversations = [memory];
    provider.searchedConversations = [memory];

    final result = await provider.deleteConversationPermanently(memory);

    expect(result, isFalse);
    expect(provider.conversations, [memory]);
    expect(provider.searchedConversations, [memory]);
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
}
