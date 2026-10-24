import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/ella/widgets/v2v_fallback_dialog.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/semantics.dart';
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

import 'dart:async';
import 'package:omi/ella/services/ella_service_result.dart';
import 'package:omi/ella/services/ella_chat_service.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements AccountCommitAuthority {
  @override
  String get uid => 'keyboard-fixture-owner';
  @override
  bool isCurrent() => SharedPreferencesUtil().uid == uid;
  @override
  bool isExactCurrent() => isCurrent();
}

ServerMessage row(int i, {bool older = false}) => ServerMessage(
    'row-$i',
    DateTime.utc(2026, 10, older ? 10 : 12),
    'Synthetic ${older ? 'older' : 'current'} row $i ' + ('variable height text ' * ((i % 3) + 1)),
    MessageSender.ai,
    MessageType.text,
    null,
    false,
    [],
    [],
    [],
    canonicalEventKey: i.toRadixString(16).padLeft(64, '0'));

class _Messages extends MessageProvider {
  _Messages(this.pending, {bool empty = false})
      : super(
            historyPaginationEnabled: true,
            activeAuthority: () => _Authority(),
            ellaChatKeysetFetcher:
                ({required cursor, required expectedAuthenticatedUid, required exactAuthority}) async => cursor == null
                    ? EllaServiceResult.success(EllaChatHistoryPage(
                        messages: List.generate(empty ? 0 : 25, (i) {
                          final message = row(i + 100);
                          if (i == 24)
                            message.text = 'Synthetic oversized reply\n' +
                                List.generate(100,
                                        (n) => 'Synthetic long-content line $n. A bounded offline gesture fixture.')
                                    .join('\n');
                          return message;
                        }),
                        hasMore: true,
                        isKeyset: true,
                        nextCursor: 'first.token'))
                    : await pending.future);
  final Completer<EllaServiceResult<EllaChatHistoryPage>> pending;
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
  for (final dark in [false, true])
    for (final scale in [1.0, 3.5])
      for (final keyboard in [0.0, 390.0]) {
        for (final empty in [false]) {
          testWidgets('fixed history actions oversized reply dark=$dark scale=$scale keyboard=$keyboard',
              (tester) async {
            tester.view.physicalSize = const Size(393, 852);
            tester.view.devicePixelRatio = 1;
            tester.view.padding = const FakeViewPadding(top: 59, bottom: 34);
            tester.view.viewPadding = const FakeViewPadding(top: 59, bottom: 34);
            addTearDown(tester.view.resetPhysicalSize);
            addTearDown(tester.view.resetDevicePixelRatio);
            addTearDown(tester.view.resetPadding);
            addTearDown(tester.view.resetViewPadding);
            addTearDown(tester.view.resetViewInsets);
            final pending = Completer<EllaServiceResult<EllaChatHistoryPage>>();
            final messages = _Messages(pending, empty: empty);
            await messages.refreshMessages();
            messages.messages.last.text = 'Synthetic oversized reply\n' +
                List.generate(100, (i) => 'Synthetic long-content line $i. A bounded offline gesture fixture.')
                    .join('\n');
            final home = HomeProvider()..setIndex(1);
            EllaCaptureHost.installForTesting(
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
                    builder: (context, child) => RepaintBoundary(
                        key: const Key('coherent-render'),
                        child: MediaQuery(
                            data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale)),
                            child: child!)),
                    localizationsDelegates: AppLocalizations.localizationsDelegates,
                    supportedLocales: AppLocalizations.supportedLocales,
                    home: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                      const SizedBox.shrink(),
                      ChatPage(isPivotBottom: true, draftAuthenticatedUid: () => 'keyboard-fixture-owner'),
                      const SizedBox.shrink(),
                      const SizedBox.shrink()
                    ]))));
            await tester.pump(const Duration(seconds: 1));
            final chat = tester.state<ChatPageState>(find.byType(ChatPage));
            Future<void> capture(String name) async {
              await tester.runAsync(() async {
                final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(const Key('coherent-render')));
                final image = await boundary.toImage();
                final bytes = (await image.toByteData(format: ui.ImageByteFormat.png))!.buffer.asUint8List();
                final out = Directory('../rendered-coherent-screens')..createSync(recursive: true);
                File('${out.path}/$name.png').writeAsBytesSync(bytes);
                image.dispose();
              });
            }

            if (scale == 1 && keyboard == 0) {
              chat.textFieldFocusNode.unfocus();
              await tester.pump(const Duration(milliseconds: 500));
              await capture('chat-${dark ? 'dark' : 'light'}-transparent-header');
              await EllaV7CompanionPreferences.shared
                  .save(uid: 'keyboard-fixture-owner', name: 'Echo', colorIndex: 1, styleIndex: 1);
              await tester.pump(const Duration(milliseconds: 500));
              await capture('chat-${dark ? 'dark' : 'light'}-leaf-style');
              await tester.tap(find.byKey(const Key('chat-composer-dictation')));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              await tester.pump();
              expect(find.byKey(const Key('chat-dictation-sheet')), findsOneWidget);
              expect(find.text('Dictation is unavailable right now. You can type your message.'), findsOneWidget);
              expect(tester.widget<TextButton>(find.byKey(const Key('chat-dictation-start'))).onPressed, isNull);
              await capture('dictation-${dark ? 'dark' : 'light'}-honest-unavailable');
              final close = find.byKey(const Key('chat-dictation-cancel'));
              expect(close.hitTestable(), findsOneWidget);
              expect(tester.getSize(close).width, greaterThanOrEqualTo(48));
              expect(tester.getSize(close).height, greaterThanOrEqualTo(48));
              await tester.tap(close);
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              await tester.pump();
              showDialog<V2VFailureChoice>(
                  context: tester.element(find.byType(ChatPage)),
                  builder: (_) => const V2VFallbackDialog(
                      receipt: V2VConnectionReceipt(
                          connected: false,
                          provider: 'grok-voice',
                          voiceMode: 'offline-fixture-only',
                          stage: V2VConnectionStage.websocket,
                          httpStatus: 403,
                          errorCode: 'fixture_provider_unavailable')));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              await tester.pump();
              expect(find.byKey(const ValueKey('v2v-failure-elevenlabs')), findsNothing);
              await capture('voice-${dark ? 'dark' : 'light'}-truthful-error');
              Navigator.of(tester.element(find.byType(AlertDialog))).pop();
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              await tester.pump();
            }
            if (scale > 1 && keyboard == 0) {
              chat.textFieldFocusNode.unfocus();
              await tester.pump();
              await tester.tap(find.byKey(const Key('chat-composer-dictation')));
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              final close = find.byKey(const Key('chat-dictation-cancel'));
              await tester.ensureVisible(close);
              await tester.pump();
              expect(close.hitTestable(), findsOneWidget);
              final button = tester.widget<TextButton>(close);
              final ink = button.style!.foregroundColor!.resolve({})!;
              final surface = tester.widget<AlertDialog>(find.byType(AlertDialog)).backgroundColor!;
              final luminances = [ink.computeLuminance(), surface.computeLuminance()]..sort();
              expect((luminances.last + .05) / (luminances.first + .05), greaterThan(4.5));
              final scrollable = find.descendant(of: find.byType(AlertDialog), matching: find.byType(Scrollable)).first;
              final scroll = tester.state<ScrollableState>(scrollable).position;
              expect(scroll.maxScrollExtent, greaterThan(0));
              scroll.jumpTo(scroll.maxScrollExtent);
              await tester.pump();
              final body = find.text('Dictation is unavailable right now. You can type your message.');
              expect(tester.getRect(body).bottom, lessThanOrEqualTo(tester.getRect(close).top + 1),
                  reason: 'The ending is revealable while the real Close action stays pinned.');
              await capture('dictation-${dark ? 'dark' : 'light'}-large-readable-close');
              await tester.tap(close);
              await tester.pump();
              await tester.pump(const Duration(milliseconds: 500));
              expect(find.byType(AlertDialog), findsNothing);
            }
            final field = find.byType(TextField).first;
            await tester.tap(field);
            await tester.enterText(field, 'Protected offline draft');
            tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
            await tester.pump(const Duration(seconds: 1));
            final viewport =
                find.byWidgetPredicate((w) => w is ListView && identical(w.controller, chat.scrollController));
            final load = find.byKey(const Key('ella-load-earlier-history'));
            final older = find.byKey(const Key('chat-history-older'));
            final newer = find.byKey(const Key('chat-history-newer'));
            expect(load.hitTestable(), findsOneWidget,
                reason: 'load action reachable before any gesture through giant reply');
            expect(older.hitTestable(), findsOneWidget);
            expect(newer.hitTestable(), findsOneWidget);
            expect(tester.getRect(load).bottom, lessThanOrEqualTo(tester.getRect(viewport).top + 1));
            final before = chat.scrollController.offset;
            final semantics = tester.ensureSemantics();
            final oldNode = tester.getSemantics(older);
            expect(oldNode.getSemanticsData().hasAction(SemanticsAction.tap), true);
            tester.binding.pipelineOwner.semanticsOwner!.performAction(oldNode.id, SemanticsAction.tap);
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 300));
            expect(chat.scrollController.offset, greaterThan(before + 10));
            semantics.dispose();
            final after = chat.scrollController.offset;
            await tester.tap(newer);
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 300));
            expect(chat.scrollController.offset, lessThan(after));
            expect(chat.textController.text, 'Protected offline draft');
            expect(chat.textFieldFocusNode.hasFocus, true);
            final offset = chat.scrollController.offset;
            await tester.tap(load);
            await tester.pump();
            expect(messages.isLoadingEarlierHistory, true);
            expect(tester.widget<TextButton>(load).onPressed, isNull);
            pending.complete(EllaServiceResult.success(
                EllaChatHistoryPage(messages: [row(1, older: true)], hasMore: false, isKeyset: true)));
            await tester.pump();
            await tester.pump(const Duration(milliseconds: 300));
            expect(messages.messages.length, 26);
            expect(chat.scrollController.offset, closeTo(offset, 1));
            expect(chat.textController.text, 'Protected offline draft');
            expect(chat.textFieldFocusNode.hasFocus, true);
            expect(load, findsNothing);
            expect(older.hitTestable(), findsOneWidget,
                reason: 'viewport traversal remains after history is exhausted');
            expect(tester.takeException(), isNull);
            await tester.pumpWidget(const SizedBox());
          }, variant: TargetPlatformVariant.only(TargetPlatform.iOS));
        }
      }
}
