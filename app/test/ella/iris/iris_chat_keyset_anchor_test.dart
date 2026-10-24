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
                        messages: List.generate(empty ? 0 : 25, (i) => row(i + 100)),
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
    for (final keyboard in [0.0, 390.0]) {
      for (final empty in [false, true]) {
        testWidgets('keyset load preserves visible anchor and unsent draft dark=$dark inset=$keyboard empty=$empty',
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
                  localizationsDelegates: AppLocalizations.localizationsDelegates,
                  supportedLocales: AppLocalizations.supportedLocales,
                  home: HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                    const SizedBox.shrink(),
                    ChatPage(isPivotBottom: true, draftAuthenticatedUid: () => 'keyboard-fixture-owner'),
                    const SizedBox.shrink(),
                    const SizedBox.shrink()
                  ]))));
          await tester.pump(const Duration(seconds: 1));
          final field = find.byType(TextField).first;
          await tester.tap(field);
          await tester.enterText(field, 'Unsent keyset draft');
          tester.view.viewInsets = FakeViewPadding(bottom: keyboard);
          await tester.pump(const Duration(seconds: 1));
          final chat = tester.state<ChatPageState>(find.byType(ChatPage));
          for (var settle = 0; settle < 8; settle++) {
            chat.scrollController.jumpTo(chat.scrollController.position.maxScrollExtent);
            await tester.pump(const Duration(milliseconds: 100));
          }
          final anchor = find
              .byWidgetPredicate((w) => w is RichText && w.text.toPlainText().startsWith('Synthetic current row 100 '));
          if (!empty) expect(anchor, findsOneWidget);
          final before = empty ? null : tester.getRect(anchor);
          final button = find.byKey(const Key('ella-load-earlier-history'));
          expect(button.hitTestable(), findsOneWidget);
          expect(tester.widget<TextButton>(button).style!.foregroundColor!.resolve({}),
              EllaPalette.of(tester.element(button)).textPrimary);
          expect(tester.getSize(button).height, greaterThanOrEqualTo(44));
          await tester.tap(button);
          await tester.pump();
          expect(messages.isLoadingEarlierHistory, true);
          expect(tester.widget<TextButton>(button).onPressed, isNull);
          pending.complete(EllaServiceResult.success(EllaChatHistoryPage(
              messages: List.generate(10, (i) => row(i, older: true)), hasMore: false, isKeyset: true)));
          await tester.pump();
          await tester.pump(const Duration(milliseconds: 300));
          expect(messages.messages.length, empty ? 10 : 35);
          expect(chat.textController.text, 'Unsent keyset draft');
          expect(chat.textFieldFocusNode.hasFocus, true);
          if (!empty) expect(tester.getRect(anchor).top, closeTo(before!.top, 1));
          expect(find.byKey(const Key('ella-load-earlier-history')), findsNothing);
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox());
        });
      }
    }
}
