import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/home/today_page.dart';

void main() {
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-800.ttf')))
        .load();
    var flutterCache = File(Platform.resolvedExecutable).parent;
    while (!File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      flutterCache = flutterCache.parent;
    }
    final materialIcons = File('${flutterCache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf');
    await (FontLoader('MaterialIcons')
          ..addFont(materialIcons.readAsBytes().then((bytes) => ByteData.sublistView(Uint8List.fromList(bytes)))))
        .load();
  });

  Future<void> pumpStatusSurfaces(
    WidgetTester tester, {
    required bool necklaceConnected,
    required bool necklaceConnecting,
    required bool headsetConnected,
    required bool usesPhoneSpeaker,
  }) async {
    tester.view.physicalSize = const Size(430, 620);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final actionable = !necklaceConnected || usesPhoneSpeaker;
    await tester.pumpWidget(
      MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: ellaThemeData(),
        locale: const Locale('en'),
        localizationsDelegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(EllaSizes.screenPadding),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TodayStatusStrip(
                    necklaceConnected: necklaceConnected,
                    necklaceConnecting: necklaceConnecting,
                    batteryLevel: 96,
                    deviceType: DeviceType.omi,
                    headsetConnected: headsetConnected,
                    audioOutputName: 'Ella headset',
                    onTap: () {},
                  ),
                  if (actionable) ...[
                    const SizedBox(height: EllaSizes.cardGap),
                    TodayActionableDeviceCard(
                      necklaceConnected: necklaceConnected,
                      necklaceConnecting: necklaceConnecting,
                      deviceType: DeviceType.omi,
                      usesPhoneSpeaker: usesPhoneSpeaker,
                      onReconnect: () {},
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
    final imageElements = find.byType(Image).evaluate().toList();
    await tester.runAsync(
      () => Future.wait(
        imageElements.map(
          (element) => precacheImage((element.widget as Image).image, element),
        ),
      ),
    );
    // Bounded pumps: the reconnecting state hosts the breathing dot, whose
    // looped animation never settles.
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));
  }

  testWidgets('healthy status strip screenshot', (tester) async {
    await pumpStatusSurfaces(
      tester,
      necklaceConnected: true,
      necklaceConnecting: false,
      headsetConnected: true,
      usesPhoneSpeaker: false,
    );
    expect(find.byType(TodayActionableDeviceCard), findsNothing);
    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ux_1106_status_strip_healthy.png'));
  });

  testWidgets('actionable reconnecting with loudspeaker warning screenshot', (tester) async {
    await pumpStatusSurfaces(
      tester,
      necklaceConnected: false,
      necklaceConnecting: true,
      headsetConnected: false,
      usesPhoneSpeaker: true,
    );
    expect(find.text('Headset is off — Ella will speak from the phone.'), findsOneWidget);
    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ux_1106_actionable_devices.png'));
  });
}
