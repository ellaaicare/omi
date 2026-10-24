import 'dart:io';
import 'dart:ui' show ImageByteFormat;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/stock_capture/pages/conversations/widgets/live_capture_card.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('.SF UI Text')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf')))
        .load();
  });

  for (final status in [
    'Necklace connected · waiting for audio',
    'Necklace audio not confirmed',
    'Receiving necklace audio',
    'Audio stalled — check necklace',
  ]) {
    for (final dark in [false, true]) {
      for (final scale in [1.0, 3.5]) {
        testWidgets('complete Home status $status dark=$dark scale=$scale', (tester) async {
          tester.view.physicalSize = const Size(390, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          var pauses = 0;
          var opens = 0;
          await tester.pumpWidget(
            MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: MediaQuery(
                data: MediaQueryData(size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
                child: Scaffold(
                  body: Center(
                    child: RepaintBoundary(
                      key: const Key('complete-status-render'),
                      child: SizedBox(
                        width: 390,
                        child: EllaHomeTranscriptCard(
                          status: status,
                          lastAudio: 'Last audio: not observed',
                          transcription: 'Transcription not confirmed',
                          preview: 'Fictional words only',
                          onOpen: () => opens++,
                          stockCapture: LiveCaptureCard(
                            source: 'omi',
                            status: 'Pendant',
                            compact: true,
                            onPauseToggle: () => pauses++,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          );
          await tester.pump();
          final pause = find.byTooltip('Pause');
          expect(pause.hitTestable(), findsOneWidget);
          expect(tester.getSize(pause).height, greaterThanOrEqualTo(44));
          await tester.tap(pause);
          expect(pauses, 1);
          final open = find.byKey(const Key('home-open-transcript'));
          expect(open.hitTestable(), findsOneWidget);
          expect(tester.getSize(open).height, greaterThanOrEqualTo(48));
          expect(find.text(scale >= 2 ? 'Words' : 'Current transcript'), findsOneWidget);
          final health = find.byKey(const Key('home-transcript-health'));
          final text = tester.widget<Text>(health);
          expect(text.data, status);
          expect(text.maxLines, null);
          expect(text.softWrap, true);
          expect(text.overflow, TextOverflow.visible);
          final paragraph = tester.renderObject<RenderParagraph>(health);
          expect(paragraph.didExceedMaxLines, false);
          final boxes = paragraph.getBoxesForSelection(TextSelection(baseOffset: 0, extentOffset: status.length));
          expect(boxes, isNotEmpty);
          final scroll = find.byKey(const Key('home-transcript-preview-scroll'));
          bool finalLineVisible() {
            final viewport = tester.getRect(scroll);
            final box = boxes.last;
            final top = paragraph.localToGlobal(Offset(box.left, box.top));
            final bottom = paragraph.localToGlobal(Offset(box.right, box.bottom));
            return top.dy >= viewport.top && bottom.dy <= viewport.bottom;
          }

          // Ordinary bounded drags must reveal the whole final line, including
          // the distinguishing suffix. Merely finding offscreen text is not proof.
          for (var i = 0; i < 100 && !finalLineVisible(); i++) {
            await tester.drag(scroll, const Offset(0, -16));
            await tester.pump();
          }
          expect(finalLineVisible(), true);
          expect(paragraph.didExceedMaxLines, false);
          expect(open.hitTestable(), findsOneWidget);
          await tester.tap(open);
          expect(opens, 1);
          expect(pauses, 1);
          expect(tester.takeException(), null);
          final output = Platform.environment['ELLA_HOME_STATUS_SNAPSHOTS'];
          if (output != null) {
            await tester.runAsync(() async {
              await Directory(output).create(recursive: true);
              final boundary = tester.renderObject<RenderRepaintBoundary>(
                find.byKey(const Key('complete-status-render')),
              );
              final image = await boundary.toImage(pixelRatio: 2);
              final data = await image.toByteData(format: ImageByteFormat.png);
              final name = status.replaceAll(RegExp(r'[^a-zA-Z0-9]+'), '-');
              await File('$output/$name-$dark-$scale.png').writeAsBytes(data!.buffer.asUint8List());
              image.dispose();
            });
          }
        });
      }
    }
  }
}
