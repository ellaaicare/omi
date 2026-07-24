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

  Future<void> pumpHardwareCard(
    WidgetTester tester, {
    required bool headsetConnected,
    required bool usesPhoneSpeaker,
  }) async {
    tester.view.physicalSize = const Size(430, 620);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

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
              child: TodayHardwareStatusCard(
                necklaceConnected: true,
                necklaceConnecting: false,
                batteryLevel: 96,
                deviceType: DeviceType.omi,
                fallbackDeviceImagePath: 'assets/images/omi-devkit-without-rope.png',
                headsetConnected: headsetConnected,
                audioOutputName: 'Ella headset',
                usesPhoneSpeaker: usesPhoneSpeaker,
                onOpenNecklace: () {},
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
    await tester.pumpAndSettle();
  }

  testWidgets('connected hardware card screenshot', (tester) async {
    await pumpHardwareCard(tester, headsetConnected: true, usesPhoneSpeaker: false);
    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ux_1098_hardware_connected.png'));
  });

  testWidgets('phone speaker warning screenshot', (tester) async {
    await pumpHardwareCard(tester, headsetConnected: false, usesPhoneSpeaker: true);
    await expectLater(find.byType(MaterialApp), matchesGoldenFile('goldens/ux_1098_phone_speaker_warning.png'));
  });
}
