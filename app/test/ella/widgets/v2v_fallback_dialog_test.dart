import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/v2v_client.dart';
import 'package:omi/ella/widgets/v2v_fallback_dialog.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  const receipt = V2VConnectionReceipt(
    connected: false,
    provider: 'gemini-native-live',
    voiceMode: 'gemini-native-live-v1',
    stage: V2VConnectionStage.session,
    httpStatus: 503,
    errorCode: 'isolated_voice_not_ready',
  );

  Widget buildApp() => const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: V2VFallbackDialog(receipt: receipt)),
      );

  testWidgets('names failed provider, safe receipt, and explicit ElevenLabs fallback', (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    expect(find.text("Gemini Native Live couldn't connect"), findsOneWidget);
    expect(find.textContaining('session'), findsOneWidget);
    expect(find.textContaining('HTTP 503'), findsOneWidget);
    expect(find.textContaining('isolated_voice_not_ready'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
    expect(find.text('Use ElevenLabs'), findsOneWidget);
    expect(find.text('Cancel'), findsOneWidget);
    expect(find.byKey(const ValueKey('v2v-failure-cancel')), findsOneWidget);
  });

  testWidgets('scoped failures are truthful and close-only after automatic refresh is exhausted', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: V2VFallbackDialog(receipt: receipt, allowStandardFallback: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text("Talk isn't available for this item"), findsOneWidget);
    expect(find.textContaining('Close this window'), findsOneWidget);
    expect(find.text('Use ElevenLabs'), findsNothing);
    expect(find.text('Retry'), findsNothing);
    expect(find.text('Close'), findsOneWidget);
  });

  testWidgets('explicit cancel returns stop instead of forcing another retry', (tester) async {
    V2VFailureChoice? choice;
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () async {
                choice = await showV2VFallbackDialog(context, receipt, allowStandardFallback: false);
              },
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('v2v-failure-cancel')));
    await tester.pumpAndSettle();

    expect(choice, V2VFailureChoice.stop);
    expect(find.byType(V2VFallbackDialog), findsNothing);
  });

  test('cancel closes only memory voice modals after teardown', () {
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.stop, modalPresentation: true), isTrue);
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.stop, modalPresentation: false), isFalse);
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.retry, modalPresentation: true), isFalse);
  });
}
