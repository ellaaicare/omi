import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/pages/ella_voice_chat_page.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/l10n/app_localizations.dart';

void main() {
  test('Demo voice fixtures never initialize speech recognition', () {
    expect(
      EllaVoiceChatPage.shouldInitializeSpeech(
        EllaVoiceDemoState(quota: EllaAccessDemoFixtures.active.quota),
      ),
      isFalse,
    );
    expect(EllaVoiceChatPage.shouldInitializeSpeech(null), isTrue);
  });

  Future<void> pumpVoice(
    WidgetTester tester, {
    required EllaVoiceDemoState state,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: EllaVoiceChatPage(demoState: state),
      ),
    );
    await tester.pump();
  }

  testWidgets('soft warning and remaining time are small gentle voice-surface states', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(quota: EllaAccessDemoFixtures.softDaily.quota),
    );

    expect(find.textContaining('left'), findsOneWidget);
    expect(find.textContaining('nearing today’s voice time'), findsOneWidget);
    expect(find.text('Demo preview — voice is not active'), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsNothing);
  });

  testWidgets('all policy outcomes have distinct claim-compliant copy', (tester) async {
    final cases = {
      EllaVoicePolicyReason.quotaDaily: 'you can talk again tomorrow',
      EllaVoicePolicyReason.quotaMonthly: 'after the monthly reset',
      EllaVoicePolicyReason.concurrent: 'End the other voice conversation',
      EllaVoicePolicyReason.suspended: 'You can still use Ella’s other features',
      EllaVoicePolicyReason.sessionMax: 'Start a new voice conversation',
    };

    for (final entry in cases.entries) {
      await pumpVoice(
        tester,
        state: EllaVoiceDemoState(
          quota: EllaAccessDemoFixtures.active.quota,
          policyReason: entry.key,
        ),
      );
      expect(find.textContaining(entry.value), findsOneWidget, reason: entry.key.name);
      expect(find.textContaining('connection needs a moment'), findsNothing, reason: entry.key.name);
    }
  });

  testWidgets('technical failure is not labeled as quota or policy denial', (tester) async {
    await pumpVoice(
      tester,
      state: EllaVoiceDemoState(
        quota: EllaAccessDemoFixtures.active.quota,
        technicalFailure: true,
      ),
    );

    expect(find.textContaining('connection needs a moment'), findsOneWidget);
    expect(find.textContaining('tomorrow'), findsNothing);
    expect(find.textContaining('monthly reset'), findsNothing);
  });
}
