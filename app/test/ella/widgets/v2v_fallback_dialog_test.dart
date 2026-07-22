import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

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
    expect(find.text('Not now'), findsOneWidget);
  });
}
