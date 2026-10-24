import 'dart:io';
import 'package:flutter/foundation.dart';
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
    for (final keyboard in [0.0]) {
      for (final empty in [false]) {
        testWidgets('oversized reply body and gutter touch gestures dark=$dark', (tester) async {
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
          chat.textFieldFocusNode.unfocus();
          await tester.pump(const Duration(seconds: 1));
          final viewport =
              find.byWidgetPredicate((w) => w is ListView && identical(w.controller, chat.scrollController));
          final rect = tester.getRect(viewport);
          final before = chat.scrollController.offset;
          await tester.dragFrom(rect.center, const Offset(0, 180));
          await tester.pump(const Duration(milliseconds: 600));
          expect(chat.scrollController.offset, greaterThan(before + 20), reason: 'touch gesture over long text');
          final body = chat.scrollController.offset;
          await tester.dragFrom(Offset(rect.left + 3, rect.center.dy), const Offset(0, 180));
          await tester.pump(const Duration(milliseconds: 600));
          expect(chat.scrollController.offset, greaterThan(body + 20), reason: 'touch gesture over blank gutter');
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox());
        }, variant: TargetPlatformVariant.only(TargetPlatform.iOS));
      }
    }
}
