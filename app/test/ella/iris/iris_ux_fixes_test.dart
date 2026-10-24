import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
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
import 'package:omi/pages/chat/widgets/ai_message.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/integration_provider.dart';
import 'package:omi/providers/voice_recorder_provider.dart';
import 'package:omi/services/services.dart';

ServerMessage _message(String id, String text, MessageSender sender) =>
    ServerMessage(id, DateTime(2026, 10, 9, 9, 41), text, sender, MessageType.text, null, false, [], [], [],
        askForNps: false);

class _Messages extends MessageProvider {
  @override
  Future refreshMessages({bool dropdownSelected = false}) async {}
}

void main() {
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
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  test('only leading generated assistant label is presentation metadata', () {
    for (final text in ['A quote says [Ella] in the middle.', '"[Ella] quoted words"', '[Other] source label']) {
      final message = _message('quoted', text, MessageSender.ai);
      final widget = buildMessageWidget(message, (_) {}, false, false, null, (_) {}, (int value, {String? reason}) {});
      expect((widget as NormalMessageWidget).messageText, text);
      expect(message.text, text);
    }
  });
  for (final dark in [false, true]) {
    test('user bubble separate tokens meet contrast ${dark ? 'dark' : 'light'}', () {
      final palette = EllaPalette(dark);
      final luminances = [palette.userBubbleFill.computeLuminance(), palette.userBubbleInk.computeLuminance()]..sort();
      expect((luminances.last + .05) / (luminances.first + .05), greaterThanOrEqualTo(4.5));
      expect(palette.tealDeep, dark ? const Color(0xFFB4C6FF) : EllaColors.tealDeep);
    });
    for (final scale in [1.0, 1.27, 1.28, 1.35, 3.0]) {
      testWidgets('actual Chat source render ${dark ? 'dark' : 'light'} ${scale}x', (tester) async {
        tester.view.physicalSize = Size(scale == 3 ? 320 : 390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final home = HomeProvider()..setIndex(1);
        final messages = _Messages()
          ..messages = [
            _message('ai', '[Ella] We can take this one step at a time. Your original words remain available.',
                MessageSender.ai),
            _message('human', '[Ella] is a label I wrote myself.', MessageSender.human),
            _message('overheard', 'SPEAKER_0: Synthetic overheard words', MessageSender.human)
              ..isCanonicalOverheardTranscript = true,
          ];
        final apps = AppProvider();
        final connection = ConnectivityProvider();
        final recorder = VoiceRecorderProvider();
        final conversations = ConversationProvider();
        final integrations = IntegrationProvider();
        final boundary = GlobalKey();
        await tester.pumpWidget(MultiProvider(
            providers: [
              ChangeNotifierProvider<HomeProvider>.value(value: home),
              ChangeNotifierProvider<MessageProvider>.value(value: messages),
              ChangeNotifierProvider<AppProvider>.value(value: apps),
              ChangeNotifierProvider<ConnectivityProvider>.value(value: connection),
              ChangeNotifierProvider<VoiceRecorderProvider>.value(value: recorder),
              ChangeNotifierProvider<ConversationProvider>.value(value: conversations),
              ChangeNotifierProvider<IntegrationProvider>.value(value: integrations),
            ],
            child: MaterialApp(
                theme: dark ? ellaDarkThemeData() : ellaThemeData(),
                localizationsDelegates: AppLocalizations.localizationsDelegates,
                supportedLocales: AppLocalizations.supportedLocales,
                builder: (context, child) => MediaQuery(
                    data:
                        MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                    child: child!),
                home: Builder(
                    builder: (context) => RepaintBoundary(
                        key: boundary,
                        child: const HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                          SizedBox.shrink(),
                          ChatPage(isPivotBottom: true),
                          SizedBox.shrink(),
                          SizedBox.shrink()
                        ]))))));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 350));
        primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 400));
        expect(find.text('[Ella] is a label I wrote myself.'), findsOneWidget);
        expect(find.text('SPEAKER_0: Synthetic overheard words'), findsNothing);
        expect(messages.messages.any((m) => m.id == 'overheard'), isTrue);
        final ai = tester.widget<NormalMessageWidget>(find.byType(NormalMessageWidget));
        expect(ai.messageText.startsWith('[Ella]'), isFalse);
        expect(messages.messages.first.text.startsWith('[Ella]'), isTrue);
        final bar = tester.widget<AppBar>(find.byType(AppBar));
        expect(bar.backgroundColor, Colors.transparent);
        expect(bar.scrolledUnderElevation, 0);
        expect(tester.takeException(), isNull);
        await tester.runAsync(() async {
          final image = await (boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary).toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final dir = Directory('/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-923-review-renders')
            ..createSync(recursive: true);
          File('${dir.path}/chat-${dark ? 'dark' : 'light'}-${scale}x-test-render.png')
              .writeAsBytesSync(bytes!.buffer.asUint8List());
          image.dispose();
        });
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump(const Duration(seconds: 1));
        for (final provider in [home, messages, apps, connection, recorder, conversations, integrations]) {
          provider.dispose();
        }
      });
    }
  }
}
