import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/guardian_alert.dart';
import 'package:omi/ella/pages/guardian_alert_history_page.dart';
import 'package:omi/ella/services/guardian_alert_history_api.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _NavigationSpy extends NavigatorObserver {
  int pushes = 0;
  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    pushes++;
  }
}

class _Authority implements ExactAccountAuthorityVerifier {
  _Authority(this.uid);
  @override
  final String uid;
  bool current = true;
  @override
  bool isExactCurrent() => current;
}

GuardianAlertHistoryResult _result(String text) => GuardianAlertHistoryResult(
      source: GuardianAlertHistorySource.backend,
      records: [
        GuardianAlertRecord(
            id: text,
            sourceConversationId: 'source-fixture',
            alertText: text,
            triggerType: 'memory',
            deliveryTarget: 'user',
            playbackStatus: 'queued',
            createdAt: DateTime(2026, 10, 9))
      ],
    );
Widget _app(Widget child, ConversationProvider conversations) =>
    ChangeNotifierProvider.value(value: conversations, child: MaterialApp(theme: ellaThemeData(), home: child));

void main() {
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
    final material = File('/Users/ellaai/dev/flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader('MaterialIcons')..addFont(Future.value(ByteData.sublistView(await material.readAsBytes()))))
        .load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  testWidgets('actual shell leaves lazy Whispers absent and preserves six-slot legacy indices', (tester) async {
    final home = HomeProvider();
    final connectivity = ConnectivityProvider();
    addTearDown(home.dispose);
    addTearDown(connectivity.dispose);
    await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<HomeProvider>.value(value: home),
          ChangeNotifierProvider<ConnectivityProvider>.value(value: connectivity),
        ],
        child: MaterialApp(
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            home: HomePage(
                runtimeSideEffectsEnabled: false,
                pagesOverride: const [Text('memory'), Text('chat'), Text('talk'), Text('more')]))));
    await tester.pump();
    expect(home.selectedIndex, 1);
    expect(find.byType(GuardianAlertHistoryPage, skipOffstage: false), findsNothing);
    final stack = tester.widget<IndexedStack>(find.byType(IndexedStack));
    expect(stack.children.length, 6);
    expect(stack.children[4], isA<SizedBox>());
    expect(stack.children[5], isA<SizedBox>());
    for (final index in [0, 2, 3, 1, 0, 1]) {
      home.setIndex(index);
      await tester.pump();
      expect(tester.widget<IndexedStack>(find.byType(IndexedStack)).index, index);
      expect(find.byType(GuardianAlertHistoryPage, skipOffstage: false), findsNothing);
    }
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('retired pending request cannot reveal late private history or fetch again', (tester) async {
    final owner = _Authority('owner-a');
    final pending = Completer<GuardianAlertHistoryResult>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var calls = 0;
    await tester.pumpWidget(_app(
        GuardianAlertHistoryPage(
            embedded: true,
            surfaceAllowed: () => true,
            authorityProvider: () => owner,
            historyLoader: () {
              calls++;
              return pending.future;
            }),
        conversations));
    expect(calls, 1);
    owner.current = false;
    await tester.pump(const Duration(milliseconds: 300));
    pending.complete(_result('private old owner'));
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('private old owner'), findsNothing);
    expect(calls, 1);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('displayed data is redacted on same-UID authority retirement without auth event', (tester) async {
    final owner = _Authority('owner-a');
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    await tester.pumpWidget(_app(
        GuardianAlertHistoryPage(
            embedded: true,
            surfaceAllowed: () => true,
            authorityProvider: () => owner,
            historyLoader: () async => _result('visible old owner')),
        conversations));
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('visible old owner'), findsOneWidget);
    owner.current = false;
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump();
    expect(find.textContaining('visible old owner'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    expect(tester.takeException(), isNull);
  });

  testWidgets('explicit fresh refresh under replacement authority fences old pending result', (tester) async {
    final first = _Authority('owner-a');
    final second = _Authority('owner-b');
    _Authority current = first;
    final pending = Completer<GuardianAlertHistoryResult>();
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var calls = 0;
    await tester.pumpWidget(_app(
        GuardianAlertHistoryPage(
            embedded: true,
            surfaceAllowed: () => true,
            authorityProvider: () => current,
            historyLoader: () {
              calls++;
              return calls == 1 ? pending.future : Future.value(_result('new owner'));
            }),
        conversations));
    first.current = false;
    current = second;
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump();
    final refresh = tester.widget<RefreshIndicator>(find.byType(RefreshIndicator));
    final finished = refresh.onRefresh();
    await tester.pump();
    await finished;
    await tester.pump();
    expect(calls, 2);
    expect(find.textContaining('new owner'), findsOneWidget);
    pending.complete(_result('late old owner'));
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('late old owner'), findsNothing);
    expect(find.textContaining('new owner'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    expect(tester.takeException(), isNull);
  });

  testWidgets('old source callback cannot rebind to a fresh replacement authority', (tester) async {
    final first = _Authority('owner-a');
    final second = _Authority('owner-b');
    _Authority current = first;
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    conversations.conversations = [
      ServerConversation(
          id: 'memory-a', createdAt: DateTime(2026, 10, 9), structured: Structured('Old memory', 'Old summary'))
    ];
    final observer = _NavigationSpy();
    await tester.pumpWidget(ChangeNotifierProvider.value(
        value: conversations,
        child: MaterialApp(
            theme: ellaThemeData(),
            navigatorObservers: [observer],
            home: GuardianAlertHistoryPage(
                embedded: true,
                surfaceAllowed: () => true,
                authorityProvider: () => current,
                historyLoader: () async =>
                    GuardianAlertHistoryResult(source: GuardianAlertHistorySource.backend, records: [
                      GuardianAlertRecord(
                          id: 'old',
                          alertText: 'Old private record',
                          triggerType: 'memory',
                          deliveryTarget: 'user',
                          playbackStatus: 'queued',
                          createdAt: DateTime(2026, 10, 9),
                          sourceConversationId: 'memory-a')
                    ])))));
    await tester.pump();
    await tester.pump();
    final source = find.ancestor(of: find.text('Related memory'), matching: find.byType(InkWell));
    final oldTap = tester.widget<InkWell>(source).onTap!;
    final refresh = tester.widget<RefreshIndicator>(find.byType(RefreshIndicator)).onRefresh;
    first.current = false;
    current = second;
    final before = observer.pushes;
    final pending = refresh();
    // Deliberately invoke the old render's callback before the next frame.
    oldTap();
    expect(observer.pushes, before);
    await pending;
    await tester.pumpWidget(const SizedBox.shrink());
    expect(tester.takeException(), isNull);
  });

  testWidgets('failed and queued history statuses never imply played', (tester) async {
    final authority = _Authority('owner');
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    for (final status in ['not_played', 'playback_failed', 'queued', 'unknown', 'played', 'completed', 'complete']) {
      await tester.pumpWidget(_app(
          GuardianAlertHistoryPage(
              key: ValueKey(status),
              embedded: true,
              surfaceAllowed: () => true,
              authorityProvider: () => authority,
              historyLoader: () async =>
                  GuardianAlertHistoryResult(source: GuardianAlertHistorySource.backend, records: [
                    GuardianAlertRecord(
                        id: status,
                        alertText: status,
                        triggerType: 'memory',
                        deliveryTarget: 'user',
                        playbackStatus: status,
                        createdAt: DateTime(2026, 10, 9))
                  ])),
          conversations));
      await tester.pump();
      await tester.pump();
      final dots = find.byWidgetPredicate(
          (widget) => widget is Container && widget.constraints?.maxWidth == 10 && widget.constraints?.maxHeight == 10);
      final dot = tester.widget<Container>(dots);
      final decoration = dot.decoration as BoxDecoration;
      expect(decoration.color,
          ['played', 'completed', 'complete'].contains(status) ? EllaPalette(false).teal : Colors.transparent,
          reason: status);
      expect(tester.takeException(), isNull);
    }
    await tester.pumpWidget(const SizedBox.shrink());
  });

  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.0]) {
      testWidgets('synthetic actual Whispers page ${dark ? 'dark' : 'light'} ${scale}x', (tester) async {
        tester.view.physicalSize = Size(scale == 3 ? 320 : 390, 844);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final owner = _Authority('synthetic-owner');
        final conversations = ConversationProvider();
        addTearDown(conversations.dispose);
        conversations.conversations = [
          ServerConversation(
              id: 'source-fixture',
              createdAt: DateTime(2026, 10, 9),
              structured: Structured('Synthetic source', 'Synthetic source text'))
        ];
        final boundary = GlobalKey();
        await tester.pumpWidget(ChangeNotifierProvider.value(
            value: conversations,
            child: MaterialApp(
                theme: dark ? ellaDarkThemeData() : ellaThemeData(),
                builder: (context, child) => MediaQuery(
                    data:
                        MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                    child: child!),
                home: RepaintBoundary(
                    key: boundary,
                    child: EllaIrisBackdrop(
                        child: GuardianAlertHistoryPage(
                            embedded: true,
                            surfaceAllowed: () => true,
                            authorityProvider: () => owner,
                            historyLoader: () async => _result('A quiet reminder to bring the blue folder.')))))));
        await tester.pump();
        await tester.pump();
        expect(find.textContaining('A quiet reminder'), findsOneWidget);
        expect(tester.takeException(), isNull);
        final contextText = find.text('This came up in your conversation.');
        final paragraph =
            tester.renderObject<RenderParagraph>(find.descendant(of: contextText, matching: find.byType(RichText)));
        const sentence = 'This came up in your conversation.';
        final measure = TextPainter(
            text: TextSpan(text: 'conversation.', style: tester.widget<Text>(contextText).style),
            textDirection: TextDirection.ltr,
            textScaler: TextScaler.linear(scale))
          ..layout();
        debugPrint('WORD WIDTH ${measure.width} PARAGRAPH ${paragraph.size.width}');
        final boxes = paragraph.getBoxesForSelection(
            TextSelection(baseOffset: sentence.indexOf('conversation'), extentOffset: sentence.length - 1));
        expect(boxes.map((box) => box.top).toSet(), hasLength(1),
            reason: 'conversation must wrap as a complete word at user text scale');
        await tester.ensureVisible(find.text('Related memory'));
        await tester.pump();
        expect(tester.getRect(find.text('Related memory')).bottom, lessThanOrEqualTo(844));
        await tester.runAsync(() async {
          final image = await (boundary.currentContext!.findRenderObject() as RenderRepaintBoundary).toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          final path =
              '/Users/ellaai/Documents/Codex/2026-10-07/task-4/iris-accessibility-whispers-${dark ? 'dark' : 'light'}-${scale}x-synthetic.png';
          File(path).writeAsBytesSync(bytes!.buffer.asUint8List());
          image.dispose();
        });
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }

  testWidgets('denied surface never fetches and disposed request cannot publish', (tester) async {
    final owner = _Authority('owner-a');
    final conversations = ConversationProvider();
    addTearDown(conversations.dispose);
    var calls = 0;
    await tester.pumpWidget(_app(
        GuardianAlertHistoryPage(
            surfaceAllowed: () => false,
            authorityProvider: () => owner,
            historyLoader: () {
              calls++;
              return Future.value(_result('denied'));
            }),
        conversations));
    expect(calls, 0);
    await tester.pumpWidget(const SizedBox.shrink());
    final pending = Completer<GuardianAlertHistoryResult>();
    await tester.pumpWidget(_app(
        GuardianAlertHistoryPage(
            embedded: true,
            surfaceAllowed: () => true,
            authorityProvider: () => owner,
            historyLoader: () {
              calls++;
              return pending.future;
            }),
        conversations));
    await tester.pumpWidget(const SizedBox.shrink());
    pending.complete(_result('disposed'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(calls, 1);
    expect(tester.takeException(), isNull);
  });
}
