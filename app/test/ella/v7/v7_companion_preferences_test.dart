import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_appearance.dart';
import 'package:omi/ella/widgets/ella_v7_companion_card.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'synthetic-a'});
    await SharedPreferencesUtil.init();
  });
  for (final night in [0.0, .5, 1.0]) {
    for (final scale in [1.0, 3.5]) {
      testWidgets('actual style-chip ink/fill and actions night=$night scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(393, 852);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final theme =
            ThemeData.lerp(ellaThemeData(), ellaDarkThemeData(), night).copyWith(extensions: [EllaV7Tone(night)]);
        await tester.pumpWidget(MaterialApp(
            theme: theme,
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
                data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(scale), disableAnimations: true),
                child: child!),
            home: const Scaffold(body: SingleChildScrollView(child: EllaV7CompanionCard()))));
        await tester.tap(find.byKey(const Key('v7-companion-customize')));
        await tester.pumpAndSettle();
        double ratio(Color a, Color b) {
          final x = a.computeLuminance(), y = b.computeLuminance();
          return ((x > y ? x : y) + .05) / ((x < y ? x : y) + .05);
        }

        for (var selected = 0; selected < 3; selected++) {
          final target = find.byKey(Key('v7-companion-shape-$selected'));
          await tester.ensureVisible(target);
          await tester.pumpAndSettle();
          await tester.tap(target);
          await tester.pumpAndSettle();
          for (var i = 0; i < 3; i++) {
            final chip = find.byKey(Key('v7-companion-shape-$i'));
            final raw = tester.widget<ChoiceChip>(chip);
            expect(raw.selected, i == selected);
            final ink =
                tester.widget<RichText>(find.descendant(of: chip, matching: find.byType(RichText))).text.style!.color!;
            final fill = (tester.widget<Ink>(find.descendant(of: chip, matching: find.byType(Ink))).decoration
                    as ShapeDecoration)
                .color!;
            expect(ratio(ink, fill), greaterThanOrEqualTo(4.5),
                reason: 'actual rendered style chip$i selected=$selected');
          }
        }
        final save = find.byKey(const Key('v7-companion-save'));
        await tester.ensureVisible(save);
        await tester.pumpAndSettle();
        expect(tester.getSize(save).height, greaterThanOrEqualTo(44));
        await tester.tap(save);
        await tester.pumpAndSettle();
        expect(EllaV7CompanionPreferences.shared.styleIndex, 2);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }
  test('decoration is account-local and a stale editor cannot write the new account', () async {
    final style = EllaV7CompanionPreferences.shared;
    expect(await style.save(uid: 'synthetic-a', name: 'Synthetic Lume', colorIndex: 2), isTrue);
    SharedPreferencesUtil().uid = 'synthetic-b';
    expect(style.name, isEmpty);
    expect(style.colorIndex, 0);
    expect(await style.save(uid: 'synthetic-a', name: 'Stale', colorIndex: 1), isFalse);
    expect(style.name, isEmpty);
    SharedPreferencesUtil().uid = 'synthetic-a';
    expect(style.name, 'Synthetic Lume');
    expect(style.colorIndex, 2);
  });
  testWidgets('companion semantic tap opens the editor', (tester) async {
    final semantics = tester.ensureSemantics();
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) =>
            MediaQuery(data: MediaQuery.of(context).copyWith(disableAnimations: true), child: child!),
        home: const Scaffold(body: EllaV7CompanionCard())));

    final card = find.byType(EllaV7CompanionCard);
    final node = tester.getSemantics(card);
    expect(node.getSemanticsData().hasAction(SemanticsAction.tap), isTrue);
    tester.binding.performSemanticsAction(
        SemanticsActionEvent(type: SemanticsAction.tap, viewId: tester.view.viewId, nodeId: node.id));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('v7-companion-name')), findsOneWidget);
    semantics.dispose();
  });
  testWidgets('dark companion editor keeps field, counter and Save ink readable', (tester) async {
    await tester.pumpWidget(MaterialApp(
        theme: ellaDarkThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) =>
            MediaQuery(data: MediaQuery.of(context).copyWith(disableAnimations: true), child: child!),
        home: const Scaffold(body: EllaV7CompanionCard())));
    await tester.tap(find.byKey(const Key('v7-companion-customize')));
    await tester.pumpAndSettle();
    double contrast(Color a, Color b) {
      final x = a.computeLuminance(), y = b.computeLuminance();
      return ((x > y ? x : y) + .05) / ((x < y ? x : y) + .05);
    }

    final field = tester.widget<TextField>(find.byKey(const Key('v7-companion-name')));
    final surface = field.decoration!.fillColor!;
    for (final ink in [
      field.style!.color!,
      field.decoration!.labelStyle!.color!,
      field.decoration!.counterStyle!.color!
    ]) {
      expect(contrast(ink, surface), greaterThanOrEqualTo(4.5));
    }
    final save = tester.widget<FilledButton>(find.byKey(const Key('v7-companion-save')));
    expect(contrast(save.style!.foregroundColor!.resolve({})!, save.style!.backgroundColor!.resolve({})!),
        greaterThanOrEqualTo(4.5));
  });
  testWidgets('large-text companion editor keeps name, color and save reachable with a simulated keyboard inset',
      (tester) async {
    tester.view.physicalSize = const Size(393, 852);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    await tester.pumpWidget(MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(2), disableAnimations: true),
            child: child!),
        home: const Scaffold(body: SingleChildScrollView(child: EllaV7CompanionCard()))));
    await tester.tap(find.byKey(const Key('v7-companion-customize')));
    await tester.pumpAndSettle();
    final name = find.byKey(const Key('v7-companion-name'));
    await tester.ensureVisible(name);
    await tester.enterText(name, 'Synthetic Nova');
    tester.view.viewInsets = const FakeViewPadding(bottom: 320);
    await tester.pump();
    primaryFocus?.unfocus();
    tester.view.viewInsets = const FakeViewPadding();
    await tester.pumpAndSettle();
    final color = find.byKey(const Key('v7-companion-color-2'));
    await tester.ensureVisible(color);
    await tester.pumpAndSettle();
    await tester.tap(color);
    await tester.pump();
    final save = find.byKey(const Key('v7-companion-save'));
    await tester.ensureVisible(save);
    await tester.pumpAndSettle();
    expect(tester.getSize(save).height, greaterThanOrEqualTo(44));
    await tester.tap(save);
    await tester.pumpAndSettle();
    expect(EllaV7CompanionPreferences.shared.name, 'Synthetic Nova');
    expect(EllaV7CompanionPreferences.shared.colorIndex, 2);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
