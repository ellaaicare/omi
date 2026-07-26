import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/widgets/ai_consent_sheet.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  Widget buildApp() => const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: AiConsentSheet()),
      );

  testWidgets('presents as a bounded sheet with Not now always visible', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(context),
              child: const Text('Show consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Show consent'));
    await tester.pumpAndSettle();

    final screenHeight = tester.view.physicalSize.height / tester.view.devicePixelRatio;
    expect(tester.getSize(find.byType(AiConsentSheet)).height, lessThanOrEqualTo(screenHeight * 0.7));
    expect(find.text('Not now').hitTestable(), findsOneWidget);
  });

  testWidgets('names every voice processor and explains routed data before acceptance', (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    final disclosure =
        tester.widgetList<RichText>(find.byType(RichText)).map((widget) => widget.text.toPlainText()).join(' ');
    expect(disclosure, contains("Ella's secure backend"));
    expect(disclosure, contains('live microphone audio'));
    expect(disclosure, contains('Deepgram'));
    expect(disclosure, contains('OpenRouter'));
    expect(disclosure, contains('selected model provider'));
    expect(disclosure, contains('Google (Gemini)'));
    expect(disclosure, contains('OpenAI'));
    expect(disclosure, contains('Groq'));
    expect(disclosure, contains('xAI (Grok)'));
    expect(disclosure, contains('ElevenLabs'));
    expect(disclosure, contains('response text'));
    expect(disclosure, contains('selected stored memory'));
    expect(disclosure, contains('related people, topics, and dates'));
    expect(disclosure, contains('Google (Gemini) or xAI (Grok) voice processor'));
    expect(disclosure, contains('Full processor details in Privacy Policy'));
    expect(find.text('Not now'), findsOneWidget);
  });

  testWidgets('accept records the current processor contract', (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Allow and continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Allow and continue'));
    await tester.pumpAndSettle();

    final preferences = SharedPreferencesUtil();
    expect(preferences.aiConsentAccepted, isTrue);
    expect(preferences.aiConsentContractVersion, SharedPreferencesUtil.currentAiConsentContractVersion);
    expect(preferences.aiConsentDeferredVersion, isEmpty);
  });

  testWidgets('Not now defers the current processor contract', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.acceptAiConsent();

    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Not now'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();

    expect(preferences.aiConsentAccepted, isFalse);
    expect(preferences.aiConsentContractVersion, isEmpty);
    expect(preferences.isCurrentAiConsentDeferred, isTrue);
  });
}
