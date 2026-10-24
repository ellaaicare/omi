import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/page.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/ella/pages/ella_settings_page.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';
import 'package:omi/widgets/conversation_bottom_bar.dart';

class _DockProbe extends StatefulWidget {
  const _DockProbe(this.onMount);
  final VoidCallback onMount;
  @override
  State<_DockProbe> createState() => _DockProbeState();
}

class _DockProbeState extends State<_DockProbe> {
  @override
  void initState() {
    super.initState();
    widget.onMount();
  }

  @override
  Widget build(BuildContext context) => const SizedBox(height: 48, child: Text('Capture state probe'));
}

Widget _app(Widget child, {double scale = 1}) => MaterialApp(
      theme: ellaThemeData(),
      locale: const Locale('en'),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
          child: child!),
      home: child,
    );

void main() {
  setUpAll(() async {
    try {
      await ServiceManager.init();
    } catch (_) {}
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(EllaCaptureHost.resetForTesting);

  for (final scale in [1.0, 1.28, 1.35, 3.0]) {
    testWidgets('922 regression Privacy has visible Back returning to three tabs scale=$scale', (tester) async {
      final home = HomeProvider();
      final device = DeviceProvider(automaticallyReconnectOnReady: false);
      final user = UserProvider();
      final connectivity = ConnectivityProvider();
      addTearDown(home.dispose);
      addTearDown(device.dispose);
      addTearDown(user.dispose);
      addTearDown(connectivity.dispose);
      await tester.pumpWidget(MultiProvider(
          providers: [
            ChangeNotifierProvider<HomeProvider>.value(value: home),
            ChangeNotifierProvider<DeviceProvider>.value(value: device),
            ChangeNotifierProvider<UserProvider>.value(value: user),
            ChangeNotifierProvider<ConnectivityProvider>.value(value: connectivity),
          ],
          child: _app(
              const HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
                Text('Journal route'),
                Text('Chat route'),
                SizedBox.shrink(),
                EllaSettingsPage(runtimeSideEffectsEnabled: false, authenticatedUidOverride: ''),
              ]),
              scale: scale)));
      for (var repeat = 0; repeat < 2; repeat++) {
        await tester.tap(scale < 2 ? find.text('More') : find.byKey(const Key('iris-navigation-3')));
        await tester.pump();
        final privacy = find.byKey(const Key('iris-more-privacy'));
        await tester.scrollUntilVisible(privacy, 120, scrollable: find.byType(Scrollable).last);
        await tester.ensureVisible(privacy);
        await tester.pump();
        for (var scroll = 0; scroll < 5 && privacy.hitTestable().evaluate().isEmpty; scroll++) {
          await tester.drag(find.byType(Scrollable).last, const Offset(0, -100));
          await tester.pumpAndSettle();
        }
        expect(privacy.hitTestable(), findsOneWidget);
        await tester.tap(privacy);
        await tester.pumpAndSettle();
        final back = find.byKey(const Key('iris-privacy-back'));
        expect(back.hitTestable(), findsOneWidget);
        expect(tester.getSize(back).width, greaterThanOrEqualTo(44));
        expect(tester.getSize(back).height, greaterThanOrEqualTo(44));
        await tester.tap(back);
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('iris-privacy-back')), findsNothing);
        for (final label in ['Chat', 'Journal', 'More']) {
          if (scale < 2) {
            expect(find.text(label).hitTestable(), findsWidgets);
          } else {
            final index = {'Chat': 1, 'Journal': 0, 'More': 3}[label]!;
            expect(find.byKey(Key('iris-navigation-$index')).hitTestable(), findsOneWidget);
          }
        }
        await tester.tap(scale < 2 ? find.text('Journal') : find.byKey(const Key('iris-navigation-0')));
        await tester.pump();
        expect(find.text('Journal route'), findsOneWidget);
      }
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }

  testWidgets('shell starts Chat and owns one dock across real destination changes', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final home = HomeProvider();
    final connectivity = ConnectivityProvider();
    addTearDown(home.dispose);
    addTearDown(connectivity.dispose);
    var mounts = 0;
    EllaCaptureHost.installForTesting(homeCaptureDockBuilder: (_) => _DockProbe(() => mounts++));
    await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<HomeProvider>.value(value: home),
          ChangeNotifierProvider<ConnectivityProvider>.value(value: connectivity),
        ],
        child: _app(HomePage(runtimeSideEffectsEnabled: false, pagesOverride: [
          Builder(
              builder: (context) => Text(
                  EllaCaptureDockPlacementScope.active(context) ? 'Memory without duplicate dock' : 'Wrong placement')),
          Builder(
              builder: (context) => Column(children: [
                    const Text('Chat page'),
                    EllaCaptureHost.homeCaptureDockBuilder!(context),
                  ])),
          const Text('Talk page'),
          const Text('More page'),
        ]))));
    await tester.pump();
    expect(home.selectedIndex, 1);
    expect(mounts, 1);
    expect(find.text('Chat page'), findsOneWidget);
    expect(find.text('Reminders'), findsNothing);
    for (final destination in [('Journal', 0), ('More', 3), ('Chat', 1)]) {
      await tester.tap(find.text(destination.$1));
      await tester.pump();
      expect(home.selectedIndex, destination.$2);
      expect(mounts, 1);
    }
    expect(tester.takeException(), isNull);
  });

  for (final scale in [1.0, 3.0]) {
    testWidgets('three direct destinations remain visible at scale=$scale', (tester) async {
      tester.view.physicalSize = const Size(320, 780);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final home = HomeProvider();
      addTearDown(home.dispose);
      final selected = <int>[];
      await tester.pumpWidget(ChangeNotifierProvider.value(
          value: home,
          child: _app(Scaffold(body: BottomNavBar(onTabTap: (index, repeat) => selected.add(index))), scale: scale)));
      for (final label in ['Chat', 'Journal', 'More']) {
        if (scale < 2) {
          expect(find.text(label), findsOneWidget);
        } else {
          expect(find.text(label), findsNothing);
          expect(find.bySemanticsLabel(label), findsOneWidget);
          expect(tester.getSemantics(find.bySemanticsLabel(label)).getSemanticsData().hasAction(SemanticsAction.tap),
              isTrue);
        }
      }
      expect(find.text('Talk'), findsNothing);
      expect(find.text('Whispers'), findsNothing);
      expect(find.byKey(const Key('iris-navigation-menu')), findsNothing);
      await tester.tap(scale < 2 ? find.text('Journal') : find.byKey(const Key('iris-navigation-0')));
      await tester.pump();
      expect(selected, [0]);
      expect(tester.takeException(), isNull);
    });
    testWidgets('memory section menu keeps original words and actions at scale=$scale', (tester) async {
      var selected = ConversationTab.summary;
      final memory = ServerConversation(
          id: 'source',
          createdAt: DateTime(2026, 10, 9),
          structured: Structured('Title', 'Body'),
          transcriptSegments: [
            TranscriptSegment(
                id: 'segment',
                text: 'Original text',
                speaker: 'SPEAKER_0',
                isUser: false,
                personId: null,
                translations: [],
                start: 0,
                end: 1)
          ]);
      await tester.pumpWidget(_app(
          Scaffold(
              body: ConversationBottomBar(
            mode: ConversationBottomBarMode.memorySource,
            selectedTab: selected,
            conversation: memory,
            onTabSelected: (tab) => selected = tab,
            onStopPressed: () {},
            hasSegments: true,
            hasActionItems: true,
          )),
          scale: scale));
      await tester.tap(find.byKey(const Key('iris-memory-section-menu')));
      await tester.pumpAndSettle();
      expect(find.text('Original words'), findsOneWidget);
      expect(find.text('Action Items'), findsOneWidget);
      await tester.tap(find.text('Original words'));
      await tester.pumpAndSettle();
      expect(selected, ConversationTab.transcript);
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets('non-transcript original is labeled Source and empty memory keeps summary', (tester) async {
    for (final hasSource in [true, false]) {
      await tester.pumpWidget(_app(
          Scaffold(
              body: ConversationBottomBar(
            mode: ConversationBottomBarMode.memorySource,
            selectedTab: ConversationTab.summary,
            conversation: ServerConversation(
                id: 'photo-source', createdAt: DateTime(2026), structured: Structured('Title', 'Body')),
            hasSegments: hasSource,
            hasActionItems: false,
            onTabSelected: (_) {},
            onStopPressed: () {},
          )),
          scale: 3));
      await tester.tap(find.byKey(const Key('iris-memory-section-menu')));
      await tester.pumpAndSettle();
      expect(find.text('Original words'), findsNothing);
      expect(find.text('Source'), hasSource ? findsOneWidget : findsNothing);
      expect(find.text('Ella’s summary'), findsWidgets);
      expect(tester.takeException(), isNull);
      await tester.tapAt(const Offset(10, 10));
      await tester.pumpAndSettle();
    }
  });
  testWidgets('empty direct-source/action entry retains truthful selected section', (tester) async {
    for (final tab in [ConversationTab.transcript, ConversationTab.actionItems]) {
      await tester.pumpWidget(_app(
          Scaffold(
              body: ConversationBottomBar(
            mode: ConversationBottomBarMode.memorySource,
            selectedTab: tab,
            hasSegments: false,
            hasActionItems: false,
            onTabSelected: (_) {},
            onStopPressed: () {},
          )),
          scale: 3));
      expect(find.text(tab == ConversationTab.transcript ? 'Source' : 'Action Items'), findsOneWidget);
      expect(tester.takeException(), isNull);
    }
  });
}
