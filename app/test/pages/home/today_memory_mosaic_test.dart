import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/action_item.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/today_card.dart';
import 'package:omi/ella/services/today_card_repository.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';
import 'package:omi/providers/action_items_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/services/services.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/widgets/bottom_nav_bar.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Medium.ttf')))
        .load();
    await (FontLoader(
      'packages/font_awesome_flutter/FontAwesomeSolid',
    )..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Free-Solid-900.otf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      flutterCache = flutterCache.parent;
    }
    final materialIcons = File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader(
      'MaterialIcons',
    )..addFont(materialIcons.readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
    try {
      await ServiceManager.init();
    } catch (_) {
      // Process-global test services may already be initialized.
    }
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({'givenName': 'Margaret'});
    await SharedPreferencesUtil.init();
  });

  testWidgets('ready Home matches the selected Memory Mosaic hierarchy', (tester) async {
    final photoData = await rootBundle.load('assets/images/onboarding-bg-1.webp');
    final conversations = _ConversationFixtures.withMemories(photoBase64: base64Encode(photoData.buffer.asUint8List()));
    final harness = await _pumpHome(tester, conversations: conversations, includeBottomNav: true);
    addTearDown(harness.dispose);

    final renderedText =
        tester.widgetList<Text>(find.byType(Text)).map((widget) => widget.data).whereType<String>().toList();
    expect(find.text('SUNDAY · AUGUST 9'), findsOneWidget, reason: renderedText.join(' | '));
    expect(find.text('Good morning, Margaret'), findsOneWidget);
    expect(find.text('Record a moment'), findsOneWidget);
    expect(find.text('Records on this iPhone'), findsOneWidget);
    expect(find.text('Recent memories'), findsOneWidget);
    expect(find.byKey(const Key('memory-source-photo')), findsOneWidget);
    expect(find.byKey(const Key('memory-fallback-art')), findsOneWidget);
    expect(find.text('Talk'), findsOneWidget);
    expect(find.text('Voice'), findsNothing);
    expect(find.text('Read aloud'), findsNothing);
    expect(find.textContaining('Ella will speak from'), findsNothing);
    expect(find.textContaining('Phone only'), findsNothing);

    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ella_home_memory_mosaic.png'));
  });

  testWidgets('phone capture transforms in place and finishes the moment', (tester) async {
    final harness = await _pumpHome(tester, conversations: const []);
    addTearDown(harness.dispose);

    final recordTarget = find.byKey(const Key('today-record-moment'));
    expect(tester.getSize(recordTarget).height, greaterThanOrEqualTo(48));
    expect(find.text('Records on this iPhone'), findsOneWidget);
    expect(find.textContaining('Ella is listening'), findsNothing);

    await tester.tap(recordTarget);
    await tester.pump();
    expect(harness.capture.phoneStarts, 1);
    expect(find.text('Listening… tap to finish'), findsOneWidget);

    await tester.tap(recordTarget);
    await tester.pump();
    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finishes, 1);
    expect(find.text('Record a moment'), findsOneWidget);
  });

  testWidgets('phone capture failure stays stopped and explains the missing transcript service', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      phoneStartResult: PhoneCaptureStartResult.transcriptionUnavailable,
    );
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStarts, 1);
    expect(harness.capture.recordingState, RecordingState.stop);
    expect(find.text("Ella couldn't connect to transcription, so recording didn't start."), findsOneWidget);
    expect(find.text('Listening… tap to finish'), findsNothing);
  });

  testWidgets('empty phone capture creates no processing memory', (tester) async {
    final harness = await _pumpHome(tester, conversations: const [], captureHasContent: false);
    addTearDown(harness.dispose);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();

    expect(harness.capture.phoneStops, 1);
    expect(harness.capture.finishes, 0);
    expect(find.text('No words were captured, so no memory was created.'), findsOneWidget);
  });

  testWidgets('Home-owned necklace capture stops its stream before finishing', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true
      ..batteryLevel = 84;
    final harness = await _pumpHome(tester, conversations: const [], device: device);
    addTearDown(harness.dispose);

    expect(find.text('Records with your necklace'), findsOneWidget);
    expect(find.text('Phone only'), findsNothing);
    expect(find.textContaining('Headset is off'), findsNothing);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.deviceStarts, 1);
    expect(find.text('Listening… tap to finish'), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.deviceStops, 1);
    expect(harness.capture.finishes, 1);
    expect(harness.capture.recordingState, RecordingState.stop);
    expect(find.text('Record a moment'), findsOneWidget);
  });

  testWidgets('continuous necklace stream gets exact moment boundaries without being stopped', (tester) async {
    final necklace = BtDevice(name: 'Ella', id: 'necklace-1', type: DeviceType.omi, rssi: -30);
    final device = DeviceProvider()
      ..pairedDevice = necklace
      ..connectedDevice = necklace
      ..isConnected = true;
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      device: device,
      initialRecordingState: RecordingState.deviceRecord,
    );
    addTearDown(harness.dispose);

    expect(find.byKey(const Key('today-necklace-continuous-recording')), findsOneWidget);
    expect(find.text('Your necklace is recording continuously'), findsOneWidget);

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.deviceStarts, 0);
    expect(harness.capture.deviceStops, 0);
    expect(harness.capture.finishes, 1, reason: 'the start tap must exclude pre-tap necklace audio');

    await tester.tap(find.byKey(const Key('today-record-moment')));
    await tester.pump();
    expect(harness.capture.deviceStops, 0, reason: 'Home must preserve a stream it did not start');
    expect(harness.capture.finishes, 2, reason: 'the finish tap closes only the intentional moment');
    expect(harness.capture.recordingState, RecordingState.deviceRecord);
  });

  testWidgets('day-one state is useful and reduced motion removes capture transitions', (tester) async {
    final harness = await _pumpHome(
      tester,
      conversations: const [],
      todayResponse: const TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.newUser,
      ),
      disableAnimations: true,
    );
    addTearDown(harness.dispose);

    expect(find.text('Your first note begins with a moment'), findsOneWidget);
    expect(find.textContaining('note worth returning to'), findsOneWidget);
    expect(find.byKey(const Key('memory-journal-empty')), findsOneWidget);
    expect(find.text('Your journal begins with one moment'), findsOneWidget);
    final container = tester.widget<AnimatedContainer>(
      find.ancestor(of: find.byKey(const Key('today-record-moment')), matching: find.byType(AnimatedContainer)),
    );
    expect(container.duration, Duration.zero);
  });

  testWidgets('Home keeps more than two recent memories in the vertical journal', (tester) async {
    final harness = await _pumpHome(tester, conversations: _ConversationFixtures.manyMemories());
    addTearDown(harness.dispose);

    await tester.scrollUntilVisible(
      find.byKey(const Key('memory-journal-card-memory-4')),
      350,
      scrollable: find.descendant(of: find.byKey(const Key('today-scroll')), matching: find.byType(Scrollable)),
    );

    expect(find.byKey(const Key('memory-journal-card-memory-1')), findsOneWidget);
    expect(find.byKey(const Key('memory-journal-card-memory-2')), findsOneWidget);
    expect(find.byKey(const Key('memory-journal-card-memory-3')), findsOneWidget);
    expect(find.byKey(const Key('memory-journal-card-memory-4')), findsOneWidget);
  });

  testWidgets('200 percent text stays readable and preserves capture semantics at 320 width', (tester) async {
    final semantics = tester.ensureSemantics();
    final harness = await _pumpHome(
      tester,
      conversations: _ConversationFixtures.withMemories(photoBase64: ''),
      viewport: const Size(320, 844),
      textScaler: const TextScaler.linear(2),
      includeBottomNav: true,
    );
    addTearDown(harness.dispose);

    final homeScrollable = tester.state<ScrollableState>(
      find.descendant(of: find.byKey(const Key('today-scroll')), matching: find.byType(Scrollable)),
    );
    homeScrollable.position.jumpTo(0);
    await tester.pump();
    await expectLater(
      find.byType(MaterialApp),
      matchesGoldenFile('goldens/ella_home_memory_mosaic_320_200_full_shell.png'),
    );

    final recordTarget = find.byKey(const Key('today-record-moment'));
    await tester.dragFrom(const Offset(160, 400), const Offset(0, -600));
    await tester.pump();
    expect(recordTarget, findsOneWidget);
    expect(tester.getSize(recordTarget).height, greaterThanOrEqualTo(48));
    expect(find.bySemanticsLabel(RegExp(r'Record a moment.*Records on this iPhone')), findsOneWidget);
    expect(tester.takeException(), isNull);

    await tester.dragFrom(const Offset(160, 400), const Offset(0, -1200));
    await tester.pump();
    expect(find.byKey(const Key('memory-journal-card-memory-1')), findsOneWidget);
    expect(find.byKey(const Key('memory-journal-card-memory-2')), findsOneWidget);
    expect(find.text('Home'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);
    expect(find.text('Talk'), findsOneWidget);
    expect(find.text('Settings'), findsOneWidget);
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });
}

class _HomeHarness {
  const _HomeHarness({
    required this.capture,
    required this.actionItems,
    required this.conversations,
    required this.device,
    required this.home,
    required this.authorityChanges,
  });

  final _FakeCaptureProvider capture;
  final _NoActionsProvider actionItems;
  final _FixtureConversationProvider conversations;
  final DeviceProvider device;
  final HomeProvider home;
  final ValueNotifier<int> authorityChanges;

  void dispose() {
    capture.dispose();
    actionItems.dispose();
    conversations.dispose();
    device.dispose();
    home.dispose();
    authorityChanges.dispose();
  }
}

Future<_HomeHarness> _pumpHome(
  WidgetTester tester, {
  required List<ServerConversation> conversations,
  DeviceProvider? device,
  TodayCardResponse? todayResponse,
  bool includeBottomNav = false,
  bool disableAnimations = false,
  Size viewport = const Size(390, 844),
  TextScaler textScaler = TextScaler.noScaling,
  RecordingState initialRecordingState = RecordingState.stop,
  PhoneCaptureStartResult phoneStartResult = PhoneCaptureStartResult.started,
  bool captureHasContent = true,
}) async {
  tester.view.physicalSize = viewport;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final capture = _FakeCaptureProvider(
    initialRecordingState,
    phoneStartResult: phoneStartResult,
    hasContent: captureHasContent,
  );
  final actionItems = _NoActionsProvider();
  final conversationProvider = _FixtureConversationProvider(conversations);
  final deviceProvider = device ?? DeviceProvider();
  final home = HomeProvider();
  final authorityChanges = ValueNotifier<int>(0);
  final response = todayResponse ??
      TodayCardResponse(
        contractVersion: todayCardContractVersion,
        status: TodayCardStatus.ready,
        card: TodayCard(
          id: 'mosaic',
          version: 1,
          kind: TodayCardKind.memory,
          eyebrow: 'ELLA’S DAILY NOTE',
          headline: 'A small ritual that helped',
          body: 'Morning tea by the window made the day feel calmer.',
          generatedAt: DateTime(2026, 8, 9, 8),
          sourceRefs: const [TodayCardSourceRef(kind: 'hermes_memory', id: 'memory-1', versionId: 'v1')],
        ),
      );

  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<ActionItemsProvider>.value(value: actionItems),
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<ConversationProvider>.value(value: conversationProvider),
        ChangeNotifierProvider<DeviceProvider>.value(value: deviceProvider),
        ChangeNotifierProvider<HomeProvider>.value(value: home),
      ],
      child: MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MediaQuery(
          data: MediaQueryData(
            size: viewport,
            padding: const EdgeInsets.only(top: 47, bottom: 34),
            disableAnimations: disableAnimations,
            textScaler: textScaler,
          ),
          child: Scaffold(
            body: Stack(
              children: [
                TodayPage(
                  nowProvider: () => DateTime(2026, 8, 9, 9, 12),
                  todayCardRepository: _FixedTodayCardRepository(response),
                  todayCardCache: _MemoryTodayCardCache(),
                  todayCardAuthoritySnapshotProvider: () =>
                      (uid: 'test-user', authorityKey: 'test-authority', isProvisioningReady: true),
                  todayCardAuthorityChanges: authorityChanges,
                  guardianAvailability: () => false,
                ),
                if (includeBottomNav) BottomNavBar(onTabTap: (_, __) {}),
              ],
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
  for (final element in find.byType(Image).evaluate()) {
    await tester.runAsync(() => precacheImage((element.widget as Image).image, element));
  }
  await tester.pump(const Duration(milliseconds: 100));

  return _HomeHarness(
    capture: capture,
    actionItems: actionItems,
    conversations: conversationProvider,
    device: deviceProvider,
    home: home,
    authorityChanges: authorityChanges,
  );
}

class _NoActionsProvider extends ActionItemsProvider {
  @override
  List<ActionItemWithMetadata> get actionItems => const [];

  @override
  Future<void> fetchActionItems({bool showShimmer = false}) async {}
}

class _FixtureConversationProvider extends ConversationProvider {
  _FixtureConversationProvider(List<ServerConversation> values) {
    conversations = values;
    hasLoadedConversations = true;
    hasFreshConversations = true;
  }

  @override
  Future<void> ensureFreshConversations() async {}
}

class _FakeCaptureProvider extends CaptureProvider {
  _FakeCaptureProvider(
    RecordingState initialState, {
    required this.phoneStartResult,
    required this.hasContent,
  }) {
    recordingState = initialState;
  }

  final PhoneCaptureStartResult phoneStartResult;
  final bool hasContent;

  int phoneStarts = 0;
  int phoneStops = 0;
  int deviceStarts = 0;
  int deviceStops = 0;
  int finishes = 0;

  @override
  bool get hasCapturableContent => hasContent;

  @override
  Future<PhoneCaptureStartResult> streamRecording() async {
    phoneStarts++;
    updateRecordingState(
      phoneStartResult == PhoneCaptureStartResult.started ? RecordingState.record : RecordingState.stop,
    );
    return phoneStartResult;
  }

  @override
  Future<void> stopStreamRecording() async {
    phoneStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<void> streamDeviceRecording({BtDevice? device}) async {
    deviceStarts++;
    updateRecordingDevice(device);
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    deviceStops++;
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<void> forceProcessingCurrentConversation() async {
    finishes++;
  }
}

class _FixedTodayCardRepository implements TodayCardRepository {
  const _FixedTodayCardRepository(this.response);

  final TodayCardResponse response;

  @override
  Future<TodayCardResponse> fetch({required String uid}) async => response;
}

class _MemoryTodayCardCache implements TodayCardCache {
  TodayCard? card;

  @override
  Future<void> clear({required String uid, String authorityKey = ''}) async => card = null;

  @override
  Future<TodayCardCacheEntry?> read({required String uid, required String authorityKey}) async => null;

  @override
  Future<bool> write({
    required String uid,
    required String authorityKey,
    required TodayCard card,
    required Duration maxAge,
    required bool Function() isCurrent,
  }) async {
    if (!isCurrent()) return false;
    this.card = card;
    return true;
  }
}

class _ConversationFixtures {
  static List<ServerConversation> manyMemories() => List.generate(
        4,
        (index) => ServerConversation(
          id: 'memory-${index + 1}',
          createdAt: DateTime(2026, 8, 8 - index, 9),
          startedAt: DateTime(2026, 8, 8 - index, 9),
          structured: Structured('Memory ${index + 1}', 'A readable memory overview ${index + 1}.'),
        ),
      );

  static List<ServerConversation> withMemories({required String photoBase64}) => [
        ServerConversation(
          id: 'memory-1',
          createdAt: DateTime(2026, 8, 8, 9),
          startedAt: DateTime(2026, 8, 8, 9),
          structured: Structured(
            'Coffee by the window',
            'A slow morning, warm light, and a few minutes that felt entirely your own.',
          ),
          photos: [ConversationPhoto(id: 'photo-1', base64: photoBase64, createdAt: DateTime(2026, 8, 8, 9))],
        ),
        ServerConversation(
          id: 'memory-2',
          createdAt: DateTime(2026, 8, 7, 15),
          startedAt: DateTime(2026, 8, 7, 15),
          structured: Structured(
            'A garden check-in',
            'You noticed the first green tomatoes and made a plan to check again after the weekend.',
          ),
        ),
      ];
}
