import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _VoiceReceiptAuthority implements ExactAccountAuthorityVerifier {
  bool current = true;
  @override
  String get uid => 'voice-owner';
  @override
  bool isExactCurrent() => current;
}

void main() {
  test('Demo voice fixtures never initialize speech recognition', () {
    expect(
      EllaVoiceChatPage.shouldInitializeSpeech(
        EllaVoiceDemoState(quota: EllaAccessDemoFixtures.active.quota),
      ),
      isFalse,
    );
    expect(EllaVoiceChatPage.shouldInitializeSpeech(null), isTrue);
  });

  for (final path in ['standard', 'v2v']) {
    test('$path voice waits for active phone capture stop/finalization before microphone takeover', () async {
      final coordinator = VoicePhoneCaptureTakeoverCoordinator();
      final finalization = Completer<PhoneCaptureStopResult>();
      final events = <String>[];

      final takeover = path == 'standard'
          ? coordinator.prepareStandard(
              phoneCaptureActive: true,
              phoneCaptureContentful: true,
              stopAndFinalizePhoneCapture: () {
                events.add('finalize-start');
                return finalization.future.whenComplete(() => events.add('finalize-ack'));
              },
            )
          : coordinator.prepareV2V(
              phoneCaptureActive: true,
              phoneCaptureContentful: true,
              stopAndFinalizePhoneCapture: () {
                events.add('finalize-start');
                return finalization.future.whenComplete(() => events.add('finalize-ack'));
              },
            );

      await pumpEventQueue();
      expect(events, ['finalize-start']);

      finalization.complete(PhoneCaptureStopResult.finalized);
      if (await takeover) events.add('microphone-start');

      expect(events, ['finalize-start', 'finalize-ack', 'microphone-start']);
    });
  }

  test('failed phone capture acknowledgement blocks voice microphone takeover', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final events = <String>[];

    final acknowledged = await coordinator.prepareStandard(
      phoneCaptureActive: false,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () async {
        events.add('finalize');
        return PhoneCaptureStopResult.failed;
      },
    );
    if (acknowledged) events.add('microphone-start');

    expect(acknowledged, isFalse);
    expect(events, ['finalize']);
  });

  test('thrown phone capture finalization fails closed before voice microphone takeover', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();

    expect(
      await coordinator.prepareV2V(
        phoneCaptureActive: true,
        phoneCaptureContentful: true,
        stopAndFinalizePhoneCapture: () => throw StateError('synthetic finalization failure'),
      ),
      isFalse,
    );
  });

  test('active empty phone capture may hand off after its transcript stop completes', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final transcriptStop = Completer<PhoneCaptureStopResult>();
    var microphoneStarted = false;

    final takeover = coordinator.prepareStandard(
      phoneCaptureActive: true,
      phoneCaptureContentful: false,
      stopAndFinalizePhoneCapture: () => transcriptStop.future,
    );
    await pumpEventQueue();
    expect(microphoneStarted, isFalse);

    transcriptStop.complete(PhoneCaptureStopResult.empty);
    if (await takeover) microphoneStarted = true;

    expect(microphoneStarted, isTrue);
  });

  test('concurrent standard and V2V takeover share one phone capture finalization', () async {
    final coordinator = VoicePhoneCaptureTakeoverCoordinator();
    final finalization = Completer<PhoneCaptureStopResult>();
    var finalizationCalls = 0;

    final standard = coordinator.prepareStandard(
      phoneCaptureActive: true,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () {
        finalizationCalls++;
        return finalization.future;
      },
    );
    final v2v = coordinator.prepareV2V(
      phoneCaptureActive: true,
      phoneCaptureContentful: true,
      stopAndFinalizePhoneCapture: () async {
        finalizationCalls++;
        return PhoneCaptureStopResult.failed;
      },
    );

    await pumpEventQueue();
    expect(finalizationCalls, 1);

    finalization.complete(PhoneCaptureStopResult.finalized);
    expect(await standard, isTrue);
    expect(await v2v, isTrue);
    expect(finalizationCalls, 1);

    expect(
      await coordinator.prepareStandard(
        phoneCaptureActive: false,
        phoneCaptureContentful: false,
        stopAndFinalizePhoneCapture: () async {
          finalizationCalls++;
          return PhoneCaptureStopResult.empty;
        },
      ),
      isTrue,
    );
    expect(finalizationCalls, 1);
  });

  test('standard and V2V production paths use the finalizing phone capture takeover', () {
    final source = File('lib/ella/pages/ella_voice_chat_page.dart').readAsStringSync();

    expect(RegExp(r'_preparePhoneCaptureForVoice\(v2v: false\)').allMatches(source), hasLength(1));
    expect(RegExp(r'_preparePhoneCaptureForVoice\(v2v: true\)').allMatches(source), hasLength(1));
    expect(
      RegExp(r'stopAndFinalizePhoneCapture: captureProvider\.stopPhoneCaptureForVoiceTakeover').allMatches(source),
      hasLength(2),
    );
    expect(source, contains('captureProvider.hasUnfinalizedPhoneCaptureContent'));
    expect(source, isNot(contains('captureDiagnostics.source == CaptureDiagnosticSource.phone')));
    expect(source, isNot(contains('captureProvider.stopStreamRecording()')));
  });

  MemoryReinterpretationEvent receiptEvent(String id) => MemoryReinterpretationEvent(
        state: 'applied',
        sessionId: 'voice-session',
        conversationId: 'voice-memory',
        correctionId: id,
      );

  ConversationCorrectionReceipt voiceReceipt(String id, {String status = 'applied'}) => ConversationCorrectionReceipt(
        conversationId: 'voice-memory',
        correctionId: id,
        status: status,
        before: const ConversationCorrectionSummary(title: 'Before'),
        after: const ConversationCorrectionSummary(title: 'After'),
      );

  Future<void> pumpMemoryVoice(
    WidgetTester tester, {
    required Stream<MemoryReinterpretationEvent> events,
    required _VoiceReceiptAuthority authority,
    required MemoryVoiceReceiptOperation fetchReceipt,
    required MemoryVoiceReceiptOperation undo,
    String conversationId = 'voice-memory',
  }) async {
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: EllaVoiceChatPage(
        demoState: EllaVoiceDemoState(quota: EllaAccessDemoFixtures.active.quota),
        sessionScope: V2VSessionScope.memory(conversationId: conversationId),
        memoryReinterpretationEvents: events,
        memoryReceiptFetcher: fetchReceipt,
        memoryUndo: undo,
        memoryReceiptAuthorityProvider: () => authority,
      ),
    ));
    await tester.pump();
  }

  Future<void> pumpReceiptFrames(WidgetTester tester) async {
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump();
  }

  testWidgets('Voice delayed receipt stays retired after conversation A B A replacement', (tester) async {
    final events = StreamController<MemoryReinterpretationEvent>();
    final authority = _VoiceReceiptAuthority();
    final pending = Completer<ConversationCorrectionReceipt?>();
    var reads = 0;
    Future<ConversationCorrectionReceipt?> fetch({
      required String conversationId,
      required String correctionId,
      String? expectedAuthenticatedUid,
      ExactAccountAuthorityVerifier? exactAuthority,
    }) {
      reads++;
      return pending.future;
    }

    for (final id in ['voice-memory', 'other-memory', 'voice-memory']) {
      await pumpMemoryVoice(tester,
          events: events.stream,
          authority: authority,
          fetchReceipt: fetch,
          undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
              fail('retired receipt must not Undo'),
          conversationId: id);
      if (id == 'voice-memory' && reads == 0) {
        events.add(receiptEvent('receipt-a'));
        await tester.pump();
      }
    }
    pending.complete(voiceReceipt('receipt-a'));
    await pumpReceiptFrames(tester);
    expect(reads, 1);
    expect(find.text('Review'), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    unawaited(events.close());
  });

  for (final next in ['pending', 'applied']) {
    testWidgets('Voice Review A cannot Undo after newer receipt B is $next', (tester) async {
      final events = StreamController<MemoryReinterpretationEvent>();
      final authority = _VoiceReceiptAuthority();
      final pending = Completer<ConversationCorrectionReceipt?>();
      final undos = <String>[];
      await pumpMemoryVoice(tester,
          events: events.stream,
          authority: authority,
          fetchReceipt: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) =>
              correctionId == 'receipt-a' ? Future.value(voiceReceipt(correctionId)) : pending.future,
          undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
            undos.add(correctionId);
            return voiceReceipt(correctionId, status: 'undone');
          });
      events.add(receiptEvent('receipt-a'));
      await tester.pump();
      await pumpReceiptFrames(tester);
      await tester.tap(find.text('Review'));
      await pumpReceiptFrames(tester);
      events.add(receiptEvent('receipt-b'));
      await tester.pump();
      if (next == 'applied') {
        pending.complete(voiceReceipt('receipt-b'));
        await pumpReceiptFrames(tester);
      }
      await tester.tap(find.text('Undo update'));
      await pumpReceiptFrames(tester);
      expect(undos, isEmpty, reason: 'retired displayed receipt never dispatches Undo');
      expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
      expect(find.text('Memory update undone'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      if (!pending.isCompleted) pending.complete(null);
      unawaited(events.close());
    });
  }

  testWidgets('Voice current receipt permits exactly its own Undo after Review reopens', (tester) async {
    final events = StreamController<MemoryReinterpretationEvent>();
    final authority = _VoiceReceiptAuthority();
    final undos = <String>[];
    await pumpMemoryVoice(tester,
        events: events.stream,
        authority: authority,
        fetchReceipt: (
                {required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
            voiceReceipt(correctionId),
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
          expect(expectedAuthenticatedUid, authority.uid);
          expect(exactAuthority, same(authority));
          undos.add(correctionId);
          return voiceReceipt(correctionId, status: 'undone');
        });
    events.add(receiptEvent('receipt-a'));
    await pumpReceiptFrames(tester);
    await tester.tap(find.text('Review'));
    await pumpReceiptFrames(tester);
    tester.state<NavigatorState>(find.byType(Navigator).first).pop();
    await pumpReceiptFrames(tester);
    await tester.tap(find.text('Review'));
    await pumpReceiptFrames(tester);
    await tester.tap(find.text('Undo update'));
    await pumpReceiptFrames(tester);
    expect(undos, ['receipt-a']);
    expect(find.text('Memory update undone'), findsWidgets);
    await tester.pumpWidget(const SizedBox.shrink());
    unawaited(events.close());
  });

  testWidgets('Voice retained Review rejects owner replacement before Undo transport', (tester) async {
    final events = StreamController<MemoryReinterpretationEvent>();
    final authority = _VoiceReceiptAuthority();
    var undos = 0;
    await pumpMemoryVoice(tester,
        events: events.stream,
        authority: authority,
        fetchReceipt: (
                {required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
            voiceReceipt(correctionId),
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
          undos++;
          return voiceReceipt(correctionId, status: 'undone');
        });
    events.add(receiptEvent('receipt-a'));
    await pumpReceiptFrames(tester);
    await tester.tap(find.text('Review'));
    await pumpReceiptFrames(tester);
    authority.current = false;
    await tester.tap(find.text('Undo update'));
    await pumpReceiptFrames(tester);
    expect(undos, 0);
    expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    unawaited(events.close());
  });

  for (final replacement in ['dispose', 'owner']) {
    testWidgets('Voice delayed receipt is ignored after $replacement replacement', (tester) async {
      final events = StreamController<MemoryReinterpretationEvent>();
      final authority = _VoiceReceiptAuthority();
      final response = Completer<ConversationCorrectionReceipt?>();
      await pumpMemoryVoice(tester,
          events: events.stream,
          authority: authority,
          fetchReceipt: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) {
            expect(expectedAuthenticatedUid, authority.uid);
            expect(exactAuthority, same(authority));
            return response.future;
          },
          undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
              null);
      events.add(receiptEvent('receipt-a'));
      await tester.pump();
      if (replacement == 'dispose') {
        await tester.pumpWidget(const SizedBox.shrink());
      } else {
        authority.current = false;
      }
      response.complete(voiceReceipt('receipt-a'));
      await pumpReceiptFrames(tester);
      expect(find.text('Review'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      unawaited(events.close());
    });
  }

  testWidgets('Voice in-flight Undo A cannot replace newer receipt B after response', (tester) async {
    final events = StreamController<MemoryReinterpretationEvent>();
    final authority = _VoiceReceiptAuthority();
    final response = Completer<ConversationCorrectionReceipt?>();
    var undos = 0;
    await pumpMemoryVoice(tester,
        events: events.stream,
        authority: authority,
        fetchReceipt: (
                {required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
            voiceReceipt(correctionId),
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) {
          expect(correctionId, 'receipt-a');
          expect(expectedAuthenticatedUid, authority.uid);
          expect(exactAuthority, same(authority));
          undos++;
          return response.future;
        });
    events.add(receiptEvent('receipt-a'));
    await pumpReceiptFrames(tester);
    await tester.tap(find.text('Review'));
    await pumpReceiptFrames(tester);
    final undo = tester.widget<OutlinedButton>(find.widgetWithText(OutlinedButton, 'Undo update')).onPressed!;
    undo();
    undo();
    await tester.pump();
    expect(undos, 1);
    events.add(receiptEvent('receipt-b'));
    await pumpReceiptFrames(tester);
    response.complete(voiceReceipt('receipt-a', status: 'undone'));
    await pumpReceiptFrames(tester);
    expect(find.byKey(const ValueKey('memory-correction-undo-error')), findsOneWidget);
    expect(find.text('Memory update undone'), findsNothing);
    tester.state<NavigatorState>(find.byType(Navigator).first).pop();
    await pumpReceiptFrames(tester);
    expect(find.text('Memory updated'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    unawaited(events.close());
  });

  testWidgets('Voice wrong event tuple and failed receipt read never admit Review', (tester) async {
    final events = StreamController<MemoryReinterpretationEvent>();
    final authority = _VoiceReceiptAuthority();
    var reads = 0;
    await pumpMemoryVoice(tester,
        events: events.stream,
        authority: authority,
        fetchReceipt: (
            {required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async {
          reads++;
          throw StateError('Synthetic read failure');
        },
        undo: ({required conversationId, required correctionId, expectedAuthenticatedUid, exactAuthority}) async =>
            null);
    events.add(const MemoryReinterpretationEvent(
        state: 'applied', sessionId: 'voice-session', conversationId: 'other-memory', correctionId: 'receipt-a'));
    await pumpReceiptFrames(tester);
    expect(reads, 0);
    events.add(receiptEvent('receipt-a'));
    await pumpReceiptFrames(tester);
    expect(reads, 1);
    expect(find.text('Review'), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    unawaited(events.close());
  });

  Future<void> pumpVoice(
    WidgetTester tester, {
    required EllaVoiceDemoState state,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: EllaVoiceChatPage(demoState: state),
      ),
    );
    await tester.pump();
  }

  testWidgets('soft warning and remaining time are small gentle voice-surface states', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(quota: EllaAccessDemoFixtures.softDaily.quota),
    );

    expect(find.textContaining('left'), findsOneWidget);
    expect(find.textContaining('nearing today’s voice time'), findsOneWidget);
    expect(find.text('Demo preview — voice is not active'), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsNothing);
  });

  testWidgets('all policy outcomes have distinct claim-compliant copy', (tester) async {
    final cases = {
      EllaVoicePolicyReason.quotaDaily: 'you can talk again tomorrow',
      EllaVoicePolicyReason.quotaMonthly: 'after the monthly reset',
      EllaVoicePolicyReason.concurrent: 'End the other voice conversation',
      EllaVoicePolicyReason.suspended: 'You can still use Ella’s other features',
      EllaVoicePolicyReason.sessionMax: 'Start a new voice conversation',
    };

    for (final entry in cases.entries) {
      await pumpVoice(
        tester,
        state: EllaVoiceDemoState(
          quota: EllaAccessDemoFixtures.active.quota,
          policyReason: entry.key,
        ),
      );
      expect(find.textContaining(entry.value), findsOneWidget, reason: entry.key.name);
      expect(find.textContaining('connection needs a moment'), findsNothing, reason: entry.key.name);
    }
  });

  testWidgets('technical failure is not labeled as quota or policy denial', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(
        quota: EllaAccessDemoFixtures.active.quota,
        technicalFailure: true,
      ),
    );

    expect(find.textContaining('connection needs a moment'), findsOneWidget);
    expect(find.textContaining('tomorrow'), findsNothing);
    expect(find.textContaining('monthly reset'), findsNothing);
  });
}
