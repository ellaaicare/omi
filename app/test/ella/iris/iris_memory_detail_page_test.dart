import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
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
  for (final scale in [1.0, 3.0]) {
    for (final initial in [0, 1, 2]) {
      testWidgets('actual detail page section agrees with initial=$initial at ${scale}x', (tester) async {
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
                  theme: ellaThemeData(),
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
        final label = ['Original words', 'Ella’s summary', 'Action Items'][initial];
        expect(find.descendant(of: find.byKey(const Key('iris-memory-section-menu')), matching: find.text(label)),
            findsOneWidget);
        expect(tester.takeException(), isNull);
        if (initial == 0) {
          expect(find.textContaining('Recorded original words', findRichText: true), findsWidgets);
        }
        await tester.runAsync(() async {
          final renderer = boundary.currentContext!.findRenderObject() as RenderRepaintBoundary;
          final image = await renderer.toImage(pixelRatio: 1);
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final renderFile = File('../iris-renders/memory-detail-$initial-${scale}x.png');
          await renderFile.parent.create(recursive: true);
          await renderFile.writeAsBytes(bytes!.buffer.asUint8List());
          image.dispose();
        });
        final controls = find.byKey(const Key('iris-memory-controls'));
        expect(controls, findsOneWidget);
        await tester.ensureVisible(controls);
        await tester.tap(controls);
        await tester.pumpAndSettle();
        expect(find.byTooltip('Star Conversation'), findsOneWidget);
        expect(find.byTooltip('Share'), findsOneWidget);
        if (initial != 2) expect(find.byTooltip('Search'), findsOneWidget);
        expect(find.byTooltip('Memory controls'), findsOneWidget);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });
    }
  }
}
