import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/pages/ella_invite_sent_screen.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/onboarding/auth.dart';
import 'package:omi/providers/auth_provider.dart';
import 'package:omi/widgets/consent_bottom_sheet.dart';
import 'package:omi/utils/l10n_extensions.dart';

class _AuthFixture extends ChangeNotifier implements AuthenticationProvider {
  @override
  bool loading = false;
  int privacyCalls = 0;
  int termsCalls = 0;

  @override
  void openPrivacyPolicy() => privacyCalls++;
  @override
  void openTermsOfService() => termsCalls++;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

const _boundaryKey = Key('presentation-render');

Future<void> _render(WidgetTester tester, String name) async {
  final directory = Platform.environment['ELLA_LOGIN_RENDER_DIR'];
  if (directory == null) return;
  final boundary = tester.renderObject<RenderRepaintBoundary>(find.byKey(_boundaryKey));
  await tester.runAsync(() async {
    final image = await boundary.toImage(pixelRatio: 1);
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    await Directory(directory).create(recursive: true);
    await File('$directory/$name.png').writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    await (FontLoader('Manrope')
          ..addFont(rootBundle.load('assets/fonts/Manrope-400.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-600.ttf'))
          ..addFont(rootBundle.load('assets/fonts/Manrope-700.ttf')))
        .load();
    await (FontLoader('packages/font_awesome_flutter/FontAwesomeBrands')
          ..addFont(rootBundle.load('packages/font_awesome_flutter/lib/fonts/Font-Awesome-7-Brands-Regular-400.otf')))
        .load();
    var cache = File(Platform.resolvedExecutable).parent;
    while (!File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf').existsSync()) {
      cache = cache.parent;
    }
    await (FontLoader('MaterialIcons')
          ..addFont(File('${cache.path}/artifacts/material_fonts/MaterialIcons-Regular.otf')
              .readAsBytes()
              .then(ByteData.sublistView)))
        .load();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({'givenName': 'Avery'});
    await SharedPreferencesUtil.init();
  });

  Future<_AuthFixture> pumpSurface(
    WidgetTester tester, {
    required Widget child,
    Size size = const Size(320, 568),
    double scale = 3,
    String language = 'en',
    bool loading = false,
    bool disableAnimations = false,
  }) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final fixture = _AuthFixture()..loading = loading;
    addTearDown(fixture.dispose);
    await tester.pumpWidget(
      ChangeNotifierProvider<AuthenticationProvider>.value(
        value: fixture,
        child: MaterialApp(
          theme: ellaThemeData(),
          locale: Locale(language),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, widget) => MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: TextScaler.linear(scale), disableAnimations: disableAnimations),
            child: RepaintBoundary(key: _boundaryKey, child: widget!),
          ),
          home: Scaffold(body: child),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 400));
    if (!loading) await tester.pumpAndSettle();
    return fixture;
  }

  for (final language in ['en', 'de', 'ar']) {
    for (final size in [const Size(320, 568), const Size(390, 844)]) {
      for (final scale in [1.0, 2.0, 3.0]) {
        final name = '${language}_${size.width.toInt()}_${scale.toInt()}';
        testWidgets('real sign in fits $name and retains reachable controls', (tester) async {
          await pumpSurface(tester,
              child: AuthComponent(onSignIn: () {}), size: size, scale: scale, language: language);
          await _render(tester, 'auth_$name');
          expect(tester.takeException(), isNull);
          for (final button in find.byType(ElevatedButton).evaluate()) {
            final finder = find.byWidget(button.widget);
            await tester.ensureVisible(finder);
            await tester.pumpAndSettle();
            expect(finder.hitTestable(), findsOneWidget);
            expect(tester.getSize(finder).height, greaterThanOrEqualTo(48));
          }
        });
        testWidgets('actual sent invite fits $name with readable actions', (tester) async {
          await pumpSurface(
            tester,
            child: const EllaInviteSentScreen(name: 'Alex', email: 'alex@example.test', inviteCode: 'ELLA7K9Q'),
            size: size,
            scale: scale,
            language: language,
          );
          await _render(tester, 'invite_$name');
          expect(tester.takeException(), isNull);
          for (final finder in [
            find.byKey(const Key('invite-copy')),
            find.byType(ElevatedButton),
            find.byKey(const Key('invite-done')),
          ]) {
            await tester.ensureVisible(finder);
            await tester.pumpAndSettle();
            expect(finder.hitTestable(), findsOneWidget);
            expect(tester.getSize(finder).height, greaterThanOrEqualTo(48));
          }
          await _render(tester, 'invite_actions_$name');
        });
        testWidgets('actual auth disclosure fits $name and permits cancel', (tester) async {
          await pumpSurface(
            tester,
            child: ConsentBottomSheet(authMethod: 'google', onContinue: () {}),
            size: size,
            scale: scale,
            language: language,
          );
          await _render(tester, 'consent_$name');
          expect(tester.takeException(), isNull);
          for (final key in ['consent-privacy', 'consent-terms', 'consent-continue', 'consent-cancel']) {
            final finder = find.byKey(Key(key));
            await tester.ensureVisible(finder);
            await tester.pumpAndSettle();
            expect(finder.hitTestable(), findsOneWidget);
            expect(tester.getSize(finder).height, greaterThanOrEqualTo(48));
          }
          await _render(tester, 'consent_actions_$name');
        });
      }
    }
  }

  testWidgets('busy sign in cannot open another consent sheet', (tester) async {
    await pumpSurface(tester, child: AuthComponent(onSignIn: () {}), loading: true, scale: 1);
    for (final element in find.byType(ElevatedButton).evaluate()) {
      expect((element.widget as ElevatedButton).onPressed, isNull);
    }
  });

  testWidgets('legal links remain scalable labelled 48 point commands', (tester) async {
    final fixture = await pumpSurface(tester, child: AuthComponent(onSignIn: () {}));
    final privacy = find.byKey(const Key('auth-privacy'));
    final terms = find.byKey(const Key('auth-terms'));
    for (final finder in [privacy, terms]) {
      await tester.ensureVisible(finder);
      await tester.pumpAndSettle();
      expect(tester.getSize(finder).height, greaterThanOrEqualTo(48));
      expect(finder.hitTestable(), findsOneWidget);
      await tester.tap(finder);
    }
    expect(fixture.privacyCalls, 1);
    expect(fixture.termsCalls, 1);
  });

  testWidgets('actual sign in opens disclosure; cancel never starts authentication', (tester) async {
    var signIns = 0;
    await pumpSurface(tester, child: AuthComponent(onSignIn: () => signIns++));
    final signIn = find.byType(ElevatedButton).last;
    await tester.ensureVisible(signIn);
    await tester.pumpAndSettle();
    await tester.tap(signIn);
    await tester.pumpAndSettle();
    expect(find.byType(ConsentBottomSheet), findsOneWidget);
    final cancel = find.byKey(const Key('consent-cancel'));
    await tester.ensureVisible(cancel);
    await tester.pumpAndSettle();
    await tester.tap(cancel);
    await tester.pumpAndSettle();
    expect(find.byType(ConsentBottomSheet), findsNothing);
    expect(signIns, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets('actual modal preserves full disclosure and explicit continue exactly once', (tester) async {
    var continues = 0;
    final fixture = await pumpSurface(tester, child: Builder(builder: (context) {
      return Center(
          child: ElevatedButton(
              onPressed: () {
                ConsentBottomSheet.show(context, authMethod: 'apple', onContinue: () => continues++);
              },
              child: const Text('Open')));
    }));
    await tester.tap(find.byType(ElevatedButton));
    await tester.pumpAndSettle();
    final sheetContext = tester.element(find.byType(ConsentBottomSheet));
    expect(find.text(sheetContext.l10n.ellaAuthDataDisclosure), findsOneWidget);
    for (final key in ['consent-privacy', 'consent-terms']) {
      final finder = find.byKey(Key(key));
      await tester.ensureVisible(finder);
      await tester.pumpAndSettle();
      await tester.tap(finder);
    }
    expect(fixture.privacyCalls, 1);
    expect(fixture.termsCalls, 1);
    expect(continues, 0);
    final proceed = find.byKey(const Key('consent-continue'));
    await tester.ensureVisible(proceed);
    await tester.pumpAndSettle();
    await tester.tap(proceed);
    await tester.pumpAndSettle();
    expect(continues, 1);
    expect(find.byType(ConsentBottomSheet), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('invite copies exact code and share failure is safe localized text', (tester) async {
    String? copied;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform,
        (call) async {
      if (call.method == 'Clipboard.setData') copied = (call.arguments as Map)['text'] as String;
      return null;
    });
    const shareChannel = MethodChannel('dev.fluttercommunity.plus/share');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(shareChannel,
        (call) async {
      throw PlatformException(code: 'private-detail', message: 'secret internal diagnostic');
    });
    addTearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null);
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(shareChannel, null);
    });
    await pumpSurface(tester,
        child: const EllaInviteSentScreen(name: 'Alex', email: 'alex@example.test', inviteCode: 'ELLA7K9Q'),
        language: 'de',
        disableAnimations: true);
    final copy = find.byKey(const Key('invite-copy'));
    await tester.ensureVisible(copy);
    await tester.pumpAndSettle();
    await tester.tap(copy);
    expect(copied, 'ELLA7K9Q');
    tester.state<ScaffoldMessengerState>(find.byType(ScaffoldMessenger).first).removeCurrentSnackBar();
    await tester.pumpAndSettle();
    final share = find.byType(ElevatedButton);
    await tester.ensureVisible(share);
    await tester.pumpAndSettle();
    await tester.tap(share);
    await tester.pumpAndSettle();
    final context = tester.element(find.byType(EllaInviteSentScreen));
    expect(find.text(context.l10n.wrappedFailedToShare), findsOneWidget);
    expect(find.textContaining('secret internal'), findsNothing);
    expect(find.textContaining('private-detail'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('primary and legal controls keep readable colors in pressed and focus states', (tester) async {
    await pumpSurface(tester, child: ConsentBottomSheet(authMethod: 'google', onContinue: () {}), scale: 1);
    final button = tester.widget<ElevatedButton>(find.byKey(const Key('consent-continue')));
    final legal = tester.widget<TextButton>(find.byKey(const Key('consent-privacy')));
    for (final states in [
      <WidgetState>{},
      {WidgetState.pressed},
      {WidgetState.focused}
    ]) {
      final foreground = button.style!.foregroundColor!.resolve(states)!;
      final background = button.style!.backgroundColor!.resolve(states)!;
      final contrast = (foreground.computeLuminance() + 0.05) / (background.computeLuminance() + 0.05);
      expect(contrast, greaterThan(4.5));
      expect(legal.style!.foregroundColor!.resolve(states), EllaColors.tealDeep);
    }
  });

  testWidgets('invite without code retains reachable done and returns to prior route', (tester) async {
    await pumpSurface(tester, disableAnimations: true, child: Builder(builder: (context) {
      return Center(
          child: ElevatedButton(
              onPressed: () {
                Navigator.of(context).push(MaterialPageRoute<void>(
                    builder: (_) => const EllaInviteSentScreen(name: 'Alex', email: 'alex@example.test')));
              },
              child: const Text('Open')));
    }));
    await tester.tap(find.byType(ElevatedButton));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('invite-copy')), findsNothing);
    expect(tester.widget<ScaleTransition>(find.byKey(const Key('invite-confirmation'))).scale.value, 1);
    final done = find.byKey(const Key('invite-done'));
    await tester.ensureVisible(done);
    await tester.pumpAndSettle();
    expect(done.hitTestable(), findsOneWidget);
    await tester.tap(done);
    await tester.pumpAndSettle();
    expect(find.byType(EllaInviteSentScreen), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
