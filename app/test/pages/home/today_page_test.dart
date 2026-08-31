import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/widgets/capture_diagnostics_panel.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/enums.dart';

void main() {
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

  testWidgets('compact dock distinguishes idle, initialising, and reconnecting states', (tester) async {
    await _pumpRecordControl(tester);
    var l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockRecord), findsOneWidget);
    expect(find.text(l10n.todayDockPhoneReady), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
    expect(find.byKey(const Key('today-capture-dock')), findsOneWidget);
    expect(find.byKey(const Key('today-capture-proof-panel')), findsNothing);
    expect(_recordActionSemantics(tester).properties.selected, isFalse);

    await _pumpRecordControl(tester, recordingState: RecordingState.initialising);
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.todayDockStarting), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);

    await _pumpRecordControl(tester, necklaceConnecting: true);
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.todayDockNecklaceConnecting), findsOneWidget);
    expect(find.text(l10n.todayDockPhoneReady), findsNothing);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNotNull);
  });

  testWidgets('remembered necklace exposes a direct reconnect action from Home', (tester) async {
    var reconnects = 0;
    await _pumpRecordControl(
      tester,
      hasNecklace: true,
      onReconnectNecklace: () => reconnects += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockNecklaceNotConnected), findsOneWidget);
    expect(find.byIcon(Icons.refresh_rounded), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-dock-status')));
    await tester.pump();

    expect(reconnects, 1);
  });

  testWidgets('non-Home active capture keeps Finish and Transcript as separate actions', (tester) async {
    var primaryTaps = 0;
    var finishTaps = 0;
    var transcriptTaps = 0;
    await _pumpRecordControl(
      tester,
      recordingState: RecordingState.record,
      onTap: () => primaryTaps += 1,
      onFinishExternalCapture: () => finishTaps += 1,
      onViewTranscript: () => transcriptTaps += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.transcript), findsOneWidget);
    expect(find.text(l10n.todayDockRecord), findsNothing);
    expect(find.text(l10n.todayDockFinish), findsOneWidget);
    expect(find.byIcon(Icons.subject_rounded), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
    expect(_recordActionSemantics(tester).properties.selected, isTrue);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(primaryTaps, 0);
    expect(finishTaps, 1);

    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();

    expect(transcriptTaps, 1);
  });

  testWidgets('interrupted Home necklace ownership keeps recovery and transcript actions', (tester) async {
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

    expect(find.text(l10n.todayDockFinish), findsOneWidget);
    expect(find.text(l10n.transcript), findsOneWidget);
    expect(find.text(l10n.todayDockRecordingNeedsAttention), findsOneWidget);
    expect(_recordActionSemantics(tester).properties.selected, isFalse);

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

    expect(find.text(l10n.todayDockFinish), findsOneWidget);
    expect(find.text(l10n.todayDockNecklaceConnecting), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNotNull);
  });

  testWidgets('capture error keeps the iPhone recorder available for recovery', (tester) async {
    var recordTaps = 0;
    var unavailableTaps = 0;
    await _pumpRecordControl(
      tester,
      recordingState: RecordingState.error,
      onTap: () => recordTaps += 1,
      onUnavailable: () => unavailableTaps += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockPhoneReady), findsOneWidget);
    final actionSemantics = tester.widget<Semantics>(
      find.ancestor(of: find.byKey(const Key('today-record-moment')), matching: find.byType(Semantics)).first,
    );
    expect(actionSemantics.properties.enabled, isTrue);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(recordTaps, 1);
    expect(unavailableTaps, 0);
  });

  testWidgets('compact dock never exposes numeric capture diagnostics', (tester) async {
    await _pumpRecordControl(
      tester,
      diagnostics: const CaptureDiagnostics(
        source: CaptureDiagnosticSource.phone,
        phase: CaptureDiagnosticPhase.finalizing,
        physicalFrames: 12,
        physicalBytes: 2400,
        transmittedFrames: 11,
        transmittedBytes: 2200,
        transcriptSegments: 2,
        latestTranscript: 'A visible test transcript',
        finalizationAttempts: 1,
      ),
    );

    expect(find.byKey(const Key('today-capture-audio-proof')), findsNothing);
    expect(find.byKey(const Key('today-capture-delivery-proof')), findsNothing);
    expect(find.byKey(const Key('today-capture-transcript-proof')), findsNothing);
    expect(find.byKey(const Key('today-capture-memory-proof')), findsNothing);
    expect(find.byKey(const Key('today-capture-dock')), findsOneWidget);
  });

  testWidgets('technical capture proof remains available in the Device diagnostics panel', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: CaptureDiagnosticsPanel(
            diagnostics: CaptureDiagnostics(
              source: CaptureDiagnosticSource.phone,
              phase: CaptureDiagnosticPhase.receivingTranscript,
              physicalFrames: 12,
              physicalBytes: 2400,
              transmittedFrames: 11,
              transcriptSegments: 2,
              latestTranscript: 'A visible test transcript',
            ),
          ),
        ),
      ),
    );

    expect(find.byKey(const Key('device-capture-diagnostics-panel')), findsOneWidget);
    expect(find.byKey(const Key('device-capture-audio-proof')), findsOneWidget);
    expect(find.textContaining('2400'), findsOneWidget);
    expect(find.byKey(const Key('device-capture-delivery-proof')), findsOneWidget);
    expect(find.textContaining('A visible test transcript'), findsOneWidget);
  });
}

Semantics _recordActionSemantics(WidgetTester tester) => tester.widget<Semantics>(
      find.ancestor(of: find.byKey(const Key('today-record-moment')), matching: find.byType(Semantics)).first,
    );

Future<void> _pumpRecordControl(
  WidgetTester tester, {
  bool homeCaptureOwned = false,
  bool homeCaptureUsesNecklace = false,
  bool starting = false,
  bool hasNecklace = false,
  bool necklaceConnected = false,
  bool necklaceConnecting = false,
  RecordingState recordingState = RecordingState.stop,
  CaptureDiagnostics diagnostics = const CaptureDiagnostics(),
  bool necklaceContinuouslyRecording = false,
  VoidCallback? onViewTranscript,
  VoidCallback? onFinishExternalCapture,
  VoidCallback? onUnavailable,
  VoidCallback? onReconnectNecklace,
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
          hasNecklace: hasNecklace,
          necklaceConnected: necklaceConnected,
          necklaceConnecting: necklaceConnecting,
          recordingState: recordingState,
          diagnostics: diagnostics,
          necklaceContinuouslyRecording: necklaceContinuouslyRecording,
          onViewTranscript: onViewTranscript ?? () {},
          onFinishExternalCapture: onFinishExternalCapture ?? () {},
          onUnavailable: onUnavailable,
          onReconnectNecklace: onReconnectNecklace,
          onTap: onTap ?? () {},
        ),
      ),
    ),
  );
}
