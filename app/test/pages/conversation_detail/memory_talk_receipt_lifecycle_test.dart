import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/services/memory_reinterpretation_receipt_service.dart';
import 'package:omi/ella/widgets/memory_correction_receipt.dart';
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

void main() {
  setUp(() async {
    authority = MemoryTestAuthority();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
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
