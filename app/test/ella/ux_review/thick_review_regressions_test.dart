import 'dart:io';
import 'dart:ui' as ui;
import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:firebase_core_platform_interface/firebase_core_platform_interface.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_auth_platform_interface/firebase_auth_platform_interface.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_companion_card.dart';
import 'package:omi/ella/widgets/v2v_fallback_dialog.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/pages/onboarding/ella/ella_welcome.dart';
import 'package:omi/l10n/app_localizations.dart';

class _Core extends FirebasePlatform {
  final fixture = FirebaseAppPlatform(
      '[DEFAULT]',
      const FirebaseOptions(
          apiKey: 'fictional', appId: 'fictional', messagingSenderId: 'fictional', projectId: 'fictional'));
  @override
  FirebaseAppPlatform app([String name = '[DEFAULT]']) => fixture;
  @override
  List<FirebaseAppPlatform> get apps => [fixture];
}

class _Auth extends FirebaseAuthPlatform {
  @override
  FirebaseAuthPlatform delegateFor({required FirebaseApp app}) => this;
  @override
  FirebaseAuthPlatform setInitialValues({PigeonUserDetails? currentUser, String? languageCode}) => this;
  @override
  UserPlatform? get currentUser => null;
}

const receipt = V2VConnectionReceipt(
    connected: false,
    provider: 'grok-voice',
    voiceMode: 'grok-voice-v1',
    stage: V2VConnectionStage.websocket,
    httpStatus: 403,
    errorCode: 'fixture_provider_unavailable');
const boundary = Key('review-render');
Widget app(Widget child, {required bool dark, double scale = 1}) => MaterialApp(
    theme: dark ? ellaDarkThemeData() : ellaThemeData(),
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(
            textScaler: TextScaler.linear(scale),
            padding: const EdgeInsets.only(top: 59, bottom: 34),
            viewPadding: const EdgeInsets.only(top: 59, bottom: 34),
            disableAnimations: true),
        child: RepaintBoundary(key: boundary, child: child!)),
    home: child);
double ratio(Color a, Color b) =>
    (math.max(a.computeLuminance(), b.computeLuminance()) + .05) /
    (math.min(a.computeLuminance(), b.computeLuminance()) + .05);
Future<void> shot(WidgetTester tester, String name) async {
  final directory = Platform.environment['ELLA_UX_REVIEW_SCREENSHOTS'];
  if (directory == null) return;
  await tester.pump();
  final render = tester.renderObject<RenderRepaintBoundary>(find.byKey(boundary));
  await tester.runAsync(() async {
    final image = await render.toImage(pixelRatio: 1);
    final data = await image.toByteData(format: ui.ImageByteFormat.png);
    final folder = Directory(directory)..createSync(recursive: true);
    File('${folder.path}/$name.png').writeAsBytesSync(data!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    FirebasePlatform.instance = _Core();
    FirebaseAuthPlatform.instance = _Auth();
    await (FontLoader('.SF UI Text')
          ..addFont(File('/System/Library/Fonts/SFNS.ttf').readAsBytes().then(ByteData.sublistView)))
        .load();
    await (FontLoader('InstrumentSerif')
          ..addFont(rootBundle.load('assets/fonts/instrument-serif/InstrumentSerif-Regular.ttf')))
        .load();
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('Fraunces')..addFont(rootBundle.load('assets/fonts/Fraunces-Latin-Regular.ttf'))).load();
    await (FontLoader('MaterialIcons')
          ..addFont(Future.value(ByteData.sublistView(
              File('/Users/ellaai/dev/flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf')
                  .readAsBytesSync()))))
        .load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'fictional-review'});
    await SharedPreferencesUtil.init();
  });
  for (final dark in [false, true])
    for (final scale in [1.0, 3.5]) {
      testWidgets('Welcome safe area coherent host and reachable Next dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(393, 852);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester.pumpWidget(app(EllaWelcome(onNext: () {}), dark: dark, scale: scale));
        await tester.pumpAndSettle();
        final step = find.text('Screen 1 of 3');
        expect(tester.getRect(step).top, greaterThanOrEqualTo(59));
        expect(Theme.of(tester.element(find.byType(EllaV7CompanionCard))).brightness, Brightness.light);
        await shot(tester, 'welcome-${dark ? 'dark' : 'light'}-${scale}-top');
        final next = find.widgetWithText(ElevatedButton, 'Next');
        await tester.ensureVisible(next);
        await tester.pumpAndSettle();
        final button = tester.widget<ElevatedButton>(next);
        expect(button.onPressed, isNull);
        final ink = button.style!.foregroundColor!.resolve({WidgetState.disabled})!;
        final fill = button.style!.backgroundColor!.resolve({WidgetState.disabled})!;
        expect(ratio(ink, fill), greaterThanOrEqualTo(4.5));
        expect(tester.getSize(next).height, greaterThanOrEqualTo(64));
        await shot(tester, 'welcome-${dark ? 'dark' : 'light'}-${scale}-next');
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
      testWidgets('Companion current name editor title and header return dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(393, 852);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester.pumpWidget(
            app(const Scaffold(body: SingleChildScrollView(child: EllaV7CompanionCard())), dark: dark, scale: scale));
        await tester.tap(find.byKey(const Key('v7-companion-customize')));
        await tester.pumpAndSettle();
        final name = tester.widget<TextField>(find.byKey(const Key('v7-companion-name')));
        expect(name.controller!.text, 'Lume');
        expect(find.text('Your companion'), findsOneWidget);
        await shot(tester, 'editor-${dark ? 'dark' : 'light'}-$scale-top');
        await tester.ensureVisible(find.byKey(const Key('v7-companion-save')));
        await tester.pumpAndSettle();
        await tester.ensureVisible(find.text('Your companion'));
        await tester.pumpAndSettle();
        expect(tester.getRect(find.text('Your companion')).top, greaterThanOrEqualTo(59));
        final close = find.byTooltip('Close');
        expect(tester.getRect(close).bottom, lessThanOrEqualTo(852));
        await tester.tap(close);
        await tester.pumpAndSettle();
        expect(find.byKey(const Key('v7-companion-name')), findsNothing);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
      testWidgets('Voice ordinary copy contextual surface and closed Details dark=$dark scale=$scale', (tester) async {
        tester.view.physicalSize = const Size(393, 852);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await tester
            .pumpWidget(app(const Scaffold(body: V2VFallbackDialog(receipt: receipt)), dark: dark, scale: scale));
        await tester.pumpAndSettle();
        expect(find.text('Ella’s voice couldn’t connect'), findsOneWidget);
        expect(find.textContaining('fixture_provider'), findsNothing);
        final dialog = tester.widget<AlertDialog>(find.byType(AlertDialog));
        final palette = EllaPalette(dark);
        expect(dialog.backgroundColor, palette.card);
        expect(dialog.elevation, 0);
        expect(dialog.shadowColor, Colors.transparent);
        final title = tester.widget<Text>(find.text('Ella’s voice couldn’t connect'));
        expect(ratio(title.style!.color!, palette.card), greaterThanOrEqualTo(4.5));
        await shot(tester, 'voice-${dark ? 'dark' : 'light'}-$scale-collapsed');
        final detail = find.byKey(const Key('v2v-failure-details'));
        await tester.ensureVisible(detail);
        await tester.pumpAndSettle();
        await tester.tap(find.text('Details'));
        await tester.pumpAndSettle();
        expect(find.textContaining('fixture_provider_unavailable'), findsOneWidget);
        final close = find.byKey(const ValueKey('v2v-failure-cancel'));
        await tester.ensureVisible(close);
        await tester.pumpAndSettle();
        expect(tester.widget<OutlinedButton>(close).onPressed, isNotNull);
        expect(tester.getSize(close).height, greaterThanOrEqualTo(48));
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
}
