import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';
import 'package:omi/widgets/transcript.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Medium.ttf')))
        .load();
    await (FontLoader(
      'packages/font_awesome_flutter/FontAwesomeSolid',
    )..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      flutterCache = flutterCache.parent;
    }
    final materialIcons = File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader(
      'MaterialIcons',
    )..addFont(materialIcons.readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Process-global test services may already be initialized.
    }
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({'givenName': 'Margaret'});
    await SharedPreferencesUtil.init();
  });

  testWidgets('ready Home matches the reviewed Memory Canvas hierarchy', (tester) async {
    final photoData = await rootBundle.load('assets/images/onboarding-bg-1.webp');
    final conversations = _ConversationFixtures.withMemories(photoBase64: base64Encode(photoData.buffer.asUint8List()));
    final harness = await _pumpHome(tester, conversations: conversations, includeBottomNav: true);
    addTearDown(harness.dispose);

    final renderedText =
        tester.widgetList<Text>(find.byType(Text)).map((widget) => widget.data).whereType<String>().toList();
    expect(find.text('SUNDAY · AUGUST 9'), findsOneWidget, reason: renderedText.join(' | '));
    expect(find.text('Good morning, Margaret'), findsOneWidget);
    expect(find.text('Record'), findsOneWidget);
    expect(find.text('iPhone · Ready'), findsOneWidget);
    expect(find.text('Recent memories'), findsNothing);
    expect(find.byKey(const Key('memory-source-photo')), findsOneWidget);
    expect(find.byKey(const Key('memory-curated-art-memory-2')), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.text('Reminders'), findsNothing);
    expect(find.byIcon(Icons.tune_rounded), findsNothing);
    expect(find.text('Talk'), findsOneWidget);
    expect(find.text('Voice'), findsNothing);
    expect(find.text('Read aloud'), findsNothing);
    expect(find.textContaining('Ella will speak from'), findsNothing);
    expect(find.textContaining('Phone only'), findsNothing);

    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ella_home_memory_mosaic.png'));
  });

  testWidgets('Daily Note preview opens the full note and hands off to scoped talk', (tester) async {
    const fullBody =
        'A longer Daily Note keeps its complete grounded text available here while Home remains a compact overview.';
    var talkOpens = 0;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      todayResponse: TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'detail-note',
          version: 2,
          kind: TodayCardKind.memory,
          eyebrow: 'ELLA’S DAILY NOTE',
          headline: 'A note worth opening',
          body: fullBody,
          generatedAt: DateTime(2026, 8, 10, 8),
          sourceRefs: const [TodayCardSourceRef(kind: 'memory', id: 'memory-1')],
        ),
      ),
      todayCardTalkRouteOpener: (_, __) async => talkOpens += 1,
    );
    addTearDown(harness.dispose);

    await tester.ensureVisible(find.byKey(const Key('today-card-read-more')));
    await tester.drag(find.byKey(const Key('today-scroll')), const Offset(0, -180));
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-card-read-more')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('today-card-detail-scroll')), findsOneWidget);
    expect(find.text(fullBody), findsNWidgets(2));
    expect(find.byKey(const Key('today-card-detail-talk')), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-card-detail-talk')));
    await tester.pumpAndSettle();

    expect(talkOpens, 1);
    expect(find.byKey(const Key('today-card-detail-scroll')), findsNothing);
  });

  testWidgets('Home memory feed supports confirmed swipe deletion', (tester) async {
    final conversations = _ConversationFixtures.manyMemories();
    final harness = await _pumpHome(tester, conversations: conversations);
    addTearDown(harness.dispose);

    expect(harness.conversations.permanentDeletes, isEmpty);
    await tester.fling(find.byKey(const Key('memory-card-memory-1')), const Offset(-420, 0), 1600);
    await tester.pumpAndSettle();
    expect(find.text('Delete Conversation?'), findsOneWidget);

    await tester.tap(find.text('Delete'));
    await tester.pumpAndSettle();
    expect(harness.conversations.permanentDeletes, ['memory-1']);
    expect(find.byKey(const Key('memory-card-memory-1')), findsNothing);
  });

  testWidgets('Home memory cards preserve sensitive-title sanitization', (tester) async {
    final sensitiveMemory = ServerConversation(
      id: 'sensitive-memory',
      createdAt: DateTime(2026, 8, 8, 9),
      startedAt: DateTime(2026, 8, 8, 9),
      structured: Structured(
        '[MED] Doctor appointment and Emergency monitoring',
        'A private memory overview.',
      ),
    );
    final harness = await _pumpHome(tester, conversations: [sensitiveMemory]);
    addTearDown(harness.dispose);

    expect(find.text('Untitled Conversation'), findsOneWidget);
    expect(find.textContaining('[MED]'), findsNothing);
    expect(find.textContaining('Doctor'), findsNothing);
    expect(find.textContaining('Emergency'), findsNothing);
  });

  testWidgets('Home never renders inherited reminder content', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: _ConversationFixtures.withMemories(photoBase64: ''),
      actionItems: [
        ActionItemWithMetadata(
          id: 'medical-reminder',
          description: '[MED] Placeholder dose',
          completed: false,
          dueAt: DateTime(2026, 8, 9, 23, 59),
        ),
      ],
    );
    addTearDown(harness.dispose);

    expect(find.text('Reminders'), findsNothing);
    expect(find.textContaining('[MED]'), findsNothing);
    expect(find.textContaining('11:59'), findsNothing);
  });

  testWidgets('phone capture transforms in place and finishes the moment', (tester) async {
    final harness = await _pumpHome(tester, conversations: const []);
    addTearDown(harness.dispose);

    final recordTarget = find.byKey(const Key('today-record-moment'));
    expect(tester.getSize(recordTarget).height, greaterThanOrEqualTo(48));
    expect(find.text('iPhone · Ready'), findsOneWidget);
    expect(find.textContaining('Ella is listening'), findsNothing);

    await tester.tap(recordTarget);
    await tester.pump();
    expect(harness.capture.phoneStarts, 1);
    expect(find.text('Recording on this iPhone'), findsOneWidget);

    await tester.tap(recordTarget);
    await tester.pump();
    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finishes, 1);
    expect(find.text('Record'), findsOneWidget);
  });

  testWidgets('confirmed phone audio remains visibly identified outside a Home-owned moment', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      initialRecordingState: RecordingState.record,
    );
    addTearDown(harness.dispose);

    expect(find.byKey(const Key('today-dock-status')), findsOneWidget);
    expect(find.text('Recording on this iPhone'), findsOneWidget);
  });

  testWidgets('continuous necklace exposes live transcript and explicit process control', (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.deviceRecord,
    );
    addTearDown(harness.dispose);
    harness.capture.segments = [
      TranscriptSegment(
        id: 'live-segment',
        text: 'This transcript is visible while recording.',
        speaker: 'SPEAKER_00',
        isUser: false,
        personId: null,
        start: 0,
        end: 1,
        translations: const [],
      ),
    ];
    harness.capture.notifyListeners();
    await tester.pump();

    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    final transcript = tester.widget<TranscriptWidget>(find.byType(TranscriptWidget));
    expect(transcript.segments.single.text, 'This transcript is visible while recording.');
    expect(find.text('Process Now'), findsOneWidget);

    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.finishes, 1);
    expect(harness.capture.deviceStops, 0, reason: 'processing must preserve the continuous necklace stream');
    expect(harness.capture.recordingState, RecordingState.deviceRecord);
    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
  });

  testWidgets('Home Record suspends ambient necklace capture for the iPhone and restores it after finish',
      (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.deviceRecord,
    );
    addTearDown(harness.dispose);

    expect(find.text('Recording with your necklace'), findsOneWidget);
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.deviceStops, 1);
    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.recordingState, RecordingState.record);
    expect(find.text('Recording on this iPhone'), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.deviceStarts, 1);
    expect(harness.capture.recordingState, RecordingState.deviceRecord);
  });

  testWidgets('failed phone finalization keeps ambient necklace stopped until the same moment succeeds',
      (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.deviceRecord,
      finalizationResults: [true, false, true],
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    harness.capture.segments = [_liveTranscriptSegment('phone-retry-before-necklace')];
    harness.capture.notifyListeners();
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalizationCalls, 2);
    expect(harness.capture.deviceStarts, 0, reason: 'necklace must remain stopped while the phone moment is pending');
    expect(harness.capture.recordingState, RecordingState.stop);

    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.phoneStarts, 1, reason: 'retry must not create a replacement phone capture');
    expect(harness.capture.phoneStops, 1, reason: 'retry must not stop the phone transport twice');
    expect(harness.capture.finalizationCalls, 3);
    expect(harness.capture.deviceStarts, 1, reason: 'ambient necklace resumes only after the phone moment succeeds');
    expect(harness.capture.recordingState, RecordingState.deviceRecord);
  });

  testWidgets('capture transport failure remains actionable and retries with the iPhone microphone', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      initialRecordingState: RecordingState.error,
    );
    addTearDown(harness.dispose);

    expect(find.byKey(const Key('today-dock-status')), findsOneWidget);
    expect(find.text('iPhone · Ready'), findsOneWidget);
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.recordingState, RecordingState.record);
    expect(find.text('Recording on this iPhone'), findsOneWidget);
  });

  testWidgets('phone capture failure stays stopped and explains the missing transcript service', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      phoneStartResult: PhoneCaptureStartResult.transcriptionUnavailable,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.recordingState, RecordingState.stop);
    expect(find.text("Ella couldn't connect to transcription, so recording didn't start."), findsOneWidget);
    expect(find.text('Listening… tap to finish'), findsNothing);
  });

  testWidgets('empty phone capture creates no processing memory', (tester) async {
    final harness = await _pumpHome(tester, conversations: const [], captureHasContent: false);
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finishes, 0);
    expect(find.text('No words were captured, so no memory was created.'), findsOneWidget);
  });

  testWidgets('phone stop waits for the server-final transcript before processing', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      captureHasContent: false,
      captureHasFinalContent: true,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalContentChecks, 1);
    expect(harness.capture.finishes, 1);
    expect(find.text('No words were captured, so no memory was created.'), findsNothing);
  });

  testWidgets('failed Home phone processing retries without restarting or re-stopping capture', (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      finalizationResults: [false, true],
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    harness.capture.segments = [_liveTranscriptSegment('phone-retry')];
    harness.capture.notifyListeners();
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalizationCalls, 1);
    expect(find.text('Process Now'), findsOneWidget);

    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pumpAndSettle();

    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalizationCalls, 2);
    expect(harness.capture.finishes, 1);
    expect(find.text('Process Now'), findsNothing);
  });

  testWidgets('rapid Process Now taps share one Home finalization', (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final finalizationGate = Completer<void>();
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      finalizationGate: finalizationGate,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    harness.capture.segments = [_liveTranscriptSegment('phone-double-tap')];
    harness.capture.notifyListeners();
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    final processNow = find.text('Process Now');
    await tester.tap(processNow);
    await tester.tap(processNow);
    await tester.pump();

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalizationCalls, 1);
    expect(find.byKey(const Key('conversation-process-now-progress')), findsOneWidget);

    finalizationGate.complete();
    await tester.pumpAndSettle();

    expect(harness.capture.finalizationCalls, 1);
    expect(harness.capture.finishes, 1);
    expect(find.text('Process Now'), findsNothing);
  });

  testWidgets('leaving transcript blocks a new capture until Home finalization settles', (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final finalizationGate = Completer<void>();
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      finalizationResults: [false],
      finalizationGate: finalizationGate,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    harness.capture.segments = [_liveTranscriptSegment('phone-route-abandon')];
    harness.capture.notifyListeners();
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Process Now'));
    await tester.pump();

    expect(harness.capture.finalizationCalls, 1);
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(find.byType(ConversationCapturingPage), findsNothing);
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStarts, 1, reason: 'old finalization must remain a capture barrier after route exit');

    finalizationGate.complete();
    await tester.pumpAndSettle();
    expect(harness.capture.finalizationCalls, 1);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.phoneStarts, 1,
        reason: 'a failed routed moment must be retried before a new capture starts');
    expect(harness.capture.finalizationCalls, 2);
  });

  testWidgets('account authority change invalidates a pending Home finalization retry', (tester) async {
    SharedPreferencesUtil().showSummarizeConfirmation = false;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      finalizationResults: [false, true],
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    harness.capture.segments = [_liveTranscriptSegment('phone-transition')];
    harness.capture.notifyListeners();
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.finalizationCalls, 1);
    harness.authorityChanges.value += 1;
    await tester.pump();

    await tester.tap(find.text('Process Now'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finalizationCalls, 1, reason: 'replacement authority must not retry old-account work');
    expect(harness.capture.finishes, 0);
    expect(find.text('Process Now'), findsOneWidget);
  });

  testWidgets('connected necklace does not change Home Record from the iPhone microphone', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true
      ..batteryLevel = 84;
    final harness = await _pumpHome(tester, conversations: const [], device: device);
    addTearDown(harness.dispose);

    expect(find.text('iPhone · Ready'), findsOneWidget);
    expect(find.text('Phone only'), findsNothing);
    expect(find.textContaining('Headset is off'), findsNothing);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.deviceStarts, 0);
    expect(find.text('Recording on this iPhone'), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.deviceStops, 0);
    expect(harness.capture.finishes, 1);
    expect(harness.capture.recordingState, RecordingState.stop);
    expect(find.text('Record'), findsOneWidget);
  });

  testWidgets('necklace transport error is cleaned before an iPhone retry', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.error,
    );
    addTearDown(harness.dispose);

    expect(find.text('iPhone · Ready'), findsOneWidget);
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.deviceStops, 1);
    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.deviceStarts, 0);
    expect(harness.capture.recordingState, RecordingState.record);
    expect(find.text('Recording on this iPhone'), findsOneWidget);
  });

  testWidgets('failed phone start restores the ambient necklace stream', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.deviceRecord,
      phoneStartResult: PhoneCaptureStartResult.transcriptionUnavailable,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.deviceStops, 1);
    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.deviceStarts, 1);
    expect(harness.capture.recordingState, RecordingState.deviceRecord);
    expect(find.text('Recording with your necklace'), findsOneWidget);
  });

  testWidgets('day-one state is useful and reduced motion removes capture transitions', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      todayResponse: const TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.newUser,
      ),
      disableAnimations: true,
    );
    addTearDown(harness.dispose);

    expect(find.text('Your first note begins with a moment'), findsNothing);
    expect(find.textContaining('note worth returning to'), findsNothing);
    expect(find.byKey(const Key('memory-journal-empty')), findsOneWidget);
    expect(find.text('Your memories will appear here'), findsOneWidget);
    expect(find.byKey(const Key('today-capture-proof-panel')), findsNothing);
    expect(find.byKey(const Key('today-capture-dock')), findsOneWidget);
  });

  testWidgets('Home shows the full lazy memory feed with layout and sort controls', (tester) async {
    final harness = await _pumpHome(tester, conversations: _ConversationFixtures.manyMemories());
    addTearDown(harness.dispose);

    expect(find.byKey(const Key('memory-layout-journal-memory-1')), findsOneWidget);
    expect(find.byKey(const Key('home-memory-layout-menu')), findsOneWidget);
    expect(find.byKey(const Key('home-memory-sort-menu')), findsOneWidget);
    expect(find.byKey(const Key('memories-see-all')), findsNothing);
    await tester.scrollUntilVisible(
      find.byKey(const Key('memory-card-memory-4')),
      300,
      scrollable: find.descendant(of: find.byKey(const Key('today-scroll')), matching: find.byType(Scrollable)),
    );
    expect(find.byKey(const Key('memory-card-memory-4')), findsOneWidget);
  });

  testWidgets('Home requests the next memory page only when the lazy feed nears its end', (tester) async {
    final initial = List.generate(
      16,
      (index) => _ConversationFixtures.memory('memory-$index', daysAgo: index),
    );
    final older = _ConversationFixtures.memory('memory-older', daysAgo: 30);
    final harness = await _pumpHome(
      tester,
      conversations: initial,
      olderConversationPages: [
        [older],
      ],
    );
    addTearDown(harness.dispose);

    expect(harness.conversations.pageRequests, 0);
    final scrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('today-scroll')), matching: find.byType(Scrollable)),
    );
    scrollable.position.jumpTo(scrollable.position.maxScrollExtent);
    await tester.pumpAndSettle();

    expect(harness.conversations.pageRequests, 1);
    expect(harness.conversations.conversations.map((item) => item.id), contains('memory-older'));
    expect(harness.conversations.hasMoreConversations, isFalse);
  });

  testWidgets('Home resets oldest sorting when a refresh restores incomplete pagination', (tester) async {
    final memories = List.generate(
      16,
      (index) => _ConversationFixtures.memory('memory-sort-$index', daysAgo: index),
    );
    final harness = await _pumpHome(tester, conversations: memories);
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('home-memory-sort-menu')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Oldest first'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<PopupMenuButton<MemoryGallerySort>>(find.byKey(const Key('home-memory-sort-menu'))).initialValue,
      MemoryGallerySort.oldest,
    );

    harness.conversations.restoreIncompletePage(memories);
    await tester.pump();

    expect(
      tester.widget<PopupMenuButton<MemoryGallerySort>>(find.byKey(const Key('home-memory-sort-menu'))).initialValue,
      MemoryGallerySort.recent,
    );
  });

  testWidgets('Home reloads account-profile memory layout when authority changes', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'test-user';
    await preferences.saveString('aiConsentProfileBindingId', 'profile-test-user');
    await preferences.saveMemoryGalleryLayout(MemoryGalleryLayout.list.name);
    await preferences.saveString(
      'ellaMemoryGalleryLayout:other-user:profile-other-user',
      MemoryGalleryLayout.grid.name,
    );
    final harness = await _pumpHome(
      tester,
      conversations: [
        _ConversationFixtures.memory('memory-layout-hero', daysAgo: 0),
        _ConversationFixtures.memory('memory-layout-authority', daysAgo: 1),
      ],
    );
    addTearDown(harness.dispose);

    expect(find.byKey(const Key('memory-layout-list-memory-layout-authority')), findsOneWidget);

    await preferences.saveString('aiConsentProfileBindingId', 'profile-other-user');
    preferences.uid = 'other-user';
    harness.authorityChanges.value += 1;
    await tester.pump();

    expect(find.byKey(const Key('memory-layout-grid-memory-layout-authority')), findsOneWidget);
  });

  testWidgets('200 percent text stays readable and preserves capture semantics at 320 width', (tester) async {
    final semantics = tester.ensureSemantics();
    final harness = await _pumpHome(
      tester,
      conversations: _ConversationFixtures.withMemories(photoBase64: ''),
      viewport: const Size(320, 844),
      textScaler: const TextScaler.linear(2),
      includeBottomNav: true,
    );
    addTearDown(harness.dispose);

    final homeScrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('today-scroll')), matching: find.byType(Scrollable)),
    );
    homeScrollable.position.jumpTo(0);
    await tester.pump();
    await expectLater(
      find.byType(MaterialApp),
      matchesGoldenFile('goldens/ella_home_memory_mosaic_320_200_full_shell.png'),
    );

    final recordTarget = find.byKey(const Key('today-record-moment'));
    await tester.dragFrom(const Offset(160, 400), const Offset(0, -600));
    await tester.pump();
    expect(recordTarget, findsOneWidget);
    expect(tester.getSize(recordTarget).height, greaterThanOrEqualTo(48));
    expect(tester.getSemantics(recordTarget).label, 'Record');
    expect(tester.takeException(), isNull);

    await tester.dragFrom(const Offset(160, 400), const Offset(0, -1200));
    await tester.pump();
    expect(find.byKey(const Key('memory-card-memory-2')), findsOneWidget);
    expect(find.text('Home'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);
    expect(find.text('Talk'), findsOneWidget);
    expect(find.text('Settings'), findsOneWidget);
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });
}

TranscriptSegment _liveTranscriptSegment(String id) => TranscriptSegment(
      id: id,
      text: 'A live transcript that is ready to process.',
      speaker: 'SPEAKER_00',
      isUser: false,
      personId: null,
      start: 0,
      end: 1,
      translations: const [],
    );

class _HomeHarness {
  const _HomeHarness({
    required this.capture,
    required this.actionItems,
    required this.conversations,
    required this.device,
    required this.home,
    required this.authorityChanges,
  });

  final _FakeCaptureProvider capture;
  final _FixtureActionsProvider actionItems;
  final _FixtureConversationProvider conversations;
  final DeviceProvider device;
  final HomeProvider home;
  final ValueNotifier<int> authorityChanges;

  void dispose() {
    capture.dispose();
    actionItems.dispose();
    conversations.dispose();
    device.dispose();
    home.dispose();
    authorityChanges.dispose();
  }
}

Future<_HomeHarness> _pumpHome(
  WidgetTester tester, {
  required List<ServerConversation> conversations,
  DeviceProvider? device,
  TodayCardResponse? todayResponse,
  bool includeBottomNav = false,
  bool disableAnimations = false,
  Size viewport = const Size(390, 844),
  TextScaler textScaler = TextScaler.noScaling,
  RecordingState initialRecordingState = RecordingState.stop,
  PhoneCaptureStartResult phoneStartResult = PhoneCaptureStartResult.started,
  bool captureHasContent = true,
  bool captureHasFinalContent = false,
  bool captureHasDeviceBoundaryEvidence = false,
  List<bool> finalizationResults = const [],
  Completer<void>? finalizationGate,
  TodayCardTalkRouteOpener? todayCardTalkRouteOpener,
  List<ActionItemWithMetadata> actionItems = const [],
  List<List<ServerConversation>> olderConversationPages = const [],
}) async {
  tester.view.physicalSize = viewport;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final capture = _FakeCaptureProvider(
    initialRecordingState,
    phoneStartResult: phoneStartResult,
    hasContent: captureHasContent,
    hasFinalContent: captureHasFinalContent,
    hasDeviceBoundaryEvidence: captureHasDeviceBoundaryEvidence,
    finalizationResults: finalizationResults,
    finalizationGate: finalizationGate,
  );
  final actionItemsProvider = _FixtureActionsProvider(actionItems);
  final conversationProvider = _FixtureConversationProvider(
    conversations,
    olderConversationPages: olderConversationPages,
  );
  final deviceProvider = device ?? DeviceProvider();
  final home = HomeProvider();
  final authorityChanges = ValueNotifier<int>(0);
  final response = todayResponse ??
      TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'mosaic',
          version: 1,
          kind: TodayCardKind.memory,
          eyebrow: 'ELLA’S DAILY NOTE',
          headline: 'A small ritual that helped',
          body: 'Morning tea by the window made the day feel calmer.',
          generatedAt: DateTime(2026, 8, 9, 8),
          sourceRefs: const [TodayCardSourceRef(kind: 'hermes_memory', id: 'memory-1', versionId: 'v1')],
        ),
      );

  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<ActionItemsProvider>.value(value: actionItemsProvider),
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<ConversationProvider>.value(value: conversationProvider),
        ChangeNotifierProvider<DeviceProvider>.value(value: deviceProvider),
        ChangeNotifierProvider<HomeProvider>.value(value: home),
      ],
      child: MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MediaQuery(
          data: MediaQueryData(
            size: viewport,
            padding: const EdgeInsets.only(top: 47, bottom: 34),
            disableAnimations: disableAnimations,
            textScaler: textScaler,
          ),
          child: Scaffold(
            body: Stack(
              children: [
                TodayPage(
                  nowProvider: () => DateTime(2026, 8, 9, 9, 12),
                  todayCardRepository: _FixedTodayCardRepository(response),
                  todayCardCache: _MemoryTodayCardCache(),
                  todayCardAuthoritySnapshotProvider: () =>
                      (uid: 'test-user', authorityKey: 'test-authority', isProvisioningReady: true),
                  todayCardAuthorityChanges: authorityChanges,
                  todayCardTalkRouteOpener: todayCardTalkRouteOpener,
                  guardianAvailability: () => false,
                ),
                if (includeBottomNav) BottomNavBar(onTabTap: (_, __) {}),
              ],
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
  for (final element in find.byType(Image).evaluate()) {
    await tester.runAsync(() => precacheImage((element.widget as Image).image, element));
  }
  await tester.pump(const Duration(milliseconds: 100));

  return _HomeHarness(
    capture: capture,
    actionItems: actionItemsProvider,
    conversations: conversationProvider,
    device: deviceProvider,
    home: home,
    authorityChanges: authorityChanges,
  );
}

class _FixtureActionsProvider extends ActionItemsProvider {
  _FixtureActionsProvider(this.values);

  final List<ActionItemWithMetadata> values;

  @override
  List<ActionItemWithMetadata> get actionItems => values;

  @override
  Future<void> fetchActionItems({bool showShimmer = false}) async {}
}

class _FixtureConversationProvider extends ConversationProvider {
  _FixtureConversationProvider(
    List<ServerConversation> values, {
    List<List<ServerConversation>> olderConversationPages = const [],
  }) : _olderConversationPages = olderConversationPages.map(List<ServerConversation>.of).toList() {
    conversations = values;
    hasLoadedConversations = true;
    hasFreshConversations = true;
    hasMoreConversations = _olderConversationPages.isNotEmpty;
  }

  final List<List<ServerConversation>> _olderConversationPages;
  int pageRequests = 0;

  void restoreIncompletePage(List<ServerConversation> values) {
    conversations = List<ServerConversation>.of(values);
    hasMoreConversations = true;
    notifyListeners();
  }

  @override
  Future<void> ensureFreshConversations() async {}

  @override
  Future<void> getMoreConversationsFromServer() async {
    if (!hasMoreConversations || isLoadingMoreConversations) return;
    pageRequests += 1;
    isLoadingMoreConversations = true;
    notifyListeners();
    await Future<void>.delayed(Duration.zero);
    final page = _olderConversationPages.removeAt(0);
    conversations.addAll(page);
    hasMoreConversations = _olderConversationPages.isNotEmpty;
    isLoadingMoreConversations = false;
    notifyListeners();
  }

  final List<String> permanentDeletes = [];

  @override
  Future<bool> deleteConversationPermanently(ServerConversation conversation) async {
    permanentDeletes.add(conversation.id);
    conversations.removeWhere((item) => item.id == conversation.id);
    notifyListeners();
    return true;
  }
}

class _FakeCaptureProvider extends CaptureProvider {
  _FakeCaptureProvider(
    RecordingState initialState, {
    required this.phoneStartResult,
    required this.hasContent,
    required this.hasFinalContent,
    required this.hasDeviceBoundaryEvidence,
    required List<bool> finalizationResults,
    this.finalizationGate,
  }) : finalizationResults = List<bool>.of(finalizationResults) {
    recordingState = initialState;
  }

  final PhoneCaptureStartResult phoneStartResult;
  final bool hasContent;
  final bool hasFinalContent;
  final bool hasDeviceBoundaryEvidence;
  final List<bool> finalizationResults;
  final Completer<void>? finalizationGate;

  int phoneStarts = 0;
  int phoneStops = 0;
  int deviceStarts = 0;
  int deviceStops = 0;
  int deviceBoundaries = 0;
  int finishes = 0;
  int finalizationCalls = 0;
  int finalContentChecks = 0;

  @override
  bool get hasCapturableContent => hasContent;

  @override
  bool get hasActiveDeviceCaptureBoundaryEvidence => hasDeviceBoundaryEvidence;

  @override
  Future<bool> awaitFinalCapturableContent({
    int maxAttempts = 3,
    Duration retryDelay = const Duration(milliseconds: 250),
  }) async {
    finalContentChecks++;
    return hasFinalContent;
  }

  @override
  Future<bool> finalizeCurrentConversation({
    int maxTranscriptAttempts = 3,
    Duration transcriptRetryDelay = const Duration(milliseconds: 250),
    bool closeTranscriptTransportBeforeProcessing = false,
  }) async {
    finalContentChecks++;
    if (!hasContent && !hasFinalContent) return false;
    finalizationCalls++;
    await finalizationGate?.future;
    final result = finalizationResults.isEmpty ? true : finalizationResults.removeAt(0);
    if (!result) return false;
    finishes++;
    return true;
  }

  @override
  Future<bool> finalizeCurrentDeviceConversationAndContinue() async {
    deviceBoundaries++;
    return finalizeCurrentConversation();
  }

  @override
  Future<PhoneCaptureStartResult> streamRecording() async {
    phoneStarts++;
    updateRecordingState(
      phoneStartResult == PhoneCaptureStartResult.started ? RecordingState.record : RecordingState.stop,
    );
    return phoneStartResult;
  }

  @override
  Future<void> stopStreamRecording() async {
    phoneStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> stopStreamRecordingAndFinalize() async {
    await stopStreamRecording();
    return finalizeCurrentConversation();
  }

  @override
  Future<void> streamDeviceRecording({BtDevice? device}) async {
    deviceStarts++;
    updateRecordingDevice(device);
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    deviceStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<bool> stopStreamDeviceRecordingAndFinalize({bool cleanDevice = false}) async {
    await stopStreamDeviceRecording(cleanDevice: cleanDevice);
    return finalizeCurrentConversation();
  }

  @override
  Future<bool> forceProcessingCurrentConversation({CaptureFinalizationOperation? operation}) async {
    finishes++;
    return true;
  }
}

class _FixedTodayCardRepository implements TodayCardRepository {
  const _FixedTodayCardRepository(this.response);

  final TodayCardResponse response;

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => response;
}

class _MemoryTodayCardCache implements TodayCardCache {
  TodayCard? card;

  @override
  Future<void> clear({required String uid, String authorityKey = ''}) async => card = null;

  @override
  Future<TodayCardCacheEntry?> read({required String uid, required String authorityKey}) async => null;

  @override
  Future<bool> write({
    required String uid,
    required String authorityKey,
    required TodayCard card,
    required Duration maxAge,
    required bool Function() isCurrent,
  }) async {
    if (!isCurrent()) return false;
    this.card = card;
    return true;
  }
}

class _ConversationFixtures {
  static ServerConversation memory(String id, {required int daysAgo}) => ServerConversation(
        id: id,
        createdAt: DateTime(2026, 8, 8).subtract(Duration(days: daysAgo)),
        startedAt: DateTime(2026, 8, 8).subtract(Duration(days: daysAgo)),
        structured: Structured('Memory $id', 'A readable memory overview for $id.'),
      );

  static List<ServerConversation> manyMemories() => List.generate(
        4,
        (index) => ServerConversation(
          id: 'memory-${index + 1}',
          createdAt: DateTime(2026, 8, 8 - index, 9),
          startedAt: DateTime(2026, 8, 8 - index, 9),
          structured: Structured('Memory ${index + 1}', 'A readable memory overview ${index + 1}.'),
        ),
      );

  static List<ServerConversation> withMemories({required String photoBase64}) => [
        ServerConversation(
          id: 'memory-1',
          createdAt: DateTime(2026, 8, 8, 9),
          startedAt: DateTime(2026, 8, 8, 9),
          structured: Structured(
            'Coffee by the window',
            'A slow morning, warm light, and a few minutes that felt entirely your own.',
          ),
          photos: [ConversationPhoto(id: 'photo-1', base64: photoBase64, createdAt: DateTime(2026, 8, 8, 9))],
        ),
        ServerConversation(
          id: 'memory-2',
          createdAt: DateTime(2026, 8, 7, 15),
          startedAt: DateTime(2026, 8, 7, 15),
          structured: Structured(
            'A garden check-in',
            'You noticed the first green tomatoes and made a plan to check again after the weekend.',
          ),
        ),
      ];
}
