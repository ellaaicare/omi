import 'dart:io';
import 'dart:math' as math;
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/quiet_memory_preview.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    await (FontLoader(EllaTextStyles.uiFont)
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('MaterialIcons')..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'))).load();
  });
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('actual memory popup contrast and action dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        var opened = 0;
        final boundary = GlobalKey();
        final record = ServerConversation(
          id: 'fictional-popup',
          createdAt: DateTime(2026),
          structured: Structured('Fictional', ''),
          transcriptSegments: [],
        );
        await tester.pumpWidget(MaterialApp(
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)),
            child: RepaintBoundary(key: boundary, child: child!),
          ),
          home: Scaffold(
              body: QuietMemoryPreview(
            conversation: record,
            title: 'Fictional',
            onClose: () {},
            onFullConversation: () => opened++,
          )),
        ));
        await tester.pumpAndSettle();
        final more = find.byKey(const Key('iris-memory-more'));
        for (var i = 0; i < 5 && more.hitTestable().evaluate().isEmpty; i++) {
          await tester.drag(find.byType(SingleChildScrollView).first, const Offset(0, -200));
          await tester.pumpAndSettle();
        }
        await tester.tap(more.hitTestable());
        await tester.pumpAndSettle();
        final label = find.text('Full conversation');
        expect(label, findsOneWidget);
        final rich = find.descendant(of: label, matching: find.byType(RichText));
        final paragraph = tester.renderObject<RenderParagraph>(rich);
        final fg = (paragraph.text as TextSpan).style!.color!;
        RenderObject? surface = paragraph;
        while (surface != null && surface is! RenderPhysicalModel && surface is! RenderPhysicalShape) {
          surface = surface.parent;
        }
        final bg = switch (surface) {
          RenderPhysicalModel model => model.color,
          RenderPhysicalShape shape => shape.color,
          _ => throw StateError('No actual painted popup surface'),
        };
        final contrast = (math.max(fg.computeLuminance(), bg.computeLuminance()) + .05) /
            (math.min(fg.computeLuminance(), bg.computeLuminance()) + .05);
        print('ACTUAL_POPUP dark=$dark scale=$scale foreground=$fg surface=$bg ratio=$contrast');
        expect(contrast, greaterThanOrEqualTo(4.5));
        expect(tester.getSize(find.byType(PopupMenuItem<String>)).height, greaterThanOrEqualTo(48));
        final semantics = tester.ensureSemantics();
        expect(tester.getSemantics(label).label, contains('Full conversation'));
        final directory = Platform.environment['ELLA_POPUP_SCREENSHOTS'];
        if (directory != null) {
          await tester.runAsync(() async {
            final render = boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
            final image = await render.toImage();
            final data = await image.toByteData(format: ui.ImageByteFormat.png);
            await File('$directory/popup-$dark-$scale.png').writeAsBytes(data!.buffer.asUint8List());
            image.dispose();
          });
        }
        await tester.tap(label.hitTestable());
        await tester.pumpAndSettle();
        expect(opened, 1);
        expect(find.byType(PopupMenuItem<String>), findsNothing);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
        semantics.dispose();
      });
    }
  }
}
