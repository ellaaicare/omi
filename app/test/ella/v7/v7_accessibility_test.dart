import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:omi/ella/widgets/ella_v7_appearance.dart';
import 'package:omi/ella/widgets/iris_capture_controls.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  test('clock has continuous30minute transitions, fixed choices and no location source', () {
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 6, 45), EllaV7Appearance.dayNight), 1);
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 7), EllaV7Appearance.dayNight), .5);
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 7, 15), EllaV7Appearance.dayNight), 0);
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 19), EllaV7Appearance.dayNight), .5);
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 1), EllaV7Appearance.light), 0);
    expect(ellaV7NightAmount(DateTime(2026, 10, 11, 12), EllaV7Appearance.dark), 1);
  });
  test('twilight ink retains contrast on both blended surfaces throughout transition', () {
    double ratio(Color a, Color b) {
      final x = a.computeLuminance(), y = b.computeLuminance();
      return ((x > y ? x : y) + .05) / ((x < y ? x : y) + .05);
    }

    for (var step = 0; step <= 100; step++) {
      final p = EllaPalette(step >= 50, nightMix: step / 100);
      for (final ink in [p.ink, p.inkSoft]) {
        for (final surface in [p.paper, p.card, p.elevatedCard])
          expect(ratio(ink, surface), greaterThanOrEqualTo(4.5), reason: 'step=$step');
      }
    }
  });
  testWidgets('native accessibility read changes glass, malformed/unavailable reads stay opaque', (tester) async {
    const channel = MethodChannel('com.ella.v7/accessibility');
    Object? settings = {'reduceTransparency': false, 'increaseContrast': false};
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, (_) async => settings);
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await EllaV7Accessibility.shared.refresh();
    await tester.pumpWidget(
        MaterialApp(theme: ellaThemeData(), home: const Scaffold(body: EllaV7Glass(child: Text('synthetic')))));
    expect(tester.widget<BackdropFilter>(find.byType(BackdropFilter)).enabled, isTrue);
    settings = {'reduceTransparency': true, 'increaseContrast': false};
    await EllaV7Accessibility.shared.refresh();
    await tester.pump();
    expect(tester.widget<BackdropFilter>(find.byType(BackdropFilter)).enabled, isFalse);
    settings = {'reduceTransparency': false};
    await EllaV7Accessibility.shared.refresh();
    await tester.pump();
    expect(tester.widget<BackdropFilter>(find.byType(BackdropFilter)).enabled, isFalse);
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (_) async => throw MissingPluginException());
    await EllaV7Accessibility.shared.refresh();
    expect(EllaV7Accessibility.shared.value, isTrue);
  });
  testWidgets('accessibility surface change retains editor, draft, selection and focus', (tester) async {
    const channel = MethodChannel('com.ella.v7/accessibility');
    var reduce = false;
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (_) async => {'reduceTransparency': reduce, 'increaseContrast': false});
    final text = TextEditingController();
    addTearDown(text.dispose);
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await EllaV7Accessibility.shared.refresh();
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: EllaV7Glass(child: TextField(controller: text)))));
    final field = find.byType(TextField);
    await tester.showKeyboard(field);
    await tester.enterText(field, 'synthetic unsent draft');
    text.selection = const TextSelection.collapsed(offset: 9);
    await tester.pump();
    final editor = tester.state<EditableTextState>(find.byType(EditableText));
    reduce = true;
    await EllaV7Accessibility.shared.refresh();
    await tester.pump();
    expect(identical(editor, tester.state<EditableTextState>(find.byType(EditableText))), isTrue);
    expect(text.text, 'synthetic unsent draft');
    expect(text.selection.extentOffset, 9);
    expect(editor.widget.focusNode.hasFocus, isTrue);
    await tester.pumpWidget(const SizedBox.shrink());
  });
  testWidgets('twilight uses opaque surfaces even when native transparency is enabled', (tester) async {
    const channel = MethodChannel('com.ella.v7/accessibility');
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (_) async => {'reduceTransparency': false, 'increaseContrast': false});
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await EllaV7Accessibility.shared.refresh();
    await tester.pumpWidget(MaterialApp(
        home: Theme(
            data: ellaThemeData().copyWith(extensions: [const EllaV7Tone(.5)]),
            child: const Scaffold(body: EllaV7Glass(child: Text('synthetic twilight'))))));
    final filter = tester.widget<BackdropFilter>(find.byType(BackdropFilter));
    expect(filter.enabled, isFalse);
    final surface = tester
        .widget<DecoratedBox>(find.descendant(of: find.byType(EllaV7Glass), matching: find.byType(DecoratedBox)).first);
    expect((surface.decoration as BoxDecoration).color!.a, 1);
  });
  for (final dark in [false, true]) {
    testWidgets('appearance choices retain readable ink dark=$dark', (tester) async {
      await tester.pumpWidget(MaterialApp(
          theme: dark ? ellaDarkThemeData() : ellaThemeData(),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: const Scaffold(body: EllaV7AppearancePicker())));
      double contrast(Color a, Color b) {
        final x = a.computeLuminance(), y = b.computeLuminance();
        return ((x > y ? x : y) + .05) / ((x < y ? x : y) + .05);
      }

      final chips = tester.widgetList<ChoiceChip>(find.byType(ChoiceChip));
      expect(chips.length, 3);
      for (final chip in chips) {
        final ink = (chip.label as Text).style!.color!;
        final surface = chip.selected ? chip.selectedColor! : chip.backgroundColor!;
        expect(contrast(ink, surface), greaterThanOrEqualTo(4.5));
      }
    });
    test('v7 primary and secondary text contrast dark=$dark', () {
      final p = EllaPalette(dark);
      double contrast(Color a, Color b) {
        final x = a.computeLuminance(), y = b.computeLuminance();
        return ((x > y ? x : y) + .05) / ((x < y ? x : y) + .05);
      }

      expect(contrast(p.ink, p.card), greaterThanOrEqualTo(4.5));
      expect(contrast(p.inkSoft, p.card), greaterThanOrEqualTo(4.5));
      expect(contrast(p.paper, p.ink), greaterThanOrEqualTo(4.5));
    });
    for (final scale in [1.0, 1.25, 3.5]) {
      for (final state in IrisListeningState.values) {
        testWidgets('transparent header retains status semantics and44pt targets dark=$dark scale=$scale state=$state',
            (tester) async {
          tester.view.physicalSize = const Size(393, 852);
          tester.view.devicePixelRatio = 1;
          addTearDown(tester.view.resetPhysicalSize);
          addTearDown(tester.view.resetDevicePixelRatio);
          final owner = Object();
          final c = IrisCaptureControls.shared;
          var disconnected = state == IrisListeningState.unavailable;
          c.register(owner,
              status: () async {},
              whispers: () async {},
              necklace: () async {},
              listening: () => state,
              whispersEnabled: () => true,
              necklaceDisconnected: () => disconnected);
          addTearDown(() => c.unregister(owner));
          double extent = 0;
          await tester.pumpWidget(MaterialApp(
              theme: dark ? ellaDarkThemeData() : ellaThemeData(),
              localizationsDelegates: AppLocalizations.localizationsDelegates,
              supportedLocales: AppLocalizations.supportedLocales,
              builder: (context, child) => MediaQuery(
                  data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                  child: child!),
              home: Scaffold(body: Builder(builder: (context) {
                extent = EllaV7ChatIdentity.extent(context);
                return Stack(children: [
                  Positioned(top: 59, left: 8, right: 8, height: extent, child: const EllaV7ChatIdentity())
                ]);
              }))));
          await tester.pump(const Duration(milliseconds: 100));
          expect(tester.takeException(), isNull);
          final status = find.byKey(const Key('v7-chat-companion-status'));
          expect(
              find.descendant(of: find.byType(EllaV7ChatIdentity), matching: find.byType(EllaV7Glass)), findsNothing);
          final rect = tester.getRect(status);
          expect(rect.height, greaterThanOrEqualTo(44));
          expect(rect.bottom, lessThanOrEqualTo(59 + extent));
          expect(find.textContaining('Whispering'), findsNothing,
              reason: 'enabled preference is not actual audio output');
          disconnected = false;
          c.changed();
          await tester.pump();
          expect(tester.takeException(), isNull);
          await tester.pumpWidget(const SizedBox.shrink());
        });
      }
    }
  }
  testWidgets('reduced transparency selects opaque glass and disables backdrop filtering', (tester) async {
    await tester.pumpWidget(MaterialApp(
        home: MediaQuery(
            data: const MediaQueryData(highContrast: true), child: const EllaV7Glass(child: Text('fixture')))));
    expect(tester.widget<BackdropFilter>(find.byType(BackdropFilter)).enabled, isFalse);
    expect(tester.takeException(), isNull);
  });
}
