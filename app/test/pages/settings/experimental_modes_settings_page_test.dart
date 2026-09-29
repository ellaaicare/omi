import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/models/experimental_mode.dart';
import 'package:omi/pages/settings/experimental_modes_settings_page.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('resolveCoreWhisperMode', () {
    test('defaults to MEMORY_SUPPORT (G4 product decision), not ACTIVE_SUPPORT', () {
      expect(resolveCoreWhisperMode(const []), 'MEMORY_SUPPORT');
      expect(defaultCoreWhisperMode, 'MEMORY_SUPPORT');
    });

    test('picks whichever core mode is present in the saved features', () {
      expect(resolveCoreWhisperMode(const ['MEMORY_SUPPORT']), 'MEMORY_SUPPORT');
      expect(resolveCoreWhisperMode(const ['ACTIVE_SUPPORT']), 'ACTIVE_SUPPORT');
      expect(resolveCoreWhisperMode(const ['EMERGENCY_ONLY']), 'EMERGENCY_ONLY');
    });

    test('falls back to the default for an unrelated or absent feature set', () {
      // Whispers off entirely.
      expect(resolveCoreWhisperMode(const []), defaultCoreWhisperMode);
      // A feature/override outside the core three (e.g. MAXIMUM_AWARENESS).
      expect(resolveCoreWhisperMode(const ['MAXIMUM_AWARENESS']), defaultCoreWhisperMode);
    });

    test('core mode key order is memory_support, active_support, emergency_only', () {
      expect(coreWhisperModeKeys, ['MEMORY_SUPPORT', 'ACTIVE_SUPPORT', 'EMERGENCY_ONLY']);
    });
  });

  Future<void> pumpPage(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(430, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: ExperimentalModesSettingsPage(),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('registry modes render as unavailable previews', (tester) async {
    await pumpPage(tester);

    expect(find.text('Whispers & Experimental Modes'), findsOneWidget);
    expect(find.text('Experimental modes'), findsOneWidget);
    expect(find.text('Einstein'), findsOneWidget);
    expect(find.text('Cyborg'), findsOneWidget);

    final switches = tester.widgetList<Switch>(find.byType(Switch)).toList();
    expect(switches, hasLength(ExperimentalMode.registry.length));
    expect(switches.every((s) => s.value == false), isTrue);
    expect(switches.every((s) => s.onChanged == null), isTrue);
    expect(find.text('Preview, Coming Soon'), findsNWidgets(ExperimentalMode.registry.length));
  });

  testWidgets(
      'this is one consolidated settings surface: core Whispers modes stay build-gated, '
      'experimental modes never are', (tester) async {
    await pumpPage(tester);

    // The core Whispers mode picker is gated by the same build/identity
    // policy as the rest of the Guardian surface (allowsGuardianCareSurface),
    // which is unavailable in the default test build (no ELLA_GUARDIAN_ENABLED,
    // no authenticated guardian identity). It must not appear as a second,
    // separate surface elsewhere - and it must not crash or block the
    // experimental modes section when unavailable.
    expect(find.text('Whispers mode'), findsNothing);
    for (final key in coreWhisperModeKeys) {
      expect(find.byKey(Key('core_whisper_mode_row_$key')), findsNothing);
    }

    // The opt-in experimental modes section is never gated by that policy.
    expect(find.text('Experimental modes'), findsOneWidget);
    expect(find.byKey(const Key('experimental_mode_card_einstein')), findsOneWidget);
    expect(find.byKey(const Key('experimental_mode_card_cyborg')), findsOneWidget);
  });

  testWidgets('tapping a preview cannot enable a mode', (tester) async {
    await pumpPage(tester);

    final einsteinCard = find.byKey(const Key('experimental_mode_card_einstein'));
    final einsteinSwitch = find.descendant(of: einsteinCard, matching: find.byType(Switch));

    await tester.tap(einsteinSwitch);
    await tester.pumpAndSettle();

    expect(tester.widget<Switch>(einsteinSwitch).value, isFalse);
    expect(tester.widget<Switch>(einsteinSwitch).onChanged, isNull);

    final cyborgCard = find.byKey(const Key('experimental_mode_card_cyborg'));
    final cyborgSwitch = find.descendant(of: cyborgCard, matching: find.byType(Switch));
    expect(tester.widget<Switch>(cyborgSwitch).value, isFalse);
    expect(tester.widget<Switch>(cyborgSwitch).onChanged, isNull);
  });
}
