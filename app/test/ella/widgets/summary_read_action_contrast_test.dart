import 'dart:io';
import 'dart:math' as math;
import 'dart:ui' show ImageByteFormat;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/summary_read_freshness.dart';
import 'package:omi/ella/widgets/summary_refresh_notice.dart';

// Inspect rendered paragraph ink and the actual Material fill, not just a
// declared style or Text finder (the invisible956 control passed those).
double contrast(Color a, Color b) =>
    (math.max(a.computeLuminance(), b.computeLuminance()) + .05) /
    (math.min(a.computeLuminance(), b.computeLuminance()) + .05);

Future<void> visible(WidgetTester tester, Finder action) async {
  for (var i = 0; i < 20 && action.hitTestable().evaluate().isEmpty; i++) {
    await tester.drag(find.byType(SingleChildScrollView), const Offset(0, -80));
    await tester.pump();
  }
  expect(action.hitTestable(), findsOneWidget);
}

void checkPaint(WidgetTester tester, Finder action, String label) {
  final text = find.descendant(of: action, matching: find.text(label));
  final paragraph = tester.renderObject<RenderParagraph>(find.descendant(of: text, matching: find.byType(RichText)));
  final material = tester.widget<Material>(find.descendant(of: action, matching: find.byType(Material)).first);
  final page = Theme.of(tester.element(action)).scaffoldBackgroundColor;
  final fill = Color.alphaBlend(material.color ?? Colors.transparent, page);
  final ink = paragraph.text.style!.color!;
  expect(ink.a, 1);
  expect(fill.a, 1);
  expect(contrast(ink, fill), greaterThanOrEqualTo(4.5));
  expect(paragraph.didExceedMaxLines, false);
  final bounds = tester.getRect(action).inflate(.5);
  expect(bounds.height, greaterThanOrEqualTo(48));
  for (final box in paragraph.getBoxesForSelection(TextSelection(baseOffset: 0, extentOffset: label.length))) {
    expect(bounds.contains(paragraph.localToGlobal(Offset(box.left, box.top))), true);
    expect(bounds.contains(paragraph.localToGlobal(Offset(box.right, box.bottom))), true);
  }
}

Future<void> screenshot(WidgetTester tester, String name) async {
  final out = Platform.environment['ELLA_REFRESH_MOCKS'];
  if (out == null) return;
  await tester.runAsync(() async {
    await Directory(out).create(recursive: true);
    final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('refresh-mock')));
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ImageByteFormat.png);
    await File('$out/MOCK-$name.png').writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  setUpAll(() async {
    await (FontLoader('.SF UI Text')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf')))
        .load();
  });
  for (final dark in [false, true]) {
    testWidgets('real notice painted Refresh label contrasts against actual page dark=$dark', (tester) async {
      await tester.pumpWidget(MaterialApp(
        theme: dark ? ellaDarkThemeData() : ellaThemeData(),
        home: Scaffold(
            body: SummaryRefreshNotice(
          observation: const SummaryReadObservation(SummaryReadPhase.checked),
          onRefresh: () {},
        )),
      ));
      await tester.pump(const Duration(milliseconds: 250));
      final action = find
          .ancestor(
              of: find.text('Refresh summary'),
              matching: find.byWidgetPredicate((widget) => widget is ButtonStyleButton))
          .first;
      checkPaint(tester, action, 'Refresh summary');
    });
  }
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      for (final phase in SummaryReadPhase.values) {
        testWidgets('painted read action contrast and geometry $dark $scale $phase', (tester) async {
          tester.view.physicalSize = const Size(390, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          var reads = 0;
          await tester.pumpWidget(MaterialApp(
            theme: dark ? ellaDarkThemeData() : ellaThemeData(),
            home: RepaintBoundary(
              key: const Key('refresh-mock'),
              child: Scaffold(
                  body: MediaQuery(
                data: MediaQueryData(size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
                child: SingleChildScrollView(
                    child: Padding(
                  padding: const EdgeInsets.all(20),
                  child: SummaryRefreshNotice(
                    observation: SummaryReadObservation(phase),
                    onRefresh: () => reads++,
                  ),
                )),
              )),
            ),
          ));
          await tester.pump(const Duration(milliseconds: 250));
          final action = find.byKey(const Key('summary-refresh-action'));
          await visible(tester, action);
          final label = phase == SummaryReadPhase.checking ? 'Checking summary…' : 'Refresh summary';
          checkPaint(tester, action, label);
          expect(tester.widget<FilledButton>(action).onPressed == null, phase == SummaryReadPhase.checking);
          await screenshot(tester, 'read-action-$dark-$scale-${phase.name}');
          await tester.tap(action);
          expect(reads, phase == SummaryReadPhase.checking ? 0 : 1);
          expect(tester.takeException(), null);
        });
      }
    }
  }
  testWidgets('pending read disables repeat and settles back to explicit retry without auto read', (tester) async {
    var phase = SummaryReadPhase.checked;
    var reads = 0;
    late StateSetter update;
    await tester.pumpWidget(MaterialApp(
        theme: ellaDarkThemeData(),
        home: Scaffold(
          body: StatefulBuilder(builder: (context, setState) {
            update = setState;
            return SummaryRefreshNotice(
                observation: SummaryReadObservation(phase),
                onRefresh: () {
                  reads++;
                  setState(() => phase = SummaryReadPhase.checking);
                });
          }),
        )));
    final action = find.byKey(const Key('summary-refresh-action'));
    await tester.tap(action);
    await tester.pump(const Duration(milliseconds: 250));
    expect(reads, 1);
    expect(tester.widget<FilledButton>(action).onPressed, null);
    await tester.tap(action);
    expect(reads, 1);
    update(() => phase = SummaryReadPhase.stale);
    await tester.pump(const Duration(milliseconds: 250));
    expect(find.text('Refresh summary'), findsOneWidget);
    expect(reads, 1);
    await tester.tap(action);
    expect(reads, 2);
  });
  testWidgets('no refresh authority keeps action absent and performs no work', (tester) async {
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        home: const Scaffold(
          body: SummaryRefreshNotice(observation: SummaryReadObservation(SummaryReadPhase.checked)),
        )));
    expect(find.byKey(const Key('summary-refresh-action')), findsNothing);
  });
  testWidgets('956 inherited unstyled button really paints matching page ink', (tester) async {
    await tester.pumpWidget(MaterialApp(
        theme: ellaDarkThemeData(),
        home: Scaffold(
          body: TextButton(onPressed: () {}, child: const Text('Original refresh control')),
        )));
    final paragraph = tester.renderObject<RenderParagraph>(
        find.descendant(of: find.text('Original refresh control'), matching: find.byType(RichText)));
    final scaffold = tester.element(find.byType(Scaffold));
    expect(contrast(paragraph.text.style!.color!, Theme.of(scaffold).scaffoldBackgroundColor), 1);
  });
}
