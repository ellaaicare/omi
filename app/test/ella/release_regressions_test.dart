import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'package:omi/ella/upstream_capture/ella_upstream_capture_runtime.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('.SF UI Text')
      ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
      ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))).load();
  });
  test('discarded and unknown finalization never announce a saved memory', () {
    expect(ellaCaptureFinishMessage(EllaCaptureFinishOutcome.discarded), contains('Not saved'));
    expect(ellaCaptureFinishMessage(null), contains('requested'));
    expect(ellaCaptureFinishMessage(EllaCaptureFinishOutcome.processing), contains('processing'));
    expect(ellaCaptureFinishMessage(EllaCaptureFinishOutcome.failed), contains('failed'));
    expect(ellaCaptureFinishMessage(EllaCaptureFinishOutcome.saved), contains('created'));
  });
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('transcript and Create stay separate from chat at scale=$scale dark=$dark', (tester) async {
        tester.view.physicalSize = const Size(320, 700);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        var opened = 0, finished = 0;
        await tester.pumpWidget(MaterialApp(
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          home: MediaQuery(
            data: MediaQueryData(size: const Size(320, 700), textScaler: TextScaler.linear(scale)),
            child: Scaffold(body: Column(children: [
              EllaHomeTranscriptCard(
                status: 'Receiving necklace audio', lastAudio: 'Last audio: now',
                transcription: 'Transcription ready', preview: 'PRIVATE TRANSCRIPT NOT INLINE',
                receivingAudio: true,
                stockCapture: const Text('LEGACY PAUSE CONTROL'),
                onOpen: () => opened++, onFinish: () => finished++,
              ),
              const Expanded(child: Center(child: Text('Chat remains below', key: Key('chat')))),
            ])),
          ),
        ));
        expect(find.text('PRIVATE TRANSCRIPT NOT INLINE'), findsNothing);
        expect(find.text('LEGACY PAUSE CONTROL'), findsNothing);
        expect(find.byIcon(Icons.pause), findsNothing);
        expect(find.byIcon(Icons.play_arrow), findsNothing);
        expect(tester.getRect(find.byType(EllaHomeTranscriptCard)).bottom,
            lessThanOrEqualTo(tester.getRect(find.byKey(const Key('chat'))).top));
        final open = find.byKey(const Key('home-open-transcript'));
        await tester.ensureVisible(open);
        await tester.pumpAndSettle();
        expect(open.hitTestable(), findsOneWidget);
        expect(tester.getSize(open).height, greaterThanOrEqualTo(48));
        await tester.tap(open);
        final finish = find.byKey(const Key('home-create-memory'));
        await tester.ensureVisible(finish);
        await tester.pumpAndSettle();
        expect(finish.hitTestable(), findsOneWidget);
        expect(tester.getSize(finish).height, greaterThanOrEqualTo(48));
        await tester.tap(finish);
        expect(opened, 1);
        expect(finished, 1);
        expect(tester.takeException(), isNull);
      });
    }
  }
  for (final mode in ['receiving', 'connected', 'stalled', 'pending']) {
    testWidgets('status $mode does not imply audio or enable duplicate Finish', (tester) async {
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: EllaHomeTranscriptCard(
        status: mode, lastAudio: '', transcription: '', preview: '', onOpen: null,
        receivingAudio: mode == 'receiving', audioStalled: mode == 'stalled',
        finishing: mode == 'pending', onFinish: () {},
      ))));
      final dot = tester.widget<Icon>(find.byKey(const Key('home-capture-status-dot')));
      expect(dot.color == const Color(0xFF238636), mode == 'receiving');
      final finish = tester.widget<TextButton>(find.byKey(const Key('home-create-memory')));
      expect(finish.onPressed == null, mode == 'pending');
      expect(tester.takeException(), isNull);
    });
  }
}
