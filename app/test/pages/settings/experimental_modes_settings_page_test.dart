import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/models/experimental_mode.dart';
import 'package:omi/pages/settings/experimental_modes_settings_page.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  Future<void> pumpPage(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(430, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const ExperimentalModesSettingsPage(),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('registry modes render and are OFF by default', (tester) async {
    await pumpPage(tester);

    expect(find.text('Einstein'), findsOneWidget);
    expect(find.text('Cyborg'), findsOneWidget);

    final switches = tester.widgetList<Switch>(find.byType(Switch)).toList();
    expect(switches, hasLength(ExperimentalMode.registry.length));
    expect(switches.every((s) => s.value == false), isTrue);

    expect(SharedPreferencesUtil().enabledExperimentalModeIds, isEmpty);
  });

  testWidgets('toggling a mode persists the opt-in selection', (tester) async {
    await pumpPage(tester);

    final einsteinCard = find.byKey(const Key('experimental_mode_card_einstein'));
    final einsteinSwitch = find.descendant(of: einsteinCard, matching: find.byType(Switch));

    await tester.tap(einsteinSwitch);
    await tester.pumpAndSettle();

    expect(SharedPreferencesUtil().enabledExperimentalModeIds, ['einstein']);
    expect(tester.widget<Switch>(einsteinSwitch).value, isTrue);

    final cyborgCard = find.byKey(const Key('experimental_mode_card_cyborg'));
    final cyborgSwitch = find.descendant(of: cyborgCard, matching: find.byType(Switch));
    expect(tester.widget<Switch>(cyborgSwitch).value, isFalse);

    // Toggling back off removes it from the persisted selection.
    await tester.tap(einsteinSwitch);
    await tester.pumpAndSettle();

    expect(SharedPreferencesUtil().enabledExperimentalModeIds, isEmpty);
  });
}
