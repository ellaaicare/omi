import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_appearance.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/folder_provider.dart';

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
  for (final night in [0.0, 0.5, 1.0]) {
    for (final scale in [1.0, 3.5]) {
      for (final initial in [1]) {
        testWidgets('actual detail page section agrees with initial=$initial at ${scale}x night=$night',
            (tester) async {
          SharedPreferences.setMockInitialValues({'demoMode': true});
          await SharedPreferencesUtil.init();
          tester.view.physicalSize = const Size(320, 844);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final memory = ServerConversation(
            id: 'source-fixture',
            createdAt: DateTime(2026, 10, 9),
            structured: (Structured('Test memory', 'Maya brings the blue folder at 4 PM.')
              ..actionItems = [ActionItem('Bring the folder')]),
            transcriptSegments: [
              TranscriptSegment(
                  id: 'segment',
                  text: 'Recorded original words',
                  speaker: 'SPEAKER_0',
                  isUser: false,
                  personId: null,
                  translations: [],
                  start: 0,
                  end: 1)
            ],
            audioFiles: [
              AudioFile(
                  id: 'audio', uid: 'owner-fixture', conversationId: 'source-fixture', chunkTimestamps: [], duration: 1)
            ],
          );
          final boundary = GlobalKey();
          final detail = ConversationDetailProvider()
            ..selectedDate = DateTime(2026, 10, 9)
            ..setCachedConversation(memory);
          final conversations = ConversationProvider();
          final folders = FolderProvider();
          addTearDown(detail.dispose);
          addTearDown(conversations.dispose);
          addTearDown(folders.dispose);
          await tester.pumpWidget(MultiProvider(
              providers: [
                ChangeNotifierProvider<ConversationDetailProvider>.value(value: detail),
                ChangeNotifierProvider<ConversationProvider>.value(value: conversations),
                ChangeNotifierProvider<FolderProvider>.value(value: folders),
              ],
              child: RepaintBoundary(
                  key: boundary,
                  child: MaterialApp(
                    debugShowCheckedModeBanner: false,
                    theme: ThemeData.lerp(ellaThemeData(), ellaDarkThemeData(), night)
                        .copyWith(extensions: [EllaV7Tone(night)]),
                    locale: const Locale('en'),
                    localizationsDelegates: AppLocalizations.localizationsDelegates,
                    supportedLocales: AppLocalizations.supportedLocales,
                    builder: (context, child) => MediaQuery(
                        data: MediaQuery.of(context)
                            .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                        child: child!),
                    home: ConversationDetailPage(conversation: memory, initialTabIndex: initial),
                  ))));
          await tester.pump();
          await tester.pump();
          final bg = tester.widget<Scaffold>(find.byType(Scaffold).first).backgroundColor!;
          double contrast(Color ink, Color paper) {
            final a = ink.computeLuminance(), b = paper.computeLuminance();
            return (a > b ? (a + .05) / (b + .05) : (b + .05) / (a + .05));
          }

          final header = tester.widget<Text>(find.text('Memory and source'));
          expect(contrast(header.style!.color!, bg), greaterThanOrEqualTo(4.5), reason: 'actual route header');
          if (initial == 1) {
            await tester.scrollUntilVisible(find.byType(TextField), 180,
                scrollable: find.byType(Scrollable).first, maxScrolls: 25);
            final title = tester.widget<TextField>(find.byType(TextField).first);
            expect(contrast(title.style!.color!, bg), greaterThanOrEqualTo(4.5), reason: 'actual editable title');
            await tester.scrollUntilVisible(find.byType(MarkdownBody), 180,
                scrollable: find.byType(Scrollable).first, maxScrolls: 25);
            expect(find.byType(MarkdownBody), findsWidgets);
            for (final body in tester.widgetList<MarkdownBody>(find.byType(MarkdownBody))) {
              expect(contrast(body.styleSheet!.p!.color!, bg), greaterThanOrEqualTo(4.5),
                  reason: 'actual summary body');
            }
          }
          expect(tester.takeException(), isNull);
          await tester.runAsync(() async {
            final renderer = boundary.currentContext!.findRenderObject() as RenderRepaintBoundary;
            final image = await renderer.toImage(pixelRatio: 1);
            final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
            final renderFile = File('../iris-renders/memory-detail-$initial-${scale}x-night$night.png');
            await renderFile.parent.create(recursive: true);
            await renderFile.writeAsBytes(bytes!.buffer.asUint8List());
            image.dispose();
          });
          expect(find.byTooltip('Back'), findsOneWidget);
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox.shrink());
          await tester.pump();
        });
      }
    }
  }
}
