import 'dart:io';
import 'dart:ui' as ui;
import 'package:firebase_auth_platform_interface/firebase_auth_platform_interface.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_core_platform_interface/test.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_companion_art.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/onboarding/ella/ella_welcome.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Mirrors the repository's established Firebase SDK test mock. No real user,
/// credential, auth transport, service singleton startup, or native Firebase call.
class _WelcomeAuth extends FirebaseAuthPlatform {
  int currentUserReads = 0;
  @override
  FirebaseAuthPlatform delegateFor({required FirebaseApp app}) => this;
  @override
  FirebaseAuthPlatform setInitialValues({PigeonUserDetails? currentUser, String? languageCode}) => this;
  @override
  UserPlatform? get currentUser {
    currentUserReads++;
    return null;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final auth = _WelcomeAuth();
  setUpAll(() async {
    setupFirebaseCoreMocks();
    FirebaseAuthPlatform.instance = auth;
    await Firebase.initializeApp();
    await (FontLoader('.SF UI Text')
          ..addFont(
            File('/System/Library/Fonts/SFNS.ttf').existsSync()
                ? File('/System/Library/Fonts/SFNS.ttf').readAsBytes().then(ByteData.sublistView)
                : rootBundle.load('assets/fonts/Manrope-400.ttf'),
          ))
        .load();
    await (FontLoader(
      'InstrumentSerif',
    )..addFont(rootBundle.load('assets/fonts/instrument-serif/InstrumentSerif-Regular.ttf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(
            File(
              '${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf',
            ).readAsBytes().then(ByteData.sublistView),
          ))
        .load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({'uid': 'offline-welcome-fixture'});
    await SharedPreferencesUtil.init();
    auth.currentUserReads = 0;
  });
  for (final fixture in [
    (name: 'light', dark: false, scale: 1.0),
    (name: 'dark', dark: true, scale: 1.0),
    (name: 'light-large', dark: false, scale: 2.0),
  ]) {
    testWidgets('actual Welcome ${fixture.name}: shared style save updates hero and enrollment continues', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(393, 852);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final boundary = GlobalKey();
      var nextCalls = 0;
      await tester.pumpWidget(
        RepaintBoundary(
          key: boundary,
          child: MaterialApp(
            debugShowCheckedModeBanner: false,
            theme: fixture.dark ? ellaDarkThemeData() : ellaThemeData(),
            localizationsDelegates: AppLocalizations.localizationsDelegates,
            supportedLocales: AppLocalizations.supportedLocales,
            builder: (context, child) => MediaQuery(
              data: MediaQuery.of(
                context,
              ).copyWith(disableAnimations: true, textScaler: TextScaler.linear(fixture.scale)),
              child: child!,
            ),
            home: EllaWelcome(onNext: () => nextCalls++),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(auth.currentUserReads, 1);
      final hero = find.byWidgetPredicate((w) => w is EllaV7Lume && w.size == 160);
      expect(hero, findsOneWidget, reason: 'Welcome must show the same selected character as the app header.');
      EllaV7CompanionPainter heroPainter() => tester
          .widget<CustomPaint>(
            find.descendant(
              of: hero,
              matching: find.byWidgetPredicate((w) => w is CustomPaint && w.painter is EllaV7CompanionPainter),
            ),
          )
          .painter! as EllaV7CompanionPainter;
      expect(heroPainter().styleIndex, 0);
      Future<void> capture(String stage) async {
        const output = String.fromEnvironment(
          'WELCOME_PROOF_DIR',
          defaultValue: 'test/pages/onboarding/ella/welcome-proof',
        );
        await tester.runAsync(() async {
          Directory(output).createSync(recursive: true);
          final image = await (boundary.currentContext!.findRenderObject() as RenderRepaintBoundary).toImage();
          final bytes = (await image.toByteData(format: ui.ImageByteFormat.png))!.buffer.asUint8List();
          File('$output/welcome-${fixture.name}-$stage.png').writeAsBytesSync(bytes);
          image.dispose();
        });
      }

      await capture('initial');
      final customize = find.byKey(const Key('v7-companion-customize'));
      await tester.ensureVisible(customize);
      await tester.pumpAndSettle();
      await tester.tap(customize);
      await tester.pumpAndSettle();
      final shape = find.byKey(const Key('v7-companion-shape-2'));
      await tester.ensureVisible(shape);
      await tester.pumpAndSettle();
      await tester.tap(shape);
      await tester.pumpAndSettle();
      await capture('style-editor');
      final save = find.byKey(const Key('v7-companion-save'));
      await tester.ensureVisible(save);
      await tester.pumpAndSettle();
      await tester.tap(save);
      await tester.pumpAndSettle();
      expect(EllaV7CompanionPreferences.shared.styleIndex, 2);
      expect(heroPainter().styleIndex, 2);
      expect(auth.currentUserReads, 1, reason: 'Decorative style selection must not restart account reads.');
      await tester.ensureVisible(hero);
      await tester.pumpAndSettle();
      await capture('selected-star');
      final name = find.descendant(of: find.byType(EllaWelcome), matching: find.byType(TextField)).first;
      await tester.ensureVisible(name);
      await tester.enterText(name, 'Offline fixture');
      primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      final next = find.descendant(of: find.byType(EllaWelcome), matching: find.byType(ElevatedButton));
      await tester.ensureVisible(next);
      await tester.pumpAndSettle();
      expect(tester.getSize(next).height, greaterThanOrEqualTo(44));
      await tester.tap(next);
      await tester.pumpAndSettle();
      expect(nextCalls, 1);
      expect(SharedPreferencesUtil().givenName, 'Offline fixture');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    });
  }
}
