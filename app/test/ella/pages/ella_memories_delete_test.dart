import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/services.dart';
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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'test-user'});
    await SharedPreferencesUtil.init();
  });

  ServerConversation memory(String id, {String title = 'A test memory'}) {
    final startedAt = DateTime.parse('2026-08-10T18:00:00Z');
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 3)),
      structured: Structured(title, 'A short transcript created this memory.'),
    );
  }

  Future<void> pumpPage(WidgetTester tester, ConversationProvider provider) async {
    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider<ConversationProvider>.value(value: provider),
          ChangeNotifierProvider<CaptureProvider>(create: (_) => CaptureProvider()),
        ],
        child: MaterialApp(
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: const EllaMemoriesPage(),
        ),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('memory row exposes a 48-point delete action and confirms permanent deletion', (tester) async {
    final requestedIds = <String>[];
    final authority = _MutableAuthority('test-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (id, exactAuthority) async {
        expect(exactAuthority.uid, 'test-user');
        requestedIds.add(id);
        return true;
      },
    )
      ..conversations = [memory('memory-1')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);

    final deleteAction = find.byKey(const Key('delete-memory-memory-1'));
    expect(deleteAction, findsOneWidget);
    expect(tester.getSize(deleteAction).width, greaterThanOrEqualTo(48));
    expect(tester.getSize(deleteAction).height, greaterThanOrEqualTo(48));

    await tester.tap(deleteAction);
    await tester.pumpAndSettle();
    expect(find.text('Delete Memory'), findsOneWidget);
    expect(find.textContaining('cannot be undone'), findsOneWidget);
    expect(requestedIds, isEmpty);

    await tester.tap(find.byKey(const Key('confirm-delete-memory')));
    await tester.pumpAndSettle();

    expect(requestedIds, ['memory-1']);
    expect(find.text('A test memory'), findsNothing);
    expect(find.text('Memory Deleted.'), findsOneWidget);
  });

  testWidgets('cancelling memory deletion leaves the memory untouched', (tester) async {
    final requestedIds = <String>[];
    final authority = _MutableAuthority('test-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (id, _) async {
        requestedIds.add(id);
        return true;
      },
    )
      ..conversations = [memory('memory-2')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.tap(find.byKey(const Key('delete-memory-memory-2')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    expect(requestedIds, isEmpty);
    expect(find.text('A test memory'), findsOneWidget);
  });

  testWidgets('account transition rejects delayed success and never shows deletion success', (tester) async {
    final authority = _MutableAuthority('test-user');
    final response = Completer<bool>();
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (_, __) => response.future,
    )
      ..conversations = [memory('memory-delayed')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.tap(find.byKey(const Key('delete-memory-memory-delayed')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('confirm-delete-memory')));
    await tester.pump();
    expect(find.byKey(const Key('deleting-memory-progress')), findsOneWidget);

    authority.current = false;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    response.complete(true);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));

    expect(find.text('Memory Deleted.'), findsNothing);
    expect(find.text('An error occurred. Please try again.'), findsOneWidget);
  });

  testWidgets('scrolling near the end requests the next memory page', (tester) async {
    final authority = _MutableAuthority('test-user');
    var pageRequests = 0;
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        pageRequests += 1;
        expect(limit, 50);
        expect(offset, 50);
        return ConversationsFetchResult.success([memory('memory-older', title: 'An older memory')]);
      },
    )
      ..conversations = List.generate(50, (index) => memory('memory-$index', title: 'Memory $index'))
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.fling(find.byKey(const Key('ella-memories-list')), const Offset(0, -6000), 5000);
    await tester.pumpAndSettle();

    expect(pageRequests, 1);
    expect(provider.conversations.map((item) => item.id), contains('memory-older'));
    expect(provider.hasMoreConversations, isFalse);
  });

  testWidgets('a short first page automatically requests older memories without a scroll event', (tester) async {
    final authority = _MutableAuthority('test-user');
    var pageRequests = 0;
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        pageRequests += 1;
        expect(offset, 1);
        return ConversationsFetchResult.success([memory('memory-older', title: 'An older memory')]);
      },
    )
      ..conversations = [memory('memory-current')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);

    expect(pageRequests, 1);
    expect(provider.conversations.map((item) => item.id), containsAll(['memory-current', 'memory-older']));
    expect(provider.hasMoreConversations, isFalse);
  });

  testWidgets('Back to recent appears after scrolling and returns to the newest memories', (tester) async {
    final provider = ConversationProvider()
      ..conversations = List.generate(36, (index) => memory('memory-$index', title: 'Memory $index'))
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.fling(find.byKey(const Key('ella-memories-list')), const Offset(0, -3200), 4000);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('back-to-recent-memories')), findsOneWidget);

    await tester.tap(find.byKey(const Key('back-to-recent-memories')));
    await tester.pumpAndSettle();

    final scrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('ella-memories-list')), matching: find.byType(Scrollable)).first,
    );
    expect(scrollable.position.pixels, closeTo(0, 0.5));
    expect(find.byKey(const Key('back-to-recent-memories')), findsNothing);
  });
}
