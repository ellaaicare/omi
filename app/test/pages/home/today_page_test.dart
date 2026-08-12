import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/utils/enums.dart';

void main() {
  test('selects only incomplete upcoming reminders due today', () {
    final now = DateTime(2026, 7, 19, 10);
    final items = [
      ActionItemWithMetadata(id: 'today', description: 'Call Greg', completed: false, dueAt: DateTime(2026, 7, 19, 11)),
      ActionItemWithMetadata(
        id: 'completed',
        description: 'Already done',
        completed: true,
        dueAt: DateTime(2026, 7, 19, 12),
      ),
      ActionItemWithMetadata(
        id: 'tomorrow',
        description: 'Tomorrow',
        completed: false,
        dueAt: DateTime(2026, 7, 20, 9),
      ),
    ];

    expect(todayUpcomingReminders(items, now).map((item) => item.id), ['today']);
  });

  test('action item source labels survive API parsing', () {
    final item = ActionItemWithMetadata.fromJson({
      'id': 'from-david',
      'description': 'Dinner with David',
      'completed': false,
      'source_label': 'David',
    });

    expect(item.sourceLabel, 'David');
    expect(item.toJson()['source_label'], 'David');
  });

  testWidgets('record control distinguishes idle, initialising, and reconnecting states', (tester) async {
    await _pumpRecordControl(tester);
    var l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayRecordMoment), findsOneWidget);
    expect(find.text(l10n.startRecording), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsNothing);

    await _pumpRecordControl(tester, recordingState: RecordingState.initialising);
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.initialisingRecorder), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);

    await _pumpRecordControl(tester, necklaceConnecting: true);
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.todayStripReconnecting), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);
  });

  testWidgets('non-Home active capture opens its live transcript from the primary action', (tester) async {
    var primaryTaps = 0;
    var transcriptTaps = 0;
    await _pumpRecordControl(
      tester,
      recordingState: RecordingState.record,
      onTap: () => primaryTaps += 1,
      onViewTranscript: () => transcriptTaps += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.liveTranscript), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsNothing);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(primaryTaps, 0);
    expect(transcriptTaps, 1);
  });

  testWidgets('interrupted Home necklace ownership keeps process and transcript actions', (tester) async {
    var processTaps = 0;
    var transcriptTaps = 0;
    await _pumpRecordControl(
      tester,
      homeCaptureOwned: true,
      homeCaptureUsesNecklace: true,
      recordingState: RecordingState.error,
      onTap: () => processTaps += 1,
      onViewTranscript: () => transcriptTaps += 1,
    );
    var l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayRecordMoment), findsOneWidget);
    expect(find.text(l10n.stopRecording), findsOneWidget);
    expect(find.text(l10n.liveTranscript), findsOneWidget);
    expect(find.text(l10n.todayRecordWithNecklace), findsOneWidget);
    expect(find.byKey(const Key('today-recording-error-status')), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();

    expect(processTaps, 1);
    expect(transcriptTaps, 1);

    await _pumpRecordControl(
      tester,
      homeCaptureOwned: true,
      homeCaptureUsesNecklace: true,
      necklaceConnecting: true,
    );
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.stopRecording), findsOneWidget);
    expect(find.text(l10n.todayStripReconnecting), findsOneWidget);
    expect(find.byKey(const Key('today-recording-reconnecting-status')), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNotNull);
  });
}

Future<void> _pumpRecordControl(
  WidgetTester tester, {
  bool homeCaptureOwned = false,
  bool homeCaptureUsesNecklace = false,
  bool starting = false,
  bool necklaceConnected = false,
  bool necklaceConnecting = false,
  RecordingState recordingState = RecordingState.stop,
  bool necklaceContinuouslyRecording = false,
  VoidCallback? onViewTranscript,
  VoidCallback? onTap,
}) {
  return tester.pumpWidget(
    MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(
        body: TodayRecordMomentControl(
          homeCaptureOwned: homeCaptureOwned,
          homeCaptureUsesNecklace: homeCaptureUsesNecklace,
          starting: starting,
          necklaceConnected: necklaceConnected,
          necklaceConnecting: necklaceConnecting,
          recordingState: recordingState,
          necklaceContinuouslyRecording: necklaceContinuouslyRecording,
          onViewTranscript: onViewTranscript ?? () {},
          onTap: onTap ?? () {},
        ),
      ),
    ),
  );
}
