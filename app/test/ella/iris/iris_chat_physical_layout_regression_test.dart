import 'package:omi/stock_capture/pages/conversations/widgets/live_capture_card.dart';
import 'package:omi/ella/upstream_capture/ella_home_transcript_panel.dart';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/chat/page.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/services.dart';

ServerMessage _message(String id, String text, MessageSender sender) => ServerMessage(
      id,
      DateTime(2026, 10, 9, 9, 41),
      text,
      sender,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
      askForNps: false,
    );

class _Messages extends MessageProvider {
  _Messages() : super(historyPaginationEnabled: true);
  @override
  bool get showsEarlierHistoryControl => true;
  @override
  bool get canLoadEarlierHistory => true;
  @override
  Future refreshMessages({bool dropdownSelected = false}) async {}
  @override
  Future<void> fetchChatApps() async {}
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
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader(
      'packages/font_awesome_flutter/FontAwesomeSolid',
    )..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(
            File(
              '${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf',
            ).readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes))),
          ))
        .load();
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    SharedPreferencesUtil().uid = 'keyboard-fixture-owner';
  });

  tearDown(EllaCaptureHost.resetForTesting);

  for (final dark in [false, true]) {
    for (final scale in [1.0, 1.35, 1.75, 3.5]) {
      for (final keyboard in [0.0, 310.0]) {
        for (final check in ['composer', 'history']) {
          testWidgets('physical layout regression $check dark=$dark scale=$scale keyboard=$keyboard', (tester) async {
            tester.view.physicalSize = const Size(390, 844);
            tester.view.devicePixelRatio = 1;
            tester.view.padding = const FakeViewPadding(top: 47, bottom: 34);
            tester.view.viewPadding = const FakeViewPadding(top: 47, bottom: 34);
            addTearDown(tester.view.resetPhysicalSize);
            addTearDown(tester.view.resetDevicePixelRatio);
            addTearDown(tester.view.resetPadding);
            addTearDown(tester.view.resetViewPadding);
            addTearDown(tester.view.resetViewInsets);
            final home = HomeProvider()..setIndex(1);
            final messages = _Messages()
              ..hasCachedMessages = true
              ..messages = List.generate(
                  3,
                  (i) => _message(
                      'fictional-$i',
                      'Fictional layout row $i. ' + List.filled(20, 'Paper moon and blue lantern.').join(' '),
                      MessageSender.human));
            EllaCaptureHost.installForTesting(
                homeTranscriptPreviewBuilder: (context) => EllaHomeTranscriptCard(
                    stockCapture:
                        LiveCaptureCard(source: 'omi', status: 'Receiving audio', compact: true, onPauseToggle: () {}),
                    status: 'Receiving necklace audio',
                    lastAudio: 'Last audio: 9:59 AM',
                    transcription: 'Transcription connected',
                    preview: 'Fictional preview. ' * 50,
                    compact: MediaQuery.viewInsetsOf(context).bottom > 0,
                    onOpen: () {}),
                homeCaptureDockBuilder: (_) => IconButton(onPressed: () {}, icon: const Icon(Icons.settings)));
            await tester.pumpWidget(MultiProvider(
                providers: [
                  ChangeNotifierProvider<HomeProvider>.value(value: home),
                  ChangeNotifierProvider<MessageProvider>.value(value: messages),
                  ChangeNotifierProvider<AppProvider>(create: (_) => AppProvider()),
                  ChangeNotifierProvider<ConnectivityProvider>(create: (_) => ConnectivityProvider()),
                  ChangeNotifierProvider<VoiceRecorderProvider>(create: (_) => VoiceRecorderProvider()),
                  ChangeNotifierProvider<ConversationProvider>(create: (_) => ConversationProvider()),
                  ChangeNotifierProvider<IntegrationProvider>(create: (_) => IntegrationProvider())
                ],
                child: MaterialApp(
                    theme: dark ? ellaDarkThemeData() : ellaThemeData(),
                    localizationsDelegates: AppLocalizations.localizationsDelegates,
                    supportedLocales: AppLocalizations.supportedLocales,
                    builder: (context, child) => RepaintBoundary(
                        key: const Key('layout-repaint'),
                        child: MediaQuery(
                            data: MediaQuery.of(context)
                                .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                            child: child!)),
                    home: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                      const SizedBox.shrink(),
                      ChatPage(isPivotBottom: true, draftAuthenticatedUid: () => 'keyboard-fixture-owner'),
                      const SizedBox.shrink(),
                      const SizedBox.shrink()
                    ]))));
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 400));
            final field = find.byType(TextField);
            await tester.tap(field);
            await tester.enterText(field, 'A\nB');
            tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
            tester.view.padding = const FakeViewPadding(top: 47);
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 400));
            final transcriptButton = find.byKey(const Key('home-open-transcript'));
            if (keyboard == 0) {
              expect(transcriptButton.hitTestable(), findsOneWidget);
              expect(find.byTooltip('Pause').hitTestable(), findsOneWidget);
              final pauseRect = tester.getRect(find.byTooltip('Pause'));
              expect(pauseRect.height, greaterThanOrEqualTo(44));
              expect(pauseRect.bottom, lessThanOrEqualTo(tester.getRect(field).top));
              final rect = tester.getRect(transcriptButton);
              expect(rect.height, greaterThanOrEqualTo(48));
              expect(
                  rect.top, greaterThanOrEqualTo(tester.getRect(find.byKey(const Key('chat-history-older'))).bottom));
              expect(rect.bottom, lessThanOrEqualTo(tester.getRect(field).top));
            } else {
              expect(transcriptButton, findsNothing, reason: 'Do not crowd keyboard/composer with the preview');
            }
            final directory = Platform.environment['ELLA_CHAT_LAYOUT_SCREENSHOTS'];
            if (directory != null) {
              await tester.runAsync(() async {
                final b = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('layout-repaint')));
                final img = await b.toImage();
                final bytes = (await img.toByteData(format: ui.ImageByteFormat.png))!.buffer.asUint8List();
                Directory(directory).createSync(recursive: true);
                File('$directory/$check-${dark ? 'dark' : 'light'}-$scale-$keyboard.png').writeAsBytesSync(bytes);
                img.dispose();
              });
            }
            if (check == 'composer') {
              final editor =
                  tester.state<EditableTextState>(find.descendant(of: field, matching: find.byType(EditableText)));
              final text = editor.widget.controller.text;
              expect(text, 'A\nB');
              final first = editor.renderEditable.getLocalRectForCaret(const TextPosition(offset: 0));
              final last = editor.renderEditable.getLocalRectForCaret(TextPosition(offset: text.length));
              expect(first.top, greaterThanOrEqualTo(-1),
                  reason: 'both short draft lines fit; first is not partially clipped');
              expect(first.bottom, lessThanOrEqualTo(editor.renderEditable.size.height + 1));
              expect(last.top, greaterThanOrEqualTo(-1));
              expect(last.bottom, lessThanOrEqualTo(editor.renderEditable.size.height + 1));
              expect(tester.getRect(find.byType(ListView).first).height, greaterThan(30));
              expect(tester.widget<TextField>(field).controller!.text, text, reason: 'layout never edits the draft');
            } else {
              final history = tester.getRect(find.byType(ListView).first);
              final older = tester.getRect(find.byKey(const Key('ella-load-earlier-history')));
              expect(history.top, greaterThanOrEqualTo(older.bottom),
                  reason: 'history viewport clears the actual paging control, not fixed row assumption');
              final chat = tester.state<ChatPageState>(find.byType(ChatPage));
              final before = chat.scrollController.offset;
              await tester.tap(find.byKey(const Key('chat-history-older')));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 400));
              expect(chat.scrollController.offset, greaterThan(before),
                  reason: 'ordinary older arrow moves oversized history');
              chat.scrollController.jumpTo(chat.scrollController.position.maxScrollExtent);
              await tester.pump();
              final firstMessage = find.byWidgetPredicate(
                  (widget) => widget is SelectableText && widget.data?.startsWith('Fictional layout row 0.') == true);
              expect(firstMessage, findsOneWidget);
              final firstEditor = tester
                  .state<EditableTextState>(find.descendant(of: firstMessage, matching: find.byType(EditableText)));
              final firstGlyph = firstEditor.renderEditable
                  .getLocalRectForCaret(const TextPosition(offset: 0))
                  .shift(firstEditor.renderEditable.localToGlobal(Offset.zero));
              expect(firstGlyph.top, greaterThanOrEqualTo(history.top),
                  reason: 'oldest first line clears paging region at start of history');
              expect(firstGlyph.bottom, lessThanOrEqualTo(history.bottom));
            }
            expect(tester.takeException(), isNull);
            expect(find.byKey(const Key('chat-composer-send')).hitTestable(), findsOneWidget);
          });
        }
      }
    }
  }
}
