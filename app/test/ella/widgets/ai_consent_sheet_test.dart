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

  Widget buildApp() => MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: AiConsentSheet(
            onAccept: () async {
              final preferences = SharedPreferencesUtil();
              preferences.uid = 'uid-a';
              preferences.acceptAiConsent(
                receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
                uid: 'uid-a',
              );
              return true;
            },
          ),
        ),
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
    expect(disclosure, contains('secure backend'));
    expect(disclosure, contains('live or stored microphone audio'));
    expect(disclosure, contains('Deepgram'));
    expect(disclosure, contains('Google Firebase'));
    expect(disclosure, contains('self-hosted Hermes'));
    expect(disclosure, contains('Honcho memory'));
    expect(disclosure, contains('OpenRouter'));
    expect(disclosure, contains('selected model provider'));
    expect(disclosure, contains('Google Gemini'));
    expect(disclosure, contains('OpenAI'));
    expect(disclosure, contains('Groq'));
    expect(disclosure, contains('xAI Grok'));
    expect(disclosure, contains('ElevenLabs'));
    expect(disclosure, contains('response text'));
    expect(disclosure, contains('selected memory context'));
    expect(disclosure, contains('will not send audio, transcripts, messages, or memory context'));
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
    expect(preferences.aiConsentProcessorSetHash, SharedPreferencesUtil.currentAiConsentProcessorSetHash);
    expect(preferences.aiConsentDeferredVersion, isEmpty);
  });

  testWidgets('Not now defers the current processor contract', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
    );

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

  testWidgets('review mode exposes revoke and deletion actions', (tester) async {
    final preferences = SharedPreferencesUtil();
    preferences.uid = 'uid-a';
    preferences.acceptAiConsent(
      receiptId: '${SharedPreferencesUtil.currentAiConsentReceiptPrefix}receipt-a',
      uid: 'uid-a',
    );
    var revokeCalled = false;

    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => AiConsentSheet.show(
                context,
                reviewMode: true,
                onDecline: () async => revokeCalled = true,
                onRequestDeletion: () async {},
              ),
              child: const Text('Review consent'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Review consent'));
    await tester.pumpAndSettle();

    expect(find.text('Revoke AI permission'), findsOneWidget);
    expect(find.text('Delete my account and data'), findsOneWidget);

    await tester.tap(find.text('Revoke AI permission'));
    await tester.pumpAndSettle();

    expect(revokeCalled, isTrue);
    expect(preferences.aiConsentAccepted, isFalse);
  });
}
