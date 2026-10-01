import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';
import 'package:omi/ella/widgets/memory_correction_receipt.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

const conversationId = 'conversation-1';
const sessionId = 'session-1';
const correctionId = 'correction-1';

ServerConversation memoryConversation({String id = conversationId, String title = 'A seeded memory'}) =>
    ServerConversation(
      id: id,
      createdAt: DateTime.utc(2026, 7, 24),
      structured: Structured(title, 'The selected memory overview'),
    );

ConversationReinterpretationJob appliedJob() => const ConversationReinterpretationJob(
      jobId: 'job-1',
      sessionId: sessionId,
      conversationId: conversationId,
      status: 'applied',
      outcome: 'applied',
      correctionIds: [correctionId],
      receipts: [
        ConversationReinterpretationReceiptReference(
          conversationId: conversationId,
          correctionId: correctionId,
          status: 'applied',
        ),
      ],
    );

ConversationCorrectionReceipt appliedReceipt() => const ConversationCorrectionReceipt(
      correctionId: correctionId,
      conversationId: conversationId,
      status: 'applied',
      before: ConversationCorrectionSummary(title: 'Before'),
      after: ConversationCorrectionSummary(title: 'After'),
    );

class MemoryTestAuthority implements ExactAccountAuthorityVerifier {
  bool current = true;
  @override
  String get uid => 'owner';
  @override
  bool isExactCurrent() => current;
}

late MemoryTestAuthority authority;

class _FakeMemoryVoiceSheet extends StatefulWidget {
  const _FakeMemoryVoiceSheet({required this.onSessionEnded});

  final ValueChanged<MemoryReceiptDiscoveryRequest> onSessionEnded;

  @override
  State<_FakeMemoryVoiceSheet> createState() => _FakeMemoryVoiceSheetState();
}

class _FakeMemoryVoiceSheetState extends State<_FakeMemoryVoiceSheet> {
  @override
  void dispose() {
    widget.onSessionEnded(
      const MemoryReceiptDiscoveryRequest(conversationId: conversationId, sessionId: sessionId),
    );
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ElevatedButton(
          key: const ValueKey('end-memory-session'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('End session'),
        ),
      ),
    );
  }
}

Widget app({
  required MemoryReinterpretationReceiptDiscovery discovery,
  ConversationDetailProvider? provider,
  ValueNotifier<ServerConversation>? selectedConversation,
  Future<ConversationCorrectionReceipt?> Function({
    required String conversationId,
    required String correctionId,
    String? expectedAuthenticatedUid,
    ExactAccountAuthorityVerifier? exactAuthority,
  })? undoCorrection,
}) {
  final detail = provider ?? detailProvider();
  Widget button(ServerConversation memory) => MemoryTalkButton(
        conversation: memory,
        authorityProvider: () => authority,
        receiptDiscovery: discovery,
        undoCorrection: undoCorrection ?? undoConversationCorrection,
        routeOpener: (context, _, onSessionEnded) => showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          isDismissible: false,
          enableDrag: false,
          builder: (_) => FractionallySizedBox(
            heightFactor: 0.94,
            child: _FakeMemoryVoiceSheet(onSessionEnded: onSessionEnded),
          ),
        ),
      );
  return MaterialApp(
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: ChangeNotifierProvider.value(
      value: detail,
      child: Scaffold(
        body: Column(children: [
          Consumer<ConversationDetailProvider>(
            builder: (_, provider, __) => Text(provider.conversation.structured.title, key: const ValueKey('summary')),
          ),
          selectedConversation == null
              ? button(memoryConversation())
              : ValueListenableBuilder<ServerConversation>(
                  valueListenable: selectedConversation,
                  builder: (_, memory, __) => button(memory),
                ),
        ]),
      ),
    ),
  );
}

ConversationDetailProvider detailProvider({ConversationDetailLoader? loader}) {
  final provider = ConversationDetailProvider(
    conversationLoader: loader ?? (id, {expectedAuthenticatedUid, exactAuthority}) async => memoryConversation(id: id),
  );
  provider.selectedDate = memoryConversation().createdAt;
  provider.setCachedConversation(memoryConversation());
  return provider;
}

Future<void> endSession(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('memory-talk-$conversationId')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const ValueKey('end-memory-session')));
  await tester.pumpAndSettle();
}

Widget typedApp({
  required CorrectionReceiptPoller poller,
  required ConversationDetailProvider provider,
  ValueNotifier<ServerConversation>? selected,
  double textScale = 1,
  Future<ConversationCorrectionReceipt?> Function(
          {required String conversationId,
          required String correctionId,
          String? expectedAuthenticatedUid,
          ExactAccountAuthorityVerifier? exactAuthority})?
      undo,
}) {
  Widget button(ServerConversation memory) => MemoryTypeCorrectionButton(
        conversation: memory,
        appSummary: memory.structured.overview,
        authorityProvider: () => authority,
        receiptPoller: poller,
        undoCorrection: undo ?? undoConversationCorrection,
        submitter: (
                {required conversationId,
                required correctionId,
                required correctionText,
                summaryTitle,
                summaryOverview,
                appSummary,
                expectedAuthenticatedUid,
                exactAuthority,
                required requestTimeout}) async =>
            ConversationCorrectionSubmitResult.accepted(ConversationCorrectionSubmission(
                conversationId: conversationId,
                correctionId: correctionId,
                traceId: 'test',
                status: 'queued',
                queued: true)),
      );
  return MaterialApp(
    theme: ellaThemeData(),
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    builder: (context, child) =>
        MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(textScale)), child: child!),
    home: ChangeNotifierProvider.value(
        value: provider,
        child: Scaffold(
            body: SingleChildScrollView(
                child: selected == null
                    ? button(memoryConversation())
                    : ValueListenableBuilder<ServerConversation>(
                        valueListenable: selected, builder: (_, memory, __) => button(memory))))),
  );
}

Future<void> submitTyped(WidgetTester tester) async {
  await tester.tap(find.text('Type a correction'));
  await tester.pumpAndSettle();
  await tester.enterText(find.byKey(const ValueKey('type-correction-input')), 'A private correction');
  await tester.ensureVisible(find.byKey(const ValueKey('type-correction-submit')));
  await tester.tap(find.byKey(const ValueKey('type-correction-submit')));
  await tester.pumpAndSettle();
}

void main() {
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeSolid')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      if (flutterCache.parent.path == flutterCache.path) throw StateError('Flutter material font unavailable');
      flutterCache = flutterCache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then(ByteData.sublistView)))
        .load();
  });
  setUp(() async {
    authority = MemoryTestAuthority();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  testWidgets('typed applied canonical receipt retains Review and Undo with original authority', (tester) async {
    var reloads = 0;
    var undos = 0;
    final detail = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
      expect(identical(exactAuthority, authority), isTrue);
      expect(expectedAuthenticatedUid, authority.uid);
      reloads++;
      return memoryConversation(id: id);
    });
    await tester.pumpWidget(typedApp(
        provider: detail,
        poller: (
            {required conversationId,
            required correctionId,
            required expectedAuthenticatedUid,
            required exactAuthority,
            required pollBudget}) async {
          expect(identical(exactAuthority, authority), isTrue);
          return ConversationCorrectionReceipt(
              conversationId: conversationId,
              correctionId: correctionId,
              status: 'applied',
              before: const ConversationCorrectionSummary(title: 'Before'),
              after: const ConversationCorrectionSummary(title: 'After'));
        },
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
          expect(identical(exactAuthority, authority), isTrue);
          expect(expectedAuthenticatedUid, authority.uid);
          undos++;
          return ConversationCorrectionReceipt(
              conversationId: conversationId,
              correctionId: correctionId,
              status: 'undone',
              before: const ConversationCorrectionSummary(title: 'Before'),
              after: const ConversationCorrectionSummary(title: 'After'));
        }));
    await submitTyped(tester);
    expect(find.text('Memory updated'), findsOneWidget);
    expect(reloads, 1);
    await tester.tap(find.text('Review'));
    await tester.pumpAndSettle();
    expect(find.text('Before'), findsWidgets);
    expect(find.text('After'), findsWidgets);
    await tester.tap(find.text('Undo update'));
    await tester.pumpAndSettle();
    expect(undos, 1);
    expect(reloads, 2);
    expect(find.text('Memory update undone'), findsWidgets);
  });

  for (final status in [
    'queued',
    'null',
    'exception',
    'direct_apply_failed',
    'wrong_conversation',
    'wrong_correction'
  ]) {
    testWidgets('typed receipt $status never exposes applied Review or refresh', (tester) async {
      var reloads = 0;
      final detail = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        reloads++;
        return memoryConversation(id: id);
      });
      await tester.pumpWidget(typedApp(
          provider: detail,
          poller: (
              {required conversationId,
              required correctionId,
              required expectedAuthenticatedUid,
              required exactAuthority,
              required pollBudget}) async {
            if (status == 'null') return null;
            if (status == 'exception') throw StateError('private receipt error');
            return ConversationCorrectionReceipt(
                conversationId: status == 'wrong_conversation' ? 'other' : conversationId,
                correctionId: status == 'wrong_correction' ? 'other' : correctionId,
                status: status.startsWith('wrong_') ? 'applied' : status,
                before: const ConversationCorrectionSummary(),
                after: const ConversationCorrectionSummary());
          }));
      await submitTyped(tester);
      expect(find.text('Review'), findsNothing);
      expect(find.text('Memory updated'), findsNothing);
      expect(reloads, 0);
    });
  }

  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('typed Review and Undo remain reachable at 3x on $size', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      const captureKey = ValueKey('typed-review-capture');
      await tester.pumpWidget(RepaintBoundary(
          key: captureKey,
          child: typedApp(
              provider: detailProvider(),
              textScale: 3,
              poller: (
                      {required conversationId,
                      required correctionId,
                      required expectedAuthenticatedUid,
                      required exactAuthority,
                      required pollBudget}) async =>
                  ConversationCorrectionReceipt(
                      conversationId: conversationId,
                      correctionId: correctionId,
                      status: 'applied',
                      before: const ConversationCorrectionSummary(title: 'Before'),
                      after: const ConversationCorrectionSummary(title: 'After')))));
      await submitTyped(tester);
      expect(tester.takeException(), isNull);
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Review'));
      await tester.pumpAndSettle();
      expect(tester.getSize(find.widgetWithText(TextButton, 'Review')).height, greaterThanOrEqualTo(48));
      await tester.tap(find.text('Review'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Undo update'));
      await tester.pumpAndSettle();
      expect(find.text('Undo update').hitTestable(), findsOneWidget);
      expect(tester.getSize(find.widgetWithText(OutlinedButton, 'Undo update')).height, greaterThanOrEqualTo(48));
      expect(tester.takeException(), isNull);
      if (const bool.fromEnvironment('ELLA_CAPTURE_TEST_LAYOUT')) {
        final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(captureKey));
        await tester.runAsync(() async {
          final image = await boundary.toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          await File('/tmp/ella-correction-review-${size.width.toInt()}-3x.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
      }
    });
  }

  testWidgets('typed late Undo ignores replaced account and does not refresh or claim undone', (tester) async {
    final pending = Completer<ConversationCorrectionReceipt?>();
    var reloads = 0;
    late String submittedId;
    final detail = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
      reloads++;
      return memoryConversation(id: id);
    });
    await tester.pumpWidget(typedApp(
        provider: detail,
        poller: (
            {required conversationId,
            required correctionId,
            required expectedAuthenticatedUid,
            required exactAuthority,
            required pollBudget}) async {
          submittedId = correctionId;
          return ConversationCorrectionReceipt(
              conversationId: conversationId,
              correctionId: correctionId,
              status: 'applied',
              before: const ConversationCorrectionSummary(),
              after: const ConversationCorrectionSummary());
        },
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) =>
            pending.future));
    await submitTyped(tester);
    expect(reloads, 1);
    await tester.tap(find.text('Review'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Undo update'));
    await tester.pump();
    authority.current = false;
    SharedPreferencesUtil().uid = 'replacement-owner';
    pending.complete(ConversationCorrectionReceipt(
        conversationId: conversationId,
        correctionId: submittedId,
        status: 'undone',
        before: const ConversationCorrectionSummary(),
        after: const ConversationCorrectionSummary()));
    await tester.pumpAndSettle();
    expect(reloads, 1);
    expect(find.text('Memory update undone'), findsNothing);
    expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('typed Review never undoes a newer receipt than the one displayed', (tester) async {
    final pending = Completer<ConversationCorrectionReceipt?>();
    final ids = <String>[];
    var undos = 0;
    await tester.pumpWidget(typedApp(
        provider: detailProvider(),
        poller: (
            {required conversationId,
            required correctionId,
            required expectedAuthenticatedUid,
            required exactAuthority,
            required pollBudget}) async {
          ids.add(correctionId);
          if (ids.length == 2) return pending.future;
          return ConversationCorrectionReceipt(
              conversationId: conversationId,
              correctionId: correctionId,
              status: 'applied',
              before: const ConversationCorrectionSummary(title: 'Original before'),
              after: const ConversationCorrectionSummary(title: 'Original after'));
        },
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
          undos++;
          return null;
        }));
    await submitTyped(tester);
    await submitTyped(tester);
    expect(ids, hasLength(2));
    expect(ids.first, isNot(ids.last));
    await tester.tap(find.text('Review'));
    await tester.pumpAndSettle();
    expect(find.text('Original after'), findsOneWidget);
    pending.complete(ConversationCorrectionReceipt(
        conversationId: conversationId,
        correctionId: ids.last,
        status: 'applied',
        before: const ConversationCorrectionSummary(title: 'New before'),
        after: const ConversationCorrectionSummary(title: 'New after')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Undo update'));
    await tester.pumpAndSettle();
    expect(undos, 0);
    expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
    expect(find.text('Memory update undone'), findsNothing);
  });

  testWidgets('typed cached Review invalidates on same UID account ABA without an API call', (tester) async {
    await tester.pumpWidget(typedApp(
        provider: detailProvider(),
        poller: (
                {required conversationId,
                required correctionId,
                required expectedAuthenticatedUid,
                required exactAuthority,
                required pollBudget}) async =>
            ConversationCorrectionReceipt(
                conversationId: conversationId,
                correctionId: correctionId,
                status: 'applied',
                before: const ConversationCorrectionSummary(),
                after: const ConversationCorrectionSummary())));
    await submitTyped(tester);
    expect(find.text('Review'), findsOneWidget);
    SharedPreferencesUtil().uid = 'replacement-owner';
    SharedPreferencesUtil().uid = authority.uid;
    await tester.pumpAndSettle();
    expect(find.text('Review'), findsNothing);
    expect(find.text('Memory updated'), findsNothing);
  });

  for (final replacement in ['account', 'account ABA', 'conversation', 'conversation ABA', 'dispose']) {
    testWidgets('typed late applied receipt ignored after $replacement replacement', (tester) async {
      final pending = Completer<ConversationCorrectionReceipt?>();
      late String submittedId;
      var reloads = 0;
      final selected = ValueNotifier(memoryConversation());
      addTearDown(selected.dispose);
      final detail = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        reloads++;
        return memoryConversation(id: id);
      });
      await tester.pumpWidget(typedApp(
          provider: detail,
          selected: selected,
          poller: (
              {required conversationId,
              required correctionId,
              required expectedAuthenticatedUid,
              required exactAuthority,
              required pollBudget}) {
            submittedId = correctionId;
            return pending.future;
          }));
      await submitTyped(tester);
      expect(find.text('Memory updated'), findsNothing);
      if (replacement.startsWith('account')) {
        if (replacement == 'account') authority.current = false;
        SharedPreferencesUtil().uid = 'replacement-owner';
        if (replacement == 'account ABA') SharedPreferencesUtil().uid = authority.uid;
      } else if (replacement == 'dispose') {
        await tester.pumpWidget(const SizedBox());
      } else {
        selected.value = memoryConversation(id: 'other');
        await tester.pump();
        if (replacement == 'conversation ABA') {
          selected.value = memoryConversation();
          await tester.pump();
        }
      }
      pending.complete(ConversationCorrectionReceipt(
          conversationId: conversationId,
          correctionId: submittedId,
          status: 'applied',
          before: const ConversationCorrectionSummary(),
          after: const ConversationCorrectionSummary()));
      await tester.pumpAndSettle();
      expect(find.text('Review'), findsNothing);
      expect(find.text('Memory updated'), findsNothing);
      expect(reloads, 0);
      expect(tester.takeException(), isNull);
    });
  }

  for (final failure in ['null', 'exception', 'wrong receipt', 'still applied']) {
    testWidgets('Undo $failure reports failure and preserves canonical receipt for retry', (tester) async {
      var attempts = 0;
      await tester.pumpWidget(MaterialApp(
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
              body: Builder(
                  builder: (context) => TextButton(
                      child: const Text('Review'),
                      onPressed: () =>
                          showMemoryCorrectionReceiptSheet(context, receipt: appliedReceipt(), onUndo: () async {
                            attempts++;
                            if (failure == 'exception') throw StateError('private transport sentinel');
                            if (failure == 'null') return null;
                            if (failure == 'still applied') return appliedReceipt();
                            return const ConversationCorrectionReceipt(
                                conversationId: 'other',
                                correctionId: correctionId,
                                status: 'undone',
                                before: ConversationCorrectionSummary(),
                                after: ConversationCorrectionSummary());
                          }))))));
      await tester.tap(find.text('Review'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Undo update'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
      expect(find.text('Memory update undone'), findsNothing);
      expect(find.text('private transport sentinel'), findsNothing);
      expect(find.text('Before'), findsWidgets);
      await tester.tap(find.text('Undo update'));
      await tester.pumpAndSettle();
      expect(attempts, 2);
      expect(tester.takeException(), isNull);
    });
  }
  test('memory modal keeps the exact conversation and summary version scope', () {
    final conversation = ServerConversation(
      id: conversationId,
      createdAt: DateTime.utc(2026, 7, 24),
      structured: Structured('A seeded memory', 'The selected memory overview'),
      activeSummaryVersionId: 'summary-v4',
    );

    final scope = MemoryTalkButton.sessionScopeFor(conversation);

    expect(scope.conversationId, conversationId);
    expect(scope.expectedActiveSummaryVersionId, 'summary-v4');
  });

  testWidgets('discovers a delayed receipt after the memory voice sheet closes', (tester) async {
    var refreshes = 0;
    final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
      expect(id, conversationId);
      expect(expectedAuthenticatedUid, authority.uid);
      expect(exactAuthority, same(authority));
      refreshes++;
      return memoryConversation(title: 'After');
    });
    final delayedJob = Completer<ConversationReinterpretationJob?>();
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) => delayedJob.future,
      fetchReceipt: (_, __) async => appliedReceipt(),
      wait: (_) async {},
      maxAttempts: 1,
    );

    await tester.pumpWidget(app(discovery: discovery, provider: provider));
    await tester.tap(find.byKey(const ValueKey('memory-talk-$conversationId')));
    await tester.pumpAndSettle();

    expect(find.byType(_FakeMemoryVoiceSheet), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('end-memory-session')));
    await tester.pumpAndSettle();

    expect(find.byType(_FakeMemoryVoiceSheet), findsNothing);
    expect(find.byType(MemoryCorrectionReceiptChip), findsNothing);
    expect(refreshes, 0);

    delayedJob.complete(appliedJob());
    await tester.pump();
    await tester.pumpAndSettle();

    expect(find.byType(MemoryCorrectionReceiptChip), findsOneWidget);
    expect(find.text('Memory updated'), findsOneWidget);
    expect(find.text('Review'), findsOneWidget);
    expect(refreshes, 1);
    expect(provider.conversation.structured.title, 'After');
    expect(tester.widget<Text>(find.byKey(const ValueKey('summary'))).data, 'After');
  });

  for (final status in ['pending', 'failed', 'no_change']) {
    testWidgets('$status does not refresh or claim the memory changed', (tester) async {
      var refreshes = 0;
      final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        refreshes++;
        return memoryConversation();
      });
      final discovery = MemoryReinterpretationReceiptDiscovery(
        fetchLatest: (_) async => ConversationReinterpretationJob(
          jobId: 'job-1',
          sessionId: sessionId,
          conversationId: conversationId,
          status: status,
          correctionIds: const [],
          receipts: const [],
        ),
        fetchReceipt: (_, __) async => appliedReceipt(),
        maxAttempts: 1,
      );
      await tester.pumpWidget(app(discovery: discovery, provider: provider));
      await endSession(tester);
      expect(refreshes, 0);
      expect(find.text('Memory updated'), findsNothing);
    });
  }

  testWidgets('successful Undo reloads summary and forwards original account authority', (tester) async {
    var refreshes = 0;
    final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
      refreshes++;
      return memoryConversation(title: refreshes == 1 ? 'After' : 'Before');
    });
    final discovery = MemoryReinterpretationReceiptDiscovery(
      fetchLatest: (_) async => appliedJob(),
      fetchReceipt: (_, __) async => appliedReceipt(),
      maxAttempts: 1,
    );
    await tester.pumpWidget(app(
      discovery: discovery,
      provider: provider,
      undoCorrection: (
          {required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
        expect(conversationId, appliedReceipt().conversationId);
        expect(correctionId, appliedReceipt().correctionId);
        expect(expectedAuthenticatedUid, authority.uid);
        expect(exactAuthority, same(authority));
        return ConversationCorrectionReceipt(
          conversationId: conversationId,
          correctionId: correctionId,
          status: 'undone',
          before: const ConversationCorrectionSummary(title: 'Before'),
          after: const ConversationCorrectionSummary(title: 'After'),
        );
      },
    ));
    await endSession(tester);
    await tester.tap(find.text('Review'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Undo update'));
    await tester.pumpAndSettle();
    expect(refreshes, 2);
    expect(provider.conversation.structured.title, 'Before');
    expect(tester.widget<Text>(find.byKey(const ValueKey('summary'))).data, 'Before');
    expect(find.text('Memory update undone'), findsWidgets);
  });

  for (final stale in [false, true]) {
    testWidgets('${stale ? 'stale account' : 'unsuccessful'} Undo does not reload or display undone', (tester) async {
      var refreshes = 0;
      final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        refreshes++;
        return memoryConversation(title: 'After');
      });
      final delayedUndo = Completer<ConversationCorrectionReceipt?>();
      final discovery = MemoryReinterpretationReceiptDiscovery(
        fetchLatest: (_) async => appliedJob(),
        fetchReceipt: (_, __) async => appliedReceipt(),
        maxAttempts: 1,
      );
      await tester.pumpWidget(app(
        discovery: discovery,
        provider: provider,
        undoCorrection: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) =>
            delayedUndo.future,
      ));
      await endSession(tester);
      await tester.tap(find.text('Review'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Undo update'));
      await tester.pump();
      if (stale) authority.current = false;
      delayedUndo.complete(stale
          ? const ConversationCorrectionReceipt(
              conversationId: conversationId,
              correctionId: correctionId,
              status: 'undone',
              before: ConversationCorrectionSummary(title: 'Before'),
              after: ConversationCorrectionSummary(title: 'After'),
            )
          : null);
      await tester.pumpAndSettle();
      expect(refreshes, 1);
      expect(find.text('Memory update undone'), findsNothing);
      expect(provider.conversation.structured.title, 'After');
    });
  }

  for (final accountChange in [false, true]) {
    testWidgets('late receipt after ${accountChange ? 'account' : 'route'} replacement is ignored', (tester) async {
      var refreshes = 0;
      final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
        refreshes++;
        return memoryConversation();
      });
      final delayedJob = Completer<ConversationReinterpretationJob?>();
      final selected = ValueNotifier(memoryConversation());
      final discovery = MemoryReinterpretationReceiptDiscovery(
        fetchLatest: (_) => delayedJob.future,
        fetchReceipt: (_, __) async => appliedReceipt(),
        maxAttempts: 1,
      );
      await tester.pumpWidget(app(discovery: discovery, provider: provider, selectedConversation: selected));
      await endSession(tester);
      if (accountChange) {
        authority.current = false;
      } else {
        selected.value = memoryConversation(id: 'replacement');
        await tester.pump();
      }
      delayedJob.complete(appliedJob());
      await tester.pumpAndSettle();
      expect(refreshes, 0);
      expect(find.byType(MemoryCorrectionReceiptChip), findsNothing);
    });
  }

  for (final accountChange in [false, true]) {
    test('refresh discards delayed GET after ${accountChange ? 'account' : 'route'} replacement', () async {
      final response = Completer<ServerConversation?>();
      final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future);
      final pending = provider.refreshConversation(expectedConversationId: conversationId, exactAuthority: authority);
      if (accountChange) {
        authority.current = false;
      } else {
        provider.setCachedConversation(memoryConversation(id: 'replacement'));
      }
      response.complete(memoryConversation(title: 'Incorrect late update'));
      await pending;
      expect(provider.conversation.structured.title, 'A seeded memory');
    });
  }

  test('zero-argument refresh remains compatible', () async {
    final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) async {
      expect(expectedAuthenticatedUid, isNull);
      expect(exactAuthority, isNull);
      return memoryConversation(title: 'Reloaded');
    });
    await provider.refreshConversation();
    expect(provider.conversation.structured.title, 'Reloaded');
  });

  test('a replaced route cannot accept a late GET even after returning to the same conversation', () async {
    final response = Completer<ServerConversation?>();
    final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) => response.future);
    final pending = provider.refreshConversation(expectedConversationId: conversationId, exactAuthority: authority);
    provider.setCachedConversation(memoryConversation(id: 'replacement'));
    provider.setCachedConversation(memoryConversation(title: 'New route snapshot'));
    response.complete(memoryConversation(title: 'Old route response'));
    await pending;
    expect(provider.conversation.structured.title, 'New route snapshot');
  });

  test('an older overlapping refresh cannot overwrite the newer summary', () async {
    final older = Completer<ServerConversation?>();
    final newer = Completer<ServerConversation?>();
    var calls = 0;
    final provider = detailProvider(loader: (id, {expectedAuthenticatedUid, exactAuthority}) {
      return ++calls == 1 ? older.future : newer.future;
    });
    final oldRefresh = provider.refreshConversation(expectedConversationId: conversationId, exactAuthority: authority);
    final newRefresh = provider.refreshConversation(expectedConversationId: conversationId, exactAuthority: authority);
    newer.complete(memoryConversation(title: 'Newest version'));
    await newRefresh;
    older.complete(memoryConversation(title: 'Old version'));
    await oldRefresh;
    expect(provider.conversation.structured.title, 'Newest version');
  });
}
