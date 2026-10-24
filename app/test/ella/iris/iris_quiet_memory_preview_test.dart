import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeRegular')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Regular-400.otf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeSolid')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
  });

  for (final scale in [1.0, 3.5]) {
    testWidgets('exact retained overview and labeled source peek at $scale', (tester) async {
      tester.view.physicalSize = const Size(320, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final record = ServerConversation(
          id: 'offline-record',
          createdAt: DateTime(2026, 10, 10),
          structured: Structured('Exact retained title', 'Exact retained overview. No new generated facts.'),
          transcriptSegments: List.generate(
              4,
              (i) => TranscriptSegment(
                  id: '$i',
                  text: 'Original segment $i',
                  speaker: 'SPEAKER_$i',
                  isUser: i == 0,
                  personId: null,
                  translations: [],
                  start: i.toDouble(),
                  end: i + 1.0)));
      final boundary = GlobalKey();
      var current = true;
      var fullCalls = 0;
      await tester.pumpWidget(MaterialApp(
          theme: ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) =>
              MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)), child: child!),
          home: Scaffold(
              body: RepaintBoundary(
                  key: boundary,
                  child: ColoredBox(
                      color: ellaThemeData().scaffoldBackgroundColor,
                      child: QuietMemoryAuthorityFence(
                          isCurrent: () => current,
                          child: QuietMemoryPreview(
                              conversation: record,
                              title: record.structured.title,
                              onClose: () {},
                              onFullConversation: () {
                                if (current) fullCalls++;
                              })))))));
      await tester.pump();
      expect(find.text(record.structured.overview), findsOneWidget);
      expect(find.text('Original words · first 3 segments'), findsOneWidget);
      expect(find.text('You'), findsOneWidget);
      expect(find.text('Speaker 1'), findsOneWidget);
      expect(find.text('Original segment 3'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(find.byKey(const Key('iris-memory-more')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('iris-memory-more')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Full conversation'));
      await tester.pumpAndSettle();
      expect(fullCalls, 1);
      await tester.runAsync(() async {
        final render = boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
        final image = await render.toImage();
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        await File('/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-v61-memory-preview-scale-$scale.png')
            .writeAsBytes(bytes!.buffer.asUint8List());
        image.dispose();
      });
      current = false;
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.text(record.structured.overview), findsNothing);
      expect(find.byKey(const Key('iris-memory-more')), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    });
  }
  testWidgets('no transcript or overview creates no invented source text', (tester) async {
    final record = ServerConversation(
        id: 'empty', createdAt: DateTime(2026), structured: Structured('Retained title', ''), transcriptSegments: []);
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
            body: QuietMemoryPreview(
                conversation: record, title: 'Retained title', onClose: () {}, onFullConversation: () {}))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('iris-memory-retained-overview')), findsNothing);
    expect(find.text('Original words · first 3 segments'), findsNothing);
    expect(find.text('Retained title'), findsOneWidget);
  });
}
