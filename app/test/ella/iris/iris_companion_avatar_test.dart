import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/capture_host/ella_capture_host.dart';
import 'package:omi/ella/widgets/ella_companion_avatar.dart';

void main() {
  final references = jsonDecode(File('test/ella/iris/fixtures/lume_motion_reference.json').readAsStringSync()) as List;
  for (final raw in references) {
    final state = LumeState.values.byName(raw['state']);
    test('verified JS pose parity ${raw['state']} rm=${raw['reducedMotion']} dark=${raw['dark']}', () {
      final p = LumePose.at(raw['t'], state, reducedMotion: raw['reducedMotion'], baseGlow: raw['dark'] ? .8 : .45);
      final o = raw['o'];
      final pairs = [
        (p.bob, raw['y']),
        (p.ear, o['ear']),
        (p.earLeft, o['earL']),
        (p.eyes, o['eyes']),
        (p.lookX, o['lookX'] ?? 0),
        (p.lookY, o['lookY'] ?? 0),
        (p.squash, o['squash']),
        (p.mouth, o['mouth']),
        (p.tilt, o['tilt']),
        (p.glow, o['glow'])
      ];
      for (final pair in pairs) {
        expect(pair.$1, closeTo((pair.$2 as num).toDouble(), 1e-12));
      }
    });
  }
  test('activity mapping never infers speaking from enabled mode or capture', () {
    for (final capture in [false, true]) {
      for (final pending in [false, true]) {
        expect(
            lumeStateForActivity(captureActive: capture, requestPending: pending, audioPlaying: false),
            pending
                ? LumeState.thinking
                : capture
                    ? LumeState.listening
                    : LumeState.paused);
        expect(lumeStateForActivity(captureActive: capture, requestPending: pending, audioPlaying: true),
            LumeState.speaking);
      }
    }
  });
  test('reduced motion poses invariant across time; paused and speaking cues preserved', () {
    for (final state in LumeState.values) {
      final a = LumePose.at(0, state, reducedMotion: true, baseGlow: .8);
      final b = LumePose.at(1234, state, reducedMotion: true, baseGlow: .8);
      expect([a.bob, a.ear, a.earLeft, a.eyes, a.lookX, a.lookY, a.squash, a.mouth, a.tilt, a.glow],
          [b.bob, b.ear, b.earLeft, b.eyes, b.lookX, b.lookY, b.squash, b.mouth, b.tilt, b.glow]);
    }
    expect(LumePose.at(1, LumeState.paused, reducedMotion: true, baseGlow: .8).eyes, .6);
    expect(LumePose.at(1, LumeState.speaking, reducedMotion: true, baseGlow: .8).mouth, .5);
  });
  test('blink step windows repeat on18-second clock and do not fake state', () {
    for (final t in [1.75, 6.25, 11.35, 16.15, 19.75]) {
      expect(LumePose.at(t, LumeState.listening, reducedMotion: false, baseGlow: .45).eyes, .1);
    }
    expect(LumePose.at(2, LumeState.listening, reducedMotion: false, baseGlow: .45).eyes, 1);
  });
  test('read-only capture seam defaults false and can be removed without hardware action', () {
    EllaCaptureHost.resetForTesting();
    expect(EllaCaptureHost.companionRecordingActive, false);
    int reads = 0;
    EllaCaptureHost.installCompanionRecordingReader(() {
      reads++;
      return true;
    });
    expect(EllaCaptureHost.companionRecordingActive, true);
    expect(reads, 1);
    EllaCaptureHost.resetForTesting();
    expect(EllaCaptureHost.companionRecordingActive, false);
  });
  testWidgets('real state reader changes pose even with Reduce Motion; no demo cycle', (tester) async {
    var state = LumeState.paused;
    await tester.pumpWidget(MaterialApp(
        home: MediaQuery(
            data: const MediaQueryData(disableAnimations: true),
            child: Scaffold(body: EllaCompanionAvatar(stateReader: () => state)))));
    LumePainter painter() => tester
        .widget<CustomPaint>(find.descendant(of: find.byType(EllaCompanionAvatar), matching: find.byType(CustomPaint)))
        .painter! as LumePainter;
    expect(painter().state, LumeState.paused);
    expect(painter().seconds.value, 0);
    state = LumeState.listening;
    await tester.pump(const Duration(milliseconds: 250));
    expect(painter().state, LumeState.listening);
    expect(painter().seconds.value, 0);
    await tester.pump(const Duration(seconds: 20));
    expect(painter().state, LumeState.listening);
    expect(tester.getSize(find.byType(EllaCompanionAvatar)), const Size(80, 72));
    await tester.pumpWidget(const SizedBox.shrink());
  });
  testWidgets('offscreen and background stop ticker; resume does not replay state sequence', (tester) async {
    Widget host(bool visible) =>
        MaterialApp(home: TickerMode(enabled: visible, child: const EllaCompanionAvatar(state: LumeState.listening)));
    await tester.pumpWidget(host(true));
    await tester.pump(const Duration(seconds: 1));
    LumePainter painter() => tester
        .widget<CustomPaint>(find.descendant(of: find.byType(EllaCompanionAvatar), matching: find.byType(CustomPaint)))
        .painter! as LumePainter;
    await tester.pumpWidget(host(false));
    final held = painter().seconds.value;
    await tester.pump(const Duration(seconds: 20));
    expect(painter().seconds.value, held);
    await tester.pumpWidget(host(true));
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    final background = painter().seconds.value;
    await tester.pump(const Duration(seconds: 20));
    expect(painter().seconds.value, background);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    expect(painter().state, LumeState.listening);
    expect(painter().seconds.value, greaterThan(background));
    await tester.pumpWidget(const SizedBox.shrink());
  });
  for (final dark in [false, true]) {
    for (final scale in [1.0, 3.0]) {
      testWidgets('66px header remains bounded and decorative dark=$dark text=$scale', (tester) async {
        await tester.binding.setSurfaceSize(const Size(320, 180));
        addTearDown(() => tester.binding.setSurfaceSize(null));
        await tester.pumpWidget(MaterialApp(
            theme: ThemeData(brightness: dark ? Brightness.dark : Brightness.light),
            home: MediaQuery(
                data: MediaQueryData(textScaler: TextScaler.linear(scale), disableAnimations: true),
                child: Scaffold(
                  appBar: AppBar(
                      toolbarHeight: 72,
                      leading: const BackButton(),
                      centerTitle: true,
                      title: Row(mainAxisSize: MainAxisSize.min, children: [
                        const EllaCompanionAvatar(),
                        const SizedBox(width: 8),
                        ConstrainedBox(
                            constraints: const BoxConstraints(maxWidth: 140),
                            child: const Text('Ella', style: TextStyle(fontSize: 16), overflow: TextOverflow.ellipsis)),
                      ])),
                ))));
        await tester.pumpAndSettle();
        expect(tester.getSize(find.byType(EllaCompanionAvatar)), const Size(80, 72));
        expect(find.descendant(of: find.byType(EllaCompanionAvatar), matching: find.byType(ExcludeSemantics)),
            findsOneWidget);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }
  testWidgets('isolated painter exports exact source reference poses', (tester) async {
    final dir = Platform.environment['IRIS_ANIMATED_LUME_RENDER_DIR'];
    if (dir == null) return;
    for (final raw in references) {
      final boundary = GlobalKey();
      final seconds = ValueNotifier<double>(1.75);
      await tester.binding.setSurfaceSize(const Size(80, 72));
      await tester.pumpWidget(RepaintBoundary(
          key: boundary,
          child: ColoredBox(
              color: raw['dark'] ? const Color(0xff08080f) : const Color(0xffeef0f6),
              child: CustomPaint(
                  painter: LumePainter(
                      seconds: seconds,
                      state: LumeState.values.byName(raw['state']),
                      reducedMotion: raw['reducedMotion'],
                      avatarSize: 66,
                      baseGlow: raw['dark'] ? .8 : .45)))));
      await tester.pump();
      await tester.runAsync(() async {
        final image =
            await (boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary).toImage(pixelRatio: 3);
        final data = await image.toByteData(format: ui.ImageByteFormat.png);
        await File(
                '$dir/implementation-${raw['state']}-${raw['reducedMotion'] ? 'reduced' : 'motion'}-${raw['dark'] ? 'dark' : 'light'}.png')
            .writeAsBytes(data!.buffer.asUint8List());
        image.dispose();
      });
      await tester.pumpWidget(const SizedBox.shrink());
      seconds.dispose();
    }
    await tester.binding.setSurfaceSize(null);
  });
}
