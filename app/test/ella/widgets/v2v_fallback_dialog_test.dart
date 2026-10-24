import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/ella_theme.dart';
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

  Widget buildApp() => MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const Scaffold(body: V2VFallbackDialog(receipt: receipt)),
      );

  testWidgets('ordinary voice copy hides closed receipt until Details without advertising broken fallback',
      (tester) async {
    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    expect(find.text('Ella’s voice couldn’t connect'), findsOneWidget);
    expect(find.textContaining('isolated_voice_not_ready'), findsNothing);
    await tester.tap(find.text('Details'));
    await tester.pumpAndSettle();
    expect(find.textContaining('session'), findsOneWidget);
    expect(find.textContaining('HTTP 503'), findsOneWidget);
    expect(find.textContaining('isolated_voice_not_ready'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
    expect(find.text('Use ElevenLabs'), findsNothing);
    expect(find.text('Close'), findsOneWidget);
    expect(find.byKey(const ValueKey('v2v-failure-cancel')), findsOneWidget);

    final cancel = tester.widget<OutlinedButton>(find.byKey(const ValueKey('v2v-failure-cancel')));
    final retry = tester.widget<FilledButton>(find.byKey(const ValueKey('v2v-failure-retry')));
    expect(cancel.style?.foregroundColor?.resolve({}), const EllaPalette(false).ink);
    expect(retry.style?.foregroundColor?.resolve({}), EllaColors.paper);
    expect(retry.style?.backgroundColor?.resolve({}), const EllaPalette(false).ink);
    expect(tester.getSize(find.byKey(const ValueKey('v2v-failure-retry'))).width, lessThan(180));
  });

  testWidgets('transient scoped failures preserve Retry without offering unscoped fallback', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const Scaffold(body: V2VFallbackDialog(receipt: receipt, allowStandardFallback: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text("Talk isn't available for this item"), findsOneWidget);
    expect(find.textContaining('Try again'), findsOneWidget);
    expect(find.text('Use ElevenLabs'), findsNothing);
    expect(find.text('Retry'), findsOneWidget);
    expect(find.text('Close'), findsOneWidget);
  });

  testWidgets('permanent scoped contract failures are close-only', (tester) async {
    const permanentReceipt = V2VConnectionReceipt(
      connected: false,
      provider: 'grok-voice',
      voiceMode: 'grok-voice-memory-v4',
      stage: V2VConnectionStage.session,
      httpStatus: 200,
      errorCode: 'invalid_session_scope',
    );
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const Scaffold(body: V2VFallbackDialog(receipt: permanentReceipt, allowStandardFallback: false)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Use ElevenLabs'), findsNothing);
    expect(find.text('Retry'), findsNothing);
    expect(find.text('Close'), findsOneWidget);
  });

  testWidgets('explicit cancel returns stop instead of forcing another retry', (tester) async {
    V2VFailureChoice? choice;
    await tester.pumpWidget(
      MaterialApp(
        theme: ellaThemeData(),
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

  testWidgets('standard permanent failure offers close without retry or fallback', (tester) async {
    const permanent = V2VConnectionReceipt(
      connected: false,
      provider: 'grok-voice',
      voiceMode: 'grok-voice',
      stage: V2VConnectionStage.session,
      errorCode: 'ai_consent_required',
    );
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const Scaffold(body: V2VFallbackDialog(receipt: permanent)),
    ));
    expect(find.byKey(const ValueKey('v2v-failure-retry')), findsNothing);
    expect(find.byKey(const ValueKey('v2v-failure-elevenlabs')), findsNothing);
    expect(find.text('Close'), findsOneWidget);
  });

  for (final action in ['retry', 'close', 'barrier']) {
    testWidgets('real standard failure dialog returns $action choice', (tester) async {
      V2VFailureChoice? choice;
      await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
            builder: (context) => Scaffold(
                    body: TextButton(
                  onPressed: () async => choice = await showV2VFallbackDialog(context, receipt),
                  child: const Text('Open'),
                ))),
      ));
      await tester.tap(find.text('Open'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('v2v-failure-elevenlabs')), findsNothing);
      if (action == 'barrier') {
        await tester.tapAt(const Offset(4, 4));
      } else {
        await tester.tap(find.byKey(ValueKey(action == 'retry' ? 'v2v-failure-retry' : 'v2v-failure-cancel')));
      }
      await tester.pumpAndSettle();
      expect(choice, action == 'retry' ? V2VFailureChoice.retry : V2VFailureChoice.stop);
    });
  }

  testWidgets('real failure dialog remains closable at narrow width and large text', (tester) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      builder: (context, child) =>
          MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(3)), child: child!),
      home: Builder(
          builder: (context) => Scaffold(
              body: TextButton(onPressed: () => showV2VFallbackDialog(context, receipt), child: const Text('Open')))),
    ));
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.byKey(const ValueKey('v2v-failure-cancel')).hitTestable(), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('v2v-failure-cancel')));
    await tester.pumpAndSettle();
    expect(find.byType(V2VFallbackDialog), findsNothing);
  });

  test('cancel closes only memory voice modals after teardown', () {
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.stop, modalPresentation: true), isTrue);
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.stop, modalPresentation: false), isFalse);
    expect(EllaVoiceChatPage.shouldCloseRouteAfterV2VFailure(V2VFailureChoice.retry, modalPresentation: true), isFalse);
  });
}
