import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
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

  Widget app({
    MemoryTalkCorrectionSubmitter? correctionSubmitter,
    MemoryTalkCorrectionReceiptLoader? correctionReceiptLoader,
  }) =>
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: MemoryTalkSheet(
            conversation: memory(),
            correctionSubmitter: correctionSubmitter,
            correctionReceiptLoader: correctionReceiptLoader,
          ),
        ),
      );

  Widget launcherApp({
    required MemoryTalkAmbientCapturePauser pauseAmbientCapture,
    required MemoryTalkAmbientCaptureResumer resumeAmbientCapture,
    MemoryTalkCorrectionSubmitter? correctionSubmitter,
    MemoryTalkCorrectionReceiptLoader? correctionReceiptLoader,
  }) =>
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: FilledButton(
              onPressed: () async {
                await showMemoryTalkSheet(
                  context,
                  conversation: memory(),
                  pauseAmbientCapture: pauseAmbientCapture,
                  resumeAmbientCapture: resumeAmbientCapture,
                  correctionSubmitter: correctionSubmitter,
                  correctionReceiptLoader: correctionReceiptLoader,
                );
              },
              child: const Text('Open memory talk'),
            ),
          ),
        ),
      );

  Future<void> send(WidgetTester tester, String text) async {
    final field = find.byType(TextField);
    await tester.enterText(field, text);
    await tester.testTextInput.receiveAction(TextInputAction.send);
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

  testWidgets('does not submit a correction until the user explicitly confirms it', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    var submissionCount = 0;
    await tester.pumpWidget(
      app(
        correctionSubmitter: ({
          required conversationId,
          required correctionText,
          summaryTitle,
          summaryOverview,
          appSummary,
        }) async {
          submissionCount += 1;
          return const ConversationCorrectionSubmission(
            correctionId: 'correction-1',
            conversationId: 'garden',
            status: 'queued',
            queued: true,
          );
        },
        correctionReceiptLoader: ({
          required conversationId,
          required correctionId,
        }) async =>
            ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: 'applied',
          appliedAt: DateTime(2026, 7, 23, 9, 45),
          undoneAt: null,
          beforeTitle: 'Coffee in the garden with Margaret',
          beforeOverview: 'You had coffee in the garden with Margaret this morning.',
          afterTitle: 'Coffee in the garden with Rose',
          afterOverview: 'You had coffee in the garden with Rose this morning.',
          propagationAppliedCount: 1,
          propagationRevertedCount: 0,
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    await useKeyboard(tester);

    await send(tester, "Actually, it wasn't Margaret — it was Rose who came by.");
    expect(submissionCount, 0);

    await send(tester, 'Yes');
    await tester.pump(const Duration(milliseconds: 500));
    expect(submissionCount, 1);
  });

  testWidgets('shows failure copy when the correction receipt is a terminal failure', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      app(
        correctionSubmitter: ({
          required conversationId,
          required correctionText,
          summaryTitle,
          summaryOverview,
          appSummary,
        }) async =>
            const ConversationCorrectionSubmission(
          correctionId: 'correction-1',
          conversationId: 'garden',
          status: 'queued',
          queued: true,
        ),
        correctionReceiptLoader: ({
          required conversationId,
          required correctionId,
        }) async =>
            ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: 'direct_apply_failed',
          appliedAt: null,
          undoneAt: null,
          beforeTitle: 'Coffee in the garden with Margaret',
          beforeOverview: 'You had coffee in the garden with Margaret this morning.',
          afterTitle: 'Coffee in the garden with Margaret',
          afterOverview: 'You had coffee in the garden with Margaret this morning.',
          propagationAppliedCount: 0,
          propagationRevertedCount: 0,
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    await useKeyboard(tester);

    await send(tester, "Actually, it wasn't Margaret — it was Rose who came by.");
    await send(tester, 'Yes');
    await tester.pump(const Duration(seconds: 3));

    // Terminal failure must use failure copy, never the "still working" (timeout) message.
    expect(find.text("I couldn't make that change yet. Please try again in a little while."), findsOneWidget);
    expect(find.text("I'm still working on that. You can close this and come back soon."), findsNothing);
  });

  testWidgets('resumes ambient capture after the sheet is closed without a correction', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    var pauseCount = 0;
    var resumeCount = 0;
    await tester.pumpWidget(
      launcherApp(
        pauseAmbientCapture: () async {
          pauseCount += 1;
          return true;
        },
        resumeAmbientCapture: () async {
          resumeCount += 1;
        },
      ),
    );

    await tester.tap(find.text('Open memory talk'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    expect(pauseCount, 1);
    expect(resumeCount, 0);

    await tester.tap(find.text('Done'));
    await tester.pumpAndSettle();
    expect(resumeCount, 1);
  });

  testWidgets('resumes ambient capture after an applied correction receipt closes the sheet', (tester) async {
    tester.view.physicalSize = const Size(402, 874);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    var resumeCount = 0;
    await tester.pumpWidget(
      launcherApp(
        pauseAmbientCapture: () async => true,
        resumeAmbientCapture: () async {
          resumeCount += 1;
        },
        correctionSubmitter: ({
          required conversationId,
          required correctionText,
          summaryTitle,
          summaryOverview,
          appSummary,
        }) async =>
            const ConversationCorrectionSubmission(
          correctionId: 'correction-1',
          conversationId: 'garden',
          status: 'queued',
          queued: true,
        ),
        correctionReceiptLoader: ({
          required conversationId,
          required correctionId,
        }) async =>
            ConversationCorrectionReceipt(
          correctionId: correctionId,
          conversationId: conversationId,
          status: 'applied',
          appliedAt: DateTime(2026, 7, 23, 9, 45),
          undoneAt: null,
          beforeTitle: 'Coffee in the garden with Margaret',
          beforeOverview: 'You had coffee in the garden with Margaret this morning.',
          afterTitle: 'Coffee in the garden with Rose',
          afterOverview: 'You had coffee in the garden with Rose this morning.',
          propagationAppliedCount: 1,
          propagationRevertedCount: 0,
        ),
      ),
    );

    await tester.tap(find.text('Open memory talk'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pump(const Duration(milliseconds: 100));
    await useKeyboard(tester);
    await send(tester, "Actually, it wasn't Margaret — it was Rose who came by.");
    await send(tester, 'Yes');
    await tester.pumpAndSettle();

    expect(find.byType(MemoryTalkSheet), findsNothing);
    expect(resumeCount, 1);
  });
}
