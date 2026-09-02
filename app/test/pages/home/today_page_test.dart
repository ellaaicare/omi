import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/ella/models/capture_source.dart';
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

  test('explicit source selection overrides stale startup diagnostics while active capture remains observable', () {
    expect(
      todaySelectedCaptureSource(
        state: RecordingState.error,
        diagnostics: const CaptureDiagnostics(source: CaptureDiagnosticSource.necklace),
        preferredSource: EllaCaptureSource.phone,
      ),
      EllaCaptureSource.phone,
    );
    expect(
      todaySelectedCaptureSource(
        state: RecordingState.deviceRecord,
        diagnostics: const CaptureDiagnostics(source: CaptureDiagnosticSource.necklace),
        preferredSource: EllaCaptureSource.phone,
      ),
      EllaCaptureSource.phone,
    );
  });

  testWidgets('compact dock distinguishes idle, initialising, and reconnecting states', (tester) async {
    await _pumpRecordControl(tester);
    var l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockRecord), findsOneWidget);
    expect(find.text(l10n.todayDockPhoneReady), findsOneWidget);
    expect(find.text(l10n.todayDockTranscriptPhone), findsOneWidget);
    expect(find.byKey(const Key('today-capture-source-selector')), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
    expect(find.byKey(const Key('today-capture-dock')), findsOneWidget);
    expect(find.byKey(const Key('today-capture-proof-panel')), findsNothing);
    expect(_recordActionSemantics(tester).properties.selected, isFalse);

    await _pumpRecordControl(tester, recordingState: RecordingState.initialising);
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.todayDockPhoneStarting), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);

    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnecting: true,
    );
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));
    expect(find.text(l10n.todayDockNecklaceConnecting), findsOneWidget);
    expect(find.text(l10n.todayDockPhoneReady), findsNothing);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);
  });

  testWidgets('remembered necklace exposes a direct reconnect action from Home', (tester) async {
    var reconnects = 0;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      onTap: () => reconnects += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockNecklaceNotConnected), findsOneWidget);
    expect(find.byIcon(Icons.refresh_rounded), findsWidgets);
    expect(find.text(l10n.todayDockReconnect), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(reconnects, 1);
  });

  testWidgets('missing necklace is unavailable instead of being labelled ready', (tester) async {
    var starts = 0;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      onTap: () => starts += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockNecklaceNotConnected), findsOneWidget);
    expect(find.text(l10n.todayDockNecklaceReady), findsNothing);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);

    await tester.tap(find.byKey(const Key('today-record-moment')), warnIfMissed: false);
    await tester.pump();
    expect(starts, 0);
  });

  testWidgets('legacy necklace requires Home confirmation instead of reconnecting implicitly', (tester) async {
    var confirmations = 0;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      legacyNecklaceNeedsConfirmation: true,
      onTap: () => confirmations += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayLegacyNecklaceDockStatus), findsOneWidget);
    expect(find.byIcon(Icons.link_rounded), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(confirmations, 1);
  });

  testWidgets('active phone capture shows a red Stop and source-specific transcript', (tester) async {
    var primaryTaps = 0;
    var transcriptTaps = 0;
    await _pumpRecordControl(
      tester,
      recordingState: RecordingState.record,
      onTap: () => primaryTaps += 1,
      onViewTranscript: () => transcriptTaps += 1,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockTranscriptPhone), findsOneWidget);
    expect(find.text(l10n.todayDockRecord), findsNothing);
    expect(find.text(l10n.todayDockStop), findsOneWidget);
    expect(find.byIcon(Icons.subject_rounded), findsOneWidget);
    expect(find.byKey(const Key('today-view-live-transcript')), findsOneWidget);
    expect(_recordActionSemantics(tester).properties.selected, isTrue);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(primaryTaps, 1);

    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();

    expect(transcriptTaps, 1);
  });

  testWidgets('interrupted necklace capture keeps source-specific retry and transcript actions', (tester) async {
    var processTaps = 0;
    var transcriptTaps = 0;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnected: true,
      recordingState: RecordingState.error,
      onTap: () => processTaps += 1,
      onViewTranscript: () => transcriptTaps += 1,
    );
    var l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockRetry), findsOneWidget);
    expect(find.text(l10n.todayDockTranscriptNecklace), findsOneWidget);
    expect(find.text(l10n.todayDockNecklaceNeedsAttention), findsOneWidget);
    expect(_recordActionSemantics(tester).properties.selected, isFalse);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.tap(find.byKey(const Key('today-view-live-transcript')));
    await tester.pump();

    expect(processTaps, 1);
    expect(transcriptTaps, 1);

    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnecting: true,
    );
    l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockReconnect), findsOneWidget);
    expect(find.text(l10n.todayDockNecklaceConnecting), findsOneWidget);
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);
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

    expect(find.text(l10n.todayDockPhoneNeedsAttention), findsOneWidget);
    expect(find.text(l10n.todayDockRetry), findsOneWidget);
    final actionSemantics = tester.widget<Semantics>(
      find.ancestor(of: find.byKey(const Key('today-record-moment')), matching: find.byType(Semantics)).first,
    );
    expect(actionSemantics.properties.enabled, isTrue);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(recordTaps, 1);
    expect(unavailableTaps, 0);
  });

  testWidgets('idle source selector changes explicit capture intent and locks while recording', (tester) async {
    EllaCaptureSource? selection;
    await _pumpRecordControl(
      tester,
      hasNecklace: true,
      onSourceSelected: (source) => selection = source,
    );

    await tester.tap(find.byKey(const Key('today-capture-source-necklace')));
    await tester.pump();
    expect(selection, EllaCaptureSource.necklace);

    selection = null;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      activeSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnected: true,
      recordingState: RecordingState.initialising,
      diagnostics: const CaptureDiagnostics(source: CaptureDiagnosticSource.necklace),
      onSourceSelected: (source) => selection = source,
    );
    await tester.tap(find.byKey(const Key('today-capture-source-phone')));
    await tester.pump();
    expect(selection, EllaCaptureSource.phone, reason: 'a stuck necklace start must never trap the source selector');

    selection = null;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.phone,
      activeSource: EllaCaptureSource.phone,
      starting: true,
      hasNecklace: true,
      onSourceSelected: (source) => selection = source,
    );
    await tester.tap(find.byKey(const Key('today-capture-source-necklace')));
    await tester.pump();
    expect(selection, isNull, reason: 'normal phone startup must not rewrite the next capture source');

    selection = null;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      activeSource: EllaCaptureSource.phone,
      starting: true,
      hasNecklace: true,
      onSourceSelected: (source) => selection = source,
    );
    expect(tester.widget<InkWell>(find.byKey(const Key('today-record-moment'))).onTap, isNull);
    await tester.tap(find.byKey(const Key('today-capture-source-phone')));
    await tester.pump();
    expect(selection, isNull, reason: 'a phone startup must stay locked even when a stale preference names necklace');

    selection = null;
    await _pumpRecordControl(
      tester,
      recordingState: RecordingState.record,
      onSourceSelected: (source) => selection = source,
    );
    await tester.tap(find.byKey(const Key('today-capture-source-necklace')));
    await tester.pump();
    expect(selection, isNull);
  });

  testWidgets('continuous necklace capture exposes Save moment instead of Stop', (tester) async {
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.necklace,
      activeSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnected: true,
      recordingState: RecordingState.deviceRecord,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockSaveMoment), findsOneWidget);
    expect(find.text(l10n.todayDockStop), findsNothing);
    expect(find.text(l10n.todayDockTranscriptNecklace), findsOneWidget);
  });

  testWidgets('continuous necklace capture allows an explicit iPhone handoff without relabeling its transcript',
      (tester) async {
    EllaCaptureSource? selection;
    await _pumpRecordControl(
      tester,
      selectedSource: EllaCaptureSource.phone,
      activeSource: EllaCaptureSource.necklace,
      hasNecklace: true,
      necklaceConnected: true,
      recordingState: RecordingState.deviceRecord,
      onSourceSelected: (source) => selection = source,
    );
    final l10n = AppLocalizations.of(tester.element(find.byType(TodayRecordMomentControl)));

    expect(find.text(l10n.todayDockNecklaceActivePhoneSelected), findsOneWidget);
    expect(find.text(l10n.todayDockRecord), findsOneWidget);
    expect(find.text(l10n.todayDockSaveMoment), findsNothing);
    expect(find.text(l10n.todayDockTranscriptNecklace), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-capture-source-necklace')));
    await tester.pump();
    expect(selection, EllaCaptureSource.necklace);
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
  EllaCaptureSource selectedSource = EllaCaptureSource.phone,
  EllaCaptureSource? activeSource,
  bool starting = false,
  bool hasNecklace = false,
  bool legacyNecklaceNeedsConfirmation = false,
  bool necklaceConnected = false,
  bool necklaceConnecting = false,
  RecordingState recordingState = RecordingState.stop,
  CaptureDiagnostics diagnostics = const CaptureDiagnostics(),
  VoidCallback? onViewTranscript,
  ValueChanged<EllaCaptureSource>? onSourceSelected,
  VoidCallback? onUnavailable,
  VoidCallback? onTap,
}) {
  return tester.pumpWidget(
    MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(
        body: TodayRecordMomentControl(
          selectedSource: selectedSource,
          activeSource: activeSource,
          starting: starting,
          hasNecklace: hasNecklace,
          legacyNecklaceNeedsConfirmation: legacyNecklaceNeedsConfirmation,
          necklaceConnected: necklaceConnected,
          necklaceConnecting: necklaceConnecting,
          recordingState: recordingState,
          diagnostics: diagnostics,
          onViewTranscript: onViewTranscript ?? () {},
          onSourceSelected: onSourceSelected ?? (_) {},
          onUnavailable: onUnavailable,
          onTap: onTap ?? () {},
        ),
      ),
    ),
  );
}
