import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/services.dart';

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

  ServerConversation memory(String id) {
    final startedAt = DateTime.parse('2026-08-10T18:00:00Z');
    return ServerConversation(
      id: id,
      createdAt: startedAt,
      startedAt: startedAt,
      finishedAt: startedAt.add(const Duration(minutes: 3)),
      structured: Structured('A test memory', 'A short transcript created this memory.'),
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
    final provider = ConversationProvider(
      conversationDeleteCall: (id) async {
        requestedIds.add(id);
        return true;
      },
    )
      ..conversations = [memory('memory-1')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true;
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
    final provider = ConversationProvider(
      conversationDeleteCall: (id) async {
        requestedIds.add(id);
        return true;
      },
    )
      ..conversations = [memory('memory-2')]
      ..hasLoadedConversations = true
      ..hasFreshConversations = true;
    addTearDown(provider.dispose);

    await pumpPage(tester, provider);
    await tester.tap(find.byKey(const Key('delete-memory-memory-2')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    expect(requestedIds, isEmpty);
    expect(find.text('A test memory'), findsOneWidget);
  });
}
