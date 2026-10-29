import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/device/device_page_header.dart';

Widget _wrap(Widget child) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: child),
    );

void main() {
  testWidgets('no Reconnect control without a callback (stock behavior)', (tester) async {
    await tester.pumpWidget(_wrap(const DeviceDisconnectedCard()));
    expect(find.byKey(const Key('device_reconnect_button')), findsNothing);
  });

  testWidgets('Reconnect control runs the user reconnect once per tap', (tester) async {
    var taps = 0;
    await tester.pumpWidget(_wrap(DeviceDisconnectedCard(onReconnect: () => taps++)));
    await tester.tap(find.byKey(const Key('device_reconnect_button')));
    await tester.pump();
    expect(taps, 1);
  });
}
