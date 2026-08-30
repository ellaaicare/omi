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
import 'package:omi/ella/services/memory_artwork_api.dart';
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

class _FakeArtworkApi extends MemoryArtworkApi {
  _FakeArtworkApi({this.releaseEnabled = true})
      : super(baseUrl: 'https://api.example.test', authorityProvider: () => null);

  final bool releaseEnabled;
  String selectedStyle = memoryArtworkDefaultStyle;
  int backfillCalls = 0;

  @override
  Future<MemoryArtworkPreferences?> preferences() async => MemoryArtworkPreferences(
        consent: 'accepted',
        consentVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
        styleVersion: selectedStyle,
        releaseEnabled: releaseEnabled,
      );

  @override
  Future<MemoryArtworkPreferenceUpdate> setStyle({required String consentVersion, required String styleVersion}) async {
    expect(consentVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
    selectedStyle = styleVersion;
    return const MemoryArtworkPreferenceUpdate(saved: true);
  }

  @override
  Future<MemoryArtworkBackfillPage?> backfillNext({String? cursor}) async {
    backfillCalls += 1;
    return const MemoryArtworkBackfillPage(queued: 1, existing: 0, skipped: 0, hasMore: false);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'test-user', 'aiConsentProfileBindingId': 'profile-test-user'});
    await SharedPreferencesUtil.init();
  });

  ServerConversation memory(
    String id, {
    String title = 'A test memory',
    String overview = 'A short transcript created this memory.',
    DateTime? at,
  }) {
    final startedAt = at ?? DateTime.parse('2026-08-10T18:00:00Z');
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 3)),
      structured: Structured(title, overview),
    );
  }

  Future<void> pumpPage(WidgetTester tester, ConversationProvider provider, {MemoryArtworkApi? artworkApi}) async {
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
          home: EllaMemoriesPage(artworkApi: artworkApi),
        ),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('gallery swipe-left confirms and permanently deletes the selected memory', (tester) async {
    final requestedIds = <String>[];
    final authority = _MutableAuthority('test-user');
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationDeleteCall: (id, _) async {
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

    expect(find.byKey(const Key('memory-card-memory-1')), findsOneWidget);
    expect(find.byKey(const Key('memory-layout-menu')), findsOneWidget);
    expect(find.byKey(const Key('memory-sort-menu')), findsOneWidget);
    expect(requestedIds, isEmpty);

    await tester.fling(find.byKey(const Key('memory-card-memory-1')), const Offset(-420, 0), 1600);
    await tester.pumpAndSettle();
    expect(find.text('Delete Conversation?'), findsOneWidget);
    await tester.tap(find.text('Delete'));
    await tester.pumpAndSettle();

    expect(requestedIds, ['memory-1']);
    expect(find.byKey(const Key('memory-card-memory-1')), findsNothing);
  });

  testWidgets('gallery swipe-right opens the read and edit surface without deleting', (tester) async {
    var opens = 0;
    var deletes = 0;
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: MemoryGalleryCard(
            conversation: memory('memory-gesture'),
            layout: MemoryGalleryLayout.list,
            onOpen: () => opens += 1,
            onDelete: () async {
              deletes += 1;
              return false;
            },
          ),
        ),
      ),
    );

    await tester.fling(find.byKey(const Key('memory-card-memory-gesture')), const Offset(420, 0), 1600);
    await tester.pumpAndSettle();

    expect(opens, 1);
    expect(deletes, 0);
    expect(find.byKey(const Key('memory-card-memory-gesture')), findsOneWidget);
  });

  testWidgets('gallery replaces Ella enrichment prefixes with the source indicator', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: MemoryGalleryCard(
            conversation: memory(
              'memory-enriched',
              title: '[Ella] Family dinner',
              overview: '[Ella] A grounded summary of the evening.',
            ),
            layout: MemoryGalleryLayout.list,
            onOpen: () {},
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.textContaining('[Ella]'), findsNothing);
    expect(find.byIcon(Icons.auto_awesome_rounded), findsNWidgets(2));
    expect(find.textContaining('Family dinner'), findsOneWidget);
    expect(find.textContaining('A grounded summary'), findsOneWidget);
  });

  testWidgets('shortened Home title preserves Ella enrichment provenance', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: MemoryGalleryCard(
            conversation: memory(
              'memory-enriched-home',
              title: '[Ella] Family dinner and plans',
              overview: 'A grounded summary of the evening.',
            ),
            displayTitle: 'Family dinner',
            layout: MemoryGalleryLayout.list,
            onOpen: () {},
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.textContaining('[Ella]'), findsNothing);
    expect(find.byIcon(Icons.auto_awesome_rounded), findsOneWidget);
    expect(find.textContaining('Family dinner'), findsOneWidget);
  });

  testWidgets('gallery swipe affordances follow start and end in right-to-left layouts', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Directionality(
          textDirection: TextDirection.rtl,
          child: Scaffold(
            body: MemoryGalleryCard(
              conversation: memory('memory-rtl'),
              layout: MemoryGalleryLayout.list,
              onOpen: () {},
              onDelete: () async => false,
            ),
          ),
        ),
      ),
    );

    final card = find.byKey(const Key('memory-card-memory-rtl'));
    final dismissible = tester.widget<Dismissible>(card);
    final openBackground = dismissible.background!;
    final deleteBackground = dismissible.secondaryBackground!;

    await tester.pumpWidget(
      Directionality(
        textDirection: TextDirection.rtl,
        child: SizedBox(width: 400, height: 120, child: openBackground),
      ),
    );
    expect(tester.getCenter(find.byIcon(Icons.edit_outlined)).dx, greaterThan(200));

    await tester.pumpWidget(
      Directionality(
        textDirection: TextDirection.rtl,
        child: SizedBox(width: 400, height: 120, child: deleteBackground),
      ),
    );
    expect(tester.getCenter(find.byIcon(Icons.delete_outline_rounded)).dx, lessThan(200));
  });

  testWidgets('gallery sort switches between newest and oldest memories', (tester) async {
    final provider = ConversationProvider()
      ..conversations = [
        memory('newest', title: 'Newest memory', at: DateTime.parse('2026-08-10T18:00:00Z')),
        memory('oldest', title: 'Oldest memory', at: DateTime.parse('2026-08-10T08:00:00Z')),
      ]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    expect(
      tester.getTopLeft(find.byKey(const Key('memory-card-newest'))).dy,
      lessThan(tester.getTopLeft(find.byKey(const Key('memory-card-oldest'))).dy),
    );

    await tester.tap(find.byKey(const Key('memory-sort-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Oldest first'));
    await tester.pumpAndSettle();

    expect(
      tester.getTopLeft(find.byKey(const Key('memory-card-oldest'))).dy,
      lessThan(tester.getTopLeft(find.byKey(const Key('memory-card-newest'))).dy),
    );
  });

  testWidgets('oldest-first sorting stays disabled until the full archive is loaded', (tester) async {
    final provider = ConversationProvider(
      conversationsPageFetchCall: ({required limit, required offset}) async => const ConversationsFetchResult.failure(),
    )
      ..conversations = [memory('recent-page')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.tap(find.byKey(const Key('memory-sort-menu')));
    await tester.pumpAndSettle();

    final oldestItem = tester.widget<PopupMenuItem<MemoryGallerySort>>(
      find.ancestor(of: find.text('Oldest first'), matching: find.byType(PopupMenuItem<MemoryGallerySort>)),
    );
    expect(oldestItem.enabled, isFalse);
  });

  testWidgets('layout persists per account profile and illustration style remains editable', (tester) async {
    final provider = ConversationProvider()
      ..conversations = [memory('memory-layout')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    final artworkApi = _FakeArtworkApi();
    addTearDown(provider.dispose);

    await pumpPage(tester, provider, artworkApi: artworkApi);
    expect(find.byKey(const Key('memory-layout-journal-memory-layout')), findsOneWidget);

    await tester.tap(find.byKey(const Key('memory-layout-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Compact list'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('memory-layout-list-memory-layout')), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    await pumpPage(tester, provider, artworkApi: artworkApi);
    expect(find.byKey(const Key('memory-layout-list-memory-layout')), findsOneWidget);

    final preferences = SharedPreferencesUtil()..uid = 'other-user';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-other-user');
    await tester.pumpWidget(const SizedBox.shrink());
    await pumpPage(tester, provider, artworkApi: artworkApi);
    expect(find.byKey(const Key('memory-layout-journal-memory-layout')), findsOneWidget);

    final backfillCallsBeforeStyleChange = artworkApi.backfillCalls;
    await tester.tap(find.byKey(const Key('memory-artwork-style-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Paper collage'));
    await tester.pumpAndSettle();
    expect(artworkApi.selectedStyle, memoryArtworkPaperCollageStyle);
    expect(artworkApi.backfillCalls, backfillCallsBeforeStyleChange + 1);
    expect(find.textContaining('Illustration style saved'), findsOneWidget);
  });

  testWidgets('days layout groups a local day into one comic-style tile', (tester) async {
    final provider = ConversationProvider()
      ..conversations = [
        memory('morning', title: 'Morning walk', at: DateTime.parse('2026-08-10T08:00:00Z')),
        memory('evening', title: 'Evening dinner', at: DateTime.parse('2026-08-10T18:00:00Z')),
      ]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    final artworkApi = _FakeArtworkApi();
    addTearDown(provider.dispose);

    await pumpPage(tester, provider, artworkApi: artworkApi);
    await tester.tap(find.byKey(const Key('memory-layout-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Days'));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('memory-day-evening')), findsOneWidget);
    expect(find.text('2 memories'), findsOneWidget);
    expect(find.textContaining('Morning walk'), findsOneWidget);
    expect(find.byKey(const Key('memory-card-morning')), findsNothing);

    await tester.tap(find.byKey(const Key('memory-day-evening')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('memory-day-list')), findsOneWidget);
    expect(find.byKey(const Key('memory-card-morning')), findsOneWidget);
    expect(find.byKey(const Key('memory-card-evening')), findsOneWidget);
  });

  testWidgets('Painter style control is disabled while real generation is unavailable', (tester) async {
    final provider = ConversationProvider()
      ..conversations = [memory('memory-style-disabled')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    final artworkApi = _FakeArtworkApi(releaseEnabled: false);
    addTearDown(provider.dispose);

    await pumpPage(tester, provider, artworkApi: artworkApi);

    final menu = tester.widget<PopupMenuButton<String>>(find.byKey(const Key('memory-artwork-style-menu')));
    expect(menu.enabled, isFalse);
    expect(artworkApi.backfillCalls, 0);
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
    final scrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('ella-memories-list')), matching: find.byType(Scrollable)),
    );
    scrollable.position.jumpTo(scrollable.position.maxScrollExtent);
    await tester.pump();
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

  testWidgets('a failed automatic page waits for the visible retry action', (tester) async {
    final authority = _MutableAuthority('test-user');
    var pageRequests = 0;
    final provider = ConversationProvider(
      activeAuthority: () => authority,
      conversationsPageFetchCall: ({required limit, required offset}) async {
        pageRequests += 1;
        return pageRequests == 1
            ? const ConversationsFetchResult.failure(statusCode: 503)
            : ConversationsFetchResult.success([memory('memory-older')]);
      },
    )
      ..conversations = [memory('memory-current')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = true;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.pump(const Duration(seconds: 1));

    expect(pageRequests, 1);
    expect(find.byKey(const Key('memories-load-more-failed')), findsOneWidget);

    await tester.ensureVisible(find.byKey(const Key('retry-load-more-memories')));
    await tester.drag(find.byKey(const Key('ella-memories-list')), const Offset(0, -120));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('retry-load-more-memories')));
    await tester.pumpAndSettle();

    expect(pageRequests, 2);
    expect(provider.conversations.map((item) => item.id), contains('memory-older'));
  });

  testWidgets('Back to recent appears after scrolling and returns to the newest memories', (tester) async {
    final provider = ConversationProvider()
      ..conversations = List.generate(
        36,
        (index) => memory(
          'memory-$index',
          title: 'Memory $index',
          at: DateTime.parse('2026-08-10T18:00:00Z').subtract(Duration(days: index)),
        ),
      )
      ..hasLoadedConversations = true
      ..hasFreshConversations = true
      ..hasMoreConversations = false;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.tap(find.byKey(const Key('memory-sort-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Oldest first'));
    await tester.pumpAndSettle();
    expect(find.text('Memory 35'), findsOneWidget);

    await tester.fling(find.byKey(const Key('ella-memories-list')), const Offset(0, -3200), 4000);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('back-to-recent-memories')), findsOneWidget);

    await tester.tap(find.byKey(const Key('back-to-recent-memories')));
    await tester.pumpAndSettle();

    final scrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('ella-memories-list')), matching: find.byType(Scrollable)).first,
    );
    expect(scrollable.position.pixels, closeTo(0, 0.5));
    expect(find.text('Memory 0'), findsOneWidget);
    expect(find.byKey(const Key('back-to-recent-memories')), findsNothing);
  });
}
