import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets/memory_talk_sheet.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({'demoMode': true});
    await SharedPreferencesUtil.init();
  });

  ServerConversation memory() => ServerConversation(
        id: 'garden',
        createdAt: DateTime(2026, 7, 23, 9, 40),
        structured: Structured(
          'Coffee in the garden with Margaret',
          'You had coffee in the garden with Margaret this morning.',
        ),
      );

  Widget app() => MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: MemoryTalkSheet(conversation: memory())),
      );

  Future<void> send(WidgetTester tester, String text) async {
    final field = find.byType(TextField);
    await tester.enterText(field, text);
    await tester.ensureVisible(find.bySemanticsLabel('Send'));
    await tester.pump();
    await tester.tap(find.bySemanticsLabel('Send'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 320));
  }

  Future<void> useKeyboard(WidgetTester tester) async {
    await tester.tap(find.byTooltip('Use keyboard'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
  }

  testWidgets('opens voice-first, speaks within 1.5 seconds, and keeps typing behind the in-sheet toggle',
      (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(app());
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.byType(EllaVoiceOrb), findsOneWidget);
    expect(find.text('Listening...'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
    expect(find.byTooltip('Use keyboard'), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 500));
    expect(find.text('Ella is speaking...'), findsOneWidget);
    expect(find.textContaining('What would you like to tell me about it?'), findsOneWidget);

    await useKeyboard(tester);
    expect(find.byType(TextField), findsOneWidget);
    expect(find.byTooltip('Use voice'), findsOneWidget);
  });

  testWidgets('ambiguous confirmation re-prompts instead of dropping the pending change', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(app());
    await tester.pump(const Duration(milliseconds: 100));
    await useKeyboard(tester);
    await send(tester, "Actually, it wasn't Margaret — it was Rose who came by.");
    expect(find.text('So it was Rose at the garden, not Margaret — did I get that right?'), findsOneWidget);

    await send(tester, 'Maybe');
    expect(find.text('Sorry — was that a yes or a no?'), findsOneWidget);
  });

  testWidgets('denial explicitly discards the pending change', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(app());
    await tester.pump(const Duration(milliseconds: 100));
    await useKeyboard(tester);
    await send(tester, "Actually, it wasn't Margaret — it was Rose who came by.");
    await send(tester, "No, I don't think so");

    expect(find.text("All right — I won't change it."), findsOneWidget);
    expect(memory().structured.title, contains('Margaret'));
  });
}
