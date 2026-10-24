import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/pages/ella_settings_page.dart';
import 'package:omi/ella/widgets/ella_voice_orb.dart';
import 'package:omi/pages/chat/widgets/markdown_message_widget.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/ella/pages/ella_memories_page.dart';
import 'package:omi/ella/widgets/memory_artwork_image.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
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
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  testWidgets('long display word keeps semantics and horizontal remainder reachable at3x', (tester) async {
    final semantics = tester.ensureSemantics();

    const title = 'Memories';
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        home: const MediaQuery(
            data: MediaQueryData(textScaler: TextScaler.linear(3)),
            child: Scaffold(
                body: Align(
                    alignment: Alignment.topLeft,
                    child: SizedBox(width: 80, child: EllaDisplayHeading(title, fontSize: 20)))))));
    final paragraph = tester.renderObject<RenderParagraph>(find.byType(RichText));
    final boxes = paragraph.getBoxesForSelection(const TextSelection(baseOffset: 0, extentOffset: 8));
    expect(boxes.map((box) => box.top).toSet(), hasLength(1));
    expect(tester.getSemantics(find.text(title)).getSemanticsData().label, title);
    final scroll = tester.state<ScrollableState>(find.byType(Scrollable));
    expect(scroll.position.maxScrollExtent, greaterThan(0));
    await tester.drag(find.byType(SingleChildScrollView), const Offset(-500, 0));
    await tester.pumpAndSettle();
    expect(scroll.position.pixels, scroll.position.maxScrollExtent);
    final last = paragraph.getBoxesForSelection(const TextSelection(baseOffset: 7, extentOffset: 8)).last;
    final edge = paragraph.localToGlobal(Offset(last.right, last.bottom));
    expect(edge.dx, lessThanOrEqualTo(tester.getRect(find.byType(SingleChildScrollView)).right + 1));
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });
  for (final dark in [false, true]) {
    test('Iris ${dark ? 'dark' : 'light'} text and controls meet contrast', () {
      final p = EllaPalette(dark);
      double contrast(Color a, Color b) {
        final l = [a.computeLuminance(), b.computeLuminance()]..sort();
        return (l.last + .05) / (l.first + .05);
      }

      expect(contrast(p.ink, p.elevatedCard), greaterThanOrEqualTo(4.5));
      expect(contrast(p.inkSoft, p.card), greaterThanOrEqualTo(4.5));
      expect(contrast(p.tealDeep, p.cardDeep), greaterThanOrEqualTo(4.5));
    });
    for (final scale in [1.0, 3.0]) {
      testWidgets('Iris actual Talk Settings Chat ${dark ? 'dark' : 'light'} at ${scale}x', (tester) async {
        tester.view.physicalSize = scale == 3 ? const Size(320, 568) : const Size(390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final device = DeviceProvider(automaticallyReconnectOnReady: false);
        final user = UserProvider();
        addTearDown(device.dispose);
        addTearDown(user.dispose);
        for (final surface in ['talk', 'settings', 'chat']) {
          final boundary = GlobalKey();
          await tester.pumpWidget(MultiProvider(
            providers: [
              ChangeNotifierProvider<DeviceProvider>.value(value: device),
              ChangeNotifierProvider<UserProvider>.value(value: user)
            ],
            child: MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              home: Builder(
                  builder: (context) => MediaQuery(
                      data: MediaQuery.of(context)
                          .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                      child: RepaintBoundary(
                        key: boundary,
                        child: surface == 'talk'
                            ? EllaVoiceChatPage(
                                demoState: EllaVoiceDemoState(quota: EllaAccessDemoFixtures.softDaily.quota))
                            : surface == 'settings'
                                ? const EllaSettingsPage(runtimeSideEffectsEnabled: false, authenticatedUidOverride: '')
                                : Builder(
                                    builder: (context) => Scaffold(
                                        body: SafeArea(
                                            child: SingleChildScrollView(
                                                padding: const EdgeInsets.all(20),
                                                child: getMarkdownWidget(context,
                                                    'Your memory is available.\n\nOriginal words remain available.'))))),
                      ))),
            ),
          ));
          await tester.pump();
          expect(tester.takeException(), isNull, reason: '$surface $scale $dark');
          if (surface == 'talk') {
            expect(find.text('Demo preview — voice is not active'), findsOneWidget);
            final icon =
                tester.widget<Icon>(find.descendant(of: find.byType(EllaVoiceOrb), matching: find.byType(Icon)).first);
            expect(icon.color, dark ? const EllaPalette(true).paper : Colors.white);
            await tester.ensureVisible(find.text('Demo preview — voice is not active'));
            await tester.pump();
          }
          if (surface == 'settings') {
            final consent = find.text('Privacy & data');
            await tester.scrollUntilVisible(consent, 120);
            await tester.pump();
            expect(consent.hitTestable(), findsOneWidget);
          }
          await tester.runAsync(() async {
            final image = await (boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary).toImage();
            final png = await image.toByteData(format: ui.ImageByteFormat.png);
            final dir = Directory('../iris-accessibility-renders')..createSync(recursive: true);
            File('${dir.path}/$surface-${dark ? 'dark' : 'light'}-${scale}x.png')
                .writeAsBytesSync(png!.buffer.asUint8List());
            image.dispose();
          });
          await tester.pumpWidget(const SizedBox());
        }
      });
    }
    for (final scale in [1.0, 2.0, 3.0]) {
      testWidgets('Iris ${dark ? 'dark' : 'light'} memory and navigation at ${scale}x', (tester) async {
        tester.view.physicalSize = Size(scale == 3 ? 320 : 390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final home = HomeProvider()..setIndex(0);
        addTearDown(home.dispose);
        final boundary = GlobalKey();
        var opened = 0;
        final memory = ServerConversation(
            id: 'iris-text-memory',
            createdAt: DateTime(2026, 10, 7, 9, 41),
            structured: Structured('Garden on Saturday morning',
                'You and Jordan talked about the garden. Original words and memory details remain available.'));
        final scene = Builder(builder: (context) {
          final cardList = Expanded(
              child: SafeArea(
                  child: ListView(padding: const EdgeInsets.all(16), children: [
            const EllaDisplayHeading('Memories', fontSize: 20),
            const SizedBox(height: 16),
            MemoryGalleryCard(conversation: memory, layout: MemoryGalleryLayout.journal, onOpen: () => opened++),
          ])));
          return RepaintBoundary(
              key: boundary,
              child: Scaffold(
                body: EllaIrisBackdrop(
                    child: Row(children: [
                  if (EllaNavigationRail.fits(context)) EllaNavigationRail(onTabTap: (i, repeat) => home.setIndex(i)),
                  cardList,
                ])),
                bottomNavigationBar: !EllaNavigationRail.fits(context)
                    ? SizedBox(
                        height: BottomNavBar.navigationHeight(context),
                        child: Stack(children: [
                          BottomNavBar(onTabTap: (i, repeat) => home.setIndex(i)),
                        ]))
                    : null,
              ));
        });
        await tester.pumpWidget(ChangeNotifierProvider.value(
          value: home,
          child: MaterialApp(
            theme: dark ? ellaDarkThemeData() : ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: Builder(
                builder: (context) => MediaQuery(
                      data: MediaQuery.of(context)
                          .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                      child: EllaRailScope(enabled: scale < 1.28 && EllaNavigationRail.fits(context), child: scene),
                    )),
          ),
        ));
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
        expect(tester.getSize(find.byType(MemoryArtworkImage)), Size.zero);
        expect(find.text('Garden on Saturday morning'), findsOneWidget);
        final heading = tester.renderObject<RenderParagraph>(
            find.descendant(of: find.byType(EllaDisplayHeading), matching: find.byType(RichText)));
        final boxes = heading.getBoxesForSelection(const TextSelection(baseOffset: 0, extentOffset: 8));
        expect(boxes.map((box) => box.top).toSet(), hasLength(1), reason: 'Memories remains one complete word');
        await tester.tap(find.text('Garden on Saturday morning'));
        expect(opened, 1);
        if (scale == 1) {
          final chat = find.text('Chat');
          if (chat.evaluate().isEmpty) {
            await tester.tap(find.text('Menu'));
            await tester.pumpAndSettle();
          }
          await tester.tap(find.text('Chat'));
          await tester.pump();
          expect(home.selectedIndex, 1);
        }
        final render = boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
        await tester.runAsync(() async {
          final image = await render.toImage();
          final png = await image.toByteData(format: ui.ImageByteFormat.png);
          final dir = Directory('../iris-accessibility-renders')..createSync(recursive: true);
          File('${dir.path}/${dark ? 'dark' : 'light'}-${scale}x.png').writeAsBytesSync(png!.buffer.asUint8List());
          image.dispose();
        });
      });
    }
  }
}
