import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'package:omi/stock_capture/pages/conversations/widgets/live_capture_card.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/services.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Messages extends MessageProvider {
  int sends = 0;
  @override
  Future refreshMessages({bool dropdownSelected = false}) async {}
  @override
  Future<void> fetchChatApps() async {}
  @override
  Future sendMessageStreamToServer(String text, {String? localMessageId}) async {
    sends++;
  }
}

class _Fixture {
  final home = HomeProvider();
  final nav = GlobalKey<NavigatorState>();
  final messages = _Messages()
    ..hasCachedMessages = true
    ..messages = [
      ServerMessage('synthetic', DateTime(2026, 10, 17), 'Fictional paper moon only. ' * 100, MessageSender.human,
          MessageType.text, null, false, [], [], [],
          askForNps: false),
    ];
  String status = 'Receiving necklace audio';
  double width = 390;
  int opens = 0;
  int pauses = 0;
  bool withStock = true;

  Finder get field => find.byType(TextField);
  ChatPageState chat(WidgetTester tester) => tester.state<ChatPageState>(find.byType(ChatPage, skipOffstage: false));

  Future<void> mount(WidgetTester tester, {required bool dark, required double scale}) async {
    tester.view.physicalSize = Size(width, 844);
    tester.view.devicePixelRatio = 1;
    tester.view.padding = const FakeViewPadding(top: 47, bottom: 34);
    tester.view.viewPadding = const FakeViewPadding(top: 47, bottom: 34);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPadding);
    addTearDown(tester.view.resetViewPadding);
    addTearDown(tester.view.resetViewInsets);
    EllaCaptureHost.installForTesting(
      homeCaptureDockBuilder: (_) => const SizedBox(width: 48, height: 48),
      homeTranscriptPreviewBuilder: (context) => EllaHomeTranscriptCard(
        status: status,
        stockCapture: withStock
            ? LiveCaptureCard(source: 'omi', status: 'Pendant', compact: true, onPauseToggle: () => pauses++)
            : null,
        lastAudio: 'Last audio: not observed',
        transcription: 'Transcription not confirmed',
        preview: 'Fictional retained words only.',
        compact: MediaQuery.viewInsetsOf(context).bottom > 0,
        onOpen: () {
          opens++;
          showModalBottomSheet<void>(
            context: context,
            builder: (_) => const SafeArea(child: Text('Fictional read-only transcript')),
          );
        },
      ),
    );
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<HomeProvider>.value(value: home),
        ChangeNotifierProvider<MessageProvider>.value(value: messages),
        ChangeNotifierProvider(create: (_) => AppProvider()),
        ChangeNotifierProvider(create: (_) => ConnectivityProvider()),
        ChangeNotifierProvider(create: (_) => VoiceRecorderProvider()),
        ChangeNotifierProvider(create: (_) => ConversationProvider()),
        ChangeNotifierProvider(create: (_) => IntegrationProvider()),
      ],
      child: MaterialApp(
        navigatorKey: nav,
        theme: dark ? ellaDarkThemeData() : ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
          child: child!,
        ),
        home: RepaintBoundary(
          key: const Key('focus-review-render'),
          child: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
            const Center(child: Text('Synthetic Journal — no editor')),
            ChatPage(isPivotBottom: true, draftAuthenticatedUid: () => 'focus-fixture-owner'),
            const SizedBox.shrink(),
            const SizedBox.shrink(),
          ]),
        ),
      ),
    ));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
  }

  Future<void> screenshot(WidgetTester tester, String name) async {
    final output = Platform.environment['ELLA_FOCUS_REVIEW_SNAPSHOTS'];
    if (output == null) return;
    await tester.runAsync(() async {
      await Directory(output).create(recursive: true);
      final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('focus-review-render')));
      final image = await boundary.toImage(pixelRatio: 2);
      final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
      await File('$output/MOCK-$name.png').writeAsBytes(bytes!.buffer.asUint8List());
      image.dispose();
    });
  }
}

void main() {
  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
    await (FontLoader('.SF UI Text')
          ..addFont(File('/System/Library/Fonts/SFNS.ttf').readAsBytes().then(ByteData.sublistView)))
        .load();
    await (FontLoader('InstrumentSerif')
          ..addFont(rootBundle.load('assets/fonts/instrument-serif/InstrumentSerif-Regular.ttf')))
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
              .then(ByteData.sublistView)))
        .load();
    for (final name in ['Manrope', 'Fraunces']) {
      await (FontLoader(name)
            ..addFont(rootBundle
                .load(name == 'Manrope' ? 'assets/fonts/Manrope-400.ttf' : 'assets/fonts/Fraunces-Latin-Regular.ttf')))
          .load();
    }
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'focus-fixture-owner';
  });
  tearDown(EllaCaptureHost.resetForTesting);

  for (final dark in [false, true]) {
    for (final scale in [1.0, 1.35, 3.5]) {
      for (final width in [320.0, 390.0]) {
        for (final status in [
          'Receiving necklace audio',
          'Audio stalled — check necklace',
          'Necklace connected · waiting for audio',
          'Necklace audio not confirmed'
        ]) {
          testWidgets('initial whole header $dark $scale $width $status', (tester) async {
            final f = _Fixture()
              ..width = width
              ..status = status;
            await f.mount(tester, dark: dark, scale: scale);
            final card = find.byType(EllaHomeTranscriptCard);
            final history = find
                .byWidgetPredicate((w) => w is ListView && identical(w.controller, f.chat(tester).scrollController));
            expect(card, findsOneWidget);
            expect(history, findsOneWidget);
            final cardRect = tester.getRect(card), historyRect = tester.getRect(history);
            expect(cardRect.top, greaterThanOrEqualTo(47));
            expect(cardRect.bottom, lessThanOrEqualTo(historyRect.top + .5));
            expect(historyRect.height, greaterThanOrEqualTo(48));
            final health = find.byKey(const Key('home-transcript-health'));
            final paragraph = tester.renderObject<RenderParagraph>(health);
            final viewport = tester.getRect(find.byKey(const Key('home-transcript-preview-scroll')));
            final boxes = paragraph.getBoxesForSelection(TextSelection(baseOffset: 0, extentOffset: status.length));
            expect(boxes, isNotEmpty);
            for (final box in boxes) {
              final top = paragraph.localToGlobal(Offset(box.left, box.top));
              final bottom = paragraph.localToGlobal(Offset(box.right, box.bottom));
              expect(top.dy, greaterThanOrEqualTo(viewport.top - .5));
              expect(bottom.dy, lessThanOrEqualTo(viewport.bottom + .5));
            }
            final open = find.byKey(const Key('home-open-transcript'));
            expect(open.hitTestable(), findsOneWidget);
            expect(tester.getSize(open).height, greaterThanOrEqualTo(48));
            expect(find.byTooltip('Pause').hitTestable(), findsOneWidget);
            if (width == 390 && status == 'Receiving necklace audio' && scale != 1.35) {
              await f.screenshot(tester, 'v5-initial-whole-header-$dark-$scale');
            }
            await tester.tap(open);
            await tester.pumpAndSettle();
            expect(f.opens, 1);
            expect(f.pauses, 0);
            expect(f.messages.sends, 0);
            Navigator.of(tester.element(find.text('Fictional read-only transcript'))).pop();
            await tester.pumpAndSettle();
            expect(tester.takeException(), isNull);
            if (width == 390 && status == 'Receiving necklace audio' && scale != 1.35) {
              await f.screenshot(tester, 'v5-whole-header-$dark-$scale');
            }
            await tester.pumpWidget(const SizedBox.shrink());
            await tester.pump();
          });
        }
      }
    }
  }
  for (final dark in [false, true]) {
    for (final scale in [1.0, 1.35, 3.5]) {
      for (final width in [320.0, 390.0]) {
        testWidgets('idle full-width status and action $dark $scale $width', (tester) async {
          final f = _Fixture()..width = width..status = 'Not recording'..withStock = false;
          await f.mount(tester, dark: dark, scale: scale);
          final history = find.byWidgetPredicate((w) => w is ListView && identical(w.controller, f.chat(tester).scrollController));
          final cardRect = tester.getRect(find.byType(EllaHomeTranscriptCard));
          final historyRect = tester.getRect(history);
          expect(cardRect.bottom, lessThanOrEqualTo(historyRect.top + .5));
          expect(historyRect.height, greaterThanOrEqualTo(48));
          final health = tester.renderObject<RenderParagraph>(find.byKey(const Key('home-transcript-health')));
          final viewport = tester.getRect(find.byKey(const Key('home-transcript-preview-scroll'))).inflate(.5);
          for (final box in health.getBoxesForSelection(const TextSelection(baseOffset: 0, extentOffset: 13))) {
            expect(viewport.contains(health.localToGlobal(Offset(box.left, box.top))), true);
            expect(viewport.contains(health.localToGlobal(Offset(box.right, box.bottom))), true);
          }
          expect(health.getBoxesForSelection(const TextSelection(baseOffset: 4, extentOffset: 13)).length, 1);
          final action = find.byKey(const Key('home-open-transcript'));
          expect(action.hitTestable(), findsOneWidget);
          expect(tester.getSize(action).height, greaterThanOrEqualTo(48));
          if (width == 390 && scale != 1.35) await f.screenshot(tester, 'v10-idle-$dark-$scale');
          await tester.tap(action); await tester.pumpAndSettle();
          expect(f.opens,1);expect(f.pauses,0);expect(f.messages.sends,0);
          Navigator.of(tester.element(find.text('Fictional read-only transcript'))).pop();
          await tester.pumpAndSettle();expect(tester.takeException(),isNull);
          await tester.pumpWidget(const SizedBox.shrink());await tester.pump();
        });
      }
    }
  }

}
