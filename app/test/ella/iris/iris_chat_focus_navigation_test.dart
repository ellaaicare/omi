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
import 'package:omi/widgets/bottom_nav_bar.dart';
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
      ServerMessage('synthetic', DateTime(2026, 10, 17), 'Fictional paper moon only.', MessageSender.human,
          MessageType.text, null, false, [], [], [],
          askForNps: false),
    ];
  int opens = 0;

  Finder get field => find.byType(TextField);
  ChatPageState chat(WidgetTester tester) => tester.state<ChatPageState>(find.byType(ChatPage, skipOffstage: false));

  Future<void> mount(WidgetTester tester, {required bool dark, required double scale}) async {
    tester.view.physicalSize = const Size(390, 844);
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
        status: 'Necklace audio not confirmed',
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
    for (final scale in [1.0, 3.5]) {
      testWidgets('cold Chat has no autofocus and transcript entry remains visible dark=$dark scale=$scale',
          (tester) async {
        final f = _Fixture();
        await f.mount(tester, dark: dark, scale: scale);
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        expect(find.byKey(const Key('home-open-transcript')).hitTestable(), findsOneWidget);
        expect(f.messages.sends, 0);
        await f.screenshot(tester, 'cold-chat-$dark-$scale');
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });

      testWidgets('Chat Journal Chat retires keyboard and preserves unsent draft dark=$dark scale=$scale',
          (tester) async {
        final f = _Fixture();
        await f.mount(tester, dark: dark, scale: scale);
        await tester.showKeyboard(f.field);
        await tester.enterText(f.field, 'Unsent fictional paper moon');
        await tester.pump();
        expect(f.chat(tester).textFieldFocusNode.hasFocus, true);
        tester.view.viewInsets = const FakeViewPadding(bottom: 310);
        await tester.pump();
        f.home.setIndex(0);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 500));
        expect(find.text('Synthetic Journal — no editor'), findsOneWidget);
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(f.home.isChatFieldFocused, false);
        expect(find.byType(BottomNavBar).hitTestable(), findsOneWidget);
        expect(tester.getRect(find.byType(BottomNavBar)).bottom, lessThanOrEqualTo(844 - 310));
        expect(tester.testTextInput.isVisible, false);
        await f.screenshot(tester, 'journal-after-chat-$dark-$scale');
        tester.view.viewInsets = const FakeViewPadding();
        f.home.setIndex(1);
        await tester.pump();
        await tester.pump(const Duration(seconds: 1));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        expect(f.chat(tester).textController.text, 'Unsent fictional paper moon');
        expect(find.byKey(const Key('home-open-transcript')).hitTestable(), findsOneWidget);
        expect(f.messages.sends, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });

      testWidgets('ordinary route push and pop cannot restore Chat keyboard dark=$dark scale=$scale', (tester) async {
        final f = _Fixture();
        await f.mount(tester, dark: dark, scale: scale);
        await tester.showKeyboard(f.field);
        await tester.enterText(f.field, 'Fictional unsent route draft');
        f.nav.currentState!.push(MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('Synthetic read-only route')),
        ));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 500));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        f.nav.currentState!.pop();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 500));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        expect(f.chat(tester).textController.text, 'Fictional unsent route draft');
        expect(f.messages.sends, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });

      testWidgets('background and resume do not reopen keyboard or send draft dark=$dark scale=$scale', (tester) async {
        final f = _Fixture();
        await f.mount(tester, dark: dark, scale: scale);
        await tester.showKeyboard(f.field);
        await tester.enterText(f.field, 'Fictional retained background draft');
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump();
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump();
        await tester.pump(const Duration(seconds: 1));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        expect(f.chat(tester).textController.text, 'Fictional retained background draft');
        expect(f.messages.sends, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });

      testWidgets('deliberate typing and keyboard dismissal restore existing transcript entry dark=$dark scale=$scale',
          (tester) async {
        final f = _Fixture();
        await f.mount(tester, dark: dark, scale: scale);
        await tester.showKeyboard(f.field);
        tester.view.viewInsets = const FakeViewPadding(bottom: 310);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        final link = find.byKey(const Key('home-open-transcript'));
        // Keep the shipped typing layout unchanged pending the separate visual
        // review; spontaneous launch focus no longer hides this control at rest.
        expect(link, findsNothing);
        await tester.tapAt(const Offset(12, 400));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        tester.view.viewInsets = const FakeViewPadding();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(link.hitTestable(), findsOneWidget);
        expect(tester.getSize(link).height, greaterThanOrEqualTo(48));
        expect(find.text(scale >= 2 ? 'Words' : 'Current transcript'), findsOneWidget);
        await f.screenshot(tester, 'dismissed-keyboard-transcript-$dark-$scale');
        await tester.tap(link);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 500));
        expect(f.opens, 1);
        expect(find.text('Fictional read-only transcript'), findsOneWidget);
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(tester.testTextInput.isVisible, false);
        f.nav.currentState!.pop();
        tester.view.viewInsets = const FakeViewPadding();
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 500));
        expect(f.chat(tester).textFieldFocusNode.hasFocus, false);
        expect(f.messages.sends, 0);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      });
    }
  }
}
