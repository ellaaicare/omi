import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('.SF UI Text')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf')))
        .load();
  });
  for (final dark in [false, true]) {
    for (final scale in [1.0, 1.35, 3.5]) {
      for (final width in [320.0, 390.0]) {
        testWidgets('compact retains visible status/action $dark $scale $width', (tester) async {
          tester.view.physicalSize = Size(width, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          var opens = 0;
          await tester.pumpWidget(MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              home: MediaQuery(
                  data: MediaQueryData(textScaler: TextScaler.linear(scale)),
                  child: Scaffold(
                      body: Center(
                          child: SizedBox(
                              width: width,
                              child: EllaHomeTranscriptCard(
                                  status: 'Not recording',
                                  lastAudio: 'not observed',
                                  transcription: 'not confirmed',
                                  preview: 'hidden compact fictional preview',
                                  compact: true,
                                  onOpen: () => opens++)))))));
          expect(tester.takeException(), isNull);
          expect(find.text('hidden compact fictional preview'), findsNothing);
          final action = find.byKey(const Key('home-open-transcript'));
          expect(action.hitTestable(), findsOneWidget);
          expect(tester.getSize(action).height, greaterThanOrEqualTo(48));
          final p = tester.renderObject<RenderParagraph>(find.byKey(const Key('home-transcript-health')));
          final bounds = tester.getRect(find.byKey(const Key('home-transcript-preview-scroll'))).inflate(.5);
          for (final box in p.getBoxesForSelection(const TextSelection(baseOffset: 0, extentOffset: 13))) {
            expect(bounds.contains(p.localToGlobal(Offset(box.left, box.top))), true);
            expect(bounds.contains(p.localToGlobal(Offset(box.right, box.bottom))), true);
          }
          final text = find.text(scale >= 2 ? 'Words' : 'Current transcript');
          final ink = tester.renderObject<RenderParagraph>(find.descendant(of: text, matching: find.byType(RichText)));
          final actionBounds = tester.getRect(action).inflate(.5);
          for (final box in ink.getBoxesForSelection(TextSelection(baseOffset: 0, extentOffset: scale >= 2 ? 5 : 18))) {
            expect(actionBounds.contains(ink.localToGlobal(Offset(box.left, box.top))), true);
            expect(actionBounds.contains(ink.localToGlobal(Offset(box.right, box.bottom))), true);
          }
          await tester.tap(action);
          expect(opens, 1);
          expect(tester.takeException(), isNull);
        });
      }
    }
  }
}
