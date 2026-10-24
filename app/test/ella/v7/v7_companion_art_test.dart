import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_companion_card.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/widgets/ella_v7_companion_art.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    await (FontLoader('.SF UI Text')
          ..addFont((File('/System/Library/Fonts/SFNS.ttf').existsSync()
              ? File('/System/Library/Fonts/SFNS.ttf').readAsBytes().then(ByteData.sublistView)
              : rootBundle.load('assets/fonts/Manrope-400.ttf'))))
        .load();
    await (FontLoader('InstrumentSerif')
          ..addFont(rootBundle.load('assets/fonts/instrument-serif/InstrumentSerif-Regular.ttf')))
        .load();
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Medium.ttf')))
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
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'offline-avatar-fixture'});
    await SharedPreferencesUtil.init();
  });
  test('shape is account-local, validated, and preserved by legacy saves', () async {
    final p = EllaV7CompanionPreferences.shared;
    expect(p.styleIndex, 0);
    expect(await p.save(uid: 'offline-avatar-fixture', name: 'Fixture', colorIndex: 1, styleIndex: 2), true);
    expect(await p.save(uid: 'offline-avatar-fixture', name: 'Renamed', colorIndex: 0), true);
    expect(p.styleIndex, 2);
    expect(await p.save(uid: 'offline-avatar-fixture', name: 'Bad', colorIndex: 0, styleIndex: 3), false);
    SharedPreferencesUtil().uid = 'other-fixture';
    expect(p.styleIndex, 0);
    expect(await p.save(uid: 'offline-avatar-fixture', name: 'Stale', colorIndex: 0, styleIndex: 1), false);
  });
  testWidgets('reduced motion, background and TickerMode freeze avatar; input remains reactive', (tester) async {
    Widget host({bool reduced = false, bool visible = true, bool input = false}) => MaterialApp(
        home: MediaQuery(
            data: MediaQueryData(disableAnimations: reduced),
            child: TickerMode(enabled: visible, child: EllaV7Lume(inputActive: input))));
    EllaV7CompanionPainter painter() => tester
        .widget<CustomPaint>(find.byWidgetPredicate((w) => w is CustomPaint && w.painter is EllaV7CompanionPainter))
        .painter! as EllaV7CompanionPainter;
    await tester.pumpWidget(host());
    await tester.pump(const Duration(milliseconds: 600));
    expect(painter().moving, true);
    expect(painter().phase, greaterThan(0));
    await tester.pumpWidget(host(reduced: true, input: true));
    expect(painter().moving, false);
    expect(painter().phase, 0);
    expect(painter().inputActive, true);
    await tester.pumpWidget(host(visible: false));
    expect(painter().moving, false);
    await tester.pumpWidget(host());
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump();
    expect(painter().moving, false);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    expect(painter().moving, true);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets('offline actual header and shared editor fixture', (tester) async {
    tester.view.physicalSize = const Size(393, 852);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final key = GlobalKey();
    await tester.pumpWidget(RepaintBoundary(
        key: key,
        child: MaterialApp(
            debugShowCheckedModeBanner: false,
            theme: ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) =>
                MediaQuery(data: MediaQuery.of(context).copyWith(disableAnimations: true), child: child!),
            home: Scaffold(
                body: SafeArea(
                    child: Padding(
                        padding: const EdgeInsets.all(20),
                        child: Column(children: [
                          const Row(
                              mainAxisAlignment: MainAxisAlignment.spaceBetween,
                              children: [EllaV7ChatIdentity(inputActive: true), Icon(Icons.settings_outlined)]),
                          const SizedBox(height: 24),
                          const EllaV7CompanionCard(),
                        ])))))));
    await tester.pumpAndSettle();
    expect(tester.getSize(find.byType(EllaV7ChatIdentity)).height, 48);
    Future<void> capture(String name) async {
      await tester.runAsync(() async {
        final image = await (key.currentContext!.findRenderObject() as RenderRepaintBoundary).toImage();
        final bytes = (await image.toByteData(format: ui.ImageByteFormat.png))!.buffer.asUint8List();
        File('test/ella/v7/avatar-proof/$name.png').writeAsBytesSync(bytes);
        image.dispose();
      });
    }

    Directory('test/ella/v7/avatar-proof').createSync(recursive: true);
    await capture('header-and-style-entry');
    await tester.tap(find.byKey(const Key('v7-companion-customize')));
    await tester.pumpAndSettle();
    final shape = find.byKey(const Key('v7-companion-shape-1'));
    await tester.ensureVisible(shape);
    await tester.tap(shape);
    await tester.pumpAndSettle();
    await capture('style-editor-leaf');
    await tester.tap(find.byKey(const Key('v7-companion-save')));
    await tester.pumpAndSettle();
    expect(EllaV7CompanionPreferences.shared.styleIndex, 1);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });
  test('offline rendered shapes preserve alpha and produce distinct blink/input frames', () async {
    final dir = Directory('test/ella/v7/avatar-proof')..createSync(recursive: true);
    final hashes = <int>[];
    for (var style = 0; style < 3; style++) {
      for (final frame in ['idle', 'blink', 'typing']) {
        final recorder = ui.PictureRecorder();
        final canvas = Canvas(recorder);
        EllaV7CompanionPainter(
                phase: frame == 'blink' ? .81 : .2,
                styleIndex: style,
                pose: 'idle',
                inputActive: frame == 'typing',
                moving: true)
            .paint(canvas, const Size(144, 144));
        final picture = recorder.endRecording();
        final image = await picture.toImage(144, 144);
        final raw = (await image.toByteData(format: ui.ImageByteFormat.rawRgba))!;
        expect(raw.getUint8(3), 0); // top-left pixel must be transparent.
        expect(raw.getUint8((143 * 144 + 143) * 4 + 3), 0);
        expect(raw.getUint8((50 * 144 + 72) * 4 + 3), greaterThan(0));
        final bytes = (await image.toByteData(format: ui.ImageByteFormat.png))!.buffer.asUint8List();
        File('${dir.path}/style-$style-$frame.png').writeAsBytesSync(bytes);
        hashes.add(Object.hashAll(bytes));
        image.dispose();
        picture.dispose();
      }
    }
    expect(hashes.toSet().length, 9);
  });
}
